[English](README.md) | **简体中文**

# CodePrism

**面向 LLM 时代的确定性代码库 → Markdown 上下文打包工具。**

CodePrism 将一个工程目录（或任意选中的文件/文件夹集合）整合为一份结构化
`.md` 文档，可直接粘贴给任何 AI 助手。它刻意保持极简：不做 AST 提取、不做
密钥扫描、不发网络请求——只做纯粹、可复现的文件遍历，以及精心渲染的
Markdown 文档。

本 monorepo 包含两个交付物：

| 包 | 说明 |
| :--- | :--- |
| [`core/`](core) | `codeprism-core`：Python 3.10+ 引擎 + CLI（`codeprism`），可独立发布至 PyPI |
| [`vscode-extension/`](vscode-extension) | **CodePrism** VS Code 扩展：资源管理器右键菜单、命令面板、状态栏进度、一键打包 |

## 为什么选择 CodePrism

- **文本文件原样保留**（体积上限内；见下条）。不提取代码骨架；换行符统一归一为
  LF、剥离 UTF-8 BOM，使换行与 BOM 差异绝不影响输出与统计。
- **法律样板与超大文件预览式截断。** `LICENSE`/`COPYING` 类文件以及超过体积
  上限（默认 1 MiB，`--max-file-bytes`）的文本文件保留在文件树中，但正文只渲染
  前 N 个字符（可配置，0 = 完全省略）并附明确截断提示——不倾倒上千行协议条文，
  也不会因超大文件撑爆内存。
- **二进制资产只登记、不读内容。** 图片、字体、音视频等非文本文件会出现在
  文件树与资产清单表中，附相对路径、扩展名、人类可读体积与类型
  （`Image/PNG`、`Font/WOFF2` 等），让模型感知这些静态资源的存在与引用路径。
  分类**按以下顺序**执行：命中已知二进制扩展名立即判定（内容看似文本的
  `.png` 只登记、不内联，也不再嗅探正文），其次是开头 512 字节的内容嗅探，
  最后是实际读取内容中任何位置出现 NUL 字节。二进制文件绝不全文读取。
  **产品限制：**「扩展名优先」是有意的设计变更，目前没有关闭开关或强制文本
  模式：扩展名单独决定归类，与文件真实字节无关。
- **Git 风格过滤。** 嵌套 `.gitignore`（含否定规则 `!`）由 `pathspec`
  解析；`.git/`、依赖目录、构建产物、缓存与锁文件默认忽略。优先级从高到低：
  硬性工具 invariant（`.git`、CodePrism 自身输出与写入临时文件）、你的
  `--ignore-pattern` 规则、`.gitignore` 栈（越深越优先）、内置默认规则。
  `.gitignore` 否定规则可以重新纳入默认忽略的目录，但绝不能覆盖硬性
  invariant 或用户显式排除。显式选中的路径同样遵守忽略规则——Git 拒绝
  add 被忽略的文件，CodePrism 也一样（并给出警告）。
- **防逃逸渲染。** 代码围栏按被包裹内容中最长反引号串动态取 `N+1` 个反引号，
  Markdown 套 Markdown 也绝不会提前闭合、破坏文档。
- **确定性输出。** 路径与树条目稳定排序，重复运行字节级一致——有利于 LLM
  Prompt Cache 命中。
- **天生隐私。** 全部本地运行，工具在运行时绝不发起任何网络连接（构建扩展的
  内置依赖时会从 PyPI/npm 下载）。CodePrism **不做密钥检测**：分享文档前请确保
  敏感文件（`.env`、私钥、凭据）已被 `.gitignore` 或 `--ignore-pattern` 覆盖；
  文档还会记录绝对源路径（含用户名）。

## 输出格式

````markdown
# CodePrism Context: my-project

## 1. Summary
- **Source Directory**: `/path/to/my-project`
- **Total Files**: 42 (37 text files, 5 non-text files)
- **Total Characters / Lines**: 123456 chars / 4321 lines

## 2. File Tree
...制表符文件树，二进制标注为 `[Non-Text: .png, 18.4 KB]`...

## 3. Non-Text Files (Binary & Media Assets)
| File Path | Extension | Size | Type |
...

## 4. Text Files Content
### `src/main.py`
```python
# 完整原始源码
```
````

## 快速开始 — Python 引擎

```bash
pip install codeprism-core        # 或在本仓库内：pip install -e ./core
codeprism .                       # 生成 ./codeprism-context.md
codeprism src tests -o ctx.md     # 打包指定选择
codeprism . --copy                # 同时复制到系统剪贴板
codeprism . --stdout              # 输出到 stdout
codeprism . --json-stats          # 机器可读的统计行
codeprism . --include-lock-files --ignore-pattern "*.log"
codeprism . --exclude-path ./private.md   # 字面精确排除（文件或目录）
codeprism . --preview-chars 400 --truncate-pattern "NOTICES.md"
codeprism . --max-file-bytes 2097152   # 超限文本只取开头预览
```

