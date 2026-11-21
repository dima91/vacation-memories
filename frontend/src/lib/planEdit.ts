// The user's edits of a plan (section 12 of UI-SPEC.md), as pure functions: they take a plan and give a new one, never
// change the one they receive. The rules (limits) live in Python and arrive next to the plan; here an edit is kept
// inside them with the bounds the server sent, and the server checks again on every save (section 4).
//
// What an edit changes in the transitions is settled here only as far as it must for the plan to stay saveable: an
// effect that no longer fits its clip is shortened or removed, the dissolve of a clip that has no clip before it goes.
import type { CatalogItem, Effect, EffectType, Exclusion, Plan, PlanElement } from "../api/types";
import { STEP, snap } from "./drag";
import type { Range } from "./limits";
import { clamp } from "./timeline";

export const MIN_EFFECT_SECONDS = 0.2;
export const PHOTO_FALLBACK_SECONDS = 3;
export const PHOTO_BOUNDS: Range = { min: 1, max: 30 };
const TOLERANCE = 1e-3;
/** A moment that shares more than this with a deleted one is left out (the planner's rule, `EXCLUSION_OVERLAP_SECONDS`). */
const OVERLAP_SECONDS = 1e-3;

export const round6 = (value: number): number => Math.round(value * 1e6) / 1e6;

// --- exclusions ---------------------------------------------------------------------------------------------

/** Whether a deleted or hidden entry covers a photo (no extent) or a moment of a video. */
export function covers(exclusion: Exclusion, source: string, start?: number | null, end?: number | null): boolean {
  if (exclusion.source !== source) return false;
  if (exclusion.start === null || exclusion.end === null) return true;
  if (start === undefined || start === null || end === undefined || end === null) return false;
  return Math.min(end, exclusion.end) - Math.max(start, exclusion.start) > OVERLAP_SECONDS;
}

export const isExcluded = (exclusions: readonly Exclusion[], source: string, start?: number | null, end?: number | null): boolean =>
  exclusions.some((exclusion) => covers(exclusion, source, start, end));

export const exclusionOf = (source: string, start?: number | null, end?: number | null): Exclusion => ({
  source, start: start ?? null, end: end ?? null, reason: "user",
});

function withExclusion(plan: Plan, exclusion: Exclusion): Plan {
  const known = plan.excluded.some((entry) => entry.source === exclusion.source && entry.start === exclusion.start && entry.end === exclusion.end);
  return known ? plan : { ...plan, excluded: [...plan.excluded, exclusion] };
}

// --- building elements --------------------------------------------------------------------------------------

/** `user-<n>`: the next number after the ones in the plan. */
export function nextUserId(plan: Plan): string {
  let highest = 0;
  for (const element of plan.elements) {
    const found = /^user-(\d+)$/.exec(element.id);
    if (found) highest = Math.max(highest, Number(found[1]));
  }
  return `user-${highest + 1}`;
}

/** What a photo added by the user lasts: the middle of the photos already there, or 3 s. */
export function defaultPhotoSeconds(plan: Plan): number {
  const lengths = plan.elements.filter((element) => element.type === "photo").map((element) => element.duration).sort((a, b) => a - b);
  if (lengths.length === 0) return PHOTO_FALLBACK_SECONDS;
  const middle = lengths[Math.floor(lengths.length / 2)] ?? PHOTO_FALLBACK_SECONDS;
  return clamp(snap(middle), PHOTO_BOUNDS.min, PHOTO_BOUNDS.max);
}

