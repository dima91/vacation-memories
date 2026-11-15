// The user's edits of the transitions (sections 11 and 12 of UI-SPEC.md), as pure functions in the style of
// `planEdit.ts`: they take a plan and give a new one. The rules live in Python (`plan_rules.effect_room`) and the server
// checks again on every save; `roomFor` is the same geometry, written here so that an edit that follows another one
// (the "apply to the whole video" button, a drop after a drag) is placed in the space the first edit left, and a test
// compares it with the rooms the server sent for the fixture plan.
//
// Where an effect can sit (decision D1): `fade_in` and `dissolve` at the start of a clip, `fade_out` at its end,
// `dip` anywhere. A dissolve is stored once, in the incoming clip (D4). A boundary has one transition: putting a
// transition where another one is replaces it.
import type { Effect, EffectType, Plan, PlanElement } from "../api/types";
import { snap } from "./drag";
import { MIN_EFFECT_SECONDS, conclude, round6 } from "./planEdit";
import { clamp } from "./timeline";

export type Span = [number, number];
const TOLERANCE = 1e-3;

/** The library as the client knows it: the four effects the renderer makes, and the hard cut that removes the dissolves. */
export type LibraryKind = EffectType | "cut";

/** Seconds of a new transition by type: the library's defaults (the single configuration), with the specification's as a fallback. */
export type Defaults = Record<EffectType, number>;
export const FALLBACK_DEFAULTS: Defaults = { fade_in: 1, fade_out: 3, dissolve: 0.8, dip: 0.8 };

export function defaultsOf(library: readonly { type: EffectType; default_seconds: number }[]): Defaults {
  const defaults = { ...FALLBACK_DEFAULTS };
  for (const item of library) defaults[item.type] = item.default_seconds;
  return defaults;
}

// --- the free space of a clip ---------------------------------------------------------------------------------

/** The stretches of a clip the effects occupy, the dissolve into the next clip included (it covers the end of this one). */
function takenSpans(elements: readonly PlanElement[], index: number, ignore?: string): Span[] {
  const element = elements[index];
  if (!element) return [];
  const spans: Span[] = element.effects.filter((effect) => effect.id !== ignore).map((effect) => [effect.offset, effect.offset + effect.duration]);
  for (const effect of elements[index + 1]?.effects ?? []) {
    if (effect.type === "dissolve" && effect.id !== ignore) spans.push([Math.max(0, element.duration - effect.duration), element.duration]);
  }
  return spans.sort((a, b) => a[0] - b[0]);
}

function gaps(taken: readonly Span[], length: number): Span[] {
  const found: Span[] = [];
  let cursor = 0;
  for (const [start, end] of taken) {
    if (start > cursor) found.push([cursor, start]);
    cursor = Math.max(cursor, end);
  }
  if (length > cursor) found.push([cursor, length]);
  return found;
}

/**
 * Where an effect of `type` fits on clip `index` (`plan_rules.effect_room`): for `fade_in` and `dissolve` the window starts at the
 * clip's start and is also the longest they can last, `fade_out` ends at the clip's end, `dip` slides inside any window. `ignore` is the
 * id of the effect being moved or resized. Windows shorter than `minimum` are left out.
 */
export function roomFor(elements: readonly PlanElement[], index: number, type: EffectType, minimum: number = MIN_EFFECT_SECONDS, ignore?: string): Span[] {
  const element = elements[index];
  if (!element) return [];
  const taken = takenSpans(elements, index, ignore);
  let windows: Span[];
  if (type === "fade_in") windows = [[0, Math.min(element.duration, ...taken.map(([start]) => start))]];
  else if (type === "fade_out") windows = [[Math.max(0, ...taken.map(([, end]) => end)), element.duration]];
  else if (type === "dissolve") {
    const before = elements[index - 1];
    if (!before) return [];
    const head = Math.min(element.duration, ...taken.map(([start]) => start));
    const tail = before.duration - Math.max(0, ...takenSpans(elements, index - 1, ignore).map(([, end]) => end));
    windows = [[0, Math.min(head, tail)]];
  } else windows = gaps(taken, element.duration);
  return windows.map(([start, end]): Span => [round6(start), round6(end)]).filter(([start, end]) => end - start >= minimum - TOLERANCE);
}

// --- ids and lookups -------------------------------------------------------------------------------------------

/** `fx-<n>`: the next number after the effects in the plan (the planner's pattern). */
export function nextEffectId(plan: Plan): string {
  let highest = 0;
  for (const element of plan.elements) {
    for (const effect of element.effects) {
      const found = /^fx-(\d+)$/.exec(effect.id);
      if (found) highest = Math.max(highest, Number(found[1]));
    }
  }
  return `fx-${highest + 1}`;
}

