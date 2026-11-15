// The rules of `plan_rules.check_plan` that an edit can break, written again here only to check the edits against them.
import type { Plan } from "../src/api/types";

const TOL = 1e-3;

export function violations(plan: Plan, minimum = 0.2): string[] {
  const found: string[] = [];
  plan.elements.forEach((element, index) => {
    const spans: [number, number, string][] = [];
    if (element.type === "photo" && element.duration_origin === "user" && (element.duration < 1 - TOL || element.duration > 30 + TOL)) found.push(`${element.id}: photo duration`);
    if (element.type === "video" && element.duration < 1 - TOL) found.push(`${element.id}: moment shorter than a second`);
    for (const effect of element.effects) {
      const end = effect.offset + effect.duration;
      if (effect.offset < -TOL || end > element.duration + TOL) found.push(`${effect.id}: outside the clip`);
      if ((effect.type === "fade_in" || effect.type === "dissolve") && Math.abs(effect.offset) > TOL) found.push(`${effect.id}: not at the start`);
      if (effect.type === "fade_out" && Math.abs(end - element.duration) > TOL) found.push(`${effect.id}: not at the end`);
      if (effect.origin === "user" && effect.duration < minimum - TOL) found.push(`${effect.id}: shorter than the minimum`);
      if (effect.type === "dissolve") {
        const before = plan.elements[index - 1];
        if (!before) found.push(`${effect.id}: dissolve on the first clip`);
        else if (effect.duration > before.duration + TOL) found.push(`${effect.id}: longer than the previous clip`);
        else if (effect.origin === "planner" && effect.duration > 0.25 * Math.min(before.duration, element.duration) + TOL) found.push(`${effect.id}: over 25%`);
      }
      spans.push([effect.offset, end, effect.id]);
    }
    const next = plan.elements[index + 1]?.effects.find((effect) => effect.type === "dissolve");
    if (next) spans.push([Math.max(0, element.duration - next.duration), element.duration, `dissolve into ${plan.elements[index + 1]!.id}`]);
    for (let a = 0; a < spans.length; a += 1) {
      for (let b = a + 1; b < spans.length; b += 1) {
        if (spans[a]![0] < spans[b]![1] - TOL && spans[b]![0] < spans[a]![1] - TOL) found.push(`${spans[a]![2]} overlaps ${spans[b]![2]}`);
      }
    }
  });
  return found;
}


/** The music rules of `check_plan`: ordered parts of at least a second, inside their track, fades that fit, a shift that keeps a second of music. */
export function musicViolations(plan: Plan, videoSeconds: number, trackLengths: Readonly<Record<string, number>> = {}): string[] {
  const music = plan.music;
  if (!music) return [];
  const found: string[] = [];
  let total = 0;
  music.tracks.forEach((track, number) => {
    if (track.parts.length === 0) found.push(`tracks[${number}]: no parts`);
    let previousEnd = 0;
    track.parts.forEach((part, position) => {
      const here = `tracks[${number}].parts[${position}]`;
      const length = part.end - part.start;
      total += length;
      if (part.start < previousEnd - TOL) found.push(`${here}: overlaps the part before`);
      previousEnd = Math.max(previousEnd, part.end);
      if (length < 1 - TOL) found.push(`${here}: shorter than a second`);
      const known = trackLengths[track.file];
      if (known !== undefined && part.end > known + TOL) found.push(`${here}: ends after the track`);
      if (part.fade_in + part.fade_out > length + TOL) found.push(`${here}: the fades do not fit`);
      if (part.fade_in < 0 || part.fade_out < 0) found.push(`${here}: negative fade`);
    });
  });
  const low = -Math.max(0, total - 1);
  const high = Math.max(0, videoSeconds - 1);
  if (music.offset < low - TOL || music.offset > high + TOL) found.push(`music.offset ${music.offset} outside ${low}…${high}`);
  return found;
}
