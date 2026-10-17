const COLOR = /^#[0-9a-f]{6}$/;
const CAPABILITIES = new Set([
  "foreground_color",
  "bold",
  "underline",
  "timeline",
  "character_foreground",
  "character_case",
  "character_style",
  "character_text",
]);
const MAX_PROGRAMS = 4;
const MAX_VARIANTS = 4;
const MAX_KEYFRAMES = 3;
const MAX_TARGET_CHARACTERS = 4096;
const MAX_SWEEP_GRAPHEMES = 100;
const MAX_TRAIL_WIDTH = 8;
const MAX_PALETTE_COLORS = 8;

function exactKeys(value, allowed) {
  return Object.keys(value).every((key) => allowed.has(key));
}

function validatedStyle(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!exactKeys(value, new Set(["foreground", "bold", "underline", "reverse"]))) return null;
  const style = {};
  if (value.foreground !== undefined) {
    if (typeof value.foreground !== "string" || !COLOR.test(value.foreground)) return null;
    style.foreground = value.foreground;
  }
  for (const key of ["bold", "underline", "reverse"]) {
    if (value[key] !== undefined) {
      if (value[key] !== true) return null;
      style[key] = true;
    }
  }
  return style;
}

function validatedVariant(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!exactKeys(value, new Set([
    "requires",
    "style",
    "foreground_keyframes",
    "character_sweep",
    "character_hue_wave",
    "character_palette_front",
    "character_style_sweep",
    "character_sparkle",
    "character_reveal",
  ]))) return null;
  if (
    !Array.isArray(value.requires) ||
    value.requires.length === 0 ||
    value.requires.length > CAPABILITIES.size ||
    new Set(value.requires).size !== value.requires.length ||
    !value.requires.every((item) => typeof item === "string" && CAPABILITIES.has(item))
  ) {
    return null;
  }
  const style = validatedStyle(value.style);
  if (!style || !Array.isArray(value.foreground_keyframes)) return null;
  const keyframes = value.foreground_keyframes.map((frame) => {
    if (
      !frame ||
      typeof frame !== "object" ||
      Array.isArray(frame) ||
      !exactKeys(frame, new Set(["at", "color"])) ||
      !Number.isFinite(frame.at) ||
      frame.at < 0 ||
      frame.at > 1 ||
      typeof frame.color !== "string" ||
      !COLOR.test(frame.color)
    ) {
      return null;
    }
    return { at: frame.at, color: frame.color };
  });
  if (keyframes.some((frame) => frame === null)) return null;
  let characterSweep = null;
  if (value.character_sweep !== undefined) {
    const sweep = value.character_sweep;
    const trailMode = sweep?.trail_mode ?? "symmetric";
    const startPadding = sweep?.start_padding ?? 0;
    const endPadding = sweep?.end_padding ?? 0;
    if (
      !sweep ||
      typeof sweep !== "object" ||
      Array.isArray(sweep) ||
      !exactKeys(
        sweep,
        new Set([
          "positions",
          "base_color",
          "head_color",
          "trail_width",
          "uppercase_head",
          "trail_mode",
          "start_padding",
          "end_padding",
          "fill_behind",
        ]),
      ) ||
      !Array.isArray(sweep.positions) ||
      sweep.positions.length < 2 ||
      sweep.positions.length > MAX_KEYFRAMES ||
      typeof sweep.base_color !== "string" ||
      !COLOR.test(sweep.base_color) ||
      typeof sweep.head_color !== "string" ||
      !COLOR.test(sweep.head_color) ||
      !Number.isFinite(sweep.trail_width) ||
      sweep.trail_width < 0.5 ||
      sweep.trail_width > MAX_TRAIL_WIDTH ||
      typeof sweep.uppercase_head !== "boolean" ||
      (sweep.fill_behind !== undefined && typeof sweep.fill_behind !== "boolean") ||
      !["symmetric", "behind", "ahead"].includes(trailMode) ||
      (sweep.fill_behind === true && trailMode !== "behind") ||
      !Number.isFinite(startPadding) ||
      startPadding < 0 ||
      startPadding > MAX_TRAIL_WIDTH ||
      !Number.isFinite(endPadding) ||
      endPadding < 0 ||
      endPadding > MAX_TRAIL_WIDTH
    ) {
      return null;
    }
    const positions = sweep.positions.map((frame) => {
      if (
        !frame ||
        typeof frame !== "object" ||
        Array.isArray(frame) ||
        !exactKeys(frame, new Set(["at", "position"])) ||
        !Number.isFinite(frame.at) ||
        frame.at < 0 ||
        frame.at > 1 ||
        !Number.isFinite(frame.position) ||
        frame.position < 0 ||
        frame.position > 1
      ) {
        return null;
      }
      return { at: frame.at, position: frame.position };
    });
    if (
      positions.some((frame) => frame === null) ||
      positions[0].at !== 0 ||
      positions.at(-1).at !== 1 ||
      positions.some((frame, index) => index > 0 && positions[index - 1].at >= frame.at) ||
      !value.requires.includes("character_foreground") ||
      !value.requires.includes("timeline") ||
      (sweep.uppercase_head && !value.requires.includes("character_case"))
    ) {
      return null;
    }
    characterSweep = {
      ...sweep,
      positions,
      trail_mode: trailMode,
      start_padding: startPadding,
      end_padding: endPadding,
      fill_behind: sweep.fill_behind ?? false,
    };
  }
  let characterHueWave = null;
  if (value.character_hue_wave !== undefined) {
    const wave = value.character_hue_wave;
    if (
      !wave ||
      typeof wave !== "object" ||
      Array.isArray(wave) ||
      !exactKeys(wave, new Set(["base_color", "saturation", "brightness", "spatial_cycles", "temporal_cycles"])) ||
      typeof wave.base_color !== "string" || !COLOR.test(wave.base_color) ||
      !Number.isFinite(wave.saturation) || wave.saturation < 0 || wave.saturation > 1 ||
      !Number.isFinite(wave.brightness) || wave.brightness < 0.1 || wave.brightness > 1 ||
      !Number.isFinite(wave.spatial_cycles) || wave.spatial_cycles < 0.1 || wave.spatial_cycles > 4 ||
      !Number.isFinite(wave.temporal_cycles) || wave.temporal_cycles < 0.1 || wave.temporal_cycles > 4
    ) return null;
    characterHueWave = { ...wave };
  }
  let characterPaletteFront = null;
  if (value.character_palette_front !== undefined) {
    const front = value.character_palette_front;
    if (
      !front ||
      typeof front !== "object" ||
      Array.isArray(front) ||
      !exactKeys(front, new Set(["colors", "spread", "direction"])) ||
      !Array.isArray(front.colors) ||
      front.colors.length < 2 ||
      front.colors.length > MAX_PALETTE_COLORS ||
      !front.colors.every((color) => typeof color === "string" && COLOR.test(color)) ||
      !Number.isFinite(front.spread) || front.spread < 0 || front.spread > 1 ||
      !["forward", "backward"].includes(front.direction)
    ) return null;
    characterPaletteFront = { ...front, colors: [...front.colors] };
  }
  let characterStyleSweep = null;
  if (value.character_style_sweep !== undefined) {
    const sweep = value.character_style_sweep;
    if (
      !sweep || typeof sweep !== "object" || Array.isArray(sweep) ||
      !exactKeys(sweep, new Set([
        "positions", "base_style", "active_style", "width", "trail_mode",
        "start_padding", "end_padding", "uppercase_active", "lowercase_inactive", "stepped",
      ])) ||
      !Array.isArray(sweep.positions) || sweep.positions.length < 2 || sweep.positions.length > MAX_KEYFRAMES ||
      !Number.isFinite(sweep.width) || sweep.width < 0.5 || sweep.width > MAX_TRAIL_WIDTH ||
      !["symmetric", "behind", "ahead"].includes(sweep.trail_mode) ||
      !Number.isFinite(sweep.start_padding) || sweep.start_padding < 0 || sweep.start_padding > MAX_TRAIL_WIDTH ||
      !Number.isFinite(sweep.end_padding) || sweep.end_padding < 0 || sweep.end_padding > MAX_TRAIL_WIDTH ||
      typeof sweep.uppercase_active !== "boolean" || typeof sweep.lowercase_inactive !== "boolean" ||
      typeof sweep.stepped !== "boolean"
    ) return null;
    const positions = sweep.positions.map((frame) =>
      frame && typeof frame === "object" && !Array.isArray(frame) &&
      exactKeys(frame, new Set(["at", "position"])) && Number.isFinite(frame.at) &&
      frame.at >= 0 && frame.at <= 1 && Number.isFinite(frame.position) &&
      frame.position >= 0 && frame.position <= 1
        ? { at: frame.at, position: frame.position } : null
    );
    const baseStyle = validatedStyle(sweep.base_style);
    const activeStyle = validatedStyle(sweep.active_style);
    if (!baseStyle || !activeStyle || positions.some((frame) => frame === null) ||
      positions[0].at !== 0 || positions.at(-1).at !== 1 ||
      positions.some((frame, index) => index > 0 && positions[index - 1].at >= frame.at) ||
      !value.requires.includes("character_style") || !value.requires.includes("timeline") ||
      ((baseStyle.foreground || activeStyle.foreground) && !value.requires.includes("character_foreground")) ||
      ((sweep.uppercase_active || sweep.lowercase_inactive) && !value.requires.includes("character_case"))) return null;
    characterStyleSweep = { ...sweep, positions, base_style: baseStyle, active_style: activeStyle };
  }
  let characterSparkle = null;
  if (value.character_sparkle !== undefined) {
    const sparkle = value.character_sparkle;
    if (!sparkle || typeof sparkle !== "object" || Array.isArray(sparkle) ||
      !exactKeys(sparkle, new Set(["base_color", "accent_color", "count", "seed"])) ||
      typeof sparkle.base_color !== "string" || !COLOR.test(sparkle.base_color) ||
      typeof sparkle.accent_color !== "string" || !COLOR.test(sparkle.accent_color) ||
      !Number.isInteger(sparkle.count) || sparkle.count < 1 || sparkle.count > 10 ||
      !Number.isInteger(sparkle.seed) || sparkle.seed < 0 || sparkle.seed > 0xffffffff ||
      !value.requires.includes("character_foreground") || !value.requires.includes("timeline")) return null;
    characterSparkle = { ...sparkle };
  }
  let characterReveal = null;
  if (value.character_reveal !== undefined) {
    const reveal = value.character_reveal;
    if (!reveal || typeof reveal !== "object" || Array.isArray(reveal) ||
      !exactKeys(reveal, new Set([
        "interval_ms", "base_color", "accent_color", "glitch_width", "settle_width",
        "seed", "glitch_characters", "inline_glitch_chance",
      ])) || !Number.isFinite(reveal.interval_ms) || reveal.interval_ms <= 0 ||
      typeof reveal.base_color !== "string" || !COLOR.test(reveal.base_color) ||
      typeof reveal.accent_color !== "string" || !COLOR.test(reveal.accent_color) ||
      !Number.isInteger(reveal.glitch_width) || reveal.glitch_width < 1 || reveal.glitch_width > 20 ||
      !Number.isInteger(reveal.settle_width) || reveal.settle_width < 0 || reveal.settle_width > 20 ||
      !Number.isInteger(reveal.seed) || reveal.seed < 0 || reveal.seed > 0xffffffff ||
      typeof reveal.glitch_characters !== "string" || reveal.glitch_characters.length < 1 ||
      reveal.glitch_characters.length > 256 || /[^\x21-\x7e]/.test(reveal.glitch_characters) ||
      !Number.isFinite(reveal.inline_glitch_chance) || reveal.inline_glitch_chance < 0 || reveal.inline_glitch_chance > 1 ||
      !value.requires.includes("character_foreground") || !value.requires.includes("character_text") ||
      !value.requires.includes("timeline")) return null;
    characterReveal = { ...reveal };
  }
  if (keyframes.length) {
    if (
      keyframes.length < 2 ||
      keyframes.length > MAX_KEYFRAMES ||
      keyframes[0].at !== 0 ||
      keyframes.at(-1).at !== 1 ||
      keyframes.some((frame, index) => index > 0 && keyframes[index - 1].at >= frame.at) ||
      !value.requires.includes("foreground_color") ||
      !value.requires.includes("timeline")
    ) {
      return null;
    }
    if (!style.foreground) return null;
  }
  const characterTracks = [
    characterSweep, characterHueWave, characterPaletteFront, characterStyleSweep,
    characterSparkle, characterReveal,
  ].filter(Boolean);
  if ((keyframes.length ? 1 : 0) + characterTracks.length > 1) return null;
  if (!Object.keys(style).length && !keyframes.length && !characterTracks.length) return null;
  const used = new Set();
  if (style.foreground) used.add("foreground_color");
  if (style.bold) used.add("bold");
  if (style.underline) used.add("underline");
  if (keyframes.length) used.add("timeline");
  if (characterSweep) {
    used.add("character_foreground");
    used.add("timeline");
    if (characterSweep.uppercase_head) used.add("character_case");
  }
  if (characterHueWave || characterPaletteFront) {
    used.add("character_foreground");
    used.add("timeline");
  }
  if (characterStyleSweep) {
    used.add("character_style");
    used.add("timeline");
    if (characterStyleSweep.base_style.foreground || characterStyleSweep.active_style.foreground) used.add("character_foreground");
    if (characterStyleSweep.uppercase_active || characterStyleSweep.lowercase_inactive) used.add("character_case");
  }
  if (characterSparkle) {
    used.add("character_foreground");
    used.add("timeline");
  }
  if (characterReveal) {
    used.add("character_foreground");
    used.add("character_text");
    used.add("timeline");
  }
  if (![...used].every((capability) => value.requires.includes(capability))) return null;
  return {
    requires: value.requires,
    style,
    foreground_keyframes: keyframes,
    character_sweep: characterSweep,
    character_hue_wave: characterHueWave,
    character_palette_front: characterPaletteFront,
    character_style_sweep: characterStyleSweep,
    character_sparkle: characterSparkle,
    character_reveal: characterReveal,
  };
}

