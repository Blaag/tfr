import assert from "node:assert/strict";
import test from "node:test";

import {
  eventsAfterClearCutoff,
  isRestoreSwipe,
  renderScreenClearFrame,
  selectScreenClearEffect,
  validatedScreenClearConfig,
} from "../src/tfr/web/screen-clear.mjs";

test("screen clear configuration accepts only supported portable effects", () => {
  assert.deepEqual(
    validatedScreenClearConfig({
      mode: "locked",
      effect: "doom_fire",
      effects: [
        { name: "doom_fire", duration_ms: 3200, frames_per_second: 24 },
        { name: "arbitrary_javascript", duration_ms: 1, frames_per_second: 1000 },
      ],
    }),
    {
      mode: "locked",
      effect: "doom_fire",
      effects: [{ name: "doom_fire", duration_ms: 3200, frames_per_second: 24, options: {} }],
    },
  );
});

test("all bundled portable screen clear effects are accepted", () => {
  const names = [
    "water_ripple",
    "doom_fire",
    "acid_rain",
    "film_burn",
    "flame",
    "sandstorm",
    "vortex",
    "water",
  ];

  const config = validatedScreenClearConfig({
    effects: names.map((name) => ({ name, duration_ms: 1000, frames_per_second: 24 })),
  });

  assert.deepEqual(config.effects.map((effect) => effect.name), names);
});

test("each portable effect renders a bounded intermediate frame and an empty final frame", () => {
  const config = validatedScreenClearConfig(null);
  const cells = [
    {
      text: "A",
      x: 20,
      y: 30,
      width: 10,
      height: 18,
      color: "#ffffff",
      fontFamily: "monospace",
      fontSize: "14px",
      fontStyle: "normal",
      fontWeight: "400",
    },
    {
      text: "界",
      x: 40,
      y: 70,
      width: 20,
      height: 18,
      color: "#87d7ff",
      fontFamily: "monospace",
      fontSize: "14px",
      fontStyle: "normal",
      fontWeight: "700",
    },
  ];

  for (const effect of config.effects) {
    const calls = [];
    const context = {
      fillStyle: "",
      font: "",
      fillText: (...arguments_) => calls.push(arguments_),
    };
    const snapshot = { cells, width: 120, height: 100, seed: 7, effectState: null };
    renderScreenClearFrame(context, snapshot, effect, 0.08);
    assert.ok(calls.length > 0, `${effect.name} renders an intermediate frame`);
    assert.ok(calls.length < 100, `${effect.name} keeps frame work bounded`);
    calls.length = 0;
    renderScreenClearFrame(context, snapshot, effect, 1);
    assert.equal(calls.length, 0, `${effect.name} finishes empty`);
  }
});

test("cycle selection advances without repeating the previous effect", () => {
  const first = selectScreenClearEffect(null);
  const second = selectScreenClearEffect(null, first.name);

  assert.equal(first.name, "water_ripple");
  assert.equal(second.name, "doom_fire");
});

test("random selection avoids an immediate repeat when alternatives exist", () => {
  const selected = selectScreenClearEffect({ mode: "random" }, "water_ripple", () => 0);

  assert.equal(selected.name, "doom_fire");
});

test("clear cutoff hides old events without deleting them", () => {
  const events = [{ id: "one" }, { id: "two" }, { id: "three" }];

  assert.deepEqual(eventsAfterClearCutoff(events, "two"), [{ id: "three" }]);
  assert.equal(eventsAfterClearCutoff(events, null), events);
});

test("restore requires a deliberate downward swipe at the top", () => {
  const start = { x: 100, y: 100, time: 0 };

  assert.equal(isRestoreSwipe(start, { x: 105, y: 190, time: 400 }, true), true);
  assert.equal(isRestoreSwipe(start, { x: 105, y: 190, time: 400 }, false), false);
  assert.equal(isRestoreSwipe(start, { x: 190, y: 180, time: 400 }, true), false);
});
