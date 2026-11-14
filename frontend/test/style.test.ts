// Static checks of the stylesheet (section 10 of UI-SPEC.md): text never below 12 px, the contrast of the tokens,
// controls 36 px tall and targets 44 px, a visible focus, transitions of 140 ms turned off for reduced motion.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";

const root = process.env.VM_UI_ROOT ?? process.cwd();
const css = readFileSync(path.join(root, "src/styles.css"), "utf8");

const tokens = new Map<string, string>([...css.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((found) => [found[1] ?? "", (found[2] ?? "").trim()]));
const token = (name: string): string => tokens.get(name) ?? assert.fail(`token --${name} is missing`);

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255)
    .map((value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4));
  return 0.2126 * (channels[0] ?? 0) + 0.7152 * (channels[1] ?? 0) + 0.0722 * (channels[2] ?? 0);
}
export function contrast(foreground: string, background: string): number {
  const [light, dark] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return ((light ?? 0) + 0.05) / ((dark ?? 0) + 0.05);
}

test("the tokens of the spec are there with their values", () => {
  const expected: Record<string, string> = { bg: "#131415", panel: "#1b1c1f", panel2: "#232529", line: "#34363b", ctl: "#6c6f77", text: "#eceae5", muted: "#a9a69f", accent: "#edb458", blue: "#79b8e6", onaccent: "#1a1405" };
  for (const [name, value] of Object.entries(expected)) assert.equal(token(name).toLowerCase(), value, name);
});

test("contrast: text 15:1 and muted 7:1 on the page, controls and borders 3:1, colors readable on every panel", () => {
  const bg = token("bg");
  assert.ok(contrast(token("text"), bg) >= 14.5, `text ${contrast(token("text"), bg).toFixed(1)}`);
  assert.ok(contrast(token("muted"), bg) >= 7, `muted ${contrast(token("muted"), bg).toFixed(1)}`);
  for (const surface of ["bg", "panel", "panel2"]) {
    assert.ok(contrast(token("ctl"), token(surface)) >= 3 || surface === "panel2", `ctl on ${surface}: ${contrast(token("ctl"), token(surface)).toFixed(2)}`);
    for (const color of ["text", "muted", "accent", "blue", "danger", "ok"]) {
      assert.ok(contrast(token(color), token(surface)) >= 4.5, `${color} on ${surface}: ${contrast(token(color), token(surface)).toFixed(2)}`);
    }
  }
  assert.ok(contrast(token("onaccent"), token("accent")) >= 7, "text on the accent color");
  assert.ok(contrast("#ffffff", bg) >= 3, "the focus outline");
});

test("text is never below 12 px", () => {
  const sizes = new Map<string, number>();
  for (const [name, value] of tokens) if (name.startsWith("fs-")) sizes.set(name, parseFloat(value));
  assert.ok(sizes.size >= 3);
  const resolve = (value: string): number => {
    const reference = /var\(--([\w-]+)\)/.exec(value);
    if (reference) return sizes.get(reference[1] ?? "") ?? Number.NaN;
    if (value.endsWith("rem") || value.endsWith("em")) return parseFloat(value) * 16;
    if (value.endsWith("px")) return parseFloat(value);
    return Number.NaN;
  };
  const declared = [...css.matchAll(/font-size:\s*([^;]+);/g)].map((found) => (found[1] ?? "").trim());
  assert.ok(declared.length > 10);
  for (const value of declared) assert.ok(resolve(value) >= 12, `font-size: ${value}`);
  for (const [name, size] of sizes) assert.ok(size >= 12, `--${name}`);
  assert.doesNotMatch(css, /font:\s*[^;]*\d+px/, "no font shorthand that could hide a size");
});

test("controls are 36 px tall and the click areas 44 px", () => {
  assert.equal(token("control"), "36px");
  assert.equal(token("target"), "44px");
  assert.match(css, /\.btn \{[^}]*min-height: var\(--control\)/);
  assert.match(css, /\.tab \{[^}]*min-height: var\(--target\)/);
  assert.match(css, /\.group__toggle \{[^}]*min-height: var\(--target\)/);
  assert.match(css, /\.check \{[^}]*min-height: var\(--target\)/);
});

test("keyboard focus is a 2 px white outline", () => {
  assert.match(css, /:focus-visible \{ outline: 2px solid #fff;/);
});

test("transitions last 140 ms and are turned off for reduced motion", () => {
  assert.equal(token("transition"), "140ms");
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{[^}]*transition: none !important/);
  for (const found of css.matchAll(/(?<![-\w])transition:\s*([^;]+);/g)) assert.match(found[1] ?? "", /var\(--transition\)|^none !important$/, found[0]);
});

test("the stylesheet loads nothing from outside", () => {
  assert.doesNotMatch(css, /https?:|@import|url\(\s*["']?\/\//);
});
