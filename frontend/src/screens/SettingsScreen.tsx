// Settings (section 7): shown before the first analysis and reachable later from the top bar. Every field is a CLI
// option with the CLI's validation: the server answers with its message and the field it concerns.
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiFailure, api } from "../api/client";
import type { AiStatus, Catalog, ProjectState, Requirement, Settings, Structure } from "../api/types";
import { ErrorNotice, Field } from "../components/ui";
import { useI18n, type MessageKey } from "../i18n";

const ASPECTS = ["16:9", "9:16", "1:1", "4:3"] as const;
const RESOLUTIONS = ["480p", "720p", "1080p", "1440p", "4k"] as const;
type AiChoice = "auto" | "yes" | "no";

interface Form {
  ai: AiChoice;
  aiTimeBudget: string;
  aiFrames: string;
  assumeTz: string;
  duration: string;
  structure: Structure;
  aspect: string;
  resolution: string;
  music: string[];
  jobs: string;
  hwaccel: string;
  nice: string;
  autoPreview: boolean;
}

export function formFrom(settings: Settings): Form {
  return {
    ai: settings.ai === null ? "auto" : settings.ai ? "yes" : "no",
    aiTimeBudget: settings.ai_time_budget === null ? "" : String(settings.ai_time_budget),
    aiFrames: settings.ai_frames === null ? "" : String(settings.ai_frames),
    assumeTz: settings.assume_tz ?? "",
    duration: settings.duration,
    structure: settings.structure,
    aspect: settings.aspect,
    resolution: settings.resolution,
    music: [...settings.music],
    jobs: String(settings.jobs),
    hwaccel: settings.hwaccel,
    nice: String(settings.nice),
    autoPreview: settings.auto_preview,
  };
}

const numberOrNull = (text: string): number | null => (text.trim() === "" ? null : Number(text));

export function settingsFrom(form: Form): Settings {
  const jobs = form.jobs.trim() === "" ? "auto" : /^\d+$/.test(form.jobs.trim()) ? Number(form.jobs.trim()) : form.jobs.trim();
  return {
    ai: form.ai === "auto" ? null : form.ai === "yes",
    ai_time_budget: numberOrNull(form.aiTimeBudget),
    ai_frames: numberOrNull(form.aiFrames),
    assume_tz: form.assumeTz.trim() === "" ? null : form.assumeTz.trim(),
    duration: form.duration.trim(),
    structure: form.structure,
    aspect: form.aspect,
    resolution: form.resolution,
    music: form.music,
    jobs,
    hwaccel: form.hwaccel.trim(),
    nice: Number(form.nice),
    auto_preview: form.autoPreview,
  };
}

interface Props {
  project: ProjectState;
  /** Before the first analysis the button starts it; later the settings are only saved. */
  mode: "analyze" | "edit";
  onSaved: () => void;
  onStarted: () => void;
  onBack?: () => void;
}

/** The confirmation before a new plan replaces the current one (section 5): what is kept, and that a copy is made. */
export function RegenerateDialog({ edited, working, onConfirm, onCancel }: { edited: boolean; working: boolean; onConfirm: () => void; onCancel: () => void }) {
  const { t } = useI18n();
  return (
    <div className="notice notice--warn" role="alertdialog" aria-labelledby="regenerate-title" aria-describedby="regenerate-body">
      <div>
        <p id="regenerate-title"><strong>{t("regenerate.title")}</strong></p>
        <p id="regenerate-body">{edited ? `${t("regenerate.body")} ${t("regenerate.bodyEdited")}` : t("regenerate.body")}</p>
        <div className="actions actions--tight">
          <button type="button" className="btn btn--primary" disabled={working} autoFocus onClick={onConfirm}>{t("regenerate.confirm")}</button>
          <button type="button" className="btn" onClick={onCancel}>{t("regenerate.cancel")}</button>
        </div>
      </div>
    </div>
  );
}

