"""`review.html`: the local page where the user watches the versions, flags clips and rates them.

The page is one self-contained file: CSS, script and data are inline, the previews and thumbnails
are relative paths inside the run folder, and nothing is fetched, so it works by opening the file
(`file://`) on any computer the folder is copied to. It is blind: versions are only letters, and
it carries no variant names, reasons or scores. It keeps the work in the browser's local storage
and exports `ratings.json` with a button.

`page_data` builds the data model from `prepare.json` and the persistent labels, apart from the
HTML of `render_page`, so the review gallery of Milestone 4 can reuse it.
"""

import json

from vacation_memories.evaluate.labels import LENGTHS, Labels, Segment

PAGE_NAME = "review.html"
PAGE_VERSION = 1
RATINGS_VERSION = 1
RATINGS_NAME = "ratings.json"
RHYTHMS = ("too_slow", "right", "too_fast")
MIN_RATING, MAX_RATING = 1, 5


def segment_of(entry: dict) -> Segment:
    return Segment(entry["source"], entry["start"], entry["end"])


def page_data(prepared: dict, labels: Labels) -> dict:
    """What the page shows, with the flags of earlier cycles already filled in."""
    segments = prepared["segments"]
    variants = []
    shown: list[str] = []
    for letter in sorted(prepared["variants"]):
        variant = prepared["variants"][letter]
        clips = []
        for clip in variant["clips"]:
            entry = segments[clip["key"]]
            clips.append({
                "key": clip["key"], "thumb": entry["thumb"], "kind": entry["kind"], "start": clip["start"],
                "duration": clip["duration"], "section": clip["section"], "moment_type": entry["moment_type"],
                "day": entry["day"],
            })
            shown.append(clip["key"])
        variants.append({
            "label": letter, "video": variant["video"],
            "sections": [section["name"] for section in variant["sections"] if section["clips"]],
            "clips": clips,
        })
    excluded = []
    for item in prepared["excluded"]:
        entry = segments[item["key"]]
        excluded.append({"key": item["key"], "thumb": entry["thumb"], "kind": entry["kind"], "day": entry["day"]})
        shown.append(item["key"])

    known, new = {}, []
    for key in dict.fromkeys(shown):
        label = labels.match(segment_of(segments[key]))
        if label is None:
            new.append(key)
        else:
            known[key] = label.flags()
    return {
        "version": PAGE_VERSION,
        "ratings_version": RATINGS_VERSION,
        "run_id": prepared["run_id"],
        "variants": variants,
        "excluded": excluded,
        "labels": known,
        "new": new,
        "rhythms": list(RHYTHMS),
        "lengths": list(LENGTHS),
        "rating_range": [MIN_RATING, MAX_RATING],
    }


