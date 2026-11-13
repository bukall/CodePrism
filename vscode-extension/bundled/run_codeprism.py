"""Entry point used by the CodePrism VS Code extension.

Runs the bundled copy of the core engine with vendored dependencies, so the
user does not need to pip-install anything. Requires Python 3.10+.
"""

import os
import sys

# tree_builder.py 在模块级使用 PEP 604 联合类型别名，Python < 3.10 会在
# import 阶段抛裸 TypeError。扩展侧已探测 >=3.10 才 spawn，但手动运行本
# 入口时也给出可操作的提示，而不是一段看不懂的 traceback。
if sys.version_info < (3, 10):
    sys.stderr.write(
        "CodePrism requires Python 3.10 or newer; found "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.\n"
        "Set codeprism.pythonPath in VS Code Settings to a Python 3.10+ interpreter.\n"
    )
    sys.exit(1)

# The extension install directory may be read-only; never litter it with
# __pycache__ bytecode.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
sys.path.insert(0, HERE)

from codeprism.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
