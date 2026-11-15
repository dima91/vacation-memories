import assert from "node:assert/strict";
import { test } from "node:test";
import type { JobInfo } from "../src/api/types";
import {
  autoPreviewDue, canRender, exportNameProblem, exportNamed, latestJob, outcomeOf, partialOf, renderUsesOlderPlan,
} from "../src/lib/render";
import { clone, projectNew, projectPlanned, projectRendered } from "./fixtures";

const job = (changes: Partial<JobInfo> = {}): JobInfo => ({
  id: "j", type: "export", name: "memories", state: "running", started_at: "2026-11-15T10:00:00+00:00", finished_at: null, exit: null,
  plan_revision: "aaaa", ...changes,
});
const failed = (key: string, code: number): JobInfo => job({ state: "failed", exit: { code, key, message: key, detail: null } });

test("a name for the export is a plain file name, as the server wants it", () => {
  for (const name of ["memories", "summer trip", "Trip-2026.final", "a", "x".repeat(64)]) assert.equal(exportNameProblem(name), null, name);
  assert.equal(exportNameProblem(""), "empty");
  assert.equal(exportNameProblem("   "), "empty");
  for (const name of ["../x", "a/b", ".hidden", "-x", "x".repeat(65), "tab\tname", "line\nbreak"]) assert.equal(exportNameProblem(name), "invalid", name);
  for (const name of ["x_preview", "x.partial", "a..b"]) assert.equal(exportNameProblem(name), "reserved", name);
});

test("a render starts only with a valid plan, no job running and no other process on the project", () => {
  assert.equal(canRender(projectPlanned), true);
  assert.equal(canRender(projectNew), false, "no plan yet");
  const invalid = clone(projectPlanned);
  invalid.plan.valid = false;
  assert.equal(canRender(invalid), false);
  const busy = clone(projectPlanned);
  busy.busy = true;
  assert.equal(canRender(busy), false);
  const running = clone(projectPlanned);
  running.job = job({ type: "preview" });
  assert.equal(canRender(running), false);
  running.job = job({ type: "preview", state: "stopping" });
  assert.equal(canRender(running), false);
  running.job = job({ type: "preview", state: "done" });
  assert.equal(canRender(running), true, "a finished job does not hold the next one back");
});

test("the preview starts by itself after a new plan, unless the setting is off or something else is going on", () => {
  assert.equal(autoPreviewDue(projectPlanned), true);
  const off = clone(projectPlanned);
  off.settings.auto_preview = false;
  assert.equal(autoPreviewDue(off), false);
  const busy = clone(projectPlanned);
  busy.busy = true;
  assert.equal(autoPreviewDue(busy), false);
  assert.equal(autoPreviewDue(projectNew), false);
});

test("what a finished job is for the user: done, stopped, failed final check, failed", () => {
  assert.equal(outcomeOf(job()), "running");
  assert.equal(outcomeOf(job({ state: "stopping" })), "running");
  assert.equal(outcomeOf(job({ state: "done" })), "done");
  assert.equal(outcomeOf(job({ state: "interrupted" })), "interrupted");
  assert.equal(outcomeOf(failed("check_failed", 3)), "check_failed");
  assert.equal(outcomeOf(failed("missing_requirements", 4)), "failed");
  assert.equal(outcomeOf(failed("invalid_input", 2)), "failed");
  assert.equal(outcomeOf(job({ state: "failed" })), "failed", "a failure with no exit information");
});

test("the files of the export: the complete one by name, and the partial one a failed check left", () => {
  const exports = [
    { name: "memories", path: "projects/p/memories.mp4", size: 10, modified: "x", partial: false },
    { name: "memories.partial", path: "projects/p/memories.partial.mp4", size: 5, modified: "x", partial: true },
  ];
  assert.equal(exportNamed(exports, "memories")?.size, 10);
  assert.equal(exportNamed(exports, "memories.partial"), undefined, "a partial file is not an export");
  assert.equal(partialOf(exports, "memories")?.path, "projects/p/memories.partial.mp4");
  assert.equal(partialOf(exports, "other"), undefined);
  assert.equal(partialOf(exports, null), undefined);
  assert.equal(projectRendered.exports[0]?.path.startsWith("projects/"), true, "paths are relative to the work folder");
  assert.equal(projectRendered.exports[0]?.path.startsWith("/"), false);
});

test("a save during a render is flagged: the render keeps the version it read when it started", () => {
  assert.equal(renderUsesOlderPlan(job(), "aaaa"), false);
  assert.equal(renderUsesOlderPlan(job(), "bbbb"), true);
  assert.equal(renderUsesOlderPlan(job({ state: "done" }), "bbbb"), false, "a finished job is not running on anything");
  assert.equal(renderUsesOlderPlan(job({ type: "plan", plan_revision: null }), "bbbb"), false);
  assert.equal(renderUsesOlderPlan(job({ plan_revision: null }), "bbbb"), false);
  assert.equal(renderUsesOlderPlan(job(), null), false);
  assert.equal(renderUsesOlderPlan(null, "bbbb"), false);
});

test("of the polled job and the one read with the project, the newer one counts", () => {
  const older = job({ id: "old", started_at: "2026-11-15T09:00:00+00:00", state: "done" });
  const newer = job({ id: "new", started_at: "2026-11-15T10:00:00+00:00" });
  assert.equal(latestJob(newer, older)?.id, "new");
  assert.equal(latestJob(older, newer)?.id, "new");
  assert.equal(latestJob(null, older)?.id, "old");
  assert.equal(latestJob(newer, null)?.id, "new");
  assert.equal(latestJob(undefined, undefined), null);
  assert.equal(latestJob(newer, { ...newer, state: "running" })?.state, "running", "the polled one wins when both say the same");
});

test("a poll that arrives after the job ended does not bring it back to life", () => {
  const stale = job({ state: "stopping" });
  const ended = job({ state: "interrupted" });
  assert.equal(latestJob(stale, ended)?.state, "interrupted");
  assert.equal(latestJob(ended, stale)?.state, "interrupted");
});
