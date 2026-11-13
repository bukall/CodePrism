"""Vendor the pure-Python runtime dependencies into the VS Code extension.

Usage (from anywhere):  python scripts/prepare-vendor.py [--force]

Downloads wheels compatible with Python 3.10 (the extension's minimum) into
vscode-extension/bundled/vendor. Only pure-Python distributions are accepted
(chardet is pinned below 6 because newer releases ship mypyc-compiled wheels);
the result works on every platform without compilation and is reproducible
regardless of the build machine's OS. Downloads are verified against hardcoded
sha256 wheel hashes (pip --require-hashes). A current vendor directory (recorded
pins, RECORD digests and intact file contents) is skipped so packaging does not
force network I/O; pass --force to re-download.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "vscode-extension" / "bundled" / "vendor"
# Exact pins: the vsix must be reproducible across time and machines.
# Bump deliberately and re-run the integration suite after changing these.
PACKAGES = [
    "pathspec==1.1.1",
    "binaryornot==0.6.0",
    "chardet==5.2.0",
    "click==8.5.1",
    "pyperclip==1.11.0",
]
PACKAGE_NAMES = ["pathspec", "binaryornot", "chardet", "click", "pyperclip"]
# sha256 of the exact py3-none-any wheels above (pip download --python-version
# 3.10 --only-binary=:all: --no-deps). --require-hashes turns any registry
# substitution or corrupted download into a hard failure instead of code execution.
WHEEL_SHA256 = {
    "pathspec==1.1.1": "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189",
    "binaryornot==0.6.0": "900adfd5e1b821255ba7e63139b0396b14c88b9286e74e03b6f51e0200331337",
    "chardet==5.2.0": "e1cf59446890a00105fe7b7912492ea04b6e6f06d4b742b2c788469e34c82970",
    "click==8.5.1": "8bb5376d2006950386f8c6427613334a2acadc5f559cb80e638d00b4df957e37",
    "pyperclip==1.11.0": "299403e9ff44581cb9ba2ffeed69c7aa96a008622ad0c46cb575ca75b5b84273",
}
# .pyc 也必须算编译产物：能写 vendor 目录者可植入一个 mtime/size 与源码
# 匹配的 .pyc，import 时优先加载它，从而绕过逐文件 sha256（源码字节未变）。
COMPILED_SUFFIXES = ("*.pyd", "*.so", "*.dylib", "*.dll", "*.pyc")
META_NAME = "_codeprism_vendor.json"

# 独立于 vendor 目录的信任锚：每个 dist-info 的「规范化 RECORD」sha256。
# stamp、RECORD、包源码都位于同一个可写目录；只用目录内的 stamp 做锚，
# 攻击者一致改写 代码+RECORD+stamp 就能洗白，旧 stamp 的自动升级甚至会把
# 篡改后的 RECORD 固化成基线。把哈希硬编码在脚本里（与 WHEEL_SHA256 同一
# 模式）才有目录外的锚点。升级 pin 时必须同步更新，check-vendor-pins.py
# 会检查覆盖完整性。
RECORD_SHA256 = {
    "binaryornot-0.6.0.dist-info": "c7ec3ed1ba2adacee68c465f02129fb676ab5105b52cfa4e105ebd813875a181",
    "chardet-5.2.0.dist-info": "8292dc490c8820f9b394fc61e3e98ad374430417beccff53b6bab571bc22af2c",
    "click-8.5.1.dist-info": "ab8f0db25b8ca7855af1f15e1609201a762bc83257bd05dd2f18087ef90aea1c",
    "pathspec-1.1.1.dist-info": "fb2571e50787f512edd97c103b88fa908f5dedc0d5c2ea67e3b52210a386d3c4",
    "pyperclip-1.11.0.dist-info": "9ab824c0644af8ed390f643bea21a78a02d028515c32ba4f32fdfdfd00e2530b",
}


# 安装器元数据：内容由 pip 的安装方式/pip 版本决定（REQUESTED 甚至可能
# 不存在），不含任何包代码；必须排除在锚之外，否则跨 pip 版本漂移。
_INSTALLER_METADATA = frozenset({"REQUESTED", "INSTALLER", "direct_url.json"})


def _record_hash(record_bytes: bytes) -> str:
    """规范化 RECORD 哈希：只保留与包内容相关的条目。

    剔除三类「构建环境相关」的行，使同一 pin 的锚在 OS / 解释器 / pip
    版本之间稳定：
    * ``../../bin/...``、``../../Scripts/...``：console script 存根，OS 相关；
    * ``__pycache__/*.cpython-<XY>.pyc``：pip 用**宿主机**解释器编译，
      行名内嵌解释器 tag（3.14 上生成 cpython-314），跨版本必然失配；
    * dist-info 的 ``REQUESTED``/``INSTALLER``/``direct_url.json``：
      安装器痕迹，随 pip 版本/安装方式变化。

    这些行在逐文件校验中本就被跳过（.pyc/脚本），或只含安装器痕迹（不含
    包代码）。锚排除它们不代表放弃覆盖：磁盘上是否存在编译产物由
    ``_vendor_current()`` 的 ``COMPILED_SUFFIXES``/``__pycache__`` 检查单独
    强制（构建期用 ``--no-compile``，检查期一律拒绝）。
    """
    lines: list[str] = []
    for line in record_bytes.decode("utf-8").splitlines():
        if not line:
            continue
        rel = line.split(",", 1)[0]
        segments = rel.split("/")
        if rel.startswith(("../", "bin/", "Scripts/")):
            continue
        if "__pycache__" in segments or rel.endswith(".pyc"):
            continue
        if (
            len(segments) == 2
            and segments[0].endswith(".dist-info")
            and segments[1] in _INSTALLER_METADATA
        ):
            continue
        lines.append(line)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _record_digests(target: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for dist_info in target.glob("*.dist-info"):
        record = dist_info / "RECORD"
        if record.is_file():
            digests[dist_info.name] = _record_hash(record.read_bytes())
    return digests


def _upgrade_stamp(target: Path, meta: dict) -> None:
    """旧 stamp 信息性升级：原子补写 recordDigests。

    安全判定已由目录外的 ``RECORD_SHA256`` 锚承担，这里只让旧格式 stamp
    与新格式一致。用临时文件 + os.replace，避免进程中断留下截断的 stamp。
    写入失败（只读目录）不阻塞 currency check。
    """
    tmp = target / f"{META_NAME}.tmp-{os.getpid()}"
    try:
        upgraded = {**meta, "recordDigests": _record_digests(target)}
        tmp.write_text(json.dumps(upgraded, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target / META_NAME)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _strip_console_scripts(staging: Path) -> None:
    """删除 pip --target 按当前系统 sysconfig scheme 写入的脚本目录。

    POSIX scheme 落在 <target>/bin，nt scheme 落在 <target>/Scripts。两者都
    是 OS 相关的 console-script stub（chardet 带 chardetect.exe），对
    run_codeprism.py 无用，也不能随 vsix 发布；只删 bin 会在 Windows 构建机
    上残留 Scripts/，使产物与构建机 OS 相关。
    """
    for name in ("bin", "Scripts"):
        shutil.rmtree(staging / name, ignore_errors=True)


def _record_entry_intact(digest: str, size: str, file_path: Path) -> bool:
    """按 wheel RECORD 的 sha256/size 校验已落盘文件的内容。

    未知算法/空/格式错误的 digest 一律拒绝：仅靠 size 匹配无法证明内容
    完整，会让「逐文件哈希校验」的保证强于实际实现。
    """
    if not digest.startswith("sha256="):
        return False
    if not file_path.is_file():
        return False
    data = file_path.read_bytes()
    actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    if actual != digest[len("sha256=") :]:
        return False
    if size and len(data) != int(size):
        return False
    return True


def _verify_staged_records(staging: Path) -> None:
    """下载路径也必须校验锚，而不是只在 _vendor_current() 里校验。

    否则 bump pin 忘更新锚、或 pip 改变 RECORD 布局时，本次构建会「成功」
    并写入 stamp，但此后每次 ``npm run vendor`` / ``vscode:prepublish`` 都
    判为过期并联网重下——离线构建会退化成 pip 失败而非清晰提示。
    """
    digests = _record_digests(staging)
    # 按 PACKAGES 推导（与 _vendor_current() 同口径），而不是遍历锚表：
    # 新增依赖但漏配锚时也必须失败，否则会静默产出一棵「下次运行必判过期」
    # 的树——正是本函数要防的失败模式。
    for dist_info_name in _expected_dist_infos():
        expected = RECORD_SHA256.get(dist_info_name)
        if expected is None:
            raise SystemExit(
                f"No RECORD_SHA256 anchor for {dist_info_name}; "
                "add it in the same commit as the pin."
            )
        actual = digests.get(dist_info_name)
        if actual != expected:
            raise SystemExit(
                f"RECORD trust anchor mismatch for {dist_info_name}: "
                f"got {actual or 'missing'}, expected {expected}. "
                "Update RECORD_SHA256 in the same commit as the pin change."
            )


def _is_link(path: Path) -> bool:
    """符号链接 / Windows junction / 其他 reparse point。

    vendor 树是纯 wheel 解包产物，不应含任何链接。必须显式拒绝：``rglob``
    默认不递归符号链接目录，``is_file()`` 对链接目录又返回 False，否则
    指向外部的 ``vendor/<pkg>`` 会被 ``import`` 加载而完全不被枚举。
    无法读取元数据时按 fail-closed 处理。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_reparse_tag", 0))


def _record_target(rel: str) -> Path | None:
    """把 RECORD 的相对路径解析为 vendor 内路径，越界/绝对路径返回 None。

    RECORD 来自 wheel，但 currency check 的目标正是防止本地元数据被篡改：
    必须先确认解析结果仍在 TARGET 内，再读取文件内容。
    """
    try:
        resolved = (TARGET / rel).resolve()
        resolved.relative_to(TARGET.resolve())
    except (OSError, ValueError):
        return None
    return TARGET / rel


def _expected_dist_infos() -> list[str]:
    """按 PACKAGES 推导 dist-info 目录名（_vendor_current/_verify 同口径）。"""
    names: list[str] = []
    for pin in PACKAGES:
        name, _, version = pin.partition("==")
        escaped = re.sub(r"[-_.]+", "_", name).lower()
        names.append(f"{escaped}-{version}.dist-info")
    return names


def _vendor_current() -> bool:
    try:
        meta = json.loads((TARGET / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    # 合法 JSON 也可能是数组/字符串/null；不做类型守卫会让 meta.get()
    # 抛 AttributeError，把构建变成裸 traceback。
    if not isinstance(meta, dict):
        return False
    if meta.get("packages") != PACKAGES:
        return False
    record_digests = meta.get("recordDigests")
    if record_digests is not None and not isinstance(record_digests, dict):
        return False
    # 光有 stamp/文件存在不够：本地代码被改写后 npm run vendor 会跳过下载，
    # vsix 就会携带被篡改的依赖。安全判定不依赖目录内的 stamp：
    #   (1) 用硬编码在脚本里的 RECORD_SHA256 锚锁定 RECORD 本身；
    #   (2) 逐文件比对 RECORD 中的 sha256/size；
    #   (3) RECORD 路径必须解析在 TARGET 内；
    #   (4) TARGET 下不允许存在 RECORD 之外的常规文件（见函数末尾）。
    # 因此「代码+RECORD」「代码+RECORD+stamp」一致改写、旧 stamp 自动升级
    # 前的联合篡改、以及新增模块（如 pyperclip 会 import 的 qtpy）都无法通过。
    recorded: set[str] = set()
    for dist_info_name in _expected_dist_infos():
        dist_info = TARGET / dist_info_name
        if not dist_info.is_dir():
            return False
        expected = RECORD_SHA256.get(dist_info_name)
        if expected is None:
            return False
        try:
            record_bytes = (dist_info / "RECORD").read_bytes()
            if _record_hash(record_bytes) != expected:
                return False
            rows = csv.reader(record_bytes.decode("utf-8").splitlines())
            checked = 0
            for row in rows:
                if not row or not row[0]:
                    continue
                rel = row[0]
                segments = rel.split("/")
                # 文件集合比对用：所有 RECORD 行（含被跳过校验的 bin/Scripts、
                # .pyc、安装器元数据）都算「已记录」，避免正常条目被误判。
                if ".." not in segments:
                    recorded.add(rel)
                digest = row[1] if len(row) > 1 else ""
                size = row[2] if len(row) > 2 else ""
                # wheel 规范：RECORD 自身一行允许空 hash/size。
                if rel.endswith("/RECORD") and not digest and not size:
                    continue
                # 构建时刻意清理的字节码缓存、console scripts 与路径逃逸项。
                if (
                    ".." in segments
                    or rel.startswith("bin/")
                    or rel.startswith("Scripts/")
                    or rel.endswith(".pyc")
                    or "__pycache__" in segments
                ):
                    continue
                target_path = _record_target(rel)
                if target_path is None or not _record_entry_intact(digest, size, target_path):
                    return False
                checked += 1
            # 空 RECORD（或全部被跳过）证明不了任何东西：hollow dist-info
            # 不能让 vendor 目录看起来是最新的。
            if checked == 0:
                return False
        except (OSError, ValueError, csv.Error):
            return False
    # pyperclip ships as a single module, the rest as packages.
    if not all((TARGET / name).is_dir() or (TARGET / f"{name}.py").is_file() for name in PACKAGE_NAMES):
        return False
    if any(p for pattern in COMPILED_SUFFIXES for p in TARGET.rglob(pattern)):
        return False
    # 「0 pyc」是构建不变量（--no-compile + 构建期清理），检查侧必须同样
    # 强制：否则可植入 mtime/size 与源码匹配的 .pyc 绕过逐文件 sha256，
    # 让 import 加载编译产物。空 __pycache__ 目录也一并拒绝，保持不变量简单。
    if any(TARGET.rglob("__pycache__")):
        return False
    # 文件集合比对：RECORD 之外的文件（如 vendor/qtpy.py、vendor/PyQt5/）
    # 不在锚与逐文件校验的覆盖内，却位于 sys.path 上会被真实 import 执行。
    # 构建期已 strip bin/Scripts 且不该留下任何未记录文件，因此：
    #   * bin/、Scripts/ 前缀一律拒绝（其 RECORD 行在校验中被跳过，不能靠行放行）；
    #   * 其余文件必须是某个 RECORD 条目，stamp 本身除外。
    for path in TARGET.rglob("*"):
        if _is_link(path):
            # 链接目录不会被 rglob 递归、is_file() 也返回 False；不显式拒绝
            # 就等于允许一个指向外部的 import 路径。
            return False
        if not path.is_file():
            continue
        rel = path.relative_to(TARGET).as_posix()
        if rel.startswith(("bin/", "Scripts/")):
            return False
        if rel != META_NAME and rel not in recorded:
            return False
    if record_digests is None:
        _upgrade_stamp(TARGET, meta)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Vendor CodePrism's pure-Python runtime dependencies.")
    parser.add_argument("--force", action="store_true", help="Re-download even if the vendor directory is current.")
    args = parser.parse_args()
    if not args.force and _vendor_current():
        print(f"Vendor directory is current ({', '.join(PACKAGES)}); skipping download. Use --force to refresh.")
        return
    # Build into a staging directory and swap only on success: a failed pip
    # run must never leave the extension with an empty vendor directory.
    staging = Path(tempfile.mkdtemp(dir=TARGET.parent, prefix=".vendor-build-"))
    req_fd, req_name = tempfile.mkstemp(dir=TARGET.parent, prefix=".vendor-req-", suffix=".txt")
    requirements = Path(req_name)
    try:
        with os.fdopen(req_fd, "w", encoding="utf-8") as handle:
            for pin in PACKAGES:
                handle.write(f"{pin} --hash=sha256:{WHEEL_SHA256[pin]}\n")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(staging),
            "--python-version",
            "3.10",
            "--only-binary=:all:",
            # Every runtime dependency is in PACKAGES; --no-deps keeps the
            # hash lock complete (no unhashed transitive resolution).
            "--no-deps",
            # 不生成 .pyc：pip 默认用**宿主机**解释器编译，RECORD 里会写入
            # 内嵌 cpython-<XY> 标签的行，跨解释器版本破坏 RECORD 锚的稳定性。
            "--no-compile",
            "--require-hashes",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ]
        print("Running:", " ".join(cmd))
        subprocess.check_call(cmd)
        compiled = [p for pattern in COMPILED_SUFFIXES for p in staging.rglob(pattern)]
        if compiled:
            raise SystemExit(f"Refusing to vendor compiled artifacts: {compiled[:5]}")
        for cache in list(staging.rglob("__pycache__")):
            shutil.rmtree(cache, ignore_errors=True)
        _strip_console_scripts(staging)
        # 锚是目录外的信任根：下载路径也必须与它一致，不能只依赖
        # _vendor_current() 在「下一次运行」才发现漂移。
        _verify_staged_records(staging)
        # 锁定每个 RECORD 的哈希，使「同时改写代码与 RECORD」的本地篡改也
        # 不能通过 currency check。
        (staging / META_NAME).write_text(
            json.dumps({"packages": PACKAGES, "recordDigests": _record_digests(staging)}, indent=2) + "\n",
            encoding="utf-8",
        )
        # Three-step swap: move the old vendor aside first, so a failed
        # rename (file lock / antivirus) can be rolled back instead of
        # leaving the extension without any vendor directory.
        backup = TARGET.with_name(f".vendor-old-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            if TARGET.exists():
                os.rename(TARGET, backup)
            os.rename(staging, TARGET)
        except OSError:
            if backup.exists() and not TARGET.exists():
                os.rename(backup, TARGET)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        requirements.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    print(f"Vendored pure-Python dependencies into {TARGET}")


if __name__ == "__main__":
    main()
