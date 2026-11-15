// The preview and the export of step U7, in a real browser: the page served by nginx, on the plan the job made from a
// synthetic vacation. The renders are real (FFmpeg in the editor container), at 480p so they stay short; the assertions look
// at the page, at the state the server reports and at the file the browser downloads.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { clipButtons, openEditor, photoClips, resetPlan, saved, state } from "../helpers";

test.describe.configure({ mode: "serial" });
test.setTimeout(180_000);

const RENDER = { timeout: 150_000 };
const base = () => `/api/projects/${state().project}`;

interface Project {
  settings: Record<string, unknown>;
  preview: { present: boolean; outdated: boolean; name: string | null };
  exports: { name: string; path: string; partial: boolean }[];
  job: { type: string; state: string } | null;
}

const project = async (request: APIRequestContext): Promise<Project> => (await (await request.get(base())).json()) as Project;
const running = (job: Project["job"]) => job !== null && ["running", "stopping"].includes(job.state);

/** Waits until no job runs, stopping the one a failed test left behind. */
async function idle(request: APIRequestContext): Promise<void> {
  if (running((await project(request)).job)) await request.post(`${base()}/jobs/cancel`, { data: {} });
  await expect.poll(async () => running((await project(request)).job), RENDER).toBe(false);
}

/** A preview of the plan as it is now, made through the API: the starting point of the tests that need one. */
async function previewReady(request: APIRequestContext): Promise<void> {
  await idle(request);
  const response = await request.post(`${base()}/jobs`, { data: { type: "preview" } });
  expect(response.ok(), await response.text()).toBeTruthy();
  await expect.poll(async () => running((await project(request)).job), RENDER).toBe(false);
  const done = await project(request);
  expect(done.job).toMatchObject({ type: "preview", state: "done" });
}

let resolution: unknown;
let autoPreview: unknown;

test.beforeAll(async ({ request }) => {
  const settings = (await project(request)).settings;
  ({ resolution, auto_preview: autoPreview } = settings);
  await request.put(`${base()}/settings`, { data: { resolution: "480p", auto_preview: true } });
});

test.afterAll(async ({ request }) => {
  await idle(request);
  await request.put(`${base()}/settings`, { data: { resolution, auto_preview: autoPreview } });
});

test.beforeEach(async ({ request }) => {
  await idle(request);
  await resetPlan(request);
});

const topBar = (page: Page) => page.locator(".topbar");
const exportButton = (page: Page) => topBar(page).getByRole("button", { name: "Esporta video" });
const exportDialog = (page: Page) => page.getByRole("dialog", { name: "Esporta il video" });
const detail = (page: Page) => page.getByRole("complementary");

async function changeFirstPhoto(page: Page): Promise<void> {
  const index = state().plan.elements.findIndex((element) => element.type === "photo");
  const start = state().plan.elements[index]!.duration;
  await clipButtons(page).nth(index).click();
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill(String(Math.min(30, start + 1)));
  await saved(page);
  await page.getByRole("button", { name: "Torna al riepilogo" }).click();
}

async function openExport(page: Page, name: string): Promise<void> {
  await exportButton(page).click();
  await expect(exportDialog(page)).toBeVisible();
  await exportDialog(page).getByLabel("Nome del file").fill(name);
}

// --- preview ---

