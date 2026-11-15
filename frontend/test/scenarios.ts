// Plans as the editor produces them after a sequence of edits, on the plan of the shared fixture. `scenarios.test.ts`
// checks them against `tests/fixtures/editor/edited_plans.json`, and `tests/test_editor_edits.py` checks the same file
// with the rules of the server: what the interface saves is what `plan_rules` accepts.
import type { Plan } from "../src/api/types";
import { trimRange } from "../src/lib/limits";
import {
  addElements, elementFromCandidate, excludeCandidate, excludeElement, moveElement, newMoment, removeElement, restoreCandidate, setMomentEdge,
  setPhotoDuration,
} from "../src/lib/planEdit";
import { catalog, planDocument } from "./fixtures";

const base = planDocument.plan;
const limits = planDocument.limits;
const photos = base.elements.filter((element) => element.type === "photo");
const moment = base.elements.find((element) => element.type === "video")!;
const spare = catalog.photos.find((item) => item.id.endsWith("IMG_0014.jpg"))!;
const hidden = catalog.photos.find((item) => item.status === "excluded")!;
const secondVideo = catalog.videos[1]!;
const bounds = trimRange(limits.elements[moment.id], moment)!;
const last = base.elements[base.elements.length - 1]!;

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
};
