// The transitions and the music of step U6, in a real browser: the page served by nginx, on the plan the job made from a
// synthetic vacation, with the synthetic tracks of `dev-media/e2e-music` as the music root. The assertions look at the
// page and at the plan the server saved.
import { expect, test, type Page } from "@playwright/test";
import {
  TRACK_A, TRACK_B, addTrack, clipButtons, clips, dragTo, effectButtons, musicParts, openEditor, readPlan, resetPlan, saved, state,
  type Element, type Plan,
} from "../helpers";

test.beforeEach(async ({ request }) => {
  await resetPlan(request);
});

const server = async (request: Parameters<typeof readPlan>[0]): Promise<Plan> => (await readPlan(request, state().project)).plan;
const effectsOf = (plan: Plan, index: number) => plan.elements[index]!.effects;

async function selectClip(page: Page, index: number) {
  await clipButtons(page).nth(index).click();
  await expect(clipButtons(page).nth(index)).toHaveAttribute("aria-pressed", "true");
}

/** The index of a photo that is not the first or the last clip: it has a clip on each side. */
const middlePhoto = (elements: Element[]): number => elements.findIndex((element, index) => index > 1 && index < elements.length - 2 && element.type === "photo");

// --- transitions ---

test("a transition put with the buttons of a clip is saved, owned by the user, and selected with its two handles", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("button", { name: "Da nero all'inizio" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).map((effect) => effect.type)).toEqual(["fade_in"]);
  const [fade] = effectsOf(await server(request), index);
  expect(fade).toMatchObject({ offset: 0, duration: 1, origin: "user" });
  const handles = page.getByRole("separator", { name: /^Transizione: / });
  await expect(handles).toHaveCount(2);
  await expect(page.getByRole("separator", { name: "Transizione: inizio" })).toHaveAttribute("aria-disabled", "true");
  await expect(page.getByRole("separator", { name: "Transizione: fine" })).toHaveAttribute("aria-valuenow", "1");
  await saved(page);
});

test("a dissolve 'at the end' of a clip is written in the next clip, and replaces the planner's", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("button", { name: "Dissolvenza alla fine" }).click();
  await expect.poll(async () => (await server(request)).elements[index + 1]!.effects.find((effect) => effect.type === "dissolve")?.origin).toBe("user");
  const plan = await server(request);
  expect(effectsOf(plan, index + 1).filter((effect) => effect.type === "dissolve")).toHaveLength(1);
  expect(effectsOf(plan, index + 1).find((effect) => effect.type === "dissolve")).toMatchObject({ offset: 0, duration: 0.8 });
  expect(effectsOf(plan, index).filter((effect) => effect.type === "dissolve").length, "the dissolve into this clip is not touched").toBe(
    state().plan.elements[index]!.effects.filter((effect) => effect.type === "dissolve").length);
});

test("the first clip has no dissolve at its start: the button is off", async ({ page }) => {
  await openEditor(page);
  await selectClip(page, 0);
  await expect(page.getByRole("button", { name: "Dissolvenza all'inizio" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "A nero alla fine" })).toBeEnabled();
});

test("a transition of the library is dragged onto a clip", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await page.getByRole("tab", { name: /^Transizioni/ }).click();
  const box = (await page.locator(".clip").nth(index).boundingBox())!;
  await dragTo(page, page.locator("ul.library li[draggable='true']").filter({ hasText: "Da nero" }), box.x + box.width * 0.2, box.y + box.height / 2);
  await expect.poll(async () => effectsOf(await server(request), index).map((effect) => effect.type)).toContain("fade_in");
  const fade = effectsOf(await server(request), index).find((effect) => effect.type === "fade_in")!;
  expect(fade).toMatchObject({ offset: 0, origin: "user" });
  await expect(effectButtons(page).filter({ hasText: "Da nero" })).toHaveCount(2); // the one of the first clip too
});

test("a dissolve dropped on the second half of a clip goes to the next clip", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await page.getByRole("tab", { name: /^Transizioni/ }).click();
  const box = (await page.locator(".clip").nth(index).boundingBox())!;
  await dragTo(page, page.locator("ul.library li[draggable='true']").filter({ hasText: "Dissolvenza" }), box.x + box.width * 0.85, box.y + box.height - 6);
  await expect.poll(async () => effectsOf(await server(request), index + 1).find((effect) => effect.type === "dissolve")?.origin).toBe("user");
});

