// The plans of `scenarios.ts` against the shared file `tests/fixtures/editor/edited_plans.json`. To regenerate it after
// a deliberate change of the edits:
//   docker compose run --rm -T -e VM_UPDATE_FIXTURES=1 ui-dev npm test -- scenarios
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { scenarios } from "./scenarios";

const root = process.env.VM_UI_ROOT ?? process.cwd();
const file = path.join(root, "..", "tests", "fixtures", "editor", "edited_plans.json");
const current = () => Object.entries(scenarios).map(([name, make]) => ({ name, plan: make() }));

test("the plans the edits produce are the ones in the shared fixture, which the server's rules check", () => {
  const produced = JSON.parse(JSON.stringify(current())) as unknown;
  if (process.env.VM_UPDATE_FIXTURES) {
    // One scenario per line: a diff of the file shows which edit changed.
    writeFileSync(file, `[\n${(produced as unknown[]).map((entry) => JSON.stringify(entry)).join(",\n")}\n]\n`);
    return;
  }
  assert.deepEqual(produced, JSON.parse(readFileSync(file, "utf8")), "the edits changed: regenerate tests/fixtures/editor/edited_plans.json (see the top of this file)");
});
