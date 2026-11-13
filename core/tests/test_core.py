"""Unit tests for the CodePrism core engine (stdlib unittest, no extra deps)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from pathspec import PathSpec

from codeprism.api import PackStats, pack, resolve_root
from codeprism.classifier import ClassifiedFile, _decode_raw, classify, decode_text, format_size
from codeprism.cli import _over_clipboard_budget, _silence_broken_pipe, main
from codeprism.defaults import binary_type_for, language_for
from codeprism.display import code_span, sanitize_name
from codeprism.markdown_renderer import _table_cell, fence_for, render_markdown
from codeprism.scanner import ScannedFile, Scanner, _detect_case_fold, _is_junction, make_spec

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"IEND\xaeB`\x82"


class TempProjectCase(unittest.TestCase):
    def setUp(self) -> None:
        # resolve() normalises Windows 8.3 short names returned by mkdtemp.
        self.tmp = Path(tempfile.mkdtemp(prefix="codeprism-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, rel: str, content) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            # newline="" prevents Windows CRLF translation so fixtures are
            # byte-identical to the literals in this file.
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        return path

    def pack(self, paths=None, **kwargs):
        return pack(paths if paths is not None else [self.tmp], **kwargs)

    def rel_paths(self, result):
        return [f.rel_path for f in result.files]


class GitIgnoreTests(TempProjectCase):
    def test_root_gitignore_patterns(self):
        self.write(".gitignore", "*.log\nbuild/\n")
        self.write("app.py", "print(1)\n")
        self.write("debug.log", "noise")
        self.write("build/output.js", "noise")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "app.py"])

    def test_nested_gitignore_relative_to_its_directory(self):
        self.write(".gitignore", "*.log\n")
        self.write("src/.gitignore", "draft*\n")
        self.write("src/keep.py", "ok\n")
        self.write("src/draft.py", "hidden")
        self.write("draft.py", "visible at root")
        self.write("root.log", "hidden")
        result = self.pack()
        self.assertEqual(
            self.rel_paths(result),
            [".gitignore", "draft.py", "src/.gitignore", "src/keep.py"],
        )

    def test_negation_within_one_file(self):
        self.write(".gitignore", "*.tmp\n!important.tmp\n")
        self.write("junk.tmp", "x")
        self.write("important.tmp", "y")
        result = self.pack()
        self.assertIn("important.tmp", self.rel_paths(result))
        self.assertNotIn("junk.tmp", self.rel_paths(result))

    def test_deeper_gitignore_overrides_root(self):
        self.write(".gitignore", "*.md\n")
        self.write("docs/.gitignore", "!keep.md\n")
        self.write("docs/keep.md", "kept")
        self.write("docs/other.md", "dropped")
        result = self.pack()
        self.assertIn("docs/keep.md", self.rel_paths(result))
        self.assertNotIn("docs/other.md", self.rel_paths(result))

    def test_ignored_directory_cannot_be_reincluded(self):
        self.write(".gitignore", "blocked/\n")
        self.write("blocked/.gitignore", "!file.txt\n")
        self.write("blocked/file.txt", "should stay hidden")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore"])

    def test_git_dir_always_ignored_even_without_gitignore(self):
        self.write(".git/config", "[core]")
        self.write("main.py", "x")
        result = self.pack(use_gitignore=False)
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_no_gitignore_flag_keeps_default_ignores(self):
        self.write(".gitignore", "*.log\n")
        self.write("debug.log", "now visible")
        self.write("node_modules/lib/index.js", "still hidden")
        result = self.pack(use_gitignore=False)
        self.assertIn("debug.log", self.rel_paths(result))
        self.assertNotIn("node_modules/lib/index.js", self.rel_paths(result))

    def test_gitignore_rules_apply_when_packing_subdirectory_only(self):
        # Inside a Git repository, ancestor .gitignore files up to the repo
        # root still apply when only a subdirectory is packed.
        self.write(".git/HEAD", "ref: refs/heads/main")
        self.write(".gitignore", "*.log\n")
        self.write("src/keep.py", "ok")
        self.write("src/debug.log", "hidden")
        result = self.pack([self.tmp / "src"])
        self.assertEqual(self.rel_paths(result), ["keep.py"])

    def test_explicit_file_selection_respects_gitignore(self):
        self.write(".gitignore", "*.log\n")
        target = self.write("debug.log", "hidden")
        self.write("main.py", "ok")
        result = self.pack([target, self.tmp / "main.py"])
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_nested_repository_is_a_hard_boundary(self):
        self.write(".git/HEAD", "ref: refs/heads/main")
        self.write(".gitignore", "*.py\n")
        self.write("inner/.git/HEAD", "ref: refs/heads/main")
        self.write("inner/keep.py", "ok")
        result = self.pack([self.tmp / "inner"])
        self.assertEqual(self.rel_paths(result), ["keep.py"])

    @unittest.skipUnless(
        sys.platform in ("win32", "darwin") or os.path.normcase("A") != "A",
        "case-insensitive filesystem only",
    )
    def test_ignore_matching_folds_case_like_git(self):
        self.write(".gitignore", "Build/\n*.LOG\n")
        self.write("build/out.js", "x")
        self.write("debug.log", "x")
        self.write("keep.py", "ok")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "keep.py"])

    def test_pattern_syntax_not_mutated_by_case_folding(self):
        self.write(".gitignore", "\\#hash\n[Tt]mp/\n")
        self.write("#hash", "x")
        # "Tmp" matches [Tt]mp on every platform; "TMP" would only match where
        # case folding is active and fail the Linux CI leg.
        self.write("Tmp/inner.js", "x")
        self.write("keep.py", "ok")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "keep.py"])

    def test_hard_git_invariant_survives_whitelist_gitignore(self):
        self.write(".gitignore", "*\n!*/\n!*.py\n")
        self.write(".git/config", "[core]")
        self.write("a.py", "ok")
        result = self.pack()
        self.assertNotIn(".git/config", self.rel_paths(result))
        self.assertIn("a.py", self.rel_paths(result))

    def test_user_pattern_beats_gitignore_negation(self):
        self.write(".gitignore", "!keep.tmp\n")
        self.write("keep.tmp", "sensitive")
        self.write("main.py", "ok")
        result = self.pack(extra_ignore_patterns=["*.tmp"])
        self.assertNotIn("keep.tmp", self.rel_paths(result))

    def test_matryoshka_invariant_survives_negation(self):
        self.write(".gitignore", "!codeprism-context.md\n")
        self.write("codeprism-context.md", "# old\nSECRET-OLD")
        self.write("main.py", "ok")
        result = self.pack()
        self.assertNotIn("SECRET-OLD", result.markdown)
        self.assertEqual(self.rel_paths(result), [".gitignore", "main.py"])

    def test_explicit_ignored_subdir_target_is_skipped(self):
        self.write(".gitignore", "secret/\n")
        self.write("secret/x.py", "hidden")
        keep = self.write("keep.py", "ok")
        # Two targets so the pack root is the parent and the parent's
        # .gitignore applies to the explicitly named subdirectory.
        result = self.pack([self.tmp / "secret", keep])
        self.assertEqual(self.rel_paths(result), ["keep.py"])

    def test_git_file_marks_worktree_boundary(self):
        self.write(".gitignore", "*.py\n")
        self.write("inner/.git", "gitdir: ../.git/worktrees/inner")
        self.write("inner/keep.py", "ok")
        result = self.pack([self.tmp / "inner"])
        self.assertEqual(self.rel_paths(result), ["keep.py"])

    def test_non_utf8_gitignore_warns(self):
        self.write(".gitignore", b"*.log\n\xff\n")
        self.write("main.py", "ok")
        result = self.pack()
        self.assertTrue(any("Non-UTF-8" in w for w in result.warnings), result.warnings)

    def test_filesystem_root_gitignore_prefix(self):
        anchor = Path(self.tmp.anchor)
        scanner = Scanner(anchor)
        scanner._specs = [(anchor, make_spec(["*.log"]))]
        self.assertTrue(scanner._is_ignored("a.log", is_dir=False))
        self.assertFalse(scanner._is_ignored("a.py", is_dir=False))

    def test_malformed_range_line_skipped_not_whole_file(self):
        # pathspec raises re.PatternError (not ValueError) for [z-a]; the
        # neighbouring *.log rule must survive and the pack must not crash.
        self.write(".gitignore", "*.log\n[z-a]\n")
        self.write("keep.py", "ok")
        self.write("debug.log", "noise")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "keep.py"])
        self.assertTrue(any(":2" in w and "skipped" in w for w in result.warnings), result.warnings)

    def test_trailing_backslash_line_keeps_sibling_rules(self):
        self.write(".gitignore", "*.log\n\\\n")
        self.write("keep.py", "ok")
        self.write("debug.log", "noise")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "keep.py"])
        self.assertTrue(any(":2" in w and "skipped" in w for w in result.warnings), result.warnings)

    def test_malformed_nested_gitignore_isolated(self):
        self.write(".gitignore", "*.log\n")
        self.write("src/.gitignore", "[z-a]\ndraft*\n")
        self.write("src/keep.py", "ok")
        self.write("src/draft.py", "hidden")
        self.write("root.log", "hidden")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "src/.gitignore", "src/keep.py"])

    def test_user_exclusion_beats_root_and_child_negation(self):
        self.write(".gitignore", "!secret/\n")
        self.write("secret/.gitignore", "!keep.py\n")
        self.write("secret/keep.py", "sensitive")
        self.write("main.py", "ok")
        result = self.pack(extra_ignore_patterns=["secret/"])
        self.assertEqual(self.rel_paths(result), [".gitignore", "main.py"])

    def test_user_negation_reincludes_default_ignored_lock_file(self):
        self.write("package-lock.json", "{}")
        self.write("main.py", "ok")
        result = self.pack(extra_ignore_patterns=["!package-lock.json"])
        self.assertIn("package-lock.json", self.rel_paths(result))

    def test_hard_patterns_apply_at_every_depth(self):
        self.write("sub/.git/config", "metadata")
        self.write("sub/codeprism-context.md", "# old")
        self.write("sub/.codeprism-123.tmp", "transient")
        self.write("main.py", "ok")
        result = self.pack(use_gitignore=False)
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_nested_repo_skipped_when_packing_parent(self):
        self.write("main.py", "ok")
        self.write("vendor/.git/HEAD", "ref: refs/heads/main")
        self.write("vendor/lib.py", "inner")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), ["main.py"])
        self.assertTrue(
            any("Nested repository skipped" in w and "vendor" in w for w in result.warnings),
            result.warnings,
        )

    def test_excluded_nested_repo_stays_silent(self):
        self.write("main.py", "ok")
        self.write("vendor/.git/HEAD", "ref: refs/heads/main")
        self.write("vendor/lib.py", "inner")
        result = self.pack(exclude_paths=[self.tmp / "vendor"])
        self.assertEqual(self.rel_paths(result), ["main.py"])
        self.assertFalse(any("Nested repository" in w for w in result.warnings), result.warnings)

    def test_gitignored_nested_repo_stays_silent(self):
        self.write(".gitignore", "vendor/\n")
        self.write("main.py", "ok")
        self.write("vendor/.git/HEAD", "ref: refs/heads/main")
        self.write("vendor/lib.py", "inner")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), [".gitignore", "main.py"])
        self.assertFalse(any("Nested repository" in w for w in result.warnings), result.warnings)

    def test_explicit_file_under_ignored_ancestor_warns(self):
        self.write(".gitignore", "secret/\n")
        self.write("secret/deep/.gitignore", "!keep.py\n")
        target = self.write("secret/deep/keep.py", "hidden")
        keep = self.write("main.py", "ok")
        result = self.pack([target, keep])
        self.assertEqual(self.rel_paths(result), ["main.py"])
        self.assertTrue(any("Skipped ignored target" in w for w in result.warnings), result.warnings)

    def test_git_root_refused(self):
        git_dir = self.tmp / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"\.git"):
            self.pack([git_dir])

    def test_target_outside_root_friendly_error(self):
        scanner = Scanner(self.tmp)
        outside = Path(tempfile.mkdtemp(prefix="codeprism-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "x.py").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the pack root"):
            scanner.scan([outside / "x.py"])


class DefaultIgnoreTests(TempProjectCase):
    def test_lock_files_ignored_by_default(self):
        self.write("package-lock.json", "{}")
        self.write("pnpm-lock.yaml", "lockfileVersion: 9")
        self.write("Cargo.lock", "# lock")
        self.write("package.json", "{}")
        result = self.pack()
        self.assertEqual(self.rel_paths(result), ["package.json"])

    def test_build_transient_artifacts_ignored_by_default(self):
        self.write(".vendor-build-abc/pkg.py", "x")
        self.write(".vendor-old-abc/pkg.py", "x")
        self.write(".vendor-req-abc.txt", "pins")
        self.write(".codeprism-sync-abc/mod.py", "x")
        self.write(".codeprism-old-abc/mod.py", "x")
        self.write("keep.py", "ok")
        result = self.pack(use_gitignore=False)
        self.assertEqual(self.rel_paths(result), ["keep.py"])

    def test_lock_files_included_with_flag(self):
        self.write("yarn.lock", "# yarn lockfile v1")
        self.write("package.json", "{}")
        result = self.pack(ignore_lock_files=False)
        self.assertEqual(self.rel_paths(result), ["package.json", "yarn.lock"])

    def test_extra_ignore_patterns(self):
        self.write("README.md", "docs")
        self.write("trace.log", "noise")
        self.write("main.py", "ok")
        result = self.pack(extra_ignore_patterns=["*.md", "*.log"])
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_invalid_extra_pattern_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.pack(extra_ignore_patterns=["[z-a]"])

    def test_negative_preview_chars_rejected(self):
        with self.assertRaises(ValueError):
            self.pack(preview_chars=-1)

    def test_negative_max_file_bytes_rejected(self):
        with self.assertRaises(ValueError):
            self.pack(max_file_bytes=-1)

    def test_exclude_paths_directory(self):
        self.write("sub/x.py", "hidden")
        self.write("keep.py", "ok")
        result = self.pack(exclude_paths=[self.tmp / "sub"])
        self.assertEqual(self.rel_paths(result), ["keep.py"])
        targeted = self.pack([self.tmp / "sub"], exclude_paths=[self.tmp / "sub"])
        self.assertEqual(self.rel_paths(targeted), [])

    def test_output_file_excludes_itself(self):
        output = self.write("codeprism-context.md", "# old generated content\nSECRET-OLD")
        self.write("main.py", "ok")
        result = self.pack(output_path=output)
        self.assertNotIn("SECRET-OLD", result.markdown)
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_previous_context_document_not_repacked(self):
        # stdout / clipboard scenario: no output_path, but an old generated
        # document in the workspace must not be wrapped again (matryoshka).
        self.write("codeprism-context.md", "# old\nSECRET-OLD")
        self.write("main.py", "ok")
        result = self.pack()
        self.assertNotIn("SECRET-OLD", result.markdown)
        self.assertEqual(self.rel_paths(result), ["main.py"])

    def test_exclude_paths_matched_literally(self):
        secret = self.write("private.txt", "SECRET-EXCLUDED")
        self.write("main.py", "ok")
        result = self.pack(exclude_paths=[secret])
        self.assertNotIn("SECRET-EXCLUDED", result.markdown)
        self.assertEqual(self.rel_paths(result), ["main.py"])

    @unittest.skipIf(os.name == "nt", "backslash is a path separator on Windows")
    def test_exclude_path_with_literal_backslash_name(self):
        # gitignore patterns cannot match a literal backslash segment
        # (pathspec normalises it to /); exact exclusion must still work.
        weird = self.write("a\\b.md", "SECRET-WEIRD")
        self.write("main.py", "ok")
        result = self.pack(exclude_paths=[weird])
        self.assertNotIn("SECRET-WEIRD", result.markdown)
        self.assertEqual(self.rel_paths(result), ["main.py"])


class BinaryTests(TempProjectCase):
    def test_binary_listed_not_inlined(self):
        self.write("assets/logo.png", PNG_BYTES)
        self.write("main.py", "print('hi')\n")
        result = self.pack()
        binary = next(f for f in result.files if f.rel_path == "assets/logo.png")
        self.assertTrue(binary.is_binary)
        self.assertEqual(binary.binary_type, "Image/PNG")
        self.assertIsNone(binary.text)
        self.assertEqual(result.stats.binary_files, 1)
        self.assertEqual(result.stats.text_files, 1)
        self.assertIn("[Non-Text: .png,", result.markdown)
        self.assertIn("| `assets/logo.png` | `.png` |", result.markdown)
        self.assertIn("Image/PNG", result.markdown)
        self.assertNotIn("### `assets/logo.png`", result.markdown)

    def test_binary_extension_wins_over_text_content(self):
        # Documented policy: the extension list decides before any sniffing,
        # so a text-looking payload named .png is inventoried, not inlined.
        self.write("payload.png", "this is plain text without any NUL bytes\n")
        result = self.pack()
        file = result.files[0]
        self.assertTrue(file.is_binary)
        self.assertIsNone(file.text)
        self.assertEqual(file.binary_type, "Image/PNG")

    def test_sniff_read_budget_contract(self):
        # Classification sniffs exactly 512 bytes first; a binary-extension
        # hit must not read the file at all.
        self.write("plain.txt", "a" * 5000)
        self.write("x.png", "text")
        reads: list[int] = []
        real_open = open

        class TrackingHandle:
            def __init__(self, handle):
                self._handle = handle

            def read(self, n=-1):
                data = self._handle.read(n)
                reads.append(len(data))
                return data

            def fileno(self):
                return self._handle.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        def tracking_open(*args, **kwargs):
            return TrackingHandle(real_open(*args, **kwargs))

        text_file = ScannedFile(abs_path=self.tmp / "plain.txt", rel_path="plain.txt")
        with mock.patch("builtins.open", side_effect=tracking_open):
            classify(text_file, max_file_bytes=0)
        self.assertEqual(reads[0], 512)
        reads.clear()
        png_file = ScannedFile(abs_path=self.tmp / "x.png", rel_path="x.png")
        with mock.patch("builtins.open", side_effect=tracking_open):
            record = classify(png_file, max_file_bytes=0)
        self.assertTrue(record.is_binary)
        self.assertEqual(reads, [])

    def test_unknown_binary_extension_gets_generic_label(self):
        self.write("data/blob.xyz", bytes(range(256)) * 4)
        result = self.pack()
        binary = result.files[0]
        self.assertTrue(binary.is_binary)
        self.assertEqual(binary.binary_type, "Binary")

    def test_empty_file_is_text(self):
        self.write("empty.txt", "")
        result = self.pack()
        self.assertEqual(result.stats.text_files, 1)
        self.assertEqual(result.stats.total_characters, 0)

    def test_crlf_normalized_to_lf(self):
        self.write("windows.py", b"line1\r\nline2\r\n")
        result = self.pack()
        self.assertEqual(result.files[0].text, "line1\nline2\n")
        self.assertNotIn("\r", result.markdown)
        self.assertEqual(result.stats.total_characters, len("line1\nline2\n"))

    def test_gbk_text_is_decoded(self):
        comment = "# 中文注释：这段文本用于编码检测，需要足够多的汉字才能被识别出来。" * 6
        raw = ("# -*- coding: gbk -*-\n" + comment + "\nprint(1)\n").encode("gbk")
        self.write("legacy.py", raw)
        result = self.pack()
        file = result.files[0]
        self.assertFalse(file.is_binary)
        self.assertIn("中文注释", file.text)
        self.assertIn("print(1)", file.text)

    def test_svg_is_text(self):
        self.write("icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n')
        result = self.pack()
        self.assertEqual(result.stats.text_files, 1)
        self.assertEqual(result.stats.binary_files, 0)
        self.assertIn("```svg", result.markdown)

    def test_nul_after_sniff_window_is_binary(self):
        self.write("late-nul.dat", b"A" * 600 + b"\x00" + b"B" * 50)
        result = self.pack()
        file = result.files[0]
        self.assertTrue(file.is_binary)
        self.assertIsNone(file.text)

    def test_size_limit_crlf_cut_has_no_phantom_newline(self):
        self.write("crlf.txt", b"ab\r\ncd\r\nef")
        result = self.pack(max_file_bytes=3, preview_chars=10)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertEqual(file.text, "ab")


class RendererTests(TempProjectCase):
    def test_fence_for_counts_longest_run(self):
        self.assertEqual(fence_for("no backticks"), "```")
        self.assertEqual(fence_for("a ``` b"), "````")
        self.assertEqual(fence_for("`````"), "``````")

    def test_markdown_file_with_fences_does_not_escape(self):
        inner = "# Title\n\n```python\nprint(1)\n```\n\n``````\n"
        self.write("README.md", inner)
        result = self.pack()
        self.assertIn("```````markdown", result.markdown)
        self.assertIn(inner, result.markdown)

    def test_full_document_structure(self):
        self.write("README.md", "hello\n")
        self.write("src/main.py", "print(1)\n")
        self.write("assets/logo.png", PNG_BYTES)
        result = self.pack()
        md = result.markdown
        self.assertIn(f"# CodePrism Context: {self.tmp.name}", md)
        self.assertIn("## 1. Summary", md)
        self.assertIn("## 2. File Tree", md)
        self.assertIn("## 3. Non-Text Files (Binary & Media Assets)", md)
        self.assertIn("## 4. Text Files Content", md)
        self.assertIn("### `src/main.py`\n```python\nprint(1)\n```", md)
        self.assertIn("**Total Files**: 3 (2 text files, 1 non-text files)", md)

    def test_tree_marks_directories_and_binaries(self):
        self.write("src/main.py", "x")
        self.write("assets/logo.png", PNG_BYTES)
        result = self.pack()
        self.assertIn("├── assets/", result.markdown)
        self.assertIn("└── src/", result.markdown)
        self.assertIn("└── main.py", result.markdown)

    def test_no_trailing_newline_file_gets_closing_fence_on_own_line(self):
        self.write("a.py", "print(1)")
        result = self.pack()
        self.assertIn("```python\nprint(1)\n```", result.markdown)

    def test_empty_project(self):
        result = self.pack()
        self.assertEqual(result.stats.total_files, 0)
        self.assertIn("_No non-text files detected._", result.markdown)
        self.assertIn("_No text files detected._", result.markdown)

    def test_empty_file_renders_compact_fence(self):
        self.write("empty.txt", "")
        result = self.pack()
        self.assertIn("```text\n```", result.markdown)

    def test_single_newline_file_keeps_its_newline(self):
        self.write("nl.txt", "\n")
        result = self.pack()
        self.assertIn("```text\n\n```", result.markdown)
        self.assertEqual(result.files[0].line_count, 1)

    def test_two_newline_file_keeps_both_newlines(self):
        self.write("nl2.txt", "\n\n")
        result = self.pack()
        self.assertIn("```text\n\n\n```", result.markdown)
        self.assertEqual(result.files[0].line_count, 2)

    def test_bom_stripped_from_content(self):
        self.write("bom.py", b"\xef\xbb\xbfprint(1)\n")
        result = self.pack()
        self.assertEqual(result.files[0].text, "print(1)\n")
        self.assertNotIn("\ufeff", result.markdown)

    def test_table_escapes_backslash_before_pipe(self):
        file = ClassifiedFile(
            abs_path=Path("x"),
            rel_path="a\\|b.png",
            is_binary=True,
            size_bytes=1,
            binary_type="Image/PNG",
            text=None,
            char_count=0,
            line_count=0,
        )
        stats = PackStats(
            project_name="p",
            source_paths=["/p"],
            sources_all_directories=True,
            total_files=1,
            text_files=0,
            binary_files=1,
            total_characters=0,
            total_lines=0,
        )
        md = render_markdown("p", ["/p"], [file], stats)
        self.assertIn("`a\\\\\\|b.png`", md)

    def test_table_lone_backslash_not_doubled(self):
        file = ClassifiedFile(
            abs_path=Path("x"),
            rel_path="a\\b.png",
            is_binary=True,
            size_bytes=1,
            binary_type="Image/PNG",
            text=None,
            char_count=0,
            line_count=0,
        )
        stats = PackStats(
            project_name="p",
            source_paths=["/p"],
            sources_all_directories=True,
            total_files=1,
            text_files=0,
            binary_files=1,
            total_characters=0,
            total_lines=0,
        )
        md = render_markdown("p", ["/p"], [file], stats)
        self.assertIn("`a\\b.png`", md)
        self.assertNotIn("`a\\\\b.png`", md)

    def test_table_pipe_never_splits_for_any_backslash_run(self):
        bs = chr(92)
        for k in range(5):
            cell = _table_cell("a" + bs * k + "|b.png")
            index = cell.index("|")
            run = 0
            j = index - 1
            while j >= 0 and cell[j] == bs:
                run += 1
                j -= 1
            # GFM splits on a pipe preceded by an even backslash run.
            self.assertEqual(run % 2, 1, f"k={k} emitted even run {run}: {cell!r}")
            # Even k renders byte-faithfully; odd k costs one extra backslash.
            self.assertEqual(run, k + 1 if k % 2 == 0 else k + 2, f"k={k}: {cell!r}")

    def test_deterministic_output(self):
        self.write("b.py", "2")
        self.write("a.py", "1")
        self.write("dir/z.py", "3")
        self.write("assets/x.png", PNG_BYTES)
        first = self.pack().markdown
        second = self.pack().markdown
        self.assertEqual(first, second)
        self.assertLess(first.index("### `a.py`"), first.index("### `b.py`"))
        self.assertLess(first.index("### `b.py`"), first.index("### `dir/z.py`"))


class SelectionTests(TempProjectCase):
    def test_multiple_directories_share_common_root(self):
        self.write("src/a.py", "a")
        self.write("tests/test_a.py", "t")
        result = self.pack([self.tmp / "src", self.tmp / "tests"])
        self.assertEqual(self.rel_paths(result), ["src/a.py", "tests/test_a.py"])
        self.assertEqual(result.stats.project_name, self.tmp.name)

    def test_multi_source_title_and_plural_label(self):
        self.write("alpha/a.py", "a")
        self.write("beta/b.py", "b")
        result = self.pack([self.tmp / "alpha", self.tmp / "beta"])
        self.assertIn("# CodePrism Context: alpha + beta", result.markdown)
        self.assertIn("- **Source Directories**:", result.markdown)

    def test_file_and_directory_mix_deduplicates(self):
        self.write("src/a.py", "a")
        self.write("src/b.py", "b")
        # Common root of [src/, src/a.py] is src/ itself.
        result = self.pack([self.tmp / "src", self.tmp / "src" / "a.py"])
        self.assertEqual(self.rel_paths(result), ["a.py", "b.py"])
        self.assertEqual(result.stats.project_name, "src")

    def test_resolve_root_for_files_uses_parent(self):
        a = self.write("src/deep/a.py", "a")
        b = self.write("src/b.py", "b")
        self.assertEqual(resolve_root([a, b]), self.tmp / "src")

    def test_missing_input_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.pack([self.tmp / "does-not-exist"])


class TruncationTests(TempProjectCase):
    def test_license_preview_tree_retention_and_notice(self):
        original = "GNU GENERAL PUBLIC LICENSE\n" + "x" * 5000
        self.write("LICENSE", original)
        self.write("main.py", "ok\n")
        result = self.pack()
        lic = next(f for f in result.files if f.rel_path == "LICENSE")
        self.assertTrue(lic.truncated)
        self.assertEqual(lic.char_count, 200)
        self.assertEqual(lic.omitted_chars, len(original) - 200)
        self.assertEqual(result.stats.truncated_files, 1)
        self.assertEqual(result.stats.total_characters, 200 + 3)
        self.assertIn(f"LICENSE [Truncated: first 200 of {len(original)} chars]", result.markdown)
        self.assertIn("> [CodePrism] Content truncated: showing first 200 of", result.markdown)
        self.assertIn("- **Truncated Files**: 1", result.markdown)
        self.assertIn("### `LICENSE`", result.markdown)

    def test_preview_chars_zero_omits_content(self):
        self.write("LICENSE", "legal text " * 100)
        result = self.pack(preview_chars=0)
        lic = result.files[0]
        self.assertTrue(lic.truncated)
        self.assertEqual(lic.text, "")
        self.assertIn("showing first 0 of", result.markdown)

    def test_preview_chars_configurable(self):
        self.write("COPYING", "z" * 900)
        result = self.pack(preview_chars=10)
        self.assertEqual(result.files[0].char_count, 10)

    def test_lowercase_license_truncated_on_every_platform(self):
        self.write("license", "legal text " * 100)
        result = self.pack()
        self.assertTrue(result.files[0].truncated)

    def test_extra_truncate_pattern(self):
        self.write("NOTES.md", "y" * 500)
        self.write("main.py", "ok\n")
        result = self.pack(truncate_patterns=["NOTES.md"])
        notes = next(f for f in result.files if f.rel_path == "NOTES.md")
        self.assertTrue(notes.truncated)
        self.assertEqual(result.stats.truncated_files, 1)

    def test_invalid_truncate_pattern_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.pack(truncate_patterns=["[9-0]"])

    def test_preview_longer_than_file_not_marked(self):
        self.write("LICENSE", "short legal text")
        result = self.pack(preview_chars=1000)
        self.assertFalse(result.files[0].truncated)
        self.assertNotIn("Content truncated", result.markdown)

    def test_preview_zero_renders_no_empty_fence(self):
        self.write("LICENSE", "legal text " * 100)
        result = self.pack(preview_chars=0)
        self.assertNotIn("```\n\n```", result.markdown)
        self.assertIn("showing first 0 of", result.markdown)

    def test_truncate_class_pattern_case_insensitive(self):
        # [A-Z] must keep its meaning (not be lowered to [a-z]) while matching
        # both cases on every platform, including case-sensitive Linux.
        # Distinct basenames: NOTES.MD/notes.md would collapse on Windows.
        self.write("NOTES.MD", "x" * 500)
        self.write("readme.md", "y" * 500)
        result = self.pack(truncate_patterns=["[A-Z]*.MD"], preview_chars=10)
        self.assertEqual(result.stats.truncated_files, 2)
        self.assertTrue(all(f.char_count == 10 for f in result.files))


class SizeLimitTests(TempProjectCase):
    def test_size_limited_preview(self):
        self.write("big.csv", "x" * 5000)
        result = self.pack(max_file_bytes=1000, preview_chars=10)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertEqual(file.char_count, 10)
        self.assertEqual(result.stats.size_limited_files, 1)
        self.assertIn("[Size-limited: first 10 chars]", result.markdown)
        self.assertIn("> [CodePrism] Size limit", result.markdown)

    def test_size_limit_disabled(self):
        self.write("big.csv", "y" * 5000)
        result = self.pack(max_file_bytes=0)
        self.assertEqual(result.files[0].char_count, 5000)
        self.assertFalse(result.files[0].size_limited)

    def test_size_limit_chunk_cut_multibyte_char(self):
        original = "# 中文注释 comment line\n" * 300
        self.write("cjk.txt", original)
        # 9*4+4 = 40 bytes lands inside the 4th CJK char of line 2, forcing
        # the "unexpected end of data" trim branch (44 bytes would not).
        # The chardet fallback must not run for a clean UTF-8 tail cut.
        with mock.patch(
            "codeprism.classifier._decode_raw",
            side_effect=AssertionError("chardet fallback used for a UTF-8 tail cut"),
        ):
            result = self.pack(max_file_bytes=1000, preview_chars=9)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertEqual(file.text, original[:9])
        self.assertNotIn("\ufffd", file.text)

    def test_size_limit_read_capped_at_max_file_bytes(self):
        self.write("big.csv", "x" * 150)
        result = self.pack(max_file_bytes=100, preview_chars=10000)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertEqual(file.char_count, 100)

    def test_size_limit_gbk_preview_real_chardet(self):
        # No mocks: real chardet must detect GB2312 on the cut chunk and the
        # tail-trim retry must drop the partial double-byte character.
        original = "中文注释 abc\n" * 10
        self.write("legacy.txt", original.encode("gbk"))
        result = self.pack(max_file_bytes=40, preview_chars=200)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertTrue(file.text.startswith("中文注释 abc\n"), repr(file.text))
        self.assertNotIn("\ufffd", file.text)

    def test_size_limited_preview_tail_cut_does_not_warn(self):
        # 预览块在 UTF-8 多字节字符中间截断时，_decode_raw 的 tail-cut 会
        # 内部处理（lossy=False），不应产生 decoding-loss 噪音。
        self.write("big.txt", b"abcdefghijklmnop\xff" * 120)
        with mock.patch("codeprism.classifier._decode_raw", return_value=("badpreview", None, 0.0, False)):
            result = self.pack(max_file_bytes=100, preview_chars=10)
        self.assertTrue(result.files[0].size_limited)
        self.assertFalse(any("decoding loss" in w for w in result.warnings), result.warnings)

    def test_size_limited_preview_decoding_loss_warns(self):
        # 预览块彻底不可解码（lossy=True）时正文发生 U+FFFD 替换，必须
        # 与完整读取路径一样告警；此前该分支丢弃 _lossy 导致静默。
        # 前 512 字节几乎全 ASCII（sniff 判文本），但预览窗口内含非法字节，
        # 强制走 _decode_raw 的 lossy 回退路径。
        self.write("big.txt", b"a" * 10 + b"\xff" + b"a" * 589)
        with mock.patch("codeprism.classifier._decode_raw", return_value=("bad\ufffd", None, 0.0, True)):
            result = self.pack(max_file_bytes=100, preview_chars=10)
        self.assertTrue(result.files[0].size_limited)
        self.assertTrue(any("decoding loss" in w for w in result.warnings), result.warnings)

    def test_full_read_decoding_loss_still_warns(self):
        self.write("small.txt", b"abcdefghijklmnop\xff")
        with mock.patch("codeprism.classifier._decode_raw", return_value=("abc\ufffd", None, 0.0, True)):
            result = self.pack()
        self.assertTrue(any("decoding loss" in w for w in result.warnings), result.warnings)

    def test_legitimate_replacement_char_not_decoding_loss(self):
        # A valid UTF-8 source that literally contains U+FFFD is content,
        # not a decoding artefact: no loss warning may fire.
        self.write("legit.txt", "mentions the replacement char: \ufffd\n")
        result = self.pack()
        self.assertFalse(any("decoding loss" in w for w in result.warnings), result.warnings)

    def test_growing_file_read_capped_at_initial_size(self):
        # fstat says 3 bytes but the file keeps growing mid-read (active log):
        # the snapshot must stay at the recorded size, in both read modes.
        self.write("grow.txt", "abcdefghij")
        file = ScannedFile(abs_path=self.tmp / "grow.txt", rel_path="grow.txt")
        fake_stat = os.stat_result((0o100644, 0, 0, 0, 0, 0, 3, 0, 0, 0))
        with mock.patch("codeprism.classifier.os.fstat", return_value=fake_stat):
            full = classify(file, max_file_bytes=0)
            limited = classify(file, max_file_bytes=2, preview_chars=100)
        self.assertEqual(full.size_bytes, 3)
        self.assertEqual(full.text, "abc")
        self.assertTrue(limited.size_limited)
        self.assertEqual(limited.text, "ab")

    def test_size_limit_lone_cr_at_cut_survives(self):
        self.write("mac.txt", b"ab\rcd\ref")
        result = self.pack(max_file_bytes=3, preview_chars=100)
        file = result.files[0]
        self.assertTrue(file.size_limited)
        self.assertEqual(file.text, "ab\n")

    def test_size_limit_crlf_at_cut_drops_cr(self):
        self.write("win.txt", b"ab\r\ncd")
        result = self.pack(max_file_bytes=3, preview_chars=100)
        self.assertEqual(result.files[0].text, "ab")

    def test_nul_after_sniff_window_still_binary(self):
        self.write("late.txt", b"a" * 600 + b"\x00tail")
        result = self.pack()
        self.assertTrue(result.files[0].is_binary)

    def test_nul_inside_preview_window_binary(self):
        self.write("late.txt", b"a" * 600 + b"\x00tail")
        result = self.pack(max_file_bytes=602, preview_chars=200)
        self.assertTrue(result.files[0].is_binary)

    def test_junction_fallback_checks_reparse_tag_not_attribute(self):
        class FakeEntry:
            def __init__(self, tag):
                self._tag = tag

            def stat(self, follow_symlinks=True):
                return mock.Mock(st_reparse_tag=self._tag)

        with mock.patch("codeprism.scanner.os.name", "nt"):
            self.assertTrue(_is_junction(FakeEntry(0xA0000003)))  # mount point / junction
            self.assertFalse(_is_junction(FakeEntry(0x9000001A)))  # cloud placeholder
            self.assertFalse(_is_junction(FakeEntry(0)))  # plain file

    @unittest.skipUnless(os.name == "nt", "Windows junctions only")
    def test_junction_cycle_skipped(self):
        self.write("keep.py", "ok")
        junction = self.tmp / "loop"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(self.tmp)],
            capture_output=True,
        )
        if proc.returncode != 0:
            self.skipTest("cannot create junctions in this environment")
        # Remove the junction itself before the recursive temp cleanup so no
        # interpreter version can follow it into the cycle.
        self.addCleanup(os.rmdir, junction)
        result = self.pack()
        self.assertEqual(self.rel_paths(result), ["keep.py"])


class CliContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="codeprism-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "a.py").write_text("print(1)\n", encoding="utf-8")

    def run_cli(self, *args: str):
        return subprocess.run(
            [sys.executable, "-m", "codeprism", str(self.tmp), *args],
            capture_output=True,
            timeout=60,
        )

    def test_stdout_is_lf_and_unpolluted(self):
        proc = self.run_cli("--stdout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"\r", proc.stdout)
        self.assertNotIn(b"CodePrism:", proc.stdout)
        self.assertTrue(proc.stdout.startswith(b"# CodePrism Context:"))

    def test_stdout_preserves_ansi_bytes(self):
        (self.tmp / "colors.py").write_bytes(b"print('\x1b[31mred\x1b[0m')\n")
        proc = self.run_cli("--stdout")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"\x1b[31mred\x1b[0m", proc.stdout)

    def test_stdout_json_stats_on_stderr(self):
        proc = self.run_cli("--stdout", "--json-stats")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"\r", proc.stdout)
        stats = json.loads(proc.stderr.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(stats["totalFiles"], 1)

    def test_refuses_overwrite_foreign_file(self):
        target = self.tmp / "a.py"
        proc = self.run_cli("-o", str(target))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "print(1)\n")
        proc = self.run_cli("-o", str(target), "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(target.read_text(encoding="utf-8").startswith("# CodePrism Context:"))

    def test_invalid_ignore_pattern_friendly_error(self):
        proc = self.run_cli("--stdout", "--ignore-pattern", "[9-0]")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"Traceback", proc.stderr)
        self.assertIn(b"Invalid --ignore-pattern", proc.stderr)

    def test_invalid_truncate_pattern_friendly_error(self):
        proc = self.run_cli("--stdout", "--truncate-pattern", "[9-0]")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(b"Traceback", proc.stderr)
        self.assertIn(b"Invalid --truncate-pattern", proc.stderr)

    def test_malformed_gitignore_does_not_crash_cli(self):
        (self.tmp / ".gitignore").write_bytes(b"*.py\n[z-a]\n")
        proc = self.run_cli("--stdout", "--json-stats")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)
        self.assertIn(b"skipped", proc.stderr)

    def test_exclude_path_option(self):
        proc = self.run_cli("--stdout", "--exclude-path", str(self.tmp / "a.py"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"### `a.py`", proc.stdout)

    def test_exclude_path_option_directory(self):
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "b.py").write_text("print(2)\n", encoding="utf-8")
        proc = self.run_cli("--stdout", "--exclude-path", str(self.tmp / "sub"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"### `sub/b.py`", proc.stdout)
        self.assertIn(b"### `a.py`", proc.stdout)

    def test_negative_limits_rejected(self):
        for option in ("--max-file-bytes", "--preview-chars"):
            proc = self.run_cli("--stdout", option, "-1")
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn(b"Traceback", proc.stderr)

    def test_flag_like_filename_after_separator(self):
        weird = self.tmp / "--stdout"
        weird.write_text("FLAG-NAME-CONTENT\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "codeprism", "--stdout", "--", str(weird)],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"FLAG-NAME-CONTENT", proc.stdout)

    def test_copy_failure_warns_but_succeeds(self):
        with mock.patch("pyperclip.copy", side_effect=RuntimeError("no clipboard service")):
            result = CliRunner().invoke(main, [str(self.tmp), "--copy", "--json-stats"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("clipboard copy failed", result.output)
        stats = json.loads(result.output.strip().splitlines()[-1])
        self.assertFalse(stats["copied"])
        self.assertTrue((self.tmp / "codeprism-context.md").exists())

    def test_copy_skipped_when_over_unified_budget(self):
        # 与扩展文件复制、PythonRunner stdout 缓冲共用的 UTF-8 字节预算。
        with mock.patch("codeprism.cli._CLIPBOARD_MAX_BYTES", 16), mock.patch("pyperclip.copy") as copy_mock:
            result = CliRunner().invoke(main, [str(self.tmp), "--copy", "--json-stats"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("clipboard budget", result.output)
        copy_mock.assert_not_called()
        stats = json.loads(result.output.strip().splitlines()[-1])
        self.assertFalse(stats["copied"])
        self.assertTrue((self.tmp / "codeprism-context.md").exists())

    def test_file_mode_broken_pipe_exits_quietly(self):
        # 文件模式末尾统计行/JSON 行写入已关闭管道时也必须 exit 0。
        with mock.patch("codeprism.cli.click.echo", side_effect=BrokenPipeError()), mock.patch(
            "codeprism.cli._silence_broken_pipe"
        ) as silence:
            result = CliRunner().invoke(main, [str(self.tmp)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("Traceback", result.output)
        silence.assert_called_once()

    def test_stdout_utf8_under_non_utf8_locale(self):
        # 非 UTF-8 stdout/locale 下 --stdout 仍须与文件输出逐字节一致。
        (self.tmp / "uni.txt").write_text("héllo 中文\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "cp1252"
        env["PYTHONUTF8"] = "0"
        proc = subprocess.run(
            [sys.executable, "-m", "codeprism", str(self.tmp), "--stdout"],
            capture_output=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = self.tmp / "out.md"
        proc2 = subprocess.run(
            [sys.executable, "-m", "codeprism", str(self.tmp), "-o", str(out)],
            capture_output=True, env=env, timeout=60,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc.stdout, out.read_bytes())
        self.assertIn("héllo 中文".encode("utf-8"), proc.stdout)

    def test_root_output_permission_error_is_friendly(self):
        with mock.patch(
            "codeprism.cli.tempfile.mkstemp", side_effect=PermissionError(13, "Permission denied")
        ):
            result = CliRunner().invoke(main, [str(self.tmp)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Pass -o", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_output_parent_created_even_if_pack_fails(self):
        # Documented side effect: the destination directory is created before
        # the walk, so it exists even when the pack itself fails.
        target = self.tmp / "made" / "deep" / "out.md"
        with mock.patch("codeprism.cli.pack", side_effect=ValueError("boom")):
            result = CliRunner().invoke(main, [str(self.tmp), "-o", str(target)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(target.parent.is_dir())

    def test_readonly_target_leaves_no_temp_residue(self):
        target = self.tmp / "readonly.md"
        target.write_text("foreign\n", encoding="utf-8")
        target.chmod(0o444)
        try:
            proc = self.run_cli("-o", str(target), "--force")
            if os.name == "nt":
                # Windows cannot replace a read-only target; the failure must
                # be clean and the 0600 temp file must still be removable.
                self.assertNotEqual(proc.returncode, 0)
            else:
                self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(list(self.tmp.glob(".codeprism-*.tmp")), [])
        finally:
            target.chmod(0o666)

    @unittest.skipIf(os.name == "nt", "POSIX pipe semantics")
    def test_broken_pipe_exits_quietly(self):
        (self.tmp / "big.txt").write_text("x" * 200000, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "codeprism", str(self.tmp), "--stdout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.stdout.close()
        err = proc.stderr.read()
        proc.stderr.close()
        code = proc.wait(timeout=60)
        self.assertEqual(code, 0, err)
        self.assertNotIn(b"Traceback", err)

    def test_stdout_matches_file_output_bytes(self):
        (self.tmp / "uni.txt").write_text("héllo\n", encoding="utf-8")
        proc = self.run_cli("--stdout")
        out = self.tmp / "out.md"
        proc2 = self.run_cli("-o", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc.stdout, out.read_bytes())

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_force_replaces_symlink_itself_not_target(self):
        real = self.tmp / "real.md"
        real.write_text("precious\n", encoding="utf-8")
        link = self.tmp / "link.md"
        link.symlink_to(real)
        proc = self.run_cli("-o", str(link), "--force")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(link.is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), "precious\n")
        self.assertTrue(link.read_text(encoding="utf-8").startswith("# CodePrism Context:"))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_output_preserves_existing_mode(self):
        target = self.tmp / "doc.md"
        target.write_text("# CodePrism Context: old\n", encoding="utf-8")
        target.chmod(0o640)
        proc = self.run_cli("-o", str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    @unittest.skipIf(os.name == "nt", "POSIX fifo")
    def test_fifo_output_refused_even_with_force(self):
        fifo = self.tmp / "pipe.md"
        os.mkfifo(fifo)
        proc = self.run_cli("-o", str(fifo), "--force")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"not a regular file", proc.stderr)


class SourceLabelTests(TempProjectCase):
    def test_file_selection_uses_source_paths(self):
        a = self.write("a.py", "a")
        b = self.write("b.py", "b")
        result = self.pack([a, b])
        self.assertIn("- **Source Paths**:", result.markdown)
        self.assertNotIn("**Source Directory**", result.markdown)

    def test_directory_selection_keeps_source_directory(self):
        self.write("d/a.py", "a")
        result = self.pack([self.tmp / "d"])
        self.assertIn("- **Source Directory**:", result.markdown)


class DisplaySafetyTests(unittest.TestCase):
    def test_code_span_plain(self):
        self.assertEqual(code_span("README.md"), "`README.md`")

    def test_code_span_with_backticks(self):
        self.assertEqual(code_span("a`b.md"), "``a`b.md``")
        self.assertEqual(code_span("``x``"), "``` ``x`` ```")

    def test_code_span_both_ends_spaces_padded(self):
        self.assertEqual(code_span(" x "), "`  x  `")

    def test_sanitize_name_soft_hyphen_and_marks(self):
        self.assertEqual(sanitize_name("a\u00adb"), "a\\u00adb")
        self.assertEqual(sanitize_name("a\u061cb"), "a\\u061cb")

    def test_sanitize_name_control_chars(self):
        self.assertEqual(sanitize_name("ev\nil.md"), "ev\\nil.md")
        self.assertEqual(sanitize_name("a\rb\tc"), "a\\rb\\tc")

    def test_sanitize_name_surrogates(self):
        # POSIX surrogateescape filenames must render visibly, never crash
        # UTF-8 encoding of the generated document.
        self.assertEqual(sanitize_name("a\ud800b"), "a\\ud800b")
        self.assertEqual(sanitize_name("\udfff"), "\\udfff")
        self.assertEqual(code_span("x\ud800.md"), "`x\\ud800.md`")
        sanitize_name("x\ud800.md").encode("utf-8")

    def test_renderer_escapes_hostile_paths(self):
        hostile = ClassifiedFile(
            abs_path=Path("x"),
            rel_path="ev\nil`|.md",
            is_binary=False,
            size_bytes=3,
            binary_type=None,
            text="hi\n",
            char_count=3,
            line_count=1,
        )
        stats = PackStats(
            project_name="pro\nject",
            source_paths=["/tmp/a\nb"],
            sources_all_directories=False,
            total_files=1,
            text_files=1,
            binary_files=0,
            total_characters=3,
            total_lines=1,
        )
        md = render_markdown(stats.project_name, stats.source_paths, [hostile], stats)
        self.assertNotIn("ev\nil", md)
        self.assertNotIn("pro\nject", md)
        self.assertIn("### ``ev\\nil`|.md``", md)
        self.assertIn("# CodePrism Context: pro\\nject", md)
        self.assertIn("ev\\nil`|.md", md)  # tree line stays single-line


class HelpersTests(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1023), "1023 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(18842), "18.4 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(3 * 1024 * 1024 * 1024), "3.0 GB")

    def test_language_for(self):
        self.assertEqual(language_for("src/main.py"), "python")
        self.assertEqual(language_for("web/app.TSX"), "tsx")
        self.assertEqual(language_for("Dockerfile"), "dockerfile")
        self.assertEqual(language_for("docs/README.md"), "markdown")
        self.assertEqual(language_for(".gitignore"), "gitignore")
        self.assertEqual(language_for("Makefile"), "makefile")
        self.assertEqual(language_for("data.unknownext"), "")
        self.assertEqual(language_for("noextension"), "")

    def test_binary_type_for(self):
        self.assertEqual(binary_type_for("a/logo.png"), "Image/PNG")
        self.assertEqual(binary_type_for("font.ttf"), "Font/TTF")
        self.assertEqual(binary_type_for("blob.xyz"), "Binary")
        self.assertEqual(binary_type_for("noext"), "Binary")

    def test_binary_type_for_literal_backslash_name(self):
        self.assertEqual(binary_type_for("a\\b.png"), "Image/PNG")
        self.assertEqual(language_for("a\\b.py"), "python")
        self.assertEqual(language_for("foo\\Dockerfile"), "")

    def test_decode_text_utf8_passthrough(self):
        self.assertEqual(decode_text("héllo".encode("utf-8")), "héllo")

    def test_decode_text_normalises_all_line_endings(self):
        self.assertEqual(decode_text(b"a\rb\r\nc\n"), "a\nb\nc\n")

    def test_decode_text_invalid_bytes_falls_back(self):
        decoded = decode_text(b"\xff\xfe\x00abc")
        self.assertIsInstance(decoded, str)

    def test_decode_text_chardet_tail_cut_trimmed(self):
        # A GBK preview chunk cut inside a double-byte character must drop the
        # partial tail instead of degrading the whole preview to lossy UTF-8.
        raw = b"a" * 15 + "中".encode("gbk")
        detector = mock.MagicMock()
        detector.result = {"encoding": "gbk"}
        with mock.patch("codeprism.classifier.UniversalDetector", return_value=detector):
            text = decode_text(raw[:16], tail_cut=True)
        self.assertEqual(text, "a" * 15)

    def test_decode_text_full_file_corruption_not_silently_trimmed(self):
        # Without tail_cut the same incomplete tail is genuine corruption:
        # it must surface as U+FFFD so the decoding-loss warning fires.
        raw = b"a" * 15 + "中".encode("gbk")[:1]
        detector = mock.MagicMock()
        detector.result = {"encoding": "gbk"}
        with mock.patch("codeprism.classifier.UniversalDetector", return_value=detector):
            text = decode_text(raw)
        self.assertIn("\ufffd", text)

    def test_decode_text_utf16_odd_cut_trimmed(self):
        raw = "中文文字".encode("utf-16")
        detector = mock.MagicMock()
        detector.result = {"encoding": "utf-16"}
        with mock.patch("codeprism.classifier.UniversalDetector", return_value=detector):
            text = decode_text(raw[:-1], tail_cut=True)
        self.assertEqual(text, "中文文")
        self.assertNotIn("\ufffd", text)


class DeepTreeTests(TempProjectCase):
    def test_1200_deep_tree_scanned_and_rendered(self):
        """跨平台虚拟深树：验证 walker 与 tree renderer 都是迭代的。

        旧夹具真的创建 1200 层 ``d0/d1/...``，路径约 6107 字符，超过 Linux
        PATH_MAX(4096) 与 macOS PATH_MAX(1024)，在两台 CI 上都会 ERROR；
        Windows 又因 skipIf(nt) 从不执行，等于任何平台都没有覆盖。改为
        mock os.scandir 的内存目录树后，深度可以任意大且不落盘，全平台
        都会真正跑这个用例。
        """
        depth = 1200

        class FakeEntry:
            def __init__(self, entry_path: str, name: str, is_directory: bool):
                self.path = entry_path
                self.name = name
                self._is_directory = is_directory

            def is_symlink(self):
                return False

            def is_junction(self):
                return False

            def is_dir(self, follow_symlinks=True):
                return self._is_directory

            def is_file(self, follow_symlinks=True):
                return not self._is_directory

        children: dict[str, list[FakeEntry]] = {}
        current = self.tmp
        for i in range(depth):
            child = current / f"d{i}"
            children[str(current)] = [FakeEntry(str(child), f"d{i}", True)]
            current = child
        children[str(current)] = [FakeEntry(str(current / "leaf.txt"), "leaf.txt", False)]

        def fake_scandir(directory):
            return iter(children.get(str(directory), []))

        def fake_classify(scanned: ScannedFile, **_kwargs) -> ClassifiedFile:
            return ClassifiedFile(
                abs_path=scanned.abs_path,
                rel_path=scanned.rel_path,
                is_binary=False,
                size_bytes=5,
                binary_type=None,
                text="deep\n",
                char_count=5,
                line_count=1,
            )

        # 模拟 Python 3.13+ / Linux 的 Path.exists() 对超长路径抛
        # ENAMETOOLONG（Path.exists 抛、os.path.exists 返回 False 的差异）。
        # 生产代码必须改用 os.path.exists，否则 _walk 会在 ~836 层被
        # per-entry except OSError 吞掉异常并停止下钻，深树用例在 Linux CI
        # 与 macOS 上必然失败。
        real_path_exists = Path.exists

        def raising_exists(path_obj):
            if len(str(path_obj)) > 250:
                raise OSError(36, "File name too long")
            return real_path_exists(path_obj)

        with mock.patch.object(Path, "exists", raising_exists), mock.patch(
            "codeprism.scanner.os.scandir", side_effect=fake_scandir
        ), mock.patch("codeprism.api.classify", side_effect=fake_classify):
            result = self.pack()
        self.assertEqual(len(result.files), 1)
        rel = result.files[0].rel_path
        self.assertTrue(rel.endswith("/leaf.txt"))
        # 1200 层目录 + 文件，超过递归上限 1000 仍能完成扫描与渲染。
        self.assertEqual(rel.count("/"), depth)
        self.assertIn("d1199", result.markdown)
        self.assertIn("deep", result.markdown)

    def test_real_shallow_tree_end_to_end(self):
        """真实文件系统的浅树端到端覆盖（虚拟树不覆盖 mkdir/scandir 实体）。"""
        deep = self.tmp
        for _ in range(40):
            deep = deep / "d"
        deep.mkdir(parents=True)
        (deep / "leaf.txt").write_text("deep\n", encoding="utf-8")
        result = self.pack()
        self.assertEqual(len(result.files), 1)
        self.assertTrue(result.files[0].rel_path.endswith("/leaf.txt"))
        self.assertIn("deep", result.markdown)


class CaseFoldTests(TempProjectCase):
    @staticmethod
    def _volume_exists(real_exists, *, case_sensitive):
        """显式模拟卷的大小写敏感性，不依赖宿主机文件系统。

        Linux ext4 与大小写敏感的 APFS 会让真实的 ``os.path.exists(aa)``
        返回 False；若只在「敏感卷」分支 mock、把「不敏感卷」当默认假设，
        这两个用例会在 Linux CI 上必败。两种卷都必须显式模拟。
        """

        def fake(path):
            text = str(path)
            if ".codeprism-case-" in text and text.endswith("aa"):
                return not case_sensitive
            return real_exists(path)

        return fake

    def _darwin_volumes(self, *, case_sensitive):
        real_exists = os.path.exists
        return (
            mock.patch("codeprism.scanner.sys.platform", "darwin"),
            mock.patch(
                "codeprism.scanner.os.path.exists",
                side_effect=self._volume_exists(real_exists, case_sensitive=case_sensitive),
            ),
        )

    def test_darwin_probe_is_live_and_volume_scoped(self):
        platform_patch, exists_patch = self._darwin_volumes(case_sensitive=False)
        with platform_patch, exists_patch:
            self.assertTrue(_detect_case_fold(self.tmp))
        platform_patch, exists_patch = self._darwin_volumes(case_sensitive=True)
        with platform_patch, exists_patch:
            self.assertFalse(_detect_case_fold(self.tmp))
        # 旧实现的 pathconf 调用（恒抛 ValueError 的死代码）已移除。
        self.assertNotIn(
            "os.pathconf(",
            Path(__file__).parent.parent.joinpath("codeprism", "scanner.py").read_text(encoding="utf-8"),
        )

    def test_case_sensitive_volume_does_not_fold_patterns(self):
        self.write(".gitignore", "Docs/\n")
        self.write("docs/keep.py", "x")
        platform_patch, exists_patch = self._darwin_volumes(case_sensitive=True)
        with platform_patch, exists_patch:
            result = self.pack()
        self.assertIn("docs/keep.py", self.rel_paths(result))
        # 不折叠时 Docs/ 只匹配同名目录；折叠时（不敏感卷）会被忽略。
        platform_patch, exists_patch = self._darwin_volumes(case_sensitive=False)
        with platform_patch, exists_patch:
            folded = self.pack()
        self.assertNotIn("docs/keep.py", self.rel_paths(folded))

    def test_git_rejection_does_not_write_probe_on_darwin(self):
        # 精确 .git 必须在卷探测之前拒绝：macOS 探针会在打包根（此处即
        # .git 内部）创建临时目录，守卫本应阻止的目录不应被写入。
        self.write(".git/hooks/pre-commit", "x")
        with mock.patch("codeprism.scanner.sys.platform", "darwin"), mock.patch(
            "codeprism.scanner.tempfile.mkdtemp"
        ) as mkdtemp:
            with self.assertRaises(ValueError):
                Scanner(self.tmp / ".git" / "hooks")
        mkdtemp.assert_not_called()

    def test_case_probe_readonly_volume_warns_and_is_ignored(self):
        # 只读/不可写卷回退到不敏感时必须留下可追溯警告；残留的探针目录
        # 必须被默认忽略规则覆盖，不能被下一次打包扫入文档。
        from codeprism.defaults import DEFAULT_IGNORE_PATTERNS

        warned: list[str] = []
        with mock.patch("codeprism.scanner.sys.platform", "darwin"), mock.patch(
            "codeprism.scanner.tempfile.mkdtemp", side_effect=OSError(30, "Read-only file system")
        ):
            self.assertTrue(_detect_case_fold(self.tmp, warned))
        self.assertTrue(any("Cannot probe case sensitivity" in w for w in warned), warned)
        self.assertTrue(
            make_spec(DEFAULT_IGNORE_PATTERNS).check_file(".codeprism-case-abc123/").include
        )


class ExplicitTargetTests(TempProjectCase):
    def test_directory_spec_never_judges_the_directory_itself(self):
        self.write(".gitignore", "*\n")
        self.write("D/.gitignore", "*\n")
        self.write("D/keep.py", "x")
        scanner = Scanner(self.tmp)
        pushed = scanner._preload_ancestors(self.tmp / "D")
        calls: list[str] = []
        original = PathSpec.check_file

        def recording(spec, path):
            calls.append(path)
            return original(spec, path)

        try:
            with mock.patch.object(PathSpec, "check_file", recording):
                scanner._is_ignored("D", is_dir=True, ceiling=self.tmp / "D")
        finally:
            scanner._pop(pushed)
        self.assertNotIn("./", calls, "目录自身的 .gitignore 不得用 './' 判定该目录")

    def test_explicit_file_matches_full_pack_with_whitelist_gitignore(self):
        self.write("D/.gitignore", "*\n!keep.py\n")
        self.write("D/keep.py", "keep\n")
        self.write("main.py", "main\n")
        full = self.rel_paths(self.pack())
        by_dir = self.rel_paths(self.pack([self.tmp / "D", self.tmp / "main.py"]))
        by_file = self.rel_paths(self.pack([self.tmp / "D" / "keep.py", self.tmp / "main.py"]))
        self.assertEqual(full, by_dir)
        self.assertEqual(full, by_file)

    def test_explicit_file_cannot_escape_root_ignore_via_deep_negation(self):
        self.write(".gitignore", "D/\n")
        self.write("D/.gitignore", "*\n!*/\n!keep.py\n")
        self.write("D/keep.py", "secret\n")
        self.write("main.py", "main\n")
        full = self.rel_paths(self.pack())
        by_file = self.rel_paths(self.pack([self.tmp / "D" / "keep.py", self.tmp / "main.py"]))
        # 全量打包会额外带上 root 的 .gitignore 文件本身，显式选择不会；
        # 关键契约是两种入口都不得把被忽略目录里的文件放进上下文。
        self.assertNotIn("D/keep.py", full)
        self.assertNotIn("D/keep.py", by_file)
        self.assertIn("main.py", by_file)

    def test_explicit_nested_repo_target_is_skipped_like_walk(self):
        self.write(".git/HEAD", "ref: refs/heads/main")
        self.write("nested/.git/HEAD", "ref: refs/heads/main")
        self.write("nested/inner.py", "secret\n")
        self.write("a.py", "a\n")
        self.assertEqual(self.rel_paths(self.pack()), ["a.py"])
        for targets in (
            [self.tmp / "a.py", self.tmp / "nested"],
            [self.tmp / "a.py", self.tmp / "nested" / "inner.py"],
        ):
            result = self.pack(targets)
            self.assertEqual(self.rel_paths(result), ["a.py"])
            self.assertTrue(any("Nested repository skipped" in w for w in result.warnings), result.warnings)

    def test_nested_repo_boundary_precedes_outer_file_rules(self):
        # 外层的文件级/目录名规则不得先套用到嵌套仓库内部目标：边界
        # 判定必须发生在 preload 与 _is_ignored 之前。
        self.write(".gitignore", "*.py\nsrc/\n")
        self.write("nested/.git/HEAD", "ref: refs/heads/main")
        self.write("nested/inner.py", "secret\n")
        self.write("nested/src/deep.py", "secret\n")
        self.write("a.txt", "a\n")
        for targets in (
            [self.tmp / "a.txt", self.tmp / "nested" / "inner.py"],
            [self.tmp / "a.txt", self.tmp / "nested" / "src"],
        ):
            result = self.pack(targets)
            self.assertEqual(self.rel_paths(result), ["a.txt"])
            self.assertFalse(any("Skipped ignored target" in w for w in result.warnings), result.warnings)
            self.assertTrue(
                any("Nested repository skipped" in w and "nested" in w for w in result.warnings),
                result.warnings,
            )

    def test_excluded_nested_repo_boundary_stays_silent(self):
        # --exclude-path 指向嵌套仓库根时，内部目标应静默跳过，不再报
        # Nested repository skipped（与 _walk 对被忽略目录不告警一致）。
        self.write("nested/.git/HEAD", "ref: refs/heads/main")
        self.write("nested/inner.py", "x")
        self.write("a.txt", "a")
        result = self.pack(
            [self.tmp / "a.txt", self.tmp / "nested" / "inner.py"],
            exclude_paths=[self.tmp / "nested"],
        )
        self.assertEqual(self.rel_paths(result), ["a.txt"])
        self.assertFalse(any("Nested repository skipped" in w for w in result.warnings), result.warnings)

    def test_nested_repo_inner_gitignore_is_never_read(self):
        # 边界内部的 .gitignore（含坏规则与非 UTF-8 字节）不应被 preload，
        # 否则会向外层上下文吐出与嵌套仓库无关的 warnings。
        self.write("nested/.git/HEAD", "ref: refs/heads/main")
        self.write("nested/.gitignore", b"[z-a]\n\xff\xfe\n")
        self.write("nested/inner.py", "secret\n")
        self.write("a.txt", "a\n")
        result = self.pack([self.tmp / "a.txt", self.tmp / "nested" / "inner.py"])
        self.assertEqual(self.rel_paths(result), ["a.txt"])
        self.assertFalse(
            any("Invalid ignore pattern" in w or "Non-UTF-8" in w for w in result.warnings),
            result.warnings,
        )

    def test_nonexistent_target_inside_nested_repo_still_raises(self):
        # 边界前置不得把「不存在的显式目标」变成静默跳过：Scanner.scan()
        # 的内部 API 契约仍是抛 FileNotFoundError。
        self.write("nested/.git/HEAD", "ref: refs/heads/main")
        self.write("a.txt", "a\n")
        scanner = Scanner(self.tmp)
        with self.assertRaises(FileNotFoundError):
            scanner.scan([self.tmp / "a.txt", self.tmp / "nested" / "missing.py"])

    def test_git_metadata_below_git_dir_is_refused(self):
        self.write(".git/hooks/pre-commit", "secret\n")
        with self.assertRaises(ValueError):
            self.pack([self.tmp / ".git" / "hooks" / "pre-commit"])

    def test_git_case_variant_refused_on_case_insensitive_volume(self):
        # 不敏感卷上 .GIT 与 .git 是同一目录，根守卫必须按卷语义比较。
        self.write(".GIT/hooks/pre-commit", "secret\n")
        with mock.patch("codeprism.scanner._detect_case_fold", return_value=True):
            with self.assertRaises(ValueError):
                Scanner(self.tmp / ".GIT" / "hooks")

    def test_git_case_variant_allowed_on_case_sensitive_volume(self):
        # 敏感卷上 .GIT 只是普通目录名，不应被误拒绝。
        self.write(".GIT/hooks/pre-commit", "secret\n")
        with mock.patch("codeprism.scanner._detect_case_fold", return_value=False):
            scanner = Scanner(self.tmp / ".GIT" / "hooks")
            self.assertEqual(
                [f.rel_path for f in scanner.scan([self.tmp / ".GIT" / "hooks"])], ["pre-commit"]
            )

    def test_scanner_reuse_after_walk_exception_has_clean_spec_stack(self):
        self.write(".gitignore", "*.log\n")
        self.write("a.py", "a\n")
        scanner = Scanner(self.tmp)

        def broken_walk(directory, rel):
            # 模拟 _walk 已 push 一个深层 spec 后被非 OSError 异常中断。
            scanner._specs.append((directory / "deep", make_spec(["*.py"])))
            raise KeyboardInterrupt()

        with mock.patch.object(scanner, "_walk", side_effect=broken_walk):
            with self.assertRaises(KeyboardInterrupt):
                scanner.scan([self.tmp])
        self.assertGreater(len(scanner._specs), 0)
        # 下一次 scan 必须从干净的规则栈开始，不能沿用残留的深层规则。
        self.assertEqual([f.rel_path for f in scanner.scan([self.tmp])], [".gitignore", "a.py"])


class ClassificationContractTests(TempProjectCase):
    def test_utf16_containing_nul_is_inventoried(self):
        """含 NUL 的 UTF-16/32（ASCII/Latin 或换行）均按非文本入清单。"""
        self.write("ascii16.txt", "Write-Output hello\n".encode("utf-16"))
        self.write("cjk16_nl.txt", "中文文字\n".encode("utf-16"))
        self.write("ascii32.txt", "plain ascii\n".encode("utf-32"))
        self.write("plain.txt", "plain ascii\n")
        result = self.pack()
        by_rel = {f.rel_path: f for f in result.files}
        for rel in ("ascii16.txt", "cjk16_nl.txt", "ascii32.txt"):
            self.assertTrue(by_rel[rel].is_binary, rel)
            self.assertIsNone(by_rel[rel].text, rel)
        self.assertFalse(by_rel["plain.txt"].is_binary)
        self.assertIn("plain ascii", by_rel["plain.txt"].text)

    def test_utf16_without_nul_is_not_guaranteed_binary(self):
        """钉住真实边界：NUL 规则不是 UTF-16 的完备判据。

        纯 CJK 的 UTF-16 编码可完全不含 NUL（换行 U+000A 才会带来 0x00），
        此时文件按文本处理：带 BOM 的可被 chardet 正确解码，无 BOM 的
        LE/BE 可能被猜成 Windows-1252 而输出乱码（已知限制，见
        DEVELOPMENT.md）。文档/CHANGELOG 因此不得使用 always / 一律。
        """
        self.write("cjk_le.txt", ("中文文字" * 200).encode("utf-16-le"))
        self.write("cjk_be.txt", ("中文文字" * 200).encode("utf-16-be"))
        self.write("cjk_bom.txt", ("中文文字" * 200).encode("utf-16"))
        result = self.pack()
        by_rel = {f.rel_path: f for f in result.files}
        for rel in ("cjk_le.txt", "cjk_be.txt", "cjk_bom.txt"):
            self.assertFalse(by_rel[rel].is_binary, rel)
            self.assertIsNotNone(by_rel[rel].text, rel)
        self.assertIn("中文文字", by_rel["cjk_bom.txt"].text)


class RegressionEdgeTests(TempProjectCase):
    def test_preview_chars_zero_emits_no_encoding_warning(self):
        self.write("big.txt", b"\xff\xfe\xfd" + b"x" * 100)
        warnings: list[str] = []
        classified = classify(
            ScannedFile(self.tmp / "big.txt", "big.txt"),
            max_file_bytes=10,
            preview_chars=0,
            warnings=warnings,
        )
        self.assertEqual(classified.text, "")
        self.assertEqual(warnings, [])

    def test_decode_raw_fallback_does_not_claim_unused_encoding(self):
        # chardet 猜 UTF-16 但奇数长度导致 decode 抛 truncated data，
        # 最终走 errors="replace" 回退：不得返回未生效的 encoding。
        detector = mock.MagicMock()
        detector.result = {"encoding": "utf-16", "confidence": 0.9}
        with mock.patch("codeprism.classifier.UniversalDetector", return_value=detector):
            text, encoding, confidence, lossy = _decode_raw(b"ab\xff", tail_cut=False)
        self.assertTrue(lossy)
        self.assertIsNone(encoding)
        self.assertEqual(confidence, 0.0)
        self.assertIn("\ufffd", text)

    def test_decode_fallback_warning_is_loss_not_reencode(self):
        self.write("bad.txt", b"ab\xff")
        detector = mock.MagicMock()
        detector.result = {"encoding": "utf-16", "confidence": 0.9}
        warnings: list[str] = []
        with mock.patch("codeprism.classifier.UniversalDetector", return_value=detector):
            classify(ScannedFile(self.tmp / "bad.txt", "bad.txt"), warnings=warnings)
        self.assertFalse(any("Re-encoded" in w for w in warnings), warnings)
        self.assertTrue(any("decoding loss" in w for w in warnings), warnings)

    def test_clipboard_budget_uses_cheap_lower_bound(self):
        class EncodeSpy(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("字符数已超预算时不应再物化整份 bytes")

        with mock.patch("codeprism.cli._CLIPBOARD_MAX_BYTES", 4):
            self.assertTrue(_over_clipboard_budget(EncodeSpy("abcdef")))
            self.assertFalse(_over_clipboard_budget("ab"))
            self.assertTrue(_over_clipboard_budget("中文"))  # 2 字符 / 6 UTF-8 字节

    def test_silence_broken_pipe_redirects_both_streams(self):
        fake_sys = mock.Mock()
        fake_sys.stdout.fileno.return_value = 1
        fake_sys.stderr.fileno.return_value = 2
        with mock.patch("codeprism.cli.sys", fake_sys), mock.patch(
            "codeprism.cli.os.dup2"
        ) as dup2, mock.patch("codeprism.cli.os.open", return_value=99), mock.patch(
            "codeprism.cli.os.close"
        ):
            _silence_broken_pipe()
        self.assertEqual(dup2.call_count, 2)


if __name__ == "__main__":
    unittest.main()