def render_page(data: dict) -> str:
    """The HTML of the page, with `data` inline (safe inside a script element)."""
    payload = (json.dumps(data, ensure_ascii=True, separators=(",", ":"))
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return _TEMPLATE.replace("__DATA__", payload)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vacation Memories review</title>
<style>
:root {
  --bg: #f6f5f2; --panel: #ffffff; --text: #1f2328; --muted: #5d6470; --line: #d9d6cf;
  --accent: #2f6db5; --accent-text: #ffffff; --new: #b5562f; --known: #3d7a4a; --row: #f0efeb;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17191c; --panel: #202328; --text: #e6e6e3; --muted: #a2a8b1; --line: #363a41;
    --accent: #6ea3e6; --accent-text: #0e1116; --new: #e08a5f; --known: #79bf87; --row: #272a30;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.45 system-ui, sans-serif; }
header, main { max-width: 1100px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 0 0 12px; }
p.hint { color: var(--muted); margin: 4px 0 0; }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
nav button { border: 1px solid var(--line); background: var(--panel); color: var(--text); border-radius: 6px;
  padding: 6px 14px; font: inherit; cursor: pointer; }
nav button[aria-selected="true"] { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
section.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
video { width: 100%; max-height: 62vh; background: #000; border-radius: 6px; }
.controls { display: flex; flex-wrap: wrap; gap: 16px 32px; margin: 14px 0; }
fieldset { border: 1px solid var(--line); border-radius: 6px; margin: 0; padding: 8px 12px; }
legend { color: var(--muted); padding: 0 4px; }
label { margin-right: 10px; white-space: nowrap; }
select { font: inherit; background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 4px; }
.table { width: 100%; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 560px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: middle; }
th { color: var(--muted); font-weight: 600; }
tr.current { background: var(--row); }
img.thumb { width: 96px; height: 64px; object-fit: cover; border-radius: 4px; display: block; background: var(--row); }
button.time { border: 0; background: none; color: var(--accent); font: inherit; cursor: pointer; padding: 0;
  text-decoration: underline; }
.badge { display: inline-block; font-size: 0.75rem; border-radius: 10px; padding: 0 7px; margin-left: 6px;
  border: 1px solid currentColor; }
.badge.new { color: var(--new); }
.badge.known { color: var(--known); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 12px; }
.card { border: 1px solid var(--line); border-radius: 6px; padding: 8px; }
.card img.thumb { width: 100%; height: 110px; }
.card.on { outline: 2px solid var(--accent); }
.export { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 16px; }
.export button { background: var(--accent); color: var(--accent-text); border: 0; border-radius: 6px; padding: 8px 18px;
  font: inherit; cursor: pointer; }
.muted { color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>Vacation Memories review</h1>
  <p class="hint">Watch every version, flag the clips, rate it, then open <b>Rank and export</b> and save
  <code>ratings.json</code>. Your work is kept in this browser while you go.</p>
  <nav id="tabs" role="tablist"></nav>
</header>
<main id="view"></main>
<script type="application/json" id="review-data">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("review-data").textContent);
const STORE_KEY = "vacation-memories-review:" + DATA.run_id;
const NEW = new Set(DATA.new);

// --- the model: what the user decided, apart from how it is shown ---

function emptyState() {
  const state = { version: DATA.ratings_version, run_id: DATA.run_id, variants: {}, excluded: {} };
  for (const variant of DATA.variants) {
    const entry = { rating: null, rank: null, rhythm: {}, clips: {} };
    for (const name of variant.sections) entry.rhythm[name] = null;
    for (const clip of variant.clips) {
      const known = DATA.labels[clip.key] || {};
      entry.clips[clip.key] = { repetitive: known.repetitive === true, length: known.length || "ok" };
    }
    state.variants[variant.label] = entry;
  }
  for (const item of DATA.excluded) {
    const known = DATA.labels[item.key] || {};
    state.excluded[item.key] = { important: known.important === true };
  }
  return state;
}

function restore(state) {
  let saved = null;
  try { saved = JSON.parse(window.localStorage.getItem(STORE_KEY)); } catch (error) { saved = null; }
  if (!saved || saved.run_id !== DATA.run_id || saved.version !== DATA.ratings_version) return state;
  for (const [letter, entry] of Object.entries(state.variants)) {
    const old = (saved.variants || {})[letter];
    if (!old) continue;
    entry.rating = old.rating ?? null;
    entry.rank = old.rank ?? null;
    for (const name of Object.keys(entry.rhythm)) entry.rhythm[name] = (old.rhythm || {})[name] ?? null;
    for (const [key, flags] of Object.entries(entry.clips)) Object.assign(flags, (old.clips || {})[key] || {});
  }
  for (const [key, flags] of Object.entries(state.excluded)) Object.assign(flags, (saved.excluded || {})[key] || {});
  return state;
}

const state = restore(emptyState());

function save() {
  try { window.localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (error) { /* private window */ }
}

function exportRatings() {
  const blob = new Blob([JSON.stringify(state, null, 2) + "\\n"], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "ratings.json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// --- the page ---

const TITLES = { too_slow: "too slow", right: "right", too_fast: "too fast", ok: "ok", too_long: "too long",
  too_short: "too short" };
let current = DATA.variants.length ? DATA.variants[0].label : "excluded";

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (value !== null && value !== false) node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) node.append(child instanceof Node ? child : String(child));
  return node;
}

function sectionName(name) { return name.charAt(0).toUpperCase() + name.slice(1); }
function clock(seconds) {
  const whole = Math.max(0, seconds);
  const minutes = Math.floor(whole / 60);
  return minutes + ":" + (whole - minutes * 60).toFixed(1).padStart(4, "0");
}
function badge(key) {
  if (NEW.has(key)) return element("span", { class: "badge new", title: "not seen in earlier cycles" }, "new");
  if (DATA.labels[key]) return element("span", { class: "badge known", title: "flags from an earlier cycle" }, "earlier");
  return "";
}
function thumb(path) {
  return path ? element("img", { class: "thumb", src: path, alt: "", loading: "lazy" }) : element("span", { class: "muted" }, "-");
}
function choice(name, value, current, text, onchange) {
  return element("label", {}, element("input", { type: "radio", name, value, checked: current === value, onchange }), " ", text);
}

function renderTabs() {
  const tabs = document.getElementById("tabs");
  tabs.replaceChildren();
  const entries = DATA.variants.map((variant) => [variant.label, "Version " + variant.label]);
  entries.push(["excluded", "Left out"], ["export", "Rank and export"]);
  for (const [id, text] of entries) {
    tabs.append(element("button", { role: "tab", "aria-selected": String(id === current),
      onclick: () => { current = id; render(); } }, text));
  }
}

function renderVariant(variant) {
  const entry = state.variants[variant.label];
  const video = element("video", { controls: true, preload: "metadata", src: variant.video });
  const rows = [];
  const rating = element("fieldset", {}, element("legend", {}, "Overall rating"),
    ...Array.from({ length: DATA.rating_range[1] - DATA.rating_range[0] + 1 }, (_, i) => DATA.rating_range[0] + i)
      .map((value) => choice("rating-" + variant.label, String(value), String(entry.rating), String(value),
        () => { entry.rating = value; save(); })));
  const rhythm = element("fieldset", {}, element("legend", {}, "Rhythm of each part"),
    ...variant.sections.map((name) => element("label", {}, sectionName(name) + " ",
      element("select", { onchange: (event) => { entry.rhythm[name] = event.target.value || null; save(); } },
        element("option", { value: "", selected: entry.rhythm[name] === null }, "-"),
        ...DATA.rhythms.map((value) => element("option", { value, selected: entry.rhythm[name] === value }, TITLES[value]))))));
  const body = variant.clips.map((clip) => {
    const flags = entry.clips[clip.key];
    const row = element("tr", {},
      element("td", {}, thumb(clip.thumb)),
      element("td", {}, element("button", { class: "time", title: "play from here",
        onclick: () => { video.currentTime = clip.start; video.play(); } }, clock(clip.start)), badge(clip.key)),
      element("td", {}, clip.duration.toFixed(1) + " s"),
      element("td", {}, sectionName(clip.section)),
      element("td", {}, clip.moment_type || element("span", { class: "muted" }, "-")),
      element("td", {}, element("label", {}, element("input", { type: "checkbox", checked: flags.repetitive,
        onchange: (event) => { flags.repetitive = event.target.checked; save(); } }), " repetitive")),
      element("td", {}, element("select", { onchange: (event) => { flags.length = event.target.value; save(); } },
        ...DATA.lengths.map((value) => element("option", { value, selected: flags.length === value }, TITLES[value])))));
    rows.push([clip, row]);
    return row;
  });
  video.addEventListener("timeupdate", () => {
    for (const [clip, row] of rows) {
      const inside = video.currentTime >= clip.start && video.currentTime < clip.start + clip.duration;
      row.classList.toggle("current", inside);
    }
  });
  return element("section", { class: "panel" },
    element("h2", {}, "Version " + variant.label),
    video,
    element("div", { class: "controls" }, rating, rhythm),
    element("div", { class: "table" }, element("table", {},
      element("thead", {}, element("tr", {}, ...["", "Start", "Length", "Part", "Moment", "Repeats", "Duration"]
        .map((text) => element("th", {}, text)))),
      element("tbody", {}, body))));
}

function renderExcluded() {
  const cards = DATA.excluded.map((item) => {
    const flags = state.excluded[item.key];
    const card = element("div", { class: "card" + (flags.important ? " on" : "") },
      thumb(item.thumb),
      element("div", {}, "Day " + item.day + " · " + item.kind, badge(item.key)),
      element("label", {}, element("input", { type: "checkbox", checked: flags.important,
        onchange: (event) => { flags.important = event.target.checked; card.classList.toggle("on", flags.important); save(); } }),
        " important"));
    return card;
  });
  return element("section", { class: "panel" },
    element("h2", {}, "Moments left out"),
    element("p", { class: "hint" }, "Moments the selection left out. Flag the ones that should be in the video."),
    cards.length ? element("div", { class: "grid" }, cards) : element("p", { class: "muted" }, "Nothing to show."));
}

function renderExport() {
  const count = DATA.variants.length;
  const ranks = DATA.variants.map((variant) => {
    const entry = state.variants[variant.label];
    return element("label", {}, "Version " + variant.label + " ",
      element("select", { onchange: (event) => { entry.rank = event.target.value ? Number(event.target.value) : null; save(); render(); } },
        element("option", { value: "", selected: entry.rank === null }, "-"),
        ...Array.from({ length: count }, (_, i) => i + 1).map((position) =>
          element("option", { value: String(position), selected: entry.rank === position }, String(position)))));
  });
  const used = DATA.variants.map((variant) => state.variants[variant.label].rank).filter((rank) => rank !== null);
  const clash = new Set(used).size !== used.length;
  const unrated = DATA.variants.filter((variant) => state.variants[variant.label].rating === null).map((v) => v.label);
  return element("section", { class: "panel" },
    element("h2", {}, "Rank and export"),
    element("fieldset", {}, element("legend", {}, "From the best (1) to the worst"), ranks),
    element("p", { class: clash ? "" : "muted" }, clash ? "Two versions have the same place: give each its own." : ""),
    element("p", { class: "muted" }, unrated.length ? "Not rated yet: " + unrated.map((l) => "Version " + l).join(", ") + "." : "Every version is rated."),
    element("div", { class: "export" },
      element("button", { onclick: exportRatings, disabled: clash }, "Export ratings.json"),
      element("span", { class: "muted" }, "Copy the file into the folder of this page on the server, then run evaluate report.")));
}

function render() {
  renderTabs();
  const view = document.getElementById("view");
  const variant = DATA.variants.find((item) => item.label === current);
  view.replaceChildren(variant ? renderVariant(variant) : current === "excluded" ? renderExcluded() : renderExport());
}

render();
</script>
</body>
</html>
"""
