// What is being dragged (sections 11 and 12): a photo, a moment or a whole video from the Media panel, or a clip of the
// timeline. A browser hides the data of a drag until the drop, so the payload is also kept here while it lasts.

export type DragPayload =
  | { kind: "photo"; id: string }
  | { kind: "moment"; id: string }
  | { kind: "video"; media: string }
  | { kind: "clip"; id: string };

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
    if ((value.kind === "photo" || value.kind === "moment" || value.kind === "clip") && typeof value.id === "string") {
      return { kind: value.kind, id: value.id };
    }
  } catch {
    // not ours
  }
  return null;
}
