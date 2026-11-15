import assert from "node:assert/strict";
import { test } from "node:test";
import type { Effect, Plan, PlanElement } from "../src/api/types";
import { withPlan } from "../src/lib/catalog";
import { trimRange } from "../src/lib/limits";
import {
  addElements, covers, defaultPhotoSeconds, elementFromCandidate, excludeCandidate, excludeElement, isExcluded, lostEffects, moveElement,
  newMoment, nextUserId, removeElement, restoreCandidate, restoreMoments, restoreWhere, setMomentEdge, setPhotoDuration, settle,
} from "../src/lib/planEdit";
import { catalog, clone, planDocument } from "./fixtures";
import { violations } from "./rules";

const base = planDocument.plan;
const ids = (plan: Plan) => plan.elements.map((element) => element.id);
const find = (plan: Plan, id: string): PlanElement => plan.elements.find((element) => element.id === id)!;
const TOL = 1e-3;
/** A photo of the catalog that the plan does not use (the analysis discarded it, and the user can still add it). */
const spare = catalog.photos.find((item) => item.id.endsWith("IMG_0014.jpg"))!;

test("the fixture plan is valid by the rules the checks use", () => {
  assert.deepEqual(violations(base), []);
});

test("an element of the catalog becomes an element of the plan with the candidate's id, chosen by the user", () => {
  const photo = spare;
  const element = elementFromCandidate(photo, base);
  assert.deepEqual([element.id, element.origin, element.type, element.source], [photo.id, "user", "photo", photo.source]);
  assert.equal(element.duration_origin, "planner", "the duration is the default one until the user changes it");
  assert.deepEqual(element.effects, []);
  assert.equal(element.score, photo.score);
  const moment = catalog.videos[1]!.moments[0]!;
  const video = elementFromCandidate(moment, base);
  assert.deepEqual([video.type, video.start, video.end, video.duration, video.duration_origin], ["video", 0, 4, 4, undefined]);
});

test("an added photo lasts what the photos of the plan last, or three seconds", () => {
  assert.equal(defaultPhotoSeconds(base), 2.4);
  assert.equal(defaultPhotoSeconds({ ...base, elements: [] }), 3);
});

test("adding puts the element at the slot, or at the end, and drops the destination times", () => {
  const photo = elementFromCandidate(spare, base);
  const end = addElements(base, [photo]);
  assert.deepEqual(ids(end), [...ids(base), photo.id]);
  assert.ok(end.elements.every((element) => element.timestamp === undefined), "the timestamps are the server's, they are stale now");
  const second = addElements(base, [photo], 1);
  assert.equal(second.elements[1]?.id, photo.id);
  assert.equal(addElements(end, [photo]), end, "an element already there is not added twice");
  assert.equal(addElements(base, []), base);
  assert.equal(base.elements.length, ids(base).length, "the plan received is not changed");
});

test("adding something hidden brings it back", () => {
  const hidden = catalog.photos.find((item) => item.status === "excluded")!;
  assert.ok(isExcluded(base.excluded, hidden.source));
  const plan = addElements(base, [elementFromCandidate(hidden, base)]);
  assert.equal(plan.excluded.length, 0);
});

test("removing takes the clip out and leaves a plan that is still valid", () => {
  for (const element of base.elements) {
    const plan = removeElement(base, element.id);
    assert.equal(plan.elements.length, base.elements.length - 1);
    assert.deepEqual(violations(plan), [], `after removing ${element.id}`);
  }
  assert.equal(removeElement(base, "nope"), base);
});

test("moving a clip to the front takes the dissolve off it, and the clip that was first has none to take", () => {
  const last = base.elements[base.elements.length - 1]!;
  const plan = moveElement(base, last.id, 0);
  assert.equal(plan.elements[0]?.id, last.id);
  assert.ok(!plan.elements[0]!.effects.some((effect) => effect.type === "dissolve"), "no clip before it to dissolve from");
  assert.deepEqual(violations(plan), []);
  assert.equal(plan.elements[1]?.id, base.elements[0]!.id);
});