function validatedProgram(value, textLength) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (
    !exactKeys(
      value,
      new Set([
        "start",
        "end",
        "duration_ms",
        "repeat_ms",
        "repeat_count",
        "frames_per_second",
        "variants",
        "reduced_motion",
        "fallback",
      ]),
    )
  ) {
    return null;
  }
  if (
    !Number.isInteger(value.start) ||
    !Number.isInteger(value.end) ||
    value.start < 0 ||
    value.end <= value.start ||
    value.end > textLength ||
    value.end - value.start > MAX_TARGET_CHARACTERS ||
    !Number.isInteger(value.duration_ms) ||
    value.duration_ms < 100 ||
    value.duration_ms > 30000 ||
    !Number.isInteger(value.repeat_ms) ||
    value.repeat_ms < value.duration_ms ||
    value.repeat_ms > 60000 ||
    !Number.isInteger(value.repeat_count) ||
    value.repeat_count < 1 ||
    value.repeat_count > 20 ||
    value.repeat_ms * value.repeat_count > 300000 ||
    !Number.isFinite(value.frames_per_second) ||
    value.frames_per_second < 1 ||
    value.frames_per_second > 30 ||
    !Array.isArray(value.variants) ||
    value.variants.length < 1 ||
    value.variants.length > MAX_VARIANTS
  ) {
    return null;
  }
  const variants = value.variants.map(validatedVariant);
  const reducedMotion = validatedStyle(value.reduced_motion);
  const fallback = validatedStyle(value.fallback);
  if (variants.some((variant) => variant === null) || !reducedMotion || !fallback) return null;
  return {
    start: value.start,
    end: value.end,
    duration_ms: value.duration_ms,
    repeat_ms: value.repeat_ms,
    repeat_count: value.repeat_count,
    frames_per_second: value.frames_per_second,
    variants,
    reduced_motion: reducedMotion,
    fallback,
  };
}

