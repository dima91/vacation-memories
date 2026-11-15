// The rules of the previews and of the export that the interface needs (sections 6, 13 and 14 of UI-SPEC.md): when a
// preview starts by itself, what a name for the export may be, and what a finished job tells. Pure functions, no DOM.
import type { ExportEntry, JobInfo, ProjectState } from "../api/types";

export const DEFAULT_EXPORT_NAME = "memories";
const NAME = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$/; // the server's rule (`valid_video_name`)
const RESERVED_ENDINGS = ["_preview", ".partial"];

export type NameProblem = "empty" | "invalid" | "reserved";

/** Why a name cannot be the name of the export, as the server would refuse it (the server checks again). */
export function exportNameProblem(name: string): NameProblem | null {
  if (name.trim() === "") return "empty";
  if (!NAME.test(name)) return "invalid";
  if (RESERVED_ENDINGS.some((ending) => name.endsWith(ending)) || name.includes("..")) return "reserved";
  return null;
}

export const isRunning = (job: JobInfo | null | undefined): boolean => job?.state === "running" || job?.state === "stopping";

/** A render (preview or export) can start: there is a valid plan, no other process uses the project and no job runs. */
export function canRender(project: ProjectState): boolean {
  return project.plan.present && project.plan.valid && !project.busy && !isRunning(project.job);
}

/** The preview starts by itself after a new plan, when the setting is on and nothing else is going on (section 6). */
export const autoPreviewDue = (project: ProjectState): boolean => project.settings.auto_preview && canRender(project);

/** The file of an export by name, the complete one rather than its `.partial` copy. */
export const exportNamed = (exports: readonly ExportEntry[], name: string): ExportEntry | undefined =>
  exports.find((entry) => entry.name === name && !entry.partial);

/** The partial file the failed check of an export left (`<name>.partial.mp4`, code 3), if there is one. */
export const partialOf = (exports: readonly ExportEntry[], name: string | null): ExportEntry | undefined =>
  name === null ? undefined : exports.find((entry) => entry.partial && entry.name === `${name}.partial`);

export type Outcome = "running" | "done" | "interrupted" | "check_failed" | "failed";

/** What a job's end is for the user: done, stopped (it resumes), final check failed (a partial file is offered) or failed. */
export function outcomeOf(job: JobInfo): Outcome {
  if (isRunning(job)) return "running";
  if (job.state === "done") return "done";
  if (job.state === "interrupted") return "interrupted";
  return job.exit?.key === "check_failed" ? "check_failed" : "failed";
}

/** A save made while a render runs: the render keeps the version it read when it started (section 6). */
export const renderUsesOlderPlan = (job: JobInfo | null | undefined, revision: string | null): boolean =>
  isRunning(job) && job?.type !== "plan" && !!job?.plan_revision && !!revision && job.plan_revision !== revision;

/**
 * The newer of two descriptions of the jobs of a project: the polled status or the state read with the project. Of two
 * descriptions of the same job the finished one wins: a job never runs again, so the running one is a poll that came late.
 */
export function latestJob(polled: JobInfo | null | undefined, known: JobInfo | null | undefined): JobInfo | null {
  if (!polled) return known ?? null;
  if (!known) return polled;
  if (polled.id === known.id) return isRunning(polled) && !isRunning(known) ? known : polled;
  return polled.started_at >= known.started_at ? polled : known;
}
