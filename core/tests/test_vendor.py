"""Regression tests for the vendored-dependency integrity checks.

These construct a minimal in-memory vendor tree instead of depending on
``vscode-extension/bundled/vendor`` (a git-ignored build artifact that does
not exist in the core CI jobs).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "codeprism_prepare_vendor_under_test", REPO_ROOT / "scripts" / "prepare-vendor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


class VendorIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()
        self.tmp = Path(tempfile.mkdtemp(prefix="codeprism-vendor-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pins = ["fake==1.0"]
        for patcher in (
            mock.patch.object(self.module, "TARGET", self.tmp),
            mock.patch.object(self.module, "PACKAGES", self.pins),
            mock.patch.object(self.module, "PACKAGE_NAMES", ["fake"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.tmp / "fake").mkdir()
        self.code = self.tmp / "fake" / "__init__.py"
        self.code.write_bytes(b"VALUE = 1\n")
        self.dist = self.tmp / "fake-1.0.dist-info"
        self.dist.mkdir()
        self.metadata = self.dist / "METADATA"
        self.metadata.write_bytes(b"Name: fake\nVersion: 1.0\n")
        self.record = self.dist / "RECORD"
        self.meta = self.tmp / self.module.META_NAME
        self._write_record()
        self._write_meta()
        # 夹具用假包，需给目录外信任锚一个对应的合法值；任何对 RECORD
        # 的改写都会与它失配（正是外部锚要证明的行为）。
        anchor = mock.patch.object(
            self.module,
            "RECORD_SHA256",
            {self.dist.name: self.module._record_hash(self.record.read_bytes())},
        )
        anchor.start()
        self.addCleanup(anchor.stop)

    def _write_record(self, extra: str = "") -> None:
        rows = [
            f"fake/__init__.py,{_digest(self.code.read_bytes())},{self.code.stat().st_size}",
            f"fake-1.0.dist-info/METADATA,{_digest(self.metadata.read_bytes())},{self.metadata.stat().st_size}",
            "fake-1.0.dist-info/RECORD,,",
            "../../bin/chardetect.exe,sha256=AAAA,10",
        ]
        if extra:
            rows.append(extra)
        self.record.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def _write_meta(self) -> None:
        digests = {self.dist.name: self.module._record_hash(self.record.read_bytes())}
        self.meta.write_text(
            json.dumps({"packages": self.pins, "recordDigests": digests}), encoding="utf-8"
        )

    def test_current_when_intact(self):
        self.assertTrue(self.module._vendor_current())

    def test_tampered_code_is_detected(self):
        # 同长度改写：只检查文件存在/大小的实现会漏掉。
        self.code.write_bytes(b"VALUE = 2\n")
        self.assertFalse(self.module._vendor_current())

    def test_tampered_record_is_detected(self):
        # 同时改写代码与 RECORD（逐文件校验会通过），目录外的 RECORD_SHA256
        # 信任锚仍会拒绝（stamp 位于同一可写目录，不再是安全边界）。
        self.code.write_bytes(b"VALUE = 2\n")
        self._write_record()
        self.assertFalse(self.module._vendor_current())

    def test_non_object_meta_is_rejected_without_traceback(self):
        # 合法 JSON 但非对象：必须安静地判为过期，不能抛 AttributeError。
        for payload in ("[]", "null", '"stamp"', "3"):
            self.meta.write_text(payload, encoding="utf-8")
            self.assertFalse(self.module._vendor_current())

    def test_hollow_record_is_rejected(self):
        self.record.write_text("fake-1.0.dist-info/RECORD,,\n", encoding="utf-8")
        self._write_meta()
        self.assertFalse(self.module._vendor_current())

    def test_legacy_stamp_without_digests_still_verifies_files(self):
        # 旧 stamp 没有 recordDigests：逐文件哈希仍生效，且会自动补写 digests。
        self.meta.write_text(json.dumps({"packages": self.pins}), encoding="utf-8")
        self.assertTrue(self.module._vendor_current())
        self.assertIn("recordDigests", json.loads(self.meta.read_text(encoding="utf-8")))
        self.code.write_bytes(b"VALUE = 2\n")
        self.assertFalse(self.module._vendor_current())

    def test_legacy_stamp_upgrade_locks_record_against_joint_tampering(self):
        # 旧 stamp 自愈后，「同时改写代码与 RECORD」也必须被拒绝，而不是
        # 需要 --force 联网重下载才生效。
        self.meta.write_text(json.dumps({"packages": self.pins}), encoding="utf-8")
        self.assertTrue(self.module._vendor_current())
        self.code.write_bytes(b"VALUE = 2\n")
        self._write_record()
        self.assertFalse(self.module._vendor_current())

    def test_tamper_before_stamp_upgrade_is_not_laundered(self):
        # 评审实测的窗口：旧 stamp（无 recordDigests）时，若代码与 RECORD
        # 在首次自愈「之前」已被一致改写，不得被洗白成可信基线。
        self.meta.write_text(json.dumps({"packages": self.pins}), encoding="utf-8")
        self.code.write_bytes(b"VALUE = 2\n")
        self._write_record()
        self.assertFalse(self.module._vendor_current())
        self.assertNotIn("recordDigests", json.loads(self.meta.read_text(encoding="utf-8")))

    def test_triple_tampering_rejected_by_external_anchor(self):
        # 代码 + RECORD + stamp 的 recordDigests 全部一致改写也无效：
        # 锚硬编码在脚本里，不在可写的 vendor 目录内。
        self.code.write_bytes(b"VALUE = 2\n")
        self._write_record()
        self._write_meta()
        self.assertFalse(self.module._vendor_current())

    def test_unknown_or_empty_digest_is_rejected(self):
        # 仅靠 size 匹配的 size-only 校验不成立。
        self.assertFalse(self.module._record_entry_intact("", "11", self.code))
        self.assertFalse(self.module._record_entry_intact("md5=AAAA", "11", self.code))
        self.assertTrue(
            self.module._record_entry_intact(
                _digest(self.code.read_bytes()), str(self.code.stat().st_size), self.code
            )
        )

    def test_record_hash_stable_across_interpreter_tags(self):
        # pip 在安装阶段用宿主机解释器写 .pyc 行（行名内嵌 cpython-<XY>），
        # 锚必须在 3.10/3.12/3.13/3.14 之间稳定，否则非 3.14 的 CI 腿
        # 会永远判 vendor 过期、离线跳过失效。
        def record(tag: str) -> bytes:
            return (
                "fake/__init__.py,sha256=AAAA,11\n"
                f"fake/__pycache__/__init__.cpython-{tag}.pyc,,\n"
                "fake-1.0.dist-info/METADATA,sha256=BBBB,24\n"
                "fake-1.0.dist-info/RECORD,,\n"
            ).encode()

        hashes = {self.module._record_hash(record(tag)) for tag in ("310", "312", "313", "314")}
        self.assertEqual(len(hashes), 1, hashes)

    def test_record_hash_ignores_installer_metadata(self):
        # REQUESTED/INSTALLER/direct_url.json 是安装器痕迹，随 pip 版本/
        # 安装方式变化，且不含包代码。
        with_meta = (
            b"fake/__init__.py,sha256=AAAA,11\n"
            b"fake-1.0.dist-info/REQUESTED,sha256=CCCC,0\n"
            b"fake-1.0.dist-info/INSTALLER,sha256=DDDD,4\n"
            b"fake-1.0.dist-info/RECORD,,\n"
        )
        without = b"fake/__init__.py,sha256=AAAA,11\nfake-1.0.dist-info/RECORD,,\n"
        self.assertEqual(self.module._record_hash(with_meta), self.module._record_hash(without))

    def test_pyc_artifact_is_rejected(self):
        # 植入一个与源码 mtime/size 匹配的 .pyc 可以让 import 加载编译产物、
        # 而源码字节保持不变（逐文件 sha256 仍通过）。检查侧必须拒绝任何
        # 编译产物，把「0 pyc」从构建期清理升级为自强制不变量。
        cache = self.tmp / "fake" / "__pycache__"
        cache.mkdir()
        (cache / "__init__.cpython-314.pyc").write_bytes(b"\x00" * 16)
        self.assertFalse(self.module._vendor_current())

    def test_unrecorded_file_is_rejected(self):
        # vendor 在 run_codeprism.py 里被插入 sys.path：未记录的新增模块
        # （如 pyperclip 会尝试 import 的 qtpy）会被真实导入执行，必须由
        # 文件集合比对拒绝。
        (self.tmp / "qtpy.py").write_text("VALUE = 9\n", encoding="utf-8")
        self.assertFalse(self.module._vendor_current())

    def test_symlink_directory_is_rejected(self):
        # rglob 默认不递归符号链接目录、is_file() 对其返回 False；若不显式
        # 拒绝，指向外部的 vendor/<pkg> 会被 sys.path 上的 import 执行。
        outside = self.tmp.parent / f"{self.tmp.name}-outside"
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "__init__.py").write_text("VALUE = 9\n", encoding="utf-8")
        link = self.tmp / "evilpkg"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:  # Windows 需开发者模式/管理员
            self.skipTest(f"cannot create symlink here: {exc}")
        self.assertFalse(self.module._vendor_current())

    def test_console_script_file_is_rejected_even_with_record_line(self):
        # bin/Scripts 前缀的 RECORD 行在校验中被跳过（构建期已 strip），
        # 因此必须由文件集合比对拒绝这些目录出现——即使有合法的哈希行。
        script = self.tmp / "bin" / "x.py"
        script.parent.mkdir()
        script.write_text("VALUE = 9\n", encoding="utf-8")
        self._write_record(extra=f"bin/x.py,{_digest(script.read_bytes())},{script.stat().st_size}")
        self._write_meta()
        anchor = {self.dist.name: self.module._record_hash(self.record.read_bytes())}
        with mock.patch.object(self.module, "RECORD_SHA256", anchor):
            self.assertFalse(self.module._vendor_current())

    def test_staged_records_require_anchor_for_every_pin(self):
        # 新增依赖但漏配锚时必须在构建期失败，而不是产出一棵「下次运行
        # 必判过期」的树（_vendor_current 同口径遍历 PACKAGES）。
        with mock.patch.object(self.module, "PACKAGES", self.pins + ["newpkg==2.0"]):
            with self.assertRaises(SystemExit):
                self.module._verify_staged_records(self.tmp)

    def test_staged_records_must_match_anchor(self):
        # 下载路径也校验锚：bump pin 忘更新锚时立即失败，而不是静默写入
        # 一个「下次运行必判过期」的树。
        self.module._verify_staged_records(self.tmp)
        with mock.patch.object(self.module, "RECORD_SHA256", {self.dist.name: "0" * 64}):
            with self.assertRaises(SystemExit):
                self.module._verify_staged_records(self.tmp)

    def test_record_path_cannot_escape_vendor_root(self):
        self.assertIsNone(self.module._record_target(str(self.tmp.parent / "escape.txt")))
        self.assertIsNone(self.module._record_target("../escape.txt"))
        self.assertEqual(self.module._record_target("fake/__init__.py"), self.code)

    def test_stamp_upgrade_leaves_no_temp_file(self):
        self.meta.write_text(json.dumps({"packages": self.pins}), encoding="utf-8")
        self.assertTrue(self.module._vendor_current())
        self.assertIn("recordDigests", json.loads(self.meta.read_text(encoding="utf-8")))
        self.assertEqual(list(self.tmp.glob(f"{self.module.META_NAME}.tmp-*")), [])

    def test_console_script_dirs_removed_for_both_schemes(self):
        for name in ("bin", "Scripts"):
            directory = self.tmp / name
            directory.mkdir()
            (directory / "chardetect.exe").write_bytes(b"stub")
        self.module._strip_console_scripts(self.tmp)
        self.assertFalse((self.tmp / "bin").exists())
        self.assertFalse((self.tmp / "Scripts").exists())


if __name__ == "__main__":
    unittest.main()
