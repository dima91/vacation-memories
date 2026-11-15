// Geometry of the timeline (section 12 of UI-SPEC.md), pure functions without a DOM: the visible window, the
// zoom and the scroll, the position of the clips and of the music on the time axis.
import type { MusicPlan, PlanElement } from "../api/types";

export const MIN_WINDOW_SECONDS = 90;
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 6;
export const ZOOM_STEP = 0.5;

/** The seconds the visible window shows at zoom 1: at least 90, and the video plus 5 s, rounded up to 10. */
export function windowSeconds(total: number): number {
  return Math.max(MIN_WINDOW_SECONDS, Math.ceil((Math.max(total, 0) + 5) / 10) * 10);
}

/** Seconds between two ticks of the ruler: 10, or 20 for a long window. */
export function tickStep(window: number): number {
  return window > 150 ? 20 : 10;
}

export function tickTimes(window: number): number[] {
  const step = tickStep(window);
  const times: number[] = [];
  for (let time = 0; time <= window; time += step) times.push(time);
  return times;
}

export function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

export interface View {
  window: number; // seconds at zoom 1
  zoom: number; // 1 to 6
  width: number; // pixels of the visible part of the lanes
}

export const contentWidth = (view: View): number => view.width * view.zoom;
export const pixelsPerSecond = (view: View): number => (view.window > 0 ? contentWidth(view) / view.window : 0);
export const maxScroll = (view: View): number => Math.max(0, contentWidth(view) - view.width);

export function clampScroll(scroll: number, view: View): number {
  return Math.min(maxScroll(view), Math.max(0, scroll));
}

/** A position in pixels from the start of the content (the scroll is not subtracted). */
export const timeToX = (time: number, view: View): number => time * pixelsPerSecond(view);
export const xToTime = (x: number, view: View): number => (pixelsPerSecond(view) > 0 ? x / pixelsPerSecond(view) : 0);

/** The time under a pointer, from its distance to the left edge of the lanes. */
export function timeAtPointer(offsetX: number, scroll: number, view: View, total: number): number {
  return clamp(xToTime(offsetX + scroll, view), 0, total);
}

/** The scroll that keeps `anchorTime` at the same pixel of the viewport when the zoom changes. */
export function scrollAfterZoom(scroll: number, view: View, newZoom: number, anchorTime: number): number {
  const next: View = { ...view, zoom: clampZoom(newZoom) };
  const anchorPixel = timeToX(anchorTime, view) - scroll;
  return clampScroll(timeToX(anchorTime, next) - anchorPixel, next);
}

/** Scrolls by a fraction of the viewport (the ‹ › buttons). */
export function scrollByPage(scroll: number, view: View, direction: -1 | 1, fraction = 0.5): number {
  return clampScroll(scroll + direction * view.width * fraction, view);
}

/** Brings a time into the viewport, scrolling the least that is needed; unchanged if it is already visible. */
export function scrollToReveal(scroll: number, view: View, time: number, margin = 24): number {
  const x = timeToX(time, view);
  if (x < scroll + margin) return clampScroll(x - margin, view);
  if (x > scroll + view.width - margin) return clampScroll(x - view.width + margin, view);
  return scroll;
}

/** The playhead after an arrow key: ±1 s, ±5 s with Shift, inside the video. */
export function stepPlayhead(current: number, direction: -1 | 1, shift: boolean, total: number): number {
  return round1(clamp(current + direction * (shift ? 5 : 1), 0, total));
}

// --- clips ---

export interface ClipRect {
  id: string;
  index: number;
  start: number;
  duration: number;
  x: number;
  width: number;
}

/**
 * Where a clip starts when the server has not said (an edit dropped its `timestamp`): where the previous one ends, less the dissolve
 * that overlaps them (`layout_timeline`).
 */
function startAfter(element: PlanElement, cursor: number): number {
  if (element.timestamp !== undefined) return element.timestamp;
  const dissolve = element.effects.find((effect) => effect.type === "dissolve")?.duration ?? 0;
  return Math.max(0, cursor - dissolve);
}

/** Where every clip sits on the time axis: the server's `timestamp`, or the end of the previous clip. */
export function clipRects(elements: readonly PlanElement[], view: View): ClipRect[] {
  const rects: ClipRect[] = [];
  let cursor = 0;
  elements.forEach((element, index) => {
    const start = startAfter(element, cursor);
    rects.push({ id: element.id, index, start, duration: element.duration, x: timeToX(start, view),
      width: element.duration * pixelsPerSecond(view) });
    cursor = start + element.duration;
  });
  return rects;
}

/** The clip that is playing at a time (the later one in a dissolve, where two overlap). */
export function clipAt(elements: readonly PlanElement[], time: number): PlanElement | null {
  let found: PlanElement | null = null;
  let cursor = 0;
  for (const element of elements) {
    const start = startAfter(element, cursor);
    if (time >= start && time < start + element.duration) found = element;
    cursor = start + element.duration;
  }
  return found;
}

export function clipStart(elements: readonly PlanElement[], id: string): number | null {
  let cursor = 0;
  for (const element of elements) {
    const start = startAfter(element, cursor);
    if (element.id === id) return start;
    cursor = start + element.duration;
  }
  return null;
}

// --- music ---

export interface MusicSlot {
  track: number;
  part: number;
  file: string;
  start: number; // on the video's time axis, after the shift: it may be negative
  end: number;
  trackStart: number; // the stretch of the track the part plays
  trackEnd: number;
  muted: boolean;
}

/** The parts of the tracks laid out in sequence on the time axis, shifted by `offset` (section 12, "Music"). */
export function musicSlots(music: MusicPlan | undefined): MusicSlot[] {
  if (!music) return [];
  const slots: MusicSlot[] = [];
  let cursor = music.offset;
  music.tracks.forEach((track, trackIndex) => {
    track.parts.forEach((part, partIndex) => {
      const length = part.end - part.start;
      slots.push({ track: trackIndex, part: partIndex, file: track.file, start: cursor, end: cursor + length,
        trackStart: part.start, trackEnd: part.end, muted: part.muted });
      cursor += length;
    });
  });
  return slots;
}

/** The peaks of a stretch of a track, from the peaks of the whole of it. */
export function peaksBetween(peaks: readonly number[], trackSeconds: number, start: number, end: number): number[] {
  if (peaks.length === 0 || trackSeconds <= 0) return [];
  const per = trackSeconds / peaks.length;
  const first = clamp(Math.floor(start / per), 0, peaks.length);
  const last = clamp(Math.ceil(end / per), first, peaks.length);
  return peaks.slice(first, last);
}

// --- small helpers ---

export const clamp = (value: number, low: number, high: number): number => Math.min(high, Math.max(low, value));
export const round1 = (value: number): number => Math.round(value * 10) / 10;
