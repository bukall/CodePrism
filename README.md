**English** | [简体中文](README.zh-CN.md)

# CodePrism

**Deterministic codebase-to-Markdown context packing for the LLM era.**

CodePrism turns a project directory (or any selection of files/folders) into a
single structured `.md` document you can paste into any AI assistant. It is
deliberately minimal: no AST extraction, no secret scanning, no network calls —
just a pure, reproducible file walk and a carefully rendered Markdown document.

Two deliverables live in this monorepo:

| Package | What it is |
| :--- | :--- |
| [`core/`](core) | `codeprism-core`, a Python 3.10+ engine + CLI (`codeprism`), publishable to PyPI |
| [`vscode-extension/`](vscode-extension) | **CodePrism** VS Code extension: Explorer context menu, command palette, status bar progress, one-click packing |

## Why CodePrism

- **Text files are included verbatim** (up to the size limit; see next bullet).
  No skeletonisation; line endings are normalised to LF and UTF-8 BOMs stripped
  so line-ending and BOM differences never affect output or statistics.
- **Legal boilerplate and oversized files are preview-truncated.**
  `LICENSE`/`COPYING`-like files and text files above the size limit (default
  1 MiB, `--max-file-bytes`) stay in the file tree but render only their first
  N characters (configurable, 0 = omit) plus an explicit truncation notice —
  no thousand-line licence dumps, no runaway memory use.
- **Binary assets are inventoried, never dumped.** Images, fonts, media and
  other non-text files appear in the file tree and an asset table with their
  relative path, extension, human-readable size and type (`Image/PNG`,
  `Font/WOFF2`, ...), so the model knows they exist and where they live.
  Classification is applied **in this order**: a known binary extension wins
  immediately (a text-looking `.png` is inventoried, never inlined, and its
  body is not sniffed), then a 512-byte content sniff, then a NUL byte
  anywhere in the content actually read. Binary payloads are never read in
  full. **Product limitation:** extension-first is a deliberate design change
  with no disable switch or forced-text mode — an extension alone decides the
  file's fate, regardless of its bytes.
