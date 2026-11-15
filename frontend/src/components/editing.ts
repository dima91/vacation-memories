// What the panels can ask of the editor (sections 11 and 12 of UI-SPEC.md). `EditorScreen` implements it on the plan on
// screen; the panels only call it, so they do not know how an edit is made, kept inside the limits or saved.
import type { CatalogItem } from "../api/types";
import type { DragPayload } from "../lib/dnd";

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
  restoreMoments(): void;
  setDuration(id: string, seconds: number): void;
  setEdge(id: string, edge: "start" | "end", value: number): void;
  /** Ends a gesture (a drag, a slider): what it changed is one step of the history. */
  seal(): void;
  /** Something dropped on the timeline at a slot. */
  drop(payload: DragPayload, slot: number): void;
}
