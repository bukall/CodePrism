# codeprism-core

Deterministic codebase-to-Markdown context packer. Point it at a project
directory (or a set of files/directories) and get one structured `.md` file
ready to paste into any LLM.

- **Text files are included verbatim** within the size limit — no AST
  extraction (line endings normalised to LF, UTF-8 BOM stripped).
- **LICENSE-like and oversized files are preview-truncated** (first 200 chars
  by default; `--preview-chars`, `--truncate-pattern`, `--max-file-bytes`),
  with an explicit notice; they remain listed in the file tree. Oversized
  files are never read in full.
- **Non-text files are listed, never dumped**: path, extension, size and type
  appear in the file tree and the asset inventory. Classification runs in a
  fixed order: a known binary extension wins immediately (a text-looking
  `.png` is inventoried without sniffing its body), then a 512-byte content
  sniff, then a NUL byte anywhere in the read content. Extension-first is a
  deliberate design change and an explicit product limitation (no disable
  switch or forced-text mode). Binary bytes are never included.
- **Nested `.gitignore` respected** via `pathspec` (negations included), plus
  built-in high-noise defaults (`.git/`, `node_modules/`, lock files, caches).
- **Escape-proof Markdown**: fences are sized `N+1` against the longest
  backtick run inside each file.
- **Deterministic ordering** for prompt-cache friendliness.
- **No network access, no secret scanning, no LLM calls.** Ensure sensitive
  files are gitignored (or `--ignore-pattern` / `--exclude-path`) before
  sharing the output; the Summary records absolute source paths.

## Install

```bash
pip install codeprism-core   # once published; from this repo: pip install -e ./core
```

## Usage

```bash
codeprism .                          # -> ./codeprism-context.md
codeprism src tests -o context.md    # pack selected directories
codeprism . --copy                   # also copy to clipboard
codeprism . --stdout                 # print Markdown to stdout (LF on every OS)
codeprism . --json-stats             # stats JSON on stdout (stderr with --stdout)
codeprism . --include-lock-files     # keep package-lock.json & friends
codeprism . --ignore-pattern "*.log" --ignore-pattern "docs/**"
codeprism . --exclude-path ./private.md   # literal exclusion (file or directory)
codeprism . --preview-chars 400 --truncate-pattern "NOTICES.md"
codeprism . --max-file-bytes 2097152 # oversized text becomes a leading preview
codeprism . -o notes.md --force      # explicitly overwrite a non-CodePrism file
```

Python API:

```python
from pathlib import Path
from codeprism import pack

result = pack([Path(".")])
print(result.stats.total_files)
Path("context.md").write_text(result.markdown, encoding="utf-8")
```

Requires Python 3.10+. Licensed GPL-3.0. Part of the
[CodePrism](https://github.com/bukall/CodePrism) monorepo.
