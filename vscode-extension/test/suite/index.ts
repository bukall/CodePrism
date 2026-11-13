import * as assert from "assert";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

export async function run(): Promise<void> {
  console.log("[suite] start");
  const originalError = vscode.window.showErrorMessage;
  vscode.window.showErrorMessage = ((message: string, ...rest: unknown[]) => {
    console.log(`[suite-error] ${message}`);
    return (originalError as (...args: unknown[]) => Thenable<unknown>).apply(vscode.window, [message, ...rest]);
  }) as typeof vscode.window.showErrorMessage;
  // 桩安装在顶层 try 之前：若初始打包断言就失败，finally 尚未接管，
  // 退出前也必须还原，避免同进程内（或复跑）残留被替换的 API。
  process.once("exit", () => {
    vscode.window.showErrorMessage = originalError;
  });
  const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
  assert.ok(workspaceFolder, "test workspace must be open");
  const ws = workspaceFolder.uri.fsPath;
  const out = path.join(ws, "codeprism-context.md");

  await vscode.commands.executeCommand("codeprism.packWorkspace");
  assert.ok(fs.existsSync(out), "packWorkspace must create the output file");
  let md = fs.readFileSync(out, "utf8");
  assert.ok(md.includes("# CodePrism Context:"), "summary header present");
  assert.ok(md.includes("print('a')"), "text content included verbatim");
  assert.ok(md.includes("export const b = 1;"), "nested text content included");
  assert.ok(!md.includes("noise.log"), "gitignored file must not appear");
  assert.ok(md.includes("[Non-Text: .png,"), "binary asset annotated in tree");
  assert.ok(md.includes("| `logo.png` |"), "binary asset listed in inventory");
  assert.ok(md.includes("LICENSE [Truncated: first 200 of"), "LICENSE preview-truncated in tree");
  assert.ok(md.includes("> [CodePrism] Content truncated:"), "truncation notice rendered");

  const fileUri = vscode.Uri.file(path.join(ws, "a.py"));
  const dirUri = vscode.Uri.file(path.join(ws, "sub"));
  await vscode.commands.executeCommand("codeprism.packSelected", fileUri, [fileUri, dirUri]);
  md = fs.readFileSync(out, "utf8");
  assert.ok(md.includes("### `a.py`"), "selected file packed");
  assert.ok(md.includes("### `sub/b.ts`"), "selected directory packed recursively");
  assert.ok(!md.includes("### `logo.png`"), "unselected file must not be packed");

  // Selected paths named like CLI flags must reach the engine as paths
  // (options first, then the "--" separator), never as options.
  const flagNames = ["--stdout", "--force", "--"];
  const flagFiles = flagNames.map((name) => path.join(ws, name));
  flagFiles.forEach((file, i) => fs.writeFileSync(file, `FLAG-NAME-CONTENT-${i}\n`));
  try {
    await vscode.commands.executeCommand(
      "codeprism.packSelected",
      vscode.Uri.file(flagFiles[0]),
      flagFiles.map((file) => vscode.Uri.file(file)),
    );
    md = fs.readFileSync(out, "utf8");
    flagNames.forEach((name, i) => {
      assert.ok(md.includes(`FLAG-NAME-CONTENT-${i}`), `flag-named file ${name} content packed`);
      assert.ok(md.includes(`### \`${name}\``), `flag-named file ${name} rendered as a section`);
    });
  } finally {
    for (const file of flagFiles) {
      fs.rmSync(file, { force: true });
    }
  }

  await vscode.commands.executeCommand("codeprism.refreshContext");
  assert.ok(fs.existsSync(out), "refresh keeps the output file");

  const mtimeBefore = fs.statSync(out).mtimeMs;
  // Environment probe: VS Code 1.146+ routes the clipboard through the
  // focused renderer; test-electron windows cannot read it back (the probe
  // logs ""). Capture at the vscode.env.clipboard API boundary where the
  // build allows it; otherwise verify the copy path via its notification.
  await vscode.commands.executeCommand("workbench.action.focusWindow");
  await vscode.env.clipboard.writeText("SUITE-CLIP-PROBE");
  console.log(`[suite] clipboard probe: ${JSON.stringify(await vscode.env.clipboard.readText())}`);
  const realClipboard = vscode.env.clipboard;
  const captured: string[] = [];
  let clipboardStubbed = false;
  const fakeClipboard = {
    writeText: async (text: string): Promise<void> => {
      captured.push(text);
      try {
        await realClipboard.writeText(text);
      } catch {
        // System clipboard may be unavailable in headless test windows.
      }
    },
    readText: async (): Promise<string> =>
      captured.length > 0 ? captured[captured.length - 1] : realClipboard.readText(),
  };
  try {
    Object.defineProperty(vscode.env, "clipboard", { value: fakeClipboard, configurable: true });
    clipboardStubbed = true;
  } catch {
    console.log("[suite] vscode.env.clipboard is sealed; copy assertions use the result notification");
  }
  const infoMessages: string[] = [];
  const originalInfo = vscode.window.showInformationMessage;
  vscode.window.showInformationMessage = ((message: string, ...rest: unknown[]) => {
    infoMessages.push(message);
    return (originalInfo as (...args: unknown[]) => Thenable<unknown>).apply(vscode.window, [message, ...rest]);
  }) as typeof vscode.window.showInformationMessage;
  // 任何断言抛出时也必须还原被替换的 API，避免同进程内后续用例看到脏
  // 状态；stub 安装都在 try 之前完成，finally 才能访问到原值。
  try {
  const readClipboard = async (): Promise<string> => fakeClipboard.readText();
  // 每次 copy 命令前同时清空捕获数组与通知记录：sealed 分支的
  // assertCopied 依赖 infoMessages，若不清空，后两次复制即使完全没有
  // writeText 也会命中第一次的成功通知，形成假通过。
  const resetCaptured = (): void => {
    captured.length = 0;
    infoMessages.length = 0;
  };
  // 即便 stub 成功，也要求本次命令确实调用了 writeText；否则 readClipboard
  // 会回落到真实剪贴板里的旧内容，仍可能形成假通过。
  const assertCaptured = (label: string): void => {
    assert.ok(
      captured.length > 0,
      `${label}: clipboard writeText must have been called for this command`,
    );
  };
  const assertCopied = (label: string): void => {
    if (clipboardStubbed) {
      return; // content assertions below are meaningful
    }
    // sealed 构建中 vscode.env.clipboard 不可替换：只能验证通知文本，
    // 无法断言实际写入的 Markdown 内容。明确登记该覆盖边界（见 DEVELOPMENT.md），
    // 不再把「维持测试含义」的结论无限制套用到 sealed 分支。
    console.log(`[suite] ${label}: clipboard API sealed; clipboard content is NOT verifiable in this build`);
    assert.ok(
      infoMessages.some((m) => m.includes("Copied to clipboard.")),
      `${label}: copy path must report clipboard success`,
    );
  };
  resetCaptured();
  await vscode.commands.executeCommand("codeprism.copySelected", fileUri);
  assert.strictEqual(fs.statSync(out).mtimeMs, mtimeBefore, "copy-only must not rewrite the workspace output");
  const clipboard = await readClipboard();
  if (clipboardStubbed) {
    assertCaptured("copy-only");
    assert.ok(
      clipboard.includes("# CodePrism Context:"),
      `copy-only must place Markdown on the VS Code clipboard; got: ${JSON.stringify(clipboard.slice(0, 160))}`,
    );
    assert.ok(clipboard.includes("print('a')"), "clipboard carries the packed content");
  } else {
    assertCopied("copy-only");
  }

  const config = vscode.workspace.getConfiguration("codeprism");
  await config.update("additionalIgnorePatterns", ["*.ts"], vscode.ConfigurationTarget.Workspace);
  try {
    await vscode.commands.executeCommand("codeprism.packWorkspace");
    md = fs.readFileSync(out, "utf8");
    assert.ok(!md.includes("export const b = 1;"), "additionalIgnorePatterns must exclude matches");
  } finally {
    await config.update("additionalIgnorePatterns", [], vscode.ConfigurationTarget.Workspace);
  }

  // A custom output name containing glob metacharacters must still self-exclude
  // in clipboard mode via the literal --exclude-path mechanism.
  fs.writeFileSync(path.join(ws, "ctx[1].md"), "MARKER-OLD-CTX\n");
  await config.update("outputFileName", "ctx[1].md", vscode.ConfigurationTarget.Workspace);
  try {
    resetCaptured();
    await vscode.commands.executeCommand("codeprism.copySelected");
    const clip = await readClipboard();
    if (clipboardStubbed) {
      assertCaptured("palette copy");
      assert.ok(clip.includes("# CodePrism Context:"), "palette copy falls back to workspace roots");
      assert.ok(!clip.includes("MARKER-OLD-CTX"), "glob-named previous output must not be re-packed");
    } else {
      assertCopied("palette copy");
    }
  } finally {
    await config.update("outputFileName", undefined, vscode.ConfigurationTarget.Workspace);
    fs.rmSync(path.join(ws, "ctx[1].md"), { force: true });
  }

  // Multi-root: the output lands in the first folder while the pack root is
  // the common parent; literal --exclude-path must still exclude the old
  // custom-named document regardless of that relative distance.
  const waitForFolderCount = (count: number): Promise<void> =>
    new Promise((resolve, reject) => {
      // Poll the authoritative property (events can lag or be swallowed by a
      // workspace conversion) and let the event accelerate resolution.
      const check = (): boolean => {
        if ((vscode.workspace.workspaceFolders ?? []).length === count) {
          cleanup();
          resolve();
          return true;
        }
        return false;
      };
      const sub = vscode.workspace.onDidChangeWorkspaceFolders(() => check());
      const poll = setInterval(check, 250);
      const timer = setTimeout(() => {
        cleanup();
        reject(
          new Error(
            `workspace folder count did not reach ${count} within 30s (current: ${(vscode.workspace.workspaceFolders ?? []).length})`,
          ),
        );
      }, 30000);
      const cleanup = (): void => {
        sub.dispose();
        clearInterval(poll);
        clearTimeout(timer);
      };
      check();
    });
  const sibling = fs.mkdtempSync(path.join(os.tmpdir(), "codeprism-sibling-"));
  // Best-effort cleanup even when an assertion aborts the suite mid-run.
  process.once("exit", () => {
    try {
      fs.rmSync(sibling, { recursive: true, force: true });
    } catch {
      // already gone
    }
  });
  fs.writeFileSync(path.join(sibling, "sibling.py"), "print('sibling')\n");
  fs.writeFileSync(path.join(sibling, "ctx[1].md"), "MARKER-SIBLING-OLD\n");
  fs.writeFileSync(path.join(ws, "ctx[1].md"), "MARKER-MULTIROOT-OLD\n");
  await vscode.workspace.updateWorkspaceFolders(1, 0, { uri: vscode.Uri.file(sibling) });
  console.log("[suite] multi-root add requested");
  await waitForFolderCount(2);
  console.log("[suite] multi-root active");
  await config.update("outputFileName", "ctx[1].md", vscode.ConfigurationTarget.Workspace);
  try {
    resetCaptured();
    await vscode.commands.executeCommand("codeprism.copySelected");
    const clip = await readClipboard();
    if (clipboardStubbed) {
      assertCaptured("multi-root copy");
      assert.ok(clip.includes("print('sibling')"), "multi-root copy packs all roots");
      assert.ok(!clip.includes("MARKER-MULTIROOT-OLD"), "multi-root old output must self-exclude");
      assert.ok(!clip.includes("MARKER-SIBLING-OLD"), "old output in the second root must self-exclude too");
    } else {
      assertCopied("multi-root copy");
    }
  } finally {
    await config.update("outputFileName", undefined, vscode.ConfigurationTarget.Workspace);
    // Do NOT remove the sibling folder here: newer VS Code builds return
    // false for updateWorkspaceFolders removals while the single-folder →
    // untitled-multi-root conversion is pending. Later assertions tolerate
    // the extra root; the per-run profile isolates state, and the sibling is
    // deleted at the very end of the suite.
    fs.rmSync(path.join(ws, "ctx[1].md"), { force: true });
  }

  // Refresh must survive deleted selection targets via a friendly fallback.
  const doomed = path.join(ws, "doomed.py");
  fs.writeFileSync(doomed, "print('doomed')\n");
  await vscode.commands.executeCommand("codeprism.packSelected", vscode.Uri.file(doomed));
  fs.rmSync(doomed);
  // Delete the output too: only a real fallback re-pack can bring it back,
  // so the existence assertion below actually proves the branch ran.
  fs.rmSync(out, { force: true });
  console.log("[suite] refresh with deleted target");
  await vscode.commands.executeCommand("codeprism.refreshContext");
  console.log("[suite] refresh done");
  assert.ok(fs.existsSync(out), "refresh fallback still produces a document");

  // Size-limited files render a leading preview instead of the full content.
  fs.writeFileSync(path.join(ws, "huge.csv"), "z".repeat(5000));
  await config.update("maxFileBytes", 1000, vscode.ConfigurationTarget.Workspace);
  await config.update("truncatedPreviewChars", 10, vscode.ConfigurationTarget.Workspace);
  try {
    await vscode.commands.executeCommand("codeprism.packWorkspace");
    console.log("[suite] size-limit pack done");
    md = fs.readFileSync(out, "utf8");
    assert.ok(md.includes("[Size-limited: first 10 chars]"), "size limit preview applied");
    assert.ok(md.includes("> [CodePrism] Size limit"), "size limit notice rendered");
  } finally {
    await config.update("maxFileBytes", undefined, vscode.ConfigurationTarget.Workspace);
    await config.update("truncatedPreviewChars", undefined, vscode.ConfigurationTarget.Workspace);
    fs.rmSync(path.join(ws, "huge.csv"), { force: true });
  }

  // Overwrite confirmation must reach the engine as --force: cancelling keeps
  // the foreign file byte-for-byte, confirming replaces it.
  const foreign = path.join(ws, "foreign.md");
  fs.writeFileSync(foreign, "FOREIGN-CONTENT\n");
  await config.update("outputFileName", "foreign.md", vscode.ConfigurationTarget.Workspace);
  const originalWarning = vscode.window.showWarningMessage;
  let overwriteAsked = 0;
  const warningStub = (async (message: string, ...rest: unknown[]) => {
    if (typeof message === "string" && message.includes("Overwrite it?")) {
      overwriteAsked += 1;
      return overwriteAsked === 1 ? undefined : "Overwrite";
    }
    return (originalWarning as (...args: unknown[]) => Thenable<unknown>).apply(vscode.window, [message, ...rest]);
  }) as typeof vscode.window.showWarningMessage;
  vscode.window.showWarningMessage = warningStub;
  try {
    await vscode.commands.executeCommand("codeprism.packWorkspace");
    assert.strictEqual(fs.readFileSync(foreign, "utf8"), "FOREIGN-CONTENT\n", "cancel must keep the foreign file");
    await vscode.commands.executeCommand("codeprism.packWorkspace");
    assert.ok(
      fs.readFileSync(foreign, "utf8").startsWith("# CodePrism Context:"),
      "confirmed overwrite must replace the foreign file",
    );
    assert.strictEqual(overwriteAsked, 2, "modal must be shown for a foreign output file");
    console.log("[suite] overwrite confirmation verified");
  } finally {
    vscode.window.showWarningMessage = originalWarning;
    await config.update("outputFileName", undefined, vscode.ConfigurationTarget.Workspace);
    fs.rmSync(foreign, { force: true });
  }

  // Packing must be refused in untrusted workspaces (runtime guard on top of
  // the untrustedWorkspaces capability). Stub isTrusted where the API allows
  // it; older builds expose it read-only on the prototype.
  const trustedDescriptor = Object.getOwnPropertyDescriptor(vscode.workspace, "isTrusted");
  let trustStubbed = false;
  try {
    Object.defineProperty(vscode.workspace, "isTrusted", { configurable: true, get: () => false });
    trustStubbed = true;
    const before = fs.existsSync(out) ? fs.readFileSync(out, "utf8") : null;
    await vscode.commands.executeCommand("codeprism.packWorkspace");
    const after = fs.existsSync(out) ? fs.readFileSync(out, "utf8") : null;
    assert.strictEqual(after, before, "untrusted workspace must not pack");
    console.log("[suite] untrusted-workspace guard verified");
  } catch (err) {
    if (trustStubbed) {
      throw err;
    }
    console.log(`[suite] isTrusted not stubbable in this VS Code build; capability declaration covers it (${String(err)})`);
  } finally {
    if (trustStubbed) {
      if (trustedDescriptor) {
        Object.defineProperty(vscode.workspace, "isTrusted", trustedDescriptor);
      } else {
        delete (vscode.workspace as { isTrusted?: boolean }).isTrusted;
      }
    }
  }

  // 成功路径：移除第二个工作区根（异常路径由 process.once("exit") 兜底）。
  fs.rmSync(sibling, { recursive: true, force: true });
  // 只有完整跑通才打印完成标记：断言失败时不应出现 "[suite] done"，
  // 文档以 start/done 同时出现作为「真实跑通」的凭据。
  console.log("[suite] done");
  } finally {
    // 成功或断言失败都要还原；测试进程内不再残留被替换的 API。
    if (clipboardStubbed) {
      Object.defineProperty(vscode.env, "clipboard", { value: realClipboard, configurable: true });
    }
    vscode.window.showInformationMessage = originalInfo;
    vscode.window.showErrorMessage = originalError;
  }
}
