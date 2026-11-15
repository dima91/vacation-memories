// The final timeline (section 12): two lanes on one time axis, the ruler, zoom and scrolling, with the clips,
// their transitions and the music laid out from the plan. Clips are reordered by dragging, items of the Media panel are
// dropped on the lane, and the duration of a selected photo is changed with its two handles (U5). The transitions are
// dragged onto the clips, moved, resized with their two handles or with the arrows; the tracks of the music lane are
// reordered by their name, cut at the playhead, and shifted by dragging the body of a part or with the arrows (U6).
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type PointerEvent } from "react";
import { urls } from "../api/client";
import type { Effect, EffectType, PlanDocument, PlanElement, Waveform } from "../api/types";
import { useI18n, type MessageKey } from "../i18n";
import { DRAG_TYPE, beginDrag, currentDrag, decodePayload, encodePayload, endDrag, isMusicDrag, isTransitionDrag, type DragPayload } from "../lib/dnd";
import { insertionSlot, isNoopSlot, slotX, valueFromDrag, valueFromKey } from "../lib/drag";
import { boundsOf } from "../lib/effectEdit";
import { clock } from "../lib/format";
import { durationRange } from "../lib/limits";
import { offsetBounds, trackExtents } from "../lib/musicEdit";
import type { Selection } from "../lib/selection";
import {
  MAX_ZOOM, MIN_ZOOM, ZOOM_STEP, clipRects, clampZoom, contentWidth, musicSlots, peaksBetween, pixelsPerSecond,
  scrollAfterZoom, scrollByPage, scrollToReveal, stepPlayhead, tickTimes, timeAtPointer, timeToX, windowSeconds, type MusicSlot, type View,
} from "../lib/timeline";
import type { EditActions } from "./editing";
import { Handle } from "./Handle";
import { ChevronLeftIcon, ChevronRightIcon } from "./icons";

interface Props {
  projectId: string;
  document: PlanDocument;
  waveforms: ReadonlyMap<string, Waveform | null>;
  selection: Selection;
  playhead: number;
  actions: EditActions;
  onSelect: (selection: Selection) => void;
  onSeek: (time: number) => void;
}

const DEFAULT_WIDTH = 1000;