export interface Located {
  index: number;
  element: PlanElement;
  effect: Effect;
}

export function locate(plan: Plan, id: string): Located | null {
  for (const [index, element] of plan.elements.entries()) {
    const effect = element.effects.find((entry) => entry.id === id);
    if (effect) return { index, element, effect };
  }
  return null;
}

const withEffects = (plan: Plan, change: (element: PlanElement, index: number) => PlanElement): Plan => ({
  ...plan,
  elements: plan.elements.map((element, index) => change(element, index)),
});

function withoutEffects(plan: Plan, ids: ReadonlySet<string>): Plan {
  if (ids.size === 0) return plan;
  return withEffects(plan, (element) => (element.effects.some((effect) => ids.has(effect.id))
    ? { ...element, effects: element.effects.filter((effect) => !ids.has(effect.id)) } : element));
}

/** The effects a new one of `type` on clip `index` takes the place of: a boundary has one transition. */
function occupants(plan: Plan, index: number, type: EffectType): Set<string> {
  const here = plan.elements[index];
  const found = new Set<string>();
  if (!here) return found;
  const collect = (element: PlanElement | undefined, kinds: readonly EffectType[]) => {
    for (const effect of element?.effects ?? []) if (kinds.includes(effect.type)) found.add(effect.id);
  };
  if (type === "fade_in") collect(here, ["fade_in", "dissolve"]);
  else if (type === "dissolve") { collect(here, ["fade_in", "dissolve"]); collect(plan.elements[index - 1], ["fade_out"]); }
  else if (type === "fade_out") { collect(here, ["fade_out"]); collect(plan.elements[index + 1], ["dissolve"]); }
  return found;
}

// --- adding ----------------------------------------------------------------------------------------------------------

const floorTenth = (value: number): number => Math.floor(value * 10 + 1e-6) / 10;

/** Where and how long a new effect is, in the windows the clip leaves: shortened to the room, on the grid of a tenth. */
function fit(type: EffectType, windows: readonly Span[], length: number, wanted: number, wish: number | undefined, minimum: number): { offset: number; duration: number } | null {
  if (windows.length === 0) return null;
  let window: Span | undefined = windows[0];
  if (type === "dip") {
    const point = wish ?? (length - wanted) / 2;
    const distance = ([start, end]: Span) => (point < start ? start - point : point > end - minimum ? point - (end - minimum) : 0);
    window = [...windows].sort((a, b) => distance(a) - distance(b))[0];
  }
  if (!window) return null;
  const room = window[1] - window[0];
  const asked = Math.max(wanted, minimum);
  const duration = asked <= room + TOLERANCE ? Math.min(asked, room) : Math.min(room, Math.max(minimum, floorTenth(room)));
  const offset = type === "dip" ? clamp(snap(wish ?? (length - duration) / 2), window[0], window[1] - duration)
    : type === "fade_out" ? length - duration : 0;
  return { offset: round6(offset), duration: round6(duration) };
}

export interface AddOptions {
  /** How long it is asked to be: shortened to the room the clip has. */
  duration: number;
  /** For a `dip`: where it starts, in seconds from the start of the clip (the middle by default). */
  offset?: number;
  minimum?: number;
  /** The id to keep, when an effect is being moved. */
  id?: string;
}

/** Puts an effect on a clip, in the room it leaves. `null` if there is none (nothing changes). Its origin is the user's. */
export function addEffect(plan: Plan, elementId: string, type: EffectType, options: AddOptions): Plan | null {
  const minimum = options.minimum ?? MIN_EFFECT_SECONDS;
  const index = plan.elements.findIndex((element) => element.id === elementId);
  if (index < 0) return null;
  const cleared = withoutEffects(plan, occupants(plan, index, type));
  const element = cleared.elements[index];
  if (!element) return null;
  const placed = fit(type, roomFor(cleared.elements, index, type, minimum), element.duration, options.duration, options.offset, minimum);
  if (!placed) return null;
  const effect: Effect = { id: options.id ?? nextEffectId(plan), type, offset: placed.offset, duration: placed.duration, origin: "user" };
  const next = withEffects(cleared, (entry, position) => (position === index ? { ...entry, effects: [...entry.effects, effect] } : entry));
  return conclude(next, plan, minimum);
}

/** The click alternatives of the drags (section 11): where on a clip a transition goes. */
export type Placement = "fade_in_start" | "dissolve_start" | "dissolve_end" | "fade_out_end" | "dip_here";