export function validatedPresentation(event) {
  const value = event?.presentation;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!exactKeys(value, new Set(["version", "programs"])) || ![1, 2].includes(value.version)) return null;
  if (!Array.isArray(value.programs) || value.programs.length < 1 || value.programs.length > MAX_PROGRAMS) {
    return null;
  }
  const textLength = Array.from(event.text || "").length;
  const programs = value.programs.map((program) => validatedProgram(program, textLength));
  if (programs.some((program) => program === null)) return null;
  if (value.version === 1 && programs.some((program) =>
    program.duration_ms > 3000 || program.variants.some((variant) =>
      variant.style.reverse || variant.character_style_sweep || variant.character_sparkle ||
      variant.character_reveal || variant.character_sweep?.fill_behind
    )
  )) return null;
  const ordered = [...programs].sort((left, right) => left.start - right.start);
  const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
  const boundaries = new Set([0]);
  let offset = 0;
  for (const segment of segmenter.segment(event.text || "")) {
    offset += Array.from(segment.segment).length;
    boundaries.add(offset);
  }
  if (ordered.some((program) => !boundaries.has(program.start) || !boundaries.has(program.end))) {
    return null;
  }
  if (
    ordered.some((program) => {
      if (!program.variants.some((variant) =>
        variant.character_sweep || variant.character_hue_wave || variant.character_palette_front ||
        variant.character_style_sweep || variant.character_sparkle
      )) return false;
      const target = Array.from(event.text || "").slice(program.start, program.end).join("");
      return [...segmenter.segment(target)].length > MAX_SWEEP_GRAPHEMES;
    })
  ) {
    return null;
  }
  return ordered;
}

