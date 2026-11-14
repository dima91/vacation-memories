// The bar of the editor: project, state of the analysis and of the jobs, requested duration, settings.
// Undo/redo and "Export video" come with the steps that make them work (U5 and U7).
import type { Catalog, ProjectState } from "../api/types";
import { useI18n } from "../i18n";
import { clock } from "../lib/format";
import { CheckIcon, AlertIcon, FilmIcon, FolderIcon, GearIcon } from "./icons";

interface Props {
  project: ProjectState;
  catalog: Catalog | null;
  requested: number | null;
  working: boolean;
  onChangeFolder: () => void;
  onSettings: () => void;
}

export function TopBar({ project, catalog, requested, working, onChangeFolder, onSettings }: Props) {
  const { t } = useI18n();
  const analysis = project.analysis;
  const complete = analysis.present && analysis.complete;
  const folder = project.folder === "." ? t("folders.root") : project.folder;
  return (
    <header className="topbar">
      <div className="brand"><FilmIcon /><span className="brand__name">{t("app.name")}</span></div>
      <div className="topbar__project">
        <FolderIcon />
        <span className="topbar__folder" title={folder} aria-label={`${t("top.project")}: ${folder}`}>{folder}</span>
        <button type="button" className="link" onClick={onChangeFolder}>{t("top.change")}</button>
      </div>
      <p className={`topbar__status${complete ? "" : " topbar__status--warn"}`} role="status">
        {complete ? <CheckIcon /> : <AlertIcon />}
        <span>
          {complete
            ? t("top.analysis.complete", { photos: catalog?.photos.length ?? 0, videos: catalog?.videos.length ?? 0 })
            : t("top.analysis.incomplete")}
          {working ? ` · ${t("top.job.running")}` : ""}
          {project.busy ? ` · ${t("top.busy")}` : ""}
        </span>
      </p>
      <div className="topbar__spacer" />
      {requested ? <span className="chip" aria-label={t("top.duration")}>{t("top.duration")} <span className="num">{clock(requested)}</span></span> : null}
      <button type="button" className="btn" onClick={onSettings}><GearIcon />{t("top.settings")}</button>
    </header>
  );
}
