// Numbers, durations and dates for the language catalog's locale (D10): `Intl`, never hand-made separators.

export interface Formatter {
  number(value: number, digits?: number): string;
  seconds(value: number, digits?: number): string; // "3,5 s"
  score(value: number | null): string; // "0,87", or a dash
  date(iso: string): string;
  dateTime(iso: string): string;
  bytes(value: number): string;
}

export const clock = (seconds: number): string => {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
};

export function makeFormatter(locale: string, secondsUnit: string): Formatter {
  const numbers = new Map<number, Intl.NumberFormat>();
  const numberFormat = (digits: number) => {
    let format = numbers.get(digits);
    if (!format) {
      format = new Intl.NumberFormat(locale, { minimumFractionDigits: digits, maximumFractionDigits: digits });
      numbers.set(digits, format);
    }
    return format;
  };
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium" });
  const dateTimeFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
  const parse = (iso: string): Date | null => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  return {
    number: (value, digits = 0) => numberFormat(digits).format(value),
    seconds: (value, digits = 1) => `${numberFormat(digits).format(value)} ${secondsUnit}`,
    score: (value) => (value === null ? "–" : numberFormat(2).format(value)),
    date: (iso) => { const date = parse(iso); return date ? dateFormat.format(date) : iso; },
    dateTime: (iso) => { const date = parse(iso); return date ? dateTimeFormat.format(date) : iso; },
    bytes: (value) => {
      const units = ["B", "KB", "MB", "GB"];
      let size = value;
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
      return `${numberFormat(unit === 0 ? 0 : 1).format(size)} ${units[unit]}`;
    },
  };
}
