const DEFAULT_EFFECTS = Object.freeze([
  { name: "water_ripple", duration_ms: 2600, frames_per_second: 24 },
  { name: "doom_fire", duration_ms: 3200, frames_per_second: 24 },
  { name: "acid_rain", duration_ms: 3000, frames_per_second: 24 },
  { name: "film_burn", duration_ms: 4200, frames_per_second: 24 },
  { name: "flame", duration_ms: 2100, frames_per_second: 24 },
  { name: "sandstorm", duration_ms: 2800, frames_per_second: 24 },
  { name: "vortex", duration_ms: 3000, frames_per_second: 24 },
  { name: "water", duration_ms: 5000, frames_per_second: 30 },
]);

const SUPPORTED_EFFECTS = new Set(DEFAULT_EFFECTS.map((effect) => effect.name));

const FIRE_STAGES = [
  [0.12, "#5f0000", "."],
  [0.27, "#870000", ":"],
  [0.43, "#d70000", "*"],
  [0.62, "#ff5f00", "o"],
  [0.78, "#ffaf00", "O"],
  [0.92, "#ffd700", "#"],
  [1, "#ffffd7", "@"],
];

const MAX_CAPTURED_CELLS = 5000;
const RESTORE_DISTANCE = 72;
const RESTORE_DURATION_MS = 700;

function boundedNumber(value, fallback, minimum, maximum) {
  return Number.isFinite(value) ? Math.min(maximum, Math.max(minimum, value)) : fallback;
}

function validatedEffectOptions(name, value) {
  const options = value && typeof value === "object" ? value : {};
  if (name === "acid_rain") {
    return {
      density: boundedNumber(options.density, 0.32, 0, 1),
      streak_length: Math.round(boundedNumber(options.streak_length, 4, 1, 12)),
    };
  }
  if (name === "sandstorm") {
    return {
      direction: options.direction === "right-to-left" ? "right-to-left" : "left-to-right",
      gust_strength: boundedNumber(options.gust_strength, 1.4, 0, 4),
    };
  }
  if (name === "vortex") {
    return {
      direction: options.direction === "counterclockwise" ? "counterclockwise" : "clockwise",
      turns: boundedNumber(options.turns, 2.25, 0.1, 10),
    };
  }
  if (name === "water") {
    return {
      color_mode: options.color_mode === "dynamic" ? "dynamic" : "source",
      slosh_strength: boundedNumber(options.slosh_strength, 1, 0, 1),
    };
  }
  if (name === "film_burn") {
    return {
      minimum_holes: Math.round(boundedNumber(options.minimum_holes, 3, 1, 32)),
      maximum_holes: Math.round(boundedNumber(options.maximum_holes, 7, 1, 32)),
    };
  }
  return {};
}

export function validatedScreenClearConfig(value) {
  const mode = ["cycle", "random", "locked"].includes(value?.mode) ? value.mode : "cycle";
  const effects = Array.isArray(value?.effects)
    ? value.effects.flatMap((effect) => {
        if (!effect || !SUPPORTED_EFFECTS.has(effect.name)) return [];
        return [{
          name: effect.name,
          duration_ms: boundedNumber(effect.duration_ms, 2600, 100, 60000),
          frames_per_second: boundedNumber(effect.frames_per_second, 24, 1, 30),
          options: validatedEffectOptions(effect.name, effect.options),
        }];
      })
    : [];
  const available = Array.isArray(value?.effects)
    ? effects
    : DEFAULT_EFFECTS.map((effect) => ({
        ...effect,
        options: validatedEffectOptions(effect.name, null),
      }));
  const locked = available.some((effect) => effect.name === value?.effect) ? value.effect : null;
  return { mode: mode === "locked" && !locked ? "cycle" : mode, effect: locked, effects: available };
}

export function selectScreenClearEffect(config, previous = null, random = Math.random) {
  const validated = validatedScreenClearConfig(config);
  if (!validated.effects.length) return null;
  if (validated.mode === "locked") {
    return validated.effects.find((effect) => effect.name === validated.effect) || validated.effects[0];
  }
  if (validated.mode === "random") {
    const candidates = validated.effects.length > 1
      ? validated.effects.filter((effect) => effect.name !== previous)
      : validated.effects;
    return candidates[Math.min(candidates.length - 1, Math.floor(random() * candidates.length))];
  }
  const previousIndex = validated.effects.findIndex((effect) => effect.name === previous);
  return validated.effects[(previousIndex + 1) % validated.effects.length];
}

