"""Display-safe rendering of file names and paths inside Markdown.

File names are attacker-controlled input in the sense that a crafted name
(backticks, newlines on POSIX, pipes) must never break the document structure
or inject headings/instructions into the generated LLM context.
"""

from __future__ import annotations

import re

_BACKTICK_RUN = re.compile(r"`+")
# C0/DEL, C1, line/paragraph separators, Unicode bidi/zero-width controls and
# lone surrogates (POSIX surrogateescape filenames): all rendered visibly so a
# crafted name cannot spoof or reflow the document. Backticks are NOT escaped
# here; code_span() picks a fence the name's backticks cannot close.
_CONTROL_CHAR = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u061c\u180e\u2028\u2029\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2060\ufeff\ud800-\udfff]"
)
_VISIBLE_ESCAPE = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def sanitize_name(text: str) -> str:
    """Render control characters visibly so names stay on a single line."""

    def replace(match: re.Match) -> str:
        char = match.group(0)
        if char in _VISIBLE_ESCAPE:
            return _VISIBLE_ESCAPE[char]
        return f"\\u{ord(char):04x}"

    return _CONTROL_CHAR.sub(replace, text)


def code_span(text: str) -> str:
    """Wrap ``text`` in a CommonMark code span that its backticks cannot close."""
    safe = sanitize_name(text)
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(safe)), default=0)
    fence = "`" * (longest + 1)
    # Padding is required when delimiters would merge with content backticks,
    # and when both ends are spaces (CommonMark strips one space from each end
    # of such a span, which would silently rename " x " to "x").
    if safe.startswith("`") or safe.endswith("`") or (safe.startswith(" ") and safe.endswith(" ")):
        return f"{fence} {safe} {fence}"
    return f"{fence}{safe}{fence}"
