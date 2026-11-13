// Copy core/codeprism into bundled/codeprism so the extension ships the
// current engine. Run from vscode-extension/: npm run sync:core
// Staging + backup swap: a failed copy never leaves a half-written engine.
import { cpSync, existsSync, renameSync, rmSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
const src = join(repoRoot, "core", "codeprism");
const dest = join(repoRoot, "vscode-extension", "bundled", "codeprism");

if (!existsSync(src)) {
  console.error(`Core package not found: ${src}`);
  process.exit(1);
}

// Unique suffix: a killed run's leftovers can never collide with a later
// process that happens to reuse the same PID.
const suffix = `${process.pid}-${randomUUID().slice(0, 8)}`;
const staging = join(dirname(dest), `.codeprism-sync-${suffix}`);
const backup = join(dirname(dest), `.codeprism-old-${suffix}`);
try {
  cpSync(src, staging, {
    recursive: true,
    // Segment-wise: a repo path containing "__pycache__" must not filter everything.
    filter: (p) => !p.split(sep).includes("__pycache__"),
  });
  try {
    if (existsSync(dest)) {
      renameSync(dest, backup);
    }
    renameSync(staging, dest);
  } catch (err) {
    if (existsSync(backup) && !existsSync(dest)) {
      renameSync(backup, dest);
    }
    throw err;
  }
  // The swap already succeeded: a locked backup (AV / IDE) must not turn a
  // good sync into a failed build.
  try {
    rmSync(backup, { recursive: true, force: true });
  } catch (err) {
    console.warn(`NOTE: could not remove backup ${backup}: ${err.message}`);
  }
} finally {
  try {
    rmSync(staging, { recursive: true, force: true });
  } catch (err) {
    console.warn(`NOTE: could not remove staging ${staging}: ${err.message}`);
  }
}
console.log(`Synced ${src} -> ${dest}`);

const vendorMarker = join(repoRoot, "vscode-extension", "bundled", "vendor", "pathspec");
if (!existsSync(vendorMarker)) {
  console.log("NOTE: bundled/vendor is missing. Run: npm run vendor");
}
