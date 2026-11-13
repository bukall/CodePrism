# Development & Release Guide

Local development, integration testing, and the (currently deferred) store
publishing checklist for CodePrism.

> Documentation maintenance: the root README exists in two languages —
> `README.md` (English) and `README.zh-CN.md` (Simplified Chinese), cross-linked
> at the top of each file. Any change to documented behaviour, commands,
> counts or layout must be applied to **both** files in the same change.

## 1. Prerequisites

- Python **3.10+** (`python` / `python3` on PATH)
- Node.js **20+** and npm — `npm run compile` / tests also work on 18, but
  `@vscode/vsce` 3.2+ (used by `npm run package`) declares `node >= 20`
- VS Code 1.85+ (for extension development and integration tests)
- Git
- `pip install -e ./core` builds with PEP 639 metadata (`license =
  "GPL-3.0-only"` plus `license-files`), which requires **setuptools >= 77**
  in the isolated build environment; pip fetches it automatically, so no
  system-wide setuptools upgrade is needed (and older setuptools would treat
  the SPDX string as a file path). An editable install additionally prints one
  `BuildBackendWarning: Editable installation`; that is setuptools'
  `InformationOnly` reminder about same-named folders shadowing the package —
  not a deprecation, and emitted by every editable strategy (`compat` instead
  prints a real removal warning, so it is not an improvement). Use
  `pip install ./core` (non-editable) when a warning-free build log matters;
  it produces the identical wheel and only `-e` is needed for edit-without-
  reinstall development.
- `pip install packaging` — only needed to run `scripts/check-vendor-pins.py`
  locally (CI installs it explicitly; the script requires Python 3.11+)

## 2. Python core engine

```bash
# from the repository root
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -e ./core
python -m unittest discover -s core/tests -v
```

CLI smoke tests:

```bash
codeprism --version
codeprism core -o /tmp/smoke.md --json-stats
codeprism core --stdout | head -40
codeprism core --stdout --json-stats   # Markdown on stdout, JSON stats on stderr
codeprism core --ignore-pattern "*.md" --json-stats
codeprism core --preview-chars 0 --truncate-pattern "NOTICES.md"
```

`--copy` exercises the system clipboard (pyperclip); on headless Linux you need
`xclip`/`xsel` installed. If the copy fails the document is still written: the
CLI prints a warning, exits 0, and the JSON stats report `copied: false`
(same semantics as the extension).

## 3. VS Code extension

```bash
cd vscode-extension
npm install

# refresh the bundled engine copy after changing core/
npm run sync:core

# (re)vendor pure-Python runtime deps; auto-detects python3/python/py and
# skips the download when the vendor directory already matches the pins
npm run vendor

npm run compile        # tsc --noEmit + esbuild -> dist/extension.js
```

Press **F5** in VS Code (with `vscode-extension/` open as the workspace) to
launch an Extension Development Host with the extension loaded.

### Automated integration tests

```bash
npm run test:integration
```

> **Reproducing the integration run:** some Electron-hosted terminals export
> `ELECTRON_RUN_AS_NODE=1`, which makes `Code.exe` start in Node mode and try
> to load the temporary workspace path as a module. Clear it first with
> `unset ELECTRON_RUN_AS_NODE NODE_OPTIONS` (Git Bash / POSIX) or
> `$env:ELECTRON_RUN_AS_NODE=$null` (PowerShell). In Git Bash do **not** use
> `env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS npm run test:integration`:
> that form can silently do nothing yet return exit code 0. Always confirm a
> real run by finding `[suite] start` / `[suite] done` in the log, not by the
> exit status alone.

This syncs the bundled engine, vendors the runtime dependencies (skipped when
current), type-checks, builds the **production** esbuild bundle (the same
artifact `vsce package` ships) and compiles `test/`, then drives a real VS Code
instance via `@vscode/test-electron` against a temporary fixture workspace.
It asserts:

- `packWorkspace` creates `codeprism-context.md` with all four sections,
  verbatim text, gitignore filtering and binary annotations;
- `packSelected` with a multi-selection (file + folder) packs exactly the
  selection;
- selected paths named like CLI flags (`--stdout`, `--force`, `--`) are packed
  as files, never parsed as options;