export function presentationValidationResult(event) {
  if (event?.presentation === undefined) {
    return { value: null, error: { code: "presentation_missing", path: "presentation" } };
  }
  const value = validatedPresentation(event);
  if (value) return { value, error: null };
  const variants = Array.isArray(event?.presentation?.programs)
    ? event.presentation.programs.flatMap((program) =>
        Array.isArray(program?.variants) ? program.variants : []
      )
    : [];
  if (variants.some((variant) => variant?.character_palette_front !== undefined)) {
    return {
      value: null,
      error: {
        code: "palette_front_rejected",
        path: "presentation.programs[].variants[].character_palette_front",
      },
    };
  }
  if (variants.some((variant) => variant?.character_hue_wave !== undefined)) {
    return {
      value: null,
      error: {
        code: "hue_wave_rejected",
        path: "presentation.programs[].variants[].character_hue_wave",
      },
    };
  }
  return {
    value: null,
    error: { code: "presentation_schema_rejected", path: "presentation.programs" },
  };
}

export function selectPresentation(program, animationsEnabled, animationActive = true) {
  if (!animationActive) return { style: program.fallback, animation: null };
  if (!animationsEnabled) return { style: program.reduced_motion, animation: null };
  const variant = program.variants.find((candidate) =>
    candidate.requires.every((capability) => CAPABILITIES.has(capability)),
  );
  if (!variant) return { style: program.fallback, animation: null };
  return {
    style: variant.character_sweep
      ? { foreground: variant.character_sweep.base_color }
      : variant.character_hue_wave
        ? { foreground: variant.character_hue_wave.base_color }
      : variant.character_palette_front
        ? { foreground: variant.character_palette_front.colors.at(-1) }
      : variant.character_style_sweep
        ? variant.character_style_sweep.base_style
      : variant.character_sparkle
        ? { foreground: variant.character_sparkle.base_color }
      : variant.character_reveal
        ? {}
      : variant.style,
    animation: variant.foreground_keyframes.length ? variant.foreground_keyframes : null,
    ...(variant.character_sweep ? { characterSweep: variant.character_sweep } : {}),
    ...(variant.character_hue_wave ? { characterHueWave: variant.character_hue_wave } : {}),
    ...(variant.character_palette_front ? { characterPaletteFront: variant.character_palette_front } : {}),
    ...(variant.character_style_sweep ? { characterStyleSweep: variant.character_style_sweep } : {}),
    ...(variant.character_sparkle ? { characterSparkle: variant.character_sparkle } : {}),
    ...(variant.character_reveal ? { characterReveal: variant.character_reveal } : {}),
  };
}

