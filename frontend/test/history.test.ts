import assert from "node:assert/strict";
import { test } from "node:test";
import { HISTORY_LIMIT, amend, canRedo, canUndo, commit, redo, seal, startHistory, undo } from "../src/lib/history";

test("a change is a step: undo goes back and redo comes forward", () => {
  let history = startHistory(0);
  assert.equal(canUndo(history), false);
  history = commit(commit(history, 1), 2);
  assert.equal(history.present, 2);
  history = undo(history);
  assert.deepEqual([history.present, canUndo(history), canRedo(history)], [1, true, true]);
  history = redo(history);
  assert.deepEqual([history.present, canRedo(history)], [2, false]);
  assert.equal(undo(startHistory(0)).present, 0, "nothing to undo");
  assert.equal(redo(startHistory(0)).present, 0, "nothing to redo");
});

test("a new change after an undo drops what could be redone", () => {
  let history = commit(commit(startHistory("a"), "b"), "c");
  history = commit(undo(history), "d");
  assert.deepEqual([history.present, canRedo(history), history.past], ["d", false, ["a", "b"]]);
});

test("the history keeps 60 steps", () => {
  let history = startHistory(0);
  for (let value = 1; value <= HISTORY_LIMIT + 25; value += 1) history = commit(history, value);
  assert.equal(history.past.length, HISTORY_LIMIT);
  assert.equal(history.past[0], 25, "the oldest steps are forgotten");
  let back = history;
  while (canUndo(back)) back = undo(back);
  assert.equal(back.present, 25);
  let forward = back;
  while (canRedo(forward)) forward = redo(forward);
  assert.equal(forward.present, HISTORY_LIMIT + 25);
});

test("the same value is not a step", () => {
  const plan = { a: 1 };
  const history = commit(startHistory(plan), plan);
  assert.equal(canUndo(history), false);
});

test("the changes of one gesture are one step", () => {
  let history = startHistory(3);
  for (const value of [3.1, 3.2, 3.3, 3.4]) history = commit(history, value, "duration:a");
  assert.equal(history.past.length, 1);
  history = seal(history);
  history = commit(history, 5, "duration:a");
  assert.equal(history.past.length, 2, "after the gesture is sealed the next one is a new step");
  assert.deepEqual(undo(history).present, 3.4);
});

test("a different gesture, or an edit without one, closes the gesture in progress", () => {
  let history = commit(startHistory(0), 1, "a");
  history = commit(history, 2, "b");
  assert.equal(history.past.length, 2);
  history = commit(history, 3);
  assert.equal(history.past.length, 3);
});

test("a drag brought back to where it started leaves no step", () => {
  let history = startHistory({ duration: 3 });
  history = commit(history, { duration: 4 }, "d");
  history = commit(history, { duration: 3 }, "d");
  history = seal(history);
  assert.equal(canUndo(history), false);
  assert.deepEqual(history.present, { duration: 3 });
});

test("undo while a gesture is open closes it first", () => {
  let history = startHistory(0);
  history = commit(history, 1, "g");
  history = commit(history, 2, "g");
  history = undo(history);
  assert.equal(history.present, 0);
  assert.equal(history.group, null);
  assert.equal(redo(history).present, 2);
});

test("amend replaces the present without a step", () => {
  const history = amend(commit(startHistory(1), 2), 20);
  assert.deepEqual([history.present, history.past], [20, [1]]);
});
