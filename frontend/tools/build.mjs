// Builds the interface into `dist/` (or the folder given as the first argument): one script, one stylesheet, the
// bundled fonts and an `index.html` that refers to them with relative-to-root paths. Nothing is loaded from outside.
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// The fontsource stylesheets list a woff next to every woff2; every browser that can run the interface reads woff2.
const woff2Only = {
  name: "woff2-only",
  setup(build) {
    build.onLoad({ filter: /@fontsource\/.*\.css$/ }, async (args) => {
      const css = (await readFile(args.path, "utf8")).replace(/,\s*url\([^)]*\.woff\)\s*format\(["']woff["']\)/g, "");
      return { contents: css, loader: "css", resolveDir: path.dirname(args.path) };
    });
  },
};

export async function buildInterface(outdir = path.join(root, "dist")) {
  await rm(outdir, { recursive: true, force: true });
  await mkdir(outdir, { recursive: true });
  const result = await build({
    entryPoints: [path.join(root, "src/main.tsx")],
    outdir: path.join(outdir, "assets"),
    entryNames: "app-[hash]",
    assetNames: "[name]-[hash]",
    bundle: true,
    minify: true,
    sourcemap: false,
    target: "es2022",
    format: "esm",
    platform: "browser",
    jsx: "automatic",
    legalComments: "none",
    loader: { ".woff2": "file" },
    plugins: [woff2Only],
    define: { "process.env.NODE_ENV": '"production"' },
    metafile: true,
    logLevel: "warning",
  });
  const outputs = Object.keys(result.metafile.outputs).map((file) => path.basename(file));
  const script = outputs.find((name) => name.endsWith(".js"));
  const style = outputs.find((name) => name.endsWith(".css"));
  if (!script || !style) throw new Error("the build produced no script or no stylesheet");
  const template = await readFile(path.join(root, "index.html"), "utf8");
  const page = template.replace("{{style}}", `/assets/${style}`).replace("{{script}}", `/assets/${script}`);
  await writeFile(path.join(outdir, "index.html"), page);
  return { outdir, script, style };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const built = await buildInterface(process.argv[2] ? path.resolve(process.argv[2]) : undefined);
  console.log(`built ${built.outdir}: ${built.script}, ${built.style}`);
}