function srgbToLinear(channel) {
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
}

function linearToSrgb(channel) {
  return channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055;
}

function colorToOklab(color) {
  const channels = [1, 3, 5].map((index) => srgbToLinear(Number.parseInt(color.slice(index, index + 2), 16) / 255));
  const light = 0.4122214708 * channels[0] + 0.5363325363 * channels[1] + 0.0514459929 * channels[2];
  const medium = 0.2119034982 * channels[0] + 0.6806995451 * channels[1] + 0.1073969566 * channels[2];
  const short = 0.0883024619 * channels[0] + 0.2817188376 * channels[1] + 0.6299787005 * channels[2];
  const roots = [Math.cbrt(light), Math.cbrt(medium), Math.cbrt(short)];
  return [
    0.2104542553 * roots[0] + 0.793617785 * roots[1] - 0.0040720468 * roots[2],
    1.9779984951 * roots[0] - 2.428592205 * roots[1] + 0.4505937099 * roots[2],
    0.0259040371 * roots[0] + 0.7827717662 * roots[1] - 0.808675766 * roots[2],
  ];
}

function oklabToLinearRgb(lightness, greenRed, blueYellow) {
  const lightRoot = lightness + 0.3963377774 * greenRed + 0.2158037573 * blueYellow;
  const mediumRoot = lightness - 0.1055613458 * greenRed - 0.0638541728 * blueYellow;
  const shortRoot = lightness - 0.0894841775 * greenRed - 1.291485548 * blueYellow;
  const [light, medium, short] = [lightRoot ** 3, mediumRoot ** 3, shortRoot ** 3];
  return [
    4.0767416621 * light - 3.3077115913 * medium + 0.2309699292 * short,
    -1.2684380046 * light + 2.6097574011 * medium - 0.3413193965 * short,
    -0.0041960863 * light - 0.7034186147 * medium + 1.707614701 * short,
  ];
}

function oklabToColor(lightness, greenRed, blueYellow) {
  let channels = oklabToLinearRgb(lightness, greenRed, blueYellow);
  if (channels.some((channel) => channel < 0 || channel > 1)) {
    let lower = 0;
    let upper = 1;
    for (let index = 0; index < 16; index += 1) {
      const scale = (lower + upper) / 2;
      const candidate = oklabToLinearRgb(lightness, greenRed * scale, blueYellow * scale);
      if (candidate.every((channel) => channel >= 0 && channel <= 1)) {
        lower = scale;
        channels = candidate;
      } else {
        upper = scale;
      }
    }
  }
  return `#${channels
    .map((channel) => Math.round(255 * Math.min(1, Math.max(0, linearToSrgb(channel)))).toString(16).padStart(2, "0"))
    .join("")}`;
}

export function interpolatePresentationColor(start, end, progress) {
  const bounded = Math.min(1, Math.max(0, progress));
  const source = colorToOklab(start);
  const target = colorToOklab(end);
  return oklabToColor(
    source[0] + (target[0] - source[0]) * bounded,
    source[1] + (target[1] - source[1]) * bounded,
    source[2] + (target[2] - source[2]) * bounded,
  );
}

function timelineColor(keyframes, progress) {
  let before = keyframes[0];
  let after = keyframes.at(-1);
  for (const candidate of keyframes.slice(1)) {
    after = candidate;
    if (progress <= candidate.at) break;
    before = candidate;
  }
  const width = after.at - before.at;
  const local = width === 0 ? 1 : (progress - before.at) / width;
  return interpolatePresentationColor(before.color, after.color, local);
}

function timelineValue(keyframes, progress, property) {
  let before = keyframes[0];
  let after = keyframes.at(-1);
  for (const candidate of keyframes.slice(1)) {
    after = candidate;
    if (progress <= candidate.at) break;
    before = candidate;
  }
  const width = after.at - before.at;
  const local = width === 0 ? 1 : (progress - before.at) / width;
  return before[property] + (after[property] - before[property]) * local;
}

export function presentationAnimationFrames(program, keyframes, baseColor) {
  const count = Math.ceil((program.duration_ms / 1000) * program.frames_per_second);
  const activeRatio = program.duration_ms / program.repeat_ms;
  const frames = Array.from({ length: count + 1 }, (_, index) => {
    const progress = index / count;
    return { color: timelineColor(keyframes, progress), offset: progress * activeRatio };
  });
  if (activeRatio < 1) {
    frames.push({ color: baseColor, offset: Math.min(1, activeRatio + 0.0001) });
    frames.push({ color: baseColor, offset: 1 });
  }
  return frames;
}

