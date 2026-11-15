// The user's edits of the music (section 12 of UI-SPEC.md, "Music" lane), as pure functions in the style of `planEdit.ts`.
// The tracks lie one after the other on the video's time axis, cut into parts; deleting a part shifts the following ones;
// the shift of the whole music is `music.offset`. The rules live in Python (`plan_rules`: the shift keeps a second of music in
// the video, a part lasts at least a second, its fades fit in it) and the server checks again on every save.
// `offsetBounds` is `music_offset_limits` written here so that the shift stays valid right after a structural edit, before the
// server has sent its limits again; a test compares it with the limits of the fixture.
import type { MusicPart, MusicPlan, MusicTrack, Plan } from "../api/types";
import { snap } from "./drag";
import { round6 } from "./planEdit";
import { clamp, musicSlots } from "./timeline";

/** The server's value arrives with the limits (`limits.music.min_part`); this is the specification's, for a plan without a music block. */
export const MIN_PART_SECONDS = 1;
const MIN_AUDIBLE_SECONDS = 1;
const TOLERANCE = 1e-3;

const EMPTY: MusicPlan = { tracks: [], offset: 0 };

export const musicOf = (plan: Plan): MusicPlan => plan.music ?? EMPTY;
const withMusic = (plan: Plan, music: MusicPlan): Plan => ({ ...plan, music });

export const partLength = (part: Pick<MusicPart, "start" | "end">): number => part.end - part.start;
export const musicLength = (music: MusicPlan): number => music.tracks.reduce((sum, track) => sum + track.parts.reduce((inner, part) => inner + partLength(part), 0), 0);

// --- the shift --------------------------------------------------------------------------------------------------------

export interface Bounds {
  min: number;
  max: number;
}

/** How far the music can be shifted: at least a second of it stays in the video, from either end. */
export function offsetBounds(music: MusicPlan, videoSeconds: number): Bounds {
  return { min: -Math.max(0, musicLength(music) - MIN_AUDIBLE_SECONDS), max: Math.max(0, videoSeconds - MIN_AUDIBLE_SECONDS) };
}

/** The shift after a structural edit: kept inside the bounds the new music has. */
function fitOffset(plan: Plan, videoSeconds: number): Plan {
  const music = plan.music;
  if (!music) return plan;
  const bounds = offsetBounds(music, videoSeconds);
  const offset = clamp(music.offset, bounds.min, bounds.max);
  return offset === music.offset ? plan : withMusic(plan, { ...music, offset: round6(offset) });
}

/** The shift of the music, on the grid of a tenth of a second and inside the bounds. */
export function setOffset(plan: Plan, seconds: number, bounds: Bounds): Plan {
  const music = musicOf(plan);
  const offset = round6(clamp(snap(seconds), bounds.min, bounds.max));
  if (Math.abs(offset - music.offset) < 1e-6 && plan.music) return plan;
  return withMusic(plan, { ...music, offset });
}

// --- tracks -----------------------------------------------------------------------------------------------------------

const whole = (length: number): MusicPart => ({ start: 0, end: round6(length), muted: false, fade_in: 0, fade_out: 0 });

/** A track of the music root added to the video, whole, at a slot (0 to n, before the track there; the end by default). */
export function addTrack(plan: Plan, file: string, length: number, slot: number | null, videoSeconds: number): Plan {
  const music = musicOf(plan);
  const at = slot === null ? music.tracks.length : clamp(slot, 0, music.tracks.length);
  const track: MusicTrack = { file, parts: [whole(length)] };
  const tracks = [...music.tracks.slice(0, at), track, ...music.tracks.slice(at)];
  return fitOffset(withMusic(plan, { ...music, tracks }), videoSeconds);
}

/** Moves a track to a slot counted in the current list (before the track that is there now), as `moveElement` does for a clip. */
export function moveTrack(plan: Plan, from: number, slot: number): Plan {
  const music = musicOf(plan);
  const moved = music.tracks[from];
  if (!moved) return plan;
  const to = clamp(slot > from ? slot - 1 : slot, 0, music.tracks.length - 1);
  if (to === from) return plan;
  const rest = music.tracks.filter((_, index) => index !== from);
  return withMusic(plan, { ...music, tracks: [...rest.slice(0, to), moved, ...rest.slice(to)] });
}

export function removeTrack(plan: Plan, track: number, videoSeconds: number): Plan {
  const music = musicOf(plan);
  if (!music.tracks[track]) return plan;
  return fitOffset(withMusic(plan, { ...music, tracks: music.tracks.filter((_, index) => index !== track) }), videoSeconds);
}

/** Deletes a part: the following ones shift to fill its place, and a track left without parts goes away (`plan_rules.remove_part`). */
export function removePart(plan: Plan, track: number, part: number, videoSeconds: number): Plan {
  const music = musicOf(plan);
  const current = music.tracks[track];
  if (!current?.parts[part]) return plan;
  const parts = current.parts.filter((_, index) => index !== part);
  const tracks = parts.length > 0 ? music.tracks.map((entry, index) => (index === track ? { ...entry, parts } : entry)) : music.tracks.filter((_, index) => index !== track);
  return fitOffset(withMusic(plan, { ...music, tracks }), videoSeconds);
}

// --- parts -------------------------------------------------------------------------------------------------------------

