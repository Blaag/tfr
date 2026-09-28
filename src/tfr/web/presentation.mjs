const COLOR = /^#[0-9a-f]{6}$/;
const CAPABILITIES = new Set(["foreground_color", "bold", "underline", "timeline"]);
const MAX_PROGRAMS = 1;
const MAX_VARIANTS = 4;
const MAX_KEYFRAMES = 3;
const MAX_TARGET_CHARACTERS = 2048;

function exactKeys(value, allowed) {
  return Object.keys(value).every((key) => allowed.has(key));
}

function validatedStyle(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!exactKeys(value, new Set(["foreground", "bold", "underline"]))) return null;
  const style = {};
  if (value.foreground !== undefined) {
    if (typeof value.foreground !== "string" || !COLOR.test(value.foreground)) return null;
    style.foreground = value.foreground;
  }
  for (const key of ["bold", "underline"]) {
    if (value[key] !== undefined) {
      if (value[key] !== true) return null;
      style[key] = true;
    }
  }
  return style;
}

function validatedVariant(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (!exactKeys(value, new Set(["requires", "style", "foreground_keyframes"]))) return null;
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
  if (!Object.keys(style).length && !keyframes.length) return null;
  const used = new Set();
  if (style.foreground) used.add("foreground_color");
  if (style.bold) used.add("bold");
  if (style.underline) used.add("underline");
  if (keyframes.length) used.add("timeline");
  if (![...used].every((capability) => value.requires.includes(capability))) return null;
  return { requires: value.requires, style, foreground_keyframes: keyframes };
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
    value.duration_ms < 1000 ||
    value.duration_ms > 3000 ||
    !Number.isInteger(value.repeat_ms) ||
    value.repeat_ms < value.duration_ms ||
    value.repeat_ms > 60000 ||
    !Number.isInteger(value.repeat_count) ||
    value.repeat_count < 1 ||
    value.repeat_count > 20 ||
    value.repeat_ms * value.repeat_count > 300000 ||
    !Number.isFinite(value.frames_per_second) ||
    value.frames_per_second < 1 ||
    value.frames_per_second > 20 ||
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
  if (!exactKeys(value, new Set(["version", "programs"])) || value.version !== 1) return null;
  if (!Array.isArray(value.programs) || value.programs.length < 1 || value.programs.length > MAX_PROGRAMS) {
    return null;
  }
  const textLength = Array.from(event.text || "").length;
  const programs = value.programs.map((program) => validatedProgram(program, textLength));
  if (programs.some((program) => program === null)) return null;
  const ordered = [...programs].sort((left, right) => left.start - right.start);
  if (ordered.some((program, index) => index > 0 && ordered[index - 1].end > program.start)) {
    return null;
  }
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
  return ordered;
}

export function selectPresentation(program, animationsEnabled) {
  if (!animationsEnabled) return { style: program.reduced_motion, animation: null };
  const variant = program.variants.find((candidate) =>
    candidate.requires.every((capability) => CAPABILITIES.has(capability)),
  );
  if (!variant) return { style: program.fallback, animation: null };
  return {
    style: variant.style,
    animation: variant.foreground_keyframes.length ? variant.foreground_keyframes : null,
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

export function splitPresentationRuns(event, runs, animationsEnabled) {
  const programs = validatedPresentation(event);
  const baseRuns = runs || [{ text: event.text || "", style: {}, role: undefined }];
  if (!programs) return baseRuns.map((run) => ({ ...run, presentation: null }));
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
  }
  const ordered = [...boundaries].sort((left, right) => left - right);
  return ordered.slice(0, -1).map((start, index) => {
    const end = ordered[index + 1];
    const run = normalizedRuns.find((candidate) => candidate.start <= start && end <= candidate.end);
    const program = programs.find((candidate) => candidate.start <= start && end <= candidate.end);
    const selected = program ? selectPresentation(program, animationsEnabled) : null;
    return {
      text: characters.slice(start, end).join(""),
      style: { ...(run?.style || {}) },
      role: run?.role,
      presentation:
        program && selected
          ? {
              program,
              keyframes: selected.animation,
              baseColor: selected.style.foreground,
              style: selected.style,
            }
          : null,
    };
  });
}

export function animatePresentationElement(element, presentation, eventTimestamp, now = Date.now()) {
  if (!presentation?.keyframes || typeof element?.animate !== "function") return null;
  const age = Math.max(0, now - Date.parse(eventTimestamp));
  const total = presentation.program.repeat_ms * presentation.program.repeat_count;
  if (!Number.isFinite(age) || age >= total) return null;
  return element.animate(
    presentationAnimationFrames(
      presentation.program,
      presentation.keyframes,
      presentation.baseColor,
    ),
    {
      duration: presentation.program.repeat_ms,
      iterations: presentation.program.repeat_count,
      delay: -age,
      fill: "none",
    },
  );
}