test("the hard cut dragged onto a dissolve takes it away, and undo brings it back", async ({ page, request }) => {
  const index = state().plan.elements.findIndex((element, position) => position > 0 && element.effects.some((effect) => effect.type === "dissolve"));
  test.skip(index < 0, "the plan has no dissolve");
  await openEditor(page);
  await page.getByRole("tab", { name: /^Transizioni/ }).click();
  const dissolve = page.locator(".clip").nth(index).locator(".clip__fx--dissolve");
  const box = (await dissolve.boundingBox())!;
  await dragTo(page, page.locator("ul.library li[draggable='true']").filter({ hasText: "Taglio netto" }), box.x + box.width / 2, box.y + box.height / 2);
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "dissolve")).toBe(false);
  await page.keyboard.press("Control+z");
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "dissolve")).toBe(true);
});

test("a transition dragged to another clip moves there", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("button", { name: "Da nero all'inizio" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).map((effect) => effect.type)).toEqual(["fade_in"]); // the planner's dissolve is gone
  const id = effectsOf(await server(request), index)[0]!.id;
  const target = (await page.locator(".clip").nth(index + 2).boundingBox())!;
  const fade = page.locator(".clip").nth(index).locator(".clip__fxwrap").filter({ has: page.locator(".clip__fx--fade_in") });
  await dragTo(page, fade, target.x + target.width * 0.2, target.y + target.height / 2);
  await expect.poll(async () => effectsOf(await server(request), index + 2).some((effect) => effect.id === id)).toBe(true);
  expect(effectsOf(await server(request), index).some((effect) => effect.id === id)).toBe(false);
});

test("the arrows lengthen a transition by a tenth, Shift by a second, and a handle announces the end", async ({ page, request }) => {
  await openEditor(page);
  await selectClip(page, 0);
  const fade = page.locator(".clip").nth(0).locator(".clip__fx--fade_in");
  await fade.click();
  const end = page.getByRole("separator", { name: "Transizione: fine" });
  const start = Number(await end.getAttribute("aria-valuenow"));
  await end.focus();
  for (let press = 0; press < 3; press += 1) await page.keyboard.press("ArrowRight");
  await expect(end).toHaveAttribute("aria-valuenow", String(Math.round((start + 0.3) * 10) / 10));
  await expect.poll(async () => effectsOf(await server(request), 0).find((effect) => effect.type === "fade_in")?.duration).toBeCloseTo(start + 0.3, 5);
  await fade.focus();
  await page.keyboard.press("ArrowLeft");
  await expect.poll(async () => effectsOf(await server(request), 0).find((effect) => effect.type === "fade_in")?.duration).toBeCloseTo(start + 0.2, 5);
  expect(effectsOf(await server(request), 0).find((effect) => effect.type === "fade_in")?.origin).toBe("user");
});

test("dragging the end handle of a transition changes its length, as one step of the history", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("button", { name: "A nero alla fine" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "fade_out")).toBe(true);
  const before = effectsOf(await server(request), index).find((effect) => effect.type === "fade_out")!.duration;
  const handle = page.getByRole("separator", { name: "Transizione: inizio" });
  const box = (await handle.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 20, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  await expect.poll(async () => effectsOf(await server(request), index).find((effect) => effect.type === "fade_out")!.duration).toBeLessThan(before);
  await saved(page);
  await page.getByRole("button", { name: "Annulla (Ctrl+Z)" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).find((effect) => effect.type === "fade_out")!.duration).toBe(before);
});

test("a dip is put at the playhead and moved with the Detail slider", async ({ page, request }) => {
  const index = middlePhoto(state().plan.elements);
  await openEditor(page);
  await selectClip(page, index);
  await page.getByRole("button", { name: "Passaggio per il nero qui" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "dip")).toBe(true);
  const dip = effectsOf(await server(request), index).find((effect) => effect.type === "dip")!;
  const slider = page.getByRole("slider", { name: "Inizia a", exact: true });
  await expect(slider).toBeEnabled();
  const wanted = Math.round((dip.offset + 0.1) * 10) / 10;
  await slider.fill(String(wanted));
  await expect.poll(async () => effectsOf(await server(request), index).find((effect) => effect.type === "dip")?.offset).toBeCloseTo(wanted, 5);
});

