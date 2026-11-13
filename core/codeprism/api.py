"""Public orchestration API: scan -> classify -> render."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from .classifier import ClassifiedFile, classify
from .defaults import DEFAULT_MAX_FILE_BYTES, TRUNCATE_CONTENT_PATTERNS
from .markdown_renderer import render_markdown
from .scanner import Scanner, make_spec

DEFAULT_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class PackStats:
    project_name: str
    source_paths: list[str]
    sources_all_directories: bool
    total_files: int
    text_files: int
    binary_files: int
    total_characters: int
    total_lines: int
    truncated_files: int = 0
    size_limited_files: int = 0
    max_file_bytes: int = 0


@dataclass(frozen=True)
class PackResult:
    markdown: str
    stats: PackStats
    files: list[ClassifiedFile]
    warnings: list[str] = field(default_factory=list)


def resolve_root(paths: Sequence[Path]) -> Path:
    """Common root directory for the inputs (files contribute their parent)."""
    if not paths:
        raise ValueError("No input paths provided")
    resolved = [Path(p).resolve() for p in paths]
    dirs = [p if p.is_dir() else p.parent for p in resolved]
    if len(dirs) == 1:
        return dirs[0]
    try:
        common = os.path.commonpath([str(d) for d in dirs])
    except ValueError as exc:
        raise ValueError(f"Input paths must share a common root (same drive on Windows): {exc}") from exc
    return Path(common)


def pack(
    paths: Sequence[Path],
    *,
    output_path: Path | None = None,
    ignore_lock_files: bool = True,
    use_gitignore: bool = True,
    extra_ignore_patterns: Sequence[str] = (),
    exclude_paths: Sequence[Path] = (),
    truncate_patterns: Sequence[str] = (),
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> PackResult:
    inputs = [Path(p) for p in paths] or [Path(".")]
    for path in inputs:
        if not path.exists():
            raise FileNotFoundError(f"Input path does not exist: {path}")
    if preview_chars < 0:
        raise ValueError("preview_chars must be >= 0")
    if max_file_bytes < 0:
        raise ValueError("max_file_bytes must be >= 0 (0 disables the limit)")
    # Library callers get the same friendly contract as the CLI: invalid
    # user patterns raise ValueError, never a bare re.PatternError.
    for label, values in (
        ("extra_ignore_patterns", extra_ignore_patterns),
        ("truncate_patterns", truncate_patterns),
    ):
        for value in values:
            try:
                make_spec([value])
            except (ValueError, re.error) as exc:
                raise ValueError(f"Invalid {label} {value!r}: {exc}") from exc
    root = resolve_root(inputs)
    project_name = root.name or str(root)
    sources = [str(p.resolve()) for p in inputs]
    all_directories = all(p.is_dir() for p in inputs)

    scanner = Scanner(
        root,
        use_gitignore=use_gitignore,
        ignore_lock_files=ignore_lock_files,
        extra_patterns=extra_ignore_patterns,
        exclude_paths=[*([output_path] if output_path is not None else []), *exclude_paths],
    )
    scanned = scanner.scan(inputs)

    files: list[ClassifiedFile] = []
    warnings = list(scanner.warnings)
    for scanned_file in scanned:
        try:
            classified = classify(
                scanned_file,
                max_file_bytes=max_file_bytes,
                preview_chars=preview_chars,
                warnings=warnings,
            )
        except OSError as exc:
            warnings.append(f"Skipped {scanned_file.rel_path}: {exc}")
            continue
        files.append(classified)

    # Truncation is a content-value heuristic, not Git semantics: match
    # case-insensitively on every platform (without lowering the patterns,
    # which would corrupt character classes such as [A-Z]).
    truncate_spec = make_spec(
        [*TRUNCATE_CONTENT_PATTERNS, *truncate_patterns], case_insensitive=True
    )
    for index, file in enumerate(files):
        if file.is_binary or file.text is None or file.size_limited:
            continue
        if truncate_spec.check_file(file.rel_path).include:
            preview = file.text[:preview_chars]
            omitted = len(file.text) - len(preview)
            if omitted > 0:
                files[index] = replace(
                    file,
                    text=preview,
                    char_count=len(preview),
                    line_count=len(preview.splitlines()),
                    truncated=True,
                    omitted_chars=omitted,
                )

    text_files = [f for f in files if not f.is_binary]
    binary_files = [f for f in files if f.is_binary]
    stats = PackStats(
        project_name=project_name,
        source_paths=sources,
        sources_all_directories=all_directories,
        total_files=len(files),
        text_files=len(text_files),
        binary_files=len(binary_files),
        total_characters=sum(f.char_count for f in text_files),
        total_lines=sum(f.line_count for f in text_files),
        truncated_files=sum(1 for f in files if f.truncated and not f.size_limited),
        size_limited_files=sum(1 for f in files if f.size_limited),
        max_file_bytes=max_file_bytes,
    )
    markdown = render_markdown(project_name, sources, files, stats)
    return PackResult(markdown=markdown, stats=stats, files=files, warnings=warnings)