test("moving counts the slot in the current list, and a slot next to the clip changes nothing", () => {
  const plan = moveElement(base, base.elements[0]!.id, 3);
  assert.deepEqual(ids(plan).slice(0, 3), [base.elements[1]!.id, base.elements[2]!.id, base.elements[0]!.id]);
  assert.equal(moveElement(base, base.elements[2]!.id, 2), base);
  assert.equal(moveElement(base, base.elements[2]!.id, 3), base);
  assert.equal(moveElement(base, "nope", 1), base);
  assert.deepEqual(violations(moveElement(base, base.elements[3]!.id, base.elements.length)), []);
});

test("a dissolve the planner wrote whose clips changed becomes the user's: the planner's 25% belongs to the clips it chose", () => {
  const plan = moveElement(base, base.elements[4]!.id, 1);
  const moved = plan.elements.find((element) => element.id === base.elements[4]!.id)!;
  assert.equal(moved.effects.find((effect) => effect.type === "dissolve")?.origin, "user");
  const untouched = plan.elements.find((element) => element.id === base.elements[10]!.id)!;
  assert.equal(untouched.effects.find((effect) => effect.type === "dissolve")?.origin, "planner", "clips that kept their neighbors are left alone");
});

test("a plan that is already valid is settled to itself", () => {
  assert.equal(settle(base, base), base);
});

test("a shorter clip shortens its effects to the free space and removes the ones that do not fit", () => {
  const last = base.elements[base.elements.length - 1]!; // 3.6 s: dissolve 0.6 at the start, fade_out 3.0 at the end
  const plan = setPhotoDuration(base, last.id, 1.2);
  const clip = find(plan, last.id);
  assert.equal(clip.duration, 1.2);
  assert.deepEqual(clip.effects.map((effect) => [effect.type, effect.offset, effect.duration]), [["dissolve", 0, 0.6], ["fade_out", 0.6, 0.6]]);
  assert.deepEqual(violations(plan), []);
  assert.equal(lostEffects(base, plan), 0);

  const tiny = setPhotoDuration(base, last.id, 1);
  assert.deepEqual(find(tiny, last.id).effects.map((effect) => [effect.type, effect.offset, effect.duration]), [["dissolve", 0, 0.6], ["fade_out", 0.6, 0.4]]);
  assert.deepEqual(violations(tiny), []);
});

test("an effect left under the minimum is removed and counted", () => {
  const shaped = clone(base);
  const last = shaped.elements[shaped.elements.length - 1]!;
  last.duration = 2;
  last.effects = [{ id: "fx-90", type: "dissolve", offset: 0, duration: 0.9, origin: "user" }, { id: "fx-91", type: "fade_out", offset: 0.9, duration: 1.1, origin: "user" }];
  assert.deepEqual(violations(shaped), []);
  const plan = setPhotoDuration(shaped, last.id, 1);
  assert.deepEqual(find(plan, last.id).effects.map((effect) => effect.type), ["dissolve"], "0.1 s are left for the fade out, under the 0.2 s minimum");
  assert.equal(lostEffects(shaped, plan), 1);
  assert.deepEqual(violations(plan), []);
});

test("a dip stays inside its clip when the clip is trimmed", () => {
  const moment = base.elements.find((element) => element.type === "video")!; // 2.8 s with a dissolve and a dip at 1.0
  const limits = planDocument.limits.elements[moment.id]!;
  const bounds = trimRange(limits, moment)!;
  const plan = setMomentEdge(base, moment.id, "end", (moment.start ?? 0) + 1.2, bounds);
  const clip = find(plan, moment.id);
  assert.equal(clip.duration, 1.2);
  assert.deepEqual(violations(plan), []);
  assert.ok(clip.effects.every((effect) => effect.offset + effect.duration <= 1.2 + TOL));
});

test("a photo lasts 1 to 30 s on the grid of a tenth, and the duration becomes the user's", () => {
  const photo = base.elements.find((element) => element.type === "photo" && element.duration_origin === "planner")!;
  const longer = setPhotoDuration(base, photo.id, 7.26);
  assert.deepEqual([find(longer, photo.id).duration, find(longer, photo.id).duration_origin], [7.3, "user"]);
  assert.equal(find(setPhotoDuration(base, photo.id, 99), photo.id).duration, 30);
  assert.equal(find(setPhotoDuration(base, photo.id, 0.2), photo.id).duration, 1);
  assert.equal(find(setPhotoDuration(base, photo.id, 5, { min: 2, max: 4 }), photo.id).duration, 4, "the server's bounds win");
  assert.equal(setPhotoDuration(base, "nope", 5), base);
  const moment = base.elements.find((element) => element.type === "video")!;
  assert.equal(setPhotoDuration(base, moment.id, 5), base, "a moment has no duration of its own");
  const same = setPhotoDuration(longer, photo.id, 7.3);
  assert.equal(same, longer, "the same value is not an edit");
});