test("the full preview from the summary shows its progress, plays when it is ready and says it is up to date", async ({ page, request }) => {
  await openEditor(page);
  await detail(page).getByRole("button", { name: /^(Anteprima completa|Aggiorna l'anteprima)$/ }).click();
  await expect(topBar(page)).toContainText("Anteprima in corso");
  await expect(detail(page).getByRole("progressbar")).toBeVisible();
  await expect(detail(page).getByRole("button", { name: "Ferma" })).toBeVisible();
  await expect(detail(page).getByRole("button", { name: "Esporta video" })).toHaveCount(0);
  await expect(topBar(page)).not.toContainText("Anteprima in corso", RENDER);
  await expect(detail(page).locator("video")).toBeVisible();
  await expect(detail(page)).toContainText("Anteprima aggiornata.");
  expect((await project(request)).preview).toMatchObject({ present: true, outdated: false });
});

test("an edit marks the preview as outdated, and updating it takes the mark away", async ({ page, request }) => {
  await previewReady(request);
  await openEditor(page);
  await expect(detail(page)).toContainText("Anteprima aggiornata.");
  await expect(topBar(page)).not.toContainText("Anteprima non aggiornata");
  await changeFirstPhoto(page);
  await expect(topBar(page)).toContainText("Anteprima non aggiornata");
  await expect(detail(page)).toContainText("L'anteprima non è aggiornata rispetto al piano.");
  await expect(detail(page)).toContainText("Non è in tempo reale");
  expect((await project(request)).preview.outdated).toBe(true);
  await detail(page).getByRole("button", { name: "Aggiorna l'anteprima" }).click();
  await expect(topBar(page)).toContainText("Anteprima in corso");
  await expect(topBar(page)).not.toContainText("Anteprima in corso", RENDER);
  await expect(topBar(page)).not.toContainText("Anteprima non aggiornata");
  await expect(detail(page)).toContainText("Anteprima aggiornata.");
});

test("a plan saved while the preview renders leaves it outdated, with a warning while it runs", async ({ page, request }) => {
  await openEditor(page);
  await detail(page).getByRole("button", { name: /^(Anteprima completa|Aggiorna l'anteprima)$/ }).click();
  await expect(topBar(page)).toContainText("Anteprima in corso");
  await changeFirstPhoto(page);
  await expect(detail(page)).toContainText("usa la versione di prima");
  await expect(topBar(page)).not.toContainText("Anteprima in corso", RENDER);
  await expect(topBar(page)).toContainText("Anteprima non aggiornata");
  expect((await project(request)).preview.outdated).toBe(true);
});

test("the preview can be stopped, and the editor stays usable meanwhile", async ({ page, request }) => {
  await openEditor(page);
  await detail(page).getByRole("button", { name: /^(Anteprima completa|Aggiorna l'anteprima)$/ }).click();
  await expect(detail(page).getByRole("button", { name: "Ferma" })).toBeVisible();
  await photoClips(page).first().click();
  await expect(photoClips(page).first()).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Torna al riepilogo" }).click();
  await detail(page).getByRole("button", { name: "Ferma" }).click();
  await expect(topBar(page)).not.toContainText("Anteprima in corso", RENDER);
  expect((await project(request)).job).toMatchObject({ type: "preview", state: "interrupted" });
  await expect(detail(page)).not.toContainText("non è riuscita");
  await expect(detail(page).getByRole("button", { name: /^(Anteprima completa|Aggiorna l'anteprima)$/ })).toBeEnabled();
});

// --- export ---

test("the export asks for a name, shows its progress, says where the file is and gives it for download", async ({ page, request }) => {
  await openEditor(page);
  await openExport(page, "e2e export");
  await expect(exportDialog(page)).toContainText("e2e export.mp4");
  await exportDialog(page).getByRole("button", { name: "Avvia l'esportazione" }).click();
  await expect(exportDialog(page).getByRole("progressbar")).toBeVisible();
  await expect(exportDialog(page).getByRole("button", { name: "Ferma" })).toBeVisible();
  await expect(exportDialog(page)).toContainText("Esportazione di «e2e export.mp4»");
  await expect(exportDialog(page)).toContainText("Il video è pronto.", RENDER);
  await expect(exportDialog(page)).toContainText(`projects/${state().project}/e2e export.mp4`);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    exportDialog(page).getByRole("link", { name: "Scarica il video" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("e2e export.mp4");
  const response = await request.get(`${base()}/video/e2e%20export?download=1`);
  expect(response.headers()["content-disposition"]).toContain('attachment; filename="e2e export.mp4"');
  expect((await response.body()).subarray(4, 8).toString()).toBe("ftyp");
  const entry = (await project(request)).exports.find((item) => item.name === "e2e export");
  expect(entry?.path).toBe(`projects/${state().project}/e2e export.mp4`);
  await exportDialog(page).getByRole("button", { name: "Chiudi" }).click();
  await expect(detail(page)).toContainText("Percorso: projects/");
  await expect(detail(page).getByRole("link", { name: "Scarica e2e export.mp4" })).toBeVisible();
});

test("a bad name is refused before anything starts, and an existing one is announced", async ({ page, request }) => {
  const before = (await project(request)).job;
  await openEditor(page);
  await openExport(page, "x_preview");
  await expect(exportDialog(page)).toContainText("non può finire con");
  await expect(exportDialog(page).getByRole("button", { name: "Avvia l'esportazione" })).toBeDisabled();
  await exportDialog(page).getByLabel("Nome del file").fill("../x");
  await expect(exportDialog(page)).toContainText("Usa lettere, numeri");
  await exportDialog(page).getByLabel("Nome del file").fill("");
  await expect(exportDialog(page)).toContainText("Scrivi un nome");
  const existing = (await project(request)).exports.find((item) => !item.partial);
  if (existing) {
    await exportDialog(page).getByLabel("Nome del file").fill(existing.name);
    await expect(exportDialog(page)).toContainText("Esiste già un video con questo nome");
    await expect(exportDialog(page).getByRole("button", { name: "Avvia l'esportazione" })).toBeEnabled();
  }
  expect((await project(request)).job).toEqual(before); // nothing was started
});

test("the export can be stopped and started again with the same name", async ({ page, request }) => {
  await openEditor(page);
  await openExport(page, "e2e-stop");
  await exportDialog(page).getByRole("button", { name: "Avvia l'esportazione" }).click();
  await exportDialog(page).getByRole("button", { name: "Ferma" }).click();
  await expect(exportDialog(page)).toContainText("Il lavoro è stato fermato", RENDER);
  expect((await project(request)).job).toMatchObject({ type: "export", state: "interrupted" });
  await exportDialog(page).getByRole("button", { name: "Esporta di nuovo" }).click();
  await expect(exportDialog(page).getByLabel("Nome del file")).toHaveValue("e2e-stop");
});

test("while a render runs the export button is off, with the reason, and the editor stays usable", async ({ page }) => {
  await openEditor(page);
  await openExport(page, "e2e-running");
  await exportDialog(page).getByRole("button", { name: "Avvia l'esportazione" }).click();
  await expect(exportDialog(page).getByRole("button", { name: "Ferma" })).toBeVisible();
  await exportDialog(page).getByRole("button", { name: "Chiudi" }).click();
  await expect(exportButton(page)).toBeDisabled();
  await expect(exportButton(page)).toHaveAttribute("title", /Un altro lavoro è in corso/);
  await expect(topBar(page)).toContainText("Esportazione in corso");
  await expect(detail(page)).toContainText("Esportazione in corso");
  await changeFirstPhoto(page); // the plan can be saved during a render
  await expect(detail(page)).toContainText("usa la versione di prima");
  await detail(page).getByRole("button", { name: "Ferma" }).click();
  await expect(exportButton(page)).toBeEnabled(RENDER);
});

// --- the preview after a new plan ---

async function regenerate(page: Page): Promise<void> {
  await openEditor(page);
  await topBar(page).getByRole("button", { name: "Impostazioni" }).click();
  await page.getByRole("button", { name: "Rigenera il piano…" }).click();
  await page.getByRole("button", { name: "Rigenera il piano", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Analisi in corso" })).toBeVisible();
  await expect(page.getByText("Piano pronto")).toBeVisible(RENDER);
}

test("a new plan starts the preview by itself", async ({ page, request }) => {
  await regenerate(page);
  await page.getByRole("button", { name: "Apri l'editor" }).click();
  await expect(clipButtons(page).first()).toBeVisible();
  await expect(topBar(page)).toContainText("Anteprima in corso");
  expect((await project(request)).job).toMatchObject({ type: "preview", state: "running" });
  await expect(topBar(page)).not.toContainText("Anteprima in corso", RENDER);
});

test("with the setting off a new plan starts no preview", async ({ page, request }) => {
  await request.put(`${base()}/settings`, { data: { auto_preview: false } });
  try {
    await regenerate(page);
    await page.getByRole("button", { name: "Apri l'editor" }).click();
    await expect(clipButtons(page).first()).toBeVisible();
    await expect(topBar(page)).not.toContainText("Anteprima in corso");
    expect((await project(request)).job?.type).toBe("plan");
  } finally {
    await request.put(`${base()}/settings`, { data: { auto_preview: true } });
  }
});
