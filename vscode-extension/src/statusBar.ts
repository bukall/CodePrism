import * as vscode from "vscode";
import type { PackStats } from "./pythonRunner";

export class StatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.idle();
    this.item.show();
  }

  idle(): void {
    this.item.text = "$(file-code) CodePrism";
    this.item.tooltip = "Pack entire workspace to Markdown";
    this.item.command = "codeprism.packWorkspace";
  }

  busy(): void {
    this.item.text = "$(sync~spin) CodePrism: packing...";
    this.item.tooltip = "Generating Markdown context";
    this.item.command = undefined;
  }

  done(stats: PackStats, outputUri?: vscode.Uri): void {
    this.item.text = `$(check) CodePrism: ${stats.totalFiles} files`;
    const limits = limitSuffix(stats);
    const warnings = stats.warnings?.length ?? 0;
    this.item.tooltip =
      `Last pack: ${stats.textFiles} text, ${stats.binaryFiles} non-text, ${stats.totalCharacters} chars` +
      (limits ? `\n${limits}` : "") +
      (warnings ? `\n${warnings} engine warning(s) - see the CodePrism output channel` : "") +
      (outputUri ? `\nClick to open ${outputUri.fsPath}` : "");
    this.item.command = outputUri
      ? { command: "vscode.open", title: "Open context file", arguments: [outputUri] }
      : "codeprism.showOutput";
  }

  failed(): void {
    this.item.text = "$(error) CodePrism: failed";
    this.item.tooltip = "Last pack failed - click to open the log";
    this.item.command = "codeprism.showOutput";
  }

  dispose(): void {
    this.item.dispose();
  }
}

function limitSuffix(stats: PackStats): string {
  const parts: string[] = [];
  if (stats.truncatedFiles > 0) {
    parts.push(`${stats.truncatedFiles} truncated`);
  }
  if (stats.sizeLimitedFiles > 0) {
    parts.push(`${stats.sizeLimitedFiles} size-limited`);
  }
  return parts.join(", ");
}

export async function notifyResult(
  stats: PackStats,
  outputUri: vscode.Uri | undefined,
  copied: boolean,
): Promise<void> {
  // The document is already open (file mode) or on the clipboard (copy mode);
  // the status bar item offers opening it again, so no duplicate button here.
  const limits = limitSuffix(stats);
  const summary =
    `CodePrism: packed ${stats.totalFiles} files (${stats.textFiles} text, ${stats.binaryFiles} non-text)` +
    (limits ? `; ${limits} (preview only)` : "") +
    ".";
  const suffix = copied ? (outputUri ? " Also copied to clipboard." : " Copied to clipboard.") : "";
  const warnings = stats.warnings?.length ?? 0;
  if (warnings > 0) {
    void vscode.window.showWarningMessage(
      `CodePrism: ${warnings} engine warning(s) (skipped rules or files, decoding loss). See the CodePrism output channel.`,
    );
  }
  await vscode.window.showInformationMessage(summary + suffix);
}

export function notifyError(err: unknown, output: vscode.OutputChannel): void {
  const message = err instanceof Error ? err.message : String(err);
  void vscode.window.showErrorMessage(`CodePrism: ${message}`, "Show Log").then((choice) => {
    if (choice === "Show Log") {
      output.show();
    }
  });
}
