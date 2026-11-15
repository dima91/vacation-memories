import assert from "node:assert/strict";
import { test } from "node:test";
import type { EffectType, Plan } from "../src/api/types";
import {
  FALLBACK_DEFAULTS, PLACEMENTS, addEffect, applyToVideo, boundsOf, defaultsOf, hardCutNear, hostFor, locate, moveEffect, nextEffectId,
  placeEffect, removeEffect, roomFor, setEffectDuration, setEffectEdge, setEffectStart, type Placement,
} from "../src/lib/effectEdit";
import { setPhotoDuration } from "../src/lib/planEdit";
import { catalog, clone, planDocument } from "./fixtures";
import { violations } from "./rules";

const base = planDocument.plan;
const defaults = defaultsOf(catalog.transitions);
const effectsOf = (plan: Plan, id: string) => plan.elements.find((element) => element.id === id)!.effects;

/** Three photos of the given lengths with no effects: the smallest plan on which the geometry can be read. */
function photos(...lengths: number[]): Plan {
  const template = base.elements.find((entry) => entry.type === "photo")!;
  return {
    ...base, excluded: [],
    elements: lengths.map((duration, index) => ({ ...template, id: `p${index}`, source: `p${index}.jpg`, duration, duration_origin: "user" as const, effects: [] })),
  };
}

test("the library defaults come from the catalog, with the specification's as a fallback", () => {
  assert.deepEqual(defaults, { fade_in: 1, fade_out: 3, dissolve: 0.8, dip: 0.8 });
  assert.deepEqual(defaultsOf([]), FALLBACK_DEFAULTS);
});

test("the rooms agree with the server's: for the fixture plan, for every type and for every effect", () => {
  base.elements.forEach((clip, index) => {
    const limits = planDocument.limits.elements[clip.id]!;
    for (const type of ["fade_in", "fade_out", "dissolve", "dip"] as const satisfies readonly EffectType[]) {
      assert.deepEqual(roomFor(base.elements, index, type), limits.available[type].windows, `${clip.id}: where a ${type} could go`);
    }
    for (const effect of clip.effects) {
      assert.deepEqual(roomFor(base.elements, index, effect.type, 0.2, effect.id), limits.effects[effect.id]!.windows, `${effect.id}: its own room`);
    }
  });
});

test("a clip with nothing on it has its whole length for a fade or a dip, and a dissolve has the room of the shorter clip", () => {
  const plan = photos(4, 3, 5);
  assert.deepEqual(roomFor(plan.elements, 0, "fade_in"), [[0, 4]]);
  assert.deepEqual(roomFor(plan.elements, 0, "fade_out"), [[0, 4]]);
  assert.deepEqual(roomFor(plan.elements, 0, "dissolve"), [], "the first clip has no clip before it");
  assert.deepEqual(roomFor(plan.elements, 1, "dissolve"), [[0, 3]]);
  assert.deepEqual(roomFor(plan.elements, 2, "dissolve"), [[0, 3]]);
  assert.deepEqual(roomFor(plan.elements, 1, "dip"), [[0, 3]]);
});

test("a fade-in goes at the start of the clip with its default length, owned by the user", () => {
  const plan = addEffect(photos(4, 3), "p0", "fade_in", { duration: 1 })!;
  assert.deepEqual(effectsOf(plan, "p0"), [{ id: "fx-1", type: "fade_in", offset: 0, duration: 1, origin: "user" }]);
  assert.deepEqual(violations(plan), []);
});

test("a fade-out ends at the end of the clip", () => {
  const plan = addEffect(photos(4, 3), "p1", "fade_out", { duration: 3 })!;
  assert.deepEqual(effectsOf(plan, "p1")[0], { id: "fx-1", type: "fade_out", offset: 0, duration: 3, origin: "user" });
  const shorter = addEffect(photos(4, 3), "p1", "fade_out", { duration: 1 })!;
  assert.equal(effectsOf(shorter, "p1")[0]?.offset, 2);
});

