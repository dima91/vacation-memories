// `npm test`: compiles the test files (and what they import) with esbuild and runs them with `node --test`.
// The modules under test are plain TypeScript without a DOM (D11); the components are rendered with
// `react-dom/server`. Nothing is written inside the repository: the compiled tests live in a temporary folder.
import { spawnSync } from "node:child_process";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const testDir = path.join(root, "test");
const only = process.argv.slice(2);

const files = (await readdir(testDir)).filter((name) => /\.test\.tsx?$/.test(name))
  .filter((name) => only.length === 0 || only.some((pattern) => name.includes(pattern)));
const scratch = await mkdtemp(path.join(os.tmpdir(), "vm-ui-test-"));
try {
  await build({
    entryPoints: files.map((name) => path.join(testDir, name)),
    outdir: scratch,
    outExtension: { ".js": ".mjs" },
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node24",
    jsx: "automatic",
    sourcemap: "inline",
    external: ["esbuild", "node:*"],
    banner: { js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);" },
    loader: { ".woff2": "empty", ".woff": "empty", ".css": "empty" },
    logLevel: "warning",
  });
  const compiled = files.map((name) => path.join(scratch, name.replace(/\.tsx?$/, ".mjs")));
  const run = spawnSync(process.execPath, ["--test", "--enable-source-maps", ...compiled], {
    stdio: "inherit", cwd: root, env: { ...process.env, VM_UI_ROOT: root },
  });
  process.exitCode = run.status ?? 1;
} finally {
  await rm(scratch, { recursive: true, force: true });
}
