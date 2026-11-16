import assert from "node:assert/strict";
import { test } from "node:test";
import { projectFromHash } from "../src/App";
import { ApiFailure, failureFrom, urls } from "../src/api/client";
import { chooseView } from "../src/screens/ProjectScreen";
import { aiChoiceBody } from "../src/screens/SettingsScreen";
import { clone, projectNew, projectPlanned } from "./fixtures";

test("the project is in the address", () => {
  assert.equal(projectFromHash("#/p/vacation-1b24e0c1"), "vacation-1b24e0c1");
  assert.equal(projectFromHash(""), null);
  assert.equal(projectFromHash("#/p/../etc"), null);
  assert.equal(projectFromHash("#/other"), null);
});

test("the flow: settings without a plan, progress while the analysis runs, the editor otherwise", () => {
  assert.equal(chooseView(projectNew), "settings");
  assert.equal(chooseView(projectPlanned), "editor");
  const running = clone(projectNew);
  running.job = { id: "j", type: "plan", name: null, state: "running", started_at: "now", finished_at: null, exit: null, plan_revision: null };
  assert.equal(chooseView(running), "progress");
  const stopping = clone(running);
  stopping.job!.state = "stopping";
  assert.equal(chooseView(stopping), "progress");
  const preview = clone(projectPlanned);
  preview.job = { ...running.job!, type: "preview" };
  assert.equal(chooseView(preview), "editor", "a preview or an export does not take the editor away");
  const finished = clone(running);
  finished.job!.state = "failed";
  assert.equal(chooseView(finished), "settings");
});

test("an error of the API keeps its code, field and details", () => {
  const failure = failureFrom(409, { error: { code: "ai_unavailable", message: "no Ollama", reason: "model_missing", model: "m" } });
  assert.ok(failure instanceof ApiFailure);
  assert.deepEqual([failure.status, failure.code, failure.message, failure.field], [409, "ai_unavailable", "no Ollama", null]);
  assert.deepEqual(failure.details, { reason: "model_missing", model: "m" });
  assert.equal(failureFrom(400, { error: { code: "invalid_settings", message: "bad", field: "duration" } }).field, "duration");
  assert.equal(failureFrom(502, "<html>bad gateway</html>").code, "unknown");
});

test("media are named by opaque ids in the URLs, never by file names", () => {
  assert.equal(urls.thumb("p-1", "0123456789abcdef"), "/api/projects/p-1/thumb/0123456789abcdef");
  assert.equal(urls.thumb("p-1", "0123456789abcdef", 3.5), "/api/projects/p-1/thumb/0123456789abcdef?t=3.5");
  assert.equal(urls.clip("p-1", "fedcba9876543210"), "/api/projects/p-1/clip/fedcba9876543210");
  assert.equal(urls.video("p-1", "memories_preview"), "/api/projects/p-1/video/memories_preview");
});

test("the notice about the AI says what is missing", () => {
  assert.equal(aiChoiceBody("model_missing"), "ai.choice.model_missing");
  assert.equal(aiChoiceBody("ollama_unreachable"), "ai.choice.unreachable");
  assert.equal(aiChoiceBody(""), "ai.choice.body");
});
