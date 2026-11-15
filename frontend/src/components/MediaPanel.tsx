// The Media panel (section 11): every candidate, selected, not selected and discarded, in four tabs. Photos and moments
// are added to the timeline by dragging them or with the buttons, hidden or deleted and restored; the bar of a video adds
// a moment with a click on a free point and trims the moments that are in the timeline (U5). The transitions and the
// music are read here: they are edited with step U6.
import { useRef, useState, type DragEvent, type MouseEvent } from "react";
import { urls } from "../api/client";
import type { Catalog, CatalogItem, CatalogVideo, LibraryTransition, PlanDocument, Waveform } from "../api/types";
import { useI18n, type MessageKey } from "../i18n";
import { discardKey, groupPhotos, hiddenMoments, isBelowThreshold, type PhotoGroupId } from "../lib/catalog";
import { DRAG_TYPE, beginDrag, encodePayload, endDrag, type DragPayload } from "../lib/dnd";
import { clock } from "../lib/format";
import { trimRange } from "../lib/limits";
import type { Selection } from "../lib/selection";
import { sameSelection } from "../lib/selection";
import type { EditActions } from "./editing";
import { Handle } from "./Handle";
import { CheckIcon, ChevronDownIcon, ChevronRightIcon, CopyIcon, EyeIcon, EyeOffIcon, MusicIcon, PlusIcon } from "./icons";

export type MediaTab = "photos" | "videos" | "transitions" | "music";
const TABS: readonly MediaTab[] = ["photos", "videos", "transitions", "music"];

interface Props {
  projectId: string;
  /** The catalog as the plan on screen has it (`withPlan`). */
  catalog: Catalog;
  document: PlanDocument | null;
  waveforms: ReadonlyMap<string, Waveform | null>;
  selection: Selection;
  tab: MediaTab;
  actions: EditActions;
  onTab: (tab: MediaTab) => void;
  onSelect: (selection: Selection) => void;
}

export const fileName = (path: string): string => path.split("/").pop() ?? path;

/** The props of an element that can be dragged to the timeline (a wrapper, because a button is not draggable in every browser). */
function dragProps(payload: DragPayload | null, readOnly: boolean) {
  if (!payload || readOnly) return {};
  return {
    draggable: true,
    onDragStart: (event: DragEvent<HTMLElement>) => {
      event.stopPropagation();
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData(DRAG_TYPE, encodePayload(payload));
      beginDrag(payload);
    },
    onDragEnd: () => endDrag(),
  };
}

const canAdd = (item: CatalogItem): boolean => item.status === "available" || item.status === "discarded";

export function MediaPanel({ projectId, catalog, document: doc, waveforms, selection, tab, actions, onTab, onSelect }: Props) {
  const { t } = useI18n();
  const counts: Record<MediaTab, number> = {
    photos: catalog.photos.length, videos: catalog.videos.length, transitions: catalog.transitions.length, music: catalog.music.tracks.length,
  };
  return (
    <section className="media" aria-labelledby="media-title">
      <header className="media__head">
        <h2 id="media-title">{t("media.title")}</h2>
        <div role="tablist" aria-label={t("media.tabs")} className="tabs">
          {TABS.map((name) => (
            <button key={name} type="button" role="tab" id={`tab-${name}`} aria-selected={tab === name} aria-controls={tab === name ? `panel-${name}` : undefined}
              tabIndex={tab === name ? 0 : -1} className={`tab${tab === name ? " is-active" : ""}`} onClick={() => onTab(name)}
              onKeyDown={(event) => {
                const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
                if (step) {
                  event.preventDefault();
                  const next = TABS[(TABS.indexOf(name) + step + TABS.length) % TABS.length];
                  if (next) { onTab(next); window.document.getElementById(`tab-${next}`)?.focus(); }
                }
              }}>
              {t(`media.tab.${name}` as MessageKey)} <span className="tab__count">{counts[name]}</span>
            </button>
          ))}
        </div>
      </header>
      <div className="media__body" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "photos" ? <PhotosTab projectId={projectId} photos={catalog.photos} selection={selection} actions={actions} onSelect={onSelect} /> : null}
        {tab === "videos" ? <VideosTab projectId={projectId} videos={catalog.videos} hidden={hiddenMoments(catalog).length} document={doc} selection={selection} actions={actions} onSelect={onSelect} /> : null}
        {tab === "transitions" ? <TransitionsTab library={catalog.transitions} selection={selection} onSelect={onSelect} /> : null}
        {tab === "music" ? <MusicTab catalog={catalog} document={doc} waveforms={waveforms} selection={selection} onSelect={onSelect} /> : null}
      </div>
    </section>
  );
}

// --- photos ---

