// The Detail panel (section 13): it changes with the selection and shows the preview, the scores, the information,
// the limits the server computed and a help text. Without a selection it summarizes the whole video.
import { useEffect, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { urls } from "../api/client";
import type { Catalog, CatalogItem, EffectType, PlanDocument, PlanElement, ProjectState, Score, StatusResponse, Waveform } from "../api/types";
import { useI18n, type MessageKey, type Translator } from "../i18n";
import { discardKey, similarTo } from "../lib/catalog";
import { clock } from "../lib/format";
import { durationRange, effectBounds, largestWindow, trimRange } from "../lib/limits";
import { durationDeviation, findElement, planStats, positionOf } from "../lib/plan";
import type { Selection } from "../lib/selection";
import { clamp, clipAt, clipStart } from "../lib/timeline";
import { ChevronLeftIcon, ChevronRightIcon, CloseIcon } from "./icons";
import { fileName } from "./MediaPanel";
import { ProgressBar } from "./ui";
import { WaveformBars } from "./Timeline";

export interface DetailProps {
  projectId: string;
  project: ProjectState;
  document: PlanDocument | null;
  catalog: Catalog;
  waveforms: ReadonlyMap<string, Waveform | null>;
  selection: Selection;
  playhead: number;
  seekVersion: number;
  job: StatusResponse | null;
  collapsed: boolean;
  playerRef: MutableRefObject<HTMLVideoElement | null>;
  onTime: (time: number) => void;
  onSelect: (selection: Selection) => void;
  onToggle: () => void;
}

// --- the player ---

interface PlayerProps {
  src: string;
  poster?: string;
  time: number; // where the video should be (seconds from its own start) when `seekVersion` changes
  seekVersion: number;
  offset: number; // the video's start on the timeline: what `onTime` reports is shifted by it
  label: string;
  playerRef: MutableRefObject<HTMLVideoElement | null>;
  onTime: (time: number) => void;
}

export function Player({ src, poster, time, seekVersion, offset, label, playerRef, onTime }: PlayerProps) {
  const { t } = useI18n();
  const element = useRef<HTMLVideoElement | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");

  useEffect(() => { setState("loading"); }, [src]);
  useEffect(() => {
    const video = element.current;
    if (video && Math.abs(video.currentTime - time) > 0.05) video.currentTime = Math.max(0, time);
  }, [seekVersion]);
  useEffect(() => () => { if (playerRef.current === element.current) playerRef.current = null; }, [playerRef]);

  return (
    <div className="player">
      <video ref={(node) => { element.current = node; playerRef.current = node; }} src={src} poster={poster} controls preload="metadata" playsInline
        aria-label={label} onLoadedData={() => setState("ready")} onError={() => setState("failed")}
        onTimeUpdate={(event) => onTime(offset + event.currentTarget.currentTime)} />
      {state === "loading" ? <p className="player__state" role="status">{t("detail.previewLoading")}</p> : null}
      {state === "failed" ? <p className="player__state player__state--error" role="alert">{t("detail.previewFailed")}</p> : null}
    </div>
  );
}

// --- small pieces ---

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {rows.filter(([, value]) => value !== null && value !== undefined && value !== "").map(([name, value]) => (
        <div key={name} className="facts__row"><dt>{name}</dt><dd>{value}</dd></div>
      ))}
    </dl>
  );
}

