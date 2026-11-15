// The plan (schema 2, section 8 of UI-SPEC.md) and the API (section 9) in one module. `tests/fixtures/editor/` holds
// real answers of the server, and `test/types.test.ts` checks these types against them.

export type FolderState = "not_analyzed" | "incomplete" | "analyzed" | "plan_edited" | "rendered";

export interface FolderEntry {
  id: string;
  path: string; // relative to the media root; "." is the root itself
  photos: number;
  videos: number;
  project: boolean;
  state: FolderState;
}

export interface FoldersResponse {
  folders: FolderEntry[];
}

export type Structure = "narrative" | "chronological";

export interface Settings {
  ai: boolean | null; // null: with the AI if it is available
  ai_time_budget: number | null;
  ai_frames: number | null;
  assume_tz: string | null;
  duration: string;
  structure: Structure;
  aspect: string;
  resolution: string;
  music: string[];
  jobs: string | number;
  hwaccel: string;
  nice: number;
}

export type Requirement = "analysis" | "plan" | "render" | "music";

export interface SettingsSaved {
  settings: Settings;
  requires: Requirement[];
}

export interface AiCoverage {
  model: string;
  mode: string;
  limit: number;
  candidates: number;
  selected: number;
  analyzed: number;
  from_cache: number;
  failed: number;
  skipped_by_budget: number;
  skipped_after_failures: number;
  aborted: boolean;
  seconds_per_frame: number | null;
  placement: string;
}

export interface AiUsage {
  used: boolean;
  coverage: AiCoverage | null;
}

export interface AnalysisInfo {
  present: boolean;
  complete: boolean;
  files: { total: number; analyzed: number; new_or_changed: number | null; removed: number };
  ai: AiUsage | null;
  generated_at: string | null;
}

export interface PlanInfo {
  present: boolean;
  edited: boolean;
  valid: boolean;
  revision: string | null;
  error: string | null;
}

export interface PreviewInfo {
  present: boolean;
  name: string | null;
  outdated: boolean;
}

export interface ExportEntry {
  name: string;
  size: number;
  modified: string;
  partial: boolean;
}

export type JobType = "plan" | "preview" | "export";
export type JobState = "running" | "stopping" | "done" | "failed" | "interrupted";

export interface ExitInfo {
  code: number;
  key: string;
  message: string;
  detail: string | null;
}

export interface JobInfo {
  id: string;
  type: JobType;
  name: string | null;
  state: JobState;
  started_at: string;
  finished_at: string | null;
  exit: ExitInfo | null;
}

export interface ProjectState {
  id: string;
  folder: string;
  settings: Settings;
  analysis: AnalysisInfo;
  plan: PlanInfo;
  preview: PreviewInfo;
  exports: ExportEntry[];
  job: JobInfo | null;
  busy: boolean;
}

export interface OpenedProject {
  id: string;
  folder: string;
  state: FolderState;
}

export interface Progress {
  state: string;
  phase: string | null;
  done: number | null;
  total: number | null;
  eta_seconds: number | null;
  started_at: string | null;
  updated_at: string | null;
}

export interface StatusResponse {
  job: JobInfo | null;
  progress: Progress | null;
  busy: boolean;
}

export interface AiStatus {
  available: boolean;
  reachable: boolean;
  model: string;
  model_present: boolean;
  reason: "ollama_unreachable" | "model_missing" | null;
}

// --- the plan ---

export type Origin = "planner" | "user";
export type EffectType = "fade_in" | "fade_out" | "dissolve" | "dip";
export type ElementType = "photo" | "video";

export interface Effect {
  id: string;
  type: EffectType;
  offset: number; // seconds from the start of the clip
  duration: number;
  origin: Origin;
}

export interface Score {
  final: number | null;
  technical_score: number;
  narrative_score: number | null;
  components: Record<string, number> | null;
}

export interface PlanElement {
  id: string;
  origin: Origin;
  source: string;
  type: ElementType;
  start?: number;
  end?: number;
  duration: number;
  duration_origin?: Origin | null;
  score?: Score;
  moment_type?: string;
  category?: string;
  section?: string;
  description?: string;
  reason?: string;
  captured_at?: string;
  date_source?: string;
  assumed_tz?: string;
  effects: Effect[];
  timestamp?: number; // where the clip starts in the video, computed by the server
}

export interface Exclusion {
  source: string;
  start: number | null;
  end: number | null;
  reason: string;
}

export interface MusicPart {
  start: number;
  end: number;
  muted: boolean;
  fade_in: number;
  fade_out: number;
}

export interface MusicTrack {
  file: string; // relative to the music root
  parts: MusicPart[];
}

export interface MusicPlan {
  tracks: MusicTrack[];
  offset: number;
}

export interface Plan {
  schema_version: 2;
  requested_duration: number | null;
  planned_duration: number | null;
  output: { width: number; height: number; fps: number };
  assumed_tz: string | null;
  runtime?: Record<string, unknown>;
  elements: PlanElement[];
  excluded: Exclusion[];
  music?: MusicPlan;
}

// --- limits, computed by the server (plan_rules) ---

export interface Limits {
  min: number;
  max: number;
  step: number;
}

export interface TrimLimits {
  start_min: number;
  end_max: number | null;
  min_length: number;
}

export interface EffectRoom {
  type: EffectType;
  windows: [number, number][]; // stretches of the clip, in seconds from its start
}

export interface ElementLimits {
  kind: ElementType;
  duration: Limits | null;
  trim: TrimLimits | null;
  effects: Record<string, EffectRoom>;
  available: Record<EffectType, EffectRoom>;
}

export interface PlanLimits {
  min_effect_seconds: number;
  elements: Record<string, ElementLimits>;
  music: { offset: Limits; min_part: number } | null;
}

export interface PlanWarning {
  code: string;
  message: string;
  element?: string;
}

export interface ElementRefs {
  clip: string; // opaque id of the per-clip preview
  media: string; // opaque id of the file (thumbnail)
}

export interface PlanDocument {
  revision: string;
  plan: Plan;
  limits: PlanLimits;
  duration: number;
  warnings: PlanWarning[];
  refs: Record<string, ElementRefs>;
}

// --- the catalog ---

export type CandidateStatus = "selected" | "excluded" | "discarded" | "available";

export interface CatalogItem {
  id: string;
  source: string;
  media: string;
  status: CandidateStatus;
  discard: { reason: string; detail: string | null } | null;
  score: Score;
  moment_type: string | null;
  category: string | null;
  narrative_value: number | null;
  description: string | null;
  captured_at: string;
  day: string;
  group: { representative: string; size: number } | null;
  start?: number;
  end?: number;
  thumb_time?: number;
}

export interface CatalogVideo {
  media: string;
  source: string;
  duration: number | null;
  readable: boolean;
  moments: CatalogItem[];
}

export interface LibraryTransition {
  type: EffectType;
  position: "start" | "end" | "anywhere";
  default_seconds: number;
  min_seconds: number;
}

export interface MusicLibraryTrack {
  id: string;
  file: string;
}

export interface Catalog {
  analysis: { generated_at: string | null; counts: Record<string, number> | null; ai: AiUsage | null } | null;
  photos: CatalogItem[];
  videos: CatalogVideo[];
  transitions: LibraryTransition[];
  music: { tracks: MusicLibraryTrack[] };
  revision: string | null;
}

export interface Waveform {
  duration: number;
  peaks: number[];
}

export interface ApiErrorBody {
  error: { code: string; message: string; field?: string; [detail: string]: unknown };
}
