const SHADES = Object.freeze(["·", "░", "▒", "▓", "█"]);
const WEEKDAYS = Object.freeze(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]);
const BLOCKS = "▁▂▃▄▅▆▇█";

function count(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
}

export function compactSummary(summary) {
  const value = summary && typeof summary === "object" ? summary : {};
  return {
    lines: count(value.lines),
    speakers: count(value.speakers),
    unattributed: count(value.unattributed_lines),
    sayPercent: Number.isFinite(Number(value.say_percent)) ? Number(value.say_percent) : 0,
    posePercent: Number.isFinite(Number(value.pose_percent)) ? Number(value.pose_percent) : 0,
    coverage: Number.isFinite(Number(value.classification_coverage))
      ? Number(value.classification_coverage)
      : 0,
    first: typeof value.first === "string" ? value.first : null,
    last: typeof value.last === "string" ? value.last : null,
  };
}

export function barGlyph(value, maximum, width = 24) {
  const boundedWidth = Math.max(1, Math.min(48, Math.floor(width) || 1));
  const total = count(maximum);
  if (!total) return "";
  const length = Math.round((count(value) / total) * boundedWidth);
  return "█".repeat(Math.max(count(value) ? 1 : 0, Math.min(boundedWidth, length)));
}

export function activitySparkline(series, maximumPoints = 48) {
  const source = Array.isArray(series)
    ? series.map((item) => ({
        label: typeof item?.label === "string" ? item.label : "?",
        value: count(item?.count),
      }))
    : [];
  if (!source.length) return { plot: "", maximum: 0, first: null, last: null };
  const limit = Math.max(1, Math.min(60, Math.floor(maximumPoints) || 1));
  const chunkSize = Math.max(1, Math.ceil(source.length / limit));
  const values = [];
  for (let start = 0; start < source.length; start += chunkSize) {
    const chunk = source.slice(start, start + chunkSize);
    values.push({ label: chunk[0].label, value: chunk.reduce((sum, item) => sum + item.value, 0) });
  }
  const maximum = Math.max(0, ...values.map((item) => item.value));
  return {
    plot: values.map((item) => maximum ? BLOCKS[Math.round((item.value / maximum) * 7)] : BLOCKS[0]).join(""),
    maximum,
    first: source[0].label,
    last: source.at(-1).label,
  };
}

export function heatmapModel(matrix) {
  const source = Array.isArray(matrix) ? matrix : [];
  const values = WEEKDAYS.map((_name, day) =>
    Array.from({ length: 24 }, (_unused, hour) => count(source[day]?.[hour])),
  );
  const maximum = Math.max(0, ...values.flat());
  return WEEKDAYS.map((name, day) => ({
    name,
    cells: values[day].map((value, hour) => {
      const level = maximum ? Math.round((value / maximum) * 4) : 0;
      return {
        hour,
        value,
        level,
        glyph: SHADES[level],
        label: `${name} ${String(hour).padStart(2, "0")}:00 — ${value} line${value === 1 ? "" : "s"}`,
      };
    }),
  }));
}

export const heatmapLegend = SHADES.map((glyph, level) => ({
  glyph,
  level,
  label: ["none", "low", "medium", "high", "peak"][level],
}));
