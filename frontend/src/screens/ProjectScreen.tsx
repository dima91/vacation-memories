// A project: it chooses the screen of the flow (section 2): settings and analysis when the plan is missing, the
// progress while the analysis runs, the editor otherwise.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { ProjectState } from "../api/types";
import { ErrorNotice, Spinner } from "../components/ui";
import { useI18n } from "../i18n";
import { autoPreviewDue } from "../lib/render";
import { EditorScreen } from "./EditorScreen";
import { ProgressScreen } from "./ProgressScreen";
import { SettingsScreen } from "./SettingsScreen";

export type ProjectView = "editor" | "settings" | "progress";

export function chooseView(project: ProjectState): ProjectView {
  if (project.job && (project.job.state === "running" || project.job.state === "stopping") && project.job.type === "plan") return "progress";
  return project.plan.present ? "editor" : "settings";
}

export function ProjectScreen({ projectId, onLeave }: { projectId: string; onLeave: () => void }) {
  const { t } = useI18n();
  const [project, setProject] = useState<ProjectState | null>(null);
  const [view, setView] = useState<ProjectView | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (chooseAgain: boolean) => {
    try {
      const next = await api.project(projectId);
      setProject(next);
      setError(null);
      if (chooseAgain) setView(chooseView(next));
      return next;
    } catch (failure) {
      setError(failure);
      return null;
    }
  }, [projectId]);

  /** A new plan is there: the preview of the whole video starts by itself, unless the setting is off (section 6). */
  const previewOfNewPlan = useCallback(async () => {
    const next = await load(false);
    if (!next || !autoPreviewDue(next)) return;
    try {
      await api.startRender(projectId, "preview");
    } catch {
      return; // the editor offers the button, with the reason if it fails again
    }
    await load(false);
  }, [load, projectId]);

  useEffect(() => {
    setProject(null);
    setView(null);
    void load(true);
  }, [load]);

  if (error && !project) {
    return <main className="screen" id="content"><ErrorNotice error={error} /><div className="actions"><button type="button" className="btn" onClick={onLeave}>{t("top.change")}</button></div></main>;
  }
  if (!project || !view) return <main className="screen" id="content"><Spinner label={t("editor.loading")} /></main>;

  if (view === "progress") {
    return <ProgressScreen projectId={projectId} onBack={() => { void load(false); setView("settings"); }}
      onPlanReady={() => { void previewOfNewPlan(); }} onDone={() => { void load(true); }} />;
  }
  if (view === "settings") {
    return <SettingsScreen project={project} mode={project.plan.present ? "edit" : "analyze"}
      onSaved={() => { void load(false); }} onStarted={() => setView("progress")}
      onBack={project.plan.present ? () => setView("editor") : undefined} />;
  }
  return <EditorScreen project={project} onProjectChanged={() => { void load(false); }} onChangeFolder={onLeave} onSettings={() => setView("settings")} />;
}
