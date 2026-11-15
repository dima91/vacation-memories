// The editing session (sections 4, 12 and 14 of UI-SPEC.md): the plan on screen with its undo history, and what is
// known of what the server holds. The plan is saved at every confirmed change; if it changed elsewhere the user chooses
// between reloading and overwriting. A pure reducer without a DOM (D11): `EditorScreen` runs the requests.
import type { Plan, PlanDocument } from "../api/types";
import { amend, canRedo, canUndo, commit, redo, seal, startHistory, undo, type History } from "./history";

export interface Failure {
  code: string;
  message: string;
  field: string | null;
}

export interface Session {
  /** The last answer of the server: its revision, limits and duration, and the plan it holds. */
  readonly document: PlanDocument;
  /** The plan on screen, with the steps to undo and redo. */
  readonly history: History<Plan>;
  /** Counts the changes of the plan on screen; `saved` is the one the server has. */
  readonly version: number;
  readonly saved: number;
  /** The version being sent, while a request is out. */
  readonly sending: number | null;
  /** The plan changed elsewhere: waiting for the user to reload or overwrite. */
  readonly conflict: boolean;
  /** The user chose to overwrite: the next save does not check the revision. */
  readonly overwrite: boolean;
  /** The last save failed for another reason: edits are kept, saving waits for a retry. */
  readonly failure: Failure | null;
}

export function startSession(document: PlanDocument): Session {
  return { document, history: startHistory(document.plan), version: 0, saved: 0, sending: null, conflict: false, overwrite: false, failure: null };
}

export type SessionAction =
  | { type: "edit"; plan: Plan; group?: string }
  | { type: "seal" }
  | { type: "undo" }
  | { type: "redo" }
  | { type: "sending" }
  | { type: "saved"; document: PlanDocument; version: number }
  | { type: "refused"; failure: Failure }
  | { type: "conflict" }
  | { type: "failed"; failure: Failure }
  | { type: "overwrite" }
  | { type: "retry" }
  | { type: "reload"; document: PlanDocument };

const changed = (session: Session, history: History<Plan>): Session =>
  history.present === session.history.present ? { ...session, history } : { ...session, history, version: session.version + 1 };

export function sessionReducer(session: Session, action: SessionAction): Session {
  switch (action.type) {
    case "edit":
      return changed(session, commit(session.history, action.plan, action.group ?? null));
    case "seal":
      return changed(session, seal(session.history));
    case "undo":
      return changed(session, undo(session.history));
    case "redo":
      return changed(session, redo(session.history));
    case "sending":
      return { ...session, sending: session.version };
    case "saved": {
      // The server's version replaces the one on screen only if nothing was changed since the request left.
      const upToDate = action.version === session.version;
      const history = upToDate ? amend(session.history, action.document.plan) : session.history;
      return { ...session, document: action.document, history, saved: action.version, sending: null, conflict: false, overwrite: false, failure: null };
    }
    case "refused":
      // The server did not accept the plan: back to the one it holds, with the reason.
      return { ...startSession(session.document), version: session.version, saved: session.version, failure: action.failure };
    case "conflict":
      return { ...session, sending: null, conflict: true, overwrite: false };
    case "failed":
      return { ...session, sending: null, failure: action.failure };
    case "overwrite":
      return { ...session, conflict: false, overwrite: true, failure: null };
    case "retry":
      return { ...session, failure: null };
    case "reload":
      return startSession(action.document);
  }
}

export const isDirty = (session: Session): boolean => session.version !== session.saved;
export const canSave = (session: Session): boolean => isDirty(session) && session.sending === null && !session.conflict && session.failure === null;
export const canUndoSession = (session: Session): boolean => canUndo(session.history);
export const canRedoSession = (session: Session): boolean => canRedo(session.history);

/** What the panels show: the server's document (limits, refs, duration) with the plan on screen. */
export const shown = (session: Session): PlanDocument =>
  session.history.present === session.document.plan ? session.document : { ...session.document, plan: session.history.present };

export type SaveState = "saved" | "saving" | "unsaved" | "conflict" | "failed";

export function saveState(session: Session): SaveState {
  if (session.conflict) return "conflict";
  if (session.failure) return "failed";
  if (session.sending !== null) return "saving";
  return isDirty(session) ? "unsaved" : "saved";
}
