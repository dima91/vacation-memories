import assert from "node:assert/strict";
import { test } from "node:test";
import { catalogs, errorText, exitText, interpolate, makeTranslator } from "../src/i18n";
import { it } from "../src/i18n/it";

const placeholders = (text: string): string[] => [...text.matchAll(/\{(\w+)\}/g)].map((found) => found[1] ?? "").sort();

test("every catalog has every key of the Italian one, with the same placeholders", () => {
  for (const [language, messages] of Object.entries(catalogs)) {
    const missing = Object.keys(it).filter((key) => !(key in messages));
    assert.deepEqual(missing, [], `${language} lacks keys`);
    for (const [key, text] of Object.entries(it)) {
      assert.deepEqual(placeholders((messages as Record<string, string>)[key] ?? ""), placeholders(text), `${language}: ${key}`);
    }
  }
});

test("Italian is the default language and the only one for now", () => {
  assert.deepEqual(Object.keys(catalogs), ["it"]);
  assert.equal(makeTranslator("it").languages.length, 1, "with one catalog there is no selector to show");
  assert.equal(makeTranslator("xx").language, "xx");
  assert.equal(makeTranslator("xx").t("app.name"), "Vacation Memories", "an unknown language falls back to Italian");
});

test("placeholders are filled and unknown ones are left alone", () => {
  assert.equal(interpolate("{a} e {b}", { a: 1, b: "due" }), "1 e due");
  assert.equal(interpolate("{a} {missing}", { a: 1 }), "1 {missing}");
  assert.equal(interpolate("nessuno"), "nessuno");
});

test("plurals follow the language", () => {
  const { plural } = makeTranslator("it");
  assert.equal(plural("videos.moments", 1), "1 momento");
  assert.equal(plural("videos.moments", 4), "4 momenti");
  assert.equal(plural("summary.musicValue", 0), "0 brani");
});

test("numbers, durations and dates use the catalog's locale", () => {
  const { format } = makeTranslator("it");
  assert.equal(format.seconds(3.5), "3,5 s");
  assert.equal(format.score(0.87), "0,87");
  assert.equal(format.score(null), "–");
  assert.equal(format.number(12345.5, 1), "12.345,5");
  assert.match(format.date("2023-07-14"), /14 lug 2023/);
  assert.equal(format.bytes(1536), "1,5 KB");
});

test("a server error code is translated, an unknown one shows the server's English message", () => {
  const translator = makeTranslator("it");
  assert.equal(errorText(translator, "revision_conflict", "the plan changed elsewhere"), it["error.revision_conflict"]);
  assert.equal(errorText(translator, "brand_new_code", "something English"), "something English");
  assert.equal(exitText(translator, "interrupted", "stopped"), it["exit.interrupted"]);
  assert.equal(exitText(translator, "strange", "the CLI said so"), "the CLI said so");
});
