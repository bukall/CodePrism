# Change Log

## 0.1.0

- Initial release.
- Pack entire workspace or Explorer selection (multi-select supported) to a
  deterministic Markdown context file.
- Copy-to-clipboard command and optional copy-after-generate setting; clipboard
  writes use the VS Code API and work over Remote-SSH / WSL / DevContainers.
- Nested `.gitignore` filtering with per-line tolerance for malformed patterns,
  default high-noise ignores, lock-file toggle, extra ignore patterns and
  literal output-path self-exclusion. Ignore priority: hard invariants
  (`.git`, own output) > explicit user patterns > `.gitignore` > defaults.
- Binary asset inventory (path, extension, size, type). Classification runs
  in a fixed order — binary extension list first (extension alone decides;
  no disable switch), then a 512-byte content sniff, then NUL bytes in the
  read content; full bytes are never read. **Behaviour change:** a NUL byte
  in the raw bytes is now decisive, so UTF-16/UTF-32 text that contains NUL
  (ASCII/Latin characters or a newline — including BOM-prefixed, ASCII-heavy
  PowerShell logs) is inventoried as non-text instead of being inlined; this
  avoids C-string clipboard truncation. UTF-16 consisting only of CJK
  characters can contain no NUL and is still classified as text (BOM-less
  variants may be mis-decoded by chardet) — a known product limitation.
- Licence-style content truncation plus a configurable file-size limit
  (`codeprism.maxFileBytes`, default 1 MiB): oversized text renders a leading
  preview with an explicit notice instead of being read in full.
- Overwrite protection: a foreign output file requires CLI `--force` or the
  extension's modal confirmation (wired through to the engine).
- Dynamic N+1 backtick fences to prevent Markdown escape.
- Bundled Python engine with vendored dependencies (Python 3.10+ required).
- Status bar progress with truncation/size-limit counts, cancellable
  notifications, editor title refresh button. Engine warnings (skipped rules
  or files, decoding loss) surface as a notification and in the tooltip.
- Workspace Trust aware: packing is disabled in untrusted workspaces; the
  extension runs on the workspace (remote) side in remote windows.