export function splitPresentationRuns(event, runs, animationsEnabled, now = null) {
  const programs = validatedPresentation(event);
  const baseRuns = runs || [{ text: event.text || "", style: {}, role: undefined }];
  if (!programs) return baseRuns.map((run) => ({ ...run, presentation: null, presentations: [] }));
  const characters = Array.from(event.text || "");
  const boundaries = new Set([0, characters.length]);
  const normalizedRuns = [];
  let offset = 0;
  for (const run of baseRuns) {
    const length = Array.from(run.text).length;
    normalizedRuns.push({ ...run, start: offset, end: offset + length });
    boundaries.add(offset);
    boundaries.add(offset + length);
    offset += length;
  }
  for (const program of programs) {
    boundaries.add(program.start);
    boundaries.add(program.end);
    const age = now === null ? 0 : Math.max(0, now - Date.parse(event.timestamp));
    const animationActive = Number.isFinite(age) && age < program.repeat_ms * program.repeat_count;
    const selected = selectPresentation(program, animationsEnabled, animationActive);
    if (selected.characterSweep || selected.characterHueWave || selected.characterPaletteFront ||
      selected.characterStyleSweep || selected.characterSparkle || selected.characterReveal) {
      let graphemeOffset = program.start;
      const target = characters.slice(program.start, program.end).join("");
      for (const segment of new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(target)) {
        graphemeOffset += Array.from(segment.segment).length;
        boundaries.add(graphemeOffset);
      }
    }
  }
  const ordered = [...boundaries].sort((left, right) => left - right);
  return ordered.slice(0, -1).map((start, index) => {
    const end = ordered[index + 1];
    const run = normalizedRuns.find((candidate) => candidate.start <= start && end <= candidate.end);
    const presentations = programs
      .filter((candidate) => candidate.start <= start && end <= candidate.end)
      .map((program) => {
        const age = now === null ? 0 : Math.max(0, now - Date.parse(event.timestamp));
        const animationActive = Number.isFinite(age) && age < program.repeat_ms * program.repeat_count;
        const selected = selectPresentation(program, animationsEnabled, animationActive);
        const hasCharacterTrack = selected.characterSweep || selected.characterHueWave ||
          selected.characterPaletteFront || selected.characterStyleSweep ||
          selected.characterSparkle || selected.characterReveal;
        const characterSegments = hasCharacterTrack
          ? [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(
              characters.slice(program.start, program.end).join(""),
            )]
          : [];
        let characterIndex = null;
        if (characterSegments.length) {
          let characterOffset = program.start;
          characterIndex = characterSegments.findIndex((segment) => {
            const next = characterOffset + Array.from(segment.segment).length;
            const matches = characterOffset === start && next === end;
            characterOffset = next;
            return matches;
          });
        }
        return {
          program,
          keyframes: selected.animation,
          baseColor: selected.style.foreground,
          style: selected.style,
          ...(selected.characterSweep
            ? { characterSweep: selected.characterSweep, characterIndex, characterCount: characterSegments.length }
            : {}),
          ...(selected.characterHueWave
            ? { characterHueWave: selected.characterHueWave, characterIndex, characterCount: characterSegments.length }
            : {}),
          ...(selected.characterPaletteFront
            ? { characterPaletteFront: selected.characterPaletteFront, characterIndex, characterCount: characterSegments.length }
            : {}),
          ...(selected.characterStyleSweep
            ? { characterStyleSweep: selected.characterStyleSweep, characterIndex, characterCount: characterSegments.length }
            : {}),
          ...(selected.characterSparkle
            ? { characterSparkle: selected.characterSparkle, characterIndex, characterCount: characterSegments.length }
            : {}),
          ...(selected.characterReveal
            ? { characterReveal: selected.characterReveal, characterIndex, characterCount: characterSegments.length }
            : {}),
        };
      });
    return {
      text: characters.slice(start, end).join(""),
      style: { ...(run?.style || {}) },
      role: run?.role,
      presentation: presentations[0] || null,
      presentations,
    };
  });
}

function hueWaveColor(wave, index, count, progress) {
  const hue = ((index / Math.max(1, count)) * wave.spatial_cycles + progress * wave.temporal_cycles) % 1;
  const section = hue * 6;
  const chroma = wave.brightness * wave.saturation;
  const intermediate = chroma * (1 - Math.abs((section % 2) - 1));
  const [red, green, blue] = section < 1 ? [chroma, intermediate, 0]
    : section < 2 ? [intermediate, chroma, 0]
      : section < 3 ? [0, chroma, intermediate]
        : section < 4 ? [0, intermediate, chroma]
          : section < 5 ? [intermediate, 0, chroma]
            : [chroma, 0, intermediate];
  const match = wave.brightness - chroma;
  return `#${[red, green, blue].map((channel) =>
    Math.round((channel + match) * 255).toString(16).padStart(2, "0")
  ).join("")}`;
}

function paletteFrontColor(front, index, count, progress) {
  let normalized = index / Math.max(1, count - 1);
  if (front.direction === "backward") normalized = 1 - normalized;
  const local = progress * (1 + front.spread) - normalized * front.spread;
  if (local < 0 || local >= 1) return front.colors.at(-1);
  const scaled = local * (front.colors.length - 1);
  const section = Math.min(front.colors.length - 2, Math.floor(scaled));
  return interpolatePresentationColor(front.colors[section], front.colors[section + 1], scaled - section);
}