export function WaveformBars({ peaks }: { peaks: readonly number[] }) {
  if (peaks.length === 0) return null;
  const path = peaks.map((peak, index) => {
    const half = Math.max(0.04, peak) / 2;
    return `M${index + 0.5} ${0.5 - half}V${0.5 + half}`;
  }).join("");
  return (
    <svg className="wave" viewBox={`0 0 ${peaks.length} 1`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <path d={path} />
    </svg>
  );
}

export function Timeline({ projectId, document: doc, waveforms, selection, playhead, actions, onSelect, onSeek }: Props) {
  const { t, format } = useI18n();
  const { plan } = doc;
  const total = doc.duration;
  const slots = useMemo(() => musicSlots(plan.music), [plan.music]);
  const musicEnd = slots.reduce((end, slot) => Math.max(end, slot.end), 0);
  const [showAllMusic, setShowAllMusic] = useState(false);
  const [zoom, setZoom] = useState(MIN_ZOOM);
  const [width, setWidth] = useState(DEFAULT_WIDTH);
  const [scroll, setScroll] = useState(0);
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<number | null>(null);
  const [mark, setMark] = useState<number | null>(null);
  const [musicMark, setMusicMark] = useState<number | null>(null);

  const view: View = { window: windowSeconds(showAllMusic ? Math.max(total, musicEnd) : total), zoom, width };
  const pps = pixelsPerSecond(view);
  const rects = clipRects(plan.elements, view);

  useLayoutEffect(() => {
    const element = viewportRef.current;
    if (!element) return undefined;
    const measure = () => setWidth(element.clientWidth || DEFAULT_WIDTH);
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    if (pendingScroll.current !== null && viewportRef.current) {
      viewportRef.current.scrollLeft = pendingScroll.current;
      pendingScroll.current = null;
    }
  });

  // The playhead stays in view while it moves (playback).
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const next = scrollToReveal(element.scrollLeft, view, playhead);
    if (next !== element.scrollLeft) element.scrollLeft = next;
  }, [playhead]);

  const changeZoom = (next: number) => {
    const clamped = clampZoom(next);
    const anchor = timeToX(playhead, view) - scroll >= 0 && timeToX(playhead, view) - scroll <= width ? playhead : (scroll + width / 2) / pps;
    pendingScroll.current = scrollAfterZoom(scroll, view, clamped, anchor);
    setZoom(clamped);
  };

  const scrollPage = (direction: -1 | 1) => {
    const element = viewportRef.current;
    if (element) element.scrollLeft = scrollByPage(element.scrollLeft, view, direction);
  };

  const seekFromPointer = (event: PointerEvent<HTMLDivElement>) => {
    const rect = contentRef.current?.getBoundingClientRect();
    if (rect) onSeek(timeAtPointer(event.clientX - rect.left, 0, view, total));
  };
  const onRulerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture?.(event.pointerId);
    seekFromPointer(event);
  };
  const onRulerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (event.buttons === 1) seekFromPointer(event);
  };
  const onRulerKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      onSeek(stepPlayhead(playhead, event.key === "ArrowLeft" ? -1 : 1, event.shiftKey, total));
    } else if (event.key === "Home") {
      event.preventDefault();
      onSeek(0);
    } else if (event.key === "End") {
      event.preventDefault();
      onSeek(total);
    }
  };

  // Dragging over the lane: a clip of the timeline or an item of the Media panel, dropped at the insertion mark.
  const slotAt = (event: DragEvent<HTMLElement>): number => {
    const rect = contentRef.current?.getBoundingClientRect();
    return insertionSlot(rects, event.clientX - (rect?.left ?? 0));
  };
  const onLaneOver = (event: DragEvent<HTMLDivElement>) => {
    const drag = currentDrag();
    if (!drag || actions.readOnly || isTransitionDrag(drag) || isMusicDrag(drag)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = drag.kind === "clip" ? "move" : "copy";
    const slot = slotAt(event);
    const from = drag.kind === "clip" ? plan.elements.findIndex((element) => element.id === drag.id) : -1;
    setMark(from >= 0 && isNoopSlot(from, slot) ? null : slot);
  };
  const onLaneDrop = (event: DragEvent<HTMLDivElement>) => {
    const payload = currentDrag() ?? decodePayload(event.dataTransfer.getData(DRAG_TYPE));
    setMark(null);
    if (!payload || actions.readOnly || isTransitionDrag(payload) || isMusicDrag(payload)) return;
    event.preventDefault();
    const slot = slotAt(event);
    endDrag();
    actions.drop(payload, slot);
  };
  const onLaneLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setMark(null);
  };

  // Dragging over the music lane: a track of the Media panel or one of the lane, dropped between the tracks.
  const trackRects = trackExtents(plan.music).map((extent) => ({ x: timeToX(extent.start, view), width: (extent.end - extent.start) * pps }));
  const musicSlotAt = (event: DragEvent<HTMLElement>): number => {
    const rect = contentRef.current?.getBoundingClientRect();
    return insertionSlot(trackRects, event.clientX - (rect?.left ?? 0));
  };
  const onMusicOver = (event: DragEvent<HTMLDivElement>) => {
    const drag = currentDrag();
    if (!isMusicDrag(drag) || actions.readOnly) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = drag.kind === "musicTrack" ? "move" : "copy";
    const slot = musicSlotAt(event);
    setMusicMark(drag.kind === "musicTrack" && isNoopSlot(drag.index, slot) ? null : slot);
  };
  const onMusicDrop = (event: DragEvent<HTMLDivElement>) => {
    const payload = currentDrag() ?? decodePayload(event.dataTransfer.getData(DRAG_TYPE));
    setMusicMark(null);
    if (!isMusicDrag(payload) || actions.readOnly) return;
    event.preventDefault();
    const slot = musicSlotAt(event);
    endDrag();
    actions.dropMusic(payload, slot);
  };
  const onMusicLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setMusicMark(null);
  };
  const cuttable = !actions.readOnly && slots.some((slot) => playhead >= slot.start && playhead < slot.end);
  const bounds = offsetBounds(plan.music ?? { tracks: [], offset: 0 }, total);
  const selectedElement = selection.kind === "element" ? selection.id : selection.kind === "effect" ? selection.element : null;
  const requested = plan.requested_duration;

  return (
    <section className="timeline" aria-label={t("timeline.title")}>
      <header className="timeline__head">
        <h2>{t("timeline.title")}</h2>
        <p className="timeline__counter" aria-label={requested ? t("top.durationLabel", { total: clock(total), requested: clock(requested) }) : undefined}>
          {requested ? t("timeline.counter", { total: clock(total), requested: clock(requested) }) : t("timeline.counterNoTarget", { total: clock(total) })}
        </p>
        <p className="timeline__legend">{t("timeline.help")}</p>
        <div className="timeline__tools">
          {slots.length > 0 ? (
            <button type="button" className="btn" disabled={!cuttable} title={t("timeline.cutMusicHelp")} onClick={() => actions.cutMusic(playhead)}>
              {t("timeline.cutMusic")}
            </button>
          ) : null}
          {slots.length > 0 ? (
            <button type="button" className="btn" aria-pressed={showAllMusic} onClick={() => setShowAllMusic((value) => !value)}>
              {showAllMusic ? t("timeline.hideExtraMusic") : t("timeline.showAllMusic")}
            </button>
          ) : null}
          <button type="button" className="btn btn--icon" aria-label={t("timeline.scrollLeft")} onClick={() => scrollPage(-1)} disabled={zoom === MIN_ZOOM}><ChevronLeftIcon /></button>
          <button type="button" className="btn btn--icon" aria-label={t("timeline.scrollRight")} onClick={() => scrollPage(1)} disabled={zoom === MIN_ZOOM}><ChevronRightIcon /></button>
          <label className="zoom">
            <span>{t("timeline.zoom")}</span>
            <input type="range" min={MIN_ZOOM} max={MAX_ZOOM} step={ZOOM_STEP} value={zoom} aria-label={t("timeline.zoomLabel")}
              aria-valuetext={`${format.number(zoom, 1)}×`} onChange={(event) => changeZoom(Number(event.target.value))} />
          </label>
        </div>
      </header>

      <div className="timeline__body">
        <div className="timeline__labels" aria-hidden="true">
          <div className="timeline__label timeline__label--ruler" />
          <div className="timeline__label timeline__label--clips">{t("timeline.lane.clips")}</div>
          <div className="timeline__label timeline__label--music">{t("timeline.lane.music")}</div>
        </div>

        <div className="timeline__viewport" ref={viewportRef} onScroll={(event) => setScroll(event.currentTarget.scrollLeft)}>
          <div className="timeline__content" ref={contentRef} style={{ width: contentWidth(view) }}>
            <div className="ruler" role="slider" tabIndex={0} aria-label={t("timeline.ruler")} aria-valuemin={0} aria-valuemax={Math.round(total * 10) / 10}
              aria-valuenow={Math.round(playhead * 10) / 10} aria-valuetext={t("timeline.rulerValue", { time: clock(playhead) })}
              onPointerDown={onRulerDown} onPointerMove={onRulerMove} onKeyDown={onRulerKey}>
              {tickTimes(view.window).map((time) => (
                <span key={time} className="ruler__tick" style={{ left: timeToX(time, view) }}>{clock(time)}</span>
              ))}
            </div>

            <div className="lane lane--clips" role="list" aria-label={t("timeline.clips")} onDragOver={onLaneOver} onDrop={onLaneDrop} onDragLeave={onLaneLeave}>
              {plan.elements.length === 0 ? <p className="lane__empty">{t("timeline.empty")}</p> : null}
              {plan.elements.map((element, index) => {
                const rect = rects[index];
                if (!rect) return null;
                return <ClipView key={element.id} projectId={projectId} document={doc} element={element} position={index + 1}
                  left={rect.x} width={rect.width} pps={pps} selected={selectedElement === element.id}
                  chosen={selection.kind === "element" && selection.id === element.id} actions={actions}
                  selectedEffect={selection.kind === "effect" && selection.element === element.id ? selection.id : null}
                  onSelect={onSelect} onDragEnd={() => setMark(null)} />;
              })}
              {mark !== null ? <div className="insertion" style={{ left: slotX(rects, mark) }} aria-hidden="true" /> : null}
            </div>

            <div className="lane lane--music" role="list" aria-label={t("timeline.musicLane")} onDragOver={onMusicOver} onDrop={onMusicDrop} onDragLeave={onMusicLeave}>
              {slots.length === 0 ? <p className="lane__empty">{t("timeline.noMusic")}</p> : null}
              {slots.map((slot) => {
                const visibleStart = Math.max(slot.start, 0);
                const visibleEnd = Math.min(slot.end, view.window);
                if (visibleEnd <= visibleStart) return null;
                return <PartView key={`${slot.track}-${slot.part}`} slot={slot} waveform={waveforms.get(slot.file)} view={view} pps={pps} bounds={bounds}
                  offset={plan.music?.offset ?? 0} selected={selection.kind === "part" && selection.track === slot.track && selection.part === slot.part}
                  actions={actions} onSelect={onSelect} />;
              })}
              {(plan.music?.tracks ?? []).map((track, index) => {
                const extent = trackExtents(plan.music)[index];
                if (!extent || extent.end <= 0 || extent.start >= view.window) return null;
                return (
                  <div key={`${index}-${track.file}`} className="track__tab" draggable={!actions.readOnly} title={t("timeline.trackDrag", { file: track.file })}
                    style={{ left: Math.max(timeToX(extent.start, view), 0) + 2 }} aria-hidden="true"
                    onClick={() => onSelect({ kind: "part", track: index, part: 0 })}
                    onDragStart={(event) => {
                      event.dataTransfer.effectAllowed = "move";
                      event.dataTransfer.setData(DRAG_TYPE, encodePayload({ kind: "musicTrack", index }));
                      beginDrag({ kind: "musicTrack", index });
                    }}
                    onDragEnd={() => { endDrag(); setMusicMark(null); }}>
                    {track.file.split("/").pop()}
                  </div>
                );
              })}
              {musicMark !== null ? <div className="insertion" style={{ left: slotX(trackRects, musicMark) }} aria-hidden="true" /> : null}
              {slots.length > 0 && musicEnd > total ? <div className="lane__beyond" style={{ left: timeToX(total, view) }} aria-hidden="true" /> : null}
            </div>

            <div className="end-line" style={{ left: timeToX(total, view) }} aria-hidden="true"><span>{t("timeline.endOfVideo")}</span></div>
            <div className="playhead" style={{ left: timeToX(playhead, view) }} aria-hidden="true" />
          </div>
        </div>
      </div>
    </section>
  );
}

