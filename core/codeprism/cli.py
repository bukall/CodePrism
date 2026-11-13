"""Command line interface for the CodePrism core engine."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import click

from . import __version__
from .api import DEFAULT_PREVIEW_CHARS, PackResult, pack, resolve_root
from .defaults import DEFAULT_MAX_FILE_BYTES
from .scanner import make_spec

# 三种输出路径共用的 256 MiB UTF-8 字节预算：扩展的文件复制按文件字节数
# 计算、PythonRunner 的 stdout 缓冲按 Buffer.byteLength 计算、CLI --copy 按
# UTF-8 编码后的字节数计算。超出时文档照常生成，只是不进剪贴板。
_CLIPBOARD_MAX_BYTES = 256 * 1024 * 1024


def _over_clipboard_budget(markdown: str) -> bool:
    """UTF-8 字节预算判定，先用字符数做廉价下界筛。

    UTF-8 字节数恒 >= Python 字符数，因此字符数已超限时无需再物化整份
    bytes（那会让超预算文档的内存瞬时翻倍）。
    """
    return len(markdown) > _CLIPBOARD_MAX_BYTES or (
        len(markdown.encode("utf-8")) > _CLIPBOARD_MAX_BYTES
    )


def _force_utf8_stdio() -> None:
    # Windows consoles may default to a legacy code page and CRLF translation;
    # pin both streams to UTF-8 + LF so stdout and file output are identical.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError, OSError):
            pass


def _silence_broken_pipe() -> None:
    # 下游管道已关闭：把 stdout/stderr 都指向 devnull，避免解释器退出时
    # 再打一条 "Exception ignored" 的二次 traceback。文件模式下末尾的
    # 统计/警告/JSON 行走 stderr 或 stdout，任一管道关闭都要覆盖。
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, stream.fileno())
            os.close(devnull)
        except OSError:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def _is_codeprism_document(path: Path) -> bool:
    try:
        # Bounded read: a multi-GB single-line file must not be slurped just
        # to decide whether overwriting it is allowed.
        with open(path, "rb") as handle:
            return handle.read(64).decode("utf-8", errors="replace").startswith("# CodePrism Context:")
    except OSError:
        return False


def _output_mode(output_path: Path) -> int:
    # Preserve an existing document's permissions; otherwise honour umask
    # instead of mkstemp's private 0600 or a blanket world-readable 0644.
    if output_path.exists():
        return output_path.stat().st_mode & 0o777
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def _stats_to_dict(result: PackResult, output_path: Path | None, copied: bool) -> dict:
    return {
        "projectName": result.stats.project_name,
        "sourcePaths": result.stats.source_paths,
        "totalFiles": result.stats.total_files,
        "textFiles": result.stats.text_files,
        "binaryFiles": result.stats.binary_files,
        "totalCharacters": result.stats.total_characters,
        "totalLines": result.stats.total_lines,
        "truncatedFiles": result.stats.truncated_files,
        "sizeLimitedFiles": result.stats.size_limited_files,
        "maxFileBytes": result.stats.max_file_bytes,
        "sourcesAllDirectories": result.stats.sources_all_directories,
        "warnings": result.warnings,
        "outputPath": str(output_path.resolve()) if output_path is not None else None,
        "copied": copied,
    }


@click.command(name="codeprism", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
)
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output .md file. Defaults to <project root>/codeprism-context.md.",
)
@click.option("--copy", is_flag=True, help="Also copy the generated Markdown to the system clipboard.")
@click.option("--stdout", "to_stdout", is_flag=True, help="Print Markdown to stdout instead of writing a file.")
@click.option(
    "--json-stats",
    is_flag=True,
    help="Print generation stats as a single JSON line (stdout, or stderr when combined with --stdout).",
)
@click.option("--include-lock-files", is_flag=True, help="Do not ignore dependency lock files.")
@click.option("--no-gitignore", is_flag=True, help="Skip .gitignore rules (built-in default ignores still apply).")
@click.option(
    "--ignore-pattern",
    "extra_patterns",
    multiple=True,
    help="Extra gitignore-style ignore pattern; repeatable.",
)
@click.option(
    "--exclude-path",
    "exclude_paths",
    multiple=True,
    type=click.Path(path_type=Path),
    help=(
        "Exact file or directory path to exclude, matched literally (no glob syntax). "
        "Relative paths resolve against the current directory; prefer absolute paths. Repeatable."
    ),
)
@click.option(
    "--truncate-pattern",
    "truncate_patterns",
    multiple=True,
    help="Extra content-truncation pattern (LICENSE-like files); repeatable.",
)
@click.option(
    "--preview-chars",
    type=click.IntRange(min=0),
    default=DEFAULT_PREVIEW_CHARS,
    show_default=True,
    help="Characters kept for truncated files; 0 omits their content entirely.",
)
@click.option(
    "--max-file-bytes",
    type=click.IntRange(min=0),
    default=DEFAULT_MAX_FILE_BYTES,
    show_default=True,
    help="Text files above this size are preview-only (never read in full); 0 disables the limit.",
)
@click.option("--force", is_flag=True, help="Allow overwriting an existing file that CodePrism did not generate.")
@click.version_option(version=__version__, prog_name="codeprism")
def main(
    paths: tuple[Path, ...],
    output: Path | None,
    copy: bool,
    to_stdout: bool,
    json_stats: bool,
    include_lock_files: bool,
    no_gitignore: bool,
    extra_patterns: tuple[str, ...],
    exclude_paths: tuple[Path, ...],
    truncate_patterns: tuple[str, ...],
    preview_chars: int,
    max_file_bytes: int,
    force: bool,
) -> None:
    """Pack a project directory (or selected files/directories) into one Markdown file.

    PATHS defaults to the current directory. Text files are included verbatim
    (line endings normalised to LF, BOM stripped); non-text files are listed
    with path, size and type instead of their bytes. LICENSE-like and oversized
    files are preview-truncated with an explicit notice.
    """
    _force_utf8_stdio()
    if to_stdout and output is not None:
        raise click.UsageError("--stdout and --output cannot be combined.")
    # User-supplied patterns fail fast with a friendly message instead of a
    # raw pathspec/re traceback halfway through the pack.
    for label, values in (("--ignore-pattern", extra_patterns), ("--truncate-pattern", truncate_patterns)):
        for value in values:
            try:
                make_spec([value])
            except (ValueError, re.error) as exc:
                raise click.ClickException(f"Invalid {label} {value!r}: {exc}") from exc

    inputs: list[Path] = list(paths) or [Path(".")]
    try:
        root = resolve_root(inputs)
        output_path = output or root / "codeprism-context.md"
        # Refuse before scanning: a rejected overwrite must not pay for a full
        # walk of the project first.
        if not to_stdout and output_path.exists() and not stat.S_ISREG(output_path.stat().st_mode):
            raise click.ClickException(
                f"Refusing to write to {output_path}: destination is not a regular file."
            )
        if not to_stdout and output_path.exists() and not force and not _is_codeprism_document(output_path):
            raise click.ClickException(
                f"Refusing to overwrite {output_path}: existing file was not generated by CodePrism. "
                "Use --force to override."
            )
        if not to_stdout:
            # Fail before the expensive walk if the destination is unusable.
            output_path.parent.mkdir(parents=True, exist_ok=True)
        result = pack(
            inputs,
            output_path=None if to_stdout else output_path,
            ignore_lock_files=not include_lock_files,
            use_gitignore=not no_gitignore,
            extra_ignore_patterns=extra_patterns,
            exclude_paths=exclude_paths,
            truncate_patterns=truncate_patterns,
            preview_chars=preview_chars,
            max_file_bytes=max_file_bytes,
        )
        if to_stdout:
            # Raw write, no click.echo: source files may legitimately contain
            # ANSI escapes and --stdout must be byte-identical to the file
            # output, piped or not.
            try:
                sys.stdout.write(result.markdown)
                sys.stdout.flush()
            except BrokenPipeError:
                # Unix 管道约定（如 `codeprism --stdout | head`）：静默退出。
                _silence_broken_pipe()
                raise SystemExit(0) from None
        else:
            # Atomic replace: a cancelled or failed run must never leave a
            # truncated context document at the final path.
            mode = _output_mode(output_path)
            try:
                fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".codeprism-", suffix=".tmp")
            except PermissionError as exc:
                # 默认输出落在文件系统根（如 C:\ 或 /）时常见：扩展侧有根
                # 守卫，CLI 侧不禁止用户显式意图，但给可操作的提示而非裸
                # OSError。参见 docs/DEVELOPMENT.md 的已知限制。
                raise click.ClickException(
                    f"Cannot create the output next to {output_path}: {exc}. "
                    "Pass -o <writable path> to choose a different location."
                ) from exc
            try:
                try:
                    handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
                except BaseException:
                    # fdopen did not take ownership of the descriptor.
                    os.close(fd)
                    raise
                with handle:
                    handle.write(result.markdown)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, output_path)
                # chmod after replace: a read-only temp file could not be
                # cleaned up if the replace itself fails (Windows).
                try:
                    os.chmod(output_path, mode)
                except OSError as exc:
                    # The document is already committed; failing to restore
                    # permissions must not discard a successful generation.
                    click.echo(f"Warning: document written but setting permissions failed: {exc}", err=True)
                if os.name == "posix":
                    try:
                        dir_fd = os.open(output_path.parent, getattr(os, "O_DIRECTORY", 0))
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except OSError:
                        pass
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

        copied = False
        if copy:
            try:
                # 与扩展文件复制、PythonRunner stdout 缓冲共用同一个 UTF-8
                # 字节预算（不是 JS string.length，也不是系统 locale 编码）。
                if _over_clipboard_budget(result.markdown):
                    raise ValueError(
                        f"document exceeds the {_CLIPBOARD_MAX_BYTES // (1024 * 1024)} MiB clipboard budget"
                    )
                import pyperclip

                pyperclip.copy(result.markdown)
                copied = True
            except Exception as exc:
                # The document is already written; a missing clipboard service
                # must not fail the whole command (matches extension semantics
                # and keeps automation idempotent); stats report copied: false.
                click.echo(f"Warning: Markdown generated but clipboard copy failed: {exc}", err=True)

        for warning in result.warnings:
            click.echo(f"Warning: {warning}", err=True)

        if json_stats:
            # With --stdout the Markdown owns stdout; stats go to stderr so
            # tooling can still parse a single trailing JSON line.
            click.echo(
                json.dumps(_stats_to_dict(result, None if to_stdout else output_path, copied)),
                err=to_stdout,
            )
        else:
            destination = "stdout" if to_stdout else str(output_path)
            suffix = " (copied to clipboard)" if copied else ""
            # Never pollute a --stdout pipeline with the human summary.
            click.echo(
                f"CodePrism: {result.stats.total_files} files "
                f"({result.stats.text_files} text, {result.stats.binary_files} non-text) -> {destination}{suffix}",
                err=to_stdout,
            )
    except click.ClickException:
        raise
    except BrokenPipeError:
        # 文件模式下末尾统计行/JSON 行写到已关闭的管道时，与 --stdout 模式
        # 一样静默退出（BrokenPipeError 是 OSError 子类，必须先于下一分支）。
        _silence_broken_pipe()
        raise SystemExit(0) from None
    except RecursionError as exc:
        # walk/render 均已迭代化，这里只作为防御性兜底：提示不应再声称
        # 是「递归遍历上限」，真正的来源更可能是平台路径长度或异常深度的输入。
        raise click.ClickException(
            "Input could not be processed (platform path/depth limit or recursion in a renderer)."
        ) from exc
    except (OSError, ValueError, re.error) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
