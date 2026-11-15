import assert from "node:assert/strict";
import { test } from "node:test";
import type { CatalogItem } from "../src/api/types";
import { discardKey, groupOf, groupPhotos, isBelowThreshold, similarTo } from "../src/lib/catalog";
import { catalog } from "./fixtures";

const item = (id: string, final: number | null, extra: Partial<CatalogItem> = {}): CatalogItem => ({
  ...catalog.photos[0]!, id, source: `${id}.jpg`, status: "available", discard: null, group: null,
  score: { final, technical_score: 0.5, narrative_score: final, components: null }, ...extra,
});

test("photos fall in the groups of the interest score", () => {
  assert.equal(groupOf(item("a", 0.8)), "high");
  assert.equal(groupOf(item("b", 0.79)), "medium");
  assert.equal(groupOf(item("c", 0.6)), "medium");
  assert.equal(groupOf(item("d", 0.59)), "low");
  assert.equal(groupOf(item("e", null)), "discarded");
  assert.equal(groupOf(item("f", 0.9, { status: "excluded" })), "hidden");
  assert.equal(groupOf(item("g", 0.9, { status: "discarded", discard: { reason: "mostly_black", detail: null } })), "discarded");
});

test("every group is listed, best score first, with the discarded and the hidden ones apart", () => {
  const groups = groupPhotos([item("a", 0.5), item("b", 0.9), item("c", 0.85), item("d", 0.2, { status: "excluded" })]);
  assert.deepEqual(groups.map((group) => group.id), ["high", "medium", "low", "discarded", "hidden"]);
  assert.deepEqual(groups[0]?.items.map((photo) => photo.id), ["b", "c"]);
  assert.deepEqual(groups[4]?.items.map((photo) => photo.id), ["d"]);
});

test("near-duplicates show their representative; the others are reached from it", () => {
  const group = { representative: "r", size: 3 };
  const photos = [item("r", 0.9, { group }), item("x", 0.7, { group }), item("y", 0.6, { group, status: "selected" })];
  const shown = groupPhotos(photos).flatMap((entry) => entry.items.map((photo) => photo.id)).sort();
  assert.deepEqual(shown, ["r", "y"], "a duplicate already in the timeline stays visible");
  assert.deepEqual(similarTo(photos[0]!, photos).map((photo) => photo.id), ["x", "y"]);
  assert.deepEqual(similarTo(item("alone", 0.5), photos), []);
});

test("the threshold dims what is below it and hides nothing", () => {
  assert.equal(isBelowThreshold(item("a", 0.4), 0.5), true);
  assert.equal(isBelowThreshold(item("a", 0.5), 0.5), false);
  assert.equal(isBelowThreshold(item("a", null), 0.5), false);
  const total = groupPhotos(catalog.photos).reduce((sum, group) => sum + group.items.length, 0);
  assert.ok(total <= catalog.photos.length && total > 0);
});

test("a discard reason has a text, a strange one the generic text", () => {
  assert.equal(discardKey("mostly_black"), "discard.mostly_black");
  assert.equal(discardKey("below_technical_threshold"), "discard.below_technical_threshold");
  assert.equal(discardKey("something_new"), "discard.unknown");
});
