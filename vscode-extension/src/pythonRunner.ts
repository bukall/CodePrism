import * as vscode from "vscode";
import { spawn, type ChildProcess } from "child_process";
import * as fs from "fs";
import * as path from "path";

export interface PackStats {
  projectName: string;
  sourcePaths: string[];
  totalFiles: number;
  textFiles: number;
  binaryFiles: number;
  totalCharacters: number;
  totalLines: number;
  truncatedFiles: number;
  sizeLimitedFiles: number;
  outputPath: string | null;
  copied: boolean;
  maxFileBytes?: number;
  sourcesAllDirectories?: boolean;
  warnings?: string[];
}

export interface PackOptions {
  paths: string[];
  /** Undefined = stdout mode: Markdown is captured, nothing is written. */
  outputPath?: string;
  /** Original URI of the primary target; keeps remote scheme for config scoping. */
  resourceUri?: string;
  /** User confirmed overwriting a foreign file (modal) → engine --force. */
  force: boolean;
  ignoreLockFiles: boolean;
  additionalIgnorePatterns: string[];
  /** Exact paths excluded literally (no glob syntax), e.g. the output file. */
  excludePaths: string[];
  truncatePatterns: string[];
  previewChars: number;
  maxFileBytes: number;
}

export interface RunResult {
  stats: PackStats;
  /** Full Markdown in stdout mode; null when the engine wrote a file. */
  markdown: string | null;
}

interface PythonCommand {
  command: string;
  prefixArgs: string[];
}

const MIN_MAJOR = 3;
const MIN_MINOR = 10;
const PROBE_TIMEOUT_MS = 8000;
// Clipboard mode buffers the whole document; bound it so a hostile or broken
// interpreter cannot grow the extension host heap without limit, and so a
// V8 "Invalid string length" throw inside a stream listener cannot leave the
// run Promise unsettled (which would lock the running guard forever).
// 统一按 UTF-8 字节计（Buffer.byteLength），与 CLI --copy 和扩展的文件复制
// 共用同一预算；JS string.length 是 UTF-16 code unit，和 UTF-8 字节数不同。
const MAX_STDOUT_BYTES = 256 * 1024 * 1024;
const MAX_STDERR_BYTES = 16 * 1024 * 1024;
const MAX_PROBE_CHARS = 64 * 1024;

function killProcessTree(child: ChildProcess): void {
  if (child.pid === undefined) {
    return;
  }
  if (process.platform === "win32") {
    // Absolute System32 path: a PATH-hijacked taskkill.exe must never run.
    // /T kills the whole tree: wrappers such as the py launcher spawn the
    // real interpreter as a child, which child.kill() would orphan.
    const taskkill = path.join(process.env.SystemRoot ?? "C:\\Windows", "System32", "taskkill.exe");
    try {
      const killer = spawn(taskkill, ["/F", "/T", "/PID", String(child.pid)], { windowsHide: true });
      killer.on("error", () => child.kill());
      killer.on("close", (code) => {
        if (code !== 0) {
          child.kill();
        }
      });
    } catch {
      child.kill();
    }
  } else {
    // Detached spawn made the child a process-group leader; signal the whole
    // group so wrapper-launched interpreters die too.
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      child.kill();
    }
  }
}

function escalateKill(child: ChildProcess): ReturnType<typeof setTimeout> {
  return setTimeout(() => {
    try {
      if (process.platform !== "win32" && child.pid !== undefined) {
        process.kill(-child.pid, "SIGKILL");
      } else {
        child.kill("SIGKILL");
      }
    } catch {
      // process already gone
    }
  }, 2000);
}

export class PythonRunner {
  private childExit: Promise<void> = Promise.resolve();

  constructor(
    private readonly extensionPath: string,
    private readonly output: vscode.OutputChannel,
  ) {}

  /** Resolves once the last spawned engine process has fully exited. */
  whenIdle(): Promise<void> {
    return this.childExit;
  }

