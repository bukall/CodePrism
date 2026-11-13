"""Default ignore patterns and file-type mapping tables.

The default ignore list is inspired by repomix's ``defaultIgnore.ts``: it removes
high-noise directories (VCS metadata, dependency trees, build output, caches)
without touching binary assets that live in normal source directories.
"""

from __future__ import annotations

# Tool invariants that no .gitignore rule may override: Git itself never
# tracks .git, and re-packing our own output or write transients is always a
# bug (matryoshka). Checked before user patterns and repository rules.
HARD_IGNORE_PATTERNS: list[str] = [
    ".git",
    "codeprism-context.md",
    ".codeprism-*.tmp",
]

# Applied with the lowest priority: explicit .gitignore rules (including
# negations) always win over these defaults.
DEFAULT_IGNORE_PATTERNS: list[str] = [
    ".hg/",
    ".svn/",
    "node_modules/",
    "bower_components/",
    "jspm_packages/",
    "__pycache__/",
    "*.py[cod]",
    ".venv/",
    "venv/",
    "env/",
    "ENV/",
    ".tox/",
    ".nox/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".cache/",
    ".parcel-cache/",
    ".turbo/",
    ".gradle/",
    ".vscode-test/",
    "dist/",
    "build/",
    "out/",
    "target/",
    ".next/",
    ".nuxt/",
    ".output/",
    ".svelte-kit/",
    "coverage/",
    ".nyc_output/",
    "*.egg-info/",
    ".eggs/",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    # Transient prepare-vendor / sync-core staging and backup artifacts,
    # plus the macOS case-sensitivity probe (scanner._detect_case_fold).
    # The probe is removed in a finally block, but a killed process can leave
    # it behind; it must never be packed into the next context document.
    ".vendor-build-*/",
    ".vendor-old-*/",
    ".vendor-req-*",
    ".codeprism-sync-*/",
    ".codeprism-old-*/",
    ".codeprism-case-*/",
]

# Text files larger than this are never read in full: only a leading preview
# is decoded (0 disables the limit).
DEFAULT_MAX_FILE_BYTES = 1024 * 1024

# Files whose full content adds little context value (legal boilerplate):
# they stay in the file tree, but only the first N characters are rendered,
# followed by an explicit truncation notice. Extend via --truncate-pattern /
# codeprism.truncateContentPatterns; N via --preview-chars /
# codeprism.truncatedPreviewChars (0 omits the content entirely).
TRUNCATE_CONTENT_PATTERNS: list[str] = [
    "LICENSE",
    "LICENSE.*",
    "LICENCE",
    "LICENCE.*",
    "COPYING",
    "COPYING.*",
]

# High-noise dependency lock files (toggleable via --include-lock-files).
LOCK_FILE_PATTERNS: list[str] = [
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pdm.lock",
    "pixi.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "packages.lock.json",
    "flake.lock",
]

# Extension -> fenced-code-block language identifier, in the spirit of
# files-to-prompt's Markdown mode.
EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".json": "json",
    ".jsonc": "jsonc",
    ".json5": "json5",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "mdx",
    ".html": "html",
    ".htm": "html",
    ".vue": "vue",
    ".svelte": "svelte",
    ".css": "css",
    ".scss": "scss",
    ".sass": "sass",
    ".less": "less",
    ".xml": "xml",
    ".xsl": "xml",
    ".svg": "svg",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "properties",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "fish",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".bat": "bat",
    ".cmd": "bat",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".m": "objectivec",
    ".mm": "objectivec",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".sql": "sql",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".proto": "protobuf",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".cmake": "cmake",
    ".mk": "makefile",
    ".dockerfile": "dockerfile",
    ".gradle": "gradle",
    ".groovy": "groovy",
    ".nim": "nim",
    ".zig": "zig",
    ".v": "v",
    ".sol": "solidity",
    ".tex": "latex",
    ".rst": "rst",
    ".adoc": "asciidoc",
    ".txt": "text",
    ".csv": "csv",
    ".tsv": "tsv",
    ".log": "log",
    ".diff": "diff",
    ".patch": "diff",
}