- **Git-style filtering.** Nested `.gitignore` files (including negations)
  are parsed by `pathspec`; `.git/`, dependency trees, build output, caches and
  lock files are ignored by default. Priority, highest first: hard tool
  invariants (`.git`, CodePrism's own output and write transients), your
  `--ignore-pattern` rules, the `.gitignore` stack (deeper files win), then
  built-in defaults. A `.gitignore` negation can re-include a default-ignored
  directory but never a hard invariant or an explicit user exclusion.
  Explicitly selected paths respect ignore rules too — Git refuses to add
  ignored files, and so does CodePrism (with a warning).
- **Escape-proof rendering.** Fenced blocks use `N+1` backticks relative to the
  longest backtick run inside each file, so Markdown-inside-Markdown never
  breaks the document.
- **Deterministic output.** Stable sorting of paths and tree entries keeps
  repeated runs byte-identical — friendly to LLM prompt caching.
- **Private by construction.** Everything runs locally; the tool never opens a
  network connection at runtime (building the extension's bundled dependencies
  does download from PyPI/npm). CodePrism performs **no secret detection**:
  make sure sensitive files (`.env`, private keys, credentials) are covered by
  `.gitignore` or `--ignore-pattern` before sharing the document, which also
  records absolute source paths (including your user name).

## Output format

````markdown
# CodePrism Context: my-project

## 1. Summary
- **Source Directory**: `/path/to/my-project`
- **Total Files**: 42 (37 text files, 5 non-text files)
- **Total Characters / Lines**: 123456 chars / 4321 lines

## 2. File Tree
...box-drawing tree, binaries annotated as `[Non-Text: .png, 18.4 KB]`...

## 3. Non-Text Files (Binary & Media Assets)
| File Path | Extension | Size | Type |
...

## 4. Text Files Content
### `src/main.py`
```python
# full original source
```
````

## Quick start — Python engine

```bash
pip install codeprism-core        # or: pip install -e ./core from this repo
codeprism .                       # writes ./codeprism-context.md
codeprism src tests -o ctx.md     # pack a selection
codeprism . --copy                # also copy to the system clipboard
codeprism . --stdout              # print to stdout
codeprism . --json-stats          # machine-readable stats line
codeprism . --include-lock-files --ignore-pattern "*.log"
codeprism . --exclude-path ./private.md   # literal exclusion (file or directory)
codeprism . --preview-chars 400 --truncate-pattern "NOTICES.md"
codeprism . --max-file-bytes 2097152   # oversized text becomes a leading preview
```

## Quick start — VS Code extension

Requirements: VS Code 1.85+, Python 3.10+ on the machine (no `pip install`
needed — the engine and its pure-Python dependencies are bundled).

- Command Palette → **CodePrism: Pack Entire Workspace to Markdown**
- Explorer right-click (single or multi-select) → **Pack Selected to Markdown**
  or **Copy Selected as Markdown to Clipboard**
- Refresh icon in the editor title bar of a generated context document

See the [extension README](vscode-extension/README.md) for the settings table
and store listing details.

## Repository layout

```text
CodePrism/
├── core/                    # Python engine (codeprism-core)
│   ├── codeprism/          # cli, scanner, classifier, tree_builder, markdown_renderer, api
│   └── tests/               # stdlib unittest suite
├── vscode-extension/        # VS Code extension (TypeScript, esbuild)
│   ├── src/                 # extension, pythonRunner, explorerMenu, statusBar
│   ├── bundled/             # engine copy + vendored pure-Python deps
│   └── test/                # @vscode/test-electron integration suite
├── scripts/                 # prepare-vendor.py + run-vendor.mjs build helpers
├── docs/DEVELOPMENT.md      # local dev, integration testing, publishing checklist
└── .github/workflows/       # ci.yml (core + extension tests), release.yml (deferred publishing)
```

## Development & testing

```bash
python -m venv .venv && .venv/Scripts/pip install -e ./core   # Windows
python -m venv .venv && .venv/bin/pip install -e ./core       # macOS/Linux
python -m unittest discover -s core/tests                    # core unit tests
cd vscode-extension && npm install && npm run compile
npm run test:integration                                     # real VS Code integration tests
```

Full guide, including the (deferred) Marketplace / Open VSX publishing
checklist: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Contract & edge cases

- **Overwrite protection is a header heuristic.** A file whose first 64 bytes
  start with `# CodePrism Context:` is treated as a previous CodePrism document
  and may be overwritten without `--force`. A foreign file that happens to
  start with that header will be replaced silently — the check is a prefix
  test, not a signature. Non-regular destinations (directories, FIFOs, devices)
  are always refused, even with `--force`.
- **`--force` and symlinks.** The output is written to a temp file and renamed
  into place; a symlink at the output path is replaced itself, never written
  through to its target. Existing permission bits are preserved.
- **Output directory side effect.** The output's parent directory is created
  before packing starts, so it exists even if the pack later fails.
- **Encoding.** Non-UTF-8 text is re-encoded via `chardet` and reported as a
  warning with the detector's confidence; undecodable bytes surface as U+FFFD
  with a decoding-loss warning. A preview chunk that ends in the middle of a
  multi-byte character is trimmed silently (the preview is explicitly labelled
  partial), but a preview that is genuinely undecodable raises the same loss
  warning as a full read.
- **Growing files.** Reads are capped at the file size observed when the file
  was opened, so an actively written log cannot inflate its own snapshot.
- **Nested repositories.** A directory containing `.git` inside the pack root
  is skipped with a warning; pack it separately. Packing a `.git` directory
  itself is refused.
- **Case sensitivity.** Ignore matching folds case where the filesystem does
  (Windows, case-insensitive macOS volumes); content-truncation patterns match
  case-insensitively on every platform.
- **`--stdout` fidelity.** stdout is byte-identical to the file output (ANSI
  escapes included); a closed pipe (`| head`) exits 0 quietly. With
  `--json-stats`, the JSON line goes to stderr in stdout mode.
- **Stats JSON compatibility.** `--json-stats` emits one JSON object per run;
  fields may be added over time, existing field names and meanings are stable.
- **Renamed outputs.** Only the *current* output name self-excludes. A document
  generated earlier under a different `--output`/`outputFileName` name is an
  ordinary file and may be re-packed; the engine hard-ignores only
  `codeprism-context.md`.

## Non-goals

No secret detection, no AST/tree-sitter compression, no LLM API integration.
CodePrism is a local static organiser, nothing more.

## License

GPL-3.0 — see [LICENSE](LICENSE).