  async run(
    options: PackOptions,
    token: vscode.CancellationToken,
    progress?: vscode.Progress<{ message?: string }>,
  ): Promise<RunResult> {
    if (token.isCancellationRequested) {
      throw new vscode.CancellationError();
    }
    const resource = options.resourceUri
      ? vscode.Uri.parse(options.resourceUri)
      : vscode.Uri.file(options.paths[0]);
    const python = await this.resolvePython(token, resource);
    if (token.isCancellationRequested) {
      throw new vscode.CancellationError();
    }
    const script = path.join(this.extensionPath, "bundled", "run_codeprism.py");
    if (!fs.existsSync(script)) {
      throw new Error(`Bundled CodePrism engine is missing: ${script}`);
    }
    // Options first, then "--": a selected path named like a flag (e.g.
    // "--stdout") must never be parsed as an option by click.
    const args = [script];
    if (options.outputPath === undefined) {
      args.push("--stdout", "--json-stats");
    } else {
      args.push("--output", options.outputPath, "--json-stats");
    }
    if (options.force) {
      args.push("--force");
    }
    if (!options.ignoreLockFiles) {
      args.push("--include-lock-files");
    }
    for (const pattern of options.additionalIgnorePatterns) {
      args.push("--ignore-pattern", pattern);
    }
    for (const exclude of options.excludePaths) {
      args.push("--exclude-path", exclude);
    }
    for (const pattern of options.truncatePatterns) {
      args.push("--truncate-pattern", pattern);
    }
    args.push("--preview-chars", String(options.previewChars));
    args.push("--max-file-bytes", String(options.maxFileBytes));
    args.push("--", ...options.paths);
    this.output.appendLine(`[run] ${python.command} ${[...python.prefixArgs, ...args].map((a) => JSON.stringify(a)).join(" ")}`);
    progress?.report({ message: "Running Python engine..." });
    return this.spawnAndParse(python, args, options, token);
  }

