// Plans as the editor produces them after a sequence of edits, on the plan of the shared fixture. `scenarios.test.ts`
// checks them against `tests/fixtures/editor/edited_plans.json`, and `tests/test_editor_edits.py` checks the same file
// with the rules of the server: what the interface saves is what `plan_rules` accepts.
import type { Plan } from "../src/api/types";
import {
  addEffect, applyToVideo, defaultsOf, hardCutNear, moveEffect, placeEffect, removeEffect, setEffectDuration, setEffectEdge, setEffectStart,
} from "../src/lib/effectEdit";
import { trimRange } from "../src/lib/limits";
import {
  addTrack, cutAt, moveTrack, offsetBounds, removePart, removeTrack, restoreDeleted, setOffset, setPartFade, setPartMuted,
} from "../src/lib/musicEdit";
import {
  addElements, elementFromCandidate, excludeCandidate, excludeElement, moveElement, newMoment, removeElement, restoreCandidate, setMomentEdge,
  setPhotoDuration,
} from "../src/lib/planEdit";
import { catalog, planDocument, waveform } from "./fixtures";

const base = planDocument.plan;
const limits = planDocument.limits;
const photos = base.elements.filter((element) => element.type === "photo");
const moment = base.elements.find((element) => element.type === "video")!;
const spare = catalog.photos.find((item) => item.id.endsWith("IMG_0014.jpg"))!;
const hidden = catalog.photos.find((item) => item.status === "excluded")!;
const secondVideo = catalog.videos[1]!;
const bounds = trimRange(limits.elements[moment.id], moment)!;
const last = base.elements[base.elements.length - 1]!;
const defaults = defaultsOf(catalog.transitions);
const total = planDocument.duration;
const trackFile = base.music!.tracks[0]!.file;
const trackLength = waveform.duration;
const shiftBounds = (plan: Plan) => offsetBounds(plan.music!, total);
const dip = base.elements.flatMap((element) => element.effects).find((effect) => effect.type === "dip")!;
const dissolveOf = (index: number) => base.elements[index]!.effects.find((effect) => effect.type === "dissolve")!;

function randomWalk(): Plan {
  let state = 2024;
  const random = (size: number) => { state = (state * 1103515245 + 12345) % 2147483648; return state % size; };
  const extra = [spare, hidden].map((item) => elementFromCandidate(item, base));
  let plan = base;
  for (let step = 0; step < 200; step += 1) {
    const pick = plan.elements[random(Math.max(plan.elements.length, 1))];
    const id = pick?.id ?? "";
    switch (random(6)) {
      case 0: plan = removeElement(plan, id); break;
      case 1: plan = moveElement(plan, id, random(plan.elements.length + 1)); break;
      case 2: plan = setPhotoDuration(plan, id, 1 + random(80) / 10); break;
      case 3: plan = addElements(plan, [extra[random(extra.length)]!], random(plan.elements.length + 1)); break;
      case 4: plan = excludeElement(plan, id); break;
      default:
        if (pick?.type === "video") {
          const edge = random(2) ? "end" : "start";
          plan = setMomentEdge(plan, id, edge, random(120) / 10, { start: { min: 0, max: 12 }, end: { min: 0, max: 12 }, minLength: 1 });
        }
    }
  }
  return plan;
}