test("an effect longer than the room is shortened to it, on the grid of a tenth", () => {
  const plan = addEffect(photos(1.25, 3), "p0", "fade_in", { duration: 3 })!;
  assert.equal(effectsOf(plan, "p0")[0]?.duration, 1.2);
  const dissolve = addEffect(photos(2.5, 2.5), "p1", "dissolve", { duration: 5 })!;
  assert.equal(effectsOf(dissolve, "p1")[0]?.duration, 2.5);
});

test("a dissolve is written in the incoming clip, with offset 0, and needs a clip before it", () => {
  const plan = photos(4, 3);
  assert.equal(addEffect(plan, "p0", "dissolve", { duration: 0.8 }), null);
  const next = addEffect(plan, "p1", "dissolve", { duration: 0.8 })!;
  assert.deepEqual(effectsOf(next, "p1"), [{ id: "fx-1", type: "dissolve", offset: 0, duration: 0.8, origin: "user" }]);
  assert.deepEqual(effectsOf(next, "p0"), []);
  assert.deepEqual(violations(next), []);
});

test("a dip goes where it is asked, inside a free stretch, and in the middle by default", () => {
  const plan = photos(6);
  assert.equal(effectsOf(addEffect(plan, "p0", "dip", { duration: 1, offset: 1.5 })!, "p0")[0]?.offset, 1.5);
  assert.equal(effectsOf(addEffect(plan, "p0", "dip", { duration: 1 })!, "p0")[0]?.offset, 2.5);
  assert.equal(effectsOf(addEffect(plan, "p0", "dip", { duration: 1, offset: 5.8 })!, "p0")[0]?.offset, 5, "kept inside the clip");
  assert.equal(effectsOf(addEffect(plan, "p0", "dip", { duration: 1, offset: -3 })!, "p0")[0]?.offset, 0);
});

test("a dip snaps to the nearest free stretch when the point is taken", () => {
  const withFade = addEffect(photos(6), "p0", "fade_in", { duration: 2 })!;
  const dip = addEffect(withFade, "p0", "dip", { duration: 1, offset: 0.5 })!;
  assert.equal(effectsOf(dip, "p0").find((effect) => effect.type === "dip")?.offset, 2);
  assert.deepEqual(violations(dip), []);
});

test("an effect never overlaps another one of the clip: with no room left the plan does not change", () => {
  let plan = photos(1);
  plan = addEffect(plan, "p0", "fade_in", { duration: 0.4 })!;
  plan = addEffect(plan, "p0", "fade_out", { duration: 0.4 })!;
  const dip = addEffect(plan, "p0", "dip", { duration: 0.6 })!;
  assert.deepEqual(violations(dip), []);
  assert.deepEqual(effectsOf(dip, "p0").find((effect) => effect.type === "dip"), { id: "fx-3", type: "dip", offset: 0.4, duration: 0.2, origin: "user" }, "shortened to the 0.2 s that are left");
  assert.equal(addEffect(dip, "p0", "dip", { duration: 0.2 }), null, "no window of 0.2 s is left");
  assert.equal(addEffect(dip, "p0", "fade_in", { duration: 1 }) === null, false, "a fade-in takes the place of the one there");
});

test("a transition put where another one is replaces it: a boundary has one", () => {
  let plan = addEffect(photos(4, 4, 4), "p1", "dissolve", { duration: 0.8 })!;
  plan = addEffect(plan, "p1", "fade_in", { duration: 1 })!;
  assert.deepEqual(effectsOf(plan, "p1").map((effect) => effect.type), ["fade_in"], "from black replaces the dissolve at the same edge");
  plan = addEffect(plan, "p0", "fade_out", { duration: 1 })!;
  plan = addEffect(plan, "p1", "dissolve", { duration: 0.8 })!;
  assert.deepEqual(effectsOf(plan, "p0"), [], "a dissolve into a clip takes the fade-out of the one before");
  assert.deepEqual(effectsOf(plan, "p1").map((effect) => effect.type), ["dissolve"]);
  const closing = addEffect(plan, "p0", "fade_out", { duration: 1 })!;
  assert.deepEqual(effectsOf(closing, "p1"), [], "a fade-out takes the dissolve into the next clip");
  assert.deepEqual(violations(closing), []);
});

