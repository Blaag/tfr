import assert from "node:assert/strict";
import test from "node:test";

import { swipeDirection } from "../src/tfr/web/swipe.mjs";

test("recognizes deliberate horizontal swipes", () => {
  assert.equal(swipeDirection({ x: 300, y: 100, time: 0 }, { x: 150, y: 120, time: 300 }), 1);
  assert.equal(swipeDirection({ x: 100, y: 100, time: 0 }, { x: 250, y: 80, time: 300 }), -1);
});

test("rejects short, vertical, and slow gestures", () => {
  assert.equal(swipeDirection({ x: 200, y: 100, time: 0 }, { x: 160, y: 100, time: 200 }), 0);
  assert.equal(swipeDirection({ x: 200, y: 100, time: 0 }, { x: 120, y: 220, time: 200 }), 0);
  assert.equal(swipeDirection({ x: 300, y: 100, time: 0 }, { x: 150, y: 100, time: 701 }), 0);
});
