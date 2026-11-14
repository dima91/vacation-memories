// The editor (section 10): Media, Detail and the timeline on the project's real data, with the preview playing.
// It reads and shows; changing the plan comes with step U5.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiFailure, api } from "../api/client";
import type { Catalog, PlanDocument, ProjectState, Waveform } from "../api/types";
import { DetailPanel } from "../components/DetailPanel";
import { MediaPanel, type MediaTab } from "../components/MediaPanel";
import { Timeline } from "../components/Timeline";
import { TopBar } from "../components/TopBar";
import { ErrorNotice, Spinner } from "../components/ui";
import { errorText, useI18n } from "../i18n";
import { NO_SELECTION, type Selection } from "../lib/selection";
import { clipStart } from "../lib/timeline";
import { useJobStatus } from "./ProgressScreen";

interface Props {
  project: ProjectState;
  onProjectChanged: () => void;
  onChangeFolder: () => void;
  onSettings: () => void;
}

const isTyping = (target: EventTarget | null): boolean =>
  target instanceof HTMLElement && target.closest("input, textarea, select, button, a, summary, [contenteditable='true'], [role='tab']") !== null;

export function EditorScreen({ project, onProjectChanged, onChangeFolder, onSettings }: Props) {
  const i18n = useI18n();
  const { t } = i18n;
  const [document, setDocument] = useState<PlanDocument | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [selection, setSelection] = useState<Selection>(NO_SELECTION);
  const [tab, setTab] = useState<MediaTab>("photos");
  const [playhead, setPlayhead] = useState(0);
  const [seekVersion, setSeekVersion] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [waveforms, setWaveforms] = useState<ReadonlyMap<string, Waveform | null>>(() => new Map());
  const requested = useRef(new Set<string>());
  const playerRef = useRef<HTMLVideoElement | null>(null);

  const jobRunning = project.job?.state === "running" || project.job?.state === "stopping";
  const { status } = useJobStatus(project.id, jobRunning, () => onProjectChanged());

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([api.plan(project.id), api.catalog(project.id)]).then(([planDocument, catalogDocument]) => {
      if (!alive) return;
      setDocument(planDocument);
      setCatalog(catalogDocument);
      setError(null);
    }, (failure) => alive && setError(failure)).finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [project.id, project.plan.revision]);

  // The waveforms of the tracks the plan uses, and of the one selected in the Media panel.
  const wanted = new Set<string>(document?.plan.music?.tracks.map((track) => track.file) ?? []);
  if (selection.kind === "track") wanted.add(selection.file);
  const wantedKey = [...wanted].sort().join("\n");
  useEffect(() => {
    if (!catalog) return;
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
    const id = next.kind === "element" || next.kind === "effect" ? (next.kind === "element" ? next.id : next.element) : null;
    if (id && document) {
      const start = clipStart(document.plan.elements, id);
      if (start !== null) seek(start);
    }
  }, [document, seek]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.key === " " && !isTyping(event.target)) {
        const video = playerRef.current;
        if (video) {
          event.preventDefault();
          if (video.paused) void video.play().catch(() => undefined); else video.pause();
        }
      } else if (event.key === "Escape" && !isTyping(event.target)) {
        setSelection(NO_SELECTION);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const noPlan = error instanceof ApiFailure && error.code === "no_plan";
  const missing = document?.warnings.filter((warning) => warning.code === "media_missing").length ?? 0;
  const incomplete = project.analysis.present && !project.analysis.complete;

  return (
    <div className="editor">
      <TopBar project={project} catalog={catalog} requested={document?.plan.requested_duration ?? null} working={jobRunning}
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
        {loading ? <Spinner label={t("editor.loading")} /> : null}
        {!loading && error ? (
          <div className="screen">
            {noPlan ? <p className="notice notice--info">{t("editor.noPlan")}</p>
              : error instanceof ApiFailure && error.code === "invalid_plan"
                ? <p className="notice notice--error" role="alert">{t("editor.invalidPlan", { error: error.message })}</p>
                : <ErrorNotice error={error} />}
            <div className="actions"><button type="button" className="btn" onClick={onSettings}>{t("top.settings")}</button></div>
          </div>
        ) : null}
        {!loading && !error && document && catalog ? (
          <div className="editor__panels">
            <MediaPanel projectId={project.id} catalog={catalog} document={document} waveforms={waveforms} selection={selection}
              tab={tab} onTab={setTab} onSelect={select} />
            <DetailPanel projectId={project.id} project={project} document={document} catalog={catalog} waveforms={waveforms}
              selection={selection} playhead={playhead} seekVersion={seekVersion} job={status} collapsed={collapsed} playerRef={playerRef}
              onTime={setPlayhead} onSelect={select} onToggle={() => setCollapsed((value) => !value)} />
          </div>
        ) : null}
      </main>
      {document ? <Timeline projectId={project.id} document={document} waveforms={waveforms} selection={selection}
        playhead={playhead} onSelect={select} onSeek={seek} /> : null}
      {error instanceof ApiFailure && !noPlan && error.code !== "invalid_plan" ? <span className="visually-hidden" role="alert">{errorText(i18n, error.code, error.message)}</span> : null}
    </div>
  );
}