test("a second effect of the same kind on a clip replaces the first (fade-in), a dip does not", () => {
  let plan = addEffect(photos(6), "p0", "fade_in", { duration: 1 })!;
  plan = addEffect(plan, "p0", "fade_in", { duration: 2 })!;
  assert.deepEqual(effectsOf(plan, "p0").map((effect) => [effect.type, effect.duration]), [["fade_in", 2]]);
  plan = addEffect(plan, "p0", "dip", { duration: 1, offset: 3 })!;
  plan = addEffect(plan, "p0", "dip", { duration: 1, offset: 4.5 })!;
  assert.equal(effectsOf(plan, "p0").filter((effect) => effect.type === "dip").length, 2);
});

test("ids are the planner's pattern, one after the highest", () => {
  assert.equal(nextEffectId(photos(3)), "fx-1");
  const next = nextEffectId(base);
  const highest = Math.max(...base.elements.flatMap((clip) => clip.effects.map((effect) => Number(/^fx-(\d+)$/.exec(effect.id)?.[1] ?? 0))));
  assert.equal(next, `fx-${highest + 1}`);
});

test("a dissolve dropped on the second half of a clip goes to the next clip (D4); on the last clip it has nowhere to go", () => {
  const plan = photos(4, 4, 4);
  assert.equal(hostFor(plan, "dissolve", "p0", 1), "p0");
  assert.equal(hostFor(plan, "dissolve", "p0", 3), "p1");
  assert.equal(hostFor(plan, "dissolve", "p2", 3), null);
  assert.equal(hostFor(plan, "fade_out", "p0", 0.1), "p0");
  assert.equal(hostFor(plan, "fade_in", "p0", 3.9), "p0");
});

test("the click alternatives put each transition where it belongs", () => {
  const plan = photos(4, 4, 4);
  const at = (placement: Placement, id: string) => placeEffect(plan, id, placement, defaults)!;
  assert.deepEqual(effectsOf(at("fade_in_start", "p0"), "p0").map((effect) => [effect.type, effect.offset, effect.duration]), [["fade_in", 0, 1]]);
  assert.deepEqual(effectsOf(at("fade_out_end", "p2"), "p2").map((effect) => [effect.type, effect.offset, effect.duration]), [["fade_out", 1, 3]]);
  assert.deepEqual(effectsOf(at("dissolve_start", "p1"), "p1").map((effect) => [effect.type, effect.offset]), [["dissolve", 0]]);
  assert.deepEqual(effectsOf(at("dissolve_end", "p0"), "p1").map((effect) => effect.type), ["dissolve"], "'at the end' of a clip is written in the next one");
  assert.deepEqual(effectsOf(at("dip_here", "p1"), "p1").map((effect) => [effect.type, effect.offset]), [["dip", 1.6]]);
  assert.equal(placeEffect(plan, "p0", "dissolve_start", defaults), null, "the first clip has no dissolve at its start");
  assert.equal(placeEffect(plan, "p2", "dissolve_end", defaults), null, "the last clip has no dissolve at its end");
  assert.equal(PLACEMENTS.length, 5);
});

test("a dip 'here' goes at the point asked, the playhead", () => {
  const plan = placeEffect(photos(6), "p0", "dip_here", defaults, 4.5)!;
  assert.equal(effectsOf(plan, "p0")[0]?.offset, 4.1);
});

