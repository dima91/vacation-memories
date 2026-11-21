// What the panels can ask of the editor (sections 11 and 12 of UI-SPEC.md). `EditorScreen` implements it on the plan on
// screen; the panels only call it, so they do not know how an edit is made, kept inside the limits or saved.
import type { CatalogItem } from "../api/types";
import type { DragPayload } from "../lib/dnd";
import type { LibraryKind, Placement } from "../lib/effectEdit";

export interface EditActions {
  /** The plan cannot be changed now: the project is in use elsewhere, or the analysis is running. */
  readOnly: boolean;
  addCandidates(items: readonly CatalogItem[], slot?: number | null): void;
  /**
   * A moment of about six seconds on the bar of a video, around `point` (seconds of the video) or, without one, in the first
   * free stretch. It needs a free second between the moments already in the timeline.
   */
  addMoment(source: string, videoLength: number, point: number | null): void;
  /** Moves a clip to a slot of the timeline (0 to n, before the clip there). */
  move(id: string, slot: number): void;
  /** Takes a clip out of the timeline; it stays available in the Media panel. */
  remove(id: string): void;
  /** Hides a photo or deletes a moment that is in the timeline: the planner will not choose it again. */
  hide(id: string): void;
  hideCandidate(item: CatalogItem): void;
  restore(item: CatalogItem): void;
  setDuration(id: string, seconds: number): void;
  setEdge(id: string, edge: "start" | "end", value: number): void;
  /** Ends a gesture (a drag, a slider): what it changed is one step of the history. */
  seal(): void;
  /** Something dropped on the timeline at a slot. */
  drop(payload: DragPayload, slot: number): void;

  // --- transitions (U6) ---
  /** A transition put on a clip with one click, where `placement` says; a dip goes at the playhead if it is on the clip. */
  placeTransition(elementId: string, placement: Placement): void;
  /** A transition of the library, or one of the timeline, dropped on a clip at a point of it (seconds from its start). */
  dropTransition(payload: Extract<DragPayload, { kind: "transition" | "effect" }>, elementId: string, at: number): void;
  /** The "Apply" button of a library transition: wherever it makes sense on the whole video. */
  applyTransition(kind: LibraryKind): void;
  removeEffect(id: string): void;
  /** One edge of an effect (the handles), as a time of its clip. */
  setEffectEdge(id: string, edge: "start" | "end", value: number): void;
  setEffectStart(id: string, value: number): void;
  setEffectDuration(id: string, value: number): void;

  // --- music (U6) ---
  /** A track of the music root added to the video, whole, at a slot of the music lane (the end by default). */
  addTrack(file: string, slot?: number | null): void;
  /** Every track of the music root that is not in the video yet. */
  addAllTracks(): void;
  moveTrack(index: number, slot: number): void;
  removeTrack(index: number): void;
  /** Deletes a part: the following ones shift, and a track left without parts leaves the video. */
  removePart(track: number, part: number): void;
  /** Cuts the part that plays at a time of the video in two. */
  cutMusic(time: number): void;
  setPartMuted(track: number, part: number, muted: boolean): void;
  setPartFade(track: number, part: number, which: "in" | "out", seconds: number): void;
  /** The shift of the whole music against the video. */
  setShift(seconds: number): void;
  /** Puts back the stretches of the tracks of the video that were deleted. */
  restoreMusic(): void;
  /** A track dropped on the music lane at a slot: from the Media panel it is added, from the lane it is moved. */
  dropMusic(payload: Extract<DragPayload, { kind: "track" | "musicTrack" }>, slot: number): void;
}
