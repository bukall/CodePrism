"""CodePrism core: a deterministic codebase-to-Markdown context packer."""

from __future__ import annotations

__version__ = "0.1.0"

from .api import PackResult, PackStats, pack, resolve_root

__all__ = ["PackResult", "PackStats", "pack", "resolve_root", "__version__"]
