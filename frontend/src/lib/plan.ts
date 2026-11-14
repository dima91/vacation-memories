// Facts derived from a plan document, for the counters and the Detail panel.
import type { Effect, PlanDocument, PlanElement } from "../api/types";

export interface PlanStats {
  photos: number;
  moments: number;
  transitions: number;
  tracks: number;
  total: number;
  requested: number | null;
}

export function planStats(document: PlanDocument): PlanStats {
  const { plan } = document;
  return {
    photos: plan.elements.filter((element) => element.type === "photo").length,
    moments: plan.elements.filter((element) => element.type === "video").length,
    transitions: plan.elements.reduce((sum, element) => sum + element.effects.length, 0),
    tracks: plan.music?.tracks.length ?? 0,
    total: document.duration,
    requested: plan.requested_duration,
  };
}

export function findElement(document: PlanDocument, id: string): PlanElement | undefined {
  return document.plan.elements.find((element) => element.id === id);
}

export function findEffect(element: PlanElement, id: string): Effect | undefined {
  return element.effects.find((effect) => effect.id === id);
}

export function positionOf(document: PlanDocument, id: string): number {
  return document.plan.elements.findIndex((element) => element.id === id) + 1;
}

/** How far the video is from the requested duration, as a fraction of it (section 14: the tolerance is 5%). */
export function durationDeviation(total: number, requested: number | null): number | null {
  return requested ? Math.abs(total - requested) / requested : null;
}