function Scores({ score }: { score: Score | undefined }) {
  const { t, format } = useI18n();
  if (!score) return null;
  const rows: [string, number | null][] = [
    [t("detail.score.interest"), score.final], [t("detail.score.narrative"), score.narrative_score], [t("detail.score.technical"), score.technical_score],
  ];
  return (
    <section className="detail__section" aria-label={t("detail.scores")}>
      <h3>{t("detail.scores")}</h3>
      <ul className="scores">
        {rows.map(([name, value]) => (
          <li key={name}>
            <span>{name}</span>
            <span className="scores__bar" aria-hidden="true"><span style={{ width: `${Math.round((value ?? 0) * 100)}%` }} /></span>
            <span className="num">{format.score(value)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="detail__section" aria-label={title}><h3>{title}</h3>{children}</section>;
}

function Head({ kind, title, subtitle }: { kind: string; title: string; subtitle?: string }) {
  return (
    <div className="detail__head">
      <span className="pill">{kind}</span>
      <h3 className="detail__title">{title}</h3>
      {subtitle ? <p className="detail__subtitle">{subtitle}</p> : null}
    </div>
  );
}

const momentLabel = (item: Pick<CatalogItem, "start" | "end">) => `${clock(item.start ?? 0)} – ${clock(item.end ?? 0)}`;

// --- one detail per kind of selection ---

function ElementDetail({ props, element }: { props: DetailProps; element: PlanElement }) {
  const i18n = useI18n();
  const { t, format } = i18n;
  const { document: doc, projectId, catalog } = props;
  if (!doc) return null;
  const limits = doc.limits.elements[element.id];
  const refs = doc.refs[element.id];
  const isVideo = element.type === "video";
  const item = [...catalog.photos, ...catalog.videos.flatMap((video) => video.moments)].find((candidate) => candidate.id === element.id);
  const begin = clipStart(doc.plan.elements, element.id) ?? 0;
  const duration = durationRange(limits);
  const trim = trimRange(limits, element);
  return (
    <>
      <Head kind={t(isVideo ? "detail.kind.moment" : "detail.kind.photo")} title={fileName(element.source)}
        subtitle={`${t("detail.field.position", { n: positionOf(doc, element.id) })} · ${format.seconds(element.duration)}`} />
      {refs ? <Player src={urls.clip(projectId, refs.clip)} poster={urls.thumb(projectId, refs.media, isVideo ? element.start : undefined)}
        time={clamp(props.playhead - begin, 0, element.duration)} seekVersion={props.seekVersion} offset={begin} label={t("detail.preview")}
        playerRef={props.playerRef} onTime={props.onTime} /> : null}
      <Scores score={element.score ?? item?.score} />
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.file"), element.source],
          [t("detail.field.duration"), format.seconds(element.duration)],
          [t("detail.field.range"), isVideo ? `${format.seconds(element.start ?? 0)} – ${format.seconds(element.end ?? 0)}` : null],
          [t("detail.field.captured"), element.captured_at ? format.dateTime(element.captured_at) : null],
          [t("detail.field.section"), element.section ? t(`detail.section.${element.section}` as MessageKey) : null],
          [t("detail.field.reason"), element.reason ?? null],
          [t("detail.field.description"), item?.description ?? element.description ?? null],
          [t("detail.field.momentType"), item?.moment_type ?? element.moment_type ?? null],
          [t("detail.field.origin"), t(`detail.origin.${element.origin}` as MessageKey)],
        ]} />
      </Section>
      {duration || trim ? (
        <Section title={t("detail.limits")}>
          <Facts rows={[
            [t("detail.field.minDuration"), duration ? format.seconds(duration.min) : null],
            [t("detail.field.maxDuration"), duration ? format.seconds(duration.max) : null],
            [t("detail.field.trimStart"), trim ? format.seconds(trim.start.min) : null],
            [t("detail.field.trimEnd"), trim && Number.isFinite(trim.end.max) ? format.seconds(trim.end.max) : null],
            [t("detail.field.minLength"), trim ? format.seconds(trim.minLength) : null],
          ]} />
        </Section>
      ) : null}
      {element.effects.length > 0 ? (
        <Section title={t("summary.transitions")}>
          <ul className="links">
            {element.effects.map((effect) => (
              <li key={effect.id}>
                <button type="button" className="link" onClick={() => props.onSelect({ kind: "effect", element: element.id, id: effect.id })}>
                  {t(`transition.${effect.type}` as MessageKey)} · {format.seconds(effect.duration)}
                </button>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      <Help text={t(isVideo ? "detail.help.moment" : "detail.help.photo")} />
    </>
  );
}

function Help({ text }: { text: string }) {
  const { t } = useI18n();
  return <Section title={t("detail.help")}><p className="help">{text}</p></Section>;
}

function candidateStatusText(i18n: Translator, item: CatalogItem): string {
  const { t } = i18n;
  if (item.discard) return t("detail.discarded", { reason: t(discardKey(item.discard.reason)) });
  return t(`detail.status.${item.status}` as MessageKey);
}

function CandidateDetail({ props, item }: { props: DetailProps; item: CatalogItem }) {
  const i18n = useI18n();
  const { t, format } = i18n;
  const isMoment = item.start !== undefined;
  const similar = similarTo(item, props.catalog.photos);
  return (
    <>
      <Head kind={t(isMoment ? "detail.kind.moment" : "detail.kind.photo")} title={fileName(item.source)} subtitle={candidateStatusText(i18n, item)} />
      <img className="detail__image" alt="" src={urls.thumb(props.projectId, item.media, isMoment ? item.thumb_time : undefined)} />
      <Scores score={item.score} />
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.file"), item.source],
          [t("detail.field.range"), isMoment ? momentLabel(item) : null],
          [t("detail.field.captured"), format.dateTime(item.captured_at)],
          [t("detail.field.day"), format.date(item.day)],
          [t("detail.field.status"), candidateStatusText(i18n, item)],
          [t("detail.field.description"), item.description],
          [t("detail.field.momentType"), item.moment_type],
          [t("detail.field.category"), item.category],
        ]} />
      </Section>
      {similar.length > 0 ? (
        <Section title={t("detail.field.similar")}>
          <ul className="links">
            {similar.map((other) => (
              <li key={other.id}>
                <button type="button" className="link" onClick={() => props.onSelect(other.status === "selected" ? { kind: "element", id: other.id } : { kind: "candidate", id: other.id })}>
                  {fileName(other.source)} · {format.score(other.score.final)}
                </button>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      <Help text={t(isMoment ? "detail.help.moment" : "detail.help.photo")} />
    </>
  );
}

function VideoDetail({ props, media }: { props: DetailProps; media: string }) {
  const { t, format, plural } = useI18n();
  const video = props.catalog.videos.find((entry) => entry.media === media);
  if (!video) return null;
  return (
    <>
      <Head kind={t("detail.kind.video")} title={fileName(video.source)} subtitle={video.duration ? t("videos.length", { time: clock(video.duration) }) : undefined} />
      <img className="detail__image" alt="" src={urls.thumb(props.projectId, video.media)} />
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.file"), video.source],
          [t("detail.field.moments"), plural("videos.moments", video.moments.length)],
          [t("detail.field.onTimeline"), video.moments.filter((moment) => moment.status === "selected").length],
        ]} />
      </Section>
      <Section title={t("detail.field.moments")}>
        <ul className="links">
          {video.moments.map((moment) => (
            <li key={moment.id}>
              <button type="button" className="link" onClick={() => props.onSelect(moment.status === "selected" ? { kind: "element", id: moment.id } : { kind: "candidate", id: moment.id })}>
                {momentLabel(moment)} · {format.score(moment.score.final)} · {t(`videos.status.${moment.status}` as MessageKey)}
              </button>
            </li>
          ))}
        </ul>
      </Section>
      <Help text={t("detail.help.video")} />
    </>
  );
}

function LibraryDetail({ props, type }: { props: DetailProps; type: EffectType }) {
  const { t, format } = useI18n();
  const item = props.catalog.transitions.find((entry) => entry.type === type);
  return (
    <>
      <Head kind={t("detail.kind.transition")} title={t(`transition.${type}` as MessageKey)} subtitle={t("detail.library")} />
      <div className={`swatch swatch--${type}`} aria-hidden="true" />
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.defaultDuration"), item ? format.seconds(item.default_seconds) : null],
          [t("detail.field.minDuration"), item ? format.seconds(item.min_seconds) : null],
          [t("detail.field.where"), item ? t(`transitions.where.${item.position}` as MessageKey) : null],
        ]} />
      </Section>
      <Help text={t(`transition.help.${type}` as MessageKey)} />
    </>
  );
}

function EffectDetail({ props, elementId, id }: { props: DetailProps; elementId: string; id: string }) {
  const { t, format } = useI18n();
  const doc = props.document;
  const element = doc ? findElement(doc, elementId) : undefined;
  const effect = element?.effects.find((entry) => entry.id === id);
  if (!doc || !element || !effect) return null;
  const room = doc.limits.elements[element.id]?.effects[effect.id];
  const bounds = effectBounds(room, effect);
  return (
    <>
      <Head kind={t("detail.kind.transition")} title={t(`transition.${effect.type}` as MessageKey)}
        subtitle={`${fileName(element.source)} · ${t("detail.field.position", { n: positionOf(doc, element.id) })}`} />
      <div className={`swatch swatch--${effect.type}`} aria-hidden="true" />
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.clip"), <button key="clip" type="button" className="link" onClick={() => props.onSelect({ kind: "element", id: element.id })}>{fileName(element.source)}</button>],
          [t("detail.field.startsAt"), t("detail.field.startsAtValue", { time: format.seconds(effect.offset) })],
          [t("detail.field.duration"), format.seconds(effect.duration)],
          [t("detail.field.available"), bounds ? format.seconds(bounds.max - bounds.min) : format.seconds(largestWindow(room))],
          [t("detail.field.minDuration"), format.seconds(doc.limits.min_effect_seconds)],
          [t("detail.field.origin"), t(`detail.origin.${effect.origin}` as MessageKey)],
        ]} />
      </Section>
      <Help text={t(`transition.help.${effect.type}` as MessageKey)} />
    </>
  );
}

function TrackDetail({ props, file }: { props: DetailProps; file: string }) {
  const { t, format } = useI18n();
  const waveform = props.waveforms.get(file);
  const parts = props.document?.plan.music?.tracks.filter((track) => track.file === file).reduce((sum, track) => sum + track.parts.length, 0) ?? 0;
  return (
    <>
      <Head kind={t("detail.kind.track")} title={fileName(file)} />
      <div className="detail__wave">{waveform ? <WaveformBars peaks={waveform.peaks} /> : <p className="empty">{waveform === null ? t("music.waveformFailed") : t("app.loading")}</p>}</div>
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.file"), file],
          [t("detail.field.duration"), waveform ? clock(waveform.duration) : null],
          [t("detail.field.inVideo"), parts > 0 ? t("music.inVideo", { n: parts }) : t("music.notInVideo")],
          [t("detail.field.shift"), parts > 0 && props.document?.plan.music ? format.seconds(props.document.plan.music.offset) : null],
        ]} />
      </Section>
      <Help text={t("detail.help.track")} />
    </>
  );
}

function PartDetail({ props, track, part }: { props: DetailProps; track: number; part: number }) {
  const { t, format } = useI18n();
  const doc = props.document;
  const music = doc?.plan.music;
  const data = music?.tracks[track]?.parts[part];
  if (!doc || !music || !data) return null;
  const file = music.tracks[track]?.file ?? "";
  const waveform = props.waveforms.get(file);
  const range = doc.limits.music?.offset;
  return (
    <>
      <Head kind={t("detail.kind.part")} title={fileName(file)} subtitle={t("timeline.musicPart", { part: part + 1, file: fileName(file) })} />
      <div className="detail__wave">{waveform ? <WaveformBars peaks={waveform.peaks.slice(Math.floor((data.start / waveform.duration) * waveform.peaks.length), Math.ceil((data.end / waveform.duration) * waveform.peaks.length))} /> : null}</div>
      <Section title={t("detail.info")}>
        <Facts rows={[
          [t("detail.field.trackRange"), `${clock(data.start)} – ${clock(data.end)}`],
          [t("detail.field.duration"), format.seconds(data.end - data.start)],
          [t("timeline.muted"), data.muted ? t("timeline.muted") : null],
          [t("detail.field.fadeIn"), data.fade_in > 0 ? format.seconds(data.fade_in) : null],
          [t("detail.field.fadeOut"), data.fade_out > 0 ? format.seconds(data.fade_out) : null],
          [t("detail.field.shift"), format.seconds(music.offset)],
          [t("detail.field.shiftRange"), range ? `${format.seconds(range.min)} … ${format.seconds(range.max)}` : null],
        ]} />
      </Section>
      <Help text={`${t("detail.help.part")} ${t("detail.help.shift")}`} />
    </>
  );
}

function SummaryDetail({ props }: { props: DetailProps }) {
  const i18n = useI18n();
  const { t, format, plural } = i18n;
  const { document: doc, project, job } = props;
  const analysis = props.catalog.analysis ?? project.analysis;
  const coverage = analysis?.ai?.coverage ?? null;
  const running = job?.job && (job.job.state === "running" || job.job.state === "stopping") ? job : null;
  const stats = doc ? planStats(doc) : null;
  const begin = doc ? clipAt(doc.plan.elements, props.playhead) : null;
  const preview = project.preview;
  return (
    <>
      <Head kind={t("detail.kind.timeline")} title={t("summary.title")} />
      {preview.present && preview.name ? (
        <>
          <Player src={urls.video(props.projectId, preview.name)} time={props.playhead} seekVersion={props.seekVersion} offset={0}
            label={t("summary.preview")} playerRef={props.playerRef} onTime={props.onTime} />
          <p className={`note${preview.outdated ? " note--warn" : ""}`}>{preview.outdated ? t("summary.previewOutdated") : t("summary.previewCurrent")}</p>
        </>
      ) : <p className="note">{t("summary.previewNone")}</p>}
      {stats ? (
        <Section title={t("detail.info")}>
          <Facts rows={[
            [t("summary.duration"), stats.requested ? t("summary.durationValue", { total: clock(stats.total), requested: clock(stats.requested) }) : t("summary.durationNoTarget", { total: clock(stats.total) })],
            [t("summary.photos"), stats.photos],
            [t("summary.moments"), stats.moments],
            [t("summary.transitions"), stats.transitions],
            [t("summary.music"), stats.tracks > 0 ? plural("summary.musicValue", stats.tracks) : t("summary.musicNone")],
            [t("summary.format"), t("summary.formatValue", { width: doc?.plan.output.width ?? 0, height: doc?.plan.output.height ?? 0, fps: format.number(doc?.plan.output.fps ?? 0) })],
            [t("timeline.playhead"), `${clock(props.playhead)}${begin ? ` · ${fileName(begin.source)}` : ""}`],
          ]} />
        </Section>
      ) : null}
      {stats && (durationDeviation(stats.total, stats.requested) ?? 0) > 0.05 ? <p className="note note--warn" role="status">{t("summary.durationOff")}</p> : null}
      {running ? (
        <Section title={t("summary.job")}>
          <ProgressBar value={running.progress?.done ?? 0} max={running.progress?.total ?? 0} label={t("progress.bar")} />
        </Section>
      ) : null}
      {doc && doc.warnings.length > 0 ? (
        <Section title={t("summary.warnings")}>
          <ul className="warnings">
            {doc.warnings.map((warning, index) => (
              <li key={`${warning.code}-${index}`}>{warningText(i18n, warning.code, warning.message)}</li>
            ))}
          </ul>
        </Section>
      ) : null}
      <Section title={t("summary.analysis")}>
        <Facts rows={[
          [t("summary.analysis"), coverage ? t("summary.analysisAi", { analyzed: coverage.analyzed, candidates: coverage.candidates }) : t("summary.analysisNoAi")],
          [t("summary.analysisDate"), analysis?.generated_at ? format.dateTime(analysis.generated_at) : null],
        ]} />
      </Section>
      {project.exports.length > 0 ? (
        <Section title={t("summary.exports")}>
          <ul className="links">{project.exports.map((entry) => <li key={entry.name}>{entry.name}.mp4 · {format.bytes(entry.size)}{entry.partial ? ` (${t("summary.partial")})` : ""}</li>)}</ul>
        </Section>
      ) : null}
      {stats && stats.tracks > 0 ? <Help text={t("detail.help.music")} /> : null}
    </>
  );
}

/** The text of a warning of the plan: the catalog's for a known code, the server's English message otherwise (D10). */
function warningText(i18n: Translator, code: string, message: string): string {
  return i18n.has(`warning.${code}`) ? i18n.t(`warning.${code}` as MessageKey) : message;
}

export function DetailPanel(props: DetailProps) {
  const { t } = useI18n();
  const { selection, document: doc, catalog } = props;
  let body: ReactNode;
  switch (selection.kind) {
    case "element": {
      const element = doc ? findElement(doc, selection.id) : undefined;
      body = element ? <ElementDetail props={props} element={element} /> : <SummaryDetail props={props} />;
      break;
    }
    case "candidate": {
      const item = [...catalog.photos, ...catalog.videos.flatMap((video) => video.moments)].find((candidate) => candidate.id === selection.id);
      body = item ? <CandidateDetail props={props} item={item} /> : <SummaryDetail props={props} />;
      break;
    }
    case "video": body = <VideoDetail props={props} media={selection.media} />; break;
    case "library": body = <LibraryDetail props={props} type={selection.type} />; break;
    case "effect": body = <EffectDetail props={props} elementId={selection.element} id={selection.id} />; break;
    case "track": body = <TrackDetail props={props} file={selection.file} />; break;
    case "part": body = <PartDetail props={props} track={selection.track} part={selection.part} />; break;
    default: body = <SummaryDetail props={props} />;
  }
  return (
    <aside className={`detail${props.collapsed ? " is-collapsed" : ""}`} aria-labelledby="detail-title">
      <header className="detail__bar">
        {props.collapsed ? null : <h2 id="detail-title">{t("detail.title")}</h2>}
        {props.collapsed ? <h2 id="detail-title" className="visually-hidden">{t("detail.title")}</h2> : null}
        {!props.collapsed && selection.kind !== "timeline" ? (
          <button type="button" className="btn btn--icon btn--quiet" aria-label={t("detail.clearSelection")} title={t("detail.clearSelection")} onClick={() => props.onSelect({ kind: "timeline" })}><CloseIcon /></button>
        ) : null}
        <button type="button" className="btn btn--icon" aria-expanded={!props.collapsed} aria-label={props.collapsed ? t("detail.expand") : t("detail.collapse")} onClick={props.onToggle}>
          {props.collapsed ? <ChevronLeftIcon /> : <ChevronRightIcon />}
        </button>
      </header>
      {props.collapsed ? null : <div className="detail__body">{body}</div>}
    </aside>
  );
}
