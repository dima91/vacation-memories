// Runtime shapes for the API types. Each shape is a `Record<keyof T, …>`: adding a key to a type in
// `src/api/types.ts` without describing it here is a `tsc` error, and a fixture with a key the type does not
// have, or with a value of the wrong kind, fails the test. So the types, the shared fixtures
// (`tests/fixtures/editor/`) and the server (checked by `tests/test_editor_fixtures.py`) stay in agreement.
import type {
  AiCoverage, AiStatus, AiUsage, AnalysisInfo, Catalog, CatalogItem, CatalogVideo, Effect, EffectRoom, ElementLimits, ElementRefs,
  ExitInfo, Exclusion, FolderEntry, FoldersResponse, JobInfo, LibraryTransition, Limits, MusicLibraryTrack, MusicPart, MusicPlan, MusicTrack,
  Plan, PlanDocument, PlanElement, PlanInfo, PlanLimits, PlanWarning, PreviewInfo, Progress, ProjectState, Score, Settings, StatusResponse,
  TrimLimits, Waveform,
} from "../src/api/types";

export type Check = (value: unknown, path: string) => string[];
export interface Spec { check: Check; optional: boolean }

const kind = (name: string, test: (value: unknown) => boolean): Spec => ({
  optional: false,
  check: (value, path) => (test(value) ? [] : [`${path}: expected ${name}, got ${JSON.stringify(value)?.slice(0, 40)}`]),
});
export const str = kind("a string", (value) => typeof value === "string");
export const num = kind("a number", (value) => typeof value === "number" && Number.isFinite(value));
export const bool = kind("a boolean", (value) => typeof value === "boolean");
export const any: Spec = { optional: false, check: () => [] };
export const oneOf = (...values: readonly (string | number)[]): Spec => kind(`one of ${values.join(", ")}`, (value) => values.includes(value as string));
export const opt = (spec: Spec): Spec => ({ ...spec, optional: true });
export const nullable = (spec: Spec): Spec => ({ optional: spec.optional, check: (value, path) => (value === null ? [] : spec.check(value, path)) });
export const arr = (spec: Spec): Spec => ({
  optional: false,
  check: (value, path) => (Array.isArray(value) ? value.flatMap((item, index) => spec.check(item, `${path}[${index}]`)) : [`${path}: expected an array`]),
});
export const rec = (spec: Spec): Spec => ({
  optional: false,
  check: (value, path) => (value !== null && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value).flatMap(([key, item]) => spec.check(item, `${path}.${key}`)) : [`${path}: expected an object`]),
});
export const tuple2: Spec = {
  optional: false,
  check: (value, path) => (Array.isArray(value) && value.length === 2 && value.every((item) => typeof item === "number") ? [] : [`${path}: expected [number, number]`]),
};

export type Shape<T> = { [K in keyof T]-?: Spec };
export const obj = <T>(shape: Shape<T>): Spec => ({
  optional: false,
  check: (value, path) => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return [`${path}: expected an object`];
    const record = value as Record<string, unknown>;
    const specs = shape as Record<string, Spec>;
    const problems: string[] = [];
    for (const key of Object.keys(record)) if (!(key in specs)) problems.push(`${path}.${key}: the type has no such key`);
    for (const [key, spec] of Object.entries(specs)) {
      if (!(key in record)) {
        if (!spec.optional) problems.push(`${path}.${key}: missing`);
        continue;
      }
      problems.push(...spec.check(record[key], `${path}.${key}`));
    }
    return problems;
  },
});

const origin = oneOf("planner", "user");
const effectType = oneOf("fade_in", "fade_out", "dissolve", "dip");
const limits = obj<Limits>({ min: num, max: num, step: num });
const score = obj<Score>({ final: nullable(num), technical_score: num, narrative_score: nullable(num), components: nullable(rec(num)) });
const coverage = obj<AiCoverage>({
  model: str, mode: str, limit: num, candidates: num, selected: num, analyzed: num, from_cache: num, failed: num, skipped_by_budget: num,
  skipped_after_failures: num, aborted: bool, seconds_per_frame: nullable(num), placement: str,
});
const aiUsage = obj<AiUsage>({ used: bool, coverage: nullable(coverage) });

