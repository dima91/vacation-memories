import assert from "node:assert/strict";
import { test } from "node:test";
import { durationRange, effectBounds, largestWindow, trimRange } from "../src/lib/limits";
import { planDocument } from "./fixtures";

const { plan, limits } = planDocument;
const photo = plan.elements.find((element) => element.type === "photo")!;
const moment = plan.elements.find((element) => element.type === "video")!;

test("the duration range of a photo comes from the server, a moment has none", () => {
  assert.deepEqual(durationRange(limits.elements[photo.id]), { min: 1, max: 30, step: 0.1 });
  assert.equal(durationRange(limits.elements[moment.id]), null);
  assert.equal(durationRange(undefined), null);
});

test("the bounds of a moment's start and end leave the minimum length", () => {
  const range = trimRange(limits.elements[moment.id], moment);
  assert.ok(range);
  assert.equal(range.start.min, 0);
  assert.equal(range.start.max, (moment.end ?? 0) - range.minLength);
  assert.equal(range.end.min, (moment.start ?? 0) + range.minLength);
  assert.equal(range.end.max, 12, "the end of the video");
  assert.equal(trimRange(limits.elements[photo.id], photo), null);
});

test("an unknown end of the video leaves the end unbounded", () => {
  const range = trimRange({ ...limits.elements[moment.id]!, trim: { start_min: 0, end_max: null, min_length: 1 } }, moment);
  assert.equal(range?.end.max, Number.POSITIVE_INFINITY);
});

test("the room of an effect is the free window that holds it", () => {
  const withDissolve = plan.elements.find((element) => element.type === "photo" && element.effects.some((effect) => effect.type === "dissolve"))!;
  const dissolve = withDissolve.effects.find((effect) => effect.type === "dissolve")!;
  const room = limits.elements[withDissolve.id]?.effects[dissolve.id];
  assert.deepEqual(effectBounds(room, dissolve), { min: room!.windows[0]![0], max: room!.windows[0]![1] });
  assert.equal(effectBounds(room, { offset: 5, duration: 1 }), null, "outside every window");
  assert.equal(effectBounds(undefined, dissolve), null);
});

test("the available space is the longest free stretch", () => {
  assert.equal(largestWindow({ type: "dip", windows: [[0.6, 1.0], [1.6, 3.0]] }), 1.4);
  assert.equal(largestWindow({ type: "dip", windows: [] }), 0);
  assert.equal(largestWindow(undefined), 0);
});
