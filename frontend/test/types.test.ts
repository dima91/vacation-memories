import assert from "node:assert/strict";
import { test } from "node:test";
import { aiAvailable, catalog, folders, planDocument, projectNew, projectPlanned, statusDone, waveform } from "./fixtures";
import * as shapes from "./shapes";

test("the fixtures of the server match the API types", () => {
  // `fixtures.ts` throws at load if one does not; these are the facts the other tests rely on.
  assert.equal(planDocument.plan.schema_version, 2);
  assert.ok(planDocument.plan.elements.length > 5);
  assert.ok(Object.keys(planDocument.refs).length === planDocument.plan.elements.length);
  assert.ok(catalog.photos.length > 0 && catalog.videos.length > 0);
  assert.ok(folders.folders.length > 0 && projectNew.plan.present === false && projectPlanned.plan.present);
  assert.equal(statusDone.job?.exit?.key, "done");
  assert.ok(waveform.peaks.length > 10);
  assert.equal(aiAvailable.available, true);
});

test("a key the type does not have, a missing key and a wrong kind are all reported", () => {
  const element = planDocument.plan.elements[0];
  assert.deepEqual(shapes.problemsOf(shapes.planElement, element, "element"), []);
  assert.match(shapes.problemsOf(shapes.planElement, { ...element, surprise: 1 }, "element").join("\n"), /the type has no such key/);
  const { duration: _removed, ...incomplete } = element ?? ({} as never);
  assert.match(shapes.problemsOf(shapes.planElement, incomplete, "element").join("\n"), /element\.duration: missing/);
  assert.match(shapes.problemsOf(shapes.planElement, { ...element, type: "audio" }, "element").join("\n"), /expected one of photo, video/);
});

test("the plan fixture has what the interface shows: user edits, effects, exclusions and music", () => {
  const { plan } = planDocument;
  assert.ok(plan.elements.some((element) => element.duration_origin === "user"));
  assert.ok(plan.elements.some((element) => element.effects.some((effect) => effect.origin === "user")));
  assert.ok(plan.excluded.length === 1 && (plan.music?.tracks[0]?.parts.length ?? 0) === 2);
  assert.ok(plan.music?.tracks[0]?.parts.some((part) => part.muted));
});
