// Before the tests: the editor answers, the project of the synthetic vacation exists and has a plan. The plan is made again
// by the real job (analysis without AI, 30 seconds), and kept as the baseline every test starts from.
import { request } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { BASE_URL } from "./playwright.config";
import { FOLDER, STATE_FILE, type Plan } from "./helpers";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export default async function globalSetup(): Promise<void> {
  const api = await request.newContext({ baseURL: BASE_URL, extraHTTPHeaders: { Origin: BASE_URL } });
  const get = async <T>(path: string): Promise<T> => {
    const response = await api.get(`/api${path}`);
    if (!response.ok()) throw new Error(`GET ${path}: ${response.status()} ${await response.text()}`);
    return (await response.json()) as T;
  };
  const send = async (method: "POST" | "PUT", path: string, data: unknown, headers: Record<string, string> = {}) => {
    const response = await api.fetch(`/api${path}`, { method, data, headers });
    if (!response.ok()) throw new Error(`${method} ${path}: ${response.status()} ${await response.text()}`);
    return (await response.json()) as Record<string, any>;
  };
  const idle = async (project: string) => {
    for (let waited = 0; waited < 300_000; waited += 1000) {
      const status = await get<{ job: { state: string; exit: { code: number; message: string } | null } | null }>(`/projects/${project}/status`);
      if (!status.job || !["running", "stopping"].includes(status.job.state)) return status.job;
      await sleep(1000);
    }
    throw new Error("the job did not finish in five minutes");
  };

  const folders = await get<{ folders: { path: string }[] }>("/folders");
  if (!folders.folders.some((folder) => folder.path === FOLDER)) {
    throw new Error(`dev-media/${FOLDER} is missing: make it with\n  docker compose run --rm -T dev python frontend/e2e/make_media.py`);
  }
  const { id: project } = await send("POST", "/projects", { folder: FOLDER }) as { id: string };
  await idle(project);
  await send("PUT", `/projects/${project}/settings`, { duration: "0:30", ai: false });
  await send("POST", `/projects/${project}/jobs`, { type: "plan", ai: false });
  const finished = await idle(project);
  if (finished?.state !== "done") throw new Error(`the analysis did not finish: ${JSON.stringify(finished)}`);

  // The baseline: the plan the job made, without what earlier runs hid (the planner keeps those out of it).
  const current = await get<{ plan: Plan; revision: string }>(`/projects/${project}/plan`);
  const plan: Plan = { ...current.plan, excluded: [] };
  await send("PUT", `/projects/${project}/plan`, plan, { "If-Match": current.revision });
  writeFileSync(STATE_FILE, JSON.stringify({ project, plan }));
  await api.dispose();
}
