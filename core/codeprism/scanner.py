"""Directory walker with nested ``.gitignore`` support built on pathspec.

Git semantics implemented here:

* Each directory's ``.gitignore`` applies to that directory and below, with
  paths interpreted relative to the directory containing the file.
* Deeper ``.gitignore`` files take precedence over shallower ones; within a
  single file the last matching pattern wins (pathspec handles negations).
* An ignored directory is never descended into, so files below it cannot be
  re-included by a deeper negation (matches Git behaviour). Explicitly passed
  subdirectory targets are checked against their own ignore status too.
* When the pack root lives inside a Git repository, ``.gitignore`` files from
  the repository root down to the pack root are honoured as well. Outside a
  repository, rules start at the pack root.
* Priority, highest first: hard tool invariants (``.git``, CodePrism's own
  output and write transients — never overridable), explicit user patterns
  (``--ignore-pattern`` / extension settings), the ``.gitignore`` stack, and
  built-in defaults last. A ``.gitignore`` negation may re-include a
  default-ignored noise directory (Git-faithful) but can never override a hard
  invariant or an explicit user exclusion.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pathspec import PathSpec, Pattern

from .defaults import DEFAULT_IGNORE_PATTERNS, HARD_IGNORE_PATTERNS, LOCK_FILE_PATTERNS

# pathspec 1.0+ 同时提供 gitwildmatch 与 gitignore 两个工厂，真正不可回退的
# 硬依赖是 1.0 才引入的 backend="simple"（见 make_spec）。下面的探测仅是
# 面向旧解释器/旧 pathspec 的防御性写法，不代表 0.12 仍受支持。
_PATTERN_FACTORY = "gitwildmatch"
try:
    PathSpec.from_lines("gitignore", [])
    _PATTERN_FACTORY = "gitignore"
except (LookupError, ValueError):
    pass

# Git 在 Windows/macOS 上遵循 core.ignorecase：通过给正则加 re.IGNORECASE
# 折叠大小写（也会折叠字符类，与 Git 的 WM_CASEFOLD 一致），而不是改写规则
# 文本——直接 lower 会破坏转义和字符类语法。模块级默认值不做任何 I/O。
_CASE_FOLD_DEFAULT = sys.platform in ("win32", "darwin") or os.path.normcase("A") != "A"


def _path_exists(path: os.PathLike[str] | str) -> bool:
    """存在性检查：用 ``os.path.exists`` 而非 ``Path.exists``。

    Python 3.13+ 的 ``Path.exists()`` 对超长/不可表示路径会抛
    ``OSError(ENAMETOOLONG)``，而 ``os.path.exists`` 对同样的路径稳定返回
    False。这类路径不可能含有真实的 ``.git``，视为无边界在语义上正确，
    也避免虚拟深树遍历被逐条吞掉的异常中断。
    """
    try:
        return os.path.exists(os.fspath(path))
    except (OSError, ValueError):
        return False


def _detect_case_fold(
    directory: str | os.PathLike[str] | None = None,
    warnings: list[str] | None = None,
) -> bool:
    """判断 ``directory`` 所在卷是否需要折叠忽略规则的大小写。

    CPython 既没有把 ``PC_CASE_SENSITIVE`` 放进 os.pathconf 常量表（macOS 上
    也没有前导下划线的 ``_PC_CASE_SENSITIVE`` 键），因此旧实现的 pathconf
    探测必然抛 ValueError 并被吞掉，恒返回 True。改用真实文件系统探针：在
    目标卷上创建两个仅大小写不同的名字，若小写名也能命中即为不敏感卷。
    探测传入打包根而不是 cwd，避免跨卷（外置卷/磁盘镜像）误判。

    这是本工具少数会向打包根写入的瞬态操作（目录名被默认忽略规则覆盖）。
    只读/不可写卷会回退到 macOS 默认（不敏感）并发出警告；清理失败同样
    告警，便于残留目录被人工发现。清理只用 unlink/rmdir，不依赖
    ``shutil.rmtree``（后者内部使用 os.scandir，在虚拟文件系统测试中会被
    mock 拦截而残留）。
    """
    if sys.platform == "win32":
        return True
    if sys.platform != "darwin":
        return os.path.normcase("A") != "A"
    base = os.fspath(directory) if directory is not None else os.getcwd()
    probe = ""
    marker = ""
    try:
        probe = tempfile.mkdtemp(prefix=".codeprism-case-", dir=base)
        marker = os.path.join(probe, "Aa")
        with open(marker, "wb"):
            pass
        return os.path.exists(os.path.join(probe, "aa"))
    except OSError as exc:
        if warnings is not None:
            warnings.append(
                f"Cannot probe case sensitivity on {base} ({exc}); assuming a case-insensitive volume"
            )
        return True
    finally:
        if probe:
            try:
                if marker and os.path.exists(marker):
                    os.unlink(marker)
                os.rmdir(probe)
            except OSError as exc:
                if warnings is not None:
                    warnings.append(f"Cannot remove case-sensitivity probe {probe}: {exc}")


def make_spec(
    lines: Sequence[str],
    *,
    source: str | None = None,
    warnings: list[str] | None = None,
    case_insensitive: bool | None = None,
) -> PathSpec:
    """Compile gitignore-style lines into one spec.

    ``case_insensitive`` 为 None 时用模块级保守默认值；Scanner 会显式传入
    按打包根所在卷实测的结果，覆盖默认值（例如大小写敏感的 APFS 卷）。

    With ``warnings``, each bad line is skipped individually (Git behaviour:
    one malformed line must not discard its neighbours) and reported with its
    location; without it the first invalid line raises, which is what callers
    of user-supplied patterns want. ``re.error`` is caught alongside
    ``ValueError`` because pathspec surfaces illegal character ranges such as
    ``[z-a]`` as ``re.PatternError``, which is not a ``ValueError``.
    """
    fold = _CASE_FOLD_DEFAULT if case_insensitive is None else case_insensitive
    patterns: list[Pattern] = []
    for lineno, line in enumerate(lines, 1):
        try:
            # Force the simple regex backend: optional re2/hyperscan backends
            # precompile patterns at construction, which would silently
            # disable the case-fold rewrite below on machines that have them.
            part = PathSpec.from_lines(_PATTERN_FACTORY, [line], backend="simple")
        except (ValueError, re.error) as exc:
            if warnings is None:
                raise
            location = f"{source}:{lineno}" if source else f"line {lineno}"
            warnings.append(f"Invalid ignore pattern at {location} skipped: {exc}")
            continue
        patterns.extend(part.patterns)
    spec = PathSpec(patterns, backend="simple")
    if fold:
        for pattern in spec.patterns:
            regex = getattr(pattern, "regex", None)
            if regex is not None:
                pattern.regex = re.compile(regex.pattern, regex.flags | re.IGNORECASE)
    return spec


@dataclass(frozen=True)
class ScannedFile:
    abs_path: Path
    rel_path: str  # POSIX-style, relative to the pack root


def _norm(path: object) -> str:
    # Hot-path key for paths the caller already resolved (walk entries under a
    # resolved root, resolved targets). realpath is only needed to expand
    # Windows 8.3 short names (RUNNER~1); the "~" probe keeps the common case
    # syscall-free. A false positive merely pays for one extra realpath.
    text = os.path.normcase(str(path))
    if "~" in text:
        return os.path.normcase(os.path.realpath(text))
    return text


_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def _is_junction(entry: os.DirEntry) -> bool:
    # DirEntry.is_junction() only exists on Python >= 3.12. On older Windows
    # interpreters check the reparse tag instead of the REPARSE_POINT file
    # attribute: the attribute also matches OneDrive/cloud placeholder files,
    # which are ordinary files and must not be silently dropped.
    if hasattr(entry, "is_junction"):
        return entry.is_junction()
    if os.name == "nt":
        try:
            return entry.stat(follow_symlinks=False).st_reparse_tag == _IO_REPARSE_TAG_MOUNT_POINT
        except (OSError, AttributeError):
            return False
    return False


class Scanner:
    def __init__(
        self,
        root: Path,
        *,
        use_gitignore: bool = True,
        ignore_lock_files: bool = True,
        extra_patterns: Sequence[str] = (),
        exclude_paths: Sequence[Path] = (),
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"Not a directory: {self.root}")
        # 打包根可能是 .git 内部的任意层级（例如 .git/hooks），只判断
        # root.name 会漏掉仓库元数据文件；任何一段名为 .git 都拒绝。
        # 精确小写先做零 I/O 预拒绝：macOS 的卷探针会在打包根创建临时目录，
        # 守卫本应阻止的目录不应被写入（顺序：精确 .git → 探测卷 → 变体）。
        if any(part == ".git" for part in self.root.parts):
            raise ValueError("Refusing to pack a .git directory (repository metadata).")
        # 构造期告警单独保存：scan() 每次会清空 warnings，需重新带回，
        # 否则只读卷回退/探针清理失败的提示会在第一次扫描时丢失。
        self.warnings: list[str] = []
        self._init_warnings: list[str] = []
        # 以打包根所在卷为准探测大小写敏感性，并显式下传给所有 spec，
        # 覆盖模块级默认值（大小写敏感的 APFS 卷不应折叠规则）。
        self._case_fold = _detect_case_fold(self.root, self._init_warnings)
        # 大小写不敏感卷上 .GIT/.Git 与 .git 是同一目录（Git 自身写小写，
        # 但跨系统拷贝或人为改名会留下变体）；必须先探测卷才知道该不该折叠。
        if self._case_fold and any(part.lower() == ".git" for part in self.root.parts):
            raise ValueError("Refusing to pack a .git directory (repository metadata).")
        self._hard_spec = make_spec(HARD_IGNORE_PATTERNS, case_insensitive=self._case_fold)
        self._user_spec = (
            make_spec(extra_patterns, case_insensitive=self._case_fold) if extra_patterns else None
        )
        patterns = list(DEFAULT_IGNORE_PATTERNS)
        if ignore_lock_files:
            patterns.extend(LOCK_FILE_PATTERNS)
        self._default_spec = make_spec(patterns, case_insensitive=self._case_fold)
        self._use_gitignore = use_gitignore
        # Stack of (absolute directory, spec); deepest entry last.
        self._specs: list[tuple[Path, PathSpec]] = []
        # User-supplied excludes may be symlinks or 8.3 short names: resolve
        # them fully once so they match the resolved keys used during the walk.
        self._exclude = {os.path.normcase(os.path.realpath(str(p))) for p in exclude_paths}
        self._files: dict[str, ScannedFile] = {}

    def scan(self, targets: Sequence[Path]) -> list[ScannedFile]:
        """Collect files from the given targets, deduplicated and sorted."""
        self._files.clear()
        # 每次扫描都从干净状态开始：若上一次 _walk 因非 OSError 异常
        # （KeyboardInterrupt/SystemExit/MemoryError）中断，深层 .gitignore
        # 的 spec 会滞留在栈里，复用同一 Scanner 实例会污染忽略结果。
        self._specs.clear()
        self.warnings.clear()
        self.warnings.extend(self._init_warnings)
        ceiling_pushed = self._push_repo_ancestors()
        try:
            for target in targets:
                self._scan_target(Path(target).resolve())
        finally:
            self._pop(ceiling_pushed)
        return sorted(self._files.values(), key=lambda f: f.rel_path)

    def _scan_target(self, resolved: Path) -> None:
        # 内部 API 契约：显式目标不存在时必须抛 FileNotFoundError，不能因
        # 边界检查前置而退化成静默跳过。api.pack() 会在更早处校验，但直接
        # 复用 Scanner.scan() 的库调用方同样依赖该语义。
        if not _path_exists(resolved):
            raise FileNotFoundError(f"Input path does not exist: {resolved}")
        # 嵌套仓库边界必须最先判断。对边界内的目标，只判断「边界目录自身 +
        # 其上层祖先」是否被外层排除/忽略：既不 preload 嵌套仓库内部的
        # .gitignore（可能含非 UTF-8 字节或坏规则而误报 warning），也不把
        # 外层的文件级规则（如 *.py、src/）套用到内部路径。与 _walk 在
        # nested 边界即停止的行为保持一致。
        boundary = self._nested_repo_boundary(resolved, is_dir=resolved.is_dir())
        if boundary is not None:
            boundary_dir = self.root / boundary
            # 用户显式排除的既可能是边界内的目标，也可能是整个边界目录；
            # 两种都应静默（与 _walk 对被忽略目录不告警一致）。
            if _norm(resolved) in self._exclude or _norm(boundary_dir) in self._exclude:
                return
            pushed = self._preload_ancestors(boundary_dir.parent)
            try:
                if self._is_ignored(boundary, is_dir=True) or self._ancestors_ignored(boundary_dir.parent):
                    return  # 刻意被忽略/排除的仓库保持静默
                self.warnings.append(f"Nested repository skipped (pack it separately): {boundary}")
            finally:
                self._pop(pushed)
            return
        if resolved.is_dir():
            if _norm(resolved) in self._exclude:
                return
            if resolved == self.root:
                self._walk(resolved, "")
                return
            rel = self._rel_to_root(resolved)
            pushed = self._preload_ancestors(resolved.parent)
            try:
                # Git refuses to add an ignored directory even when named
                # explicitly; keep the "never descend" invariant for targets.
                if self._is_ignored(rel, is_dir=True) or self._ancestors_ignored(resolved.parent):
                    self.warnings.append(f"Skipped ignored target: {rel}")
                    return
                self._walk(resolved, rel)
            finally:
                self._pop(pushed)
        elif resolved.is_file():
            rel = self._rel_to_root(resolved)
            pushed = self._preload_ancestors(resolved.parent)
            try:
                if _norm(resolved) in self._exclude:
                    return
                if self._ancestors_ignored(resolved.parent) or self._is_ignored(rel, is_dir=False):
                    self.warnings.append(f"Skipped ignored target: {rel}")
                    return
                self._files[_norm(resolved)] = ScannedFile(resolved, rel)
            finally:
                self._pop(pushed)
        else:
            raise FileNotFoundError(f"Input path does not exist: {resolved}")

    def _nested_repo_boundary(self, resolved: Path, *, is_dir: bool) -> str | None:
        """返回 root 与目标之间第一个含 ``.git`` 的嵌套仓库边界。

        与 ``_walk`` 的硬边界保持一致：显式多选目标也不得进入嵌套仓库，
        更不得把外层仓库的 .gitignore 规则套用到其内部。root 自身的 .git
        是正常仓库元数据，不在检查范围内（相当于 ``_walk`` 从子目录开始）。
        由 ``_scan_target`` 在任何 preload/忽略判定之前调用；边界内目标的
        exclude/ignore 判定只针对边界目录本身，刻意隐藏的目录保持静默。
        """
        start = resolved if is_dir else resolved.parent
        try:
            parts = start.relative_to(self.root).parts
        except ValueError:
            return None
        current = self.root
        for part in parts:
            current = current / part
            if _path_exists(current / ".git"):
                return current.relative_to(self.root).as_posix()
        return None

    def _rel_to_root(self, resolved: Path) -> str:
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Input path is outside the pack root ({self.root}): {resolved}") from exc

    def _ancestors_ignored(self, parent: Path) -> bool:
        """True when any ancestor directory of an explicit target is ignored.

        Mirrors Git: a file below an ignored directory stays ignored even if a
        deeper .gitignore negates it. Each ancestor is evaluated only against
        specs at or above itself (the ``ceiling``), so a deeper negation can
        never resurrect an ignored ancestor.
        """
        current = self.root
        rel = ""
        for part in parent.relative_to(self.root).parts:
            current = current / part
            rel = f"{rel}/{part}" if rel else part
            if self._is_ignored(rel, is_dir=True, ceiling=current):
                return True
        return False

    def _push_repo_ancestors(self) -> int:
        """Load .gitignore specs above the pack root, up to the Git repo root."""
        if not self._use_gitignore:
            return 0
        # The pack root being its own repository is a hard boundary: an
        # enclosing repository's rules must not leak into a nested repo.
        if _path_exists(self.root / ".git"):
            return 0
        repo_root = None
        current = self.root
        while current.parent != current:
            parent = current.parent
            if _path_exists(parent / ".git"):
                repo_root = parent
                break
            current = parent
        if repo_root is None or repo_root == self.root:
            return 0
        chain = [repo_root]
        node = repo_root
        for part in self.root.relative_to(repo_root).parts[:-1]:
            node = node / part
            chain.append(node)
        pushed = 0
        for directory in chain:
            if self._push_spec(directory):
                pushed += 1
        return pushed

    def _preload_ancestors(self, directory: Path) -> int:
        """Push .gitignore specs from the pack root down to ``directory``."""
        pushed = 0
        current = self.root
        if self._push_spec(current):
            pushed += 1
        for part in directory.relative_to(self.root).parts:
            current = current / part
            if self._push_spec(current):
                pushed += 1
        return pushed

    def _pop(self, count: int) -> None:
        if count:
            del self._specs[len(self._specs) - count:]

    def _push_spec(self, directory: Path) -> bool:
        if not self._use_gitignore:
            return False
        gitignore = directory / ".gitignore"
        try:
            # os.path.isfile 对超长/不可表示路径稳定返回 False（不抛、不告警）；
            # Path.is_file() 在部分解释器上会抛 ENAMETOOLONG，污染 warnings。
            if not os.path.isfile(gitignore):
                return False
            raw = gitignore.read_bytes()
        except OSError as exc:
            self.warnings.append(f"Cannot read {gitignore}: {exc}")
            return False
        text = raw.decode("utf-8-sig", errors="replace")
        if "\ufffd" in text:
            self.warnings.append(f"Non-UTF-8 bytes in {gitignore}; replacement characters may alter patterns")
        spec = make_spec(
            text.splitlines(),
            source=str(gitignore),
            warnings=self.warnings,
            case_insensitive=self._case_fold,
        )
        self._specs.append((directory, spec))
        return True

    def _walk(self, directory: Path, rel_dir: str) -> None:
        # Explicit stack (enter/leave frames) instead of recursion: pathological
        # directory depth must not hit Python's recursion limit. Leave frames
        # pop the directory's .gitignore spec after its subtree is processed.
        stack: list[tuple[str, Path | int, str]] = [("enter", directory, rel_dir)]
        while stack:
            kind, payload, rel = stack.pop()
            if kind == "leave":
                self._pop(int(payload))
                continue
            current = Path(payload)
            pushed = self._push_spec(current)
            stack.append(("leave", pushed, ""))
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                self.warnings.append(f"Cannot read directory {current}: {exc}")
                continue
            entries.sort(key=lambda entry: entry.name)
            # Reversed push + LIFO pop preserves sorted processing order.
            for entry in reversed(entries):
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                try:
                    # Symlinks and Windows junctions are skipped entirely:
                    # avoids cycles and keeps output deterministic.
                    if entry.is_symlink() or _is_junction(entry):
                        continue
                    key = _norm(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if key in self._exclude or self._is_ignored(child_rel, is_dir=True):
                            continue
                        child = Path(entry.path)
                        # A nested repository is a hard boundary: the outer
                        # repo's rules must not leak in (Git treats submodule
                        # contents as separate). Pack it separately. Checked
                        # after exclude/ignore so deliberately hidden directories
                        # stay silent instead of warning.
                        if _path_exists(child / ".git"):
                            self.warnings.append(f"Nested repository skipped (pack it separately): {child_rel}")
                            continue
                        stack.append(("enter", child, child_rel))
                    elif entry.is_file(follow_symlinks=False):
                        if key in self._exclude:
                            continue
                        if not self._is_ignored(child_rel, is_dir=False):
                            self._files[key] = ScannedFile(Path(entry.path), child_rel)
                except OSError as exc:
                    self.warnings.append(f"Cannot access {entry.path}: {exc}")

    def _is_ignored(self, rel: str, *, is_dir: bool, ceiling: Path | None = None) -> bool:
        suffix = "/" if is_dir else ""
        path = rel + suffix
        result = self._hard_spec.check_file(path)
        if result.include is not None:
            return bool(result.include)
        if self._user_spec is not None:
            result = self._user_spec.check_file(path)
            if result.include is not None:
                return bool(result.include)
        # String-based, normcase'd ancestry check: Path.relative_to() can raise
        # ValueError on case-mismatched prefixes on Windows, which would
        # silently drop an ancestor .gitignore.
        target = os.path.normcase(str(self.root / rel))
        ceiling_norm = os.path.normcase(str(ceiling)) if ceiling is not None else None
        for directory, spec in reversed(self._specs):
            base = os.path.normcase(str(directory))
            # 目录自身的 .gitignore 只管辖其内部条目，不能判定该目录本身。
            # 否则 relpath(target, base) 得到 "."，会用 "./" 去匹配自身规则；
            # 当前 pathspec 对 "./" 恰好返回 None，但正确性是靠实现细节而非
            # 契约，显式跳过以保证任何版本/后端下都符合 Git 语义。
            if target == base:
                continue
            # A filesystem-root base ("/" or "C:\") already ends with the
            # separator; appending another would never match anything.
            prefix = base if base.endswith(os.sep) else base + os.sep
            if ceiling_norm is not None and base != ceiling_norm and not ceiling_norm.startswith(prefix):
                continue
            if target != base and not target.startswith(prefix):
                continue
            sub = os.path.relpath(target, base).replace(os.sep, "/")
            result = spec.check_file(sub + suffix)
            if result.include is not None:
                return bool(result.include)
        result = self._default_spec.check_file(path)
        return bool(result.include)