function effectSeedValue(seed, frame, index) {
  let value = (seed ^ Math.imul(frame, 0x9e3779b1) ^ Math.imul(index, 0x85ebca77)) >>> 0;
  value ^= value >>> 16;
  value = Math.imul(value, 0x7feb352d) >>> 0;
  value ^= value >>> 15;
  return value >>> 0;
}

function styleSweepSelected(track, index, count, progress) {
  let head = -(track.start_padding) + timelineValue(track.positions, progress, "position") * (
    Math.max(0, count - 1) + track.start_padding + track.end_padding
  );
  if (track.stepped) head = Math.floor(head + 1e-9);
  const delta = index - head;
  const distance = track.trail_mode === "symmetric" ? Math.abs(delta)
    : track.trail_mode === "behind" && delta <= 0 ? -delta
      : track.trail_mode === "ahead" && delta >= 0 ? delta
        : Infinity;
  return distance < track.width;
}

function applyCharacterStyle(character, style = {}) {
  character.style.color = style.reverse ? "var(--ink)" : style.foreground || "";
  character.style.backgroundColor = style.reverse
    ? style.foreground || "var(--text)"
    : "";
  character.style.fontWeight = style.bold ? "700" : "";
  character.style.textDecoration = style.underline ? "underline" : "";
}

function setVisualCharacter(character, text, color = "currentColor") {
  character.dataset.visualText = text;
  character.style.setProperty?.("--presentation-visual-color", color);
}