- `refreshContext` re-runs the last pack, and falls back to a workspace pack
  when every previous target was deleted (output removed first, so the
  assertion proves regeneration);
- `copySelected` does not rewrite the workspace output file;
- `codeprism.additionalIgnorePatterns` is honoured;
- a glob-named custom output self-excludes in clipboard mode, including in a
  multi-root workspace, in every root (literal `--exclude-path`, not glob
  escaping);
- size-limited files render a leading preview with a notice;
- the overwrite modal keeps a foreign file on cancel and replaces it after
  confirmation (the engine receives `--force`);
- packing is refused when the workspace is untrusted (`isTrusted` stubbed
  where the VS Code build allows it).

The locally installed VS Code is preferred; set nothing to let test-electron
download a copy instead. If the local installation fails to launch (e.g. it
is mid-update), the runner retries once with a downloaded copy and a fresh
profile. Each run uses a fresh `--user-data-dir` profile so a previous run's
(possibly multi-root) workspace is never restored.

Some negative paths are verified below the integration level rather than
end-to-end: interpreter descendant-process cancellation and the 256/16 MiB
buffer caps are exercised with transpiled-runner probes (a real wrapper that
traps SIGTERM would need a native binary); vendor swap concurrency safety
rests on unique staging/backup names plus rollback; cross-drive selections
cannot be fixtured on single-drive CI runners. The core suite covers the
engine-side contracts (sniff read budget, growing files, symlink/FIFO
outputs, mkdir-before-pack side effect, deep trees). The 1200-level deep-tree
case builds an in-memory tree through a mocked `os.scandir` and therefore
runs **on every platform**, including Windows: the previous on-disk fixture
produced a ~6107-character path that exceeded Linux/macOS `PATH_MAX` (making
those CI legs error) while being skipped entirely on Windows. A separate real
40-level fixture still covers the on-disk end-to-end path.

Clipboard assertions capture at the `vscode.env.clipboard.writeText` API
boundary: VS Code 1.146+ routes the clipboard through the focused renderer,
and test-electron windows cannot read it back (a direct write/read probe
returns an empty string). The extension's contract — handing the generated
Markdown to the VS Code clipboard API — is what the suite verifies; the real
system clipboard works in focused user windows. Where the `env`-level stub
can be installed, the capture array is cleared before each copy command and
`writeText` must actually have been called (no false pass on stale content).
Where the clipboard API is sealed, only the success notification is asserted
and the suite logs that clipboard **content** is not verifiable in that build
(see Known limitations).

### Known limitations (documented, intentional)

- Ignore priority, highest first: hard tool invariants (`.git`,
  `codeprism-context.md`, `.codeprism-*.tmp` — never overridable), explicit
  user patterns (`--ignore-pattern` / `additionalIgnorePatterns`), the
  `.gitignore` stack (deepest first), built-in defaults last. A `.gitignore`
  negation can re-include default-ignored noise such as `node_modules/`
  (Git-style) but never a hard invariant or an explicit user exclusion.
  Explicitly named subdirectory targets respect their own ignore status;
  `--exclude-path` matches the named path only (an explicitly listed file
  inside an excluded directory is still packed).
- Only `.gitignore` files are honoured; `core.excludesFile` and
  `.git/info/exclude` are not read.
- Symlinks/junctions encountered during the walk are skipped; an explicitly
  passed symlink target is resolved and packed.
- A previous CLI run with a custom `-o name.md` is not auto-excluded on later
  `--stdout`/`--copy` runs; add `--ignore-pattern name.md` (the default name
  `codeprism-context.md` is always excluded, and the extension excludes its
  configured output name automatically).
- In multi-root workspaces one document is produced per pack; resource-scoped
  settings are read from the primary (first) target.
- On Windows, interpreter shims that are `.cmd`/`.bat` wrappers cannot be
  spawned without a shell (Node CVE-2024-27980 hardening); set
  `codeprism.pythonPath` to the real `python.exe` in that case.
- `codeprism.pythonPath` is machine-overridable: a workspace can override it,
  so only run CodePrism in folders you trust.
