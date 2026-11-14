// What the Media panel shows from the catalog: photos grouped by interest score (section 11).
import type { CatalogItem } from "../api/types";
import type { MessageKey } from "../i18n/it";

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