/** A photo or a moment of the catalog as an element of the plan: the candidate's id, `origin: user` (the user chose it). */
export function elementFromCandidate(item: CatalogItem, plan: Plan): PlanElement {
  const isMoment = item.start !== undefined && item.end !== undefined;
  const element: PlanElement = {
    id: item.id, origin: "user", source: item.source, type: isMoment ? "video" : "photo", duration: 0, effects: [], score: item.score,
  };
  if (isMoment) {
    element.start = item.start;
    element.end = item.end;
    element.duration = round6((item.end ?? 0) - (item.start ?? 0));
  } else {
    element.duration = defaultPhotoSeconds(plan);
    element.duration_origin = "planner";
  }
  if (item.moment_type) element.moment_type = item.moment_type;
  if (item.category) element.category = item.category;
  if (item.description) element.description = item.description;
  if (item.captured_at) element.captured_at = item.captured_at;
  return element;
}

/** A moment the user made by clicking on the bar of a video. */
export function newMoment(plan: Plan, source: string, range: { start: number; end: number }): PlanElement {
  return {
    id: nextUserId(plan), origin: "user", source, type: "video", start: range.start, end: range.end,
    duration: round6(range.end - range.start), effects: [],
  };
}

/**
 * The elements to add for some candidates: a moment that overlaps one already in the plan, or one added just before it, is left
 * out (the rules do not let moments of a video overlap) and counted.
 */
export function selectAddable(plan: Plan, items: readonly CatalogItem[]): { elements: PlanElement[]; skipped: number } {
  const elements: PlanElement[] = [];
  let skipped = 0;
  const taken = [...plan.elements];
  for (const item of items) {
    if (taken.some((element) => element.id === item.id)) continue;
    const element = elementFromCandidate(item, plan);
    const clash = element.type === "video" && taken.some((other) => other.type === "video" && other.source === element.source
      && (element.start ?? 0) < (other.end ?? 0) - OVERLAP_SECONDS && (other.start ?? 0) < (element.end ?? 0) - OVERLAP_SECONDS);
    if (clash) { skipped += 1; continue; }
    elements.push(element);
    taken.push(element);
  }
  return { elements, skipped };
}

// --- settling the transitions -----------------------------------------------------------------------------------

type Span = [number, number];

function gapsOf(taken: readonly Span[], length: number): Span[] {
  const gaps: Span[] = [];
  let cursor = 0;
  for (const [start, end] of [...taken].sort((a, b) => a[0] - b[0])) {
    if (start > cursor + TOLERANCE) gaps.push([cursor, start]);
    cursor = Math.max(cursor, end);
  }
  if (length > cursor + TOLERANCE) gaps.push([cursor, length]);
  return gaps;
}

/** Puts an effect in the free space of its clip: unchanged if it already fits, shortened if it must, `null` if it cannot stay. */
function place(effect: Effect, length: number, taken: readonly Span[], minimum: number): Effect | null {
  const free = gapsOf(taken, length);
  const end = effect.offset + effect.duration;
  const holder = free.some(([start, stop]) => start - TOLERANCE <= effect.offset && end <= stop + TOLERANCE);
  const onEdge = effect.type === "fade_in" ? Math.abs(effect.offset) <= TOLERANCE
    : effect.type === "fade_out" ? Math.abs(end - length) <= TOLERANCE : true;
  if (holder && onEdge) return effect;

  let window: Span | undefined;
  if (effect.type === "fade_in") window = free.find(([start]) => start <= TOLERANCE);
  else if (effect.type === "fade_out") window = free.find(([, stop]) => stop >= length - TOLERANCE);
  else {
    const roomy = free.filter(([start, stop]) => stop - start >= minimum - TOLERANCE);
    window = roomy.find(([start, stop]) => start - TOLERANCE <= effect.offset && effect.offset <= stop + TOLERANCE)
      ?? [...roomy].sort((a, b) => Math.abs(a[0] - effect.offset) - Math.abs(b[0] - effect.offset))[0];
  }
  if (!window) return null;
  const duration = round6(Math.min(effect.duration, window[1] - window[0]));
  if (duration < minimum - TOLERANCE) return null;
  const offset = effect.type === "fade_in" ? 0
    : effect.type === "fade_out" ? round6(length - duration)
      : round6(clamp(effect.offset, window[0], window[1] - duration));
  return { ...effect, offset, duration };
}