# Exact filenames that carry no useful extension.
SPECIAL_FILENAME_LANGUAGE_MAP: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Containerfile": "dockerfile",
    "Makefile": "makefile",
    "GNUmakefile": "makefile",
    "CMakeLists.txt": "cmake",
    "Jenkinsfile": "groovy",
    "Gemfile": "ruby",
    "Rakefile": "ruby",
    "Vagrantfile": "ruby",
    ".gitignore": "gitignore",
    ".gitattributes": "gitattributes",
    ".gitmodules": "gitmodules",
    ".editorconfig": "editorconfig",
    ".env": "dotenv",
    ".babelrc": "json",
    ".eslintrc": "json",
    ".prettierrc": "json",
}

# Extension -> human readable binary asset category. Detection itself is
# policy-driven (binary extension list first, then a 512-byte content sniff,
# then NUL bytes in the read content — see classifier.py); this table only
# labels the detected binaries.
BINARY_TYPE_MAP: dict[str, str] = {
    ".png": "Image/PNG",
    ".jpg": "Image/JPEG",
    ".jpeg": "Image/JPEG",
    ".gif": "Image/GIF",
    ".bmp": "Image/BMP",
    ".ico": "Image/ICO",
    ".webp": "Image/WebP",
    ".tif": "Image/TIFF",
    ".tiff": "Image/TIFF",
    ".avif": "Image/AVIF",
    ".heic": "Image/HEIC",
    ".psd": "Image/PSD",
    ".ttf": "Font/TTF",
    ".otf": "Font/OTF",
    ".woff": "Font/WOFF",
    ".woff2": "Font/WOFF2",
    ".eot": "Font/EOT",
    ".mp3": "Audio/MP3",
    ".wav": "Audio/WAV",
    ".flac": "Audio/FLAC",
    ".ogg": "Audio/OGG",
    ".m4a": "Audio/M4A",
    ".aac": "Audio/AAC",
    ".mp4": "Video/MP4",
    ".m4v": "Video/M4V",
    ".mov": "Video/MOV",
    ".avi": "Video/AVI",
    ".mkv": "Video/MKV",
    ".webm": "Video/WebM",
    ".zip": "Archive/ZIP",
    ".tar": "Archive/TAR",
    ".gz": "Archive/GZIP",
    ".tgz": "Archive/GZIP",
    ".bz2": "Archive/BZIP2",
    ".xz": "Archive/XZ",
    ".7z": "Archive/7Z",
    ".rar": "Archive/RAR",
    ".pdf": "Document/PDF",
    ".doc": "Document/DOC",
    ".docx": "Document/DOCX",
    ".xls": "Document/XLS",
    ".xlsx": "Document/XLSX",
    ".ppt": "Document/PPT",
    ".pptx": "Document/PPTX",
    ".sqlite": "Database/SQLite",
    ".sqlite3": "Database/SQLite",
    ".db": "Database/DB",
    ".mdb": "Database/MDB",
    ".exe": "Executable/EXE",
    ".dll": "Executable/DLL",
    ".so": "Executable/SO",
    ".dylib": "Executable/DYLIB",
    ".bin": "Binary/BIN",
    ".dat": "Binary/DAT",
    ".class": "Bytecode/Java",
    ".pyc": "Bytecode/Python",
    ".pyo": "Bytecode/Python",
    ".wasm": "Bytecode/WASM",
    ".o": "Object File",
    ".a": "Static Library",
    ".lib": "Static Library",
    ".obj": "Object File",
}


def language_for(rel_path: str) -> str:
    """Return the fenced-code-block language identifier for a relative path."""
    # rel_path is POSIX-normalised by the scanner; a literal backslash is a
    # legal POSIX name character, not a separator (same convention as
    # ClassifiedFile.extension).
    filename = rel_path.rsplit("/", 1)[-1]
    if filename in SPECIAL_FILENAME_LANGUAGE_MAP:
        return SPECIAL_FILENAME_LANGUAGE_MAP[filename]
    dot = filename.rfind(".")
    if dot <= 0:
        return ""
    return EXTENSION_LANGUAGE_MAP.get(filename[dot:].lower(), "")


def binary_type_for(rel_path: str) -> str:
    """Return a human readable category for a detected non-text file."""
    filename = rel_path.rsplit("/", 1)[-1]
    dot = filename.rfind(".")
    if dot <= 0:
        return "Binary"
    return BINARY_TYPE_MAP.get(filename[dot:].lower(), "Binary")
