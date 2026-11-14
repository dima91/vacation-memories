// The editor's JSON API (section 9). Same origin: the browser sends the `Origin` and the `Host` the server checks.
import type {
  AiStatus, ApiErrorBody, Catalog, FoldersResponse, OpenedProject, PlanDocument, ProjectState, Settings,
  SettingsSaved, StatusResponse, Waveform,
} from "./types";

export class ApiFailure extends Error {
  readonly status: number;
  readonly code: string;
  readonly field: string | null;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, field: string | null = null, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.field = field;
    this.details = details;
  }
}

export function failureFrom(status: number, body: unknown): ApiFailure {
  const error = (body as Partial<ApiErrorBody> | null)?.error;
  if (error && typeof error.code === "string") {
    const { code, message, field, ...details } = error;
    return new ApiFailure(status, code, String(message ?? code), typeof field === "string" ? field : null, details);
  }
  return new ApiFailure(status, "unknown", `request failed (${status})`);
}

async function request<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const init: RequestInit = { method, headers: { ...headers }, cache: "no-store" };
  if (method !== "GET") {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body ?? {});
  }
  let response: Response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiFailure(0, "network", "the editor does not answer");
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) throw failureFrom(response.status, data);
  return data as T;
}

const project = (id: string) => `/projects/${encodeURIComponent(id)}`;

export const api = {
  ai: () => request<AiStatus>("GET", "/ai"),
  folders: () => request<FoldersResponse>("GET", "/folders"),
  openProject: (folder: string) => request<OpenedProject>("POST", "/projects", { folder }),
  project: (id: string) => request<ProjectState>("GET", project(id)),
  saveSettings: (id: string, changes: Partial<Settings>) => request<SettingsSaved>("PUT", `${project(id)}/settings`, changes),
  plan: (id: string) => request<PlanDocument>("GET", `${project(id)}/plan`),
  catalog: (id: string) => request<Catalog>("GET", `${project(id)}/catalog`),
  waveform: (id: string, track: string) => request<Waveform>("GET", `${project(id)}/waveform/${track}`),
  startPlan: (id: string, ai?: boolean) => request<unknown>("POST", `${project(id)}/jobs`, ai === undefined ? { type: "plan" } : { type: "plan", ai }),
  status: (id: string) => request<StatusResponse>("GET", `${project(id)}/status`),
  cancel: (id: string) => request<{ stopping: boolean }>("POST", `${project(id)}/jobs/cancel`),
};

// URLs of the media: opaque ids only, never a file name (section 9).
export const urls = {
  thumb: (id: string, media: string, seconds?: number) =>
    `/api${project(id)}/thumb/${media}${seconds === undefined ? "" : `?t=${seconds}`}`,
  clip: (id: string, element: string) => `/api${project(id)}/clip/${element}`,
  video: (id: string, name: string) => `/api${project(id)}/video/${encodeURIComponent(name)}`,
};