test("Delete removes the selected transition and the notice brings it back", async ({ page, request }) => {
  const index = state().plan.elements.findIndex((element, position) => position > 0 && element.effects.some((effect) => effect.type === "dissolve"));
  test.skip(index < 0, "the plan has no dissolve");
  await openEditor(page);
  await page.locator(".clip").nth(index).locator(".clip__fx--dissolve").click();
  await page.keyboard.press("Delete");
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "dissolve")).toBe(false);
  const notice = page.getByRole("status").filter({ hasText: "Transizione rimossa" });
  await expect(notice).toBeVisible();
  await notice.getByRole("button", { name: "Annulla" }).click();
  await expect.poll(async () => effectsOf(await server(request), index).some((effect) => effect.type === "dissolve")).toBe(true);
});

test("a transition of the library is applied to the whole video with its button", async ({ page, request }) => {
  await openEditor(page);
  await page.getByRole("tab", { name: /^Transizioni/ }).click();
  await page.locator("ul.library").getByRole("button", { name: /^Taglio netto/ }).click();
  await page.getByRole("button", { name: "Applica a tutto il video" }).click();
  await expect.poll(async () => (await server(request)).elements.flatMap((element) => element.effects).some((effect) => effect.type === "dissolve")).toBe(false);
  expect((await server(request)).elements.flatMap((element) => element.effects).some((effect) => effect.type === "fade_in"), "only the dissolves go").toBe(true);
  await page.keyboard.press("Control+z");
  await expect.poll(async () => (await server(request)).elements.flatMap((element) => element.effects).some((effect) => effect.type === "dissolve")).toBe(true);

  await page.locator("ul.library").getByRole("button", { name: /^Dissolvenza/ }).click();
  await page.getByRole("button", { name: "Applica a tutto il video" }).click();
  await expect.poll(async () => (await server(request)).elements.slice(1).every((element) => element.effects.some((effect) => effect.type === "dissolve" && effect.origin === "user"))).toBe(true);
});

// --- music ---

test("a track is added to the video with the button of the Music tab, whole, and the plan keeps its file relative to the music root", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await expect(musicParts(page)).toHaveCount(1);
  await expect(musicParts(page).first()).toHaveAccessibleName(`Parte 1 di ${TRACK_A}`);
  await expect.poll(async () => (await server(request)).music?.tracks.map((track) => track.file)).toEqual([TRACK_A]);
  const music = (await server(request)).music!;
  expect(music.offset).toBe(0);
  expect(music.tracks[0]!.parts).toEqual([{ start: 0, end: 40, muted: false, fade_in: 0, fade_out: 0 }]);
  await saved(page);
  await page.reload();
  await expect(musicParts(page)).toHaveCount(1);
});

test("a track dragged from the Music tab goes onto the music lane", async ({ page, request }) => {
  await openEditor(page);
  await page.getByRole("tab", { name: /^Musica/ }).click();
  await expect(page.getByRole("button", { name: `Aggiungi al video: ${TRACK_B}` })).toBeEnabled();
  const lane = (await page.getByRole("list", { name: "Parti musicali" }).boundingBox())!;
  await dragTo(page, page.locator("ul.library li[draggable='true']").filter({ hasText: TRACK_B }), lane.x + 120, lane.y + lane.height / 2);
  await expect.poll(async () => (await server(request)).music?.tracks.map((track) => track.file)).toEqual([TRACK_B]);
});

test("the music is cut at the playhead, and a part is deleted: the following ones shift", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await expect(musicParts(page)).toHaveCount(1);
  await page.getByRole("slider", { name: /^Righello/ }).focus();
  for (let press = 0; press < 10; press += 1) await page.keyboard.press("ArrowRight");
  await page.getByRole("button", { name: "Taglia la musica qui" }).first().click();
  await expect(musicParts(page)).toHaveCount(2);
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts.map((part) => [part.start, part.end])).toEqual([[0, 10], [10, 40]]);

  await musicParts(page).nth(0).click();
  await page.keyboard.press("Delete");
  await expect(musicParts(page)).toHaveCount(1);
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts.map((part) => [part.start, part.end])).toEqual([[10, 40]]);
  const notice = page.getByRole("status").filter({ hasText: "Parte eliminata" });
  await notice.getByRole("button", { name: "Annulla" }).click();
  await expect(musicParts(page)).toHaveCount(2);
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts.length).toBe(2);
});