const sameSeconds = (a: number | undefined, b: number | undefined): boolean => a !== undefined && b !== undefined && Math.abs(a - b) < 1e-6;

/**
 * After an edit of `previous` that gave `plan`: what the transitions need to stay valid and no more (a plan that is already
 * valid comes back as it is).
 * - a dissolve needs a clip before it and fits the free space of both clips; one the planner wrote whose clips changed
 *   becomes the user's (the planner's 25% limit belongs to the clips it chose);
 * - fades and dips stay inside their clip and do not overlap the dissolves or each other;
 * - an effect that is left shorter than the minimum is removed.
 */
export function settle(plan: Plan, previous: Plan, minimum: number = MIN_EFFECT_SECONDS): Plan {
  const was = new Map(previous.elements.map((element, index) => [element.id, { duration: element.duration, before: previous.elements[index - 1] }]));
  const resolved = new Map<string, Effect | null>();
  const dissolves: number[] = plan.elements.map(() => 0);

  plan.elements.forEach((element, index) => {
    const dissolve = element.effects.find((effect) => effect.type === "dissolve");
    if (!dissolve) return;
    const before = plan.elements[index - 1];
    if (!before) { resolved.set(dissolve.id, null); return; }
    const room = round6(Math.min(element.duration, before.duration - (dissolves[index - 1] ?? 0)));
    const fits = dissolve.duration <= room + TOLERANCE;
    const duration = fits ? dissolve.duration : room;
    if (!fits && duration < minimum - TOLERANCE) { resolved.set(dissolve.id, null); return; }
    const old = was.get(element.id);
    const sameClips = old !== undefined && old.before?.id === before.id && sameSeconds(old.duration, element.duration)
      && sameSeconds(old.before?.duration, before.duration);
    const origin = dissolve.origin === "planner" && (!sameClips || !fits) ? "user" : dissolve.origin;
    dissolves[index] = duration;
    const unchanged = duration === dissolve.duration && origin === dissolve.origin && dissolve.offset === 0;
    resolved.set(dissolve.id, unchanged ? dissolve : { ...dissolve, offset: 0, duration, origin });
  });

  plan.elements.forEach((element, index) => {
    const taken: Span[] = [];
    const own = dissolves[index] ?? 0;
    if (own > 0) taken.push([0, own]);
    const next = dissolves[index + 1] ?? 0;
    if (next > 0) taken.push([Math.max(0, element.duration - next), element.duration]);
    for (const kind of ["fade_in", "fade_out", "dip"] as const satisfies readonly EffectType[]) {
      for (const effect of element.effects.filter((entry) => entry.type === kind)) {
        const placed = place(effect, element.duration, taken, minimum);
        resolved.set(effect.id, placed);
        if (placed) taken.push([placed.offset, placed.offset + placed.duration]);
      }
    }
  });

  let changed = false;
  const elements = plan.elements.map((element) => {
    const effects = element.effects.flatMap((effect) => {
      const after = resolved.get(effect.id);
      if (after === undefined) return [effect];
      if (after !== effect) changed = true;
      return after ? [after] : [];
    });
    return effects.length === element.effects.length && effects.every((effect, index) => effect === element.effects[index]) ? element : { ...element, effects };
  });
  return changed ? { ...plan, elements } : plan;
}

/** How many effects an edit took away from the clips that stay in the plan. */
export function lostEffects(before: Plan, after: Plan): number {
  const kept = new Map(after.elements.map((element) => [element.id, new Set(element.effects.map((effect) => effect.id))]));
  let lost = 0;
  for (const element of before.elements) {
    const survivors = kept.get(element.id);
    if (!survivors) continue;
    for (const effect of element.effects) if (!survivors.has(effect.id)) lost += 1;
  }
  return lost;
}