## 快速开始 — VS Code 扩展

环境要求：VS Code 1.85+、本机 Python 3.10+（**无需 `pip install`**——引擎与
其纯 Python 依赖已随插件内置）。

- 命令面板 → **CodePrism: Pack Entire Workspace to Markdown**
- 资源管理器右键（单选或多选文件/文件夹）→ **Pack Selected to Markdown**
  或 **Copy Selected as Markdown to Clipboard**
- 生成的上下文文档编辑器标题栏上的刷新图标，一键重新打包

配置项表格与商店页详情见 [扩展 README](vscode-extension/README.md)。

## 仓库结构

```text
CodePrism/
├── core/                    # Python 引擎 (codeprism-core)
│   ├── codeprism/          # cli、scanner、classifier、tree_builder、markdown_renderer、api
│   └── tests/               # 标准库 unittest 测试套件
├── vscode-extension/        # VS Code 扩展 (TypeScript + esbuild)
│   ├── src/                 # extension、pythonRunner、explorerMenu、statusBar
│   ├── bundled/             # 引擎副本 + vendor 纯 Python 依赖
│   └── test/                # @vscode/test-electron 集成测试
├── scripts/                 # prepare-vendor.py + run-vendor.mjs 构建辅助
├── docs/DEVELOPMENT.md      # 本地开发、联调测试、上架清单
└── .github/workflows/       # ci.yml（core + 扩展测试）、release.yml（暂缓的发布）
```

## 开发与测试

```bash
python -m venv .venv && .venv/Scripts/pip install -e ./core   # Windows
python -m venv .venv && .venv/bin/pip install -e ./core       # macOS/Linux
python -m unittest discover -s core/tests                    # 核心单元测试
cd vscode-extension && npm install && npm run compile
npm run test:integration                                     # 真实 VS Code 集成测试
```

完整指南（含暂缓执行的 Marketplace / Open VSX 上架清单）：
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## 契约与边界情况

- **覆盖保护是头部启发式。** 文件前 64 字节以 `# CodePrism Context:` 开头
  即被视为 CodePrism 旧文档，可无需 `--force` 直接覆盖。恰好以该头部开头的
  外来文件会被静默替换——这是前缀检测，不是签名校验。非常规目标（目录、
  FIFO、设备文件）即使加 `--force` 也一律拒绝。
- **`--force` 与符号链接。** 输出先写临时文件再原子改名；输出路径上的符号
  链接本身被替换，绝不会写穿到链接目标。已有文件的权限位保持不变。
- **输出目录副作用。** 打包开始前即创建输出文件的父目录，因此即使打包随后
  失败，该目录也已存在。
- **编码。** 非 UTF-8 文本经 `chardet` 重编码，并以警告形式报告检测置信度；
  无法解码的字节以 U+FFFD 呈现并触发解码损失警告。预览块在多字节字符中间
  截断时会被静默裁剪（预览已明确标注为部分内容）；但预览块彻底不可解码时，
  会与全量读取一样发出解码损失警告。
- **增长中的文件。** 读取量以打开文件时观察到的大小为上限，正在写入的日志
  无法撑大自己的快照。
- **嵌套仓库。** 打包根内含 `.git` 的目录会被跳过并警告，请单独打包；
  直接打包 `.git` 目录本身会被拒绝。
- **大小写。** 忽略匹配在大小写不敏感的文件系统（Windows、macOS 不敏感卷）
  上折叠大小写；内容截断模式在所有平台均按大小写不敏感匹配。
- **`--stdout` 保真。** stdout 与文件输出字节级一致（含 ANSI 转义）；管道
  提前关闭（`| head`）时静默退出 0。stdout 模式下 `--json-stats` 的 JSON
  行走 stderr。
- **统计 JSON 兼容性。** `--json-stats` 每次运行输出一个 JSON 对象；字段
  可能随版本增加，已有字段名与含义保持稳定。
- **输出改名。** 只有*当前*输出名会自我排除。早先用其他 `--output` /
  `outputFileName` 名字生成的文档是普通文件，可能被再次打包；引擎硬性忽略
  的只有 `codeprism-context.md`。

## 非目标

不做密钥检测、不做 AST/tree-sitter 压缩、不接入任何大模型 API。
CodePrism 只是一个本地静态整理器，仅此而已。

## 许可证

GPL-3.0 —— 见 [LICENSE](LICENSE)。
