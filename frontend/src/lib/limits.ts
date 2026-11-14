// The server's limits (plan_rules) turned into what the interface shows and, later, uses while dragging. The rules
// live in Python; this module only reads what the server sent next to the plan.
import type { Effect, ElementLimits, EffectRoom, Limits, PlanElement, TrimLimits } from "../api/types";

export interface Range {
  min: number;
  max: number;
}

export function durationRange(limits: ElementLimits | undefined): (Range & { step: number }) | null {
  const duration: Limits | null | undefined = limits?.duration;
  return duration ? { min: duration.min, max: duration.max, step: duration.step } : null;
}

/** Where the start and the end of a moment can be: inside its video, apart from the neighboring moments. */
export function trimRange(limits: ElementLimits | undefined, element: PlanElement): { start: Range; end: Range; minLength: number } | null {
  const trim: TrimLimits | null | undefined = limits?.trim;
  if (!trim || element.start === undefined || element.end === undefined) return null;
  const endMax = trim.end_max ?? Number.POSITIVE_INFINITY;
  return {
    start: { min: trim.start_min, max: element.end - trim.min_length },
    end: { min: element.start + trim.min_length, max: endMax },
    minLength: trim.min_length,
  };
}

/** The stretch of the clip an effect can span: the free window that holds it. */
export function effectBounds(room: EffectRoom | undefined, effect: Pick<Effect, "offset" | "duration">): Range | null {
  if (!room) return null;
  const tolerance = 1e-6;
  const holder = room.windows.find(([start, end]) => start - tolerance <= effect.offset && effect.offset + effect.duration <= end + tolerance);
  return holder ? { min: holder[0], max: holder[1] } : null;
}

/** The longest free stretch (the "available space" of the Detail panel); 0 when the effect does not fit. */
export function largestWindow(room: EffectRoom | undefined): number {
  return (room?.windows ?? []).reduce((best, [start, end]) => Math.max(best, end - start), 0);
}