- Classification is policy-driven and **ordered**: a known binary extension
  wins immediately (a text-looking `.png` is inventoried, never inlined, and
  its body is not sniffed), then a 512-byte content sniff, then a NUL byte
  anywhere in the bytes actually read. **Product limitation (accepted):** the
  extension rule is a deliberate behaviour change with no disable switch and
  no forced-text mode; an extension alone decides the file's fate, so a
  text-looking `.png`/`.bin`/`.dat` never reaches the document body. It is not
  a fix with no functional impact. A UTF-8 file consisting of a single repeated
  multi-byte character with no ASCII or newlines (e.g. `测` × 4000) may still
  be classified as binary by binaryornot's decision tree; mixed CJK/ASCII text
  and GBK text are unaffected.
  **NUL on raw bytes is decisive** (deliberate behaviour change): any NUL in
  the bytes actually read makes the file non-text, which is why UTF-16/UTF-32
  text containing ASCII/Latin characters or a newline (U+000A is `0A 00` in
  UTF-16LE) is inventoried rather than inlined — including BOM-prefixed,
  ASCII-heavy PowerShell logs. The reason is C-string clipboard truncation.
  **NUL is not a complete UTF-16 detector**: UTF-16 text consisting only of
  CJK characters can contain no NUL byte at all. Such files are classified as
  text: BOM-prefixed ones may be decoded correctly by chardet, while BOM-less
  UTF-16LE/BE can be guessed as Windows-1252 and inlined as mojibake. That is
  an accepted product limitation, not a guarantee of lossless decoding.
  Pinned by `test_utf16_containing_nul_is_inventoried` and
  `test_utf16_without_nul_is_not_guaranteed_binary`.
- `codeprism.pythonPath` must be a plain executable path, not a command line
  (`py -3` is not parsed); point it at the real interpreter instead.
- The extension activates on `onStartupFinished` to offer the always-visible
  status-bar entry; the cost is a small startup overhead even for users who
  never pack. Packing is refused in untrusted workspaces, and the extension
  declares `extensionKind: workspace` so remote windows run the host (and
  thus Node `fs`) on the remote side where the files live.
- Malformed `.gitignore` lines (illegal ranges such as `[z-a]`, a trailing
  backslash) are skipped individually with a file:line warning; the remaining
  rules in the same file stay in effect. Invalid `--ignore-pattern` /
  `--truncate-pattern` arguments fail fast with a friendly CLI error.
- Hard ignore invariants protect exactly `.git`, the default output name
  `codeprism-context.md` and `.codeprism-*.tmp` write transients — not an
  arbitrary `-o custom.md`. Callers using a custom name must exclude that
  file themselves (`--exclude-path` / `--ignore-pattern`); the extension
  passes `--exclude-path` for its configured output name in every root.
- Git repository boundaries are detected by the **existence** of a `.git`
  entry (file or directory) only. `gitdir:` indirection, worktree/submodule
  metadata and `git rev-parse` are not consulted; a stray file named `.git`
  is treated as a boundary. This is an intentional "existence is the
  boundary" contract, not Git-faithful metadata parsing.
- `_norm()` only calls `realpath()` when the path string contains `~` (a
  performance heuristic for Windows 8.3 short names). Junctions, symlinks,
  case aliases and short names presented in an already-canonical form are not
  fully normalised; duplicate/exclude matching therefore relies on the
  caller-resolved paths, not a complete realpath map.
- Every non-UTF-8/ASCII text file emits its own re-encoding warning with the
  chardet guess and confidence. A GBK/Big5-heavy project will produce a long
  `warnings` list, CLI stderr and extension notification. This per-file
  traceability is intentional; the guess can be wrong (documented in the
  warning itself).
- Remote URI handling relies on `extensionKind: workspace`: in Remote-SSH /
  WSL / Dev Container windows the extension host (and therefore Node `fs`)
  runs on the remote side where the files live, so `uri.fsPath` is a local
  path for that host. `vscode.Uri.file(real).with({ scheme, authority })` only
  preserves URI identity; it does not translate filesystem access. Existence
  checks use `vscode.workspace.fs.stat` (scheme-aware), while bounded header
  reads (`isCodeprismDocument`), copy-after-generate reads and realpath
  resolution use Node `fs` on that same host; `realpath` is asynchronous so a
  slow network filesystem cannot block the host. The VS Code `fs` API has no
  bounded partial read, and slurping a multi-GB file just to inspect 64 bytes
  would be worse. End-to-end Remote-SSH/WSL/Dev Container runs are covered by
  the manual UI checklist (item 7), not by the fixture-based integration suite.
