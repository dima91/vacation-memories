// Static accessibility checks without a real browser (section 15 of UI-SPEC.md): the components are rendered with
// `react-dom/server` on fixture data and every control must have a name, every reference an existing target.
import assert from "node:assert/strict";
import { createRef, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { test } from "node:test";
import { DetailPanel, type DetailProps } from "../src/components/DetailPanel";
import { MediaPanel, type MediaTab } from "../src/components/MediaPanel";
import { Timeline } from "../src/components/Timeline";
import { TopBar, type EditBar } from "../src/components/TopBar";
import { Toast } from "../src/components/ui";
import { FolderList } from "../src/screens/FoldersScreen";
import { RegenerateDialog, SettingsForm } from "../src/screens/SettingsScreen";
import { Workspace } from "../src/screens/EditorScreen";
import { ProgressScreen } from "../src/screens/ProgressScreen";
import { NO_SELECTION, type Selection } from "../src/lib/selection";
import { editable, readOnly } from "./actions";
import { accessibleName, all, parseHtml, type HtmlNode } from "./dom";
import { aiAvailable, catalog, clone, folders, planDocument, projectNew, projectPlanned, waveform } from "./fixtures";

const waveforms = new Map([["brano.wav", waveform]]);
const noop = () => undefined;

const NEEDS_NAME = ["button", "input", "select", "textarea", "video", "summary"];
const ROLES_WITH_NAME = ["slider", "tab", "tablist", "progressbar", "separator", "tabpanel", "alertdialog"];

export function auditHtml(html: string, where: string): HtmlNode {
  const root = parseHtml(html);
  const problems: string[] = [];
  const ids = new Map<string, number>();
  all(root, (node) => "id" in node.attrs).forEach((node) => ids.set(node.attrs.id ?? "", (ids.get(node.attrs.id ?? "") ?? 0) + 1));
  for (const [id, count] of ids) if (count > 1) problems.push(`duplicate id ${id}`);

  for (const node of all(root, () => true)) {
    const label = `<${node.tag}${node.attrs.id ? `#${node.attrs.id}` : ""} ${node.attrs.class ?? ""}>`;
    const isHidden = node.attrs.type === "hidden";
    if ((NEEDS_NAME.includes(node.tag) || ROLES_WITH_NAME.includes(node.attrs.role ?? "")) && !isHidden && !accessibleName(node, root)) {
      problems.push(`${label} has no accessible name`);
    }
    if (node.tag === "img" && !("alt" in node.attrs)) problems.push(`${label} has no alt`);
    if (node.tag === "svg" && node.attrs["aria-hidden"] !== "true") problems.push(`${label} is not hidden from assistive technology`);
    for (const attribute of ["aria-labelledby", "aria-describedby", "aria-controls"]) {
      for (const id of (node.attrs[attribute] ?? "").split(/\s+/).filter(Boolean)) if (!ids.has(id)) problems.push(`${label} ${attribute} points to a missing #${id}`);
    }
    if (node.attrs.role === "slider" && !("aria-valuenow" in node.attrs) && node.tag !== "input") problems.push(`${label} is a slider without aria-valuenow`);
    if (node.tag === "button" && !("type" in node.attrs)) problems.push(`${label} has no type`);
  }
  assert.deepEqual(problems, [], `${where}:\n${problems.join("\n")}`);
  return root;
}

const render = (element: ReactElement): string => renderToStaticMarkup(element);

const detail = (selection: Selection, overrides: Partial<DetailProps> = {}): ReactElement => (
  <DetailPanel projectId={projectPlanned.id} project={projectPlanned} document={planDocument} catalog={catalog} waveforms={waveforms}
    selection={selection} playhead={3} seekVersion={0} job={null} collapsed={false} actions={editable} playerRef={createRef<HTMLVideoElement>()}
    onTime={noop} onSelect={noop} onToggle={noop} {...overrides} />
);

test("the folder list: every folder is a named button with its counts and state", () => {
  const html = render(<FolderList folders={folders.folders} onOpen={noop} busy={null} />);
  const root = auditHtml(html, "folders");
  const buttons = all(root, (node) => node.tag === "button");
  assert.equal(buttons.length, folders.folders.length);
  assert.match(accessibleName(buttons[1]!, root), /vacation.*16 foto · 2 video.*Non analizzata/);
});

test("the settings form: every field has a label and the hints are linked to their fields", () => {
  const html = render(<SettingsForm project={projectNew} mode="analyze" onSaved={noop} onStarted={noop} ai={aiAvailable} tracks={["brano.wav"]} />);
  const root = auditHtml(html, "settings");
  const fields = all(root, (node) => ["input", "select"].includes(node.tag));
  assert.ok(fields.length >= 12);
  for (const field of fields) assert.ok(accessibleName(field, root), `field ${field.attrs.id}`);
  assert.ok(all(root, (node) => node.tag === "button" && /Analizza/.test(node.children.map((child) => child.text).join(""))).length === 1);
  assert.ok(all(root, (node) => node.tag === "details").length === 1, "the advanced options are collapsed");
});

test("the settings form of an existing project saves instead of analyzing, and offers to complete an incomplete analysis", () => {
  const incomplete = clone(projectPlanned);
  incomplete.analysis.complete = false;
  incomplete.analysis.files.new_or_changed = 3;
  const html = render(<SettingsForm project={incomplete} mode="edit" onSaved={noop} onStarted={noop} onBack={noop} ai={null} tracks={[]} />);
  auditHtml(html, "settings (edit)");
  assert.match(html, /Salva le impostazioni/);
  assert.match(html, /Completa l&#x27;analisi/);
  assert.match(html, /Torna all&#x27;editor/);
});

for (const tab of ["photos", "videos", "transitions", "music"] as MediaTab[]) {
  test(`the Media panel, ${tab} tab: tabs, groups and items have names`, () => {
    const html = render(<MediaPanel projectId={projectPlanned.id} catalog={catalog} document={planDocument} waveforms={waveforms}
      selection={NO_SELECTION} tab={tab} actions={editable} onTab={noop} onSelect={noop} />);
    const root = auditHtml(html, `media ${tab}`);
    const tabs = all(root, (node) => node.attrs.role === "tab");
    assert.equal(tabs.length, 4);
    assert.equal(tabs.filter((node) => node.attrs["aria-selected"] === "true").length, 1);
    assert.equal(all(root, (node) => node.attrs.role === "tabpanel").length, 1);
  });
}

test("a thumbnail says its score, whether it is in the timeline and how many similar photos there are", () => {
  const html = render(<MediaPanel projectId={projectPlanned.id} catalog={catalog} document={planDocument} waveforms={waveforms}
    selection={NO_SELECTION} tab="photos" actions={editable} onTab={noop} onSelect={noop} />);
  const root = parseHtml(html);
  const used = all(root, (node) => node.tag === "button" && /Nella timeline/.test(node.attrs["aria-label"] ?? ""));
  assert.ok(used.length > 0);
  assert.match(used[0]?.attrs["aria-label"] ?? "", /^Foto .*\.jpg, punteggio 0,\d\d, Nella timeline/);
});

test("the photo tab shows the threshold slider with its value and the groups collapse", () => {
  const html = render(<MediaPanel projectId={projectPlanned.id} catalog={catalog} document={planDocument} waveforms={waveforms}
    selection={NO_SELECTION} tab="photos" actions={editable} onTab={noop} onSelect={noop} />);
  assert.match(html, /Soglia di interesse/);
  assert.match(html, /≥ 0,00/);
  assert.match(html, /Comprimi tutto/);
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /aria-expanded="false"/, "the discarded group starts collapsed");
});

const selections: [string, Selection][] = [
  ["the whole timeline", NO_SELECTION],
  ["a photo of the plan", { kind: "element", id: planDocument.plan.elements.find((element) => element.type === "photo")!.id }],
  ["a moment of the plan", { kind: "element", id: planDocument.plan.elements.find((element) => element.type === "video")!.id }],
  ["a candidate that is not in the plan", { kind: "candidate", id: catalog.photos.find((item) => item.status !== "selected")!.id }],
  ["a video", { kind: "video", media: catalog.videos[0]!.media }],
  ["a transition of the library", { kind: "library", type: "dissolve" }],
  ["a transition on a clip", (() => { const element = planDocument.plan.elements.find((entry) => entry.effects.length > 0)!; return { kind: "effect", element: element.id, id: element.effects[0]!.id } as Selection; })()],
  ["a track", { kind: "track", file: "brano.wav" }],
  ["a part of the music", { kind: "part", track: 0, part: 1 }],
];
for (const [name, selection] of selections) {
  test(`the Detail panel for ${name} has a title, named controls and a help text`, () => {
    const html = render(detail(selection));
    const root = auditHtml(html, `detail ${name}`);
    assert.ok(all(root, (node) => node.tag === "h2").length === 1);
    assert.match(html, /Come funziona/);
  });
}

test("the Detail panel collapses to its button", () => {
  const html = render(detail(NO_SELECTION, { collapsed: true }));
  const root = auditHtml(html, "detail collapsed");
  assert.equal(all(root, (node) => node.tag === "button").length, 1);
  assert.match(html, /aria-expanded="false"/);
});

test("the whole-timeline summary shows duration against the request, the counts and the preview state", () => {
  const html = render(detail(NO_SELECTION));
  assert.match(html, /0:31 di 0:30/);
  assert.match(html, /Foto/);
  assert.match(html, /Non c&#x27;è ancora un&#x27;anteprima/);
  const withPreview = clone(projectPlanned);
  withPreview.preview = { present: true, name: "memories_preview", outdated: true };
  const preview = render(detail(NO_SELECTION, { project: withPreview }));
  auditHtml(preview, "detail with preview");
  assert.match(preview, /\/api\/projects\/vacation-1b24e0c1\/video\/memories_preview/);
  assert.match(preview, /non è aggiornata/);
});

test("the timeline: ruler as a slider, clips and parts as named buttons, legend F and V", () => {
  const html = render(<Timeline projectId={projectPlanned.id} document={planDocument} waveforms={waveforms} selection={NO_SELECTION}
    playhead={12} actions={editable} onSelect={noop} onSeek={noop} />);
  const root = auditHtml(html, "timeline");
  const ruler = all(root, (node) => node.attrs.role === "slider" && node.attrs.class === "ruler")[0];
  assert.equal(ruler?.attrs["aria-valuenow"], "12");
  assert.equal(ruler?.attrs["aria-valuetext"], "Posizione 0:12");
  const clips = all(root, (node) => node.tag === "button" && node.parent?.attrs.class?.startsWith("clip") === true);
  assert.ok(clips.length >= planDocument.plan.elements.length);
  assert.match(accessibleName(clips[0]!, root), /^Clip 1: foto, \d+,\d s/);
  assert.match(html, /F = foto · V = video/);
  assert.match(html, /0:31 \/ 0:30/);
  assert.match(html, /Mostra tutta la musica/);
  assert.equal(all(root, (node) => node.tag === "button" && node.attrs["aria-pressed"] !== undefined && /Parte/.test(node.attrs["aria-label"] ?? "")).length, 2);
});

test("an empty timeline says so", () => {
  const empty = clone(planDocument);
  empty.plan.elements = [];
  empty.plan.music = undefined;
  empty.duration = 0;
  const html = render(<Timeline projectId="p" document={empty} waveforms={new Map()} selection={NO_SELECTION} playhead={0} actions={editable} onSelect={noop} onSeek={noop} />);
  auditHtml(html, "empty timeline");
  assert.match(html, /La timeline è vuota/);
  assert.match(html, /Nessuna musica\. Trascina qui un brano/);
});

test("the top bar names the project and the state of the analysis", () => {
  const html = render(<TopBar project={projectPlanned} catalog={catalog} requested={30} working={false} onChangeFolder={noop} onSettings={noop} />);
  auditHtml(html, "top bar");
  assert.match(html, /Analisi completata · 16 foto · 2 video/);
  assert.match(html, /Cambia cartella/);
  assert.match(html, /Impostazioni/);
  assert.doesNotMatch(html, /<select/, "no language selector with a single catalog");
});

test("the progress screen renders before the first poll answers", () => {
  auditHtml(render(<ProgressScreen projectId="p" onDone={noop} onBack={noop} />), "progress");
});

// --- editing (U5) ---

const firstPhoto = planDocument.plan.elements.find((element) => element.type === "photo")!;
const firstMoment = planDocument.plan.elements.find((element) => element.type === "video")!;
const timelineWith = (selection: Selection, actions = editable) => render(
  <Timeline projectId={projectPlanned.id} document={planDocument} waveforms={waveforms} selection={selection} playhead={0}
    actions={actions} onSelect={noop} onSeek={noop} />);

test("a selected photo shows two handles that announce its duration and its limits", () => {
  const root = auditHtml(timelineWith({ kind: "element", id: firstPhoto.id }), "timeline with a photo selected");
  const handles = all(root, (node) => node.attrs.role === "separator");
  assert.equal(handles.length, 2);
  assert.deepEqual(handles.map((node) => node.attrs["aria-label"]), ["Durata della foto: bordo sinistro", "Durata della foto: bordo destro"]);
  for (const handle of handles) {
    assert.deepEqual([handle.attrs["aria-valuenow"], handle.attrs["aria-valuemin"], handle.attrs["aria-valuemax"], handle.attrs.tabindex], [String(firstPhoto.duration), "1", "30", "0"]);
    assert.match(handle.attrs["aria-valuetext"] ?? "", /^\d+,\d s$/);
  }
});

test("a moment, nothing selected or a read-only plan have no duration handles", () => {
  for (const [name, html] of [
    ["a moment", timelineWith({ kind: "element", id: firstMoment.id })],
    ["nothing", timelineWith(NO_SELECTION)],
    ["a read-only plan", timelineWith({ kind: "element", id: firstPhoto.id }, readOnly)],
  ] as const) assert.equal(all(auditHtml(html, name), (node) => node.attrs.role === "separator").length, 0, name);
});

test("the clips can be dragged to be reordered, unless the plan is read-only", () => {
  const draggable = (html: string) => all(parseHtml(html), (node) => node.attrs.draggable === "true" && /clip__grab/.test(node.attrs.class ?? "")).length;
  assert.equal(draggable(timelineWith(NO_SELECTION)), planDocument.plan.elements.length);
  assert.equal(draggable(timelineWith(NO_SELECTION, readOnly)), 0);
});

test("the Detail panel of a photo has a duration slider with the server's range and the buttons that do not need a drag", () => {
  const root = auditHtml(render(detail({ kind: "element", id: firstPhoto.id })), "detail photo");
  const slider = all(root, (node) => node.tag === "input" && node.attrs.type === "range")[0]!;
  assert.deepEqual([slider.attrs.min, slider.attrs.max, slider.attrs.step, slider.attrs["aria-valuetext"]], ["1", "30", "0.1", "4,5 s"]);
  assert.equal(accessibleName(slider, root), "Durata della foto");
  const buttons = all(root, (node) => node.tag === "button").map((node) => accessibleName(node, root));
  for (const name of ["Sposta prima", "Sposta dopo", "Rimuovi dalla timeline", "Nascondi la foto"]) assert.ok(buttons.includes(name), name);
  const first = all(root, (node) => node.tag === "button" && accessibleName(node, root) === "Sposta prima")[0]!;
  assert.ok("disabled" in first.attrs, "the first clip cannot move earlier");
});

test("the Detail panel of a moment has its start and its end, inside the bounds of the server", () => {
  const root = auditHtml(render(detail({ kind: "element", id: firstMoment.id })), "detail moment");
  const sliders = all(root, (node) => node.tag === "input" && node.attrs.type === "range");
  assert.deepEqual(sliders.map((node) => accessibleName(node, root)), ["Inizio del momento", "Fine del momento"]);
  assert.deepEqual(sliders.map((node) => [node.attrs.min, node.attrs.max]), [["0", String((firstMoment.end ?? 0) - 1)], [String((firstMoment.start ?? 0) + 1), "12"]]);
  assert.match(render(detail({ kind: "element", id: firstMoment.id })), /Elimina il momento/);
});

test("a read-only plan offers no edit control in the Detail panel", () => {
  for (const selection of [{ kind: "element", id: firstPhoto.id }, { kind: "candidate", id: catalog.photos.find((item) => item.status !== "selected")!.id }, { kind: "video", media: catalog.videos[0]!.media }] as Selection[]) {
    const html = render(detail(selection, { actions: readOnly }));
    const root = auditHtml(html, "detail read-only");
    assert.equal(all(root, (node) => node.tag === "input" && node.attrs.type === "range").length, 0);
    assert.match(html, /in sola lettura/);
    assert.doesNotMatch(html, /Aggiungi alla timeline|Rimuovi dalla timeline|Nascondi la foto|Aggiungi tutti i momenti/);
  }
});

test("a candidate offers to add it or hide it, a hidden one to restore it", () => {
  const free = catalog.photos.find((item) => item.discard !== null && item.status === "discarded")!;
  const hidden = catalog.photos.find((item) => item.status === "excluded")!;
  const names = (id: string) => { const root = parseHtml(render(detail({ kind: "candidate", id }))); return all(root, (node) => node.tag === "button").map((node) => accessibleName(node, root)); };
  assert.ok(names(free.id).includes("Aggiungi alla timeline") && names(free.id).includes("Nascondi la foto"));
  assert.ok(names(hidden.id).includes("Ripristina") && !names(hidden.id).includes("Aggiungi alla timeline"));
});

test("the video panel can add all the moments that are not used, a new moment, and bring deleted ones back", () => {
  const html = render(detail({ kind: "video", media: catalog.videos[1]!.media }));
  auditHtml(html, "detail video");
  for (const text of ["Aggiungi tutti i momenti", "Aggiungi un momento", "Un clic su un punto libero"]) assert.match(html, new RegExp(text));
  assert.doesNotMatch(html, /Ripristina i momenti eliminati/, "none was deleted");
  const withDeleted = clone(catalog);
  withDeleted.videos[1]!.moments[0]!.status = "excluded";
  assert.match(render(detail({ kind: "video", media: catalog.videos[1]!.media }, { catalog: withDeleted })), /Ripristina i momenti eliminati/);
});

const mediaTab = (tab: MediaTab, actions = editable, view = catalog) => render(
  <MediaPanel projectId={projectPlanned.id} catalog={view} document={planDocument} waveforms={waveforms} selection={NO_SELECTION}
    tab={tab} actions={actions} onTab={noop} onSelect={noop} />);

test("every photo can be hidden or restored with a named button, and a group can be added whole", () => {
  const html = mediaTab("photos");
  const root = auditHtml(html, "photos with actions");
  const names = all(root, (node) => node.tag === "button").map((node) => accessibleName(node, root));
  assert.ok(names.some((name) => /^Nascondi la foto IMG_\d+\.jpg$/.test(name)));
  assert.ok(!names.some((name) => /^Aggiungi alla timeline le foto del gruppo/.test(name)), "every photo of the fixture is already in the timeline");
  const spare = clone(catalog);
  spare.photos.forEach((photo) => { if (photo.status === "selected") photo.status = "available"; });
  const rootSpare = auditHtml(mediaTab("photos", editable, spare), "photos with something to add");
  const addAll = all(rootSpare, (node) => node.tag === "button" && /^Aggiungi alla timeline le foto del gruppo/.test(node.attrs["aria-label"] ?? ""));
  assert.ok(addAll.length >= 1);
  assert.equal(all(parseHtml(mediaTab("photos", readOnly, spare)), (node) => /^Aggiungi alla timeline le foto/.test(node.attrs["aria-label"] ?? "")).length, 0);
  const hidden = all(root, (node) => node.tag === "button" && /^Ripristina la foto/.test(node.attrs["aria-label"] ?? ""));
  assert.equal(hidden.length, 0, "the hidden group starts collapsed");
  assert.equal(all(parseHtml(mediaTab("photos", readOnly)), (node) => node.tag === "button" && /^(Nascondi|Ripristina) la foto/.test(node.attrs["aria-label"] ?? "")).length, 0);
  assert.equal(all(parseHtml(mediaTab("photos", readOnly)), (node) => node.attrs.draggable === "true").length, 0);
});

test("the photos that can be added are draggable, the ones in the timeline are not", () => {
  const spare = clone(catalog);
  spare.photos.forEach((photo, index) => { photo.status = index % 2 === 0 ? "available" : "selected"; photo.discard = null; });
  const cells = all(parseHtml(mediaTab("photos", editable, spare)), (node) => node.tag === "li" && node.attrs.class === "thumbcell");
  assert.ok(cells.length > 0);
  assert.ok(cells.some((cell) => cell.attrs.draggable === "true") && cells.some((cell) => cell.attrs.draggable !== "true"));
});

test("the bar of a video has handles for the moments in the timeline and drags for the others", () => {
  const view = clone(catalog);
  const root = auditHtml(mediaTab("videos", editable, view), "videos with handles");
  const handles = all(root, (node) => node.attrs.role === "separator");
  assert.equal(handles.length, 2, "one moment of the first video is in the timeline: a start and an end");
  assert.deepEqual(handles.map((node) => node.attrs["aria-valuenow"]), [String(firstMoment.start), String(firstMoment.end)]);
  assert.match(handles[0]!.attrs["aria-label"] ?? "", /^Inizio del momento: VID_0000\.mp4$/);
  assert.ok(all(root, (node) => node.attrs.class?.startsWith("moment ") === true && node.attrs.draggable === "true").length >= 2);
  assert.equal(all(parseHtml(mediaTab("videos", readOnly, view)), (node) => node.attrs.role === "separator").length, 0);
  assert.match(mediaTab("videos"), /Aggiungi i momenti/);
});

test("the top bar has undo and redo with their shortcuts, and says whether the plan is saved", () => {
  const bar = (edit: Partial<EditBar>) => render(<TopBar project={projectPlanned} catalog={catalog} requested={30} working={false}
    edit={{ canUndo: true, canRedo: false, onUndo: noop, onRedo: noop, save: "saved", readOnly: false, ...edit }} onChangeFolder={noop} onSettings={noop} />);
  const html = bar({});
  const root = auditHtml(html, "top bar with editing");
  const undo = all(root, (node) => node.tag === "button" && node.attrs["aria-label"] === "Annulla (Ctrl+Z)")[0]!;
  const redo = all(root, (node) => node.tag === "button" && node.attrs["aria-label"] === "Ripeti (Ctrl+Maiusc+Z)")[0]!;
  assert.deepEqual(["disabled" in undo.attrs, "disabled" in redo.attrs], [false, true]);
  assert.match(html, /Salvato/);
  assert.match(bar({ save: "unsaved" }), /Modifiche da salvare/);
  assert.match(bar({ save: "saving" }), /Salvataggio…/);
  assert.match(bar({ save: "conflict" }), /Piano cambiato altrove/);
  assert.match(bar({ readOnly: true }), /Sola lettura/);
  assert.ok("disabled" in all(parseHtml(bar({ readOnly: true })), (node) => node.attrs["aria-label"] === "Annulla (Ctrl+Z)")[0]!.attrs);
});

test("the notice after a removal has an undo button and a close button", () => {
  const html = render(<Toast text="Elemento rimosso" undo="Annulla" onUndo={noop} closeLabel="Chiudi l'avviso" onClose={noop} />);
  const root = auditHtml(html, "toast");
  assert.equal(all(root, (node) => node.tag === "button").length, 2);
  assert.equal(all(root, (node) => node.attrs.role === "status").length, 1);
  assert.equal(all(parseHtml(render(<Toast text="x" closeLabel="Chiudi" onClose={noop} />)), (node) => node.tag === "button").length, 1, "a notice that only informs");
});

test("the confirmation of a regeneration says what is kept when the plan has edits, and the settings offer it", () => {
  const plain = render(<RegenerateDialog edited={false} working={false} onConfirm={noop} onCancel={noop} />);
  const edited = render(<RegenerateDialog edited working={false} onConfirm={noop} onCancel={noop} />);
  const root = auditHtml(edited, "regenerate");
  assert.equal(root.children[0]?.attrs.role, "alertdialog");
  assert.doesNotMatch(plain, /plan_history/);
  assert.match(edited, /plan_history/);
  assert.match(edited, /nascoste e i momenti eliminati/);
  assert.equal(all(root, (node) => node.tag === "button").length, 2);
  const settings = render(<SettingsForm project={projectPlanned} mode="edit" onSaved={noop} onStarted={noop} ai={null} tracks={[]} />);
  assert.match(settings, /Rigenera il piano…/);
  assert.doesNotMatch(settings, /Rigenerare il piano\?/, "the confirmation appears on the click");
});

test("the editor renders its panels on the plan of the server, saved, with its three regions named", () => {
  const html = render(<Workspace project={projectPlanned} initial={planDocument} catalog={catalog} incomplete={false}
    onSaved={noop} onJobDone={noop} onReload={noop} onChangeFolder={noop} onSettings={noop} />);
  const root = auditHtml(html, "workspace");
  assert.ok(all(root, (node) => node.attrs["aria-labelledby"] === "media-title").length === 1);
  assert.ok(all(root, (node) => node.attrs["aria-labelledby"] === "detail-title").length === 1);
  assert.ok(all(root, (node) => node.tag === "section" && node.attrs["aria-label"] === "Timeline finale").length === 1);
  assert.match(html, /Salvato/);
  assert.doesNotMatch(html, /alertdialog/);
  const busy = clone(projectPlanned);
  busy.busy = true;
  assert.match(render(<Workspace project={busy} initial={planDocument} catalog={catalog} incomplete={false} onSaved={noop} onJobDone={noop} onReload={noop} onChangeFolder={noop} onSettings={noop} />), /Sola lettura/);
});


// --- transitions and music (U6) ---

const allEffects = planDocument.plan.elements.flatMap((element) => element.effects.map((effect) => ({ element, effect })));
const dip = allEffects.find(({ effect }) => effect.type === "dip")!;
const dissolve = allEffects.find(({ effect }) => effect.type === "dissolve")!;
const fadeIn = allEffects.find(({ effect }) => effect.type === "fade_in")!;
const fadeOut = allEffects.find(({ effect }) => effect.type === "fade_out")!;
const effectSelection = ({ element, effect }: (typeof allEffects)[number]): Selection => ({ kind: "effect", element: element.id, id: effect.id });
const buttonNames = (root: HtmlNode) => all(root, (node) => node.tag === "button").map((node) => accessibleName(node, root));

test("the transitions on the clips can be dragged, and so can the track names of the music lane, unless the plan is read-only", () => {
  const draggable = (html: string, pattern: RegExp) => all(parseHtml(html), (node) => node.attrs.draggable === "true" && pattern.test(node.attrs.class ?? "")).length;
  const tracks = planDocument.plan.music?.tracks.length ?? 0;
  assert.equal(draggable(timelineWith(NO_SELECTION), /clip__fxwrap/), allEffects.length);
  assert.equal(draggable(timelineWith(NO_SELECTION), /track__tab/), tracks);
  assert.equal(draggable(timelineWith(NO_SELECTION, readOnly), /clip__fxwrap|track__tab/), 0);
});

test("a selected dip shows two handles that announce their time and keep to the free space; a fade-in keeps its start on the edge of the clip", () => {
  const dipRoot = auditHtml(timelineWith(effectSelection(dip)), "timeline with a dip selected");
  const handles = all(dipRoot, (node) => node.attrs.role === "separator");
  assert.deepEqual(handles.map((node) => node.attrs["aria-label"]), ["Transizione: inizio", "Transizione: fine"]);
  assert.ok(handles.every((node) => !("aria-disabled" in node.attrs) && node.attrs.tabindex === "0"));
  const [start, end] = handles;
  assert.equal(start?.attrs["aria-valuenow"], String(dip.effect.offset));
  assert.equal(end?.attrs["aria-valuenow"], String(dip.effect.offset + dip.effect.duration));
  assert.ok(Number(start?.attrs["aria-valuemin"]) <= dip.effect.offset && Number(end?.attrs["aria-valuemax"]) <= dip.element.duration);

  const fadeRoot = auditHtml(timelineWith(effectSelection(fadeIn)), "timeline with a fade-in selected");
  const [fixed, free] = all(fadeRoot, (node) => node.attrs.role === "separator");
  assert.ok("aria-disabled" in (fixed?.attrs ?? {}) && fixed?.attrs.tabindex === "-1", "the start of a fade-in is the start of the clip");
  assert.ok(!("aria-disabled" in (free?.attrs ?? {})));
  const [, closing] = all(auditHtml(timelineWith(effectSelection(fadeOut)), "fade-out selected"), (node) => node.attrs.role === "separator");
  assert.ok("aria-disabled" in (closing?.attrs ?? {}), "the end of a fade-out is the end of the clip");
  assert.equal(all(parseHtml(timelineWith(effectSelection(dip), readOnly)), (node) => node.attrs.role === "separator").length, 0);
});

test("a part of the music is a named button; the plan without music has no part and no track name", () => {
  const root = auditHtml(timelineWith(NO_SELECTION), "music lane");
  assert.equal(all(root, (node) => node.tag === "button" && /^Parte \d+ di brano\.wav/.test(node.attrs["aria-label"] ?? "")).length, 2);
  assert.ok(buttonNames(root).some((name) => /silenziata/i.test(name)), "the muted part says so");
  assert.match(timelineWith(NO_SELECTION), /Taglia la musica qui/);
  const bare = clone(planDocument);
  bare.plan.music = undefined;
  const html = render(<Timeline projectId="p" document={bare} waveforms={new Map()} selection={NO_SELECTION} playhead={0} actions={editable} onSelect={noop} onSeek={noop} />);
  auditHtml(html, "music lane without music");
  assert.doesNotMatch(html, /Taglia la musica qui|track__tab/);
});

test("the cut of the music is off where the playhead is not on a part, and in a read-only plan", () => {
  const cutButton = (html: string) => {
    const root = parseHtml(html);
    return all(root, (node) => node.tag === "button" && accessibleName(node, root) === "Taglia la musica qui")[0];
  };
  const at = (time: number, actions = editable) => render(<Timeline projectId="p" document={planDocument} waveforms={waveforms} selection={NO_SELECTION} playhead={time} actions={actions} onSelect={noop} onSeek={noop} />);
  assert.ok(!("disabled" in (cutButton(at(5))?.attrs ?? { disabled: "" })));
  assert.ok("disabled" in (cutButton(at(25))?.attrs ?? {}), "the music ends at 20 s");
  assert.ok("disabled" in (cutButton(at(5, readOnly))?.attrs ?? {}));
});

test("a clip offers the transitions without a drag, and the one that has no room is off", () => {
  const root = auditHtml(render(detail({ kind: "element", id: planDocument.plan.elements[0]!.id })), "detail first clip");
  const names = ["Da nero all'inizio", "Dissolvenza all'inizio", "Dissolvenza alla fine", "A nero alla fine", "Passaggio per il nero qui"];
  for (const name of names) assert.ok(buttonNames(root).includes(name), name);
  const state = (name: string) => "disabled" in all(root, (node) => node.tag === "button" && accessibleName(node, root) === name)[0]!.attrs;
  assert.ok(state("Dissolvenza all'inizio"), "the first clip has no clip before it");
  assert.ok(!state("A nero alla fine") && !state("Passaggio per il nero qui"));
  const readOnlyHtml = render(detail({ kind: "element", id: planDocument.plan.elements[0]!.id }, { actions: readOnly }));
  for (const name of names) assert.ok(!readOnlyHtml.includes(name), `${name} in a read-only plan`);
});

test("the Detail panel of a transition has its duration and start sliders, a remove button and the keys", () => {
  const root = auditHtml(render(detail(effectSelection(dip))), "detail dip");
  const sliders = all(root, (node) => node.tag === "input" && node.attrs.type === "range");
  assert.deepEqual(sliders.map((node) => accessibleName(node, root)), ["Durata", "Inizia a"]);
  assert.ok(sliders.every((node) => !("disabled" in node.attrs)));
  assert.ok(buttonNames(root).includes("Rimuovi la transizione"));
  assert.match(render(detail(effectSelection(dip))), /frecce ±0,1 s per spostarla/);
  const fixed = auditHtml(render(detail(effectSelection(dissolve))), "detail dissolve");
  const [, start] = all(fixed, (node) => node.tag === "input" && node.attrs.type === "range");
  assert.ok("disabled" in (start?.attrs ?? {}), "a dissolve begins at the start of its clip");
  assert.match(render(detail(effectSelection(dissolve))), /per cambiarne la durata/);
  assert.equal(all(parseHtml(render(detail(effectSelection(dip), { actions: readOnly }))), (node) => node.tag === "input").length, 0);
});

test("a transition of the library can be applied to the whole video; the hard cut is one of them, the dip has no place", () => {
  for (const type of ["cut", "dissolve", "fade_in", "fade_out"] as const) {
    const root = auditHtml(render(detail({ kind: "library", type })), `library ${type}`);
    const apply = all(root, (node) => node.tag === "button" && accessibleName(node, root) === "Applica a tutto il video")[0]!;
    assert.ok(!("disabled" in apply.attrs), type);
  }
  const dipRoot = auditHtml(render(detail({ kind: "library", type: "dip" })), "library dip");
  assert.ok("disabled" in all(dipRoot, (node) => node.tag === "button" && accessibleName(node, dipRoot) === "Applica a tutto il video")[0]!.attrs);
  assert.match(render(detail({ kind: "library", type: "cut" })), /Taglio netto/);
  assert.doesNotMatch(render(detail({ kind: "library", type: "cut" }, { actions: readOnly })), /Applica a tutto il video/);
});

test("the Media panel lists the transitions in the order of the spec, the hard cut first, and every transition of the library can be dragged", () => {
  const html = mediaTab("transitions");
  const root = auditHtml(html, "transitions tab");
  const items = all(root, (node) => node.tag === "button" && /library__item/.test(node.attrs.class ?? ""));
  assert.deepEqual(items.map((node) => accessibleName(node, root)).map((name) => name.split(/\s{2,}|Durata|toglie/)[0]?.trim()), ["Taglio netto", "Dissolvenza", "Da nero", "A nero", "Passaggio per il nero"]);
  assert.equal(all(root, (node) => node.attrs.draggable === "true").length, 5);
  assert.equal(all(parseHtml(mediaTab("transitions", readOnly)), (node) => node.attrs.draggable === "true").length, 0);
});

test("the Music tab adds a track with a named button, all of them, and restores what was deleted; the tracks can be dragged", () => {
  const root = auditHtml(mediaTab("music"), "music tab");
  assert.ok(buttonNames(root).includes("Aggiungi al video: brano.wav"));
  assert.equal(all(root, (node) => node.attrs.draggable === "true").length, catalog.music.tracks.length);
  const toolbar = all(root, (node) => node.tag === "button" && ["Aggiungi i brani", "Ripristina le parti eliminate"].includes(accessibleName(node, root)));
  assert.equal(toolbar.length, 2);
  assert.ok(toolbar.every((node) => "disabled" in node.attrs), "the track is in the video and nothing is deleted (it is whole: 20 s of 20 s)");
  const trimmed = clone(planDocument);
  trimmed.plan.music!.tracks[0]!.parts = [{ start: 0, end: 12, muted: false, fade_in: 1, fade_out: 0 }];
  const withGap = render(<MediaPanel projectId="p" catalog={catalog} document={trimmed} waveforms={waveforms} selection={NO_SELECTION} tab="music" actions={editable} onTab={noop} onSelect={noop} />);
  const rootGap = auditHtml(withGap, "music tab with a deleted stretch");
  assert.ok(!("disabled" in all(rootGap, (node) => node.tag === "button" && accessibleName(node, rootGap) === "Ripristina le parti eliminate")[0]!.attrs));
  const locked = all(parseHtml(mediaTab("music", readOnly)), (node) => node.tag === "button" && ["Aggiungi i brani", "Ripristina le parti eliminate", "Aggiungi al video: brano.wav"].includes(node.attrs["aria-label"] ?? "") || node.attrs.draggable === "true");
  assert.ok(locked.filter((node) => node.attrs.draggable === "true").length === 0);
});

test("the Detail panel of a part of the music has mute, both fades and the shift, inside the bounds, and the buttons that replace the drags", () => {
  const root = auditHtml(render(detail({ kind: "part", track: 0, part: 0 })), "detail part");
  const sliders = all(root, (node) => node.tag === "input" && node.attrs.type === "range");
  assert.deepEqual(sliders.map((node) => accessibleName(node, root)), ["Dissolvenza in entrata", "Dissolvenza in uscita", "Spostamento della musica"]);
  const shift = sliders[2]!;
  assert.deepEqual([shift.attrs.min, shift.attrs.step], [String(planDocument.limits.music!.offset.min), "0.1"]);
  assert.ok(Math.abs(Number(shift.attrs.max) - planDocument.limits.music!.offset.max) < 1e-9, "the shift stops where the server says");
  for (const name of ["Silenzia la parte", "Taglia la musica qui", "Elimina la parte", "Brano prima", "Brano dopo", "Togli il brano dal video", "Riporta la musica all'inizio"]) {
    assert.ok(buttonNames(root).includes(name), name);
  }
  const state = (name: string) => "disabled" in all(root, (node) => node.tag === "button" && accessibleName(node, root) === name)[0]!.attrs;
  assert.ok(state("Brano prima") && state("Brano dopo") && state("Riporta la musica all'inizio"), "one track, not shifted");
  assert.ok(buttonNames(parseHtml(render(detail({ kind: "part", track: 0, part: 1 })))).includes("Riattiva l'audio della parte"), "the second part is muted");
  assert.equal(all(parseHtml(render(detail({ kind: "part", track: 0, part: 0 }, { actions: readOnly }))), (node) => node.tag === "input").length, 0);
});

test("the Detail panel of a track offers to add it, to take it out and to bring back what was deleted", () => {
  const root = auditHtml(render(detail({ kind: "track", file: "brano.wav" })), "detail track");
  for (const name of ["Aggiungi al video", "Togli il brano dal video"]) assert.ok(buttonNames(root).includes(name), name);
  assert.ok(!buttonNames(root).includes("Ripristina le parti eliminate"), "nothing deleted");
  const trimmed = clone(planDocument);
  trimmed.plan.music!.tracks[0]!.parts = [{ start: 0, end: 12, muted: false, fade_in: 1, fade_out: 0 }];
  assert.ok(buttonNames(parseHtml(render(detail({ kind: "track", file: "brano.wav" }, { document: trimmed })))).includes("Ripristina le parti eliminate"));
});
