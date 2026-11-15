// What is selected in the editor: it decides what the Detail panel shows. Selection is not part of the plan.
import type { LibraryKind } from "./effectEdit";

export type Selection =
  | { kind: "timeline" }
  | { kind: "element"; id: string } // a clip of the plan (a photo or a moment of a video)
  | { kind: "candidate"; id: string } // a photo or a moment of the catalog that is not in the plan
  | { kind: "video"; media: string }
  | { kind: "library"; type: LibraryKind } // a transition of the library, or the hard cut
  | { kind: "effect"; element: string; id: string }
  | { kind: "track"; file: string }
  | { kind: "part"; track: number; part: number };

export const NO_SELECTION: Selection = { kind: "timeline" };

export function sameSelection(a: Selection, b: Selection): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
