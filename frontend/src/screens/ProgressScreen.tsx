// The analysis in progress (section 6): `status.json` polled every second, with a stop button, and the outcome.
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { StatusResponse } from "../api/types";
import { ErrorNotice, ProgressBar } from "../components/ui";
import { StopIcon } from "../components/icons";
import { exitText, useI18n, type MessageKey } from "../i18n";
import { clock } from "../lib/format";

export const POLL_MS = 1000;

/** Polls a project's status while a job runs and a little after, and tells when it ends. */
export function useJobStatus(projectId: string, active: boolean, onFinished?: (status: StatusResponse) => void) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const finished = useRef(onFinished);
  finished.current = onFinished;
  const reported = useRef<string | null>(null);

  useEffect(() => {
    if (!active) return undefined;
    let alive = true;
    const tick = async () => {
      try {
        const next = await api.status(projectId);
        if (!alive) return;
        setStatus(next);
        setError(null);
        const job = next.job;
        if (job && job.state !== "running" && job.state !== "stopping" && reported.current !== job.id) {
          reported.current = job.id;
          finished.current?.(next);
        }
      } catch (failure) {
        if (alive) setError(failure);
      }
    };
    void tick();
    const timer = window.setInterval(tick, POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [projectId, active]);

  return { status, error };
}

interface Props {
  projectId: string;
  onDone: () => void;
  onBack: () => void;
}

export function ProgressScreen({ projectId, onDone, onBack }: Props) {
  const i18n = useI18n();
  const { t, format } = i18n;
  const [stopError, setStopError] = useState<unknown>(null);
  const [outcome, setOutcome] = useState<StatusResponse | null>(null);
  const { status, error } = useJobStatus(projectId, outcome === null, setOutcome);
  const job = (outcome ?? status)?.job ?? null;
  const progress = (outcome ?? status)?.progress ?? null;
  const running = job?.state === "running" || job?.state === "stopping";

  const stop = useCallback(() => {
    api.cancel(projectId).then(() => setStopError(null), setStopError);
  }, [projectId]);

  const title = job?.type === "preview" ? t("progress.titlePreview") : job?.type === "export" ? t("progress.titleExport") : t("progress.title");
  const phaseKey = progress?.phase ? `phase.${progress.phase}` : null;
  const phase = progress?.phase ? (phaseKey && i18n.has(phaseKey) ? t(phaseKey as MessageKey) : progress.phase) : null;
  const done = progress?.done ?? 0;
  const total = progress?.total ?? 0;

  return (
    <main className="screen screen--narrow" id="content">
      <h1>{title}</h1>
      <ErrorNotice error={error ?? stopError} />
      {!job && !error ? <p className="loading" role="status">{t("progress.starting")}</p> : null}

      {running ? (
        <section className="progress" aria-live="polite">
          {phase ? <p className="progress__phase">{t("progress.phase", { phase })}</p> : null}
          <ProgressBar value={done} max={total} label={t("progress.bar")} />
          <p className="progress__numbers">
            {total > 0 ? t("progress.items", { done: format.number(done), total: format.number(total) }) : null}
            {progress?.eta_seconds ? ` · ${t("progress.eta", { time: clock(progress.eta_seconds) })}` : null}
          </p>
          {job?.state === "stopping" ? <p role="status">{t("progress.stopping")}</p> : (
            <div className="actions">
              <button type="button" className="btn" onClick={stop}><StopIcon />{t("progress.stop")}</button>
            </div>
          )}
        </section>
      ) : null}

      {job && !running ? (
        <section className="progress">
          {job.state === "done" ? (
            <>
              <p className="notice notice--ok" role="status">{t("progress.done")}</p>
              <div className="actions"><button type="button" className="btn btn--primary" onClick={onDone}>{t("progress.open")}</button></div>
            </>
          ) : (
            <>
              <div className="notice notice--error" role="alert">
                <div>
                  <p>{job.exit ? exitText(i18n, job.exit.key, job.exit.message) : t("app.unknownError")}</p>
                  {job.exit?.detail ? <p className="notice__detail">{t("progress.log", { detail: job.exit.detail })}</p> : null}
                </div>
              </div>
              <div className="actions">
                <button type="button" className="btn" onClick={onBack}>{t("progress.back")}</button>
              </div>
            </>
          )}
        </section>
      ) : null}
    </main>
  );
}
