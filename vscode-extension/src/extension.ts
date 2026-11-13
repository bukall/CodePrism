import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { registerExplorerCommands, workspaceRoots } from "./explorerMenu";
import { PythonRunner } from "./pythonRunner";
import { notifyError, notifyResult, StatusBar } from "./statusBar";

interface LastPack {
  /** URI strings (scheme-aware, survives Remote-SSH windows). */
  targets: string[];
  output: string;
}

const DEFAULT_OUTPUT_NAME = "codeprism-context.md";
const DEFAULT_MAX_FILE_BYTES = 1024 * 1024;
// 与 CLI --copy、PythonRunner stdout 缓冲共用的 256 MiB UTF-8 字节预算。
const MAX_CLIPBOARD_BYTES = 256 * 1024 * 1024;

async function pathExists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

/** Scheme/authority-aware identity: the same path on two hosts is two files. */
function uriKey(value: string): string {
  try {
    const uri = vscode.Uri.parse(value);
    const fsPath = process.platform === "win32" ? uri.fsPath.toLowerCase() : uri.fsPath;
    return `${uri.scheme}://${uri.authority}${fsPath}`;
  } catch {
    return value;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("CodePrism");
  const statusBar = new StatusBar();
  const runner = new PythonRunner(context.extensionPath, output);
  const stored = context.workspaceState.get<LastPack>("codeprism.lastPack");
  let lastPack: LastPack | undefined =
    stored &&
    Array.isArray(stored.targets) &&
    stored.targets.every((target) => typeof target === "string") &&
    typeof stored.output === "string"
      ? stored
      : undefined;
  let running = false;

  const updateContextKeys = (): void => {
    const activeUri = vscode.window.activeTextEditor?.document.uri.toString() ?? "";
    const isContextDocument = !!lastPack && uriKey(activeUri) === uriKey(lastPack.output);
    void vscode.commands.executeCommand("setContext", "codeprism.isContextDocument", isContextDocument);
    void vscode.commands.executeCommand("setContext", "codeprism.hasLastPack", !!lastPack);
  };

  const runPack = async (targetUris: vscode.Uri[], copyOnly: boolean): Promise<void> => {
    if (!vscode.workspace.isTrusted) {
      void vscode.window.showWarningMessage("CodePrism: packing is disabled in untrusted workspaces.");
      return;
    }
    if (running) {
      void vscode.window.showWarningMessage("CodePrism: a pack is already running.");
      return;
    }
    running = true;
    statusBar.busy();
    try {
      // Drop targets that vanished since the selection/state was recorded
      // (deleted files, removed workspace folders) instead of throwing.
      // workspace.fs.stat is async and scheme-aware, unlike fs.existsSync.
      const aliveUris: vscode.Uri[] = [];
      for (const uri of targetUris) {
        if (await pathExists(uri)) {
          aliveUris.push(uri);
        }
      }
      if (aliveUris.length === 0) {
        throw new Error("None of the selected paths exist anymore.");
      }
      if (aliveUris.length !== targetUris.length) {
        const dropped = targetUris.length - aliveUris.length;
        output.appendLine(`[warn] ${dropped} of ${targetUris.length} targets no longer exist and were skipped.`);
        void vscode.window.showWarningMessage(
          `CodePrism: ${dropped} selected path(s) no longer exist and were skipped.`,
        );
      }
      targetUris = aliveUris;
      // All path preparation lives inside the try: deleted/renamed targets or
      // malformed settings must surface as friendly errors, not raw throws.
      const config = vscode.workspace.getConfiguration("codeprism", targetUris[0]);
      const targets = targetUris.map((uri) => uri.fsPath);
      // settings.json is hand-editable: validate runtime types so a string
      // where an array belongs cannot be iterated character-by-character
      // into the engine (which would silently ignore everything).
      const toStringArray = (value: unknown, name: string): string[] => {
        if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
          throw new Error(`codeprism.${name} must be an array of strings.`);
        }
        return value as string[];
      };
      const additionalIgnorePatterns = toStringArray(config.get<unknown>("additionalIgnorePatterns", []), "additionalIgnorePatterns");
      const truncatePatterns = toStringArray(config.get<unknown>("truncateContentPatterns", []), "truncateContentPatterns");
      const previewChars = config.get<number>("truncatedPreviewChars", 200);
      const maxFileBytes = config.get<number>("maxFileBytes", DEFAULT_MAX_FILE_BYTES);
      if (!Number.isInteger(previewChars) || previewChars < 0) {
        throw new Error("codeprism.truncatedPreviewChars must be a non-negative integer.");
      }
      if (!Number.isInteger(maxFileBytes) || maxFileBytes < 0) {
        throw new Error("codeprism.maxFileBytes must be a non-negative integer.");
      }
      const ignoreLockFiles = config.get<unknown>("ignoreLockFiles", true);
      if (typeof ignoreLockFiles !== "boolean") {
        throw new Error("codeprism.ignoreLockFiles must be a boolean.");
      }
      const copyAfterGenerate = config.get<unknown>("copyToClipboardAfterGenerate", false);
      if (typeof copyAfterGenerate !== "boolean") {
        throw new Error("codeprism.copyToClipboardAfterGenerate must be a boolean.");
      }
      const fileName = sanitizeFileName(config.get<unknown>("outputFileName", DEFAULT_OUTPUT_NAME));
      // clipboard-only 模式根本不写文件，但仍需该 URI 来排除旧输出文档；
      // 因此只跳过「为写入而设」的根目录守卫，而不是整段路径推导。
      const outputUri = vscode.Uri.joinPath(await chooseOutputDir(targetUris, !copyOnly), fileName);
      const filePath = outputUri.fsPath;
      const outputPath = copyOnly ? undefined : filePath;
      // Exclude the configured output name in every workspace root: a stale
      // custom-named document in any packed root must never be re-packed.
      const excludePaths = new Set<string>([filePath]);
      for (const folder of vscode.workspace.workspaceFolders ?? []) {
        excludePaths.add(vscode.Uri.joinPath(folder.uri, fileName).fsPath);
      }
      let force = false;
      if (!copyOnly && (await pathExists(outputUri))) {
        const existing = await vscode.workspace.fs.stat(outputUri);
        // FileType 是位掩码：指向目录的符号链接为 Directory|SymbolicLink。
        // 严格相等会放过这种目标，随后弹无意义的覆盖框再由引擎 S_ISREG 拒绝。
        if ((existing.type & vscode.FileType.Directory) !== 0) {
          // 名字撞上已存在的目录时，引擎最终会报 "destination is not a
          // regular file"；提前给出可操作的提示，避免无意义的覆盖确认框。
          throw new Error(
            `CodePrism: output name "${fileName}" is an existing directory. ` +
              "Choose a different codeprism.outputFileName.",
          );
        }
        if (!(await isCodeprismDocument(filePath))) {
          const choice = await vscode.window.showWarningMessage(
            `CodePrism: ${fileName} already exists and was not generated by CodePrism. Overwrite it?`,
            { modal: true },
            "Overwrite",
          );
          if (choice !== "Overwrite") {
            statusBar.idle();
            return;
          }
          // The engine refuses to overwrite foreign files without --force;
          // the modal answer must actually reach it.
          force = true;
        }
      }
      const result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "CodePrism: generating Markdown context...",
          cancellable: true,
        },
        (progress, token) =>
          runner.run(
            {
              paths: targets,
              outputPath,
              resourceUri: targetUris[0].toString(),
              force,
              ignoreLockFiles,
              additionalIgnorePatterns,
              // Exact paths avoid gitignore glob escaping, which cannot
              // represent literal backslashes in path segments.
              excludePaths: [...excludePaths],
              truncatePatterns,
              previewChars,
              maxFileBytes,
            },
            token,
            progress,
          ),
      );
      const stats = result.stats;
      let copied = false;
      if (copyOnly) {
        // The VS Code clipboard API works in every host environment, including
        // Remote-SSH/WSL/DevContainers, where engine-side pyperclip cannot.
        if (result.markdown !== null) {
          await vscode.env.clipboard.writeText(result.markdown);
          copied = true;
        } else {
          throw new Error("CodePrism: the engine returned no Markdown in clipboard mode.");
        }
        statusBar.done(stats, undefined);
        void notifyResult(stats, undefined, copied);
      } else {
        lastPack = { targets: targetUris.map((uri) => uri.toString()), output: outputUri.toString() };
        try {
          await context.workspaceState.update("codeprism.lastPack", lastPack);
        } catch (stateErr) {
          // Full or read-only workspace storage must not fail a written pack.
          output.appendLine(`[state] ${stateErr instanceof Error ? stateErr.message : String(stateErr)}`);
        }
        statusBar.done(stats, outputUri);
        // 上下文键只依赖 lastPack 与当前编辑器，与能否打开文档无关。
        // 编辑器打开失败（文档过大等）时也必须刷新 hasLastPack /
        // isContextDocument，故在 try 之外先刷新一次。
        updateContextKeys();
        // The file is already on disk: an editor open failure (e.g. file too
        // large for the editor) must not report the generation as failed.
        try {
          const doc = await vscode.workspace.openTextDocument(outputUri);
          await vscode.window.showTextDocument(doc, { preview: false });
          updateContextKeys();
        } catch (openErr) {
          output.appendLine(`[open] ${openErr instanceof Error ? openErr.message : String(openErr)}`);
          void vscode.window.showWarningMessage("CodePrism: document generated, but it could not be opened in an editor.");
        }
        if (copyAfterGenerate) {
          // A clipboard failure must not invalidate an already written file.
          try {
            const { size } = await fs.promises.stat(filePath);
            if (size > MAX_CLIPBOARD_BYTES) {
              throw new Error(`document exceeds the ${MAX_CLIPBOARD_BYTES / (1024 * 1024)} MiB clipboard budget`);
            }
            const text = await fs.promises.readFile(filePath, "utf8");
            await vscode.env.clipboard.writeText(text);
            copied = true;
          } catch (clipErr) {
            output.appendLine(`[clipboard] ${clipErr instanceof Error ? clipErr.message : String(clipErr)}`);
            void vscode.window.showWarningMessage("CodePrism: document generated, but clipboard copy failed.");
          }
        }
        void notifyResult(stats, outputUri, copied);
      }
    } catch (err) {
      if (err instanceof vscode.CancellationError) {
        statusBar.idle();
        void vscode.window.showInformationMessage("CodePrism: generation cancelled.");
      } else {
        statusBar.failed();
        output.appendLine(`[error] ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`);
        notifyError(err, output);
      }
    } finally {
      // A cancelled engine may still be dying (SIGKILL escalation window);
      // releasing the guard early could race its write against the next
      // pack's os.replace on the same output path. Bounded wait: a grandchild
      // holding the stdio pipe must not lock the guard forever.
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 5000);
        void runner.whenIdle().then(() => {
          clearTimeout(timer);
          resolve();
        });
      });
      running = false;
    }
  };

  context.subscriptions.push(
    output,
    statusBar,
    vscode.commands.registerCommand("codeprism.packWorkspace", () => {
      const roots = workspaceRoots();
      if (roots.length === 0) {
        const message = vscode.workspace.workspaceFolders?.length
          ? "CodePrism: no workspace folder uses a supported scheme (file / vscode-remote)."
          : "CodePrism: open a folder first.";
        void vscode.window.showWarningMessage(message);
        return;
      }
      return runPack(roots, false);
    }),
    vscode.commands.registerCommand("codeprism.refreshContext", async () => {
      if (lastPack && lastPack.targets.length > 0) {
        const parsed: vscode.Uri[] = [];
        let unrestorable = 0;
        for (const target of lastPack.targets) {
          try {
            const uri = vscode.Uri.parse(target);
            if (uri.scheme === "file" || uri.scheme === "vscode-remote") {
              parsed.push(uri);
            } else {
              unrestorable += 1;
            }
          } catch {
            unrestorable += 1;
          }
        }
        if (unrestorable > 0) {
          output.appendLine(
            `[warn] ${unrestorable} of ${lastPack.targets.length} recorded targets are corrupt or use unsupported schemes and were skipped.`,
          );
          void vscode.window.showWarningMessage(
            `CodePrism: ${unrestorable} recorded target(s) could not be restored and were skipped.`,
          );
        }
        // Pass the restored selection: runPack additionally filters targets
        // that no longer exist and reports how many were dropped.
        let anyAlive = false;
        for (const uri of parsed) {
          if (await pathExists(uri)) {
            anyAlive = true;
            break;
          }
        }
        if (anyAlive) {
          return runPack(parsed, false);
        }
        if (parsed.length > 0) {
          void vscode.window.showWarningMessage(
            "CodePrism: the previous selection no longer exists; packing the workspace instead.",
          );
        } else {
          void vscode.window.showWarningMessage(
            "CodePrism: the stored previous selection is unreadable; packing the workspace instead.",
          );
        }
      }
      return vscode.commands.executeCommand("codeprism.packWorkspace");
    }),
    vscode.commands.registerCommand("codeprism.showOutput", () => output.show()),
    vscode.window.onDidChangeActiveTextEditor(updateContextKeys),
    // 增删工作区文件夹会改变活动编辑器/输出归属，重算上下文键避免过期。
    vscode.workspace.onDidChangeWorkspaceFolders(updateContextKeys),
  );
  registerExplorerCommands(context, runPack);
  updateContextKeys();
  output.appendLine("CodePrism activated. Bundled engine requires Python 3.10+ on this machine.");
}

