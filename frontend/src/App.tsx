import { useEffect, useState } from "react";
import { useI18n } from "./i18n";
import { FoldersScreen } from "./screens/FoldersScreen";
import { ProjectScreen } from "./screens/ProjectScreen";

/** The project is in the address (`#/p/<id>`): a reload, or the back button, comes back to the same project. */
export function projectFromHash(hash: string): string | null {
  const found = /^#\/p\/([a-z0-9-]+)$/.exec(hash);
  return found?.[1] ?? null;
}

export function App() {
  const { t } = useI18n();
  const [projectId, setProjectId] = useState<string | null>(() => projectFromHash(window.location.hash));

  useEffect(() => {
    const onHash = () => setProjectId(projectFromHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = (id: string) => { window.location.hash = `/p/${id}`; };
  const leave = () => { window.location.hash = ""; };

  return (
    <>
      <a className="skip" href="#content">{t("app.skipToContent")}</a>
      {projectId ? <ProjectScreen projectId={projectId} onLeave={leave} /> : <FoldersScreen onOpened={open} />}
    </>
  );
}
