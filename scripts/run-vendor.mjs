// Launch prepare-vendor.py with the first available Python interpreter.
// Many macOS/Linux systems ship only python3; Windows usually has python
// (or the py launcher). npm scripts cannot express that fallback portably.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(here, "prepare-vendor.py");
const candidates =
  process.platform === "win32"
    ? [["python"], ["python3"], ["py", "-3"]]
    : [["python3"], ["python"]];
const MIN_MAJOR = 3;
const MIN_MINOR = 10;
const PROBE_TIMEOUT_MS = 8000;

for (const [command, ...prefix] of candidates) {
  // A zero exit code alone is not enough: a Python 2 interpreter would pass
  // and then die on f-strings, and Windows Store aliases can hang. Require a
  // parsed 3.10+ version and bound the probe.
  const probe = spawnSync(command, [...prefix, "--version"], {
    encoding: "utf8",
    timeout: PROBE_TIMEOUT_MS,
  });
  const text = `${probe.stdout ?? ""}${probe.stderr ?? ""}`;
  const match = /Python (\d+)\.(\d+)/.exec(text);
  const usable =
    !probe.error &&
    probe.status === 0 &&
    match !== null &&
    (Number(match[1]) > MIN_MAJOR ||
      (Number(match[1]) === MIN_MAJOR && Number(match[2]) >= MIN_MINOR));
  if (!usable) {
    continue;
  }
  const run = spawnSync(command, [...prefix, script, ...process.argv.slice(2)], { stdio: "inherit" });
  if (run.error) {
    console.error(`Failed to launch ${command}: ${run.error.message}`);
    process.exit(1);
  }
  process.exit(run.status ?? 1);
}

console.error(
  "No Python interpreter found (tried " +
    candidates.map((candidate) => candidate.join(" ")).join(", ") +
    "). Install Python 3.10+ to vendor the runtime dependencies.",
);
process.exit(1);