test("removing an effect takes it away and nothing else", () => {
  const dissolve = base.elements[1]!.effects[0]!;
  const plan = removeEffect(base, dissolve.id);
  assert.deepEqual(effectsOf(plan, base.elements[1]!.id), []);
  assert.equal(plan.elements.length, base.elements.length);
  assert.equal(removeEffect(base, "unknown"), base);
});

test("hard cut dropped on a clip removes the dissolve at the nearest boundary, and does nothing without one", () => {
  const plan = addEffect(photos(4, 4, 4), "p1", "dissolve", { duration: 0.8 })!;
  assert.deepEqual(effectsOf(hardCutNear(plan, "p1", 1), "p1"), []);
  assert.deepEqual(effectsOf(hardCutNear(plan, "p0", 3), "p1"), [], "the second half of a clip is the boundary with the next");
  assert.equal(hardCutNear(plan, "p1", 3), plan, "the boundary with the third clip has none");
  assert.equal(hardCutNear(plan, "p2", 1), plan);
});

test("an effect moved onto another clip keeps its id and its length, and the origin becomes the user's", () => {
  const plan = placeEffect(photos(4, 4, 4), "p0", "fade_out_end", { ...defaults, fade_out: 1 })!;
  const moved = moveEffect(plan, "fx-1", "p2", 2)!;
  assert.deepEqual(effectsOf(moved, "p0"), []);
  assert.deepEqual(effectsOf(moved, "p2"), [{ id: "fx-1", type: "fade_out", offset: 3, duration: 1, origin: "user" }]);
  assert.deepEqual(violations(moved), []);
});

test("a dip moved inside its clip snaps to the nearest free place and keeps inside the clip", () => {
  let plan = addEffect(photos(6), "p0", "fade_in", { duration: 2 })!;
  plan = addEffect(plan, "p0", "dip", { duration: 1, offset: 4 })!;
  const dip = effectsOf(plan, "p0").find((effect) => effect.type === "dip")!;
  const left = moveEffect(plan, dip.id, "p0", 0.5)!;
  assert.equal(locate(left, dip.id)?.effect.offset, 2, "not over the fade-in");
  const right = moveEffect(plan, dip.id, "p0", 6)!;
  assert.equal(locate(right, dip.id)?.effect.offset, 5);
});

test("an effect dropped where it already is does not change the plan (and keeps its origin)", () => {
  const dissolve = base.elements[1]!.effects[0]!;
  assert.equal(moveEffect(base, dissolve.id, base.elements[1]!.id, 0.2), base);
  assert.equal(base.elements[1]!.effects[0]!.origin, "planner");
});

test("an effect with no room where it is dropped is not moved", () => {
  const plan = photos(4, 4);
  const moved = placeEffect(plan, "p1", "dissolve_start", defaults)!;
  assert.equal(moveEffect(moved, "fx-1", "p0", 1), null, "the first clip cannot hold a dissolve");
});

test("the handles: the end of a fade-in grows into the free space and stops at the clip, the start of a fade-out the same", () => {
  let plan = addEffect(photos(4), "p0", "fade_in", { duration: 1 })!;
  assert.equal(locate(setEffectEdge(plan, "fx-1", "end", 2.57), "fx-1")?.effect.duration, 2.6);
  assert.equal(locate(setEffectEdge(plan, "fx-1", "end", 9), "fx-1")?.effect.duration, 4);
  assert.equal(locate(setEffectEdge(plan, "fx-1", "end", 0), "fx-1")?.effect.duration, 0.2, "never shorter than the minimum");
  assert.equal(setEffectEdge(plan, "fx-1", "start", 1), plan, "the start of a fade-in is the start of the clip");
  plan = addEffect(photos(4), "p0", "fade_out", { duration: 1 })!;
  const longer = locate(setEffectEdge(plan, "fx-1", "start", 1), "fx-1")!.effect;
  assert.deepEqual([longer.offset, longer.duration], [1, 3]);
  assert.equal(setEffectEdge(plan, "fx-1", "end", 3.5), plan, "the end of a fade-out is the end of the clip");
});

