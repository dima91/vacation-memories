import assert from "node:assert/strict";
import { test } from "node:test";
import type { MusicPlan } from "../src/api/types";
import {
  clampScroll, clampZoom, clipAt, clipRects, clipStart, contentWidth, maxScroll, musicSlots, peaksBetween, pixelsPerSecond,
  scrollAfterZoom, scrollByPage, scrollToReveal, stepPlayhead, tickStep, tickTimes, timeAtPointer, timeToX, windowSeconds, xToTime, type View,
} from "../src/lib/timeline";
import { planDocument } from "./fixtures";

const view = (zoom = 1, width = 900, window = 90): View => ({ window, zoom, width });

test("the window is at least 90 s and fits the video plus 5 s, in tens", () => {
  assert.equal(windowSeconds(0), 90);
  assert.equal(windowSeconds(65), 90);
  assert.equal(windowSeconds(85), 90);
  assert.equal(windowSeconds(86), 100);
  assert.equal(windowSeconds(300), 310);
  assert.equal(windowSeconds(-5), 90);
});

test("ticks are every 10 s, every 20 s above a 150 s window", () => {
  assert.equal(tickStep(150), 10);
  assert.equal(tickStep(160), 20);
  assert.deepEqual(tickTimes(30), [0, 10, 20, 30]);
  assert.equal(tickTimes(310).at(-1), 300);
});

test("the zoom stays between 1× and 6×", () => {
  assert.equal(clampZoom(0.2), 1);
  assert.equal(clampZoom(3.5), 3.5);
  assert.equal(clampZoom(9), 6);
});

test("pixels, seconds and the scroll limits at the zoom steps", () => {
  assert.equal(pixelsPerSecond(view(1, 900, 90)), 10);
  assert.equal(pixelsPerSecond(view(3, 900, 90)), 30);
  assert.equal(contentWidth(view(2)), 1800);
  assert.equal(maxScroll(view(1)), 0, "no scrollbar at 1×");
  assert.equal(maxScroll(view(2)), 900);
  assert.equal(clampScroll(-10, view(2)), 0);
  assert.equal(clampScroll(5000, view(2)), 900);
  assert.equal(xToTime(timeToX(12.5, view(4)), view(4)), 12.5);
});

test("a click on the ruler becomes a time inside the video", () => {
  assert.equal(timeAtPointer(100, 0, view(1), 60), 10);
  assert.equal(timeAtPointer(100, 450, view(2), 60), 27.5);
  assert.equal(timeAtPointer(-20, 0, view(1), 60), 0);
  assert.equal(timeAtPointer(890, 0, view(1), 60), 60, "past the end of the video it stops at the end");
});

test("zooming keeps the anchor time under the same pixel", () => {
  const before = view(1);
  const scroll = scrollAfterZoom(0, before, 3, 30);
  const after = view(3);
  assert.equal(timeToX(30, after) - scroll, timeToX(30, before) - 0);
  assert.equal(scrollAfterZoom(0, before, 1, 30), 0, "back to 1× there is nothing to scroll");
  assert.equal(scrollAfterZoom(0, before, 99, 90), maxScroll(view(6)), "capped at the end, at 6×");
});

test("the arrows scroll by half a viewport and the playhead is brought into view", () => {
  assert.equal(scrollByPage(0, view(4), 1), 450);
  assert.equal(scrollByPage(100, view(4), -1), 0);
  assert.equal(scrollToReveal(0, view(4), 5), 0, "already visible");
  assert.ok(scrollToReveal(0, view(4), 60) > 0);
  assert.equal(scrollToReveal(400, view(4), 10, 24), 376, "scrolls back just enough");
  assert.equal(scrollToReveal(0, view(4), 60, 24), 1524, "and forward just enough");
});

test("the playhead moves ±1 s, ±5 s with Shift, inside the video", () => {
  assert.equal(stepPlayhead(10, 1, false, 60), 11);
  assert.equal(stepPlayhead(10, -1, true, 60), 5);
  assert.equal(stepPlayhead(1, -1, true, 60), 0);
  assert.equal(stepPlayhead(59, 1, true, 60), 60);
});

test("clips are laid out where the server put them, in sequence otherwise", () => {
  const elements = planDocument.plan.elements;
  const rects = clipRects(elements, view(1, 900, 90));
  assert.equal(rects.length, elements.length);
  assert.equal(rects[0]?.x, 0);
  assert.ok(rects.every((rect, index) => index === 0 || rect.start >= (rects[index - 1]?.start ?? 0)));
  const bare = clipRects(elements.map((element) => ({ ...element, timestamp: undefined })), view());
  assert.equal(bare[1]?.start, bare[0]?.duration, "without timestamps the clips follow each other");
  assert.equal(clipStart(elements, elements[2]?.id ?? ""), elements[2]?.timestamp);
  assert.equal(clipStart(elements, "nothing"), null);
});

test("the clip at a time: the later one where two dissolve into each other", () => {
  const elements = planDocument.plan.elements;
  const second = elements[1];
  assert.ok(second && second.timestamp !== undefined);
  assert.equal(clipAt(elements, 0)?.id, elements[0]?.id);
  assert.equal(clipAt(elements, second.timestamp + 0.01)?.id, second.id);
  assert.equal(clipAt(elements, 9999), null);
});

test("music parts are laid out in sequence and shifted by the offset", () => {
  const music: MusicPlan = { offset: 2, tracks: [
    { file: "a.mp3", parts: [{ start: 0, end: 10, muted: false, fade_in: 0, fade_out: 0 }, { start: 10, end: 14, muted: true, fade_in: 0, fade_out: 0 }] },
    { file: "b.mp3", parts: [{ start: 5, end: 8, muted: false, fade_in: 0, fade_out: 0 }] },
  ] };
  const slots = musicSlots(music);
  assert.deepEqual(slots.map((slot) => [slot.file, slot.start, slot.end, slot.muted]), [["a.mp3", 2, 12, false], ["a.mp3", 12, 16, true], ["b.mp3", 16, 19, false]]);
  assert.equal(slots[2]?.trackStart, 5);
  assert.equal(musicSlots({ ...music, offset: -3 })[0]?.start, -3, "a negative offset skips the beginning");
  assert.deepEqual(musicSlots(undefined), []);
});

test("the peaks of a stretch of a track", () => {
  const peaks = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
  assert.deepEqual(peaksBetween(peaks, 10, 2, 5), [2, 3, 4]);
  assert.deepEqual(peaksBetween(peaks, 10, -4, 2), [0, 1]);
  assert.deepEqual(peaksBetween(peaks, 10, 8, 50), [8, 9]);
  assert.deepEqual(peaksBetween([], 10, 0, 1), []);
});
