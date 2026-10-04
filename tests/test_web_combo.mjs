import assert from "node:assert/strict";
import test from "node:test";

import { COMBO_TIMEOUT_MS, observeCombo } from "../src/tfr/web/combo.mjs";

function say(speaker, text) {
  return {
    id: `${speaker}-${text}`,
    world: "alpha",
    kind: "say",
    text: `${speaker} says, "${text}"`,
    provenance: { sender_name: speaker },
  };
}

test("conversation combos begin at three and expire after five minutes", () => {
  const streaks = new Map();
  assert.equal(observeCombo(streaks, say("Alice", "one"), 0), null);
  assert.equal(observeCombo(streaks, say("Alice", "two"), COMBO_TIMEOUT_MS - 1), null);
  const combo = observeCombo(streaks, say("Alice", "three"), 2 * COMBO_TIMEOUT_MS - 2);
  assert.equal(combo.count, 3);
  assert.equal(combo.notice, "> Speaking Spree! <");
  assert.equal(say("Alice", "three").text.slice(combo.body_start, combo.body_end), "three");
  assert.equal(observeCombo(streaks, say("Alice", "reset"), 3 * COMBO_TIMEOUT_MS), null);
});

test("Godlike multipliers continue without another message effect tier", () => {
  const streaks = new Map();
  let combo = null;
  for (let count = 1; count <= 9; count += 1) combo = observeCombo(streaks, say("Alice", String(count)), count);
  assert.equal(combo.notice, "> GODLIKE x3 <");
  assert.equal(combo.color, "#ff8000");
});

test("bare pose inference is bounded to events with configured presentation", () => {
  const streaks = new Map();
  const room = { world: "alpha", kind: "raw_output", text: "Alice waves" };
  assert.equal(observeCombo(streaks, room, 0), null);
  const pose = { ...room, presentation: { programs: [] } };
  assert.equal(observeCombo(streaks, pose, 1), null);
  assert.equal(observeCombo(streaks, pose, 2), null);
  assert.equal(observeCombo(streaks, pose, 3).count, 3);
});

test("combo body offsets use Unicode scalar positions", () => {
  const streaks = new Map();
  const event = say("😀Alice", "waves 👋");
  observeCombo(streaks, event, 1);
  observeCombo(streaks, event, 2);
  const combo = observeCombo(streaks, event, 3);
  assert.equal(Array.from(event.text).slice(combo.body_start, combo.body_end).join(""), "waves 👋");
});
