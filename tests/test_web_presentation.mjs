import assert from "node:assert/strict";
import test from "node:test";

import {
  animatePresentationElement,
  interpolatePresentationColor,
  presentationAnimationFrames,
  presentationValidationResult,
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
    (value) => (value.version = 3),
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
    presentations: [split[0].presentation],
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
    { text: event.text, style: {}, role: undefined, presentation: null, presentations: [] },
  ]);
});

test("presentation validation reports a bounded palette rejection reason", () => {
  const event = eventWithPresentation();
  const variant = event.presentation.programs[0].variants[0];
  variant.foreground_keyframes = [];
  variant.style = {};
  variant.requires = ["character_foreground", "timeline"];
  variant.character_palette_front = {
    colors: ["#ffffff"],
    spread: 0.5,
    direction: "forward",
  };

  assert.deepEqual(presentationValidationResult(event).error, {
    code: "palette_front_rejected",
    path: "presentation.programs[].variants[].character_palette_front",
  });
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
  const event = eventWithPresentation({ text: "a".repeat(101) });
  event.presentation.programs[0].end = 101;
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

test("validates bounded hue-wave and palette-front character tracks", () => {
  const hue = eventWithPresentation({ text: "Jarin" });
  const hueVariant = hue.presentation.programs[0].variants[0];
  hueVariant.requires = ["character_foreground", "timeline"];
  hueVariant.style = {};
  hueVariant.foreground_keyframes = [];
  hueVariant.character_hue_wave = {
    base_color: "#ffc9d7",
    saturation: 0.78,
    brightness: 1,
    spatial_cycles: 1,
    temporal_cycles: 1,
  };
  const selectedHue = selectPresentation(validatedPresentation(hue)[0], true);
  assert.equal(selectedHue.characterHueWave.base_color, "#ffc9d7");
  assert.equal(splitPresentationRuns(hue, null, true).length, 5);

  const ember = structuredClone(hue);
  delete ember.presentation.programs[0].variants[0].character_hue_wave;
  ember.presentation.programs[0].variants[0].character_palette_front = {
    colors: ["#ffff5f", "#ffaf00", "#d70000", "#a9914a"],
    spread: 0.55,
    direction: "forward",
  };
  const selectedEmber = selectPresentation(validatedPresentation(ember)[0], true);
  assert.equal(selectedEmber.characterPaletteFront.colors.at(-1), "#a9914a");

  ember.presentation.programs[0].variants[0].character_palette_front.colors = ["#ffffff"];
  assert.equal(validatedPresentation(ember), null);
});

test("validates version two style, sparkle, and reveal tracks", () => {
  const styleEvent = eventWithPresentation({ text: "Alice" });
  styleEvent.presentation.version = 2;
  const variant = styleEvent.presentation.programs[0].variants[0];
  variant.requires = ["character_style", "character_foreground", "character_case", "timeline"];
  variant.style = {};
  variant.foreground_keyframes = [];
  variant.character_style_sweep = {
    positions: [{ at: 0, position: 0 }, { at: 1, position: 1 }],
    base_style: { foreground: "#111111" },
    active_style: { foreground: "#eeeeee", reverse: true },
    width: 1,
    trail_mode: "symmetric",
    start_padding: 0,
    end_padding: 1,
    uppercase_active: true,
    lowercase_inactive: true,
    stepped: true,
  };
  assert.equal(validatedPresentation(styleEvent)?.length, 1);
  assert.equal(splitPresentationRuns(styleEvent, null, true).length, 5);

  const sparkleEvent = structuredClone(styleEvent);
  const sparkleVariant = sparkleEvent.presentation.programs[0].variants[0];
  sparkleVariant.requires = ["character_foreground", "timeline"];
  delete sparkleVariant.character_style_sweep;
  sparkleVariant.character_sparkle = {
    base_color: "#111111", accent_color: "#ffffff", count: 2, seed: 42,
  };
  assert.equal(validatedPresentation(sparkleEvent)?.length, 1);

  const revealEvent = structuredClone(styleEvent);
  const revealVariant = revealEvent.presentation.programs[0].variants[0];
  revealVariant.requires = ["character_foreground", "character_text", "timeline"];
  delete revealVariant.character_style_sweep;
  revealVariant.character_reveal = {
    interval_ms: 20,
    base_color: "#d7d7d7",
    accent_color: "#ffffff",
    glitch_width: 3,
    settle_width: 3,
    seed: 7,
    glitch_characters: "#%",
    inline_glitch_chance: 0.05,
  };
  assert.equal(validatedPresentation(revealEvent)?.length, 1);
  revealVariant.character_reveal.glitch_characters = "bad value";
  assert.equal(validatedPresentation(revealEvent), null);
});

test("composes overlapping speaker and reveal programs", () => {
  const event = eventWithPresentation({ text: 'Jarin says, "hello"' });
  event.presentation.version = 2;
  event.presentation.programs[0].end = 5;
  const reveal = structuredClone(event.presentation.programs[0]);
  reveal.end = Array.from(event.text).length;
  reveal.variants[0] = {
    requires: ["character_foreground", "character_text", "timeline"],
    style: {},
    foreground_keyframes: [],
    character_reveal: {
      interval_ms: 50,
      base_color: "#d7d7d7",
      accent_color: "#ffffff",
      glitch_width: 2,
      settle_width: 1,
      seed: 7,
      glitch_characters: "#%",
      inline_glitch_chance: 0,
    },
  };
  event.presentation.programs.push(reveal);

  assert.equal(validatedPresentation(event)?.length, 2);
  const runs = splitPresentationRuns(event, null, true);
  assert.equal(runs[0].presentations.length, 2);
  assert.equal(runs.at(-1).presentations.length, 1);
});

test("expired and non-budgeted character programs collapse to static runs", () => {
  const event = eventWithPresentation({ text: "Alice" });
  const variant = event.presentation.programs[0].variants[0];
  variant.requires = ["character_foreground", "timeline", "character_case"];
  variant.style = {};
  variant.foreground_keyframes = [];
  variant.character_sweep = {
    positions: [{ at: 0, position: 0 }, { at: 1, position: 1 }],
    base_color: "#180000",
    head_color: "#ff0000",
    trail_width: 2,
    uppercase_head: true,
  };
  const started = Date.parse(event.timestamp);

  const expired = splitPresentationRuns(event, null, true, started + 12001);
  const nonBudgeted = splitPresentationRuns(event, null, false, started);

  assert.equal(expired.length, 1);
  assert.equal(expired[0].text, "Alice");
  assert.equal(expired[0].presentation.characterSweep, undefined);
  assert.deepEqual(expired[0].presentation.style, { foreground: "#a9914a" });
  assert.equal(nonBudgeted.length, 1);
  assert.equal(nonBudgeted[0].presentation.characterSweep, undefined);
  assert.deepEqual(nonBudgeted[0].presentation.style, { foreground: "#fff08a", bold: true });
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

test("renders hue-wave and palette-front character colors", () => {
  const originalRequestAnimationFrame = globalThis.requestAnimationFrame;
  const originalCancelAnimationFrame = globalThis.cancelAnimationFrame;
  let scheduled = null;
  globalThis.requestAnimationFrame = (callback) => { scheduled = callback; return 1; };
  globalThis.cancelAnimationFrame = () => {};
  const characters = [..."Jarin"].map(() => ({ dataset: {}, style: {} }));
  const animation = {
    currentTime: 450,
    addEventListener() {},
  };
  const element = {
    animate() { return animation; },
    querySelectorAll() { return characters; },
  };
  const base = {
    program: { duration_ms: 1800, repeat_ms: 10000, repeat_count: 2 },
    keyframes: null,
  };
  try {
    animatePresentationElement(element, {
      ...base,
      characterHueWave: {
        base_color: "#ffc9d7",
        saturation: 0.78,
        brightness: 1,
        spatial_cycles: 1,
        temporal_cycles: 1,
      },
    }, "2026-01-01T00:00:00Z", Date.parse("2026-01-01T00:00:00Z"));
    scheduled();
    assert.equal(new Set(characters.map((character) => character.style.color)).size, 5);
    animation.currentTime = 2500;
    scheduled();
    assert.ok(characters.every((character) => character.style.color === "#ffc9d7"));

    animation.currentTime = 450;
    animatePresentationElement(element, {
      ...base,
      characterPaletteFront: {
        colors: ["#ffff5f", "#ffaf00", "#d70000", "#a9914a"],
        spread: 0.55,
        direction: "forward",
      },
    }, "2026-01-01T00:00:00Z", Date.parse("2026-01-01T00:00:00Z"));
    scheduled();
    assert.notEqual(characters[0].style.color, characters.at(-1).style.color);
    animation.currentTime = 3500;
    scheduled();
    assert.ok(characters.every((character) => character.style.color === "#a9914a"));
  } finally {
    globalThis.requestAnimationFrame = originalRequestAnimationFrame;
    globalThis.cancelAnimationFrame = originalCancelAnimationFrame;
  }
});

test("renders and restores portable style sweeps and terminal reveal", () => {
  const originalRequestAnimationFrame = globalThis.requestAnimationFrame;
  const originalCancelAnimationFrame = globalThis.cancelAnimationFrame;
  let scheduled = null;
  globalThis.requestAnimationFrame = (callback) => { scheduled = callback; return 1; };
  globalThis.cancelAnimationFrame = () => {};
  const characters = [..."Alice"].map((text) => ({
    dataset: { originalText: text, caseable: "true" }, style: {}, textContent: text,
  }));
  const animation = { currentTime: 500, addEventListener() {} };
  const element = { animate() { return animation; }, querySelectorAll() { return characters; } };
  const base = {
    program: { duration_ms: 1000, repeat_ms: 2000, repeat_count: 1, frames_per_second: 20 },
    keyframes: null,
  };
  try {
    animatePresentationElement(element, {
      ...base,
      characterStyleSweep: {
        positions: [{ at: 0, position: 0 }, { at: 1, position: 1 }],
        base_style: { foreground: "#111111" },
        active_style: { foreground: "#eeeeee", bold: true, reverse: true },
        width: 1,
        trail_mode: "symmetric",
        start_padding: 0,
        end_padding: 1,
        uppercase_active: true,
        lowercase_inactive: true,
        stepped: true,
      },
    }, "2026-01-01T00:00:00Z", Date.parse("2026-01-01T00:00:00Z"));
    scheduled();
    assert.ok(characters.some((character) => character.style.fontWeight === "700"));
    assert.ok(characters.some((character) => character.style.backgroundColor === "#eeeeee"));

    animation.currentTime = 100;
    animatePresentationElement(element, {
      ...base,
      characterReveal: {
        interval_ms: 100,
        base_color: "#d7d7d7",
        accent_color: "#ffffff",
        glitch_width: 2,
        settle_width: 1,
        seed: 7,
        glitch_characters: "#%",
        inline_glitch_chance: 0,
      },
    }, "2026-01-01T00:00:00Z", Date.parse("2026-01-01T00:00:00Z"));
    scheduled();
    assert.equal(characters[0].textContent, "A");
    assert.match(characters[0].dataset.visualText, /^[#%]$/);
    assert.equal(characters[1].textContent, "l");
    assert.equal(characters[1].dataset.visualText, " ");
  } finally {
    globalThis.requestAnimationFrame = originalRequestAnimationFrame;
    globalThis.cancelAnimationFrame = originalCancelAnimationFrame;
  }
});