export function deactivate(): void {
  // All resources are disposed via context.subscriptions.
}

function sanitizeFileName(value: unknown): string {
  let base = path.basename(typeof value === "string" ? value.trim() : "")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/[. ]+$/, "");
  // Windows device names stay reserved even with an extension (CON.txt).
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)/i.test(base)) {
    base = "";
  }
  return base && base !== "." && base !== ".." ? base : DEFAULT_OUTPUT_NAME;
}

async function commonRootOf(
  targets: vscode.Uri[],
): Promise<{ common: string; reals: string[] }> {
  // Resolve symlinks/junctions exactly like the engine's resolve_root, so the
  // output directory and the pack root cannot disagree. Use the async realpath
  // once per selection: a synchronous realpathSync can block the extension
  // host on network/remote filesystems, and chooseOutputDir needs the same
  // resolved values again below.
  const reals = await Promise.all(targets.map((target) => fs.promises.realpath(target.fsPath)));
  const dirs: string[] = [];
  for (let i = 0; i < targets.length; i++) {
    const real = reals[i];
    const realUri = vscode.Uri.file(real).with({ scheme: targets[i].scheme, authority: targets[i].authority });
    const stat = await vscode.workspace.fs.stat(realUri);
    dirs.push((stat.type & vscode.FileType.Directory) !== 0 ? real : path.dirname(real));
  }
  let common = dirs[0];
  for (const dir of dirs.slice(1)) {
    while (!isWithin(common, dir)) {
      const parent = path.dirname(common);
      if (parent === common) {
        break;
      }
      common = parent;
    }
  }
  return { common, reals };
}