function replacePart(plan: Plan, track: number, part: number, change: (part: MusicPart) => MusicPart): Plan {
  const music = musicOf(plan);
  const current = music.tracks[track]?.parts[part];
  if (!current) return plan;
  const next = change(current);
  if (JSON.stringify(next) === JSON.stringify(current)) return plan;
  const tracks = music.tracks.map((entry, index) => (index === track ? { ...entry, parts: entry.parts.map((item, position) => (position === part ? next : item)) } : entry));
  return withMusic(plan, { ...music, tracks });
}

export const setPartMuted = (plan: Plan, track: number, part: number, muted: boolean): Plan => replacePart(plan, track, part, (item) => ({ ...item, muted }));

/** A fade of a part, on the grid of a tenth: between nothing and what is left of the part after the other fade. */
export function setPartFade(plan: Plan, track: number, part: number, which: "in" | "out", seconds: number): Plan {
  return replacePart(plan, track, part, (item) => {
    const other = which === "in" ? item.fade_out : item.fade_in;
    const room = Math.max(0, partLength(item) - other);
    const value = round6(clamp(snap(seconds), 0, Math.floor(room * 10 + 1e-6) / 10));
    return which === "in" ? { ...item, fade_in: value } : { ...item, fade_out: value };
  });
}

/** The longest a fade of a part can be, for the slider. */
export function fadeRoom(part: MusicPart, which: "in" | "out"): number {
  return Math.floor(Math.max(0, partLength(part) - (which === "in" ? part.fade_out : part.fade_in)) * 10 + 1e-6) / 10;
}

export interface Cut {
  plan: Plan;
  /** The part that starts at the cut. */
  track: number;
  part: number;
}

/**
 * Cuts the part that plays at a time of the video in two (section 12: "cut at the playhead"). The fade-in stays with the first
 * piece and the fade-out with the second; both last at least `minimum`. `null` where there is no music or too little of a part.
 */
export function cutAt(plan: Plan, time: number, minimum: number = MIN_PART_SECONDS): Cut | null {
  const slot = musicSlots(plan.music).find((entry) => time >= entry.start && time < entry.end);
  const part = slot ? plan.music?.tracks[slot.track]?.parts[slot.part] : undefined;
  if (!slot || !part) return null;
  const at = round6(snap(part.start + (time - slot.start)));
  if (at - part.start < minimum - TOLERANCE || part.end - at < minimum - TOLERANCE) return null;
  const first: MusicPart = { ...part, end: at, fade_out: 0, fade_in: Math.min(part.fade_in, at - part.start) };
  const second: MusicPart = { ...part, start: at, fade_in: 0, fade_out: Math.min(part.fade_out, part.end - at) };
  const music = musicOf(plan);
  const tracks = music.tracks.map((entry, index) => (index === slot.track ? { ...entry, parts: entry.parts.flatMap((item, position) => (position === slot.part ? [first, second] : [item])) } : entry));
  return { plan: withMusic(plan, { ...music, tracks }), track: slot.track, part: slot.part + 1 };
}

// --- what was deleted ---------------------------------------------------------------------------------------------------

/** The stretches of a track the parts leave out: what was deleted (or never in the video). Only the ones of at least `minimum`. */
export function deletedStretches(track: MusicTrack, length: number, minimum: number = MIN_PART_SECONDS): { start: number; end: number }[] {
  const stretches: { start: number; end: number }[] = [];
  let cursor = 0;
  for (const part of track.parts) {
    if (part.start - cursor >= minimum - TOLERANCE) stretches.push({ start: round6(cursor), end: part.start });
    cursor = Math.max(cursor, part.end);
  }
  if (length - cursor >= minimum - TOLERANCE) stretches.push({ start: round6(cursor), end: round6(length) });
  return stretches;
}

/** How many stretches `restoreDeleted` would bring back, for the button. */
export function countDeleted(music: MusicPlan | undefined, lengths: ReadonlyMap<string, number | undefined>, minimum: number = MIN_PART_SECONDS): number {
  return (music?.tracks ?? []).reduce((sum, track) => {
    const length = lengths.get(track.file);
    return sum + (length === undefined ? 0 : deletedStretches(track, length, minimum).length);
  }, 0);
}

/** Puts back, as parts of their own, the stretches of the tracks of the video that were deleted; every part stays in the order of its track. */
export function restoreDeleted(plan: Plan, lengths: ReadonlyMap<string, number | undefined>, minimum: number, videoSeconds: number): Plan {
  const music = musicOf(plan);
  let changed = false;
  const tracks = music.tracks.map((track) => {
    const length = lengths.get(track.file);
    const stretches = length === undefined ? [] : deletedStretches(track, length, minimum);
    if (stretches.length === 0) return track;
    changed = true;
    const parts = [...track.parts, ...stretches.map((stretch): MusicPart => ({ ...stretch, muted: false, fade_in: 0, fade_out: 0 }))].sort((a, b) => a.start - b.start);
    return { ...track, parts };
  });
  return changed ? fitOffset(withMusic(plan, { ...music, tracks }), videoSeconds) : plan;
}

/** Where every track sits on the video's time axis, in seconds (shift included): the places a dragged track is dropped between. */
export function trackExtents(music: MusicPlan | undefined): { start: number; end: number }[] {
  const slots = musicSlots(music);
  return (music?.tracks ?? []).map((_, index) => {
    const mine = slots.filter((slot) => slot.track === index);
    const start = Math.min(...mine.map((slot) => slot.start));
    const end = Math.max(...mine.map((slot) => slot.end));
    return { start, end };
  });
}
