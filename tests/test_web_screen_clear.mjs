import assert from "node:assert/strict";
import test from "node:test";

import {
  eventsAfterClearCutoff,
  isRestoreSwipe,
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
      effects: [{ name: "doom_fire", duration_ms: 3200, frames_per_second: 24 }],
    },
  );
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
