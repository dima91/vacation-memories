// The bar of the editor: project, state of the analysis and of the jobs, requested duration, settings.
// Undo, redo and the state of the saving come with the editing (U5); "Export video" and the state of the preview with U7.
import type { Catalog, JobType, ProjectState } from "../api/types";
import { useI18n, type MessageKey } from "../i18n";
import { clock } from "../lib/format";
import type { SaveState } from "../lib/session";
import { CheckIcon, AlertIcon, FilmIcon, FolderIcon, GearIcon, RedoIcon, UndoIcon } from "./icons";

export interface EditBar {
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  save: SaveState;
  readOnly: boolean;
}

/** The export button (section 10): off, with the reason, while a job runs, with no plan or while the project is busy. */
export interface ExportButton {
  reason: MessageKey | null;
  onExport: () => void;
}

interface Props {
  project: ProjectState;
  catalog: Catalog | null;
  requested: number | null;
  working: boolean;
  /** The type of the job that runs, to say which one it is. */
  jobType?: JobType | null;
  previewOutdated?: boolean;
  edit?: EditBar;
  exporting?: ExportButton;
  onChangeFolder: () => void;
  onSettings: () => void;
}

export function TopBar({ project, catalog, requested, working, jobType, previewOutdated, edit, exporting, onChangeFolder, onSettings }: Props) {
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
          {working ? ` · ${t(jobType ? `top.job.${jobType}` as MessageKey : "top.job.running")}` : ""}
          {project.busy ? ` · ${t("top.busy")}` : ""}
        </span>
      </p>
      <div className="topbar__spacer" />
      {edit ? (
        <div className="topbar__edit">
          <p className={`save save--${edit.save}`} role="status">{edit.readOnly ? t("top.readOnly") : t(`save.${edit.save}` as MessageKey)}</p>
          <button type="button" className="btn btn--icon" aria-label={t("edit.undoTitle")} title={t("edit.undoTitle")} disabled={!edit.canUndo || edit.readOnly} onClick={edit.onUndo}><UndoIcon /></button>
          <button type="button" className="btn btn--icon" aria-label={t("edit.redoTitle")} title={t("edit.redoTitle")} disabled={!edit.canRedo || edit.readOnly} onClick={edit.onRedo}><RedoIcon /></button>
        </div>
      ) : null}
      {previewOutdated ? <span className="chip chip--warn">{t("top.previewOutdated")}</span> : null}
      {requested ? <span className="chip" aria-label={t("top.duration")}>{t("top.duration")} <span className="num">{clock(requested)}</span></span> : null}
      <button type="button" className="btn" onClick={onSettings}><GearIcon />{t("top.settings")}</button>
      {exporting ? (
        <button type="button" className="btn btn--primary" disabled={exporting.reason !== null}
          title={exporting.reason ? t(exporting.reason) : undefined} onClick={exporting.onExport}>{t("top.export")}</button>
      ) : null}
    </header>
  );
}