export function SettingsForm({ project, mode, onSaved, onStarted, onBack, ai, tracks, initial }: Props & { ai: AiStatus | null; tracks: string[]; initial?: Form }) {
  const { t, languages, language, setLanguage } = useI18n();
  const [form, setForm] = useState<Form>(() => initial ?? formFrom(project.settings));
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<Requirement[] | null>(null);
  const [working, setWorking] = useState(false);
  const [choice, setChoice] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const set = <K extends keyof Form>(key: K, value: Form[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setSaved(null);
  };
  const fieldError = (field: string): string | null => (error instanceof ApiFailure && error.field === field ? error.message : null);
  const incomplete = project.analysis.present && !project.analysis.complete;
  const analyzeLabel = incomplete ? t("settings.complete") : t("settings.analyze");

  const save = async (): Promise<boolean> => {
    setError(null);
    try {
      const response = await api.saveSettings(project.id, settingsFrom(form));
      setSaved(response.requires);
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    }
  };

  const start = async (withAi?: boolean) => {
    setWorking(true);
    setChoice(false);
    setConfirming(false);
    try {
      if (withAi === undefined && !(await save())) return;
      await api.startPlan(project.id, withAi);
      onStarted();
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.code === "ai_unavailable") setChoice(true);
      else setError(failure);
    } finally {
      setWorking(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (mode === "analyze") await start();
    else {
      setWorking(true);
      const ok = await save();
      setWorking(false);
      if (ok) onSaved();
    }
  };

  const aiNote = useMemo(() => {
    if (!ai) return t("settings.ai.checking");
    return ai.available ? t("settings.ai.available") : t(`settings.ai.${ai.reason ?? "ollama_unreachable"}` as MessageKey);
  }, [ai, t]);

  const toggleTrack = (file: string) =>
    set("music", form.music.includes(file) ? form.music.filter((name) => name !== file) : [...form.music, file]);

  return (
    <form className="settings" onSubmit={submit} noValidate>
      <ErrorNotice error={error} />
      {incomplete ? (
        <p className="notice notice--info">
          {t("settings.analysisIncomplete")} {t("settings.filesAnalyzed", { analyzed: project.analysis.files.analyzed, total: project.analysis.files.total })}
        </p>
      ) : null}

      <fieldset>
        <legend>{t("settings.analysis")}</legend>
        <Field id="s-ai" label={t("settings.ai")} hint={`${aiNote}${ai ? ` ${t("settings.ai.model", { model: ai.model })}` : ""}`}>
          <select id="s-ai" value={form.ai} onChange={(event) => set("ai", event.target.value as AiChoice)} aria-describedby="s-ai-hint">
            <option value="auto">{t("settings.ai.auto")}</option>
            <option value="yes">{t("settings.ai.yes")}</option>
            <option value="no">{t("settings.ai.no")}</option>
          </select>
        </Field>
        <Field id="s-budget" label={t("settings.aiTimeBudget")} hint={t("settings.aiTimeBudget.hint")} error={fieldError("ai_time_budget")}>
          <input id="s-budget" type="number" min={1} inputMode="numeric" value={form.aiTimeBudget} disabled={form.ai === "no"}
            onChange={(event) => set("aiTimeBudget", event.target.value)} aria-describedby="s-budget-hint" />
        </Field>
        <Field id="s-tz" label={t("settings.assumeTz")} hint={t("settings.assumeTz.hint")} error={fieldError("assume_tz")}>
          <input id="s-tz" type="text" value={form.assumeTz} placeholder="Europe/Rome" onChange={(event) => set("assumeTz", event.target.value)} aria-describedby="s-tz-hint" />
        </Field>
      </fieldset>

      <fieldset>
        <legend>{t("settings.edit")}</legend>
        <Field id="s-duration" label={t("settings.duration")} hint={t("settings.duration.hint")} error={fieldError("duration")}>
          <input id="s-duration" type="text" value={form.duration} onChange={(event) => set("duration", event.target.value)} aria-describedby="s-duration-hint" />
        </Field>
        <Field id="s-structure" label={t("settings.structure")} error={fieldError("structure")}>
          <select id="s-structure" value={form.structure} onChange={(event) => set("structure", event.target.value as Structure)}>
            <option value="narrative">{t("settings.structure.narrative")}</option>
            <option value="chronological">{t("settings.structure.chronological")}</option>
          </select>
        </Field>
        <Field id="s-aspect" label={t("settings.aspect")} error={fieldError("aspect")}>
          <select id="s-aspect" value={form.aspect} onChange={(event) => set("aspect", event.target.value)}>
            {ASPECTS.map((aspect) => <option key={aspect} value={aspect}>{t(`aspect.${aspect}` as MessageKey)}</option>)}
          </select>
        </Field>
        <Field id="s-resolution" label={t("settings.resolution")} error={fieldError("resolution")}>
          <select id="s-resolution" value={form.resolution} onChange={(event) => set("resolution", event.target.value)}>
            {RESOLUTIONS.map((resolution) => <option key={resolution} value={resolution}>{resolution}</option>)}
          </select>
        </Field>
        <div className="field">
          <span className="field__label" id="s-music-label">{t("settings.music")}</span>
          {tracks.length === 0 ? <p className="field__hint">{t("settings.music.none")}</p> : (
            <div role="group" aria-labelledby="s-music-label" className="checks">
              {tracks.map((file, index) => (
                <label key={file} className="check">
                  <input type="checkbox" checked={form.music.includes(file)} onChange={() => toggleTrack(file)} id={`s-music-${index}`} />
                  <span>{file}</span>
                </label>
              ))}
            </div>
          )}
          {fieldError("music") ? <p className="field__error">{fieldError("music")}</p> : null}
        </div>
        <div className="field">
          <label className="check">
            <input type="checkbox" checked={form.autoPreview} id="s-auto-preview" aria-describedby="s-auto-preview-hint"
              onChange={(event) => set("autoPreview", event.target.checked)} />
            <span>{t("settings.autoPreview")}</span>
          </label>
          <p className="field__hint" id="s-auto-preview-hint">{t("settings.autoPreview.hint")}</p>
        </div>
      </fieldset>

      {languages.length > 1 ? (
        <fieldset>
          <legend>{t("app.language")}</legend>
          <Field id="s-language" label={t("app.language")}>
            <select id="s-language" value={language} onChange={(event) => setLanguage(event.target.value)}>
              {languages.map((code) => <option key={code} value={code}>{code}</option>)}
            </select>
          </Field>
        </fieldset>
      ) : null}

      <details className="advanced">
        <summary>{t("settings.advanced")}</summary>
        <Field id="s-frames" label={t("settings.aiFrames")} hint={t("settings.aiFrames.hint")} error={fieldError("ai_frames")}>
          <input id="s-frames" type="number" min={1} inputMode="numeric" value={form.aiFrames} disabled={form.ai === "no"}
            onChange={(event) => set("aiFrames", event.target.value)} aria-describedby="s-frames-hint" />
        </Field>
        <Field id="s-jobs" label={t("settings.jobs")} hint={t("settings.jobs.hint")} error={fieldError("jobs")}>
          <input id="s-jobs" type="text" value={form.jobs} onChange={(event) => set("jobs", event.target.value)} aria-describedby="s-jobs-hint" />
        </Field>
        <Field id="s-hwaccel" label={t("settings.hwaccel")} hint={t("settings.hwaccel.hint")} error={fieldError("hwaccel")}>
          <input id="s-hwaccel" type="text" value={form.hwaccel} onChange={(event) => set("hwaccel", event.target.value)} aria-describedby="s-hwaccel-hint" />
        </Field>
        <Field id="s-nice" label={t("settings.nice")} hint={t("settings.nice.hint")} error={fieldError("nice")}>
          <input id="s-nice" type="number" min={0} max={19} value={form.nice} onChange={(event) => set("nice", event.target.value)} aria-describedby="s-nice-hint" />
        </Field>
      </details>

      {saved ? (
        <p className="notice notice--ok" role="status">
          {t("settings.saved")}{" "}
          {saved.length > 0 ? t("settings.requires", { what: saved.map((kind) => t(`requires.${kind}` as MessageKey)).join(t("requires.separator")) }) : null}
        </p>
      ) : null}

      {saved && (saved.includes("plan") || saved.includes("analysis")) && mode === "edit" ? (
        <p className="notice notice--info" role="status">{t("regenerate.hint")}</p>
      ) : null}

      {confirming ? <RegenerateDialog edited={project.plan.edited} working={working} onConfirm={() => void start()} onCancel={() => setConfirming(false)} /> : null}

      {choice ? (
        <div className="notice notice--warn" role="alertdialog" aria-labelledby="ai-choice-title" aria-describedby="ai-choice-body">
          <div>
            <p id="ai-choice-title"><strong>{t("ai.choice.title")}</strong></p>
            <p id="ai-choice-body">{t("ai.choice.body")}</p>
            <div className="actions">
              <button type="button" className="btn" onClick={() => void start()}>{t("ai.choice.wait")}</button>
              <button type="button" className="btn" onClick={() => void start(false)}>{t("ai.choice.without")}</button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="actions">
        {mode === "analyze" ? (
          <button type="submit" className="btn btn--primary" disabled={working}>{analyzeLabel}</button>
        ) : (
          <>
            <button type="submit" className="btn btn--primary" disabled={working}>{working ? t("settings.saving") : t("settings.save")}</button>
            <button type="button" className="btn" disabled={working || !project.plan.present} onClick={() => setConfirming(true)}>{t("regenerate.button")}</button>
            {incomplete ? <button type="button" className="btn" disabled={working} onClick={() => setConfirming(true)}>{t("settings.complete")}</button> : null}
          </>
        )}
        {onBack ? <button type="button" className="btn" onClick={onBack}>{t("settings.backToEditor")}</button> : null}
      </div>
    </form>
  );
}

export function SettingsScreen(props: Props) {
  const { t } = useI18n();
  const [ai, setAi] = useState<AiStatus | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);

  useEffect(() => {
    let alive = true;
    api.ai().then((status) => alive && setAi(status), () => alive && setAi(null));
    api.catalog(props.project.id).then((value) => alive && setCatalog(value), () => alive && setCatalog(null));
    return () => { alive = false; };
  }, [props.project.id]);

  return (
    <main className="screen screen--narrow" id="content">
      <h1>{t("settings.title")}</h1>
      <p className="screen__intro">{t("settings.intro")}</p>
      <p className="screen__sub">{t("settings.folder", { folder: props.project.folder === "." ? t("folders.root") : props.project.folder })}</p>
      <SettingsForm {...props} ai={ai} tracks={catalog?.music.tracks.map((track) => track.file) ?? []} />
    </main>
  );
}
