// Static accessibility checks without a real browser (section 15 of UI-SPEC.md): the components are rendered with
// `react-dom/server` on fixture data and every control must have a name, every reference an existing target.
import assert from "node:assert/strict";
import { createRef, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { test } from "node:test";
import { DetailPanel, type DetailProps } from "../src/components/DetailPanel";
import { MediaPanel, type MediaTab } from "../src/components/MediaPanel";
import { Timeline } from "../src/components/Timeline";
import { TopBar } from "../src/components/TopBar";
import { FolderList } from "../src/screens/FoldersScreen";
import { SettingsForm } from "../src/screens/SettingsScreen";
import { ProgressScreen } from "../src/screens/ProgressScreen";
import { NO_SELECTION, type Selection } from "../src/lib/selection";
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
    selection={selection} playhead={3} seekVersion={0} job={null} collapsed={false} playerRef={createRef<HTMLVideoElement>()}
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
      selection={NO_SELECTION} tab={tab} onTab={noop} onSelect={noop} />);
    const root = auditHtml(html, `media ${tab}`);
    const tabs = all(root, (node) => node.attrs.role === "tab");
    assert.equal(tabs.length, 4);
    assert.equal(tabs.filter((node) => node.attrs["aria-selected"] === "true").length, 1);
    assert.equal(all(root, (node) => node.attrs.role === "tabpanel").length, 1);
  });
}

test("a thumbnail says its score, whether it is in the timeline and how many similar photos there are", () => {
  const html = render(<MediaPanel projectId={projectPlanned.id} catalog={catalog} document={planDocument} waveforms={waveforms}
    selection={NO_SELECTION} tab="photos" onTab={noop} onSelect={noop} />);
  const root = parseHtml(html);
  const used = all(root, (node) => node.tag === "button" && /Nella timeline/.test(node.attrs["aria-label"] ?? ""));
  assert.ok(used.length > 0);
  assert.match(used[0]?.attrs["aria-label"] ?? "", /^Foto .*\.jpg, punteggio 0,\d\d, Nella timeline/);
});

test("the photo tab shows the threshold slider with its value and the groups collapse", () => {
  const html = render(<MediaPanel projectId={projectPlanned.id} catalog={catalog} document={planDocument} waveforms={waveforms}
    selection={NO_SELECTION} tab="photos" onTab={noop} onSelect={noop} />);
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
    playhead={12} onSelect={noop} onSeek={noop} />);
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
  const html = render(<Timeline projectId="p" document={empty} waveforms={new Map()} selection={NO_SELECTION} playhead={0} onSelect={noop} onSeek={noop} />);
  auditHtml(html, "empty timeline");
  assert.match(html, /La timeline è vuota/);
  assert.match(html, /Il piano non ha musica/);
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