export const PLACEMENTS: readonly Placement[] = ["fade_in_start", "dissolve_start", "dissolve_end", "fade_out_end", "dip_here"];

export const placementType = (placement: Placement): EffectType =>
  placement === "fade_in_start" ? "fade_in" : placement === "fade_out_end" ? "fade_out" : placement === "dip_here" ? "dip" : "dissolve";

/** A transition put on a clip with one click; `at` (seconds from the clip's start) is where a dip goes, the middle by default. `null` without room. */
export function placeEffect(plan: Plan, elementId: string, placement: Placement, defaults: Defaults, at?: number, minimum: number = MIN_EFFECT_SECONDS): Plan | null {
  const index = plan.elements.findIndex((element) => element.id === elementId);
  const element = plan.elements[index];
  if (!element) return null;
  const type = placementType(placement);
  const host = placement === "dissolve_end" ? plan.elements[index + 1] : element;
  if (!host) return null;
  const duration = defaults[type];
  const offset = type === "dip" ? (at ?? element.duration / 2) - duration / 2 : undefined;
  return addEffect(plan, host.id, type, { duration, offset, minimum });
}

// --- removing --------------------------------------------------------------------------------------------------------

export function removeEffect(plan: Plan, id: string, minimum: number = MIN_EFFECT_SECONDS): Plan {
  if (!locate(plan, id)) return plan;
  return conclude(withoutEffects(plan, new Set([id])), plan, minimum);
}

/** The dissolve at the boundary nearest to a point of a clip: the clip's own if the point is in the first half, the next clip's otherwise. */
function dissolveNear(plan: Plan, elementId: string, at: number): Effect | null {
  const index = plan.elements.findIndex((element) => element.id === elementId);
  const element = plan.elements[index];
  if (!element) return null;
  const host = at < element.duration / 2 ? element : plan.elements[index + 1];
  return host?.effects.find((effect) => effect.type === "dissolve") ?? null;
}

/** "Hard cut" dropped on a clip: the dissolve at the nearest boundary goes (nothing changes without one). */
export function hardCutNear(plan: Plan, elementId: string, at: number, minimum: number = MIN_EFFECT_SECONDS): Plan {
  const dissolve = dissolveNear(plan, elementId, at);
  return dissolve ? removeEffect(plan, dissolve.id, minimum) : plan;
}

// --- moving and resizing ---------------------------------------------------------------------------------------------

/** Where an effect dropped on a clip goes: a dissolve at the boundary nearest to the drop, the others by their type (D1). */
export function hostFor(plan: Plan, type: EffectType, elementId: string, at: number): string | null {
  const index = plan.elements.findIndex((element) => element.id === elementId);
  const element = plan.elements[index];
  if (!element) return null;
  if (type === "dissolve") return (at < element.duration / 2 ? element : plan.elements[index + 1])?.id ?? null;
  return element.id;
}

/**
 * An effect moved onto a clip, at a point of it (seconds from its start): it snaps to the nearest allowed position. `null` when there is
 * no room there (nothing changes); the plan itself when the effect would end up where it is.
 */
export function moveEffect(plan: Plan, id: string, elementId: string, at: number, minimum: number = MIN_EFFECT_SECONDS): Plan | null {
  const found = locate(plan, id);
  if (!found) return null;
  const { effect } = found;
  const without = withoutEffects(plan, new Set([id]));
  const host = hostFor(without, effect.type, elementId, at);
  if (!host) return null;
  const next = addEffect(without, host, effect.type, { duration: effect.duration, offset: at - effect.duration / 2, minimum, id });
  if (!next) return null;
  const now = locate(next, id);
  const same = now && now.element.id === found.element.id && Math.abs(now.effect.offset - effect.offset) < 1e-6 && Math.abs(now.effect.duration - effect.duration) < 1e-6;
  return same ? plan : next;
}

/** The window of the clip that holds an effect, with itself left out of the way. */
function windowOf(plan: Plan, found: Located, minimum: number): Span | null {
  const { effect } = found;
  const end = effect.offset + effect.duration;
  return roomFor(plan.elements, found.index, effect.type, minimum, effect.id)
    .find(([start, stop]) => start - TOLERANCE <= effect.offset && end <= stop + TOLERANCE) ?? null;
}

function replaceEffect(plan: Plan, found: Located, offset: number, duration: number, minimum: number): Plan {
  const { effect } = found;
  if (Math.abs(offset - effect.offset) < 1e-6 && Math.abs(duration - effect.duration) < 1e-6) return plan;
  const changed: Effect = { ...effect, offset: round6(offset), duration: round6(duration), origin: "user" };
  const next = withEffects(plan, (element, index) => (index === found.index
    ? { ...element, effects: element.effects.map((entry) => (entry.id === effect.id ? changed : entry)) } : element));
  return conclude(next, plan, minimum);
}

