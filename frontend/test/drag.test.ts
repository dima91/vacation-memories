import assert from "node:assert/strict";
import { test } from "node:test";
import { beginDrag, currentDrag, decodePayload, encodePayload, endDrag } from "../src/lib/dnd";
import { freeStretches, insertionSlot, isNoopSlot, momentAround, slotX, snap, valueFromDrag, valueFromKey } from "../src/lib/drag";
import { commandFor, isTextField } from "../src/lib/shortcuts";

test("a value snaps to a tenth of a second and stays free of floating-point noise", () => {
  assert.equal(snap(2.34), 2.3);
  assert.equal(snap(2.36), 2.4);
  assert.equal(snap(0.1 + 0.2), 0.3);
  assert.equal(snap(7.25, 0.5), 7.5);
});

test("a drag moves the value by the distance, inside the server's bounds", () => {
  const bounds = { min: 1, max: 30 };
  assert.equal(valueFromDrag({ start: 3, deltaPx: 50, pxPerUnit: 10, bounds }), 8);
  assert.equal(valueFromDrag({ start: 3, deltaPx: 3, pxPerUnit: 10, bounds }), 3.3);
  assert.equal(valueFromDrag({ start: 3, deltaPx: -500, pxPerUnit: 10, bounds }), 1, "never below the minimum");
  assert.equal(valueFromDrag({ start: 3, deltaPx: 5000, pxPerUnit: 10, bounds }), 30, "never above the maximum");
  assert.equal(valueFromDrag({ start: 3, deltaPx: -20, pxPerUnit: 10, sign: -1, bounds }), 5, "the left handle grows the value moving left");
  assert.equal(valueFromDrag({ start: 3, deltaPx: 20, pxPerUnit: 0, bounds }), 3, "no scale, no movement");
});

test("the arrows move by a tenth, Shift by a second, inside the bounds", () => {
  const bounds = { min: 1, max: 30 };
  assert.equal(valueFromKey("ArrowRight", false, 3, bounds), 3.1);
  assert.equal(valueFromKey("ArrowLeft", false, 3, bounds), 2.9);
  assert.equal(valueFromKey("ArrowUp", true, 3, bounds), 4);
  assert.equal(valueFromKey("ArrowDown", true, 1.5, bounds), 1);
  assert.equal(valueFromKey("ArrowRight", true, 29.5, bounds), 30);
  assert.equal(valueFromKey("Home", false, 3, bounds), 1);
  assert.equal(valueFromKey("End", false, 3, bounds), 30);
  assert.equal(valueFromKey("End", false, 3, { min: 0, max: Number.POSITIVE_INFINITY }), null, "no known end");
  assert.equal(valueFromKey("a", false, 3, bounds), null);
});

const rects = [{ x: 0, width: 100 }, { x: 100, width: 60 }, { x: 160, width: 40 }];

test("a clip dropped goes before the first clip whose middle is past the pointer", () => {
  assert.equal(insertionSlot(rects, 10), 0);
  assert.equal(insertionSlot(rects, 49), 0);
  assert.equal(insertionSlot(rects, 51), 1);
  assert.equal(insertionSlot(rects, 140), 2);
  assert.equal(insertionSlot(rects, 195), 3, "past the middle of the last clip: at the end");
  assert.equal(insertionSlot([], 30), 0);
  assert.equal(slotX(rects, 1), 100);
  assert.equal(slotX(rects, 3), 200);
  assert.equal(slotX([], 0), 0);
});

test("a slot right before or right after a clip leaves it where it is", () => {
  assert.equal(isNoopSlot(1, 1), true);
  assert.equal(isNoopSlot(1, 2), true);
  assert.equal(isNoopSlot(1, 0), false);
  assert.equal(isNoopSlot(1, 3), false);
});

test("a click on a bar makes a moment of about six seconds around the point, inside the free stretch", () => {
  assert.deepEqual(momentAround({ point: 20, freeStart: 0, freeEnd: 60 }), { start: 17, end: 23 });
  assert.deepEqual(momentAround({ point: 1, freeStart: 0, freeEnd: 60 }), { start: 0, end: 6 }, "against the start of the video");
  assert.deepEqual(momentAround({ point: 59, freeStart: 0, freeEnd: 60 }), { start: 54, end: 60 }, "against the end");
  assert.deepEqual(momentAround({ point: 9, freeStart: 8, freeEnd: 11 }), { start: 8, end: 11 }, "a small gap gives a shorter moment");
  assert.equal(momentAround({ point: 9, freeStart: 8.5, freeEnd: 9.2 }), null, "less than a second of room");
});

