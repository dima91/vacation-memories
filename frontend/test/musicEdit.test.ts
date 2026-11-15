import assert from "node:assert/strict";
import { test } from "node:test";
import type { MusicPart, MusicPlan, Plan } from "../src/api/types";
import {
  addTrack, countDeleted, cutAt, deletedStretches, fadeRoom, moveTrack, musicLength, offsetBounds, removePart, removeTrack, restoreDeleted,
  setOffset, setPartFade, setPartMuted, trackExtents,
} from "../src/lib/musicEdit";
import { musicSlots } from "../src/lib/timeline";
import { clone, planDocument } from "./fixtures";
import { musicViolations } from "./rules";

const base = planDocument.plan;
const total = planDocument.duration;
const part = (start: number, end: number, extra: Partial<MusicPart> = {}): MusicPart => ({ start, end, muted: false, fade_in: 0, fade_out: 0, ...extra });
const planWith = (music: MusicPlan | undefined): Plan => ({ ...base, music });
const two: MusicPlan = { offset: 0, tracks: [{ file: "a.mp3", parts: [part(0, 10), part(20, 25, { muted: true })] }, { file: "b.mp3", parts: [part(5, 15)] }] };
const lengths = new Map([["a.mp3", 60], ["b.mp3", 30], ["c.mp3", 40]]);
const asRecord = (map: ReadonlyMap<string, number | undefined>) => Object.fromEntries([...map].filter(([, value]) => value !== undefined)) as Record<string, number>;

test("the bounds of the shift are the server's, for the fixture plan", () => {
  const server = planDocument.limits.music!.offset;
  const mine = offsetBounds(base.music!, total);
  assert.equal(mine.min, server.min);
  assert.ok(Math.abs(mine.max - server.max) < 1e-9);
  assert.equal(planDocument.limits.music!.min_part, 1);
});

test("the bounds of the shift keep a second of music in the video, from either end", () => {
  assert.deepEqual(offsetBounds(two, 120), { min: -24, max: 119 });
  assert.deepEqual(offsetBounds({ tracks: [{ file: "a", parts: [part(0, 0.5)] }], offset: 0 }, 60), { min: -0, max: 59 });
  assert.deepEqual(offsetBounds({ tracks: [], offset: 0 }, 0.5), { min: -0, max: 0 });
});

test("the shift is set on the grid of a tenth, inside the bounds, and an unchanged shift changes nothing", () => {
  const bounds = { min: -10, max: 30 };
  assert.equal(setOffset(planWith(two), 2.04, bounds).music?.offset, 2);
  assert.equal(setOffset(planWith(two), 99, bounds).music?.offset, 30);
  assert.equal(setOffset(planWith(two), -99, bounds).music?.offset, -10);
  const plan = planWith(two);
  assert.equal(setOffset(plan, 0, bounds), plan);
  assert.deepEqual(setOffset({ ...base, music: undefined }, 0, bounds).music, { tracks: [], offset: 0 }, "the first edit makes the block");
});

test("a track is added whole, at the end or at a slot, and makes the music block of a plan that had none", () => {
  const bare: Plan = { ...base, music: undefined };
  const first = addTrack(bare, "a.mp3", 60, null, total);
  assert.deepEqual(first.music, { offset: 0, tracks: [{ file: "a.mp3", parts: [part(0, 60)] }] });
  const front = addTrack(planWith(two), "c.mp3", 40, 0, total);
  assert.deepEqual(front.music?.tracks.map((track) => track.file), ["c.mp3", "a.mp3", "b.mp3"]);
  const middle = addTrack(planWith(two), "c.mp3", 40, 1, total);
  assert.deepEqual(middle.music?.tracks.map((track) => track.file), ["a.mp3", "c.mp3", "b.mp3"]);
  assert.deepEqual(addTrack(planWith(two), "c.mp3", 40, 9, total).music?.tracks.map((track) => track.file), ["a.mp3", "b.mp3", "c.mp3"]);
  assert.deepEqual(musicViolations(middle, total, asRecord(lengths)), []);
});

test("tracks are reordered: the slot counts in the current list, like a clip's", () => {
  const files = (plan: Plan) => plan.music?.tracks.map((track) => track.file);
  const three = addTrack(planWith(two), "c.mp3", 40, null, total);
  assert.deepEqual(files(moveTrack(three, 0, 3)), ["b.mp3", "c.mp3", "a.mp3"]);
  assert.deepEqual(files(moveTrack(three, 2, 0)), ["c.mp3", "a.mp3", "b.mp3"]);
  assert.deepEqual(files(moveTrack(three, 0, 2)), ["b.mp3", "a.mp3", "c.mp3"]);
  assert.equal(moveTrack(three, 0, 0), three);
  assert.equal(moveTrack(three, 0, 1), three, "right after itself is where it is");
  assert.equal(moveTrack(three, 9, 0), three);
});

