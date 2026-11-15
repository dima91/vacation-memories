// What is being dragged (sections 11 and 12): a photo, a moment or a whole video from the Media panel, or a clip of the
// timeline; a transition of the library or one on a clip; a track of the music root or one of the music lane. A browser
// hides the data of a drag until the drop, so the payload is also kept here while it lasts.
import type { LibraryKind } from "./effectEdit";

export type DragPayload =
  | { kind: "photo"; id: string }
  | { kind: "moment"; id: string }
  | { kind: "video"; media: string }
  | { kind: "clip"; id: string }
  | { kind: "transition"; type: LibraryKind } // from the library
  | { kind: "effect"; id: string } // a transition on a clip of the timeline
  | { kind: "track"; file: string } // from the music tab
  | { kind: "musicTrack"; index: number }; // a track of the music lane, being reordered

/** What lands on a clip (a transition) or on the music lane (a track), not on the lane of the clips. */
export const isTransitionDrag = (payload: DragPayload | null): payload is Extract<DragPayload, { kind: "transition" | "effect" }> =>
  payload?.kind === "transition" || payload?.kind === "effect";
export const isMusicDrag = (payload: DragPayload | null): payload is Extract<DragPayload, { kind: "track" | "musicTrack" }> =>
  payload?.kind === "track" || payload?.kind === "musicTrack";

const LIBRARY: readonly string[] = ["fade_in", "fade_out", "dissolve", "dip", "cut"];

export const DRAG_TYPE = "application/x-vacation-memories";

let current: DragPayload | null = null;

export const beginDrag = (payload: DragPayload): void => { current = payload; };
export const endDrag = (): void => { current = null; };
export const currentDrag = (): DragPayload | null => current;

export const encodePayload = (payload: DragPayload): string => JSON.stringify(payload);

export function decodePayload(text: string): DragPayload | null {
  try {
    const value = JSON.parse(text) as Record<string, unknown>;
    if (value.kind === "video" && typeof value.media === "string") return { kind: "video", media: value.media };
    if ((value.kind === "photo" || value.kind === "moment" || value.kind === "clip" || value.kind === "effect") && typeof value.id === "string") {
      return { kind: value.kind, id: value.id };
    }
    if (value.kind === "transition" && typeof value.type === "string" && LIBRARY.includes(value.type)) return { kind: "transition", type: value.type as LibraryKind };
    if (value.kind === "track" && typeof value.file === "string") return { kind: "track", file: value.file };
    if (value.kind === "musicTrack" && typeof value.index === "number") return { kind: "musicTrack", index: value.index };
  } catch {
    // not ours
  }
  return null;
}
