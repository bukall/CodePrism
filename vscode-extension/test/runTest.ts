import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { runTests } from "@vscode/test-electron";

async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "..", "..");
  const extensionTestsPath = path.resolve(__dirname, "suite");

  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "codeprism-it-"));
  fs.writeFileSync(path.join(fixture, "a.py"), "print('a')\n");
  fs.mkdirSync(path.join(fixture, "sub"));
  fs.writeFileSync(path.join(fixture, "sub", "b.ts"), "export const b = 1;\n");
  fs.writeFileSync(path.join(fixture, ".gitignore"), "*.log\n");
  fs.writeFileSync(path.join(fixture, "noise.log"), "should be ignored\n");
  fs.writeFileSync(path.join(fixture, "LICENSE"), "GNU GENERAL PUBLIC LICENSE\n" + "legal ".repeat(500));
  fs.writeFileSync(
    path.join(fixture, "logo.png"),
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00]),
  );

  // Prefer the locally installed VS Code over downloading a fresh copy.
  const localCode =
    process.platform === "win32" && process.env.LOCALAPPDATA
      ? path.join(process.env.LOCALAPPDATA, "Programs", "Microsoft VS Code", "Code.exe")
      : undefined;
  const vscodeExecutablePath = localCode && fs.existsSync(localCode) ? localCode : undefined;

  // Fresh profile per run: the shared default profile would restore the
  // previous run's (possibly multi-root, now deleted) workspace folders.
  // The retry gets its own profile too: a failed launch can leave locks or
  // half-written state behind in the first one.
  const profiles: string[] = [];
  const makeLaunchArgs = (): string[] => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codeprism-profile-"));
    profiles.push(dir);
    return [fixture, "--disable-extensions", "--disable-workspace-trust", "--user-data-dir", dir];
  };
  try {
    try {
      await runTests({
        extensionDevelopmentPath,
        extensionTestsPath,
        vscodeExecutablePath,
        launchArgs: makeLaunchArgs(),
        timeout: 180000,
      });
    } catch (err) {
      // The local VS Code may be mid-update or otherwise unlaunchable; the
      // thrown TestRunFailedError carries no cause text, so retry once with a
      // test-electron download whenever a local executable was used. A real
      // suite failure simply fails again on the downloaded copy.
      if (vscodeExecutablePath) {
        console.log(`[runTest] run with local VS Code failed (${String(err)}); retrying with a downloaded copy`);
        await runTests({
          extensionDevelopmentPath,
          extensionTestsPath,
          launchArgs: makeLaunchArgs(),
          timeout: 180000,
        });
      } else {
        throw err;
      }
    }
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
    for (const dir of profiles) {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
