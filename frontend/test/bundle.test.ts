// The built bundle makes no external request (section 3 and 15 of UI-SPEC.md): fonts, script and style are in the
// image. The same rule is checked on the page served through nginx by `scripts/test-docker.sh`.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, test } from "node:test";
import allowed from "../tools/allowed-urls.json";

const root = process.env.VM_UI_ROOT ?? process.cwd();
const URL_LIKE = /(?:https?:)?\/\/[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}[^\s"'`)<>\\]*/g;

export function externalReferences(text: string): string[] {
  return [...text.matchAll(URL_LIKE)].map((found) => found[0]).filter((url) => !allowed.prefixes.some((prefix) => url.startsWith(prefix)));
}

let out = "";
const files = (folder: string): string[] =>
  readdirSync(folder).flatMap((name) => (statSync(path.join(folder, name)).isDirectory() ? files(path.join(folder, name)) : [path.join(folder, name)]));

before(() => {
  out = mkdtempSync(path.join(os.tmpdir(), "vm-ui-dist-"));
  const run = spawnSync(process.execPath, [path.join(root, "tools/build.mjs"), out], { cwd: root, encoding: "utf8" });
  assert.equal(run.status, 0, `the build failed:\n${run.stdout}\n${run.stderr}`);
});
after(() => rmSync(out, { recursive: true, force: true }));

test("the checker finds external references and lets the namespaces through", () => {
  assert.deepEqual(externalReferences('<script src="https://cdn.example.com/x.js">'), ["https://cdn.example.com/x.js"]);
  assert.deepEqual(externalReferences("@import url(//fonts.googleapis.com/css)"), ["//fonts.googleapis.com/css"]);
  assert.deepEqual(externalReferences('xmlns="http://www.w3.org/2000/svg"'), []);
  assert.deepEqual(externalReferences("a/b.c//d"), []);
});

test("the page loads one script and one stylesheet from its own origin", () => {
  const page = readFileSync(path.join(out, "index.html"), "utf8");
  assert.deepEqual(externalReferences(page), []);
  assert.match(page, /<link rel="stylesheet" href="\/assets\/app-[\w-]+\.css">/);
  assert.match(page, /<script type="module" src="\/assets\/app-[\w-]+\.js">/);
  assert.match(page, /<html lang="it">/);
  assert.doesNotMatch(page, /\{\{/, "no placeholder is left");
});

test("no file of the bundle refers to anything outside it", () => {
  const found = files(out).filter((file) => /\.(html|css|js|json|svg)$/.test(file))
    .flatMap((file) => externalReferences(readFileSync(file, "utf8")).map((url) => `${path.relative(out, file)}: ${url}`));
  assert.deepEqual(found, []);
});

test("the stylesheet pulls in only the bundled woff2 fonts, by relative paths", () => {
  const css = files(out).filter((file) => file.endsWith(".css")).map((file) => readFileSync(file, "utf8")).join("\n");
  assert.doesNotMatch(css, /@import/);
  const urls = [...css.matchAll(/url\(["']?([^"')]+)["']?\)/g)].map((found) => found[1] ?? "");
  assert.ok(urls.length >= 7, "Bricolage Grotesque, IBM Plex Sans and IBM Plex Mono are bundled");
  for (const url of urls) assert.match(url, /^\.\/[\w.-]+\.woff2$/, url);
  for (const family of ["Bricolage Grotesque", "IBM Plex Sans", "IBM Plex Mono"]) assert.ok(css.includes(family), family);
});

test("the script never builds an absolute URL for a request", () => {
  const script = files(out).filter((file) => file.endsWith(".js")).map((file) => readFileSync(file, "utf8")).join("\n");
  assert.ok(script.includes("/api"), "it talks to the editor's API");
  assert.doesNotMatch(script, /(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\([^)]*["'`]https?:/);
});
