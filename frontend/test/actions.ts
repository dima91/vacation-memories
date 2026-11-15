// Edit actions for rendering the components: they do nothing, or record what they were asked.
import type { EditActions } from "../src/components/editing";

const noop = () => undefined;

export const editable: EditActions = {
  readOnly: false, addCandidates: noop, addMoment: noop, move: noop, remove: noop, hide: noop, hideCandidate: noop, restore: noop,
  restoreMoments: noop, setDuration: noop, setEdge: noop, seal: noop, drop: noop,
};

export const readOnly: EditActions = { ...editable, readOnly: true };