/** The end of every edit: the transitions settled, and the destination times (the server's) dropped until it computes them again. */
export function conclude(plan: Plan, previous: Plan, minimum: number): Plan {
  const settled = settle(plan, previous, minimum);
  return { ...settled, elements: settled.elements.map(({ timestamp: _server, ...element }) => element) };
}

// --- the edits -------------------------------------------------------------------------------------------------------

/** Adds elements at a slot (0 to n, before the clip there; the end by default). Adding a hidden one brings it back. */
export function addElements(plan: Plan, additions: readonly PlanElement[], slot: number | null = null, minimum = MIN_EFFECT_SECONDS): Plan {
  const known = new Set(plan.elements.map((element) => element.id));
  const fresh = additions.filter((element, index) => !known.has(element.id) && additions.findIndex((other) => other.id === element.id) === index);
  if (fresh.length === 0) return plan;
  const at = slot === null ? plan.elements.length : clamp(slot, 0, plan.elements.length);
  const elements = [...plan.elements.slice(0, at), ...fresh, ...plan.elements.slice(at)];
  const excluded = plan.excluded.filter((entry) => !fresh.some((element) => covers(entry, element.source, element.start, element.end)));
  return conclude({ ...plan, elements, excluded }, plan, minimum);
}

export function removeElement(plan: Plan, id: string, minimum = MIN_EFFECT_SECONDS): Plan {
  if (!plan.elements.some((element) => element.id === id)) return plan;
  return conclude({ ...plan, elements: plan.elements.filter((element) => element.id !== id) }, plan, minimum);
}

/** Moves a clip to a slot (0 to n) counted in the current list: before the clip that is there now. */
export function moveElement(plan: Plan, id: string, slot: number, minimum = MIN_EFFECT_SECONDS): Plan {
  const from = plan.elements.findIndex((element) => element.id === id);
  const moved = plan.elements[from];
  if (!moved) return plan;
  const to = clamp(slot > from ? slot - 1 : slot, 0, plan.elements.length - 1);
  if (to === from) return plan;
  const rest = plan.elements.filter((element) => element.id !== id);
  return conclude({ ...plan, elements: [...rest.slice(0, to), moved, ...rest.slice(to)] }, plan, minimum);
}

/** The clip that can be joined to this one: the next, when it is a moment of the same video that starts where this one ends. */
export function mergeableWith(plan: Plan, id: string): PlanElement | null {
  const index = plan.elements.findIndex((element) => element.id === id);
  const first = plan.elements[index];
  const second = plan.elements[index + 1];
  if (!first || !second || first.type !== "video" || second.type !== "video" || first.source !== second.source) return null;
  if (first.end === undefined || second.start === undefined) return null;
  return Math.abs(first.end - second.start) <= TOLERANCE ? second : null;
}

/** The transitions a merge takes off the seam: the fade out of the first, the fade in and the dissolve of the second. */
export function seamEffects(plan: Plan, id: string): number {
  const second = mergeableWith(plan, id);
  const first = plan.elements.find((element) => element.id === id);
  if (!first || !second) return 0;
  return first.effects.filter((effect) => effect.type === "fade_out").length
    + second.effects.filter((effect) => effect.type === "fade_in" || effect.type === "dissolve").length;
}

/**
 * Joins a moment to the next one when their edges touch: one clip, with the first's id, from the start of the first to the end of
 * the second. The two moments are gone for good: their stretch goes into `excluded`, so they are not listed again and the planner
 * does not pick them again (a clip of the plan may lie over an exclusion; the rules do not look at it). The seam is no longer an edge, so what sat on it goes: the fade out of the first, and the fade in and the dissolve of
 * the second. The rest stays, the second's transitions moved by the length of the first.
 */
