// Language catalogs and the `t` function (D10). Only the Italian catalog exists now; with more than one a language
// selector appears and the choice is saved in the browser (Italian when nothing is saved, no detection).
import { createContext, createElement, useContext, useMemo, useState, type ReactNode } from "react";
import { makeFormatter, type Formatter } from "../lib/format";
import { it, type MessageKey, type Messages } from "./it";

export type { MessageKey, Messages };
export type Params = Record<string, string | number>;

export const catalogs: Record<string, Messages> = { it };
export const DEFAULT_LANGUAGE = "it";
const STORAGE_KEY = "vm.language";

/** The keys of the plural messages: `x.one` and `x.other` are written as `plural("x", n)`. */
export type PluralKey = Extract<MessageKey, `${string}.other`> extends `${infer Base}.other` ? Base : never;

export function interpolate(text: string, params?: Params): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name: string) => (name in params ? String(params[name]) : whole));
}

export interface Translator {
  language: string;
  languages: string[];
  setLanguage(language: string): void;
  t(key: MessageKey, params?: Params): string;
  plural(key: PluralKey, count: number, params?: Params): string;
  has(key: string): boolean;
  format: Formatter;
}

export function makeTranslator(language: string, setLanguage: (language: string) => void = () => undefined): Translator {
  const messages = catalogs[language] ?? it;
  const rules = new Intl.PluralRules(messages["meta.locale"]);
  const t = (key: MessageKey, params?: Params) => interpolate(messages[key] ?? it[key], params);
  return {
    language,
    languages: Object.keys(catalogs),
    setLanguage,
    t,
    plural: (key, count, params) => {
      const form = rules.select(count) === "one" ? "one" : "other";
      return t(`${key}.${form}` as MessageKey, { n: count, ...params });
    },
    has: (key) => key in messages,
    format: makeFormatter(messages["meta.locale"], messages["meta.seconds"]),
  };
}

function storedLanguage(): string {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved && saved in catalogs) return saved;
  } catch {
    // no storage (private window, blocked data): the default
  }
  return DEFAULT_LANGUAGE;
}

const I18nContext = createContext<Translator>(makeTranslator(DEFAULT_LANGUAGE));

export function I18nProvider({ children, language }: { children: ReactNode; language?: string }) {
  const [current, setCurrent] = useState(() => language ?? (typeof window === "undefined" ? DEFAULT_LANGUAGE : storedLanguage()));
  const translator = useMemo(() => makeTranslator(current, (next) => {
    if (!(next in catalogs)) return;
    setCurrent(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // not saved: it lasts until the page is closed
    }
  }), [current]);
  return createElement(I18nContext.Provider, { value: translator }, children);
}

export const useI18n = (): Translator => useContext(I18nContext);

/** The text of a server error: the catalog's for a known code, the server's English message otherwise (D10). */
export function errorText(translator: Translator, code: string, message: string): string {
  const key = `error.${code}`;
  return translator.has(key) ? translator.t(key as MessageKey) : message;
}

/** The text of a job's exit (section 6), by its stable key; the CLI's own message when the key is unknown. */
export function exitText(translator: Translator, key: string, message: string): string {
  const full = `exit.${key}`;
  return translator.has(full) ? translator.t(full as MessageKey) : message;
}