- Pre-flight output checks in the extension (`pathExists`, document-header
  check, overwrite confirmation) are **not atomic**: the target can be swapped
  for a symlink, directory or FIFO between the check and the engine write.
  Workspace Trust does not stop a concurrent process in a trusted folder. The
  authoritative boundary is the core engine's own final write checks
  (non-regular destination refused, atomic `os.replace` of a freshly created
  temp file); the extension's pre-checks are UX only.
- Process-tree cancellation on POSIX uses `detached` spawn plus group
  `SIGTERM`, with a `SIGKILL` escalation timer that is deliberately **not**
  cleared on `close`/`error` (a wrapper's grandchild can outlive the direct
  child and keep the group alive). Accepted residual race: if the group is
  gone and its PID is reused as a new group leader inside the 2s window, the
  late `process.kill(-pid, SIGKILL)` can hit an unrelated group. This is the
  inherent PID-reuse race of tree-kill implementations; `spawn` errors and
  group-creation failures fall back to killing the direct child only.
- `whenIdle()` resolves when the **direct** child closes, not when the whole
  process group is confirmed gone. `runPack`'s `finally` therefore applies a
  bounded 5s wait before releasing the `running` guard; a stubborn grandchild
  can still be alive when the next pack starts. The 2s escalation tries to
  kill the group but provides no exit confirmation. Keep this in mind when
  packing twice into the same output path in rapid succession after cancel.
- `fromPythonExtension()` times out waiting for `ms-python` activation after
  10s, but `ext.activate()` is not cancellable: the activation keeps running
  in the background (its late rejection is logged, not left unhandled) while
  interpreter probing continues with the PATH candidates.
- Clipboard output uses one **256 MiB UTF-8 byte budget** in all three modes:
  CLI `--copy` (encoded markdown length, with a cheap `len(str)` lower-bound
  check first), the extension's copy-after-generate (filesystem size), and
  `PythonRunner`'s stdout buffer (`Buffer.byteLength`, not `string.length`,
  which counts UTF-16 code units). **Exceeding it does not have one uniform
  outcome**: CLI `--copy` and copy-after-generate have already written the
  document, so only the copy is skipped (a warning; `copied: false`); in
  copy-only / stdout mode there is no file at all — `runner.run` rejects and
  the pack is reported as failed (as the extension README states). The file
  size used by copy-after-generate assumes the file was written as UTF-8.
  stderr has a separate 16 MiB byte budget.
- `--stdout` pins both streams to UTF-8 + LF via `reconfigure`, so stdout is
  byte-identical to file output even under a non-UTF-8 locale or console code
  page (`test_stdout_utf8_under_non_utf8_locale`). A destination write into a
  filesystem root is not forbidden by the CLI (explicit user intent); the
  extension refuses it with a root guard. When the default output parent is
  unwritable, the CLI reports a friendly "pass -o" error instead of a bare
  `PermissionError`.
- In integration-test builds where `vscode.env.clipboard` is sealed and cannot
  be replaced at the `env` level, the suite can only assert the success
  notification; the **clipboard content is not verifiable** in that build.
  Where the stub succeeds, `captured` (and the notification log) are cleared
  before every copy command and the suite asserts that `writeText` was actually
  called, so a silent no-op cannot pass on stale content in either branch.
- **Accepted residual races (not "solved"):** the extension's pre-flight
  output checks are not atomic (TOCTOU between check, overwrite confirmation
  and engine write); POSIX group SIGKILL escalation can theoretically hit a
  reused PID; `whenIdle()` resolves on the direct child's `close`, so a
  grandchild may still be alive when the bounded 5s wait releases the running
  guard. The core engine's final write checks and atomic replace are the real
  safety boundary; these races are documented trade-offs, not guarantees.
- **Remote URI conclusion is architectural, not end-to-end verified**: with
  `extensionKind: ["workspace"]` the extension host (and Node `fs`) runs on
  the side where the files live, so `uri.fsPath` and Node `fs` are consistent
  in Remote-SSH / WSL / Dev Container. No real remote-host integration run
  exists in CI; manual checklist item 3 covers it. The code is not
  scheme-aware at the API boundary by itself.
