// What the Media panel shows from the catalog: photos grouped by interest score (section 11).
import type { Catalog, CatalogItem, CandidateStatus as CatalogStatus, Plan, PlanElement } from "../api/types";
import type { MessageKey } from "../i18n/it";
import { isExcluded } from "./planEdit";

export type PhotoGroupId = "high" | "medium" | "low" | "discarded" | "hidden";

export const GROUP_THRESHOLDS: readonly { id: "high" | "medium" | "low"; min: number }[] = [
  { id: "high", min: 0.8 },
  { id: "medium", min: 0.6 },
  { id: "low", min: 0 },
];

export interface PhotoGroup {
  id: PhotoGroupId;
  items: CatalogItem[];
}

export function groupOf(item: CatalogItem): PhotoGroupId {
  if (item.status === "excluded") return "hidden";
  if (item.status === "discarded" || item.score.final === null) return "discarded";
  return GROUP_THRESHOLDS.find((group) => (item.score.final ?? 0) >= group.min)?.id ?? "low";
}

/**
 * A group of near-duplicates shows its representative (the best one) with the count; the others are reached from it.
 * A duplicate that is already in the timeline is still listed.
 */
export function isShownInGrid(item: CatalogItem): boolean {
  return item.group === null || item.group.representative === item.id || item.status === "selected";
}

export function groupPhotos(photos: readonly CatalogItem[]): PhotoGroup[] {
  const order: PhotoGroupId[] = ["high", "medium", "low", "discarded", "hidden"];
  const buckets = new Map<PhotoGroupId, CatalogItem[]>(order.map((id) => [id, []]));
  for (const photo of photos) {
    if (isShownInGrid(photo) || photo.status === "discarded" || photo.status === "excluded") buckets.get(groupOf(photo))?.push(photo);
  }
  for (const items of buckets.values()) items.sort((a, b) => (b.score.final ?? -1) - (a.score.final ?? -1));
  return order.map((id) => ({ id, items: buckets.get(id) ?? [] }));
}

export function similarTo(photo: CatalogItem, photos: readonly CatalogItem[]): CatalogItem[] {
  if (!photo.group) return [];
  const representative = photo.group.representative;
  return photos.filter((other) => other.id !== photo.id && other.group?.representative === representative);
}

export const isBelowThreshold = (item: CatalogItem, threshold: number): boolean =>
  item.score.final !== null && item.score.final < threshold;

const DISCARD_KEYS = {
  mostly_black: "discard.mostly_black",
  below_technical_threshold: "discard.below_technical_threshold",
} as const;

/** The catalog key of the text for a discard reason of the analysis (an unknown reason gets the generic text). */
export function discardKey(reason: string): MessageKey {
  return (DISCARD_KEYS as Record<string, MessageKey>)[reason] ?? "discard.unknown";
}

// --- the catalog as the plan has it ---

const emptyScore = { final: null, technical_score: 0, narrative_score: null, components: null };

/** The moment the user made on a bar of a video (an element of the plan that is not a candidate). */
function momentOfPlan(element: PlanElement, media: string): CatalogItem {
  return {
    id: element.id, source: element.source, media, status: "selected", discard: null, score: element.score ?? emptyScore,
    moment_type: element.moment_type ?? null, category: element.category ?? null, narrative_value: null,
    description: element.description ?? null, captured_at: element.captured_at ?? "", day: "", group: null,
    start: element.start, end: element.end, thumb_time: Math.round((((element.start ?? 0) + (element.end ?? 0)) / 2) * 10) / 10,
  };
}

function statusIn(item: CatalogItem, plan: Plan): CatalogStatus {
  if (plan.elements.some((element) => element.id === item.id)) return "selected";
  if (isExcluded(plan.excluded, item.source, item.start, item.end)) return "excluded";
  return item.discard ? "discarded" : "available";
}

/**
 * The statuses of the catalog follow the plan on screen, not the one the server last read: an element added, removed or
 * hidden shows at once. A moment in the plan has the plan's start and end (it may be trimmed), and the moments the user
 * made are listed with the ones the analysis found.
 */
export function withPlan(catalog: Catalog, plan: Plan): Catalog {
  const inPlan = new Map(plan.elements.map((element) => [element.id, element]));
  const update = (item: CatalogItem): CatalogItem => {
    const status = statusIn(item, plan);
    const element = inPlan.get(item.id);
    if (element?.type === "video" && element.start !== undefined && element.end !== undefined) {
      return { ...item, status, start: element.start, end: element.end };
    }
    return item.status === status ? item : { ...item, status };
  };
  return {
    ...catalog,
    photos: catalog.photos.map(update),
    videos: catalog.videos.map((video) => {
      // A deleted moment (or one absorbed by a merge) is gone for the user: it stays in `excluded` only so the planner never picks it again.
      const found = video.moments.map(update).filter((item) => item.status !== "excluded");
      const known = new Set(found.map((item) => item.id));
      const made = plan.elements.filter((element) => element.type === "video" && element.source === video.source && !known.has(element.id))
        .map((element) => momentOfPlan(element, video.media));
      return { ...video, moments: [...found, ...made].sort((a, b) => (a.start ?? 0) - (b.start ?? 0)) };
    }),
  };
}