export function eventsAfterClearCutoff(events, cutoffId) {
  if (!cutoffId) return events;
  const cutoff = events.findIndex((event) => event.id === cutoffId);
  return cutoff < 0 ? events : events.slice(cutoff + 1);
}

export function isRestoreSwipe(start, end, atTop) {
  if (!atTop || !start || !end || end.time - start.time > RESTORE_DURATION_MS) return false;
  const horizontal = end.x - start.x;
  const vertical = end.y - start.y;
  return vertical >= RESTORE_DISTANCE && vertical > Math.abs(horizontal) * 1.25;
}

function hash(seed, row, column, channel = 0) {
  let value = (seed * 0x9e3779b1 + row * 0x85ebca77 + column * 0xc2b2ae3d + channel * 0x27d4eb2f) >>> 0;
  value ^= value >>> 16;
  value = Math.imul(value, 0x7feb352d) >>> 0;
  value ^= value >>> 15;
  return (value >>> 0) / 0xffffffff;
}

function graphemes(text) {
  if (typeof Intl?.Segmenter === "function") {
    return [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(text)]
      .map(({ segment, index }) => ({ segment, index }));
  }
  let index = 0;
  return Array.from(text, (segment) => {
    const item = { segment, index };
    index += segment.length;
    return item;
  });
}

export function captureTranscriptCells(root, viewport) {
  if (!root || !viewport || typeof document === "undefined") return [];
  const viewportRect = viewport.getBoundingClientRect();
  const cells = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node && cells.length < MAX_CAPTURED_CELLS) {
    const parent = node.parentElement;
    if (parent && node.nodeValue) {
      const style = getComputedStyle(parent);
      for (const { segment, index } of graphemes(node.nodeValue)) {
        if (cells.length >= MAX_CAPTURED_CELLS) break;
        if (/^\s+$/u.test(segment)) continue;
        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + segment.length);
        const rect = range.getBoundingClientRect();
        if (
          rect.width <= 0 || rect.height <= 0 || rect.bottom <= viewportRect.top ||
          rect.top >= viewportRect.bottom || rect.right <= viewportRect.left ||
          rect.left >= viewportRect.right
        ) continue;
        cells.push({
          text: segment,
          x: rect.left - viewportRect.left,
          y: rect.top - viewportRect.top,
          width: rect.width,
          height: rect.height,
          color: style.color,
          fontFamily: style.fontFamily,
          fontSize: style.fontSize,
          fontStyle: style.fontStyle,
          fontWeight: style.fontWeight,
        });
      }
    }
    node = walker.nextNode();
  }
  return cells;
}

function prepareCanvas(canvas, rect) {
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  canvas.style.left = `${rect.left}px`;
  canvas.style.top = `${rect.top}px`;
  canvas.style.width = `${rect.width}px`;
  canvas.style.height = `${rect.height}px`;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.textBaseline = "top";
  return context;
}

function drawCell(context, cell, { text = cell.text, color = cell.color, x = cell.x, y = cell.y, bold = false } = {}) {
  context.fillStyle = color;
  context.font = `${cell.fontStyle} ${bold ? "700" : cell.fontWeight} ${cell.fontSize} ${cell.fontFamily}`;
  context.fillText(text, x, y + Math.max(0, (cell.height - parseFloat(cell.fontSize)) / 2));
}

function rippleFrame(context, snapshot, progress) {
  const centerX = snapshot.width / 2;
  const centerY = snapshot.height / 2;
  const maximumRadius = Math.hypot(centerX, centerY) || 1;
  for (const [index, cell] of snapshot.cells.entries()) {
    const radius = Math.hypot(cell.x + cell.width / 2 - centerX, cell.y + cell.height / 2 - centerY);
    const activation = Math.min(0.64, radius / maximumRadius * 0.6 + hash(snapshot.seed, index, 0) * 0.025);
    const local = Math.min(1, Math.max(0, (progress - activation) / (1 - activation)));
    if (local >= 0.9) continue;
    if (local <= 0) drawCell(context, cell);
    else if (local < 0.12) drawCell(context, cell, { color: "#d7ffff", bold: true });
    else if (local < 0.28) drawCell(context, cell, { color: "#87d7ff" });
    else if (local < 0.45) drawCell(context, cell, { color: "#5fafff" });
    else if (local < 0.62) drawCell(context, cell, { color: "#5f87d7" });
    else if (local < 0.79) drawCell(context, cell, { color: "#5f5faf" });
    else drawCell(context, cell, { color: "#30305f" });
  }
}