async function chooseOutputDir(targets: vscode.Uri[], forWriting: boolean): Promise<vscode.Uri> {
  const folders = vscode.workspace.workspaceFolders;
  if (folders && folders.length > 0) {
    const owner = vscode.workspace.getWorkspaceFolder(targets[0]);
    return (owner ?? folders[0]).uri;
  }
  // No workspace: write next to the pack root (same root the engine uses).
  // Uri.file canonicalises the native path (Windows drive prefix) while the
  // original scheme/authority keep remote windows on their remote host.
  const { common, reals } = await commonRootOf(targets);
  if (reals.some((real) => !isWithin(common, real))) {
    throw new Error(
      "The selection spans multiple drives; open a workspace folder or select paths on a single drive.",
    );
  }
  // forWriting=false（纯剪贴板模式）只借用该 URI 做输出自排除，不落盘；
  // 根守卫仅服务于「把文件写到 C:\ 或 /」，此时必须跳过，否则无工作区、
  // 公共根为 / 的零散文件选择会被误拒绝。
  if (forWriting && common === path.parse(common).root) {
    // Writing codeprism-context.md to C:\ or / is almost never intended;
    // allow it only for a selection of files sitting directly in the root.
    for (let i = 0; i < targets.length; i++) {
      const realUri = vscode.Uri.file(reals[i]).with({ scheme: targets[i].scheme, authority: targets[i].authority });
      const stat = await vscode.workspace.fs.stat(realUri);
      if ((stat.type & vscode.FileType.Directory) !== 0 || path.dirname(reals[i]) !== common) {
        throw new Error(
          "The pack root would be the filesystem root; open a workspace folder or select a narrower directory.",
        );
      }
    }
  }
  return vscode.Uri.file(common).with({ scheme: targets[0].scheme, authority: targets[0].authority });
}

async function isCodeprismDocument(filePath: string): Promise<boolean> {
  let handle: fs.promises.FileHandle | undefined;
  try {
    handle = await fs.promises.open(filePath, "r");
    const buffer = Buffer.alloc(64);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    return buffer.toString("utf8", 0, bytesRead).startsWith("# CodePrism Context:");
  } catch {
    return false;
  } finally {
    await handle?.close();
  }
}

function isWithin(dir: string, candidate: string): boolean {
  const rel = path.relative(dir, candidate);
  // "..foo" is a legal name inside dir; only ".." itself or a "../" prefix
  // actually escapes it.
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel));
}