/**
 * One edge of an effect moved (the handles): the value is a time of the clip. A `fade_in` and a `dissolve` begin at the edge of the clip,
 * a `fade_out` ends there, so those edges do not move; the other one keeps the effect inside its free window and at least `minimum` long.
 */
export function setEffectEdge(plan: Plan, id: string, edge: "start" | "end", value: number, minimum: number = MIN_EFFECT_SECONDS): Plan {
  const found = locate(plan, id);
  const window = found ? windowOf(plan, found, minimum) : null;
  if (!found || !window) return plan;
  const { effect, element } = found;
  const end = effect.offset + effect.duration;
  if (edge === "end") {
    if (effect.type === "fade_out") return plan;
    const next = clamp(snap(value), effect.offset + minimum, window[1]);
    return replaceEffect(plan, found, effect.offset, next - effect.offset, minimum);
  }
  if (effect.type === "fade_in" || effect.type === "dissolve") return plan;
  const limit = effect.type === "fade_out" ? element.duration : end;
  const start = clamp(snap(value), window[0], limit - minimum);
  return replaceEffect(plan, found, start, limit - start, minimum);
}

/** Where a `dip` starts (the "Starts at" slider and the arrows); the other effects begin at an edge of the clip. */
export function setEffectStart(plan: Plan, id: string, value: number, minimum: number = MIN_EFFECT_SECONDS): Plan {
  const found = locate(plan, id);
  const window = found ? windowOf(plan, found, minimum) : null;
  if (!found || !window || found.effect.type !== "dip") return plan;
  const offset = clamp(snap(value), window[0], window[1] - found.effect.duration);
  return replaceEffect(plan, found, offset, found.effect.duration, minimum);
}

/** How long an effect lasts (the "Duration" slider and the arrows): up to the free space, at least `minimum`. */
export function setEffectDuration(plan: Plan, id: string, value: number, minimum: number = MIN_EFFECT_SECONDS): Plan {
  const found = locate(plan, id);
  const window = found ? windowOf(plan, found, minimum) : null;
  if (!found || !window) return plan;
  const { effect, element } = found;
  if (effect.type === "fade_out") {
    const duration = clamp(snap(value), minimum, element.duration - window[0]);
    return replaceEffect(plan, found, element.duration - duration, duration, minimum);
  }
  const duration = clamp(snap(value), minimum, window[1] - effect.offset);
  return replaceEffect(plan, found, effect.offset, duration, minimum);
}

/** The free window of an effect on its clip, for the handles and the sliders: where it can start and end. */
export function boundsOf(plan: Plan, id: string, minimum: number = MIN_EFFECT_SECONDS): { start: number; end: number } | null {
  const found = locate(plan, id);
  const window = found ? windowOf(plan, found, minimum) : null;
  return window ? { start: window[0], end: window[1] } : null;
}

// --- the whole video -------------------------------------------------------------------------------------------------

export interface Applied {
  plan: Plan;
  /** Clips that got the transition. */
  applied: number;
  /** Clips that could not (no room), left as they were. */
  skipped: number;
}

/**
 * A library transition put wherever it makes sense on the whole video: a dissolve between all clips, "from black" on the first, "to black"
 * on the last, "hard cut" taking away every dissolve. A dip has no such place and changes nothing.
 */
export function applyToVideo(plan: Plan, kind: LibraryKind, defaults: Defaults, minimum: number = MIN_EFFECT_SECONDS): Applied {
  const count = plan.elements.length;
  if (kind === "cut") {
    const dissolves = new Set(plan.elements.flatMap((element) => element.effects.filter((effect) => effect.type === "dissolve").map((effect) => effect.id)));
    return { plan: dissolves.size === 0 ? plan : conclude(withoutEffects(plan, dissolves), plan, minimum), applied: dissolves.size, skipped: 0 };
  }
  if (kind === "dip" || count === 0) return { plan, applied: 0, skipped: 0 };
  const targets = kind === "fade_in" ? [0] : kind === "fade_out" ? [count - 1] : plan.elements.slice(1).map((_, index) => index + 1);
  let current = plan;
  let applied = 0;
  let skipped = 0;
  for (const target of targets) {
    const element = plan.elements[target];
    const next = element ? addEffect(current, element.id, kind, { duration: defaults[kind], minimum }) : null;
    if (next) { current = next; applied += 1; } else skipped += 1;
  }
  return { plan: current, applied, skipped };
}
