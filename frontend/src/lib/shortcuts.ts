// The keyboard shortcuts of the editor (section 12): they work when the focus is not in a text field.

export type Command = "undo" | "redo" | "remove" | "play" | "clear" | "earlier" | "later";

export interface KeyLike {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

export interface Target {
  tag: string; // lower case
  type?: string; // of an input
  editable?: boolean;
  role?: string;
}

const TEXT_INPUTS = new Set(["text", "number", "search", "email", "url", "tel", "password", "date", "time", ""]);

/** A place where typing is meant for the field: text inputs, text areas, selects and editable content. */
export function isTextField(target: Target | null): boolean {
  if (!target) return false;
  if (target.editable || target.tag === "textarea" || target.tag === "select") return true;
  return target.tag === "input" && TEXT_INPUTS.has(target.type ?? "");
}

/** Space belongs to a focused control (it presses a button, moves a slider) and to a tab. */
function takesSpace(target: Target | null): boolean {
  return !!target && (isTextField(target) || ["button", "input", "a", "summary"].includes(target.tag) || target.role === "tab");
}

export function commandFor(event: KeyLike, target: Target | null): Command | null {
  if (isTextField(target)) return null;
  const control = event.ctrlKey || event.metaKey;
  if (control && !event.altKey) {
    const key = event.key.toLowerCase();
    if (key === "z") return event.shiftKey ? "redo" : "undo";
    if (key === "y" && !event.shiftKey) return "redo";
    return null;
  }
  if (control) return null;
  if (event.altKey && !event.shiftKey) {
    if (event.key === "ArrowLeft") return "earlier";
    if (event.key === "ArrowRight") return "later";
    return null;
  }
  if (event.altKey) return null;
  if (event.key === "Delete" || event.key === "Backspace") return target?.role === "separator" ? null : "remove"; // a handle is not the clip
  if (event.key === " " && !takesSpace(target)) return "play";
  if (event.key === "Escape") return "clear";
  return null;
}
