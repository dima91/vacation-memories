// Small shared pieces: errors, the progress bar, labelled fields.
import type { ReactNode } from "react";
import { ApiFailure } from "../api/client";
import { errorText, useI18n } from "../i18n";
import { AlertIcon } from "./icons";

export function ErrorNotice({ error, className = "" }: { error: unknown; className?: string }) {
  const i18n = useI18n();
  const { t } = i18n;
  if (!error) return null;
  let text = t("app.unknownError");
  let detail: string | null = null;
  if (error instanceof ApiFailure) {
    text = errorText(i18n, error.code, error.message);
    // The CLI's own message (English) says what is wrong with a setting or the plan: it is shown after the text.
    if (["invalid_settings", "invalid_plan", "invalid_job", "invalid_name", "invalid_folder"].includes(error.code)) detail = error.message;
  }
  return (
    <div className={`notice notice--error ${className}`} role="alert">
      <AlertIcon />
      <div>
        <p>{text}</p>
        {detail && detail !== text ? <p className="notice__detail">{detail}</p> : null}
      </div>
    </div>
  );
}

export function ProgressBar({ value, max, label }: { value: number; max: number; label: string }) {
  const fraction = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  return (
    <div className="bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max || 1}
      aria-valuenow={max > 0 ? Math.min(value, max) : 0}>
      <div className="bar__fill" style={{ width: `${fraction * 100}%` }} />
    </div>
  );
}

export function Field({ id, label, hint, error, children }: { id: string; label: string; hint?: string; error?: string | null; children: ReactNode }) {
  return (
    <div className={`field${error ? " field--error" : ""}`}>
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <p className="field__hint" id={`${id}-hint`}>{hint}</p> : null}
      {error ? <p className="field__error" id={`${id}-error`}>{error}</p> : null}
    </div>
  );
}

export function Spinner({ label }: { label: string }) {
  return <p className="loading" role="status">{label}</p>;
}
