"""Assert the exact vendor pins satisfy codeprism-core's dependency ranges.

The library (core/pyproject.toml) declares compatible ranges for consumers;
the extension's reproducibility comes from scripts/prepare-vendor.py's exact
pins. This check keeps the two in sync in both directions: every pin must
fall inside the matching library range, and every library dependency must
have a pin. Requires Python 3.11+ (tomllib) and `packaging`
(pip install packaging).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if sys.version_info < (3, 11):
        print("check-vendor-pins.py requires Python 3.11+ (tomllib).")
        return 1
    import tomllib

    try:
        from packaging.requirements import Requirement
    except ImportError:
        print("Missing development dependency: pip install packaging")
        return 1

    spec = importlib.util.spec_from_file_location("prepare_vendor", REPO_ROOT / "scripts" / "prepare-vendor.py")
    if spec is None or spec.loader is None:
        print("Cannot load scripts/prepare-vendor.py")
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pyproject = tomllib.loads((REPO_ROOT / "core" / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(dep) for dep in pyproject["project"]["dependencies"]]

    pins: dict[str, str] = {}
    for pin in module.PACKAGES:
        name, sep, version = pin.partition("==")
        if not sep or not re.fullmatch(r"[0-9][0-9A-Za-z.\-+!]*", version):
            print(f"Vendor pin {pin!r} is not an exact name==version pin.")
            return 1
        pins[name.lower()] = version

    for name, version in pins.items():
        matching = [r for r in requirements if r.name.lower() == name]
        if not matching or not all(r.specifier.contains(version) for r in matching):
            print(f"Vendor pin {name}=={version} violates core/pyproject.toml dependency ranges.")
            return 1
    for req in requirements:
        if req.name.lower() not in pins:
            print(f"Library dependency {req.name} has no vendor pin in scripts/prepare-vendor.py.")
            return 1
    # 目录外的 RECORD 信任锚必须与 pin 同步更新，否则 currency check 会把
    # 每次构建都判为过期（或更糟：漏掉新增依赖）。
    anchors = getattr(module, "RECORD_SHA256", None)
    if not isinstance(anchors, dict):
        print("prepare-vendor.py has no RECORD_SHA256 trust anchor map.")
        return 1
    for pin in module.PACKAGES:
        name, _, version = pin.partition("==")
        escaped = re.sub(r"[-_.]+", "_", name).lower()
        dist_info = f"{escaped}-{version}.dist-info"
        value = anchors.get(dist_info)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            print(f"Missing/invalid RECORD_SHA256 trust anchor for {dist_info}.")
            return 1
    # 本地存在 vendor 时顺带校验锚值：CI 的 pin 检查发生在 vendor 生成之前，
    # 目录不存在就跳过；本地开发则能立即发现「改了 pin/锚但没重建 vendor」。
    vendor = REPO_ROOT / "vscode-extension" / "bundled" / "vendor"
    if vendor.is_dir():
        digests = module._record_digests(vendor)
        for dist_info, expected in anchors.items():
            actual = digests.get(dist_info)
            if actual != expected:
                print(
                    f"RECORD_SHA256 anchor for {dist_info} does not match the local vendor tree "
                    f"(got {actual or 'missing'}). Rebuild the vendor tree and update the anchor "
                    "in the same commit."
                )
                return 1
    print(
        f"All {len(pins)} vendor pins satisfy the library ranges, cover every dependency, "
        "have RECORD trust anchors, and the local vendor tree (if present) matches them."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
