import assert from "node:assert/strict";
import test from "node:test";

import {
  effectsForGateway,
  parseScreenClearState,
  recordScreenClearEffect,
  serializeScreenClearState,
} from "../src/tfr/web/screen-clear-state.mjs";

test("persists screen-clear cycle positions per Gateway and world", () => {
  const state = new Map();
  assert.equal(recordScreenClearEffect(state, "gateway-one", "glob", "water"), true);
  assert.equal(recordScreenClearEffect(state, "gateway-one", "alien", "vortex"), true);
  assert.equal(recordScreenClearEffect(state, "gateway-two", "glob", "film_burn"), true);

  const restored = parseScreenClearState(serializeScreenClearState(state));

  assert.deepEqual([...effectsForGateway(restored, "gateway-one")], [
    ["glob", "water"],
    ["alien", "vortex"],
  ]);
  assert.deepEqual([...effectsForGateway(restored, "gateway-two")], [["glob", "film_burn"]]);
});

test("rejects corrupt and unbounded screen-clear state", () => {
  assert.equal(parseScreenClearState("not json").size, 0);
  assert.equal(parseScreenClearState(JSON.stringify({ version: 2, gateways: {} })).size, 0);
  const state = parseScreenClearState(JSON.stringify({
    version: 1,
    gateways: {
      valid: { glob: "water", bad: "../effect" },
      "../gateway": { glob: "water" },
    },
  }));
  assert.deepEqual([...effectsForGateway(state, "valid")], [["glob", "water"]]);
  assert.equal(state.has("../gateway"), false);
});

test("does not mutate persisted state for invalid identifiers", () => {
  const state = new Map();
  assert.equal(recordScreenClearEffect(state, "gateway", "world", "Film Burn"), false);
  assert.equal(recordScreenClearEffect(state, "", "world", "water"), false);
  assert.equal(state.size, 0);
});
