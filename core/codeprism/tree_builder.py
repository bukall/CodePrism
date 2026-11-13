"""Box-drawing directory tree with non-text asset annotations.

Entries are sorted deterministically: directories first, then files, each
group alphabetically (case-insensitive with a case-sensitive tie-break), so
identical inputs always produce an identical tree.
"""

from __future__ import annotations

from .classifier import ClassifiedFile, format_size
from .display import sanitize_name

TreeNode = dict[str, "TreeNode"] | ClassifiedFile


def build_tree(project_name: str, files: list[ClassifiedFile]) -> str:
    root: dict[str, TreeNode] = {}
    for file in files:
        parts = file.rel_path.split("/")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"File/directory name conflict in tree at {part}")
            node = child
        node[parts[-1]] = file
    label = sanitize_name(project_name)
    # A filesystem-root project name may already carry a trailing separator.
    lines = [label if label.endswith(("/", "\\")) else f"{label}/"]
    _render(root, "", lines)
    return "\n".join(lines)


def _sorted_entries(node: dict[str, TreeNode]) -> list[tuple[str, TreeNode]]:
    return sorted(
        node.items(),
        key=lambda item: (0 if isinstance(item[1], dict) else 1, item[0].lower(), item[0]),
    )


def _render(node: dict[str, TreeNode], prefix: str, lines: list[str]) -> None:
    # Explicit stack: pathological directory depth must not hit Python's
    # recursion limit (the scanner is iterative for the same reason).
    stack: list[tuple[list[tuple[str, TreeNode]], int, str]] = [(_sorted_entries(node), 0, prefix)]
    while stack:
        entries, index, prefix = stack[-1]
        if index >= len(entries):
            stack.pop()
            continue
        stack[-1] = (entries, index + 1, prefix)
        name, child = entries[index]
        is_last = index == len(entries) - 1
        connector = "└── " if is_last else "├── "
        display = sanitize_name(name)
        if isinstance(child, dict):
            lines.append(f"{prefix}{connector}{display}/")
            stack.append((_sorted_entries(child), 0, prefix + ("    " if is_last else "│   ")))
        else:
            suffix = ""
            if child.is_binary:
                suffix = f" [Non-Text: {sanitize_name(child.extension) or 'no ext'}, {format_size(child.size_bytes)}]"
            elif child.size_limited:
                suffix = f" [Size-limited: first {child.char_count} chars]"
            elif child.truncated:
                total = child.char_count + child.omitted_chars
                suffix = f" [Truncated: first {child.char_count} of {total} chars]"
            lines.append(f"{prefix}{connector}{display}{suffix}")
