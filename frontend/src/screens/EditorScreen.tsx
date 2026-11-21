// The editor (section 10): Media, Detail and the timeline on the project's real data, with the preview playing, and
// the editing (U5, U6): the plan on screen changes with the user's edits (clips, transitions, music), the history undoes them
// and every confirmed change is saved with the revision that was read (sections 4, 12 and 14).
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ApiFailure, api } from "../api/client";
import type { Catalog, CatalogItem, Plan, PlanDocument, ProjectState, Waveform } from "../api/types";
import { DetailPanel } from "../components/DetailPanel";
import type { EditActions } from "../components/editing";
import { ExportDialog } from "../components/ExportDialog";
import type { RenderControls } from "../components/JobProgress";
import { MediaPanel, type MediaTab } from "../components/MediaPanel";
import { Timeline } from "../components/Timeline";
import { TopBar } from "../components/TopBar";
import { ErrorNotice, Spinner, Toast } from "../components/ui";
import { errorText, useI18n, type MessageKey } from "../i18n";
import { withPlan } from "../lib/catalog";
import type { DragPayload } from "../lib/dnd";
import { freeStretches, momentAround } from "../lib/drag";
import {
  addEffect, applyToVideo, defaultsOf, hardCutNear, hostFor, locate, moveEffect, nextEffectId, placeEffect, removeEffect, setEffectDuration,
  setEffectEdge, setEffectStart,
} from "../lib/effectEdit";
import { durationRange, trimRange } from "../lib/limits";
import {
  MIN_PART_SECONDS, addTrack, cutAt, moveTrack, offsetBounds, removePart, removeTrack, restoreDeleted, setOffset, setPartFade, setPartMuted,
} from "../lib/musicEdit";
import {
  addElements, excludeCandidate, excludeElement, lostEffects, moveElement, newMoment, removeElement, restoreCandidate,
  selectAddable, setMomentEdge, setPhotoDuration,
} from "../lib/planEdit";
import { canRender, isRunning } from "../lib/render";
import { NO_SELECTION, type Selection } from "../lib/selection";
import {
  canRedoSession, canSave, canUndoSession, isDirty, saveState, sessionReducer, shown, startSession, type Session,
} from "../lib/session";
import { commandFor, type Target } from "../lib/shortcuts";
import { clipStart } from "../lib/timeline";
import { useJobStatus } from "./ProgressScreen";

interface Props {
  project: ProjectState;
  onProjectChanged: () => void;
  onChangeFolder: () => void;
  onSettings: () => void;
}

const SAVE_DELAY_MS = 350;
const NOTICE_MS = 7000;

function describe(target: EventTarget | null): Target | null {
  if (!(target instanceof HTMLElement)) return null;
  return { tag: target.tagName.toLowerCase(), type: (target as HTMLInputElement).type, editable: target.isContentEditable, role: target.getAttribute("role") ?? undefined };
}

interface Loaded {
  document: PlanDocument;
  catalog: Catalog;
  /** Changes with every load: a new load starts a new session. */
  load: number;
}

