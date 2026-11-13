"""Text/binary classification and safe text decoding.

Binary detection is policy-driven, in this order: a known binary extension
(binaryornot's list), a 512-byte content sniff, then a NUL byte anywhere in
the content actually read. The extension rule is deliberate: a text-looking
payload named .png is inventoried, not inlined. Binary payloads are never
read in full: only their path, size and type label are recorded.
Non-UTF-8 text is re-encoded to UTF-8 via chardet detection, with a warning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from binaryornot.helpers import has_binary_extension, is_binary_string
from chardet.universaldetector import UniversalDetector

from .defaults import binary_type_for
from .scanner import ScannedFile

_DETECT_SAMPLE_BYTES = 1024 * 1024
# Local classification budget, decoupled from binaryornot internals. Matches
# binaryornot 0.6's CHUNK_SIZE; re-verify when bumping that pin. Classification
# always sniffs up to this many bytes (a constant, bounded I/O cost); the
# preview/full-read budgets below are separate and user-configurable.
_SNIFF_BYTES = 512


@dataclass(frozen=True)
class ClassifiedFile:
    abs_path: Path
    rel_path: str
    is_binary: bool
    size_bytes: int
    binary_type: str | None
    text: str | None
    char_count: int
    line_count: int
    truncated: bool = False
    omitted_chars: int = 0
    size_limited: bool = False

    @property
    def extension(self) -> str:
        name = self.rel_path.rsplit("/", 1)[-1]
        dot = name.rfind(".")
        return name[dot:].lower() if dot > 0 else ""


def _binary_record(file: ScannedFile, size: int) -> ClassifiedFile:
    return ClassifiedFile(
        abs_path=file.abs_path,
        rel_path=file.rel_path,
        is_binary=True,
        size_bytes=size,
        binary_type=binary_type_for(file.rel_path),
        text=None,
        char_count=0,
        line_count=0,
    )


def classify(
    file: ScannedFile,
    *,
    max_file_bytes: int = 0,
    preview_chars: int = 200,
    warnings: list[str] | None = None,
) -> ClassifiedFile:
    # One open file descriptor for size, sniffing and content: a file swapped
    # between separate stat/open calls cannot make the statistics, the binary
    # verdict and the packed bytes disagree. Reads are capped at the initial
    # fstat size so a file growing mid-read (active log) cannot inflate the
    # snapshot beyond its recorded size.
    with open(file.abs_path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if has_binary_extension(file.abs_path):
            return _binary_record(file, size)
        head = handle.read(_SNIFF_BYTES)
        if is_binary_string(head):
            return _binary_record(file, size)
        if max_file_bytes > 0 and size > max_file_bytes:
            # Never read oversized files in full: decode only a leading chunk
            # (4 bytes per char covers worst-case UTF-8), capped at the size
            # limit itself so a huge preview setting cannot break the contract.
            # preview_chars=0 意味着正文完全省略：不再切出 4 字节去猜测
            # 编码，否则会基于这几字节发出一条毫无意义的重编码警告。
            cap = 0 if preview_chars == 0 else min(max(0, preview_chars) * 4 + 4, max_file_bytes)
            raw = (head + handle.read(max(0, cap - len(head))))[:cap]
            if raw.endswith(b"\r"):
                # Peek at the next byte: only drop the \r when it is the first
                # half of a cut CRLF pair. A lone CR (classic Mac line ending)
                # is a real terminator and must survive normalisation.
                nxt = head[cap : cap + 1] if cap < len(head) else handle.read(1)
                if nxt == b"\n":
                    raw = raw[:-1]
            if b"\x00" in raw:
                return _binary_record(file, size)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    # The chunk cut a multi-byte character; drop the partial
                    # tail instead of letting chardet guess a wrong encoding.
                    text = raw[: exc.start].decode("utf-8")
                else:
                    text, encoding, confidence, lossy = _decode_raw(raw, tail_cut=True)
                    # 纯 tail-cut 的多字节截断由 _decode_raw 内部处理（lossy
                    # 为 False，不告警）；但预览块「彻底不可解码」时正文已发生
                    # U+FFFD 替换，必须像完整读取路径一样告警。
                    _warn_encoding(warnings, file.rel_path, encoding, confidence, lossy=lossy)
            text = _normalise(text)[: max(0, preview_chars)]
            return ClassifiedFile(
                abs_path=file.abs_path,
                rel_path=file.rel_path,
                is_binary=False,
                size_bytes=size,
                binary_type=None,
                text=text,
                char_count=len(text),
                line_count=len(text.splitlines()),
                truncated=True,
                omitted_chars=0,
                size_limited=True,
            )
        raw = (head + handle.read(max(0, size - len(head))))[:size]
    if b"\x00" in raw:
        # binaryornot only sniffs the first chunk; a NUL later in the file
        # still means binary payload (and truncates C-string clipboards).
        return _binary_record(file, size)
    text, encoding, confidence, lossy = _decode_raw(raw, tail_cut=False)
    _warn_encoding(warnings, file.rel_path, encoding, confidence, lossy=lossy)
    text = _normalise(text)
    return ClassifiedFile(
        abs_path=file.abs_path,
        rel_path=file.rel_path,
        is_binary=False,
        size_bytes=size,
        binary_type=None,
        text=text,
        char_count=len(text),
        line_count=len(text.splitlines()),
    )


def _warn_encoding(
    warnings: list[str] | None,
    rel_path: str,
    encoding: str | None,
    confidence: float,
    *,
    lossy: bool = False,
) -> None:
    if warnings is None:
        return
    if encoding and encoding.lower() not in ("utf-8", "utf8", "ascii"):
        message = (
            f"Re-encoded {rel_path} from {encoding} (chardet confidence {confidence:.2f}); "
            "content may be mis-decoded if the guess is wrong"
        )
        if lossy:
            message += "; invalid bytes were replaced with U+FFFD (decoding loss)"
        warnings.append(message)
    elif lossy:
        warnings.append(f"Possible decoding loss in {rel_path}: invalid bytes replaced with U+FFFD")


def _normalise(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_raw(raw: bytes, *, tail_cut: bool) -> tuple[str, str | None, float, bool]:
    """Decode bytes, returning (text, detected_encoding, confidence, lossy).

    UTF-8 first, then chardet guess, then lossy UTF-8. ``encoding`` is None
    for the UTF-8 passthrough. ``lossy`` is True only when the final
    errors="replace" fallback ran: a U+FFFD already present in the source is
    content, not decoding loss. ``tail_cut`` marks a deliberately truncated
    preview chunk: only then may an incomplete trailing character be dropped
    silently. For complete files the same bytes are genuine corruption and
    must surface as U+FFFD so the decoding-loss warning fires.
    """
    try:
        return raw.decode("utf-8"), None, 1.0, False
    except UnicodeDecodeError:
        pass
    detector = UniversalDetector()
    try:
        detector.feed(raw[:_DETECT_SAMPLE_BYTES])
    finally:
        detector.close()
    result = detector.result or {}
    encoding = result.get("encoding")
    confidence = float(result.get("confidence") or 0.0)
    text = None
    if encoding:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            # A preview chunk can end mid-character in the detected encoding
            # too; retry without the partial tail before giving up to lossy
            # UTF-8 (which would mojibake the whole preview). Known reason
            # strings: UTF-8 "unexpected end of data", GBK/Big5/CP932
            # "incomplete multibyte sequence", UTF-16/32 "truncated data".
            # The near-end start fallback covers other codecs/versions.
            if tail_cut and (
                exc.reason in ("unexpected end of data", "incomplete multibyte sequence", "truncated data")
                or exc.start >= len(raw) - 4
            ):
                try:
                    text = raw[: exc.start].decode(encoding)
                except UnicodeDecodeError:
                    text = None
            else:
                text = None
        except LookupError:
            text = None
    if text is None:
        # 猜测的编码并没有生效（解码失败或 codec 不存在），最终走的是
        # errors="replace" 的 UTF-8 回退。不要把未生效的 encoding 交给
        # _warn_encoding，否则告警会谎称 "Re-encoded ... from <猜测编码>"；
        # 正确信息是「解码损失 / U+FFFD 替换」。
        return raw.decode("utf-8", errors="replace"), None, 0.0, True
    return text, encoding, confidence, False


def decode_text(raw: bytes, *, tail_cut: bool = False) -> str:
    """Decode bytes as text and normalise line endings to LF.

    A leading UTF-8 BOM is stripped (encoding metadata, not content) and
    CRLF/CR are folded to LF so the generated document and its statistics are
    identical across OSes.
    """
    text, _encoding, _confidence, _lossy = _decode_raw(raw, tail_cut=tail_cut)
    return _normalise(text)


def format_size(size_bytes: int) -> str:
    """Human readable size using 1024-based units (e.g. ``18.4 KB``)."""
    size = float(size_bytes)
    if size < 1024.0:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"  # pragma: no cover - loop always returns
