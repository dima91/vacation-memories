// What the tests share: the project the setup prepared, and the plan the editor holds, read and written like the page does.
import { readFileSync } from "node:fs";
import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect } from "@playwright/test";

export const STATE_FILE = "/tmp/e2e-state.json";
export const FOLDER = "e2e-vacation";

export interface Element {
  id: string;
  type: "photo" | "video";
  source: string;
  duration: number;
  start?: number;
  end?: number;
  origin: string;
  duration_origin?: string;
  effects: { id: string; type: string; offset: number; duration: number; origin: string }[];
}

export interface MusicPart {
  start: number;
  end: number;
  muted: boolean;
  fade_in: number;
  fade_out: number;
}

export interface Music {
  tracks: { file: string; parts: MusicPart[] }[];
  offset: number;
}

export interface Plan {
  elements: Element[];
  excluded: { source: string; start: number | null; end: number | null; reason: string }[];
  music?: Music;
  [key: string]: unknown;
}

export interface State {
  project: string;
  plan: Plan;
}

export const state = (): State => JSON.parse(readFileSync(STATE_FILE, "utf8")) as State;

export async function readPlan(request: APIRequestContext, project: string): Promise<{ plan: Plan; revision: string }> {
  const response = await request.get(`/api/projects/${project}/plan`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as { plan: Plan; revision: string };
}

export async function writePlan(request: APIRequestContext, project: string, plan: Plan, revision: string): Promise<void> {
  const response = await request.put(`/api/projects/${project}/plan`, { data: plan, headers: { "If-Match": revision } });
  expect(response.ok(), await response.text()).toBeTruthy();
}

/** The plan the setup made, put back: every test starts from the same one. */
export async function resetPlan(request: APIRequestContext): Promise<void> {
  const { project, plan } = state();
  const { revision } = await readPlan(request, project);
  await writePlan(request, project, plan, revision);
}

/** Opens the editor on the project and waits for the clips. */
export async function openEditor(page: Page): Promise<void> {
  await page.goto(`/#/p/${state().project}`);
  await expect(clips(page).first()).toBeVisible();
}

export const clips = (page: Page): Locator => page.getByRole("list", { name: "Clip del video" }).getByRole("listitem");
export const clipButtons = (page: Page): Locator => page.getByRole("list", { name: "Clip del video" }).getByRole("button", { name: /^Clip \d+:/ });
export const photoClips = (page: Page): Locator => page.getByRole("list", { name: "Clip del video" }).getByRole("button", { name: /^Clip \d+: foto/ });
export const saveStatus = (page: Page): Locator => page.locator(".topbar .save");

/** Waits until the editor says everything is saved, and the server has it. */
export async function saved(page: Page): Promise<void> {
  await expect(saveStatus(page)).toHaveText("Salvato");
}

export const basename = (path: string): string => path.split("/").pop() ?? path;

/**
 * Drags an element with the mouse to a point of the page, in steps, the way a hand does. (`Locator.dragTo` waits for
 * the target to be a click target and hangs on the narrow clips of the timeline; the page only needs the events.)
 */
export async function dragTo(page: Page, source: Locator, x: number, y: number): Promise<void> {
  await source.scrollIntoViewIfNeeded();
  const box = (await source.boundingBox())!;
  const fromX = box.x + box.width / 2;
  const fromY = box.y + box.height / 2;
  await page.mouse.move(fromX, fromY);
  await page.mouse.down();
  await page.mouse.move(fromX + 10, fromY + 4, { steps: 3 }); // far enough for the browser to start the drag
  await page.mouse.move(x, y, { steps: 12 });
  await page.mouse.up();
}

/** Opens every group of the photos (the ones the analysis discarded start closed). */
export async function expandPhotos(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Espandi tutto" }).click();
  await expect(page.locator("li.thumbcell[draggable='true']").first()).toBeVisible();
}

/** The transitions on the clips of the timeline (the buttons that select them). */
export const effectButtons = (page: Page): Locator => page.locator(".clip .clip__fx");

/** The parts of the music on the music lane. */
export const musicParts = (page: Page): Locator => page.getByRole("list", { name: "Parti musicali" }).getByRole("listitem");

/** The tone the music root of the tests has, as the plan names it (`make_media.py`). */
export const TRACK_A = "tone-a.wav";
export const TRACK_B = "tone-b.wav";

/** Opens the Music tab and adds a track to the video with the button of its row (the button waits for the waveform that gives its length). */
export async function addTrack(page: Page, file: string): Promise<void> {
  await page.getByRole("tab", { name: /^Musica/ }).click();
  const button = page.getByRole("button", { name: `Aggiungi al video: ${file}` });
  await expect(button).toBeEnabled();
  await button.click();
}