interface ClipProps {
  projectId: string;
  document: PlanDocument;
  element: PlanElement;
  position: number;
  left: number;
  width: number;
  pps: number;
  selected: boolean;
  /** The clip itself is the selection (not one of its transitions): its duration handles show. */
  chosen: boolean;
  selectedEffect: string | null;
  actions: EditActions;
  onSelect: (selection: Selection) => void;
  onDragEnd: () => void;
}

function ClipView({ projectId, document: doc, element, position, left, width, pps, selected, chosen, selectedEffect, actions, onSelect, onDragEnd }: ClipProps) {
  const { t, format } = useI18n();
  const [over, setOver] = useState(false);
  const missing = doc.warnings.some((warning) => warning.code === "media_missing" && warning.element === element.id);
  const media = doc.refs[element.id]?.media;
  const isVideo = element.type === "video";
  const kind = isVideo ? t("clip.video") : t("clip.photo");
  const label = `${t("clip.label", { position, kind, duration: format.seconds(element.duration) })}${missing ? `, ${t("clip.missing")}` : ""}${element.origin === "user" ? `, ${t("clip.userAdded")}` : ""}`;
  const range = !isVideo && chosen && !actions.readOnly ? durationRange(doc.limits.elements[element.id]) : null;
  const grab = (event: DragEvent<HTMLDivElement>) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(DRAG_TYPE, encodePayload({ kind: "clip", id: element.id }));
    beginDrag({ kind: "clip", id: element.id });
  };

  // A transition of the library or of another clip dropped on this clip: where, is up to the position inside it (section 12).
  const overClip = (event: DragEvent<HTMLDivElement>) => {
    if (!isTransitionDrag(currentDrag()) || actions.readOnly) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = currentDrag()?.kind === "effect" ? "move" : "copy";
    setOver(true);
  };
  const dropOnClip = (event: DragEvent<HTMLDivElement>) => {
    const payload: DragPayload | null = currentDrag() ?? decodePayload(event.dataTransfer.getData(DRAG_TYPE));
    setOver(false);
    if (!isTransitionDrag(payload) || actions.readOnly) return;
    event.preventDefault();
    event.stopPropagation();
    const at = pps > 0 ? (event.clientX - event.currentTarget.getBoundingClientRect().left) / pps : 0;
    endDrag();
    actions.dropTransition(payload, element.id, at);
  };
  const minimum = doc.limits.min_effect_seconds;
  const handles = selectedEffect ? element.effects.find((effect) => effect.id === selectedEffect) : undefined;
  const room = handles && !actions.readOnly ? boundsOf(doc.plan, handles.id, minimum) : null;
  return (
    <div className={`clip clip--${element.type}${selected ? " is-selected" : ""}${missing ? " is-missing" : ""}${over ? " is-target" : ""}`} style={{ left, width }} role="listitem"
      onDragOver={overClip} onDragLeave={() => setOver(false)} onDrop={dropOnClip}>
      <div className="clip__grab" draggable={!actions.readOnly} onDragStart={grab} onDragEnd={() => { endDrag(); onDragEnd(); }}>
        <button type="button" className="clip__body" aria-pressed={selected} aria-label={label} onClick={() => onSelect({ kind: "element", id: element.id })}>
          {media && !missing ? <img className="clip__thumb" alt="" loading="lazy" draggable={false}
            src={urls.thumb(projectId, media, isVideo ? element.start : undefined)} /> : null}
          <span className="clip__band" aria-hidden="true" />
          <span className="clip__label" aria-hidden="true">{isVideo ? t("clip.videoLetter") : t("clip.photoLetter")} {format.number(element.duration, 1)}</span>
        </button>
      </div>
      {range ? (
        <>
          <Handle className="handle handle--start" label={t("clip.handleStart")} value={element.duration} min={range.min} max={range.max}
            valueText={format.seconds(element.duration)} sign={-1} scale={() => pps}
            onChange={(value) => actions.setDuration(element.id, value)} onCommit={actions.seal} />
          <Handle className="handle handle--end" label={t("clip.handleEnd")} value={element.duration} min={range.min} max={range.max}
            valueText={format.seconds(element.duration)} scale={() => pps}
            onChange={(value) => actions.setDuration(element.id, value)} onCommit={actions.seal} />
        </>
      ) : null}
      {element.effects.map((effect) => (
        <EffectView key={effect.id} effect={effect} pps={pps} selected={selectedEffect === effect.id} actions={actions} onSelect={() => onSelect({ kind: "effect", element: element.id, id: effect.id })} />
      ))}
      {handles && room ? <EffectHandles effect={handles} room={room} minimum={minimum} pps={pps} actions={actions} /> : null}
    </div>
  );
}

