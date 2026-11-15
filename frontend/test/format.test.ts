import assert from "node:assert/strict";
import { test } from "node:test";
import { clock } from "../src/lib/format";

test("the clock shows minutes and seconds", () => {
  assert.equal(clock(0), "0:00");
  assert.equal(clock(65), "1:05");
  assert.equal(clock(300), "5:00");
  assert.equal(clock(59.6), "1:00");
  assert.equal(clock(-3), "0:00");
});