function fireStage(heat) {
  return FIRE_STAGES.find(([maximum]) => heat <= maximum) || FIRE_STAGES.at(-1);
}

function doomFireFrame(context, snapshot, progress) {
  if (!snapshot.cells.length) return;
  const rows = snapshot.cells.map((cell) => cell.y);
  const top = Math.min(...rows);
  const bottom = Math.max(...rows);
  const span = Math.max(1, bottom - top);
  const extension = 0.3 / Math.sqrt(snapshot.cells.length);
  for (const [index, cell] of snapshot.cells.entries()) {
    const heightDelay = (bottom - cell.y) / span;
    const ignition = 0.01 + heightDelay * 0.58 + hash(snapshot.seed, index, 0) * 0.08;
    const lifetime = 0.025 + hash(snapshot.seed, index, 1) * 0.025 + extension;
    if (progress < ignition) {
      drawCell(context, cell);
      continue;
    }
    const age = progress - ignition;
    const plumeLifetime = lifetime + Math.min(0.18, cell.y / Math.max(1, snapshot.height) * 0.18);
    if (age >= plumeLifetime) continue;
    const heat = Math.max(0.02, 1 - age / plumeLifetime);
    const [, color, glyph] = fireStage(heat);
    const rise = age / plumeLifetime * Math.min(cell.y + cell.height, cell.height * 5);
    const sway = (hash(snapshot.seed, index, Math.floor(progress * 90)) - 0.5) * cell.width * 2;
    drawCell(context, cell, {
      text: glyph,
      color,
      x: cell.x + sway,
      y: cell.y - rise,
      bold: heat > 0.78,
    });
    if (age < lifetime) {
      const sourceHeat = Math.max(0.02, 1 - age / lifetime);
      const [, sourceColor, sourceGlyph] = fireStage(sourceHeat);
      drawCell(context, cell, { text: sourceGlyph, color: sourceColor, bold: sourceHeat > 0.78 });
    }
  }
}

function acidStage(local, cell, variation) {
  if (local < 0.13) return { color: "#afff00", text: cell.text, bold: true };
  if (local < 0.3) return { color: "#87d700", text: cell.text.toLocaleLowerCase() };
  if (local < 0.47) return { color: "#5f8700", text: variation < 0.5 ? "#" : "%" };
  if (local < 0.64) return { color: "#5f5f00", text: "*" };
  if (local < 0.8) return { color: "#5f3f00", text: ":" };
  if (local < 0.91) return { color: "#3f2f00", text: "." };
  return null;
}

function acidRainState(snapshot) {
  const columns = new Map();
  const bandWidth = Math.max(4, snapshot.cells[0]?.width || 10);
  snapshot.cells.forEach((cell, index) => {
    const column = Math.round((cell.x + cell.width / 2) / bandWidth);
    const entries = columns.get(column) || [];
    entries.push({ cell, index });
    columns.set(column, entries);
  });
  const impacts = new Map();
  for (const [column, entries] of columns) {
    entries.sort((left, right) => left.cell.y - right.cell.y);
    const top = entries[0].cell.y / Math.max(1, snapshot.height - entries[0].cell.height);
    const firstImpact = 0.045 + top * 0.35 + hash(snapshot.seed, column, 1) * 0.025;
    const slot = Math.max(0.01, (0.86 - firstImpact) / entries.length);
    for (const [order, entry] of entries.entries()) {
      const impact = firstImpact + order * slot + hash(snapshot.seed, column, order + 2) * slot * 0.12;
      impacts.set(entry.index, impact);
    }
  }
  return { bandWidth, columns, impacts };
}

