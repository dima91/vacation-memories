import assert from "node:assert/strict";
import { test } from "node:test";
import type { PlanDocument } from "../src/api/types";
import { removeElement, setPhotoDuration } from "../src/lib/planEdit";
import { canSave, isDirty, saveState, sessionReducer, shown, startSession, type Session, type SessionAction } from "../src/lib/session";
import { clone, planDocument } from "./fixtures";

const run = (session: Session, ...actions: SessionAction[]): Session => actions.reduce(sessionReducer, session);
const photo = planDocument.plan.elements.find((element) => element.type === "photo")!;
const serverAnswer = (document: PlanDocument, revision: string): PlanDocument => ({ ...clone(document), revision });

test("a session starts saved, with the server's plan on screen", () => {
  const session = startSession(planDocument);
  assert.deepEqual([isDirty(session), canSave(session), saveState(session)], [false, false, "saved"]);
  assert.equal(shown(session), planDocument, "nothing changed: the document as the server sent it");
});

test("an edit makes the session unsaved and shows the new plan with the server's limits", () => {
  const edited = setPhotoDuration(planDocument.plan, photo.id, 6);
  const session = run(startSession(planDocument), { type: "edit", plan: edited });
  assert.deepEqual([isDirty(session), canSave(session), saveState(session)], [true, true, "unsaved"]);
  assert.equal(shown(session).plan, edited);
  assert.equal(shown(session).limits, planDocument.limits);
  assert.equal(session.version, 1);
});

test("an answer of the server replaces the plan only if nothing was edited since the request left", () => {
  const first = setPhotoDuration(planDocument.plan, photo.id, 6);
  const second = setPhotoDuration(first, photo.id, 7);
  const answer = serverAnswer(planDocument, "r2");

  let session = run(startSession(planDocument), { type: "edit", plan: first }, { type: "sending" });
  assert.equal(saveState(session), "saving");
  assert.equal(canSave(session), false, "one request at a time");
  session = run(session, { type: "edit", plan: second }, { type: "saved", document: answer, version: 1 });
  assert.equal(session.document.revision, "r2");
  assert.equal(shown(session).plan, second, "the newer edit stays on screen");
  assert.deepEqual([isDirty(session), canSave(session)], [true, true], "and is saved next");

  session = run(session, { type: "sending" }, { type: "saved", document: serverAnswer(planDocument, "r3"), version: 2 });
  assert.equal(isDirty(session), false);
  assert.equal(shown(session).plan, session.document.plan, "the server's normalized version is what is shown");
  assert.equal(session.history.past.length, 2, "and it did not add a step");
});

test("undo and redo are changes to save", () => {
  const edited = setPhotoDuration(planDocument.plan, photo.id, 6);
  let session = run(startSession(planDocument), { type: "edit", plan: edited }, { type: "sending" }, { type: "saved", document: serverAnswer(planDocument, "r2"), version: 1 });
  assert.equal(isDirty(session), false);
  session = run(session, { type: "undo" });
  assert.deepEqual([isDirty(session), shown(session).plan === planDocument.plan], [true, true]);
  session = run(session, { type: "redo" });
  assert.equal(isDirty(session), true);
  assert.equal(shown(run(startSession(planDocument), { type: "undo" })), planDocument, "nothing to undo: no change");
  assert.equal(isDirty(run(startSession(planDocument), { type: "undo" })), false);
});

test("the changes of a gesture are one step of the history, undone together", () => {
  const start = startSession(planDocument);
  let session = start;
  for (const seconds of [4, 5, 6, 7]) session = run(session, { type: "edit", plan: setPhotoDuration(planDocument.plan, photo.id, seconds), group: `d:${photo.id}` });
  session = run(session, { type: "seal" });
  assert.equal(session.history.past.length, 1);
  session = run(session, { type: "undo" });
  assert.equal(shown(session).plan, planDocument.plan);
});

test("a plan changed elsewhere waits for the user: reload, or overwrite without the revision check", () => {
  const edited = removeElement(planDocument.plan, photo.id);
  let session = run(startSession(planDocument), { type: "edit", plan: edited }, { type: "sending" }, { type: "conflict" });
  assert.deepEqual([saveState(session), canSave(session), isDirty(session)], ["conflict", false, true]);
  const forced = run(session, { type: "overwrite" });
  assert.deepEqual([forced.overwrite, canSave(forced), forced.conflict], [true, true, false]);
  const saved = run(forced, { type: "sending" }, { type: "saved", document: serverAnswer(planDocument, "r9"), version: 1 });
  assert.deepEqual([saved.overwrite, isDirty(saved)], [false, false]);
  const fresh = serverAnswer(planDocument, "other");
  session = run(session, { type: "reload", document: fresh });
  assert.deepEqual([session.document, isDirty(session), saveState(session), session.history.past.length], [fresh, false, "saved", 0]);
});

test("another failure keeps the edits and waits for a retry", () => {
  const edited = removeElement(planDocument.plan, photo.id);
  const failure = { code: "network", message: "the editor does not answer", field: null };
  let session = run(startSession(planDocument), { type: "edit", plan: edited }, { type: "sending" }, { type: "failed", failure });
  assert.deepEqual([saveState(session), canSave(session), shown(session).plan], ["failed", false, edited]);
  session = run(session, { type: "retry" });
  assert.deepEqual([saveState(session), canSave(session)], ["unsaved", true]);
});

test("a plan the server refuses goes back to the one it holds, with the reason", () => {
  const edited = removeElement(planDocument.plan, photo.id);
  const failure = { code: "invalid_plan", message: "elements[2].effects[0]: overlaps", field: "elements[2].effects[0]" };
  const session = run(startSession(planDocument), { type: "edit", plan: edited }, { type: "sending" }, { type: "refused", failure });
  assert.equal(shown(session).plan, planDocument.plan);
  assert.deepEqual([isDirty(session), session.failure, canSave(session)], [false, failure, false]);
});