export const effect = obj<Effect>({ id: str, type: effectType, offset: num, duration: num, origin });
export const planElement = obj<PlanElement>({
  id: str, origin, source: str, type: oneOf("photo", "video"), start: opt(num), end: opt(num), duration: num,
  duration_origin: opt(nullable(origin)), score: opt(score), moment_type: opt(str), category: opt(str), section: opt(str),
  description: opt(str), reason: opt(str), captured_at: opt(str), date_source: opt(str), assumed_tz: opt(str),
  effects: arr(effect), timestamp: opt(num),
});
const exclusion = obj<Exclusion>({ source: str, start: nullable(num), end: nullable(num), reason: str });
const musicPart = obj<MusicPart>({ start: num, end: num, muted: bool, fade_in: num, fade_out: num });
const musicTrack = obj<MusicTrack>({ file: str, parts: arr(musicPart) });
const musicPlan = obj<MusicPlan>({ tracks: arr(musicTrack), offset: num });
export const plan = obj<Plan>({
  schema_version: oneOf(2), requested_duration: nullable(num), planned_duration: nullable(num),
  output: obj({ width: num, height: num, fps: num }), assumed_tz: nullable(str), runtime: opt(any),
  elements: arr(planElement), excluded: arr(exclusion), music: opt(musicPlan),
});
const effectRoom = obj<EffectRoom>({ type: effectType, windows: arr(tuple2) });
const trim = obj<TrimLimits>({ start_min: num, end_max: nullable(num), min_length: num });
const elementLimits = obj<ElementLimits>({
  kind: oneOf("photo", "video"), duration: nullable(limits), trim: nullable(trim), effects: rec(effectRoom),
  available: obj({ fade_in: effectRoom, fade_out: effectRoom, dissolve: effectRoom, dip: effectRoom }),
});
const planLimits = obj<PlanLimits>({ min_effect_seconds: num, elements: rec(elementLimits), music: nullable(obj({ offset: limits, min_part: num })) });
const warning = obj<PlanWarning>({ code: str, message: str, element: opt(str) });
const refs = obj<ElementRefs>({ clip: str, media: str });
export const planDocument = obj<PlanDocument>({
  revision: str, plan, limits: planLimits, duration: num, warnings: arr(warning), refs: rec(refs),
});

const catalogItem = obj<CatalogItem>({
  id: str, source: str, media: str, status: oneOf("selected", "excluded", "discarded", "available"),
  discard: nullable(obj({ reason: str, detail: nullable(str) })), score, moment_type: nullable(str), category: nullable(str),
  narrative_value: nullable(num), description: nullable(str), captured_at: str, day: str,
  group: nullable(obj({ representative: str, size: num })), start: opt(num), end: opt(num), thumb_time: opt(num),
});
const catalogVideo = obj<CatalogVideo>({ media: str, source: str, duration: nullable(num), readable: bool, moments: arr(catalogItem) });
const library = obj<LibraryTransition>({ type: effectType, position: oneOf("start", "end", "anywhere"), default_seconds: num, min_seconds: num });
const musicLibraryTrack = obj<MusicLibraryTrack>({ id: str, file: str });
export const catalog = obj<Catalog>({
  analysis: nullable(obj({ generated_at: nullable(str), counts: nullable(rec(num)), ai: nullable(aiUsage) })),
  photos: arr(catalogItem), videos: arr(catalogVideo), transitions: arr(library), music: obj({ tracks: arr(musicLibraryTrack) }), revision: nullable(str),
});
export const waveform = obj<Waveform & { signature: unknown }>({ duration: num, peaks: arr(num), signature: any });

const settings = obj<Settings>({
  ai: nullable(bool), ai_time_budget: nullable(num), ai_frames: nullable(num), assume_tz: nullable(str), duration: str,
  structure: oneOf("narrative", "chronological"), aspect: str, resolution: str, music: arr(str), jobs: any, hwaccel: str, nice: num,
});
const analysis = obj<AnalysisInfo>({
  present: bool, complete: bool, files: obj({ total: num, analyzed: num, new_or_changed: nullable(num), removed: num }),
  ai: nullable(aiUsage), generated_at: nullable(str),
});
const planInfo = obj<PlanInfo>({ present: bool, edited: bool, valid: bool, revision: nullable(str), error: nullable(str) });
const preview = obj<PreviewInfo>({ present: bool, name: nullable(str), outdated: bool });
const exit = obj<ExitInfo>({ code: num, key: str, message: str, detail: nullable(str) });
const job = obj<JobInfo>({
  id: str, type: oneOf("plan", "preview", "export"), name: nullable(str), state: oneOf("running", "stopping", "done", "failed", "interrupted"),
  started_at: str, finished_at: nullable(str), exit: nullable(exit),
});
export const projectState = obj<ProjectState>({
  id: str, folder: str, settings, analysis, plan: planInfo, preview, exports: arr(obj({ name: str, size: num, modified: str, partial: bool })),
  job: nullable(job), busy: bool,
});
const progress = obj<Progress>({ state: str, phase: nullable(str), done: nullable(num), total: nullable(num), eta_seconds: nullable(num), started_at: nullable(str), updated_at: nullable(str) });
export const status = obj<StatusResponse>({ job: nullable(job), progress: nullable(progress), busy: bool });
const folder = obj<FolderEntry>({
  id: str, path: str, photos: num, videos: num, project: bool,
  state: oneOf("not_analyzed", "incomplete", "analyzed", "plan_edited", "rendered"),
});
export const folders = obj<FoldersResponse>({ folders: arr(folder) });
export const aiStatus = obj<AiStatus>({ available: bool, reachable: bool, model: str, model_present: bool, reason: nullable(oneOf("ollama_unreachable", "model_missing")) });

export function problemsOf(spec: Spec, value: unknown, name: string): string[] {
  return spec.check(value, name);
}
