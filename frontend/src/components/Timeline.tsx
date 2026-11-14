// The final timeline (section 12): two lanes on one time axis, the ruler, zoom and scrolling, with the clips,
// their transitions and the music laid out from the plan. Read-only in this step: nothing here changes the plan.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { urls } from "../api/client";
import type { EffectType, PlanDocument, PlanElement, Waveform } from "../api/types";
import { useI18n, type MessageKey } from "../i18n";
import { clock } from "../lib/format";
import type { Selection } from "../lib/selection";
import {
  MAX_ZOOM, MIN_ZOOM, ZOOM_STEP, clipRects, clampZoom, contentWidth, musicSlots, peaksBetween, pixelsPerSecond,
  scrollAfterZoom, scrollByPage, scrollToReveal, stepPlayhead, tickTimes, timeAtPointer, timeToX, windowSeconds, type View,
} from "../lib/timeline";
import { ChevronLeftIcon, ChevronRightIcon } from "./icons";

interface Props {
  projectId: string;
  document: PlanDocument;
  waveforms: ReadonlyMap<string, Waveform | null>;
  selection: Selection;
  playhead: number;
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

export function Timeline({ projectId, document: doc, waveforms, selection, playhead, onSelect, onSeek }: Props) {
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

            <div className="lane lane--clips" role="list" aria-label={t("timeline.clips")}>
              {plan.elements.length === 0 ? <p className="lane__empty">{t("timeline.empty")}</p> : null}
              {plan.elements.map((element, index) => {
                const rect = rects[index];
                if (!rect) return null;
                return <ClipView key={element.id} projectId={projectId} document={doc} element={element} position={index + 1}
                  left={rect.x} width={rect.width} pps={pps} selected={selectedElement === element.id}
                  selectedEffect={selection.kind === "effect" && selection.element === element.id ? selection.id : null} onSelect={onSelect} />;
              })}
            </div>

            <div className="lane lane--music" role="list" aria-label={t("timeline.musicLane")}>
              {slots.length === 0 ? <p className="lane__empty">{t("timeline.noMusic")}</p> : null}
              {slots.map((slot) => {
                const waveform = waveforms.get(slot.file);
                const visibleStart = Math.max(slot.start, 0);
                const visibleEnd = Math.min(slot.end, view.window);
                if (visibleEnd <= visibleStart) return null;
                const from = slot.trackStart + (visibleStart - slot.start);
                const to = slot.trackStart + (visibleEnd - slot.start);
                const selected = selection.kind === "part" && selection.track === slot.track && selection.part === slot.part;
                return (
                  <button key={`${slot.track}-${slot.part}`} type="button" role="listitem"
                    className={`part${selected ? " is-selected" : ""}${slot.muted ? " is-muted" : ""}`}
                    style={{ left: timeToX(visibleStart, view), width: (visibleEnd - visibleStart) * pps }}
                    aria-pressed={selected}
                    aria-label={`${t("timeline.musicPart", { part: slot.part + 1, file: slot.file })}${slot.muted ? `, ${t("timeline.muted")}` : ""}`}
                    onClick={() => onSelect({ kind: "part", track: slot.track, part: slot.part })}>
                    {waveform ? <WaveformBars peaks={peaksBetween(waveform.peaks, waveform.duration, from, to)} /> : null}
                    {slot.part === 0 ? <span className="part__name">{slot.file}</span> : null}
                    {slot.muted ? <span className="part__muted">{t("timeline.muted")}</span> : null}
                  </button>
                );
              })}
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
  selectedEffect: string | null;
  onSelect: (selection: Selection) => void;
}

function ClipView({ projectId, document: doc, element, position, left, width, pps, selected, selectedEffect, onSelect }: ClipProps) {
  const { t, format } = useI18n();
  const missing = doc.warnings.some((warning) => warning.code === "media_missing" && warning.element === element.id);
  const media = doc.refs[element.id]?.media;
  const isVideo = element.type === "video";
  const kind = isVideo ? t("clip.video") : t("clip.photo");
  const label = `${t("clip.label", { position, kind, duration: format.seconds(element.duration) })}${missing ? `, ${t("clip.missing")}` : ""}${element.origin === "user" ? `, ${t("clip.userAdded")}` : ""}`;
  return (
    <div className={`clip clip--${element.type}${selected ? " is-selected" : ""}${missing ? " is-missing" : ""}`} style={{ left, width }} role="listitem">
      <button type="button" className="clip__body" aria-pressed={selected} aria-label={label} onClick={() => onSelect({ kind: "element", id: element.id })}>
        {media && !missing ? <img className="clip__thumb" alt="" loading="lazy" draggable={false}
          src={urls.thumb(projectId, media, isVideo ? element.start : undefined)} /> : null}
        <span className="clip__band" aria-hidden="true" />
        <span className="clip__label" aria-hidden="true">{isVideo ? t("clip.videoLetter") : t("clip.photoLetter")} {format.number(element.duration, 1)}</span>
      </button>
      {element.effects.map((effect) => (
        <button key={effect.id} type="button" className={`clip__fx clip__fx--${effect.type}${selectedEffect === effect.id ? " is-selected" : ""}`}
          style={{ left: effect.offset * pps, width: Math.max(effect.duration * pps, 6) }} aria-pressed={selectedEffect === effect.id}
          aria-label={t("clip.effect", { effect: t(`transition.${effect.type}` as MessageKey), duration: format.seconds(effect.duration) })}
          onClick={() => onSelect({ kind: "effect", element: element.id, id: effect.id })}>
          <span aria-hidden="true">{t(`transition.short.${effect.type as EffectType}` as MessageKey)}</span>
        </button>
      ))}
    </div>
  );
}