test("the handles of a dip: each edge moves on its own, the other stays", () => {
  const plan = addEffect(photos(6), "p0", "dip", { duration: 1, offset: 2 })!;
  const start = locate(setEffectEdge(plan, "fx-1", "start", 1.5), "fx-1")!.effect;
  assert.deepEqual([start.offset, start.duration], [1.5, 1.5]);
  const end = locate(setEffectEdge(plan, "fx-1", "end", 4.2), "fx-1")!.effect;
  assert.deepEqual([end.offset, end.duration], [2, 2.2]);
  const crossing = locate(setEffectEdge(plan, "fx-1", "start", 5), "fx-1")!.effect;
  assert.deepEqual([crossing.offset, crossing.duration], [2.8, 0.2], "an edge stops a minimum from the other");
});

test("an edge of a dip does not go over the effects next to it", () => {
  let plan = addEffect(photos(6), "p0", "fade_in", { duration: 1 })!;
  plan = addEffect(plan, "p0", "dip", { duration: 1, offset: 2 })!;
  const dip = effectsOf(plan, "p0").find((effect) => effect.type === "dip")!;
  assert.equal(locate(setEffectEdge(plan, dip.id, "start", 0), dip.id)?.effect.offset, 1);
  assert.deepEqual(violations(setEffectEdge(plan, dip.id, "start", 0)), []);
});

test("the sliders: 'starts at' moves a dip only, 'duration' changes any effect up to its room", () => {
  const plan = addEffect(photos(6), "p0", "dip", { duration: 1, offset: 2 })!;
  assert.equal(locate(setEffectStart(plan, "fx-1", 4.04), "fx-1")?.effect.offset, 4);
  assert.equal(locate(setEffectStart(plan, "fx-1", 9), "fx-1")?.effect.offset, 5);
  assert.equal(locate(setEffectDuration(plan, "fx-1", 9), "fx-1")?.effect.duration, 4);
  const fade = addEffect(photos(6), "p0", "fade_out", { duration: 1 })!;
  assert.equal(setEffectStart(fade, "fx-1", 1), fade, "a fade-out ends at the end of the clip");
  const longer = locate(setEffectDuration(fade, "fx-1", 2.5), "fx-1")!.effect;
  assert.deepEqual([longer.offset, longer.duration], [3.5, 2.5]);
});

test("the same value changes nothing, and does not make a planner's effect the user's", () => {
  const dissolve = base.elements[1]!.effects[0]!;
  assert.equal(setEffectDuration(base, dissolve.id, dissolve.duration), base);
  const edited = setEffectDuration(base, dissolve.id, 0.9);
  assert.equal(locate(edited, dissolve.id)?.effect.origin, "user");
  assert.equal(locate(edited, dissolve.id)?.effect.duration, 0.9);
});

test("a dissolve is bounded by the shorter of its two clips", () => {
  const plan = addEffect(photos(2, 5), "p1", "dissolve", { duration: 0.8 })!;
  assert.equal(locate(setEffectDuration(plan, "fx-1", 4), "fx-1")?.effect.duration, 2);
  assert.deepEqual(boundsOf(plan, "fx-1"), { start: 0, end: 2 });
  assert.equal(boundsOf(plan, "nothing"), null);
});

test("shortening a clip under its dissolve shrinks the dissolve, and the edit stays valid", () => {
  const plan = addEffect(photos(4, 4), "p1", "dissolve", { duration: 2 })!;
  const shorter = setPhotoDuration(plan, "p0", 1.2);
  assert.equal(locate(shorter, "fx-1")?.effect.duration, 1.2);
  assert.deepEqual(violations(shorter), []);
});