test("moving a track moves its parts on the axis of the video", () => {
  const moved = moveTrack(planWith(two), 1, 0);
  assert.deepEqual(musicSlots(moved.music).map((slot) => [slot.file, slot.start, slot.end]), [["b.mp3", 0, 10], ["a.mp3", 10, 20], ["a.mp3", 20, 25]]);
});

test("deleting a part shifts the following ones, and a track left with no part goes away", () => {
  const plan = planWith(two);
  const first = removePart(plan, 0, 0, total);
  assert.deepEqual(musicSlots(first.music).map((slot) => [slot.file, slot.start, slot.end]), [["a.mp3", 0, 5], ["b.mp3", 5, 15]]);
  assert.deepEqual(first.music?.tracks[0]?.parts, [part(20, 25, { muted: true })]);
  assert.deepEqual(removePart(plan, 1, 0, total).music?.tracks.map((track) => track.file), ["a.mp3"]);
  const only = removePart(planWith({ tracks: [{ file: "a.mp3", parts: [part(0, 10)] }], offset: 2 }), 0, 0, total);
  assert.deepEqual(only.music, { tracks: [], offset: 2 }, "a video without music: the block stays, empty");
  assert.equal(removePart(plan, 5, 0, total), plan);
  assert.equal(two.tracks[0]?.parts.length, 2, "the original is untouched");
});

test("removing a track takes all its parts", () => {
  assert.deepEqual(removeTrack(planWith(two), 0, total).music?.tracks.map((track) => track.file), ["b.mp3"]);
  assert.equal(removeTrack(planWith(two), 7, total).music, two);
});

test("deleting music brings the shift back inside the bounds it now has", () => {
  const shifted = planWith({ tracks: [{ file: "a.mp3", parts: [part(0, 30), part(30, 40)] }], offset: -35 });
  assert.deepEqual(musicViolations(shifted, total), []);
  const smaller = removePart(shifted, 0, 0, total);
  assert.equal(smaller.music?.offset, -9, "10 s of music left: at most 9 s can be skipped");
  assert.deepEqual(musicViolations(smaller, total), []);
});

test("a part is muted and unmuted, and a fade is set on the grid, inside what is left of the part", () => {
  const plan = planWith(two);
  assert.equal(setPartMuted(plan, 0, 0, true).music?.tracks[0]?.parts[0]?.muted, true);
  assert.equal(setPartMuted(plan, 0, 1, true), plan, "it was muted already");
  const faded = setPartFade(plan, 0, 0, "in", 2.34);
  assert.equal(faded.music?.tracks[0]?.parts[0]?.fade_in, 2.3);
  const both = setPartFade(faded, 0, 0, "out", 99);
  assert.equal(both.music?.tracks[0]?.parts[0]?.fade_out, 7.7, "the fade-out takes what the fade-in left");
  assert.deepEqual(musicViolations(both, total), []);
  assert.equal(setPartFade(both, 0, 0, "in", -3).music?.tracks[0]?.parts[0]?.fade_in, 0);
  assert.equal(fadeRoom(both.music!.tracks[0]!.parts[0]!, "in"), 2.3);
});

test("a part is cut where the playhead is: the fade-in stays with the first piece, the fade-out goes to the second", () => {
  const plan = planWith({ offset: 0, tracks: [{ file: "a.mp3", parts: [part(0, 20, { fade_in: 2, fade_out: 3, muted: true })] }] });
  const cut = cutAt(plan, 8)!;
  assert.deepEqual([cut.track, cut.part], [0, 1]);
  assert.deepEqual(cut.plan.music?.tracks[0]?.parts, [part(0, 8, { fade_in: 2, muted: true }), part(8, 20, { fade_out: 3, muted: true })]);
  assert.deepEqual(musicSlots(cut.plan.music).map((slot) => [slot.start, slot.end]), [[0, 8], [8, 20]], "the music plays the same");
  assert.deepEqual(musicViolations(cut.plan, total), []);
});

test("a cut follows the shift, and snaps to a tenth of the track", () => {
  const shifted = planWith({ offset: 3, tracks: [{ file: "a.mp3", parts: [part(10, 40)] }] });
  const cut = cutAt(shifted, 3 + 4.04)!;
  assert.equal(cut.plan.music?.tracks[0]?.parts[1]?.start, 14);
  const early = planWith({ offset: -5, tracks: [{ file: "a.mp3", parts: [part(0, 30)] }] });
  assert.equal(cutAt(early, 10)?.plan.music?.tracks[0]?.parts[1]?.start, 15, "the beginning of the track is before the video");
});

