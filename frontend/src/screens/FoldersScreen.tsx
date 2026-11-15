// Step 2 of the flow (section 2): the folders under the media root, with the state of their project.
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { FolderEntry } from "../api/types";
import { FolderIcon } from "../components/icons";
import { ErrorNotice, Spinner } from "../components/ui";
import { useI18n, type MessageKey } from "../i18n";

export function FolderList({ folders, onOpen, busy }: { folders: FolderEntry[]; onOpen: (folder: FolderEntry) => void; busy: string | null }) {
  const { t } = useI18n();
  if (folders.length === 0) return <p className="empty">{t("folders.empty")}</p>;
  return (
    <ul className="folders" aria-label={t("folders.list")}>
      {folders.map((folder) => {
        const name = folder.path === "." ? t("folders.root") : folder.path;
        return (
          <li key={folder.id}>
            <button type="button" className="folder-row" disabled={busy !== null} onClick={() => onOpen(folder)}
              aria-label={`${t("folders.open")}: ${name}. ${t("folders.counts", { photos: folder.photos, videos: folder.videos })}. ${t(`state.${folder.state}` as MessageKey)}`}>
              <FolderIcon />
              <span className="folder-row__name">{name}</span>
              <span className="folder-row__counts">{t("folders.counts", { photos: folder.photos, videos: folder.videos })}</span>
              <span className={`badge badge--${folder.state}`}>{t(`state.${folder.state}` as MessageKey)}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function FoldersScreen({ onOpened }: { onOpened: (projectId: string) => void }) {
  const { t } = useI18n();
  const [folders, setFolders] = useState<FolderEntry[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [opening, setOpening] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.folders().then((response) => alive && setFolders(response.folders), (failure) => alive && setError(failure));
    return () => { alive = false; };
  }, []);

  const open = (folder: FolderEntry) => {
    setOpening(folder.id);
    setError(null);
    api.openProject(folder.path).then((project) => onOpened(project.id), (failure) => {
      setError(failure);
      setOpening(null);
    });
  };

  return (
    <main className="screen" id="content">
      <h1>{t("folders.title")}</h1>
      <p className="screen__intro">{t("folders.intro")}</p>
      <ErrorNotice error={error} />
      {folders === null && !error ? <Spinner label={t("app.loading")} /> : null}
      {folders ? <FolderList folders={folders} onOpen={open} busy={opening} /> : null}
    </main>
  );
}
