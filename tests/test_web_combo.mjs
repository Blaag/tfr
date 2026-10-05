import assert from "node:assert/strict";
import test from "node:test";

import {
  COMBO_TIMEOUT_MS,
  fireworkParticleBudget,
  observeCombo,
  validatedComboCue,
} from "../src/tfr/web/combo.mjs";

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
  const world = { server: "bare" };
  assert.equal(observeCombo(streaks, room, 1, world), null);
  assert.equal(observeCombo(streaks, room, 2, world), null);
  assert.equal(observeCombo(streaks, room, 3, world).count, 3);
});

test("deterministic mixed say and pose chains reach levels three through seven", () => {
  for (let seed = 0; seed < 8; seed += 1) {
    for (let target = 3; target <= 7; target += 1) {
      const streaks = new Map();
      let value = seed + 1;
      const random = () => ((value = (1664525 * value + 1013904223) >>> 0) / 2 ** 32);
      let combo = null;
      for (let count = 1; count <= target; count += 1) {
        const item = random() < 0.5
          ? say("Alice", `message ${count}`)
          : { world: "alpha", kind: "pose", text: `${random() < 0.5 ? "Alice" : "Alice's"} poses message ${count}`, provenance: { sender_name: "Alice" } };
        combo = observeCombo(streaks, item, count, { server: "tinymux" });
        assert.equal(combo !== null, count >= 3, `seed=${seed}, count=${count}`);
        if (combo) assert.equal(combo.count, count);
      }
      assert.equal(combo.count, target);
    }
  }
});

test("configured local character resolves You say and terse self poses", () => {
  const streaks = new Map();
  const world = { server: "tinymux", character: "Hamilton" };
  assert.equal(observeCombo(streaks, { world: "alpha", kind: "speech", text: 'You say, "one"' }, 1, world), null);
  assert.equal(observeCombo(streaks, { world: "alpha", kind: "speech", text: "Hamilton poses two" }, 2, world), null);
  assert.equal(observeCombo(streaks, { world: "alpha", kind: "say", text: 'You say, "three"' }, 3, world).count, 3);
});

test("empty speech never starts or advances a combo", () => {
  const streaks = new Map();
  for (let count = 0; count < 10; count += 1) {
    assert.equal(observeCombo(streaks, say("Alice", ""), count), null);
  }
  assert.equal(observeCombo(streaks, say("Alice", "one"), 11), null);
  assert.equal(observeCombo(streaks, say("Alice", "two"), 12), null);
  assert.equal(observeCombo(streaks, say("Alice", "three"), 13).count, 3);
});

test("combo body offsets use Unicode scalar positions", () => {
  const streaks = new Map();
  const event = say("😀Alice", "waves 👋");
  observeCombo(streaks, event, 1);
  observeCombo(streaks, event, 2);
  const combo = observeCombo(streaks, event, 3);
  assert.equal(Array.from(event.text).slice(combo.body_start, combo.body_end).join(""), "waves 👋");
});

test("firework particle budgets target twelve percent with bounded growth", () => {
  assert.equal(fireworkParticleBudget(640, 240, 8, 16), 144);
  assert.equal(fireworkParticleBudget(960, 384, 8, 16), 346);
  assert.equal(fireworkParticleBudget(100_000, 100_000, 8, 16), 1024);
  assert.equal(fireworkParticleBudget(1, 1, 8, 16), 1);
});

test("validates bounded Gateway combo cues", () => {
  const value = {
    count: 3,
    speaker: "Alice",
    body_start: 13,
    body_end: 18,
    notice: "> Speaking Spree! <",
    color: "#ffffff",
  };
  assert.deepEqual(validatedComboCue(value, 'Alice says, "hello"'), value);
  assert.equal(validatedComboCue({ ...value, body_end: 999 }, 'Alice says, "hello"'), null);
});