- **macOS case-sensitivity probe writes a transient directory** named
  `.codeprism-case-*` into the pack root on every `Scanner` construction
  (only on darwin; Windows/Linux need no probe). It is removed in a `finally`
  block with `unlink`/`rmdir`, and the name is covered by both
  `DEFAULT_IGNORE_PATTERNS` and `.gitignore` so a killed process cannot make
  it appear in a later context document. On a read-only/unwritable volume the
  probe falls back to case-insensitive and emits a warning — which can
  over-ignore on a case-sensitive volume. `--no-gitignore` still probes,
  because case folding governs all ignore specs (hard/user/default), not just
  `.gitignore`. This is an accepted side effect of a tool otherwise marketed
  as a read-only packer, alongside the pre-existing output-parent creation.
- For size-limited files the NUL check only covers the preview bytes actually
  read (leading `preview_chars*4+4` bytes); a NUL later in an oversized file
  can be missed and the file shown as a text preview. Oversized files are
  never read in full by design.
- BOM-less UTF-16/UTF-32 made only of CJK characters may contain no NUL and
  can be inlined as mojibake (see the classification contract above); this is
  an accepted product limitation, not a fully solved decoding path.
- Default noise ignores such as `env/`, `ENV/`, `dist/`, `build/`, `out/` and
  `target/` match **any** directory with that name (repomix-style). A source
  folder named `src/env/` needs a `.gitignore` negation or
  `--ignore-pattern '!env/'` to be packed.
- `_output_mode()` briefly sets `umask(0)` to read the current mask; in a
  multi-threaded host embedding the library this is not thread-safe. The CLI
  itself is single-threaded.