export const scenarios: Record<string, () => Plan> = {
  "remove the first clip": () => removeElement(base, base.elements[0]!.id),
  "remove a clip in the middle": () => removeElement(base, base.elements[5]!.id),
  "move the last clip to the front": () => moveElement(base, last.id, 0),
  "move the first clip to the end": () => moveElement(base, base.elements[0]!.id, base.elements.length),
  "move a clip two places later": () => moveElement(base, base.elements[3]!.id, 6),
  "lengthen a photo": () => setPhotoDuration(base, photos[2]!.id, 9.7),
  "shorten the last photo, which has a fade out": () => setPhotoDuration(base, last.id, 1.2),
  "shorten a photo to the minimum": () => setPhotoDuration(base, last.id, 1),
  "trim the end of a moment": () => setMomentEdge(base, moment.id, "end", (moment.start ?? 0) + 1.2, bounds),
  "trim the start of a moment": () => setMomentEdge(base, moment.id, "start", (moment.end ?? 0) - 1, bounds),
  "lengthen a moment to the end of its video": () => setMomentEdge(base, moment.id, "end", 12, bounds),
  "add a photo the analysis discarded, at the front": () => addElements(base, [elementFromCandidate(spare, base)], 0),
  "add a photo that was hidden": () => addElements(base, [elementFromCandidate(hidden, base)], 4),
  "add a whole video": () => addElements(base, secondVideo.moments.map((item) => elementFromCandidate(item, base))),
  "add a moment the user made": () => addElements(base, [newMoment(base, secondVideo.source, { start: 2.5, end: 8.5 })], 3),
  "hide a photo": () => excludeElement(base, photos[1]!.id),
  "delete a moment": () => excludeElement(base, moment.id),
  "hide a photo that is not in the timeline": () => excludeCandidate(base, spare),
  "hide and restore": () => restoreCandidate(excludeCandidate(base, spare), spare),
  "two hundred random edits": randomWalk,

  // transitions
  "from black on a clip in the middle": () => placeEffect(base, base.elements[3]!.id, "fade_in_start", defaults)!,
  "a longer fade-in on the first clip replaces the planner's": () => placeEffect(base, base.elements[0]!.id, "fade_in_start", { ...defaults, fade_in: 2 })!,
  "to black on a clip in the middle, which replaces the dissolve into the next clip": () => placeEffect(base, base.elements[4]!.id, "fade_out_end", defaults)!,
  "a dissolve at the end of a clip is written in the next one": () => placeEffect(base, base.elements[2]!.id, "dissolve_end", { ...defaults, dissolve: 1.2 })!,
  "a dip at a point of a photo": () => placeEffect(base, base.elements[5]!.id, "dip_here", defaults, 1.9)!,
  "a dip dropped over a dissolve snaps after it": () => addEffect(base, base.elements[6]!.id, "dip", { duration: 0.6, offset: 0.1 })!,
  "move a dissolve to the other boundary of its clip": () => moveEffect(base, dissolveOf(2).id, base.elements[2]!.id, 2.2)!,
  "move the dip to another clip": () => moveEffect(base, dip.id, base.elements[1]!.id, 1.2)!,
  "move the dip inside its clip": () => moveEffect(base, dip.id, base.elements.find((element) => element.effects.includes(dip))!.id, 0.4)!,
  "lengthen a dissolve with the end handle": () => setEffectEdge(base, dissolveOf(3).id, "end", 1.4),
  "move the start handle of the dip": () => setEffectEdge(base, dip.id, "start", 0.5),
  "the dip with its slider": () => setEffectStart(base, dip.id, 1.1),
  "a fade-out as long as the clip lets it": () => setEffectDuration(base, last.effects.find((effect) => effect.type === "fade_out")!.id, 99),
  "remove a dissolve": () => removeEffect(base, dissolveOf(5).id),
  "hard cut dropped on a clip": () => hardCutNear(base, base.elements[6]!.id, 0.3),
  "a dissolve between all the clips": () => applyToVideo(base, "dissolve", defaults).plan,
  "no dissolve at all": () => applyToVideo(base, "cut", defaults).plan,
  "from black and to black from the library": () => applyToVideo(applyToVideo(base, "fade_in", { ...defaults, fade_in: 1.5 }).plan, "fade_out", defaults).plan,
  "shorten a clip under its dissolve": () => setPhotoDuration(base, base.elements[2]!.id, 1),

  // music
  "mute the first part": () => setPartMuted(base, 0, 0, true),
  "unmute the second part": () => setPartMuted(base, 0, 1, false),
  "fade the end of the last part": () => setPartFade(base, 0, 1, "out", 5),
  "a fade-in as long as the first part": () => setPartFade(base, 0, 0, "in", 99),
  "cut the first part at the playhead": () => cutAt(base, 6)!.plan,
  "cut the part that is muted": () => cutAt(base, 15)!.plan,
  "delete the first part": () => removePart(base, 0, 0, total),
  "delete the last part": () => removePart(base, 0, 1, total),
  "delete every part": () => removePart(removePart(base, 0, 0, total), 0, 0, total),
  "shift the music later": () => setOffset(base, 7.3, shiftBounds(base)),
  "skip the beginning of the music": () => setOffset(base, -15, shiftBounds(base)),
  "shift the music as far as it goes": () => setOffset(base, 999, shiftBounds(base)),
  "add the track again at the front": () => addTrack(base, trackFile, trackLength, 0, total),
  "reorder the tracks": () => moveTrack(addTrack(base, trackFile, trackLength, null, total), 0, 2),
  "take the track out": () => removeTrack(base, 0, total),
  "restore what was deleted": () => restoreDeleted(removePart(base, 0, 1, total), new Map([[trackFile, trackLength]]), 1, total),
  "delete a part of a shifted music": () => removePart(setOffset(base, -15, shiftBounds(base)), 0, 0, total),
  "add music to a plan that has none": () => addTrack({ ...base, music: undefined }, trackFile, trackLength, null, total),
};