  private spawnAndParse(
    python: PythonCommand,
    args: string[],
    options: PackOptions,
    token: vscode.CancellationToken,
  ): Promise<RunResult> {
    return new Promise<RunResult>((resolve, reject) => {
      if (token.isCancellationRequested) {
        reject(new vscode.CancellationError());
        return;
      }
      let releaseExit: () => void = () => {};
      this.childExit = new Promise<void>((res) => {
        releaseExit = res;
      });
      let child: ChildProcess;
      try {
        // Arguments are passed as an array (no shell), so paths with spaces,
        // quotes or non-ASCII characters need no manual escaping.
        child = spawn(python.command, [...python.prefixArgs, ...args], {
          cwd: options.outputPath === undefined ? undefined : path.dirname(options.outputPath),
          windowsHide: true,
          detached: process.platform !== "win32",
        });
      } catch (err) {
        releaseExit();
        reject(new Error(`Failed to start Python: ${err instanceof Error ? err.message : String(err)}`));
        return;
      }
      let stdout = "";
      let stderr = "";
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let settled = false;
      let killTimer: ReturnType<typeof setTimeout> | undefined;
      // Declared before finish(): no TDZ even if a listener ever fires sync.
      let cancelSub: vscode.Disposable | undefined;
      const finish = (fn: () => void) => {
        if (!settled) {
          settled = true;
          cancelSub?.dispose();
          fn();
        }
      };
      const abort = (err: unknown) => {
        killProcessTree(child);
        if (!killTimer) {
          killTimer = escalateKill(child);
        }
        finish(() => reject(err instanceof Error ? err : new Error(String(err))));
      };
      const cancelRun = () => {
        // Settle immediately so the UI never waits on a stubborn child.
        // Windows: taskkill /F /T force-kills the whole tree at once.
        // POSIX: SIGTERM to the process group, then a SIGKILL escalation
        // for wrappers that trap it.
        killProcessTree(child);
        if (!killTimer) {
          killTimer = escalateKill(child);
        }
        finish(() => reject(new vscode.CancellationError()));
      };
      cancelSub = token.onCancellationRequested(cancelRun);
      // A token already cancelled before registration must not leave the
      // child running (mock tokens may not fire the listener retroactively).
      if (token.isCancellationRequested) {
        cancelRun();
      }
      // Default stdio pipes always exist; the null union only covers custom
      // stdio configurations this runner never uses.
      const stdoutStream = child.stdout!;
      const stderrStream = child.stderr!;
      stdoutStream.setEncoding("utf8");
      stderrStream.setEncoding("utf8");
      stdoutStream.on("data", (chunk: string) => {
        if (settled) {
          return;
        }
        try {
          const chunkBytes = Buffer.byteLength(chunk, "utf8");
          if (stdoutBytes + chunkBytes > MAX_STDOUT_BYTES) {
            throw new Error(
              "CodePrism: output too large for clipboard mode; pack to a file instead or exclude large files.",
            );
          }
          stdout += chunk;
          stdoutBytes += chunkBytes;
        } catch (err) {
          // Destroy the stream so buffered chunks cannot re-trigger abort
          // (and another taskkill) after the run already settled.
          stdoutStream.destroy();
          abort(err);
        }
      });
      stderrStream.on("data", (chunk: string) => {
        if (settled) {
          return;
        }
        try {
          const chunkBytes = Buffer.byteLength(chunk, "utf8");
          if (stderrBytes + chunkBytes > MAX_STDERR_BYTES) {
            throw new Error("CodePrism: engine stderr exceeded the buffer limit.");
          }
          stderr += chunk;
          stderrBytes += chunkBytes;
        } catch (err) {
          stderrStream.destroy();
          abort(err);
        }
      });
      // A pending SIGKILL escalation is deliberately NOT cleared on
      // close/error: on POSIX the process group can outlive the direct
      // child (a wrapper's grandchild may trap SIGTERM and keep the group
      // alive). Accepted residual risk: if the group is already gone and its
      // PID is reused as a new group leader inside the 2s window, the late
      // process.kill(-pid) can hit an unrelated group. This is the standard
      // tree-kill PID-reuse race; see docs/DEVELOPMENT.md "Known limitations".
      child.on("error", (err) => {
        releaseExit();
        finish(() => reject(new Error(`Failed to run Python (${python.command}): ${err.message}`)));
      });
      child.on("close", (code) => {
        releaseExit();
        finish(() => {
          if (token.isCancellationRequested) {
            reject(new vscode.CancellationError());
            return;
          }
          if (stderr.trim()) {
            this.output.appendLine(`[stderr] ${stderr.trim()}`);
          }
          if (code !== 0) {
            // Windows CLI 输出可能是 CRLF；按 \n 切分会留下尾随 \r，
            // 让错误通知出现断行错乱。
            reject(new Error(stderr.trim().split(/\r?\n/).slice(-4).join("\n") || `Python engine exited with code ${code}`));
            return;
          }
          const stdoutMode = options.outputPath === undefined;
          // In stdout mode the Markdown owns stdout; the JSON stats line
          // arrives on stderr. Scan the last few lines so interpreter
          // shutdown warnings after the JSON cannot break parsing.
          const statsText = stdoutMode ? stderr : stdout;
          const lines = statsText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
          let stats: PackStats | undefined;
          for (let i = lines.length - 1; i >= Math.max(0, lines.length - 5); i--) {
            try {
              const parsed = JSON.parse(lines[i]) as PackStats;
              if (
                parsed &&
                typeof parsed === "object" &&
                typeof parsed.totalFiles === "number" &&
                typeof parsed.textFiles === "number" &&
                typeof parsed.binaryFiles === "number"
              ) {
                stats = parsed;
                break;
              }
            } catch {
              // not the stats line; keep scanning upwards
            }
          }
          if (!stats) {
            reject(new Error(`Cannot parse engine stats from ${stdoutMode ? "stderr" : "stdout"}: ${lines[lines.length - 1] ?? "(empty)"}`));
            return;
          }
          resolve({ stats, markdown: stdoutMode ? stdout : null });
        });
      });
    });
  }

