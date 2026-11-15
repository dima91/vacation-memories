// The progress of a running job (analysis, preview or export): phase, bar, items, time left and the stop button.
// The same piece serves the export dialog and the Detail panel (section 14: "progress bar from `status.json`, the render
// can be stopped").
import type { JobInfo, Progress } from "../api/types";
import { useI18n, type MessageKey, type Translator } from "../i18n";
import { clock } from "../lib/format";
import { StopIcon } from "./icons";
import { ProgressBar } from "./ui";

/** What the editor offers while it renders: start a preview, open the export, stop the running job. */
export interface RenderControls {
  /** A render can start: a valid plan, no other process on the project and no job running. */
  canStart: boolean;
  /** The failure of the last attempt to start or stop a job, if there was one. */
  error: unknown;
  onPreview: () => void;
  onExport: () => void;
  onStop: () => void;
}

export function phaseText(i18n: Translator, phase: string | null): string | null {
  if (!phase) return null;
  const key = `phase.${phase}`;
  return i18n.has(key) ? i18n.t(key as MessageKey) : phase;
}

export function JobProgress({ job, progress, onStop }: { job: JobInfo; progress: Progress | null; onStop: () => void }) {
  const i18n = useI18n();
  const { t, format } = i18n;
  const phase = phaseText(i18n, progress?.phase ?? null);
  const done = progress?.done ?? 0;
  const total = progress?.total ?? 0;
  return (
    <div className="jobprogress" aria-live="polite">
      {phase ? <p className="progress__phase">{t("progress.phase", { phase })}</p> : null}
      <ProgressBar value={done} max={total} label={t("progress.bar")} />
      <p className="progress__numbers">
        {total > 0 ? t("progress.items", { done: format.number(done), total: format.number(total) }) : null}
        {progress?.eta_seconds ? `${total > 0 ? " · " : ""}${t("progress.eta", { time: clock(progress.eta_seconds) })}` : null}
      </p>
      {job.state === "stopping" ? <p role="status">{t("progress.stopping")}</p> : (
        <button type="button" className="btn" onClick={onStop}><StopIcon />{t("progress.stop")}</button>
      )}
    </div>
  );
}