test("the edges of a moment stay inside the video, off the other moments and at least a second apart", () => {
  const moment = base.elements.find((element) => element.type === "video")!; // 4.6 to 7.4 of a 12 s video
  const bounds = trimRange(planDocument.limits.elements[moment.id], moment)!;
  assert.deepEqual([bounds.start.min, bounds.end.max, bounds.minLength], [0, 12, 1]);
  const later = find(setMomentEdge(base, moment.id, "end", 9.04, bounds), moment.id);
  assert.deepEqual([later.start, later.end, later.duration], [4.6, 9, 4.4]);
  assert.equal(find(setMomentEdge(base, moment.id, "end", 99, bounds), moment.id).end, 12);
  assert.equal(find(setMomentEdge(base, moment.id, "end", 4.7, bounds), moment.id).end, 5.6, "at least a second");
  assert.equal(find(setMomentEdge(base, moment.id, "start", -3, bounds), moment.id).start, 0);
  assert.equal(find(setMomentEdge(base, moment.id, "start", 7.2, bounds), moment.id).start, 6.4);
  assert.equal(setMomentEdge(base, moment.id, "start", 4.6, bounds), base);
  const photo = base.elements[0]!;
  assert.equal(setMomentEdge(base, photo.id, "start", 1, bounds), base);
  assert.deepEqual(violations(setMomentEdge(base, moment.id, "start", 5.3, bounds)), []);
});

test("hiding a photo or deleting a moment takes it out of the timeline and tells the planner not to choose it again", () => {
  const photo = base.elements.find((element) => element.type === "photo")!;
  const hidden = excludeElement(base, photo.id);
  assert.ok(!ids(hidden).includes(photo.id));
  assert.deepEqual(hidden.excluded.at(-1), { source: photo.source, start: null, end: null, reason: "user" });
  const moment = base.elements.find((element) => element.type === "video")!;
  const deleted = excludeElement(base, moment.id);
  assert.deepEqual(deleted.excluded.at(-1), { source: moment.source, start: moment.start, end: moment.end, reason: "user" });
  assert.equal(excludeElement(deleted, moment.id), deleted, "nothing left to exclude");
  assert.equal(excludeElement(excludeElement(base, photo.id), photo.id).excluded.length, base.excluded.length + 1, "no duplicate entries");
  assert.deepEqual(violations(hidden), []);
});

test("a candidate that is not in the timeline can be hidden too, and brought back", () => {
  const free = spare;
  const hidden = excludeCandidate(base, free);
  assert.ok(isExcluded(hidden.excluded, free.source));
  assert.equal(restoreCandidate(hidden, free).excluded.length, base.excluded.length);
  assert.equal(restoreCandidate(base, free), base, "nothing to restore");
});

test("a moment deleted overlaps what covers it, as the planner decides", () => {
  const moment = { source: "v.mp4", start: 4, end: 8, reason: "user" };
  assert.equal(covers(moment, "v.mp4", 6, 10), true);
  assert.equal(covers(moment, "v.mp4", 8, 12), false, "touching is not overlapping");
  assert.equal(covers(moment, "w.mp4", 4, 8), false);
  assert.equal(covers(moment, "v.mp4"), false, "a photo is not a moment");
  assert.equal(covers({ ...moment, start: null, end: null }, "v.mp4", 100, 101), true, "a whole file");
});

test("restoring the deleted moments leaves the hidden photos", () => {
  const plan = { ...base, excluded: [...base.excluded, { source: "v.mp4", start: 1, end: 2, reason: "user" }] };
  const restored = restoreMoments(plan);
  assert.deepEqual(restored.excluded, base.excluded);
  assert.equal(restoreMoments(base), base);
  assert.equal(restoreWhere(plan, () => false), plan);
});