  private async resolvePython(
    token: vscode.CancellationToken,
    resource?: vscode.Uri,
  ): Promise<PythonCommand> {
    const throwIfCancelled = () => {
      if (token.isCancellationRequested) {
        throw new vscode.CancellationError();
      }
    };
    throwIfCancelled();
    const configuredRaw = vscode.workspace.getConfiguration("codeprism", resource).get<unknown>("pythonPath", "");
    if (typeof configuredRaw !== "string") {
      throw new Error("codeprism.pythonPath must be a string: a plain executable path, without arguments.");
    }
    const configured = configuredRaw.trim();
    if (configured) {
      if (await this.probe({ command: configured, prefixArgs: [] }, token)) {
        return { command: configured, prefixArgs: [] };
      }
      throwIfCancelled();
      throw new Error(`codeprism.pythonPath "${configured}" is not a working Python ${MIN_MAJOR}.${MIN_MINOR}+ interpreter.`);
    }
    const fromExtension = await this.fromPythonExtension(resource);
    if (fromExtension) {
      if (await this.probe(fromExtension, token)) {
        return fromExtension;
      }
      throwIfCancelled();
    }
    const candidates: PythonCommand[] =
      process.platform === "win32"
        ? [
            { command: "python", prefixArgs: [] },
            { command: "python3", prefixArgs: [] },
            { command: "py", prefixArgs: ["-3"] },
          ]
        : [
            { command: "python3", prefixArgs: [] },
            { command: "python", prefixArgs: [] },
          ];
    for (const candidate of candidates) {
      throwIfCancelled();
      if (await this.probe(candidate, token)) {
        return candidate;
      }
    }
    throwIfCancelled();
    throw new Error(
      `Python ${MIN_MAJOR}.${MIN_MINOR}+ was not found. Install it from https://www.python.org/downloads/ or set "codeprism.pythonPath" in Settings.`,
    );
  }