interface EffectProps {
  effect: Effect;
  pps: number;
  selected: boolean;
  actions: EditActions;
  onSelect: () => void;
}

/** A transition on its clip: it is selected with a click, dragged onto a clip, and moved or resized with the arrows. */
function EffectView({ effect, pps, selected, actions, onSelect }: EffectProps) {
  const { t, format } = useI18n();
  const name = t(`transition.${effect.type}` as MessageKey);
  const arrows = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (actions.readOnly || event.ctrlKey || event.metaKey || event.altKey) return;
    const direction = event.key === "ArrowRight" || event.key === "ArrowUp" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowDown" ? -1 : 0;
    if (direction === 0) return;
    event.preventDefault();
    event.stopPropagation();
    const change = direction * (event.shiftKey ? 1 : 0.1);
    if (effect.type === "dip") actions.setEffectStart(effect.id, effect.offset + change);
    else actions.setEffectDuration(effect.id, effect.duration + change);
  };
  return (
    <div className="clip__fxwrap" style={{ left: effect.offset * pps, width: Math.max(effect.duration * pps, 6) }} draggable={!actions.readOnly}
      onDragStart={(event) => {
        event.stopPropagation();
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData(DRAG_TYPE, encodePayload({ kind: "effect", id: effect.id }));
        beginDrag({ kind: "effect", id: effect.id });
      }}
      onDragEnd={() => endDrag()}
      onDragOver={(event) => {
        const drag = currentDrag();
        if (drag?.kind === "transition" && drag.type === "cut" && !actions.readOnly) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; }
      }}
      onDrop={(event) => {
        const drag = currentDrag();
        if (drag?.kind !== "transition" || drag.type !== "cut" || actions.readOnly) return;
        event.preventDefault();
        event.stopPropagation();
        endDrag();
        actions.removeEffect(effect.id);
      }}>
      <button type="button" className={`clip__fx clip__fx--${effect.type}${selected ? " is-selected" : ""}`} aria-pressed={selected}
        aria-label={t("clip.effect", { effect: name, duration: format.seconds(effect.duration) })} title={`${name} · ${format.seconds(effect.duration)}`}
        onClick={onSelect} onKeyDown={arrows} onKeyUp={(event) => { if (event.key.startsWith("Arrow")) actions.seal(); }} onBlur={actions.seal}>
        <span aria-hidden="true">{t(`transition.short.${effect.type as EffectType}` as MessageKey)}</span>
      </button>
    </div>
  );
}

