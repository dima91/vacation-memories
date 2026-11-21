// Edit actions for rendering the components: they do nothing, or record what they were asked.
import type { EditActions } from "../src/components/editing";
import type { RenderControls } from "../src/components/JobProgress";

const noop = () => undefined;

export const editable: EditActions = {
  readOnly: false, addCandidates: noop, addMoment: noop, move: noop, remove: noop, hide: noop, hideCandidate: noop, restore: noop,
  setDuration: noop, setEdge: noop, seal: noop, drop: noop,
  placeTransition: noop, dropTransition: noop, applyTransition: noop, removeEffect: noop, setEffectEdge: noop, setEffectStart: noop,
  setEffectDuration: noop, addTrack: noop, addAllTracks: noop, moveTrack: noop, removeTrack: noop, removePart: noop, cutMusic: noop,
  setPartMuted: noop, setPartFade: noop, setShift: noop, restoreMusic: noop, dropMusic: noop,
};

export const readOnly: EditActions = { ...editable, readOnly: true };

export const rendering: RenderControls = { canStart: true, error: null, onPreview: noop, onExport: noop, onStop: noop };
