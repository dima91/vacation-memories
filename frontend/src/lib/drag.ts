// Geometry of the drags (sections 11 and 12 of UI-SPEC.md): from a movement of the pointer to a value inside the
// limits the server computed, and from a position on the lane to an insertion point. Pure functions without a DOM.
import { clamp, type ClipRect } from "./timeline";

export const STEP = 0.1; // seconds: the precision of the user's edits
export const BIG_STEP = 1; // Shift

const round6 = (value: number): number => Math.round(value * 1e6) / 1e6;

/** A value on the grid of `step` seconds (a tenth by default), free of floating-point noise. */
export const snap = (value: number, step: number = STEP): number => round6(Math.round(value / step) * step);

export interface Bounds {
  min: number;
  max: number;
}

/** The value a handle is dragged to: the start value moved by the pointer, snapped to the grid and kept inside the bounds. */
export function valueFromDrag(args: {
  start: number;
  deltaPx: number;
  pxPerUnit: number;
  sign?: 1 | -1; // -1 for a handle that grows the value when it moves left
  bounds: Bounds;
  step?: number;
}): number {
  const { start, deltaPx, pxPerUnit, bounds, step = STEP, sign = 1 } = args;
  const moved = pxPerUnit > 0 ? start + (sign * deltaPx) / pxPerUnit : start;
  return clamp(snap(moved, step), bounds.min, bounds.max);
}

/** The value after an arrow key: ±0.1 s, ±1 s with Shift; `null` for another key. */
export function valueFromKey(key: string, shift: boolean, current: number, bounds: Bounds, step: number = STEP): number | null {
  const direction = key === "ArrowRight" || key === "ArrowUp" ? 1 : key === "ArrowLeft" || key === "ArrowDown" ? -1 : 0;
  if (direction === 0) {
    if (key === "Home") return bounds.min;
    if (key === "End") return Number.isFinite(bounds.max) ? bounds.max : null;
    return null;
  }
  return clamp(snap(current + direction * (shift ? BIG_STEP : step), step), bounds.min, bounds.max);
}

/** Where a dragged clip or a dropped item goes: the slot (0 to n) before the first clip whose middle is past the pointer. */
export function insertionSlot(rects: readonly Pick<ClipRect, "x" | "width">[], x: number): number {
  const index = rects.findIndex((rect) => x < rect.x + rect.width / 2);
  return index === -1 ? rects.length : index;
}

/** The x of the insertion mark for a slot: the start of the clip after it, or the end of the last one. */
export function slotX(rects: readonly Pick<ClipRect, "x" | "width">[], slot: number): number {
  const after = rects[slot];
  if (after) return after.x;
  const last = rects[rects.length - 1];
  return last ? last.x + last.width : 0;
}

/** A slot that would leave a clip where it is (before itself or right after itself). */
export const isNoopSlot = (from: number, slot: number): boolean => slot === from || slot === from + 1;

/** A moment of `length` seconds around a point of a video's bar, inside the free stretch that holds the point. */
export function momentAround(args: {
  point: number;
  freeStart: number;
  freeEnd: number;
  length?: number;
  minLength?: number;
}): { start: number; end: number } | null {
  const { point, freeStart, freeEnd, length = 6, minLength = 1 } = args;
  const room = freeEnd - freeStart;
  if (room < minLength) return null;
  const wanted = Math.min(length, room);
  const start = clamp(snap(point - wanted / 2), freeStart, freeEnd - wanted);
  const begin = Math.max(freeStart, snap(start));
  const end = Math.min(freeEnd, round6(begin + wanted));
  return end - begin >= minLength - 1e-6 ? { start: round6(begin), end: round6(end) } : null;
}

/** The free stretches of a video that the moments in the timeline leave. */
export function freeStretches(moments: readonly { start: number; end: number }[], videoLength: number): Bounds[] {
  const sorted = [...moments].sort((a, b) => a.start - b.start);
  const free: Bounds[] = [];
  let cursor = 0;
  for (const moment of sorted) {
    if (moment.start > cursor) free.push({ min: cursor, max: moment.start });
    cursor = Math.max(cursor, moment.end);
  }
  if (videoLength > cursor) free.push({ min: cursor, max: videoLength });
  return free;
}