interface PhotosProps { projectId: string; photos: CatalogItem[]; selection: Selection; actions: EditActions; onSelect: (s: Selection) => void }

function PhotosTab({ projectId, photos, selection, actions, onSelect }: PhotosProps) {
  const { t, format } = useI18n();
  const groups = groupPhotos(photos);
  const [threshold, setThreshold] = useState(0);
  const [collapsed, setCollapsed] = useState<ReadonlySet<PhotoGroupId>>(() => new Set<PhotoGroupId>(["discarded", "hidden"]));
  const toggle = (id: PhotoGroupId) => setCollapsed((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  if (photos.length === 0) return <p className="empty">{t("media.empty")}</p>;
  return (
    <>
      <div className="toolbar">
        <p className="toolbar__text">{t("photos.intro")}</p>
        <button type="button" className="btn" onClick={() => setCollapsed(new Set(groups.map((group) => group.id)))}>{t("photos.collapseAll")}</button>
        <button type="button" className="btn" onClick={() => setCollapsed(new Set())}>{t("photos.expandAll")}</button>
        <label className="threshold">
          <span>{t("photos.threshold")}</span>
          <input type="range" min={0} max={1} step={0.01} value={threshold} onChange={(event) => setThreshold(Number(event.target.value))}
            aria-valuetext={t("photos.thresholdValue", { value: format.number(threshold, 2) })} />
          <output>{t("photos.thresholdValue", { value: format.number(threshold, 2) })}</output>
        </label>
      </div>
      {groups.filter((group) => group.items.length > 0 || group.id === "high" || group.id === "medium").map((group) => {
        const open = !collapsed.has(group.id);
        const name = t(`photos.group.${group.id}` as MessageKey);
        const addable = group.id === "high" || group.id === "medium" || group.id === "low" ? group.items.filter(canAdd) : [];
        return (
          <section key={group.id} className="group" aria-label={name}>
            <div className="group__bar">
              <h3 className="group__head">
                <button type="button" className="group__toggle" aria-expanded={open} aria-label={t("photos.groupToggle", { group: name })} onClick={() => toggle(group.id)}>
                  {open ? <ChevronDownIcon /> : <ChevronRightIcon />}
                  <span className={`dot dot--${group.id}`} aria-hidden="true" />
                  <span className="group__name">{name}</span>
                  <span className="group__meta">{t(`photos.group.range.${group.id}` as MessageKey)} · {t("photos.group.count.other", { n: group.items.length })}</span>
                </button>
              </h3>
              {addable.length > 0 && !actions.readOnly ? (
                <button type="button" className="btn" aria-label={t("photos.addAllLabel", { group: name })} onClick={() => actions.addCandidates(addable)}>
                  <PlusIcon />{t("photos.addAll")}
                </button>
              ) : null}
            </div>
            {open ? (
              group.items.length === 0 ? <p className="empty">{t("photos.empty")}</p> : (
                <ul className="thumbs">
                  {group.items.map((item) => (
                    <PhotoThumb key={item.id} projectId={projectId} item={item} dimmed={isBelowThreshold(item, threshold)} actions={actions}
                      selected={sameSelection(selection, { kind: "element", id: item.id }) || sameSelection(selection, { kind: "candidate", id: item.id })}
                      onSelect={onSelect} />
                  ))}
                </ul>
              )
            ) : null}
          </section>
        );
      })}
    </>
  );
}

function PhotoThumb({ projectId, item, dimmed, selected, actions, onSelect }: { projectId: string; item: CatalogItem; dimmed: boolean; selected: boolean; actions: EditActions; onSelect: (s: Selection) => void }) {
  const { t, format } = useI18n();
  const inTimeline = item.status === "selected";
  const hidden = item.status === "excluded";
  const name = fileName(item.source);
  const label = [
    t("photos.photo", { name, score: format.score(item.score.final) }),
    inTimeline ? t("photos.inTimeline") : null,
    item.group && item.group.size > 1 ? t("photos.similar", { n: item.group.size }) : null,
    dimmed ? t("photos.belowThreshold") : null,
    item.discard ? t(discardKey(item.discard.reason)) : null,
  ].filter(Boolean).join(", ");
  return (
    <li className="thumbcell" {...dragProps(canAdd(item) ? { kind: "photo", id: item.id } : null, actions.readOnly)}>
      <button type="button" className={`thumb${selected ? " is-selected" : ""}${dimmed ? " is-dimmed" : ""}${inTimeline ? " is-used" : ""}`} aria-label={label} aria-pressed={selected}
        onClick={() => onSelect(inTimeline ? { kind: "element", id: item.id } : { kind: "candidate", id: item.id })}>
        <img src={urls.thumb(projectId, item.media)} alt="" loading="lazy" draggable={false} />
        <span className="thumb__score">{format.score(item.score.final)}</span>
        {item.group && item.group.size > 1 ? <span className="thumb__similar"><CopyIcon />× {item.group.size}</span> : null}
        {inTimeline ? <span className="thumb__used" aria-hidden="true"><CheckIcon /></span> : null}
      </button>
      {actions.readOnly ? null : hidden ? (
        <button type="button" className="thumb__action" aria-label={t("photos.restore", { name })} title={t("edit.restore")} onClick={() => actions.restore(item)}><EyeIcon /></button>
      ) : (
        <button type="button" className="thumb__action" aria-label={t("photos.hide", { name })} title={t("edit.hide")} onClick={() => actions.hideCandidate(item)}><EyeOffIcon /></button>
      )}
    </li>
  );
}

// --- videos ---

interface VideosProps {
  projectId: string;
  videos: CatalogVideo[];
  hidden: number;
  document: PlanDocument | null;
  selection: Selection;
  actions: EditActions;
  onSelect: (s: Selection) => void;
}

function VideosTab({ projectId, videos, hidden, document: doc, selection, actions, onSelect }: VideosProps) {
  const { t, plural } = useI18n();
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(videos.slice(0, 1).map((video) => video.media)));
  if (videos.length === 0) return <p className="empty">{t("videos.empty")}</p>;
  return (
    <>
      <div className="toolbar">
        <p className="toolbar__text">{t("videos.intro")}</p>
        {hidden > 0 && !actions.readOnly ? <button type="button" className="btn" onClick={actions.restoreMoments}>{t("videos.restoreDeleted")}</button> : null}
      </div>
      <p className="toolbar__text toolbar__text--block">{t("videos.barHint")}</p>
      <ul className="videos">
        {videos.map((video) => {
          const expanded = open.has(video.media);
          const name = fileName(video.source);
          const addable = video.moments.filter((moment) => moment.status === "available");
          return (
            <li key={video.media} className="video">
              <div className="video__row" {...dragProps(addable.length > 0 ? { kind: "video", media: video.media } : null, actions.readOnly)}>
                <button type="button" className="video__toggle" aria-expanded={expanded} aria-label={t("videos.expand", { name })}
                  onClick={() => setOpen((current) => { const next = new Set(current); if (next.has(video.media)) next.delete(video.media); else next.add(video.media); return next; })}>
                  {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
                </button>
                <button type="button" className={`video__name${sameSelection(selection, { kind: "video", media: video.media }) ? " is-selected" : ""}`}
                  onClick={() => onSelect({ kind: "video", media: video.media })}>
                  <img className="video__thumb" src={urls.thumb(projectId, video.media)} alt="" loading="lazy" draggable={false} />
                  <span className="video__title">{name}</span>
                  <span className="video__meta">
                    {plural("videos.moments", video.moments.length)}{video.duration ? ` · ${t("videos.length", { time: clock(video.duration) })}` : ""}
                    {video.readable ? "" : ` · ${t("videos.unreadable")}`}
                  </span>
                </button>
                {addable.length > 0 && !actions.readOnly ? (
                  <button type="button" className="btn" aria-label={t("videos.addAllLabel", { name })} onClick={() => actions.addCandidates(addable)}>
                    <PlusIcon />{t("videos.addAll")}
                  </button>
                ) : null}
              </div>
              {expanded && (video.duration ?? 0) > 0 ? <MomentBar video={video} document={doc} selection={selection} actions={actions} onSelect={onSelect} /> : null}
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** The bar of a video: its moments, the click on a free point that adds one, and the two handles of the moments in the timeline. */
function MomentBar({ video, document: doc, selection, actions, onSelect }: { video: CatalogVideo; document: PlanDocument | null; selection: Selection; actions: EditActions; onSelect: (s: Selection) => void }) {
  const { t } = useI18n();
  const bar = useRef<HTMLDivElement>(null);
  const length = video.duration ?? 0;
  const name = fileName(video.source);
  const used = video.moments.filter((moment) => moment.status === "selected");

  const onBarClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || actions.readOnly || !bar.current || length <= 0) return;
    const rect = bar.current.getBoundingClientRect();
    if (rect.width <= 0) return;
    actions.addMoment(video.source, length, ((event.clientX - rect.left) / rect.width) * length);
  };

  return (
    <div className="momentbar" role="group" aria-label={t("videos.bar", { name })} ref={bar} onClick={onBarClick}>
      {video.moments.map((moment) => {
        const start = moment.start ?? 0;
        const end = moment.end ?? start;
        const picked = sameSelection(selection, { kind: "element", id: moment.id }) || sameSelection(selection, { kind: "candidate", id: moment.id });
        return (
          <div key={moment.id} className={`moment moment--${moment.status}${picked ? " is-selected" : ""}`}
            style={{ left: `${(start / length) * 100}%`, width: `${Math.max(((end - start) / length) * 100, 1.5)}%` }}
            {...dragProps(moment.status === "available" || moment.status === "discarded" ? { kind: "moment", id: moment.id } : null, actions.readOnly)}>
            <button type="button" className="moment__hit" aria-pressed={picked}
              aria-label={`${t("videos.moment", { start: clock(start), end: clock(end) })}, ${t(`videos.status.${moment.status}` as MessageKey)}`}
              onClick={() => onSelect(moment.status === "selected" ? { kind: "element", id: moment.id } : { kind: "candidate", id: moment.id })} />
          </div>
        );
      })}
      {actions.readOnly || !doc ? null : used.map((moment) => {
        const element = doc.plan.elements.find((entry) => entry.id === moment.id);
        const range = element ? trimRange(doc.limits.elements[moment.id], element) : null;
        if (!element || !range || element.start === undefined || element.end === undefined) return null;
        const scale = () => (bar.current?.clientWidth ?? 0) / length;
        const bounded = (edge: { min: number; max: number }) => ({ min: edge.min, max: Number.isFinite(edge.max) ? edge.max : length });
        const startRange = bounded(range.start);
        const endRange = bounded(range.end);
        return (
          <span key={moment.id}>
            <Handle className="handle handle--bar" label={`${t("videos.handleStart")}: ${name}`} value={element.start} min={startRange.min} max={startRange.max}
              valueText={clock(element.start)} scale={scale} style={{ left: `${(element.start / length) * 100}%` }}
              onChange={(value) => actions.setEdge(moment.id, "start", value)} onCommit={actions.seal} />
            <Handle className="handle handle--bar" label={`${t("videos.handleEnd")}: ${name}`} value={element.end} min={endRange.min} max={endRange.max}
              valueText={clock(element.end)} scale={scale} style={{ left: `${(element.end / length) * 100}%` }}
              onChange={(value) => actions.setEdge(moment.id, "end", value)} onCommit={actions.seal} />
          </span>
        );
      })}
    </div>
  );
}

// --- transitions and music ---

function TransitionsTab({ library, selection, onSelect }: { library: LibraryTransition[]; selection: Selection; onSelect: (s: Selection) => void }) {
  const { t, format } = useI18n();
  return (
    <>
      <p className="toolbar__text">{t("transitions.intro")}</p>
      <ul className="library">
        {library.map((item) => (
          <li key={item.type}>
            <button type="button" className={`library__item${sameSelection(selection, { kind: "library", type: item.type }) ? " is-selected" : ""}`}
              aria-pressed={sameSelection(selection, { kind: "library", type: item.type })} onClick={() => onSelect({ kind: "library", type: item.type })}>
              <span className={`library__swatch library__swatch--${item.type}`} aria-hidden="true" />
              <span className="library__name">{t(`transition.${item.type}` as MessageKey)}</span>
              <span className="library__meta">{t("transitions.defaultDuration", { time: format.seconds(item.default_seconds) })} · {t(`transitions.where.${item.position}` as MessageKey)}</span>
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}

function MusicTab({ catalog, document: doc, waveforms, selection, onSelect }: { catalog: Catalog; document: PlanDocument | null; waveforms: ReadonlyMap<string, Waveform | null>; selection: Selection; onSelect: (s: Selection) => void }) {
  const { t } = useI18n();
  const tracks = catalog.music.tracks;
  if (tracks.length === 0) return <p className="empty">{t("music.none")}</p>;
  return (
    <>
      <p className="toolbar__text">{t("music.intro")}</p>
      <ul className="library">
        {tracks.map((track) => {
          const used = doc?.plan.music?.tracks.filter((entry) => entry.file === track.file).reduce((sum, entry) => sum + entry.parts.length, 0) ?? 0;
          const waveform = waveforms.get(track.file);
          const picked = sameSelection(selection, { kind: "track", file: track.file });
          return (
            <li key={track.id}>
              <button type="button" className={`library__item${picked ? " is-selected" : ""}`} aria-pressed={picked} aria-label={`${t("music.track", { name: fileName(track.file) })}, ${used > 0 ? t("music.inVideo", { n: used }) : t("music.notInVideo")}`}
                onClick={() => onSelect({ kind: "track", file: track.file })}>
                <MusicIcon />
                <span className="library__name">{fileName(track.file)}</span>
                <span className="library__meta">
                  {waveform ? `${t("music.length", { time: clock(waveform.duration) })} · ` : ""}{used > 0 ? t("music.inVideo", { n: used }) : t("music.notInVideo")}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </>
  );
}