test("an element the user made gets the next user-<n> and its own range", () => {
  assert.equal(nextUserId(base), "user-1");
  const first = newMoment(base, "v.mp4", { start: 2, end: 8 });
  assert.deepEqual([first.id, first.origin, first.type, first.duration, first.effects], ["user-1", "user", "video", 6, []]);
  const plan = addElements(base, [first]);
  assert.equal(nextUserId(plan), "user-2");
  assert.equal(nextUserId({ ...plan, elements: [...plan.elements, { ...first, id: "user-7" }] }), "user-8");
});

test("the catalog follows the plan on screen: added, removed, hidden and trimmed", () => {
  const free = spare;
  const statusOf = (view: typeof catalog, id: string) => view.photos.find((item) => item.id === id)?.status;
  assert.equal(statusOf(withPlan(catalog, base), free.id), "discarded");
  const added = addElements(base, [elementFromCandidate(free, base)]);
  assert.equal(statusOf(withPlan(catalog, added), free.id), "selected");
  const used = base.elements.find((element) => element.type === "photo")!;
  assert.equal(statusOf(withPlan(catalog, removeElement(base, used.id)), used.id), "available");
  assert.equal(statusOf(withPlan(catalog, excludeElement(base, used.id)), used.id), "excluded");
  const hidden = catalog.photos.find((item) => item.status === "excluded")!;
  assert.equal(statusOf(withPlan(catalog, restoreCandidate(base, hidden)), hidden.id), "discarded", "back to what the analysis said of it");
  const moment = base.elements.find((element) => element.type === "video")!;
  const shown = withPlan(catalog, base).videos.flatMap((video) => video.moments).find((item) => item.id === moment.id)!;
  assert.deepEqual([shown.start, shown.end], [moment.start, moment.end], "the trimmed range, not the candidate's");
});

test("the moments the user made are listed with the ones the analysis found", () => {
  const video = catalog.videos[1]!;
  const made = newMoment(base, video.source, { start: 4.5, end: 7.5 });
  const view = withPlan(catalog, addElements(base, [made]));
  const moments = view.videos.find((entry) => entry.source === video.source)!.moments;
  assert.equal(moments.length, video.moments.length + 1);
  assert.deepEqual(moments.map((moment) => moment.start), [...moments.map((moment) => moment.start)].sort((a, b) => (a ?? 0) - (b ?? 0)));
  assert.equal(moments.find((moment) => moment.id === made.id)?.status, "selected");
});

test("any sequence of edits leaves the plan inside the rules", () => {
  let state = 12345;
  const random = (limit: number) => { state = (state * 1103515245 + 12345) % 2147483648; return state % limit; };
  let plan = clone(base);
  const spares = catalog.photos.filter((item) => item.status !== "selected").map((item) => elementFromCandidate(item, base));
  for (let step = 0; step < 300; step += 1) {
    const size = plan.elements.length;
    const pick = plan.elements[random(Math.max(size, 1))];
    switch (random(6)) {
      case 0: plan = removeElement(plan, pick?.id ?? ""); break;
      case 1: plan = moveElement(plan, pick?.id ?? "", random(size + 1)); break;
      case 2: plan = setPhotoDuration(plan, pick?.id ?? "", 1 + random(80) / 10); break;
      case 3: plan = addElements(plan, [clone(spares[random(spares.length)]!)], random(size + 1)); break;
      case 4: plan = excludeElement(plan, pick?.id ?? ""); break;
      default: if (pick?.type === "video") plan = setMomentEdge(plan, pick.id, random(2) ? "end" : "start", random(120) / 10, { start: { min: 0, max: 12 }, end: { min: 0, max: 12 }, minLength: 1 });
    }
    assert.deepEqual(violations(plan), [], `after step ${step}`);
    assert.equal(new Set(ids(plan)).size, plan.elements.length, "the ids stay unique");
  }
});

test("effects keep their ids through an edit", () => {
  const photo = base.elements.find((element) => element.type === "photo" && element.effects.length > 0)!;
  const plan = setPhotoDuration(base, photo.id, 5);
  const before: Effect[] = photo.effects;
  assert.deepEqual(find(plan, photo.id).effects.map((effect) => effect.id), before.map((effect) => effect.id));
});