function drawAcidStreak(context, cell, progress, impact, seed, index, streakLength, height) {
  const travelDuration = 0.1 + cell.y / Math.max(1, cell.height) / 400;
  const hold = 0.045 + hash(seed, index, 59) * 0.035;
  if (progress < impact - travelDuration || progress >= impact + hold) return;
  const travel = Math.min(1, (progress - impact + travelDuration) / travelDuration);
  const headY = -cell.height * streakLength + travel * (cell.y + cell.height * streakLength);
  for (let offset = 0; offset < streakLength; offset += 1) {
    const y = headY - offset * cell.height;
    if (y < -cell.height || y >= height) continue;
    drawCell(context, cell, {
      text: offset === 0 ? "│" : offset < streakLength / 2 ? ":" : ".",
      color: offset === 0 ? "#afff00" : offset < streakLength / 2 ? "#87d700" : "#5f8700",
      x: cell.x,
      y,
      bold: offset === 0,
    });
  }
}

function acidRainFrame(context, snapshot, progress, options) {
  const state = snapshot.effectState;
  const streakLength = options.streak_length;
  for (const [index, cell] of snapshot.cells.entries()) {
    const impact = state.impacts.get(index) ?? 0.72;
    drawAcidStreak(
      context,
      cell,
      progress,
      impact,
      snapshot.seed,
      index,
      streakLength,
      snapshot.height,
    );
    if (progress < impact) {
      drawCell(context, cell);
      continue;
    }
    const column = Math.round((cell.x + cell.width / 2) / state.bandWidth);
    const ordered = state.columns.get(column) || [];
    const position = ordered.findIndex((entry) => entry.index === index);
    const nextImpact = position >= 0 && position + 1 < ordered.length
      ? state.impacts.get(ordered[position + 1].index)
      : null;
    const duration = Math.min(
      0.3,
      Math.max(0.01, (1 - impact) / 0.91),
      nextImpact === null ? Infinity : Math.max(0.01, (nextImpact - impact) * 0.88),
    );
    const local = (progress - impact) / duration;
    const stage = acidStage(local, cell, hash(snapshot.seed, index, 90));
    if (stage) drawCell(context, cell, stage);
  }
  const spacing = state.bandWidth;
  const decorativeColumns = Math.min(120, Math.ceil(snapshot.width / spacing));
  for (let column = 0; column < decorativeColumns; column += 1) {
    if (hash(snapshot.seed, column, 101) > options.density) continue;
    const x = column * snapshot.width / decorativeColumns;
    if (state.columns.has(Math.round(x / spacing))) continue;
    const cycle = 1.1 + hash(snapshot.seed, column, 102) * 1.4;
    const phase = hash(snapshot.seed, column, 103);
    const y = ((progress * cycle + phase) % 1) * (snapshot.height + 4 * spacing) - 4 * spacing;
    const cell = snapshot.cells[0];
    if (!cell) continue;
    drawCell(context, cell, { text: "│", color: "#afff00", x, y, bold: true });
    drawCell(context, cell, { text: ":", color: "#87d700", x, y: y - cell.height });
    drawCell(context, cell, { text: ".", color: "#5f8700", x, y: y - cell.height * 2 });
  }
}

function flameFrame(context, snapshot, progress) {
  for (const [index, cell] of snapshot.cells.entries()) {
    const ignition = hash(snapshot.seed, index, 1) * 0.16;
    const burnout = 0.62 + hash(snapshot.seed, index, 2) * 0.38;
    const local = (progress - ignition) / Math.max(0.01, burnout - ignition);
    if (local < 0.1) drawCell(context, cell);
    else if (local < 0.27) drawCell(context, cell, { color: "#ffd700", bold: true });
    else if (local < 0.43) drawCell(context, cell, { color: "#ff8700", bold: true });
    else if (local < 0.59) drawCell(context, cell, { color: "#d70000", bold: true });
    else if (local < 0.7) drawCell(context, cell, { color: "#5f0000" });
    else if (local < 0.98) {
      const smoke = Math.min(1, (local - 0.7) / 0.3);
      const lift = smoke * cell.height * (1 + Math.floor(hash(snapshot.seed, index, 3) * 4));
      const sway = Math.sin(progress * 28 + index * 0.37) * smoke * cell.width * 1.5;
      const text = local < 0.82 ? cell.text.toLocaleLowerCase() : local < 0.92 ? "*" : ":";
      drawCell(context, cell, {
        text,
        color: local < 0.82 ? "#bcbcbc" : "#626262",
        x: cell.x + sway,
        y: cell.y - lift,
      });
    }
  }
}

