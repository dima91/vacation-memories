// "Export video" (sections 2, 6 and 14 of UI-SPEC.md): the name of the file, then the progress with the stop button, then
// where the file is and the download, or the reason it failed (and, when the final check failed, the partial file).
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { ApiFailure, api, urls } from "../api/client";
import type { JobInfo, ProjectState, StatusResponse } from "../api/types";
import { exitText, useI18n, type MessageKey } from "../i18n";
import {
  DEFAULT_EXPORT_NAME, exportNameProblem, exportNamed, isRunning, latestJob, outcomeOf, partialOf, renderUsesOlderPlan,
} from "../lib/render";
import { JobProgress } from "./JobProgress";
import { ErrorNotice } from "./ui";

export interface ExportDialogProps {
  project: ProjectState;
  /** The newest status polled while a job runs. */
  status: StatusResponse | null;
  /** The size of the video the export makes (from the plan's output). */
  output: { width: number; height: number };
  onClose: () => void;
  /** The project changed (a job started or was told to stop): the editor reads its state again. */
  onChanged: () => void;
  /** The id of an export this dialog already started: its result is shown instead of the form. */
  startedJob?: string;
}

export function ExportDialog({ project, status, output, onClose, onChanged, startedJob }: ExportDialogProps) {
  const i18n = useI18n();
  const { t } = i18n;
  const ref = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const nameId = useId();
  const lastExport = [...project.exports].reverse().find((entry) => !entry.partial);
  const [name, setName] = useState(lastExport?.name ?? DEFAULT_EXPORT_NAME);
  const [started, setStarted] = useState<string | null>(startedJob ?? null);
  /** The job as the server answered the request: it stands in until the polled status and the project catch up. */
  const [created, setCreated] = useState<JobInfo | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open && typeof dialog.showModal === "function") dialog.showModal();
  }, []);

  const job = latestJob(latestJob(status?.job, project.job), created);
  const exportJob = job?.type === "export" && (isRunning(job) || job.id === started) ? job : null;
  const progress = exportJob && status?.job?.id === exportJob.id ? status.progress : null;
  const trimmed = name.trim();
  const problem = exportNameProblem(trimmed);
  const exists = problem === null && exportNamed(project.exports, trimmed) !== undefined;
  const nameError = problem ? t(`export.name.${problem}` as MessageKey) : null;
  const fieldError = error instanceof ApiFailure && error.field === "name" ? error.message : null;

  const start = async (event: FormEvent) => {
    event.preventDefault();
    if (problem || working) return;
    setWorking(true);
    setError(null);
    try {
      const job = await api.startRender(project.id, "export", trimmed);
      setCreated(job);
      setStarted(job.id);
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setWorking(false);
    }
  };

  const stop = () => {
    api.cancel(project.id).then(() => { setError(null); onChanged(); }, setError);
  };

  const again = () => {
    setStarted(null);
    setCreated(null);
    setError(null);
  };

  const body = exportJob ? <JobView job={exportJob} progress={progress} project={project} onStop={stop} onAgain={again} onClose={onClose} error={error} />
    : (
      <form onSubmit={start} noValidate>
        <p>{t("export.intro", { width: output.width, height: output.height })}</p>
        <ErrorNotice error={fieldError ? null : error} />
        <div className={`field${nameError || fieldError ? " field--error" : ""}`}>
          <label htmlFor={nameId}>{t("export.name")}</label>
          <input id={nameId} type="text" value={name} maxLength={64} autoComplete="off" spellCheck={false}
            aria-describedby={`${nameId}-hint ${nameId}-error`} aria-invalid={nameError ? true : undefined}
            onChange={(event) => { setName(event.target.value); setError(null); }} />
          <p className="field__hint" id={`${nameId}-hint`}>{t("export.name.hint", { name: trimmed || "…" })}</p>
          <p className="field__error" id={`${nameId}-error`}>{nameError ?? fieldError}</p>
        </div>
        {exists ? <p className="notice notice--warn" role="status">{t("export.exists")}</p> : null}
        <div className="actions actions--tight">
          <button type="submit" className="btn btn--primary" disabled={problem !== null || working}>{t("export.start")}</button>
          <button type="button" className="btn" onClick={onClose}>{t("export.cancel")}</button>
        </div>
      </form>
    );

  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={onClose}>
      <h2 id={titleId}>{t("export.title")}</h2>
      {body}
    </dialog>
  );
}

function JobView({ job, progress, project, error, onStop, onAgain, onClose }: {
  job: JobInfo; progress: StatusResponse["progress"]; project: ProjectState; error: unknown;
  onStop: () => void; onAgain: () => void; onClose: () => void;
}) {
  const i18n = useI18n();
  const { t, format } = i18n;
  const outcome = outcomeOf(job);
  const name = job.name ?? DEFAULT_EXPORT_NAME;
  const file = exportNamed(project.exports, name);
  const partial = partialOf(project.exports, name);
  return (
    <div className="exportjob">
      <ErrorNotice error={error} />
      {outcome === "running" ? (
        <>
          <p><strong>{t("export.running", { name })}</strong></p>
          <JobProgress job={job} progress={progress} onStop={onStop} />
          <p className="help">{t("export.editWhile")}</p>
          <p className="help">{t("export.stopHint")}</p>
          {renderUsesOlderPlan(job, project.plan.revision) ? <p className="notice notice--warn" role="status">{t("summary.renderOlder")}</p> : null}
          <div className="actions actions--tight"><button type="button" className="btn" onClick={onClose}>{t("export.close")}</button></div>
        </>
      ) : null}
      {outcome === "done" ? (
        <>
          <p className="notice notice--ok" role="status">{t("export.done")}</p>
          {file ? (
            <>
              <p>{t("export.where", { path: file.path })}</p>
              <p className="help">{t("export.size", { size: format.bytes(file.size) })}</p>
            </>
          ) : null}
          {job.plan_revision && project.plan.revision && job.plan_revision !== project.plan.revision
            ? <p className="notice notice--warn" role="status">{t("export.changedAfter")}</p> : null}
          <div className="actions actions--tight">
            {file ? <a className="btn btn--primary" href={urls.download(project.id, file.name)} download>{t("export.download")}</a> : null}
            <button type="button" className="btn" onClick={onAgain}>{t("export.again")}</button>
            <button type="button" className="btn" onClick={onClose}>{t("export.close")}</button>
          </div>
        </>
      ) : null}
      {outcome !== "running" && outcome !== "done" ? (
        <>
          <div className="notice notice--error" role="alert">
            <div>
              <p><strong>{t("export.failed")}</strong></p>
              <p>{job.exit ? exitText(i18n, job.exit.key, job.exit.message) : t("app.unknownError")}</p>
              {job.exit?.detail ? <p className="notice__detail">{t("progress.log", { detail: job.exit.detail })}</p> : null}
            </div>
          </div>
          {outcome === "check_failed" && partial ? <p className="help">{t("export.partialNote")}</p> : null}
          <div className="actions actions--tight">
            {outcome === "check_failed" && partial
              ? <a className="btn" href={urls.download(project.id, partial.name)} download>{t("export.partial")}</a> : null}
            <button type="button" className="btn btn--primary" onClick={onAgain}>{t("export.again")}</button>
            <button type="button" className="btn" onClick={onClose}>{t("export.close")}</button>
          </div>
        </>
      ) : null}
    </div>
  );
}
