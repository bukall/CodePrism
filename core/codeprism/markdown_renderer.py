"""Assemble the final Markdown document.

Every fenced block uses a dynamically computed fence: one more backtick than
the longest backtick run inside the wrapped content (minimum three), so source
files that themselves contain Markdown code fences can never break out of
their container.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from .classifier import format_size
from .defaults import language_for
from .display import code_span, sanitize_name
from .tree_builder import build_tree

if TYPE_CHECKING:
    from .api import PackStats
    from .classifier import ClassifiedFile

_BACKTICK_RUN = re.compile(r"`+")


def _table_cell(text: str) -> str:
    # GFM splits a cell on any pipe preceded by an even number of backslashes
    # (\\ pairs consume; a lone \| escapes the pipe). Emit every pipe as \|
    # and top up an odd backslash run with one more backslash so the emitted
    # run is always odd: even runs render byte-faithfully, odd runs render one
    # extra backslash (unavoidable), and lone backslashes stay literal inside
    # the code span.
    out: list[str] = []
    run = 0
    for char in code_span(text):
        if char == "|":
            if run % 2 == 1:
                out.append("\\")
            out.append("\\|")
            run = 0
        else:
            out.append(char)
            run = run + 1 if char == "\\" else 0
    return "".join(out)


def fence_for(content: str) -> str:
    """Return an opening/closing fence that cannot be escaped by ``content``."""
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(content)), default=0)
    return "`" * max(3, longest + 1)


def render_markdown(
    project_name: str,
    sources: list[str],
    files: list["ClassifiedFile"],
    stats: "PackStats",
) -> str:
    text_files = [f for f in files if not f.is_binary]
    binary_files = [f for f in files if f.is_binary]

    parts: list[str] = []
    title_name = project_name if len(sources) == 1 else " + ".join(Path(s).name or s for s in sources)
    parts.append(f"# CodePrism Context: {sanitize_name(title_name)}")
    parts.append("")
    parts.append("## 1. Summary")
    source_display = ", ".join(code_span(source) for source in sources)
    if stats.sources_all_directories:
        source_label = "Source Directory" if len(sources) == 1 else "Source Directories"
    else:
        source_label = "Source Paths"
    parts.append(f"- **{source_label}**: {source_display}")
    parts.append(
        f"- **Total Files**: {stats.total_files} "
        f"({stats.text_files} text files, {stats.binary_files} non-text files)"
    )
    parts.append(
        f"- **Total Characters / Lines**: {stats.total_characters} chars / {stats.total_lines} lines"
    )
    if stats.truncated_files:
        parts.append(
            f"- **Truncated Files**: {stats.truncated_files} (content preview only, see notices below)"
        )
    if stats.size_limited_files:
        parts.append(
            f"- **Size-Limited Files**: {stats.size_limited_files} "
            f"(over {format_size(stats.max_file_bytes)}, leading preview only)"
        )
    parts.append("")

    # The tree root is the real common pack root: with several sources the
    # title enumerates them while the tree keeps the actual containing dir.
    tree = build_tree(project_name, files)
    tree_fence = fence_for(tree)
    parts.append("## 2. File Tree")
    parts.append(f"{tree_fence}text")
    parts.append(tree)
    parts.append(tree_fence)
    parts.append("")

    parts.append("## 3. Non-Text Files (Binary & Media Assets)")
    if binary_files:
        parts.append("| File Path | Extension | Size | Type |")
        parts.append("| :--- | :--- | :--- | :--- |")
        for file in binary_files:
            path_cell = _table_cell(file.rel_path)
            ext_cell = _table_cell(file.extension) if file.extension else "`-`"
            parts.append(
                f"| {path_cell} | {ext_cell} "
                f"| {format_size(file.size_bytes)} | {file.binary_type} |"
            )
    else:
        parts.append("_No non-text files detected._")
    parts.append("")

    parts.append("## 4. Text Files Content")
    parts.append("")
    if text_files:
        for file in text_files:
            text = file.text if file.text is not None else ""
            parts.append(f"### {code_span(file.rel_path)}")
            preview_only = file.char_count == 0 and (file.truncated or file.size_limited)
            if not preview_only:
                fence = fence_for(text)
                language = language_for(file.rel_path)
                parts.append(fence + language)
                # "\n".join() re-adds exactly one newline before the closing
                # fence; drop one here so trailing newlines stay byte-faithful
                # and files without a final newline still close cleanly.
                body = text[:-1] if text.endswith("\n") else text
                if text:
                    parts.append(body)
                parts.append(fence)
            if file.size_limited:
                parts.append(
                    f"> [CodePrism] Size limit {format_size(stats.max_file_bytes)}: "
                    f"file is {format_size(file.size_bytes)}; showing first {file.char_count} characters only."
                )
            elif file.truncated:
                total = file.char_count + file.omitted_chars
                parts.append(
                    f"> [CodePrism] Content truncated: showing first {file.char_count} of {total} "
                    f"characters ({file.omitted_chars} omitted). Full content at {code_span(file.rel_path)}."
                )
            parts.append("")
    else:
        parts.append("_No text files detected._")
        parts.append("")

    return "\n".join(parts)