- **Comment language convention:** code comments and test docstrings are
  written in Chinese (the project's standing convention); user-facing docs
  remain English with a Chinese root README. Mixed-language comments in the
  same file are intentional, not drift.

### Manual UI checklist

1. Open any project folder; Command Palette → *CodePrism: Pack Entire
   Workspace to Markdown* → file opens in a new tab, status bar shows counts.
2. Explorer: right-click a folder → *Pack Selected to Markdown*.
3. Explorer: Ctrl/Cmd-click several files and folders → *Copy Selected as
   Markdown to Clipboard* → paste into an editor to verify. Clipboard writes
   go through `vscode.env.clipboard`, so this also works in Remote-SSH/WSL
   sessions without xclip/xsel on the remote host.
4. Open the generated `.md` → refresh icon appears in the editor title bar →
   click it after editing a source file → content updates.
5. Cancel a long pack from the progress notification → clean cancellation
   message, status bar returns to idle.
6. Settings: toggle `ignoreLockFiles`, add `additionalIgnorePatterns`, set a
   bogus `pythonPath` → clear error notification with *Show Log*.
7. No-folder window (Windows): open loose files via *File > Open File* (no
   folder opened), right-click an entry under *Open Editors* in the Explorer →
   *Pack Selected to Markdown* → the document lands next to the common parent
   directory with a canonical `file:` URI (exercises the workspace-less output
   branch, which the fixture-based suite cannot reach).

### Packaging a .vsix locally

```bash
npm run package          # runs vscode:prepublish (sync:core + vendor + production esbuild) + vsce
code --install-extension codeprism-0.1.0.vsix   # try it in your own VS Code
```

The `.vsix` contains `dist/`, `bundled/` (engine + vendor), `media/icon.png`,
README, CHANGELOG and LICENSE. `src/`, `test/`, `node_modules/` and build
config are excluded via `.vscodeignore`.

### Regenerating the store icon

```bash
npm run icon             # procedural 128x128 PNG, no image dependencies
```

## 4. Publishing checklist (DEFERRED — do not run yet)

Publishing to the stores is intentionally postponed. When ready:

### VS Code Marketplace

1. Create a publisher at <https://marketplace.visualstudio.com/manage>
   (the ID must match `publisher` in `vscode-extension/package.json`,
   currently `codeprism`; change the manifest if you pick another ID).
2. Create an Azure DevOps Personal Access Token:
   <https://dev.azure.com> → User settings → Personal access tokens →
   New Token → Organization: **All accessible organizations** →
   Scopes: **Marketplace → Manage** → copy the token.
3. Publish:

   ```bash
   cd vscode-extension
   npx vsce login <publisher-id>     # paste the PAT, or set VSCE_PAT env var
   npx vsce publish                  # or: vsce publish --packagePath codeprism-0.1.0.vsix
   ```

### Open VSX

1. Create an account at <https://open-vsx.org>, then Settings → Access Tokens
   → create a token.
2. Create the namespace (must match the publisher ID) and publish:

   ```bash
   npx ovsx create-namespace <publisher-id> -p <token>
   npx ovsx publish codeprism-0.1.0.vsix -p <token>
   ```

### GitHub Actions automation

`.github/workflows/ci.yml` runs on every push to `main` and on every pull
request: core unit tests on Ubuntu, Windows **and macOS** × Python
3.10/3.12/3.14 (the Windows leg covers junction handling, the macOS/Windows
legs cover case-folded ignore matching; `fail-fast: false` keeps all legs
running for diagnosis), plus the extension integration suite on Ubuntu 22.04
(`xvfb`, Electron runtime libraries installed explicitly), **Windows and
macOS**, validating the production bundle. A dedicated `core-min-deps` job
installs every dependency at its exact `pyproject.toml` lower bound on Python
3.10 (notably `pathspec==1.0.0`, the first release with the `simple` backend)
so an accidental bound relaxation fails immediately. `pyperclip==1.8.0` has
only an sdist on PyPI, so that one lower bound is built from source; the other
four lower bounds have wheels. A pin-range check
(`scripts/check-vendor-pins.py`) asserts the exact vendor pins satisfy
`core/pyproject.toml`'s library ranges and cover every dependency. All
third-party actions in ci.yml are pinned to full commit SHAs (version in a
comment); release.yml publishing is deferred and still uses version tags.

Test counts quoted in review reports are measurable from the tree: the
initial snapshot had **86** `def test_` cases, round 6 had 135, round 7 had
155, and the current tree has 185 (including the vendor-integrity,
case-fold/volume, nested-repo-boundary, deep-tree, internal-API,
RECORD-anchor, compiled-artifact, unrecorded-file and link-rejection cases).
Any "N/N passing" claim must be re-measured with
`python -m unittest discover -s core/tests -v` (and the min-deps shadow),
`npm run compile`, and — where a real VS Code is available —
`npm run test:integration`; the source tree alone does not prove a CI run,
a vsix scan or a store release.

`.github/workflows/release.yml` runs on `v*` tags (and manual dispatch):
core unit tests on Python 3.10/3.12/3.14 → sync + vendor + build + `vsce
package` → attach the `.vsix` to the GitHub Release → publish to Marketplace
and Open VSX **only if** the repository secrets `VSCE_PAT` / `OVSX_PAT` are
configured (otherwise those steps skip cleanly). Publishing governance is
**deferred by an explicit user decision** and is the largest documented
residual risk: the workflow still has top-level `permissions: contents:
write`, unpinned version-tag actions (including `softprops/action-gh-release@v2`
and `actions/upload-artifact@v4`), `npx --yes` for `vsce`/`ovsx`, and no
tag guard on the publish jobs (a `workflow_dispatch` run can publish). It is
intentionally unchanged here; tackle tag guards, per-job least privilege and
SHA pinning together when publishing is enabled. Two further deferred
sub-items belong to the same decision: `package-extension` only
`needs: core-tests`, so the real VS Code integration suite (and the `npm run
check` type-check included in `compile`) does not gate a store release; and
`ci.yml` has no dependency caching (`npm ci` / `pip install` run in full on
every matrix leg), which is a pure cost issue with no correctness impact.
Both are recorded here rather than silently omitted from the risk register.

Release flow when ready:

```bash
# bump versions in core/codeprism/__init__.py and vscode-extension/package.json
git tag v0.1.0 && git push origin v0.1.0
```

## 5. Updating vendored dependencies

`bundled/codeprism/` and `bundled/vendor/` are **generated artifacts**: they are
git-ignored (so packing this repository never duplicates the engine or dumps
third-party sources) and are recreated by `sync-core.mjs` /
`prepare-vendor.py`, including in CI before packaging. The vsix still ships
them because vsce packages from the working directory.

`bundled/vendor/` holds pure-Python wheels resolved for Python 3.10 (the
extension minimum). A `_codeprism_vendor.json` stamp records the exact pins
(plus an informational `recordDigests` map). `npm run vendor` skips the
download only when the pins match, each RECORD's **normalised** sha256 equals
the `RECORD_SHA256` anchor hardcoded in `prepare-vendor.py`, every
RECORD-listed file is byte-identical to its recorded sha256/size, and every
RECORD path resolves inside the vendor directory (packaging stays offline,
but a locally tampered `pathspec/pathspec.py` can no longer be shipped
silently). Normalisation deliberately drops the OS-dependent console-script
rows, the `__pycache__/*.cpython-<XY>.pyc` rows (pip writes them with the
**host** interpreter's tag) and installer metadata (`REQUESTED`/`INSTALLER`/
`direct_url.json`), so the anchor is stable across OS, Python version
(3.10–3.14) and pip version; `pip install` runs with `--no-compile` for the
same reason. The download path verifies the staged RECORDs against the
anchor before swapping, so a pin bump that forgets the anchor fails loudly
instead of silently producing a tree that every later run considers stale. Only wheel-spec RECORD-self rows may omit a hash; unknown/empty
digests and absolute/`..` paths are rejected. An older stamp without
`recordDigests` is atomically upgraded after verification, but the stamp is
**not** the security boundary: it lives inside the same writable directory as
the code. **Trust-root boundary:** the hardcoded `RECORD_SHA256` map and
`WHEEL_SHA256` (with `pip --require-hashes`) are the only out-of-directory
anchors. A party able to rewrite code + RECORD + stamp cannot pass while the
anchors are intact; a party able to rewrite `prepare-vendor.py` itself (or
`dist/extension.js`) trivially can. Compiled artefacts are rejected outright
(`*.pyc` is in `COMPILED_SUFFIXES` and any `__pycache__` directory fails the
currency check), because a `.pyc` with a matching source mtime/size would
otherwise be loaded by `import` without changing the source bytes. The check
also enumerates the vendor tree and rejects any regular file that no RECORD
lists (e.g. a planted `qtpy.py`, which `pyperclip` would import), any
`bin/`/`Scripts/` entry, and any symlink/junction/reparse point: the directory
is on `sys.path`, so an unrecorded module would run without changing a single
byte of a recorded file. Links are rejected because `rglob` does not recurse
into symlinked directories and `is_file()` is false for them, which would
otherwise hide an entire external package tree. `--force` re-establishes the tree from
hash-locked wheels. When bumping a pin, update both `WHEEL_SHA256` and
`RECORD_SHA256` — `check-vendor-pins.py` enforces the coverage. To refresh:

```bash
npm run vendor                       # in vscode-extension/; staging + atomic swap
python scripts/prepare-vendor.py --force   # force a re-download
npm run sync:core                    # in vscode-extension/
npm run test:integration             # verify in a real VS Code
```

prepare-vendor builds into a temporary staging directory and only replaces
`vendor/` after pip succeeds and the compiled-artifact check passes, so a
failed run can never leave the extension with an empty vendor directory.
Downloads are hash-locked: every wheel's sha256 is hardcoded in
`prepare-vendor.py` and pip runs with `--require-hashes --no-deps`, so a
substituted or corrupted artifact fails the build instead of executing.
The currency check also rejects an empty RECORD (a hollow dist-info proves
nothing) and verifies every listed file against its RECORD sha256/size.
Console-script directories are removed for **both** sysconfig schemes
(`bin` on POSIX, `Scripts` on Windows): otherwise a Windows build machine
would ship `Scripts/chardetect.exe` and make the vsix depend on the build
OS. Keep the vendored set pure Python only; the extension must work on
any OS without a compiler. When bumping a pin, recompute the wheel hash
(`pip download --python-version 3.10 --only-binary=:all: --no-deps`) and
update **both** `WHEEL_SHA256` and `RECORD_SHA256` in the same commit
(`check-vendor-pins.py` checks that every pin has an anchor and, when a local
vendor tree exists, that the anchor matches it).
