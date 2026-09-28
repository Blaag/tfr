import assert from "node:assert/strict";
import test from "node:test";

import {
  animatePresentationElement,
  interpolatePresentationColor,
  presentationAnimationFrames,
  selectPresentation,
  splitPresentationRuns,
  validatedPresentation,
} from "../src/tfr/web/presentation.mjs";

function eventWithPresentation(overrides = {}) {
  return {
    text: "Alice says 👩🏽‍💻 hi",
    timestamp: "2026-01-01T00:00:00Z",
    presentation: {
      version: 1,
      programs: [
        {
          start: 0,
          end: 5,
          duration_ms: 1200,
          repeat_ms: 6000,
          repeat_count: 2,
          frames_per_second: 20,
          variants: [
            {
              requires: ["foreground_color", "timeline"],
              style: { foreground: "#a9914a" },
              foreground_keyframes: [
                { at: 0, color: "#a9914a" },
                { at: 0.5, color: "#fff08a" },
                { at: 1, color: "#a9914a" },
              ],
            },
          ],
          reduced_motion: { foreground: "#fff08a", bold: true },
          fallback: { foreground: "#a9914a" },
        },
      ],
    },
    ...overrides,
  };
}

test("validates the bounded versioned presentation schema", () => {
  assert.equal(validatedPresentation(eventWithPresentation())?.length, 1);
  for (const mutate of [
    (value) => (value.version = 2),
    (value) => (value.programs[0].end = 999),
    (value) => (value.programs[0].duration_ms = Infinity),
    (value) => (value.programs[0].variants[0].style.position = "fixed"),
    (value) => (value.programs[0].variants[0].requires = ["css"]),
    (value) => (value.programs[0].variants[0].foreground_keyframes[0].color = "red"),
  ]) {
    const event = structuredClone(eventWithPresentation());
    mutate(event.presentation);
    assert.equal(validatedPresentation(event), null);
  }
});

test("selects the timeline or explicit reduced-motion style", () => {
  const program = validatedPresentation(eventWithPresentation())[0];

  assert.equal(selectPresentation(program, true).animation.length, 3);
  assert.deepEqual(selectPresentation(program, false), {
    style: { foreground: "#fff08a", bold: true },
    animation: null,
  });
});

test("splits Unicode text by scalar offsets and preserves static run styles", () => {
  const event = eventWithPresentation();
  const runs = [
    { text: "Alice", style: { underline: true }, role: "speaker" },
    { text: " says 👩🏽‍💻 hi", style: {}, role: undefined },
  ];

  const split = splitPresentationRuns(event, runs, false);

  assert.equal(split.map((run) => run.text).join(""), event.text);
  assert.deepEqual(split[0], {
    text: "Alice",
    style: { underline: true },
    role: "speaker",
    presentation: {
      program: validatedPresentation(event)[0],
      keyframes: null,
      baseColor: "#fff08a",
      style: { foreground: "#fff08a", bold: true },
    },
  });
});

test("uses matching Oklab color interpolation and bounded sampled frames", () => {
  assert.equal(interpolatePresentationColor("#000000", "#ffffff", 0.5), "#636363");
  const program = validatedPresentation(eventWithPresentation())[0];
  const selected = selectPresentation(program, true);
  const frames = presentationAnimationFrames(program, selected.animation, selected.style.foreground);

  assert.equal(frames.length, 27);
  assert.deepEqual(frames[0], { color: "#a9914a", offset: 0 });
  assert.deepEqual(frames.at(-1), { color: "#a9914a", offset: 1 });
});

test("rejects undeclared style capabilities and grapheme-splitting targets", () => {
  const undeclared = structuredClone(eventWithPresentation());
  undeclared.presentation.programs[0].variants[0].style.bold = true;
  assert.equal(validatedPresentation(undeclared), null);

  const grapheme = eventWithPresentation({ text: "e\u0301" });
  grapheme.presentation.programs[0].start = 0;
  grapheme.presentation.programs[0].end = 1;
  assert.equal(validatedPresentation(grapheme), null);
});

test("invalid presentation falls back to unchanged readable text", () => {
  const event = eventWithPresentation();
  event.presentation.programs[0].fallback = { backgroundImage: "url(javascript:alert(1))" };

  assert.deepEqual(splitPresentationRuns(event, null, true), [
    { text: event.text, style: {}, role: undefined, presentation: null },
  ]);
});