function sandstormFrame(context, snapshot, progress, options) {
  const direction = options.direction === "right-to-left" ? -1 : 1;
  for (const [index, cell] of snapshot.cells.entries()) {
    const windward = direction > 0 ? cell.x / snapshot.width : 1 - cell.x / snapshot.width;
    const activation = 0.03 + windward * 0.18 + hash(snapshot.seed, index, 1) * 0.11;
    const local = Math.min(1, Math.max(0, (progress - activation) / (1 - activation)));
    if (local <= 0) {
      drawCell(context, cell);
      continue;
    }
    if (local >= 0.8 + hash(snapshot.seed, index, 2) * 0.14) continue;
    const travel = local ** 1.6 * (snapshot.width + cell.width * (6 + hash(snapshot.seed, index, 3) * 8));
    const turbulence = Math.sin(local * 15 + hash(snapshot.seed, index, 4) * Math.PI * 2);
    const drift = (turbulence + hash(snapshot.seed, index, 5) - 0.5) * options.gust_strength * local * cell.height;
    const breakup = Math.min(1, local / 0.65);
    const colors = ["#e5c07b", "#c89b5a", "#8b6f47"];
    const glyphs = [".", ":", "·", "*"];
    drawCell(context, cell, {
      text: glyphs[Math.floor(hash(snapshot.seed, index, 6) * glyphs.length) % glyphs.length],
      color: colors[Math.min(colors.length - 1, Math.floor(breakup * colors.length))],
      x: cell.x + direction * travel,
      y: cell.y + drift,
    });
  }
}

function vortexFrame(context, snapshot, progress, options) {
  const centerX = snapshot.width / 2;
  const centerY = snapshot.height / 2;
  const maximum = Math.hypot(centerX, centerY) || 1;
  const direction = options.direction === "counterclockwise" ? -1 : 1;
  for (const cell of snapshot.cells) {
    const x = cell.x + cell.width / 2 - centerX;
    const y = cell.y + cell.height / 2 - centerY;
    const radius = Math.hypot(x, y);
    const activation = 0.58 * radius / maximum;
    const local = Math.min(1, Math.max(0, (progress - activation) / Math.max(0.01, 1 - activation)));
    const eased = local * local * (3 - 2 * local);
    if (eased >= 0.98) continue;
    const angle = Math.atan2(y, x) + direction * Math.PI * 2 * options.turns * eased;
    const collapsedRadius = radius * (1 - eased * eased * (3 - 2 * eased));
    drawCell(context, cell, {
      x: centerX + Math.cos(angle) * collapsedRadius - cell.width / 2,
      y: centerY + Math.sin(angle) * collapsedRadius - cell.height / 2,
    });
  }
}

function filmBurnState(snapshot, options) {
  const minimum = Math.min(options.minimum_holes, options.maximum_holes);
  const maximum = Math.max(options.minimum_holes, options.maximum_holes);
  const count = minimum + Math.floor(hash(snapshot.seed, 0, 210) * (maximum - minimum + 1));
  return {
    holes: Array.from({ length: count }, (_, index) => ({
      x: hash(snapshot.seed, index, 211) * snapshot.width,
      y: hash(snapshot.seed, index, 212) * snapshot.height,
      appearance: index === 0 ? 0 : hash(snapshot.seed, index, 213) * 0.16,
      growth: 0.45 + hash(snapshot.seed, index, 214) * 0.1,
      wobble: hash(snapshot.seed, index, 215) * Math.PI * 2,
    })),
  };
}

function filmBurnFrame(context, snapshot, progress) {
  const maximumRadius = Math.hypot(snapshot.width, snapshot.height) || 1;
  for (const [index, cell] of snapshot.cells.entries()) {
    const centerX = cell.x + cell.width / 2;
    const centerY = cell.y + cell.height / 2;
    let ignition = 1;
    for (const hole of snapshot.effectState.holes) {
      const distance = Math.hypot(centerX - hole.x, centerY - hole.y);
      const angle = Math.atan2(centerY - hole.y, centerX - hole.x);
      const irregularity = 1 + 0.08 * Math.sin(angle * 3 + hole.wobble) + 0.04 * Math.sin(angle * 7 - hole.wobble);
      ignition = Math.min(ignition, hole.appearance + hole.growth * distance * irregularity / maximumRadius);
    }
    const age = progress - ignition;
    if (age < 0) {
      drawCell(context, cell);
      continue;
    }
    const local = age / 0.16;
    if (local >= 1) continue;
    const colors = ["#ff8700", "#af0000", "#5f2f17", "#000000"];
    const stage = Math.min(colors.length - 1, Math.floor(local * colors.length));
    drawCell(context, cell, { color: colors[stage], bold: stage === 0 });
    if (stage === 0 && hash(snapshot.seed, index, Math.floor(progress * 120)) > 0.72) {
      drawCell(context, cell, { text: ".", color: "#ffd700", x: cell.x + cell.width, y: cell.y - cell.height / 2 });
    }
  }
}