test("to the whole video: a dissolve between every pair of clips, a fade-in on the first, a fade-out on the last", () => {
  const plan = photos(4, 4, 4, 4);
  const dissolves = applyToVideo(plan, "dissolve", defaults);
  assert.deepEqual([dissolves.applied, dissolves.skipped], [3, 0]);
  assert.deepEqual(dissolves.plan.elements.map((clip) => clip.effects.map((effect) => effect.type)), [[], ["dissolve"], ["dissolve"], ["dissolve"]]);
  assert.deepEqual(violations(dissolves.plan), []);
  const opening = applyToVideo(plan, "fade_in", defaults);
  assert.deepEqual(opening.plan.elements.map((clip) => clip.effects.map((effect) => effect.type)), [["fade_in"], [], [], []]);
  const closing = applyToVideo(plan, "fade_out", defaults);
  assert.deepEqual(closing.plan.elements.map((clip) => clip.effects.map((effect) => effect.type)), [[], [], [], ["fade_out"]]);
  assert.equal(closing.plan.elements[3]!.effects[0]!.duration, 3);
});

test("applying a dissolve everywhere replaces the planner's, skips the clips with no room and says how many", () => {
  const plan = photos(4, 1.2, 4);
  const done = applyToVideo(plan, "dissolve", { ...defaults, dissolve: 3 });
  assert.equal(done.applied + done.skipped, 2);
  assert.deepEqual(violations(done.plan), []);
  const planner = applyToVideo(base, "dissolve", defaults);
  assert.ok(planner.plan.elements.slice(1).every((clip) => clip.effects.every((effect) => effect.type !== "dissolve" || effect.origin === "user")));
  assert.deepEqual(violations(planner.plan), []);
});

test("hard cut on the whole video takes away every dissolve and nothing else", () => {
  const cut = applyToVideo(base, "cut", defaults);
  assert.ok(cut.applied > 5);
  assert.ok(cut.plan.elements.every((clip) => clip.effects.every((effect) => effect.type !== "dissolve")));
  const kept = base.elements.flatMap((clip) => clip.effects).filter((effect) => effect.type !== "dissolve");
  assert.deepEqual(cut.plan.elements.flatMap((clip) => clip.effects), kept);
  assert.equal(applyToVideo(cut.plan, "cut", defaults).plan, cut.plan);
});

test("a dip has no place on the whole video, and an empty plan has nothing to apply to", () => {
  assert.equal(applyToVideo(base, "dip", defaults).plan, base);
  assert.equal(applyToVideo({ ...base, elements: [] }, "fade_in", defaults).applied, 0);
});

test("a random walk of transition edits never leaves the rules", () => {
  let state = 77;
  const random = (size: number) => { state = (state * 1103515245 + 12345) % 2147483648; return state % size; };
  let plan = clone(base);
  const kinds = ["fade_in", "fade_out", "dissolve", "dip"] as const;
  for (let step = 0; step < 400; step += 1) {
    const clip = plan.elements[random(plan.elements.length)]!;
    const all = plan.elements.flatMap((entry) => entry.effects);
    const effect = all[random(Math.max(all.length, 1))];
    switch (random(7)) {
      case 0: plan = addEffect(plan, clip.id, kinds[random(4)]!, { duration: 0.2 + random(30) / 10, offset: random(60) / 10 }) ?? plan; break;
      case 1: if (effect) plan = removeEffect(plan, effect.id); break;
      case 2: if (effect) plan = moveEffect(plan, effect.id, clip.id, random(50) / 10) ?? plan; break;
      case 3: if (effect) plan = setEffectEdge(plan, effect.id, random(2) ? "start" : "end", random(60) / 10); break;
      case 4: if (effect) plan = setEffectDuration(plan, effect.id, random(40) / 10); break;
      case 5: if (effect) plan = setEffectStart(plan, effect.id, random(60) / 10); break;
      default: plan = setPhotoDuration(plan, clip.id, 1 + random(60) / 10);
    }
    assert.deepEqual(violations(plan), [], `after step ${step}`);
  }
});