test("validates and splits a bounded Unicode character sweep", () => {
  const event = eventWithPresentation({ text: "alice says hi" });
  const variant = event.presentation.programs[0].variants[0];
  variant.requires = ["character_foreground", "timeline", "character_case"];
  variant.style = {};
  variant.foreground_keyframes = [];
  variant.character_sweep = {
    positions: [
      { at: 0, position: 0 },
      { at: 0.5, position: 1 },
      { at: 1, position: 0 },
    ],
    base_color: "#180000",
    head_color: "#ff0000",
    trail_width: 2,
    uppercase_head: true,
  };

  const program = validatedPresentation(event)[0];
  const selected = selectPresentation(program, true);
  const runs = splitPresentationRuns(event, null, true);

  assert.deepEqual(selected.style, { foreground: "#180000" });
  assert.equal(selected.animation, null);
  assert.equal(selected.characterSweep.head_color, "#ff0000");
  assert.deepEqual(
    runs.slice(0, 5).map((run) => [run.text, run.presentation.characterIndex]),
    [
      ["a", 0],
      ["l", 1],
      ["i", 2],
      ["c", 3],
      ["e", 4],
    ],
  );
});

test("rejects oversized or malformed character sweeps", () => {
  const event = eventWithPresentation({ text: "a".repeat(65) });
  event.presentation.programs[0].end = 65;
  const variant = event.presentation.programs[0].variants[0];
  variant.requires = ["character_foreground", "timeline", "character_case"];
  variant.style = {};
  variant.foreground_keyframes = [];
  variant.character_sweep = {
    positions: [
      { at: 0, position: 0 },
      { at: 1, position: 1 },
    ],
    base_color: "#180000",
    head_color: "#ff0000",
    trail_width: 2,
    uppercase_head: true,
  };
  assert.equal(validatedPresentation(event), null);

  event.text = "alice";
  event.presentation.programs[0].end = 5;
  variant.character_sweep.trail_width = 99;
  assert.equal(validatedPresentation(event), null);
});

test("runs one bounded animation clock for a character sweep and cleans up", () => {
  const originalRequestAnimationFrame = globalThis.requestAnimationFrame;
  const originalCancelAnimationFrame = globalThis.cancelAnimationFrame;
  let scheduled = null;
  let cancelled = null;
  globalThis.requestAnimationFrame = (callback) => {
    scheduled = callback;
    return 17;
  };
  globalThis.cancelAnimationFrame = (id) => {
    cancelled = id;
  };
  const listeners = {};
  const animation = {
    currentTime: 500,
    addEventListener(name, callback) {
      listeners[name] = callback;
    },
  };
  const characters = [..."Alice"].map((text) => ({
    dataset: { caseable: "true" },
    style: {},
    textContent: text,
  }));
  const element = {
    animate(frames, options) {
      assert.deepEqual(frames, [{ opacity: 1 }, { opacity: 1 }]);
      assert.equal(options.duration, 2000);
      return animation;
    },
    querySelectorAll() {
      return characters;
    },
  };
  const presentation = {
    program: {
      duration_ms: 2000,
      repeat_ms: 2000,
      repeat_count: 2,
      frames_per_second: 20,
    },
    keyframes: null,
    characterSweep: {
      positions: [
        { at: 0, position: 0 },
        { at: 0.5, position: 1 },
        { at: 1, position: 0 },
      ],
      base_color: "#180000",
      head_color: "#ff0000",
      trail_width: 2,
      uppercase_head: true,
    },
  };

  try {
    assert.equal(
      animatePresentationElement(element, presentation, "2026-01-01T00:00:00Z", Date.parse("2026-01-01T00:00:00Z")),
      animation,
    );
    scheduled();
    assert.equal(characters[2].style.textTransform, "uppercase");
    assert.equal(characters[0].style.textTransform, "lowercase");
    assert.equal(characters[2].style.color, "#ff0000");
    assert.notEqual(characters[1].style.color, "#180000");
    listeners.cancel();
    assert.equal(cancelled, 17);
    assert.deepEqual(characters.map((character) => character.textContent), [..."Alice"]);
    assert.ok(characters.every((character) => character.style.color === "#180000"));
    assert.ok(characters.every((character) => character.style.textTransform === "none"));
  } finally {
    globalThis.requestAnimationFrame = originalRequestAnimationFrame;
    globalThis.cancelAnimationFrame = originalCancelAnimationFrame;
  }
});
