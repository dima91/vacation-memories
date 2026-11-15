// The editor (section 10): Media, Detail and the timeline on the project's real data, with the preview playing, and
// (U5) the basic editing: the plan on screen changes with the user's edits, the history undoes them and every confirmed
// change is saved with the revision that was read (sections 4, 12 and 14).
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ApiFailure, api } from "../api/client";
import type { Catalog, CatalogItem, Plan, PlanDocument, ProjectState, Waveform } from "../api/types";
import { DetailPanel } from "../components/DetailPanel";
import type { EditActions } from "../components/editing";
import { MediaPanel, type MediaTab } from "../components/MediaPanel";
import { Timeline } from "../components/Timeline";
import { TopBar } from "../components/TopBar";
import { ErrorNotice, Spinner, Toast } from "../components/ui";
import { errorText, useI18n } from "../i18n";
import { withPlan } from "../lib/catalog";
import type { DragPayload } from "../lib/dnd";
import { freeStretches, momentAround } from "../lib/drag";
import { durationRange, trimRange } from "../lib/limits";
import {
  addElements, excludeCandidate, excludeElement, lostEffects, moveElement, newMoment, removeElement, restoreCandidate, restoreMoments,
  selectAddable, setMomentEdge, setPhotoDuration,
} from "../lib/planEdit";
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
  const requested = useRef(new Set<string>());
  const playerRef = useRef<HTMLVideoElement | null>(null);

  const jobRunning = project.job?.state === "running" || project.job?.state === "stopping";
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

  // --- waveforms of the tracks the plan uses, and of the one selected in the Media panel ---

  const wanted = new Set<string>(doc.plan.music?.tracks.map((track) => track.file) ?? []);
  if (selection.kind === "track") wanted.add(selection.file);
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
    const apply = (build: (plan: Plan) => Plan, group?: string): Plan | null => {
      if (readOnlyRef.current) return null;
      const before = latest.current;
      const next = build(before);
      if (next === before) return null;
      latest.current = next;
      dispatch({ type: "edit", plan: next, group });
      const lost = lostEffects(before, next);
      if (lost > 0) notify(plural("notice.effects", lost), true);
      return next;
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
      restoreMoments: () => { apply((plan) => restoreMoments(plan)); },
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
      drop: (payload: DragPayload, slot: number) => {
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
  }, [readOnly, notify, plural, t]);

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

  const missing = doc.warnings.filter((warning) => warning.code === "media_missing").length;
  const failure = session.failure;
  const state = saveState(session);

  return (
    <div className="editor">
      <TopBar project={project} catalog={view} requested={doc.plan.requested_duration} working={jobRunning}
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
            playerRef={playerRef} onTime={setPlayhead} onSelect={select} onToggle={() => setCollapsed((value) => !value)} />
        </div>
      </main>
      <Timeline projectId={project.id} document={doc} waveforms={waveforms} selection={selection}
        playhead={playhead} actions={actions} onSelect={select} onSeek={seek} />
      {visibleNotice ? (
        <Toast text={visibleNotice.text} undo={visibleNotice.undoable ? t("notice.undo") : undefined}
          onUndo={() => { dispatch({ type: "undo" }); setNotice(null); }} closeLabel={t("notice.close")} onClose={() => setNotice(null)} />
      ) : null}
    </div>
  );
}