function waterFrame(context, snapshot, progress, options) {
  for (const [index, cell] of snapshot.cells.entries()) {
    const release = hash(snapshot.seed, index, 301) * 0.14;
    const local = Math.max(0, (progress - release) / Math.max(0.01, 1 - release));
    if (local <= 0) {
      drawCell(context, cell);
      continue;
    }
    const drain = 0.68 + hash(snapshot.seed, index, 302) * 0.3;
    if (local >= drain) continue;
    const floor = snapshot.height - cell.height;
    const fall = (snapshot.height + cell.height) * local * local * 2.2;
    const rawY = cell.y + fall;
    const bounce = rawY <= floor ? rawY : floor - Math.abs(Math.sin((rawY - floor) / Math.max(1, cell.height) * 0.8)) * cell.height * 2 * (1 - local);
    const slosh = Math.sin(progress * 16 + index * 0.19) * cell.width * 2.5 * options.slosh_strength * local;
    const dynamic = options.color_mode === "dynamic";
    const colors = ["#d7ffff", "#87d7ff", "#5fafff", "#5f87d7", "#5f5faf"];
    drawCell(context, cell, {
      color: dynamic ? colors[Math.min(colors.length - 1, Math.floor(local * colors.length))] : cell.color,
      x: cell.x + slosh,
      y: bounce,
    });
  }
}

function prepareEffectState(snapshot, effect) {
  if (effect.name === "acid_rain") return acidRainState(snapshot);
  if (effect.name === "film_burn") return filmBurnState(snapshot, effect.options);
  return null;
}

export function renderScreenClearFrame(context, snapshot, effect, progress) {
  if (progress >= 1) return;
  if (snapshot.effectState === null && ["acid_rain", "film_burn"].includes(effect.name)) {
    snapshot.effectState = prepareEffectState(snapshot, effect);
  }
  if (effect.name === "doom_fire") doomFireFrame(context, snapshot, progress);
  else if (effect.name === "acid_rain") acidRainFrame(context, snapshot, progress, effect.options);
  else if (effect.name === "film_burn") filmBurnFrame(context, snapshot, progress);
  else if (effect.name === "flame") flameFrame(context, snapshot, progress);
  else if (effect.name === "sandstorm") sandstormFrame(context, snapshot, progress, effect.options);
  else if (effect.name === "vortex") vortexFrame(context, snapshot, progress, effect.options);
  else if (effect.name === "water") waterFrame(context, snapshot, progress, effect.options);
  else rippleFrame(context, snapshot, progress);
}

export function animateScreenClear({ canvas, transcript, cells, effect, seed, onFinish }) {
  const rect = transcript.getBoundingClientRect();
  const context = prepareCanvas(canvas, rect);
  const snapshot = { cells, width: rect.width, height: rect.height, seed, effectState: null };
  snapshot.effectState = prepareEffectState(snapshot, effect);
  const started = performance.now();
  const minimumFrameMs = 1000 / effect.frames_per_second;
  let previousFrame = -Infinity;
  let animationFrame = null;
  let stopped = false;

  canvas.hidden = false;
  const finish = () => {
    if (stopped) return;
    stopped = true;
    if (animationFrame !== null) cancelAnimationFrame(animationFrame);
    canvas.hidden = true;
    context.clearRect(0, 0, snapshot.width, snapshot.height);
    onFinish?.();
  };
  const render = (now) => {
    if (stopped) return;
    const elapsed = now - started;
    const progress = Math.min(1, elapsed / effect.duration_ms);
    if (now - previousFrame >= minimumFrameMs || progress >= 1) {
      previousFrame = now;
      context.clearRect(0, 0, snapshot.width, snapshot.height);
      renderScreenClearFrame(context, snapshot, effect, progress);
    }
    if (progress >= 1) finish();
    else animationFrame = requestAnimationFrame(render);
  };
  animationFrame = requestAnimationFrame(render);
  return { cancel: finish };
}
