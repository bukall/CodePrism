import * as vscode from "vscode";

// Remote windows (SSH/WSL/DevContainers) expose vscode-remote URIs; the
// extension host runs on the remote side, so those paths are local there.
const SUPPORTED_SCHEMES = new Set(["file", "vscode-remote"]);

/** Normalise the (uri, uris) pair VS Code passes to explorer/context commands. */
export function collectSelection(uri?: vscode.Uri, uris?: vscode.Uri[]): vscode.Uri[] {
  const raw = uris && uris.length > 0 ? uris : uri ? [uri] : [];
  const seen = new Set<string>();
  const result: vscode.Uri[] = [];
  for (const candidate of raw) {
    if (!candidate || !SUPPORTED_SCHEMES.has(candidate.scheme)) {
      continue;
    }
    const fsPath = process.platform === "win32" ? candidate.fsPath.toLowerCase() : candidate.fsPath;
    // Same path on different hosts/schemes is a different file: keep both.
    const key = `${candidate.scheme}://${candidate.authority}${fsPath}`;
    if (!seen.has(key)) {
      seen.add(key);
      result.push(candidate);
    }
  }
  return result;
}

export function workspaceRoots(): vscode.Uri[] {
  return (vscode.workspace.workspaceFolders ?? [])
    .filter((folder) => SUPPORTED_SCHEMES.has(folder.uri.scheme))
    .map((folder) => folder.uri);
}

export type PackHandler = (targets: vscode.Uri[], copyOnly: boolean) => Promise<void>;

export function registerExplorerCommands(context: vscode.ExtensionContext, pack: PackHandler): void {
  const handle = (copyOnly: boolean) => (uri?: vscode.Uri, uris?: vscode.Uri[]): Promise<void> | void => {
    const selected = collectSelection(uri, uris);
    const rawCount = uris && uris.length > 0 ? uris.length : uri ? 1 : 0;
    if (rawCount > 0 && selected.length === 0) {
      // An explicit selection was entirely filtered out: do not silently pack
      // the whole workspace instead of what the user asked for.
      void vscode.window.showWarningMessage(
        "CodePrism: the selection uses unsupported URI schemes (only file and vscode-remote are supported).",
      );
      return;
    }
    const targets = selected.length > 0 ? selected : workspaceRoots();
    if (targets.length === 0) {
      void vscode.window.showWarningMessage(
        "CodePrism: open a folder or select files/folders in the Explorer first.",
      );
      return;
    }
    return pack(targets, copyOnly);
  };
  context.subscriptions.push(
    vscode.commands.registerCommand("codeprism.packSelected", handle(false)),
    vscode.commands.registerCommand("codeprism.copySelected", handle(true)),
  );
}