/** The two handles of the selected transition: each is a time of the clip. The edge a type keeps on the edge of its clip stays put. */
function EffectHandles({ effect, room, minimum, pps, actions }: { effect: Effect; room: { start: number; end: number }; minimum: number; pps: number; actions: EditActions }) {
  const { t, format } = useI18n();
  const end = effect.offset + effect.duration;
  const startFixed = effect.type === "fade_in" || effect.type === "dissolve";
  const endFixed = effect.type === "fade_out";
  const at = (value: number) => format.seconds(value);
  return (
    <>
      <Handle className="handle handle--fx handle--fx-start" style={{ left: effect.offset * pps }} label={t("effect.handleStart")} value={effect.offset}
        min={startFixed ? effect.offset : room.start} max={startFixed ? effect.offset : end - minimum} valueText={at(effect.offset)} disabled={startFixed}
        scale={() => pps} onChange={(value) => actions.setEffectEdge(effect.id, "start", value)} onCommit={actions.seal} />
      <Handle className="handle handle--fx handle--fx-end" style={{ left: end * pps }} label={t("effect.handleEnd")} value={end}
        min={endFixed ? end : effect.offset + minimum} max={endFixed ? end : room.end} valueText={at(end)} disabled={endFixed}
        scale={() => pps} onChange={(value) => actions.setEffectEdge(effect.id, "end", value)} onCommit={actions.seal} />
    </>
  );
}