test("a part is muted, and its fades are set, with the Detail panel", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await musicParts(page).first().click();
  await page.getByRole("button", { name: "Silenzia la parte" }).click();
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts[0]?.muted).toBe(true);
  await expect(page.getByRole("button", { name: "Riattiva l'audio della parte" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("slider", { name: "Dissolvenza in entrata", exact: true }).fill("2.5");
  await page.getByRole("slider", { name: "Dissolvenza in uscita", exact: true }).fill("4");
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts[0]).toMatchObject({ muted: true, fade_in: 2.5, fade_out: 4 });
});

test("the arrows shift the music by a tenth, Shift by a second, and the button brings it back to the start", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  const part = musicParts(page).first();
  await part.focus();
  for (let press = 0; press < 3; press += 1) await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Shift+ArrowRight");
  await expect.poll(async () => (await server(request)).music?.offset).toBeCloseTo(1.3, 5);
  await part.click();
  await page.getByRole("button", { name: "Riporta la musica all'inizio" }).click();
  await expect.poll(async () => (await server(request)).music?.offset).toBe(0);
});

test("dragging the body of a part shifts the music, as one step of the history", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  const box = (await musicParts(page).first().boundingBox())!;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + 60, y);
  await page.mouse.down();
  await page.mouse.move(box.x + 100, y, { steps: 6 });
  await page.mouse.move(box.x + 160, y, { steps: 6 });
  await page.mouse.up();
  await expect.poll(async () => (await server(request)).music?.offset).toBeGreaterThan(2);
  await saved(page);
  await page.getByRole("button", { name: "Annulla (Ctrl+Z)" }).click();
  await expect.poll(async () => (await server(request)).music?.offset).toBe(0);
});

test("the tracks are reordered by dragging their names, and with the buttons of the Detail panel", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await expect(musicParts(page)).toHaveCount(1);
  await addTrack(page, TRACK_B);
  await expect(musicParts(page)).toHaveCount(2);
  await expect.poll(async () => (await server(request)).music?.tracks.map((track) => track.file)).toEqual([TRACK_A, TRACK_B]);
  const lane = (await page.getByRole("list", { name: "Parti musicali" }).boundingBox())!;
  await dragTo(page, page.locator(".track__tab").filter({ hasText: TRACK_B }), lane.x + 4, lane.y + lane.height / 2);
  await expect.poll(async () => (await server(request)).music?.tracks.map((track) => track.file)).toEqual([TRACK_B, TRACK_A]);

  await musicParts(page).nth(1).click();
  await page.getByRole("button", { name: "Brano prima" }).click();
  await expect.poll(async () => (await server(request)).music?.tracks.map((track) => track.file)).toEqual([TRACK_A, TRACK_B]);
});

test("a deleted part is brought back with the button of the Music tab", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await page.getByRole("slider", { name: /^Righello/ }).focus();
  for (let press = 0; press < 10; press += 1) await page.keyboard.press("ArrowRight");
  await page.getByRole("button", { name: "Taglia la musica qui" }).first().click();
  await expect(musicParts(page)).toHaveCount(2);
  await musicParts(page).nth(1).click();
  await page.getByRole("button", { name: "Elimina la parte" }).click();
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts.map((part) => [part.start, part.end])).toEqual([[0, 10]]);
  await page.getByRole("tab", { name: /^Musica/ }).click();
  await page.getByRole("button", { name: "Ripristina le parti eliminate" }).first().click();
  await expect.poll(async () => (await server(request)).music?.tracks[0]?.parts.map((part) => [part.start, part.end])).toEqual([[0, 10], [10, 40]]);
});

test("the last part deleted leaves a video without music, and the plan keeps the block", async ({ page, request }) => {
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await musicParts(page).first().click();
  await page.getByRole("button", { name: "Elimina la parte" }).click();
  await expect(musicParts(page)).toHaveCount(0);
  await expect.poll(async () => (await server(request)).music).toEqual({ tracks: [], offset: 0 });
  await expect(page.getByText("Nessuna musica. Trascina qui un brano dal pannello Media.")).toBeVisible();
});

test("the clips stay where they are while the music is edited", async ({ page, request }) => {
  const before = state().plan.elements.map((element) => element.id);
  await openEditor(page);
  await addTrack(page, TRACK_A);
  await expect.poll(async () => (await server(request)).music?.tracks.length).toBe(1);
  expect((await server(request)).elements.map((element) => element.id)).toEqual(before);
  await expect(clips(page)).toHaveCount(before.length);
});
