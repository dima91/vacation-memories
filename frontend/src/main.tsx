import { createRoot } from "react-dom/client";
import "@fontsource/bricolage-grotesque/latin-600.css";
import "@fontsource/bricolage-grotesque/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "./styles.css";
import { App } from "./App";
import { I18nProvider } from "./i18n";

const root = document.getElementById("root");
if (!root) throw new Error("the page has no #root");
// Ctrl+wheel and pinch would zoom the whole page; the timeline uses them to zoom itself.
window.addEventListener("wheel", (event) => event.ctrlKey && event.preventDefault(), { passive: false });
createRoot(root).render(
  <I18nProvider>
    <App />
  </I18nProvider>,
);
