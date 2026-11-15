// The basic editing of step U5, in a real browser: the page of the editor served by nginx, on the plan the job made from
// a synthetic vacation. The assertions look at the page and at the plan the server saved.
import { expect, test, type Page } from "@playwright/test";
import {
  basename, clipButtons, clips, dragTo, expandPhotos, openEditor, readPlan, resetPlan, saved, saveStatus, state, writePlan, type Element,
} from "../helpers";

test.beforeEach(async ({ request }) => {
  await resetPlan(request);
});

const server = async (request: Parameters<typeof readPlan>[0]) => (await readPlan(request, state().project)).plan;
const ids = (elements: Element[]) => elements.map((element) => element.id);
const secondsText = (seconds: number) => `${seconds.toFixed(1).replace(".", ",")} s`;
const photoIndex = (elements: Element[], nth = 0) => elements.map((element, index) => (element.type === "photo" ? index : -1)).filter((index) => index >= 0)[nth]!;

async function selectClip(page: Page, index: number) {
  await clipButtons(page).nth(index).click();
  await expect(clipButtons(page).nth(index)).toHaveAttribute("aria-pressed", "true");
}

test("the editor opens on the plan: its clips, saved, with nothing to undo", async ({ page }) => {
  await openEditor(page);
  await expect(clips(page)).toHaveCount(state().plan.elements.length);
  await expect(saveStatus(page)).toHaveText("Salvato");
  await expect(page.getByRole("button", { name: "Annulla (Ctrl+Z)" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Ripeti (Ctrl+Maiusc+Z)" })).toBeDisabled();
});

test("the duration slider of a photo changes the clip, is saved, and survives a reload", async ({ page, request }) => {
  const index = photoIndex(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill("6.5");
  await expect(clipButtons(page).nth(index)).toHaveAccessibleName(new RegExp(`foto, ${secondsText(6.5)}`));
  await expect.poll(async () => (await server(request)).elements[index]?.duration).toBe(6.5);
  expect((await server(request)).elements[index]?.duration_origin).toBe("user");
  await saved(page);
  await page.reload();
  await expect(clipButtons(page).nth(index)).toHaveAccessibleName(new RegExp(`foto, ${secondsText(6.5)}`));
});

test("the arrows on a handle move the duration by a tenth, Shift by a second", async ({ page, request }) => {
  const index = photoIndex(state().plan.elements);
  const start = state().plan.elements[index]!.duration;
  await openEditor(page);
  await selectClip(page, index);
  const handle = page.getByRole("separator", { name: "Durata della foto: bordo destro" });
  await handle.focus();
  for (let press = 0; press < 3; press += 1) await page.keyboard.press("ArrowRight");
  await expect(handle).toHaveAttribute("aria-valuenow", String(Math.round((start + 0.3) * 10) / 10));
  await page.keyboard.press("Shift+ArrowRight");
  await expect(handle).toHaveAttribute("aria-valuenow", String(Math.round((start + 1.3) * 10) / 10));
  await expect.poll(async () => (await server(request)).elements[index]?.duration).toBeCloseTo(start + 1.3, 5);
});

test("dragging the handle of a photo makes it longer, inside the limit", async ({ page, request }) => {
  const index = photoIndex(state().plan.elements);
  const start = state().plan.elements[index]!.duration;
  await openEditor(page);
  await selectClip(page, index);
  const handle = page.getByRole("separator", { name: "Durata della foto: bordo destro" });
  const box = (await handle.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 30, box.y + box.height / 2, { steps: 6 });
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBeGreaterThan(start + 2);
  await saved(page);
  // A gesture is one step: one undo takes it all back.
  await page.getByRole("button", { name: "Annulla (Ctrl+Z)" }).click();
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(start);
});

test("undo and redo, with the buttons and the keys", async ({ page, request }) => {
  const index = photoIndex(state().plan.elements);
  const start = state().plan.elements[index]!.duration;
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill("9");
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(9);
  await page.getByRole("button", { name: "Annulla (Ctrl+Z)" }).click();
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(start);
  await expect(page.getByRole("button", { name: "Ripeti (Ctrl+Maiusc+Z)" })).toBeEnabled();
  await page.keyboard.press("Control+Shift+Z");
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(9);
  await page.keyboard.press("Control+z");
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(start);
  await page.keyboard.press("Control+y");
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(9);
});

test("Delete takes the selected clip out, and the notice brings it back", async ({ page, request }) => {
  const before = ids(state().plan.elements);
  await openEditor(page);
  await selectClip(page, 2);
  await page.keyboard.press("Delete");
  await expect(clips(page)).toHaveCount(before.length - 1);
  const notice = page.getByRole("status").filter({ hasText: "Elemento rimosso" });
  await expect(notice).toBeVisible();
  await expect.poll(async () => ids((await server(request)).elements)).toEqual(before.filter((_, index) => index !== 2));
  await notice.getByRole("button", { name: "Annulla" }).click();
  await expect(clips(page)).toHaveCount(before.length);
  await expect.poll(async () => ids((await server(request)).elements)).toEqual(before);
});

test("a photo dragged from the Media panel goes into the timeline", async ({ page, request }) => {
  const before = state().plan.elements.length;
  await openEditor(page);
  await expandPhotos(page); // every photo the analysis liked is already in the timeline: the discarded ones can still be added
  const lane = (await page.getByRole("list", { name: "Clip del video" }).boundingBox())!;
  await dragTo(page, page.locator("li.thumbcell[draggable='true']").first(), lane.x + lane.width / 2, lane.y + lane.height / 2);
  await expect(clips(page)).toHaveCount(before + 1);
  await expect.poll(async () => (await server(request)).elements.length).toBe(before + 1);
  const added = (await server(request)).elements.find((element) => !state().plan.elements.some((other) => other.id === element.id))!;
  expect(added.origin).toBe("user");
  expect(added.type).toBe("photo");
});

test("a photo added with the button of the Detail panel is selected afterwards", async ({ page, request }) => {
  const before = state().plan.elements.length;
  await openEditor(page);
  await expandPhotos(page);
  await page.locator("li.thumbcell[draggable='true'] button.thumb").first().click();
  await page.getByRole("button", { name: "Aggiungi alla timeline", exact: true }).click();
  await expect(clips(page)).toHaveCount(before + 1);
  await expect(clipButtons(page).last()).toHaveAttribute("aria-pressed", "true");
  await expect.poll(async () => (await server(request)).elements.length).toBe(before + 1);
});

test("a clip dragged along the timeline changes place", async ({ page, request }) => {
  const before = ids(state().plan.elements);
  await openEditor(page);
  const target = page.locator(".clip").nth(3);
  const box = (await target.boundingBox())!;
  await dragTo(page, page.locator(".clip").nth(0).locator(".clip__grab"), box.x + box.width - 2, box.y + box.height / 2);
  const expected = [...before.slice(1, 4), before[0]!, ...before.slice(4)];
  await expect.poll(async () => ids((await server(request)).elements)).toEqual(expected);
  await saved(page);
});

test("Alt and the arrows move the selected clip without a drag", async ({ page, request }) => {
  const before = ids(state().plan.elements);
  await openEditor(page);
  await selectClip(page, 1);
  await page.keyboard.press("Alt+ArrowRight");
  await expect.poll(async () => ids((await server(request)).elements).slice(0, 3)).toEqual([before[0], before[2], before[1]]);
  await page.keyboard.press("Alt+ArrowLeft");
  await expect.poll(async () => ids((await server(request)).elements)).toEqual(before);
});

test("a clip moved to the front loses the dissolve it had from the clip before", async ({ page, request }) => {
  const elements = state().plan.elements;
  const index = elements.findIndex((element, position) => position > 2 && element.effects.some((effect) => effect.type === "dissolve"));
  test.skip(index < 0, "the plan has no dissolve to move");
  await openEditor(page);
  await selectClip(page, index);
  for (let step = 0; step < index; step += 1) await page.keyboard.press("Alt+ArrowLeft");
  await expect.poll(async () => (await server(request)).elements[0]?.id).toBe(elements[index]!.id);
  expect((await server(request)).elements[0]!.effects.some((effect) => effect.type === "dissolve")).toBe(false);
});

test("hiding a photo takes it out of the timeline and into the hidden ones, and undo brings it back", async ({ page, request }) => {
  const used = state().plan.elements.find((element) => element.type === "photo")!;
  await openEditor(page);
  await page.getByRole("button", { name: `Nascondi la foto ${basename(used.source)}` }).click();
  await expect(page.getByRole("status").filter({ hasText: "Foto nascosta" })).toBeVisible();
  await expect.poll(async () => (await server(request)).excluded.map((entry) => entry.source)).toEqual([used.source]);
  expect((await server(request)).elements.some((element) => element.id === used.id)).toBe(false);
  await page.keyboard.press("Control+z");
  await expect.poll(async () => (await server(request)).excluded.length).toBe(0);
  expect((await server(request)).elements.some((element) => element.id === used.id)).toBe(true);
});

test("the edges of a moment are trimmed from the bar of its video", async ({ page, request }) => {
  const moment = state().plan.elements.find((element) => element.type === "video");
  test.skip(!moment, "the plan has no video moment");
  await openEditor(page);
  await page.getByRole("tab", { name: /^Video/ }).click();
  const name = basename(moment!.source);
  const toggle = page.getByRole("button", { name: `Mostra o nascondi i momenti di ${name}` });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const end = page.getByRole("separator", { name: `Fine del momento: ${name}` }).first();
  await end.focus();
  for (let press = 0; press < 5; press += 1) await page.keyboard.press("ArrowLeft");
  const expected = Math.max((moment!.start ?? 0) + 1, Math.round(((moment!.end ?? 0) - 0.5) * 10) / 10);
  await expect.poll(async () => (await server(request)).elements.find((element) => element.id === moment!.id)?.end).toBeCloseTo(expected, 5);
  const saved = (await server(request)).elements.find((element) => element.id === moment!.id)!;
  expect(saved.duration).toBeCloseTo((saved.end ?? 0) - (saved.start ?? 0), 5);
});

/** The video of the Detail panel is there, has its first frame and shows no error. */
async function expectPlayablePreview(page: Page) {
  const detail = page.getByRole("complementary");
  await expect(detail.locator("video")).toBeVisible();
  await expect.poll(() => detail.locator("video").evaluate((video: HTMLVideoElement) => video.readyState), { timeout: 60_000 }).toBeGreaterThanOrEqual(2);
  await expect(detail.getByRole("alert")).toHaveCount(0);
}

test("a whole video and a moment outside the timeline play in the Detail panel, like a moment in the timeline", async ({ page }) => {
  test.skip(!state().plan.elements.some((element) => element.type === "video"), "the plan has no video moment");
  await openEditor(page);
  await page.getByRole("tab", { name: /^Video/ }).click();
  await page.locator(".video__name").first().click();
  await expectPlayablePreview(page);

  const toggle = page.getByRole("button", { name: /^Mostra o nascondi i momenti di / }).first();
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const outside = page.getByRole("group", { name: /^Momenti del video / }).getByRole("button", { name: /, (disponibile|scartato|eliminato)$/ }).first();
  if (await outside.count()) {
    await outside.click();
    await expectPlayablePreview(page);
  }

  await clipButtons(page).nth(state().plan.elements.findIndex((element) => element.type === "video")).click();
  await expectPlayablePreview(page);
});

test("a plan changed elsewhere stops the saving: reload takes the other version", async ({ page, request }) => {
  const { project, plan } = state();
  const index = photoIndex(plan.elements);
  const other = photoIndex(plan.elements, 1);
  await openEditor(page);
  const current = await readPlan(request, project);
  const changed = structuredClone(current.plan);
  changed.elements[other]!.duration = 8.8;
  await writePlan(request, project, changed, current.revision);

  await selectClip(page, index);
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill("5");
  const dialog = page.getByRole("alertdialog", { name: "Il piano è cambiato altrove" });
  await expect(dialog).toBeVisible();
  await expect(saveStatus(page)).toHaveText("Piano cambiato altrove");
  await dialog.getByRole("button", { name: "Ricarica" }).click();
  await expect(dialog).toBeHidden();
  await expect(clipButtons(page).nth(other)).toHaveAccessibleName(new RegExp(`foto, ${secondsText(8.8)}`));
  await expect(clipButtons(page).nth(index)).toHaveAccessibleName(new RegExp(`foto, ${secondsText(plan.elements[index]!.duration)}`));
});

test("a plan changed elsewhere can be overwritten", async ({ page, request }) => {
  const { project, plan } = state();
  const index = photoIndex(plan.elements);
  const other = photoIndex(plan.elements, 1);
  await openEditor(page);
  const current = await readPlan(request, project);
  const changed = structuredClone(current.plan);
  changed.elements[other]!.duration = 8.8;
  await writePlan(request, project, changed, current.revision);

  await selectClip(page, index);
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill("5");
  const dialog = page.getByRole("alertdialog", { name: "Il piano è cambiato altrove" });
  await dialog.getByRole("button", { name: "Sovrascrivi" }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(async () => (await server(request)).elements[index]!.duration).toBe(5);
  await saved(page);
});

test("a new plan asks first, keeps what was hidden and drops the rest of the edits", async ({ page, request }) => {
  test.setTimeout(240_000);
  const { project, plan } = state();
  const hiddenPhoto = plan.elements.filter((element) => element.type === "photo")[1]!;
  const index = photoIndex(plan.elements);
  await openEditor(page);
  await page.getByRole("button", { name: `Nascondi la foto ${basename(hiddenPhoto.source)}` }).click();
  await expect.poll(async () => (await server(request)).excluded.length).toBe(1);
  await selectClip(page, (await server(request)).elements.findIndex((element) => element.id === plan.elements[index]!.id));
  await page.getByRole("slider", { name: "Durata della foto", exact: true }).fill("7.7");
  await expect.poll(async () => (await server(request)).elements.some((element) => element.duration_origin === "user")).toBe(true);
  await saved(page);

  await page.getByRole("button", { name: "Impostazioni" }).click();
  await page.getByRole("button", { name: "Rigenera il piano…" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Rigenerare il piano?" });
  await expect(dialog).toContainText("plan_history");
  await dialog.getByRole("button", { name: "Annulla" }).click();
  await expect(dialog).toBeHidden();
  expect((await server(request)).elements.some((element) => element.duration_origin === "user"), "nothing happened yet").toBe(true);

  await page.getByRole("button", { name: "Rigenera il piano…" }).click();
  await dialog.getByRole("button", { name: "Rigenera il piano", exact: true }).click();
  await expect(page.getByRole("button", { name: "Apri l'editor" })).toBeVisible({ timeout: 180_000 });
  await expect.poll(async () => (await server(request)).elements.some((element) => element.duration_origin === "user"), { timeout: 30_000 }).toBe(false);
  const regenerated = await server(request);
  expect(regenerated.excluded.map((entry) => entry.source)).toEqual([hiddenPhoto.source]);
  expect(regenerated.elements.some((element) => element.source === hiddenPhoto.source)).toBe(false);
  expect((await request.get(`/api/projects/${project}`).then((response) => response.json())).plan.valid).toBe(true);
  await page.getByRole("button", { name: "Apri l'editor" }).click();
  await expect(clips(page).first()).toBeVisible();
});
