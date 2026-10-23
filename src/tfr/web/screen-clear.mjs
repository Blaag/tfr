const DEFAULT_EFFECTS = Object.freeze([
  { name: "water_ripple", duration_ms: 2600, frames_per_second: 24 },
  { name: "doom_fire", duration_ms: 3200, frames_per_second: 24 },
]);

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

export function validatedScreenClearConfig(value) {
  const mode = ["cycle", "random", "locked"].includes(value?.mode) ? value.mode : "cycle";
  const effects = Array.isArray(value?.effects)
    ? value.effects.flatMap((effect) => {
        if (!effect || !["water_ripple", "doom_fire"].includes(effect.name)) return [];
        return [{
          name: effect.name,
          duration_ms: boundedNumber(effect.duration_ms, 2600, 100, 60000),
          frames_per_second: boundedNumber(effect.frames_per_second, 24, 1, 30),
        }];
      })
    : [];
  const available = Array.isArray(value?.effects) ? effects : [...DEFAULT_EFFECTS];
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

export function animateScreenClear({ canvas, transcript, cells, effect, seed, onFinish }) {
  const rect = transcript.getBoundingClientRect();
  const context = prepareCanvas(canvas, rect);
  const snapshot = { cells, width: rect.width, height: rect.height, seed };
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
      if (effect.name === "doom_fire") doomFireFrame(context, snapshot, progress);
      else rippleFrame(context, snapshot, progress);
    }
    if (progress >= 1) finish();
    else animationFrame = requestAnimationFrame(render);
  };
  animationFrame = requestAnimationFrame(render);
  return { cancel: finish };
}