interface PartProps {
  slot: MusicSlot;
  waveform: Waveform | null | undefined;
  view: View;
  pps: number;
  bounds: { min: number; max: number };
  offset: number;
  selected: boolean;
  actions: EditActions;
  onSelect: (selection: Selection) => void;
}

/** A part of a track on the music lane: selected with a click, shifted (with the rest of the music) by dragging its body or with the arrows. */
function PartView({ slot, waveform, view, pps, bounds, offset, selected, actions, onSelect }: PartProps) {
  const { t } = useI18n();
  const drag = useRef<{ x: number; offset: number; moved: boolean } | null>(null);
  const visibleStart = Math.max(slot.start, 0);
  const visibleEnd = Math.min(slot.end, view.window);
  const from = slot.trackStart + (visibleStart - slot.start);
  const to = slot.trackStart + (visibleEnd - slot.start);
  const down = (event: PointerEvent<HTMLButtonElement>) => {
    if (actions.readOnly || event.button !== 0) return;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    drag.current = { x: event.clientX, offset, moved: false };
  };
  const move = (event: PointerEvent<HTMLButtonElement>) => {
    const start = drag.current;
    if (!start) return;
    const delta = event.clientX - start.x;
    if (!start.moved && Math.abs(delta) < 3) return;
    start.moved = true;
    actions.setShift(valueFromDrag({ start: start.offset, deltaPx: delta, pxPerUnit: pps, bounds }));
  };
  const up = () => {
    if (drag.current?.moved) actions.seal();
    drag.current = null;
  };
  const arrows = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (actions.readOnly || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const next = valueFromKey(event.key, event.shiftKey, offset, bounds);
    if (next === null) return;
    event.preventDefault();
    event.stopPropagation();
    actions.setShift(next);
  };
  return (
    <button type="button" role="listitem" className={`part${selected ? " is-selected" : ""}${slot.muted ? " is-muted" : ""}`}
      style={{ left: visibleStart * pps, width: (visibleEnd - visibleStart) * pps }} aria-pressed={selected}
      aria-label={`${t("timeline.musicPart", { part: slot.part + 1, file: slot.file })}${slot.muted ? `, ${t("timeline.muted")}` : ""}`}
      onClick={() => onSelect({ kind: "part", track: slot.track, part: slot.part })}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up}
      onKeyDown={arrows} onKeyUp={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") actions.seal(); }}>
      {waveform ? <WaveformBars peaks={peaksBetween(waveform.peaks, waveform.duration, from, to)} /> : null}
      {slot.muted ? <span className="part__muted">{t("timeline.muted")}</span> : null}
    </button>
  );
}
