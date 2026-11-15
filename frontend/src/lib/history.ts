// Undo and redo (section 12 of UI-SPEC.md): snapshots of the plan, 60 steps, kept on the client (the server keeps no
// history, it saves the plan at every confirmed change). Pure functions without a DOM (D11).
//
// A gesture (a drag, a slider) makes many changes and counts as one step: they share a `group`, and `seal` closes it.

export const HISTORY_LIMIT = 60;

export interface History<T> {
  readonly past: readonly T[];
  readonly present: T;
  readonly future: readonly T[];
  /** The gesture in progress: a change with the same group replaces the present instead of adding a step. */
  readonly group: string | null;
}

export const startHistory = <T>(present: T): History<T> => ({ past: [], present, future: [], group: null });

export const canUndo = (history: History<unknown>): boolean => history.past.length > 0;
export const canRedo = (history: History<unknown>): boolean => history.future.length > 0;

/** A new present. The same value is not a step; a change in the gesture already open does not add one. */
export function commit<T>(history: History<T>, next: T, group: string | null = null): History<T> {
  if (Object.is(next, history.present)) return history;
  if (group !== null && history.group === group) return { ...history, present: next, future: [] };
  return { past: [...history.past, history.present].slice(-HISTORY_LIMIT), present: next, future: [], group };
}

/**
 * Closes the gesture in progress. A gesture that ends where it started (a drag brought back to its place) leaves no step.
 */
export function seal<T>(history: History<T>): History<T> {
  if (history.group === null) return history;
  const previous = history.past[history.past.length - 1];
  if (previous !== undefined && JSON.stringify(previous) === JSON.stringify(history.present)) {
    return { ...history, past: history.past.slice(0, -1), present: previous, group: null };
  }
  return { ...history, group: null };
}

export function undo<T>(history: History<T>): History<T> {
  const open = seal(history);
  const previous = open.past[open.past.length - 1];
  if (previous === undefined) return open;
  return { past: open.past.slice(0, -1), present: previous, future: [open.present, ...open.future], group: null };
}

export function redo<T>(history: History<T>): History<T> {
  const open = seal(history);
  const next = open.future[0];
  if (next === undefined) return open;
  return { past: [...open.past, open.present].slice(-HISTORY_LIMIT), present: next, future: open.future.slice(1), group: null };
}

/** Replaces the present without a step: the server's normalized version of what was just saved. */
export const amend = <T>(history: History<T>, present: T): History<T> => ({ ...history, present });