export function EditorScreen({ project, onProjectChanged, onChangeFolder, onSettings }: Props) {
  const i18n = useI18n();
  const { t } = i18n;
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [reloads, setReloads] = useState(0);
  /** The revision this page holds: the plan of the project changing to another one means it changed elsewhere. */
  const known = useRef<string | null>(null);
  const forced = useRef(false);
  const count = useRef(0);

  useEffect(() => {
    if (!forced.current && known.current !== null && project.plan.revision === known.current) return undefined;
    forced.current = false;
    let alive = true;
    setLoading(true);
    Promise.all([api.plan(project.id), api.catalog(project.id)]).then(([document, catalog]) => {
      if (!alive) return;
      known.current = document.revision;
      count.current += 1;
      setLoaded({ document, catalog, load: count.current });
      setError(null);
    }, (failure) => alive && setError(failure)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [project.id, project.plan.revision, reloads]);

  const reload = useCallback(() => { forced.current = true; setReloads((value) => value + 1); }, []);
  const saved = useCallback((revision: string) => { known.current = revision; onProjectChanged(); }, [onProjectChanged]);

  const noPlan = error instanceof ApiFailure && error.code === "no_plan";
  const failed = !loaded && !loading && error;
  const incomplete = project.analysis.present && !project.analysis.complete;

  if (loaded) {
    return <Workspace key={loaded.load} project={project} initial={loaded.document} catalog={loaded.catalog} incomplete={incomplete}
      onSaved={saved} onJobDone={onProjectChanged} onReload={reload} onChangeFolder={onChangeFolder} onSettings={onSettings} />;
  }
  return (
    <div className="editor">
      <TopBar project={project} catalog={null} requested={null} working={false} onChangeFolder={onChangeFolder} onSettings={onSettings} />
      <main className="editor__main" id="content">
        {loading ? <Spinner label={t("editor.loading")} /> : null}
        {failed ? (
          <div className="screen">
            {noPlan ? <p className="notice notice--info">{t("editor.noPlan")}</p>
              : error instanceof ApiFailure && error.code === "invalid_plan"
                ? <p className="notice notice--error" role="alert">{t("editor.invalidPlan", { error: error.message })}</p>
                : <ErrorNotice error={error} />}
            <div className="actions"><button type="button" className="btn" onClick={onSettings}>{t("top.settings")}</button></div>
          </div>
        ) : null}
      </main>
    </div>
  );
}

export interface WorkspaceProps {
  project: ProjectState;
  initial: PlanDocument;
  catalog: Catalog;
  incomplete: boolean;
  onSaved: (revision: string) => void;
  /** A job (preview or export) finished: the project state has to be read again. */
  onJobDone: () => void;
  onReload: () => void;
  onChangeFolder: () => void;
  onSettings: () => void;
}

interface Notice {
  id: number;
  text: string;
  undoable: boolean;
  /** The version of the plan the notice is about: any further change closes it. */
  version: number;
}

export function Workspace({ project, initial, catalog, incomplete, onSaved, onJobDone, onReload, onChangeFolder, onSettings }: WorkspaceProps) {
  const i18n = useI18n();
  const { t, plural } = i18n;
  const [session, dispatch] = useReducer(sessionReducer, initial, startSession);
  const [selection, setSelection] = useState<Selection>(NO_SELECTION);
  const [tab, setTab] = useState<MediaTab>("photos");
  const [playhead, setPlayhead] = useState(0);
  const [seekVersion, setSeekVersion] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [waveforms, setWaveforms] = useState<ReadonlyMap<string, Waveform | null>>(() => new Map());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [exporting, setExporting] = useState(false);
  const [renderError, setRenderError] = useState<unknown>(null);
  const requested = useRef(new Set<string>());
  const playerRef = useRef<HTMLVideoElement | null>(null);

  const jobRunning = isRunning(project.job);
  const readOnly = project.busy || (jobRunning && project.job?.type === "plan");
  const { status } = useJobStatus(project.id, jobRunning, () => onJobDone());

  const doc = shown(session);
  const view = useMemo(() => withPlan(catalog, doc.plan), [catalog, doc.plan]);

  // What the actions read when they run: the plan as it is now, even between two renders.
  const latest = useRef(doc.plan);
  latest.current = doc.plan;
  const limits = useRef(doc.limits);
  limits.current = doc.limits;
  const catalogRef = useRef(view);
  catalogRef.current = view;
  const sessionRef = useRef<Session>(session);
  sessionRef.current = session;
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const waveformsRef = useRef(waveforms);
  waveformsRef.current = waveforms;
  const playheadRef = useRef(playhead);
  playheadRef.current = playhead;
  const durationRef = useRef(doc.duration);
  durationRef.current = doc.duration;
  const notices = useRef(0);

  // --- saving ---

  const save = useCallback(async () => {
    const current = sessionRef.current;
    const sent = current.version;
    dispatch({ type: "sending" });
    try {
      const answer = await api.savePlan(project.id, current.history.present, current.overwrite ? "*" : current.document.revision);
      dispatch({ type: "saved", document: answer, version: sent });
      onSaved(answer.revision);
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.code === "revision_conflict") dispatch({ type: "conflict" });
      else if (failure instanceof ApiFailure && failure.code === "invalid_plan") {
        dispatch({ type: "refused", failure: { code: failure.code, message: failure.message, field: failure.field } });
      } else {
        const known = failure instanceof ApiFailure ? failure : null;
        dispatch({ type: "failed", failure: { code: known?.code ?? "unknown", message: known?.message ?? "", field: known?.field ?? null } });
      }
    }
  }, [project.id, onSaved]);

  useEffect(() => {
    if (readOnly || !canSave(session)) return undefined;
    const timer = window.setTimeout(() => void save(), SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [session, readOnly, save]);

  // Leaving the editor with something still to save: it is saved, and the browser asks before the page closes.
  useEffect(() => () => {
    const last = sessionRef.current;
    if (!readOnlyRef.current && canSave(last)) {
      void api.savePlan(project.id, last.history.present, last.overwrite ? "*" : last.document.revision).catch(() => undefined);
    }
  }, [project.id]);
  const dirty = isDirty(session);
  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // --- waveforms of the tracks the plan uses, the one selected in the Media panel and, in the Music tab, all of them ---

  const wanted = new Set<string>(doc.plan.music?.tracks.map((track) => track.file) ?? []);
  if (selection.kind === "track") wanted.add(selection.file);
  if (tab === "music") for (const track of catalog.music.tracks) wanted.add(track.file); // the list shows every length
  const wantedKey = [...wanted].sort().join("\n");
  useEffect(() => {
    for (const file of wantedKey ? wantedKey.split("\n") : []) {
      const track = catalog.music.tracks.find((entry) => entry.file === file);
      if (!track || requested.current.has(file)) continue;
      requested.current.add(file);
      api.waveform(project.id, track.id).then(
        (waveform) => setWaveforms((current) => new Map(current).set(file, waveform)),
        () => setWaveforms((current) => new Map(current).set(file, null)),
      );
    }
  }, [catalog, wantedKey, project.id]);

  const seek = useCallback((time: number) => {
    setPlayhead(time);
    setSeekVersion((version) => version + 1);
  }, []);

  const select = useCallback((next: Selection) => {
    setSelection(next);
    const id = next.kind === "element" ? next.id : next.kind === "effect" ? next.element : null;
    if (id) {
      const start = clipStart(latest.current.elements, id);
      if (start !== null) seek(start);
    }
  }, [seek]);

  // --- notices ---

  const notify = useCallback((text: string, undoable: boolean) => {
    notices.current += 1;
    setNotice({ id: notices.current, text, undoable, version: sessionRef.current.version + 1 });
  }, []);
  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice?.id]);
  const visibleNotice = notice && notice.version === session.version ? notice : null;

  // --- the edits ---

  const actions = useMemo<EditActions>(() => {
    const minimum = () => limits.current.min_effect_seconds;

    /** Applies an edit to the plan on screen; the history and the saving follow from the session. */
    const apply = (build: (plan: Plan) => Plan, group?: string, quiet = false): Plan | null => {
      if (readOnlyRef.current) return null;
      const before = latest.current;
      const next = build(before);
      if (next === before) return null;
      latest.current = next;
      dispatch({ type: "edit", plan: next, group });
      const lost = quiet ? 0 : lostEffects(before, next);
      if (lost > 0) notify(plural("notice.effects", lost), true);
      return next;
    };
    const defaults = () => defaultsOf(catalogRef.current.transitions);
    const minimumPart = () => limits.current.music?.min_part ?? MIN_PART_SECONDS;
    const lengths = (): Map<string, number | undefined> => new Map([...waveformsRef.current].map(([file, waveform]) => [file, waveform?.duration]));
    const forgetPart = () => setSelection((current) => (current.kind === "part" ? NO_SELECTION : current));

    /** The length of a track of the music root: from its waveform, which is asked for when it is not there yet. */
    const trackLength = async (file: string): Promise<number | null> => {
      const known = waveformsRef.current.get(file);
      if (known) return known.duration;
      const track = catalogRef.current.music.tracks.find((entry) => entry.file === file);
      if (!track) return null;
      try {
        const waveform = await api.waveform(project.id, track.id);
        setWaveforms((current) => new Map(current).set(file, waveform));
        return waveform.duration;
      } catch {
        return null;
      }
    };
    const addTracks = async (files: readonly string[], slot: number | null) => {
      const found: { file: string; length: number }[] = [];
      for (const file of files) {
        const length = await trackLength(file);
        if (length === null || length < minimumPart()) notify(t("notice.trackFailed", { name: file.split("/").pop() ?? file }), false);
        else found.push({ file, length });
      }
      if (found.length === 0) return;
      const next = apply((plan) => found.reduce((current, entry, index) =>
        addTrack(current, entry.file, entry.length, slot === null ? null : slot + index, durationRef.current), plan), undefined, true);
      if (next) forgetPart();
    };
    /** After an edit of the transitions: the new effect is selected, so its handles and sliders are at hand. */
    const showEffect = (next: Plan | null, id: string) => {
      const found = next ? locate(next, id) : null;
      if (found) setSelection({ kind: "effect", element: found.element.id, id });
    };
    const itemById = (id: string): CatalogItem | undefined =>
      [...catalogRef.current.photos, ...catalogRef.current.videos.flatMap((video) => video.moments)].find((item) => item.id === id);

    const addCandidates = (items: readonly CatalogItem[], slot: number | null = null) => {
      const { elements, skipped } = selectAddable(latest.current, items);
      if (skipped > 0) notify(plural("notice.skipped", skipped), false);
      if (elements.length === 0) return;
      apply((plan) => addElements(plan, elements, slot, minimum()));
      const only = elements.length === 1 ? elements[0] : undefined;
      if (only) setSelection({ kind: "element", id: only.id });
    };

    return {
      readOnly,
      addCandidates,
      addMoment: (source, videoLength, point) => {
        const taken = latest.current.elements.filter((element) => element.type === "video" && element.source === source)
          .map((element) => ({ start: element.start ?? 0, end: element.end ?? 0 }));
        const free = freeStretches(taken, videoLength);
        const stretch = point === null ? free.find((entry) => entry.max - entry.min >= 1) : free.find((entry) => point >= entry.min && point <= entry.max);
        const range = stretch ? momentAround({ point: point ?? stretch.min + 3, freeStart: stretch.min, freeEnd: stretch.max }) : null;
        if (!range) { notify(t("notice.noRoom"), false); return; }
        const element = newMoment(latest.current, source, range);
        apply((plan) => addElements(plan, [element], null, minimum()));
        setSelection({ kind: "element", id: element.id });
      },
      move: (id, slot) => { apply((plan) => moveElement(plan, id, slot, minimum())); },
      remove: (id) => {
        if (apply((plan) => removeElement(plan, id, minimum()))) {
          setSelection((current) => (current.kind === "element" && current.id === id ? NO_SELECTION : current));
          notify(t("notice.removed"), true);
        }
      },
      hide: (id) => {
        const element = latest.current.elements.find((entry) => entry.id === id);
        if (element && apply((plan) => excludeElement(plan, id, minimum()))) {
          setSelection((current) => (current.kind === "element" && current.id === id ? NO_SELECTION : current));
          notify(t(element.type === "video" ? "notice.deleted" : "notice.hidden"), true);
        }
      },
      hideCandidate: (item) => {
        if (apply((plan) => excludeCandidate(plan, item, minimum()))) {
          setSelection((current) => (current.kind === "element" && current.id === item.id ? NO_SELECTION : current));
          notify(t(item.start !== undefined ? "notice.deleted" : "notice.hidden"), true);
        }
      },
      restore: (item) => { apply((plan) => restoreCandidate(plan, item)); },
      setDuration: (id, seconds) => {
        const range = durationRange(limits.current.elements[id]);
        apply((plan) => setPhotoDuration(plan, id, seconds, range ?? undefined, minimum()), `duration:${id}`);
      },
      setEdge: (id, edge, value) => {
        apply((plan) => {
          const element = plan.elements.find((entry) => entry.id === id);
          const bounds = element ? trimRange(limits.current.elements[id], element) : null;
          return bounds ? setMomentEdge(plan, id, edge, value, bounds, minimum()) : plan;
        }, `${edge}:${id}`);
      },
      seal: () => dispatch({ type: "seal" }),

      placeTransition: (elementId, placement) => {
        const plan = latest.current;
        const start = clipStart(plan.elements, elementId);
        const at = start === null ? undefined : playheadRef.current - start;
        const clip = plan.elements.find((element) => element.id === elementId);
        const inside = at !== undefined && clip !== undefined && at >= 0 && at <= clip.duration ? at : undefined;
        const next = placeEffect(plan, elementId, placement, defaults(), inside, minimum());
        if (!next) { notify(t("notice.noRoomEffect"), false); return; }
        const id = nextEffectId(plan);
        showEffect(apply(() => next, undefined, true), id);
      },
      dropTransition: (payload, elementId, at) => {
        const plan = latest.current;
        if (payload.kind === "effect") {
          const next = moveEffect(plan, payload.id, elementId, at, minimum());
          if (!next) { notify(t("notice.noRoomEffect"), false); return; }
          showEffect(apply(() => next, undefined, true) ?? plan, payload.id);
          return;
        }
        if (payload.type === "cut") { apply((current) => hardCutNear(current, elementId, at, minimum()), undefined, true); return; }
        const type = payload.type;
        const host = hostFor(plan, type, elementId, at);
        const wanted = defaults()[type];
        const next = host ? addEffect(plan, host, type, { duration: wanted, offset: at - wanted / 2, minimum: minimum() }) : null;
        if (!next) { notify(t("notice.noRoomEffect"), false); return; }
        const id = nextEffectId(plan);
        showEffect(apply(() => next, undefined, true), id);
      },
      applyTransition: (kind) => {
        const result = applyToVideo(latest.current, kind, defaults(), minimum());
        if (result.applied === 0 && result.skipped === 0) { notify(t("notice.nothingToApply"), false); return; }
        if (result.skipped > 0 && result.applied === 0) { notify(t("notice.noRoomEffect"), false); return; }
        if (apply(() => result.plan, undefined, true)) {
          notify(`${plural("notice.applied", result.applied)}${result.skipped > 0 ? ` ${plural("notice.appliedSkipped", result.skipped)}` : ""}`, true);
        }
      },
      removeEffect: (id) => {
        if (apply((plan) => removeEffect(plan, id, minimum()), undefined, true)) {
          setSelection((current) => (current.kind === "effect" && current.id === id ? NO_SELECTION : current));
          notify(t("notice.effectRemoved"), true);
        }
      },
      setEffectEdge: (id, edge, value) => { apply((plan) => setEffectEdge(plan, id, edge, value, minimum()), `${edge}:${id}`, true); },
      setEffectStart: (id, value) => { apply((plan) => setEffectStart(plan, id, value, minimum()), `offset:${id}`, true); },
      setEffectDuration: (id, value) => { apply((plan) => setEffectDuration(plan, id, value, minimum()), `length:${id}`, true); },

      addTrack: (file, slot = null) => { void addTracks([file], slot); },
      addAllTracks: () => {
        const used = new Set(latest.current.music?.tracks.map((track) => track.file));
        const files = catalogRef.current.music.tracks.map((track) => track.file).filter((file) => !used.has(file));
        if (files.length === 0) notify(t("notice.allTracksUsed"), false); else void addTracks(files, null);
      },
      moveTrack: (index, slot) => { if (apply((plan) => moveTrack(plan, index, slot), undefined, true)) forgetPart(); },
      removeTrack: (index) => {
        if (apply((plan) => removeTrack(plan, index, durationRef.current), undefined, true)) { forgetPart(); notify(t("notice.trackRemoved"), true); }
      },
      removePart: (track, part) => {
        if (apply((plan) => removePart(plan, track, part, durationRef.current), undefined, true)) { forgetPart(); notify(t("notice.partRemoved"), true); }
      },
      cutMusic: (time) => {
        const cut = cutAt(latest.current, time, minimumPart());
        if (!cut) { notify(t("notice.noCut"), false); return; }
        if (apply(() => cut.plan, undefined, true)) setSelection({ kind: "part", track: cut.track, part: cut.part });
      },
      setPartMuted: (track, part, muted) => { apply((plan) => setPartMuted(plan, track, part, muted), undefined, true); },
      setPartFade: (track, part, which, seconds) => { apply((plan) => setPartFade(plan, track, part, which, seconds), `fade-${which}:${track}:${part}`, true); },
      setShift: (seconds) => {
        apply((plan) => setOffset(plan, seconds, offsetBounds(plan.music ?? { tracks: [], offset: 0 }, durationRef.current)), "shift", true);
      },
      restoreMusic: () => {
        if (apply((plan) => restoreDeleted(plan, lengths(), minimumPart(), durationRef.current), undefined, true)) forgetPart();
        else notify(t("notice.nothingDeleted"), false);
      },
      dropMusic: (payload, slot) => {
        if (payload.kind === "track") void addTracks([payload.file], slot);
        else if (apply((plan) => moveTrack(plan, payload.index, slot), undefined, true)) forgetPart();
      },

      drop: (payload: DragPayload, slot: number) => {
        if (payload.kind === "transition" || payload.kind === "effect" || payload.kind === "track" || payload.kind === "musicTrack") return; // dropped on a clip or on the music lane
        if (payload.kind === "clip") { apply((plan) => moveElement(plan, payload.id, slot, minimum())); return; }
        if (payload.kind === "video") {
          const video = catalogRef.current.videos.find((entry) => entry.media === payload.media);
          addCandidates(video?.moments.filter((moment) => moment.status === "available") ?? [], slot);
          return;
        }
        const item = itemById(payload.id);
        if (item && (item.status === "available" || item.status === "discarded")) addCandidates([item], slot);
      },
    };
  }, [readOnly, notify, plural, t, project.id]);

  // --- keyboard (section 12) ---

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const command = commandFor(event, describe(event.target));
      if (!command) return;
      const element = selection.kind === "element" ? latest.current.elements.findIndex((entry) => entry.id === selection.id) : -1;
      switch (command) {
        case "play": {
          const video = playerRef.current;
          if (video) {
            event.preventDefault();
            if (video.paused) void video.play().catch(() => undefined); else video.pause();
          }
          break;
        }
        case "clear": setSelection(NO_SELECTION); break;
        case "undo": event.preventDefault(); if (!readOnly) dispatch({ type: "undo" }); break;
        case "redo": event.preventDefault(); if (!readOnly) dispatch({ type: "redo" }); break;
        case "remove":
          if (selection.kind === "element" && element >= 0) { event.preventDefault(); actions.remove(selection.id); }
          else if (selection.kind === "effect") { event.preventDefault(); actions.removeEffect(selection.id); }
          else if (selection.kind === "part") { event.preventDefault(); actions.removePart(selection.track, selection.part); }
          break;
        case "earlier":
          if (selection.kind === "element" && element > 0) { event.preventDefault(); actions.move(selection.id, element - 1); }
          break;
        case "later":
          if (selection.kind === "element" && element >= 0) { event.preventDefault(); actions.move(selection.id, element + 2); }
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selection, actions, readOnly]);

  // --- preview and export (sections 6, 13 and 14) ---

  const exportReason: MessageKey | null = project.busy ? "top.export.busy"
    : !project.plan.present || !project.plan.valid ? "top.export.noPlan"
      : jobRunning ? "top.export.running"
        : dirty ? "top.export.unsaved" : null;

  const render = useMemo<RenderControls>(() => ({
    canStart: canRender(project) && !dirty,
    error: renderError,
    onPreview: () => {
      setRenderError(null);
      api.startRender(project.id, "preview").then(() => onJobDone(), setRenderError);
    },
    onExport: () => setExporting(true),
    onStop: () => {
      setRenderError(null);
      api.cancel(project.id).then(() => onJobDone(), setRenderError);
    },
  }), [project, dirty, renderError, onJobDone]);

  const missing = doc.warnings.filter((warning) => warning.code === "media_missing").length;
  const failure = session.failure;
  const state = saveState(session);

  return (
    <div className="editor">
      <TopBar project={project} catalog={view} requested={doc.plan.requested_duration} working={jobRunning}
        jobType={jobRunning ? project.job?.type : null} previewOutdated={project.preview.present && project.preview.outdated}
        exporting={{ reason: exportReason, onExport: () => setExporting(true) }}
        edit={{ canUndo: canUndoSession(session), canRedo: canRedoSession(session), onUndo: () => dispatch({ type: "undo" }), onRedo: () => dispatch({ type: "redo" }), save: state, readOnly }}
        onChangeFolder={onChangeFolder} onSettings={onSettings} />
      <main className="editor__main" id="content">
        {project.busy ? <p className="banner banner--warn" role="status">{t("editor.busy")}</p> : null}
        {incomplete ? (
          <p className="banner banner--warn" role="status">
            {t("editor.incomplete")}{project.analysis.files.removed > 0 ? ` ${t("editor.removedFiles", { n: project.analysis.files.removed })}` : ""}
            {" "}<button type="button" className="link" onClick={onSettings}>{t("top.completeAnalysis")}</button>
          </p>
        ) : null}
        {missing > 0 ? <p className="banner banner--warn" role="status">{t("banner.missing", { n: missing })}</p> : null}
        {session.conflict ? (
          <div className="banner banner--warn" role="alertdialog" aria-labelledby="conflict-title" aria-describedby="conflict-body">
            <p id="conflict-title"><strong>{t("conflict.title")}</strong></p>
            <p id="conflict-body">{t("conflict.body")}</p>
            <div className="actions actions--tight">
              <button type="button" className="btn" onClick={onReload}>{t("conflict.reload")}</button>
              <button type="button" className="btn" onClick={() => dispatch({ type: "overwrite" })}>{t("conflict.overwrite")}</button>
            </div>
          </div>
        ) : null}
        {failure ? (
          <div className="banner banner--error" role="alert">
            <p><strong>{t("failure.title")}</strong>{" "}{failure.code === "invalid_plan" ? t("failure.refused") : errorText(i18n, failure.code, failure.message)}</p>
            {failure.code === "invalid_plan" && failure.message ? <p className="notice__detail">{failure.message}</p> : null}
            <div className="actions actions--tight">
              <button type="button" className="btn" onClick={() => dispatch({ type: "retry" })}>{t(failure.code === "invalid_plan" ? "failure.dismiss" : "failure.retry")}</button>
            </div>
          </div>
        ) : null}
        <div className="editor__panels">
          <MediaPanel projectId={project.id} catalog={view} document={doc} waveforms={waveforms} selection={selection}
            tab={tab} actions={actions} onTab={setTab} onSelect={select} />
          <DetailPanel projectId={project.id} project={project} document={doc} catalog={view} waveforms={waveforms}
            selection={selection} playhead={playhead} seekVersion={seekVersion} job={status} collapsed={collapsed} actions={actions}
            render={render} playerRef={playerRef} onTime={setPlayhead} onSelect={select} onToggle={() => setCollapsed((value) => !value)} />
        </div>
      </main>
      <Timeline projectId={project.id} document={doc} waveforms={waveforms} selection={selection}
        playhead={playhead} actions={actions} onSelect={select} onSeek={seek} />
      {exporting ? (
        <ExportDialog project={project} status={status} output={doc.plan.output} onClose={() => setExporting(false)} onChanged={onJobDone} />
      ) : null}
      {visibleNotice ? (
        <Toast text={visibleNotice.text} undo={visibleNotice.undoable ? t("notice.undo") : undefined}
          onUndo={() => { dispatch({ type: "undo" }); setNotice(null); }} closeLabel={t("notice.close")} onClose={() => setNotice(null)} />
      ) : null}
    </div>
  );
}