test("the free stretches are what the moments in the timeline leave", () => {
  assert.deepEqual(freeStretches([{ start: 4, end: 8 }], 12), [{ min: 0, max: 4 }, { min: 8, max: 12 }]);
  assert.deepEqual(freeStretches([{ start: 0, end: 12 }], 12), []);
  assert.deepEqual(freeStretches([{ start: 6, end: 8 }, { start: 2, end: 4 }], 10), [{ min: 0, max: 2 }, { min: 4, max: 6 }, { min: 8, max: 10 }]);
  assert.deepEqual(freeStretches([], 12), [{ min: 0, max: 12 }]);
});

test("the dragged item is remembered until the drag ends, and read back from its text", () => {
  assert.equal(currentDrag(), null);
  beginDrag({ kind: "photo", id: "a.jpg" });
  assert.deepEqual(currentDrag(), { kind: "photo", id: "a.jpg" });
  endDrag();
  assert.equal(currentDrag(), null);
  for (const payload of [{ kind: "photo", id: "x" }, { kind: "moment", id: "v#0-4" }, { kind: "video", media: "ab12" }, { kind: "clip", id: "c" }] as const) {
    assert.deepEqual(decodePayload(encodePayload(payload)), payload);
  }
  assert.equal(decodePayload("not json"), null);
  assert.equal(decodePayload('{"kind":"clip"}'), null);
  assert.equal(decodePayload('{"kind":"other","id":"x"}'), null);
});

const key = (name: string, extra: Partial<{ ctrlKey: boolean; metaKey: boolean; altKey: boolean; shiftKey: boolean }> = {}) =>
  ({ key: name, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...extra });
const body = { tag: "body" };

test("the shortcuts of section 12", () => {
  assert.equal(commandFor(key("z", { ctrlKey: true }), body), "undo");
  assert.equal(commandFor(key("Z", { ctrlKey: true, shiftKey: true }), body), "redo");
  assert.equal(commandFor(key("y", { ctrlKey: true }), body), "redo");
  assert.equal(commandFor(key("z", { metaKey: true }), body), "undo");
  assert.equal(commandFor(key("Delete"), body), "remove");
  assert.equal(commandFor(key("Backspace"), body), "remove");
  assert.equal(commandFor(key(" "), body), "play");
  assert.equal(commandFor(key("Escape"), body), "clear");
  assert.equal(commandFor(key("ArrowLeft", { altKey: true }), body), "earlier");
  assert.equal(commandFor(key("ArrowRight", { altKey: true }), body), "later");
  assert.equal(commandFor(key("a"), body), null);
  assert.equal(commandFor(key("z", { ctrlKey: true, altKey: true }), body), null);
});

test("in a text field the keys are the field's", () => {
  for (const field of [{ tag: "input", type: "text" }, { tag: "input", type: "number" }, { tag: "textarea" }, { tag: "select" }, { tag: "div", editable: true }]) {
    assert.equal(isTextField(field), true);
    assert.equal(commandFor(key("z", { ctrlKey: true }), field), null);
    assert.equal(commandFor(key("Delete"), field), null);
    assert.equal(commandFor(key("Backspace"), field), null);
  }
});

test("on a button or a slider Delete and undo still work, but Space presses the control", () => {
  const button = { tag: "button" };
  const slider = { tag: "input", type: "range" };
  assert.equal(commandFor(key("Delete"), button), "remove", "the selected clip is a button");
  assert.equal(commandFor(key("z", { ctrlKey: true }), slider), "undo");
  assert.equal(commandFor(key(" "), button), null);
  assert.equal(commandFor(key(" "), slider), null);
  assert.equal(commandFor(key(" "), { tag: "div", role: "tab" }), null);
  assert.equal(commandFor(key("Delete"), { tag: "div", role: "separator" }), null, "a handle is not the clip: Delete does not remove the selection");
  assert.equal(commandFor(key("z", { ctrlKey: true }), { tag: "div", role: "separator" }), "undo");
  assert.equal(isTextField(slider), false);
  assert.equal(isTextField(null), false);
});