  private async fromPythonExtension(resource?: vscode.Uri): Promise<PythonCommand | undefined> {
    const ext = vscode.extensions.getExtension("ms-python.python");
    if (!ext) {
      return undefined;
    }
    if (!ext.isActive) {
      try {
        // A blocking Python extension activation must not stall cancellation
        // indefinitely; race it against a timeout. The activation itself is
        // NOT cancellable: after the race we only stop waiting, while the
        // original promise keeps running in the background. Keep a reference
        // and attach a rejection handler so a late failure is logged instead
        // of surfacing as an unhandled rejection.
        const activation = Promise.resolve(ext.activate());
        void activation.catch((err) => {
          // 拒绝可能发生在 10s 超时之前，也可能发生在超时之后的后台，
          // 日志不能固定声称「已超时」。
          this.output.appendLine(
            `[python] ms-python activation failed (background activation is not cancellable): ${err instanceof Error ? err.message : String(err)}`,
          );
        });
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            activation,
            new Promise((resolve) => {
              timer = setTimeout(resolve, 10000);
            }),
          ]);
        } finally {
          if (timer) {
            clearTimeout(timer);
          }
        }
      } catch {
        return undefined;
      }
    }
    if (!ext.isActive) {
      return undefined;
    }
    try {
      // The ms-python API surface is untyped here; probe known shapes defensively.
      const api = ext.exports as {
        environments?: { getActiveEnvironmentPath?: (resource?: vscode.WorkspaceFolder) => { path?: string } | undefined };
        settings?: { getExecutionDetails?: (resource?: vscode.WorkspaceFolder) => { execCommand?: string[] } | undefined };
      };
      // Honour the packed target's folder, not blindly the first root.
      const folder =
        (resource ? vscode.workspace.getWorkspaceFolder(resource) : undefined) ??
        vscode.workspace.workspaceFolders?.[0];
      const envPath = api.environments?.getActiveEnvironmentPath?.(folder)?.path;
      if (envPath) {
        return { command: envPath, prefixArgs: [] };
      }
      const execCommand = api.settings?.getExecutionDetails?.(folder)?.execCommand;
      if (Array.isArray(execCommand) && typeof execCommand[0] === "string" && execCommand[0]) {
        // Launchers like ["py", "-3.12"] or ["conda", "run", ...] carry their
        // arguments in the tail; dropping them probes the wrong interpreter.
        return {
          command: execCommand[0],
          prefixArgs: execCommand.slice(1).filter((arg): arg is string => typeof arg === "string"),
        };
      }
    } catch (err) {
      this.output.appendLine(`[python] ms-python API unavailable: ${err instanceof Error ? err.message : String(err)}`);
    }
    return undefined;
  }

  private probe(candidate: PythonCommand, token: vscode.CancellationToken): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      if (token.isCancellationRequested) {
        resolve(false);
        return;
      }
      let child: ChildProcess;
      try {
        child = spawn(candidate.command, [...candidate.prefixArgs, "--version"], {
          windowsHide: true,
          detached: process.platform !== "win32",
        });
      } catch (err) {
        this.output.appendLine(
          `[python] probe spawn failed for ${candidate.command}: ${err instanceof Error ? err.message : String(err)}`,
        );
        resolve(false);
        return;
      }
      let text = "";
      let done = false;
      let timer: ReturnType<typeof setTimeout> | undefined;
      let escalation: ReturnType<typeof setTimeout> | undefined;
      let cancelSub: vscode.Disposable | undefined;
      // The escalation is never cleared early: on POSIX the process group
      // can outlive the direct child. Same accepted PID-reuse race as the
      // run path above (documented in docs/DEVELOPMENT.md).
      const finish = (ok: boolean) => {
        if (!done) {
          done = true;
          if (timer) {
            clearTimeout(timer);
          }
          cancelSub?.dispose();
          resolve(ok);
        }
      };
      const stop = () => {
        killProcessTree(child);
        if (!escalation) {
          escalation = escalateKill(child);
        }
        finish(false);
      };
      timer = setTimeout(stop, PROBE_TIMEOUT_MS);
      cancelSub = token.onCancellationRequested(stop);
      if (token.isCancellationRequested) {
        stop();
      }
      const stdoutStream = child.stdout!;
      const stderrStream = child.stderr!;
      stdoutStream.setEncoding("utf8");
      stderrStream.setEncoding("utf8");
      stdoutStream.on("data", (chunk: string) => {
        if (text.length < MAX_PROBE_CHARS) {
          text += chunk;
        }
      });
      stderrStream.on("data", (chunk: string) => {
        if (text.length < MAX_PROBE_CHARS) {
          text += chunk;
        }
      });
      child.on("error", (err) => {
        this.output.appendLine(`[python] probe error for ${candidate.command}: ${err.message}`);
        finish(false);
      });
      child.on("close", (code) => {
        const match = /Python (\d+)\.(\d+)/.exec(text);
        const ok =
          code === 0 &&
          match !== null &&
          (Number(match[1]) > MIN_MAJOR || (Number(match[1]) === MIN_MAJOR && Number(match[2]) >= MIN_MINOR));
        if (ok) {
          this.output.appendLine(`[python] using ${candidate.command} ${candidate.prefixArgs.join(" ")} (${match![0]})`);
        } else {
          this.output.appendLine(
            `[python] probe rejected ${candidate.command}: exit=${code}, output=${JSON.stringify(text.trim().slice(0, 200))}`,
          );
        }
        finish(ok);
      });
    });
  }
}