export function animatePresentationElement(element, presentation, eventTimestamp, now = Date.now()) {
  const characterTrack = presentation?.characterSweep || presentation?.characterHueWave ||
    presentation?.characterPaletteFront || presentation?.characterStyleSweep ||
    presentation?.characterSparkle || presentation?.characterReveal;
  if ((!presentation?.keyframes && !characterTrack) || typeof element?.animate !== "function") return null;
  const age = Math.max(0, now - Date.parse(eventTimestamp));
  const total = presentation.program.repeat_ms * presentation.program.repeat_count;
  if (!Number.isFinite(age) || age >= total) return null;
  const animation = element.animate(
    presentation.keyframes
      ? presentationAnimationFrames(
          presentation.program,
          presentation.keyframes,
          presentation.baseColor,
        )
      : [{ opacity: 1 }, { opacity: 1 }],
    {
      duration: presentation.program.repeat_ms,
      iterations: presentation.program.repeat_count,
      delay: -age,
      fill: "none",
    },
  );
  if (characterTrack) {
    let frame = null;
    const render = () => {
      const current = Number(animation.currentTime);
      if (!Number.isFinite(current)) return;
      const phase = current % presentation.program.repeat_ms;
      const active = phase < presentation.program.duration_ms;
      const progress = active ? phase / presentation.program.duration_ms : 0;
      const characters = [...element.querySelectorAll(".presentation-character")];
      const sweep = presentation.characterSweep;
      const position = active && sweep ? timelineValue(sweep.positions, progress, "position") : 0;
      const rawHead = sweep
        ? -(sweep.start_padding ?? 0) + position * (
          Math.max(0, characters.length - 1)
          + (sweep.start_padding ?? 0)
          + (sweep.end_padding ?? 0)
        )
        : 0;
      const head = sweep?.uppercase_head ? Math.round(rawHead) : rawHead;
      for (const [index, character] of characters.entries()) {
        const original = character.dataset.originalText ?? character.textContent;
        character.textContent = original;
        delete character.dataset.visualText;
        character.style.removeProperty?.("--presentation-visual-color");
        character.style.fontWeight = "";
        character.style.textDecoration = "";
        character.style.backgroundColor = "";
        if (active && sweep) {
          const delta = index - head;
          const trailMode = sweep.trail_mode ?? "symmetric";
          const distance = trailMode === "symmetric" ? Math.abs(delta)
            : trailMode === "behind" && delta <= 0 ? -delta
              : trailMode === "ahead" && delta >= 0 ? delta
                : Infinity;
          let intensity = sweep.fill_behind && Number.isFinite(distance)
            ? Math.min(1, distance / sweep.trail_width)
            : Math.max(0, 1 - distance / sweep.trail_width);
          intensity = intensity * intensity * (3 - 2 * intensity);
          character.style.color = interpolatePresentationColor(sweep.base_color, sweep.head_color, intensity);
        } else if (active && presentation.characterHueWave) {
          character.style.color = hueWaveColor(presentation.characterHueWave, index, characters.length, progress);
        } else if (active && presentation.characterPaletteFront) {
          character.style.color = paletteFrontColor(presentation.characterPaletteFront, index, characters.length, progress);
        } else if (active && presentation.characterStyleSweep) {
          const track = presentation.characterStyleSweep;
          const selected = styleSweepSelected(track, index, characters.length, progress);
          applyCharacterStyle(character, selected ? track.active_style : track.base_style);
          if (character.dataset.caseable === "true") {
            character.style.textTransform = selected && track.uppercase_active ? "uppercase"
              : !selected && track.lowercase_inactive ? "lowercase" : "none";
          }
        } else if (active && presentation.characterSparkle) {
          const sparkle = presentation.characterSparkle;
          const phaseSeconds = progress * presentation.program.duration_ms / 1000;
          const flashRate = presentation.program.frames_per_second / 2;
          const sparkleFrame = Math.floor(phaseSeconds * flashRate);
          const selected = [...characters.keys()]
            .sort((left, right) => effectSeedValue(sparkle.seed, sparkleFrame, left) - effectSeedValue(sparkle.seed, sparkleFrame, right))
            .slice(0, Math.min(sparkle.count, characters.length));
          const flashProgress = (phaseSeconds * flashRate) % 1;
          const intensity = selected.includes(index) ? 0.25 + 0.75 * Math.sin(Math.PI * flashProgress) : 0;
          character.style.color = interpolatePresentationColor(sparkle.base_color, sparkle.accent_color, intensity);
        } else if (active && presentation.characterReveal) {
          const reveal = presentation.characterReveal;
          const phaseMs = progress * presentation.program.duration_ms;
          const revealFrame = Math.floor((phaseMs / 1000 + 1e-12) * presentation.program.frames_per_second);
          const frontier = Math.floor(phaseMs / reveal.interval_ms);
          const revealed = Math.min(characters.length, frontier);
          if (index >= revealed) {
            setVisualCharacter(character, " ");
          } else {
            const trailPosition = frontier - 1 - index;
            if (trailPosition < reveal.glitch_width) {
              setVisualCharacter(
                character,
                reveal.glitch_characters[
                  effectSeedValue(reveal.seed, revealFrame, index) % reveal.glitch_characters.length
                ],
                reveal.accent_color,
              );
            } else if (trailPosition < reveal.glitch_width + reveal.settle_width) {
              const settlePosition = trailPosition - reveal.glitch_width;
              const settleProgress = reveal.settle_width <= 1 ? 1 : settlePosition / (reveal.settle_width - 1);
              setVisualCharacter(
                character,
                original,
                interpolatePresentationColor(reveal.accent_color, reveal.base_color, settleProgress),
              );
            } else {
              const fillDuration = characters.length * reveal.interval_ms;
              const selection = effectSeedValue(reveal.seed ^ 0xa5a5a5a5, 0, index) / 0x100000000;
              const stableAt = (index + 1 + reveal.glitch_width + reveal.settle_width) * reveal.interval_ms;
              const pulseDuration = (1 + reveal.settle_width) * reveal.interval_ms;
              const latestStart = fillDuration - pulseDuration;
              if (reveal.inline_glitch_chance > 0 && selection < reveal.inline_glitch_chance && latestStart > stableAt) {
                const position = effectSeedValue(reveal.seed ^ 0x5a5a5a5a, 0, index) / 0x100000000;
                const pulsePhase = phaseMs - (stableAt + position * (latestStart - stableAt));
                if (pulsePhase >= 0 && pulsePhase < pulseDuration) {
                  if (pulsePhase < reveal.interval_ms) {
                    character.style.fontWeight = "700";
                    setVisualCharacter(
                      character,
                      reveal.glitch_characters[
                        effectSeedValue(reveal.seed, revealFrame, index) % reveal.glitch_characters.length
                      ],
                      reveal.accent_color,
                    );
                  } else {
                    const settle = (pulsePhase - reveal.interval_ms) /
                      Math.max(reveal.interval_ms, reveal.settle_width * reveal.interval_ms);
                    setVisualCharacter(
                      character,
                      original,
                      interpolatePresentationColor(reveal.accent_color, reveal.base_color, settle),
                    );
                  }
                }
              }
            }
          }
        } else {
          if (!presentation.characterReveal) {
            character.style.color = sweep?.base_color
              || presentation.characterHueWave?.base_color
              || presentation.characterPaletteFront?.colors.at(-1)
              || presentation.characterStyleSweep?.base_style.foreground
              || presentation.characterSparkle?.base_color
              || "";
          }
        }
        if (!(active && presentation.characterStyleSweep)) {
          character.style.textTransform =
            active && sweep?.uppercase_head && character.dataset.caseable === "true"
              ? index === head
                ? "uppercase"
                : "lowercase"
              : "none";
        }
      }
      frame = requestAnimationFrame(render);
    };
    const stop = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      for (const character of element.querySelectorAll(".presentation-character")) {
        character.textContent = character.dataset.originalText ?? character.textContent;
        delete character.dataset.visualText;
        character.style.removeProperty?.("--presentation-visual-color");
        if (!presentation.characterReveal) {
          character.style.color = presentation.characterSweep?.base_color
            || presentation.characterHueWave?.base_color
            || presentation.characterPaletteFront?.colors.at(-1)
            || presentation.characterStyleSweep?.base_style.foreground
            || presentation.characterSparkle?.base_color
            || "";
        }
        character.style.textTransform = "none";
        character.style.fontWeight = "";
        character.style.textDecoration = "";
        character.style.backgroundColor = "";
      }
    };
    animation.addEventListener("finish", stop);
    animation.addEventListener("cancel", stop);
    frame = requestAnimationFrame(render);
  }
  return animation;
}