test("no cut where there is no music, or less than the minimum would be left on a side", () => {
  const plan = planWith({ offset: 0, tracks: [{ file: "a.mp3", parts: [part(0, 10)] }] });
  assert.equal(cutAt(plan, 0.5), null);
  assert.equal(cutAt(plan, 9.5), null);
  assert.equal(cutAt(plan, 10), null, "the music has ended");
  assert.notEqual(cutAt(plan, 1), null, "a piece of exactly the minimum is allowed");
  assert.notEqual(cutAt(plan, 9), null);
  assert.equal(cutAt({ ...base, music: undefined }, 5), null);
  assert.equal(cutAt(plan, 2, 3), null, "with the server's larger minimum");
});

test("the fades of a cut part fit its pieces", () => {
  const plan = planWith({ offset: 0, tracks: [{ file: "a.mp3", parts: [part(0, 10, { fade_in: 4, fade_out: 4 })] }] });
  const cut = cutAt(plan, 2)!;
  assert.deepEqual(cut.plan.music?.tracks[0]?.parts.map((entry) => [entry.fade_in, entry.fade_out]), [[2, 0], [0, 4]]);
  assert.deepEqual(musicViolations(cut.plan, total), []);
});

test("what was deleted from a track is what its parts leave of it", () => {
  const track = { file: "a.mp3", parts: [part(5, 20), part(30, 31), part(40, 50)] };
  assert.deepEqual(deletedStretches(track, 60), [{ start: 0, end: 5 }, { start: 20, end: 30 }, { start: 31, end: 40 }, { start: 50, end: 60 }]);
  assert.deepEqual(deletedStretches({ file: "a.mp3", parts: [part(0, 60)] }, 60), []);
  assert.deepEqual(deletedStretches({ file: "a.mp3", parts: [part(0.4, 59.5)] }, 60), [], "less than the minimum is not a part");
});

test("restoring puts the deleted stretches back as parts, in the order of the track, and counts them for the button", () => {
  const plan = planWith({ offset: 0, tracks: [{ file: "a.mp3", parts: [part(10, 20), part(40, 50)] }, { file: "b.mp3", parts: [part(0, 30)] }] });
  assert.equal(countDeleted(plan.music, lengths), 3);
  const restored = restoreDeleted(plan, lengths, 1, total);
  assert.deepEqual(restored.music?.tracks[0]?.parts.map((entry) => [entry.start, entry.end]), [[0, 10], [10, 20], [20, 40], [40, 50], [50, 60]]);
  assert.deepEqual(restored.music?.tracks[1], plan.music?.tracks[1]);
  assert.deepEqual(musicViolations(restored, total, asRecord(lengths)), []);
  assert.equal(countDeleted(restored.music, lengths), 0);
  assert.equal(restoreDeleted(restored, lengths, 1, total), restored, "nothing deleted, nothing changes");
  assert.equal(countDeleted(plan.music, new Map()), 0, "a track whose length is not known is left alone");
});

test("the length of the music, and where each track sits on the axis", () => {
  assert.equal(musicLength(two), 25);
  assert.deepEqual(trackExtents(two), [{ start: 0, end: 15 }, { start: 15, end: 25 }]);
  assert.deepEqual(trackExtents({ ...two, offset: 2 }), [{ start: 2, end: 17 }, { start: 17, end: 27 }]);
  assert.deepEqual(trackExtents(undefined), []);
});

test("a random walk of music edits never leaves the rules", () => {
  let state = 5;
  const random = (size: number) => { state = (state * 1103515245 + 12345) % 2147483648; return state % size; };
  let plan = clone(base);
  const files = ["a.mp3", "b.mp3", "c.mp3"];
  const known = { "a.mp3": 60, "b.mp3": 30, "c.mp3": 40, "brano.wav": 20 };
  for (let step = 0; step < 400; step += 1) {
    const tracks = plan.music?.tracks ?? [];
    const track = random(Math.max(tracks.length, 1));
    const partIndex = random(Math.max(tracks[track]?.parts.length ?? 1, 1));
    switch (random(9)) {
      case 0: { const file = files[random(3)]!; plan = addTrack(plan, file, known[file as keyof typeof known], random(tracks.length + 1), total); break; }
      case 1: plan = removePart(plan, track, partIndex, total); break;
      case 2: plan = removeTrack(plan, track, total); break;
      case 3: plan = moveTrack(plan, track, random(tracks.length + 1)); break;
      case 4: plan = cutAt(plan, random(total * 10) / 10)?.plan ?? plan; break;
      case 5: plan = setPartFade(plan, track, partIndex, random(2) ? "in" : "out", random(80) / 10); break;
      case 6: plan = setPartMuted(plan, track, partIndex, random(2) === 0); break;
      case 7: plan = setOffset(plan, random(600) / 10 - 30, offsetBounds(plan.music ?? { tracks: [], offset: 0 }, total)); break;
      default: plan = restoreDeleted(plan, new Map(Object.entries(known)), 1, total);
    }
    assert.deepEqual(musicViolations(plan, total, known), [], `after step ${step}`);
  }
});