export function mergeElements(plan: Plan, id: string, minimum = MIN_EFFECT_SECONDS): Plan {
  const second = mergeableWith(plan, id);
  const index = plan.elements.findIndex((element) => element.id === id);
  const first = plan.elements[index];
  if (!first || !second || first.start === undefined || second.end === undefined) return plan;
  const later = second.effects
    .filter((effect) => effect.type === "fade_out" || effect.type === "dip")
    .map((effect) => ({ ...effect, offset: round6(effect.offset + first.duration) }));
  const effects = [...first.effects.filter((effect) => effect.type !== "fade_out"), ...later];
  const merged: PlanElement = {
    ...first,
    origin: first.origin === "user" || second.origin === "user" ? "user" : first.origin,
    end: second.end,
    duration: round6(second.end - first.start),
    effects,
  };
  const elements = plan.elements.flatMap((element, position) => (position === index ? [merged] : position === index + 1 ? [] : [element]));
  return withExclusion(conclude({ ...plan, elements }, plan, minimum), exclusionOf(first.source, first.start, second.end));
}

/** A photo hidden or a moment deleted: it leaves the timeline and the planner will not choose it again. */
export function excludeElement(plan: Plan, id: string, minimum = MIN_EFFECT_SECONDS): Plan {
  const element = plan.elements.find((entry) => entry.id === id);
  if (!element) return plan;
  const entry = exclusionOf(element.source, element.start, element.end);
  return withExclusion(removeElement(plan, id, minimum), entry);
}

export function excludeCandidate(plan: Plan, item: CatalogItem, minimum = MIN_EFFECT_SECONDS): Plan {
  const isMoment = item.start !== undefined && item.end !== undefined;
  const without = removeElement(plan, item.id, minimum);
  return withExclusion(without, exclusionOf(item.source, isMoment ? item.start : null, isMoment ? item.end : null));
}

/** Takes back the exclusions the predicate selects. */
export function restoreWhere(plan: Plan, selected: (exclusion: Exclusion) => boolean): Plan {
  const excluded = plan.excluded.filter((exclusion) => !selected(exclusion));
  return excluded.length === plan.excluded.length ? plan : { ...plan, excluded };
}

export const restoreCandidate = (plan: Plan, item: CatalogItem): Plan =>
  restoreWhere(plan, (exclusion) => covers(exclusion, item.source, item.start, item.end));

/** The duration of a photo, in the server's range and on the grid of a tenth of a second; it becomes the user's. */
export function setPhotoDuration(plan: Plan, id: string, seconds: number, bounds: Range = PHOTO_BOUNDS, minimum = MIN_EFFECT_SECONDS): Plan {
  const element = plan.elements.find((entry) => entry.id === id);
  if (!element || element.type !== "photo") return plan;
  const duration = clamp(snap(seconds, STEP), bounds.min, bounds.max);
  if (sameSeconds(duration, element.duration) && element.duration_origin === "user") return plan;
  const elements = plan.elements.map((entry) => (entry.id === id ? { ...entry, duration, duration_origin: "user" as const } : entry));
  return conclude({ ...plan, elements }, plan, minimum);
}

export interface TrimBounds {
  start: Range;
  end: Range;
  minLength: number;
}

/** One edge of a moment moved: kept inside its video, off the neighboring moments, at least the minimum long. */
export function setMomentEdge(plan: Plan, id: string, edge: "start" | "end", value: number, bounds: TrimBounds, minimum = MIN_EFFECT_SECONDS): Plan {
  const element = plan.elements.find((entry) => entry.id === id);
  if (!element || element.type !== "video" || element.start === undefined || element.end === undefined) return plan;
  const start = edge === "start" ? clamp(snap(value), bounds.start.min, Math.min(bounds.start.max, element.end - bounds.minLength)) : element.start;
  const end = edge === "end" ? clamp(snap(value), Math.max(bounds.end.min, element.start + bounds.minLength), bounds.end.max) : element.end;
  if (sameSeconds(start, element.start) && sameSeconds(end, element.end)) return plan;
  const elements = plan.elements.map((entry) => (entry.id === id ? { ...entry, start: round6(start), end: round6(end), duration: round6(end - start) } : entry));
  return conclude({ ...plan, elements }, plan, minimum);
}
