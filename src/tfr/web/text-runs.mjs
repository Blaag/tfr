const COLOR = /^#[0-9a-f]{6}$/;
const STYLE_KEYS = new Set(["foreground", "background", "bold", "italic", "underline"]);
const MAX_INSTALLED_COLOR_RULES = 512;

export function validatedTextRuns(event) {
  const runs = event?.text_runs;
  if (!Array.isArray(runs) || runs.length === 0) return null;
  const validated = [];
  for (const run of runs) {
    if (!run || typeof run.text !== "string") return null;
    if (run.role !== undefined && run.role !== "speaker") return null;
    if (!run.style || typeof run.style !== "object" || Array.isArray(run.style)) return null;
    if (!Object.keys(run.style).every((key) => STYLE_KEYS.has(key))) return null;
    const style = {};
    for (const key of ["foreground", "background"]) {
      if (run.style[key] !== undefined) {
        if (typeof run.style[key] !== "string" || !COLOR.test(run.style[key])) return null;
        style[key] = run.style[key];
      }
    }
    for (const key of ["bold", "italic", "underline"]) {
      if (run.style[key] !== undefined) {
        if (run.style[key] !== true) return null;
        style[key] = true;
      }
    }
    validated.push({ text: run.text, style, role: run.role });
  }
  return validated.map((run) => run.text).join("") === (event.text || "") ? validated : null;
}

export function textRunClassNames(style) {
  const classes = [];
  if (style.foreground) classes.push(`ansi-fg-${style.foreground.slice(1)}`);
  if (style.background) classes.push(`ansi-bg-${style.background.slice(1)}`);
  if (style.bold) classes.push("ansi-bold");
  if (style.italic) classes.push("ansi-italic");
  if (style.underline) classes.push("ansi-underline");
  return classes;
}

export function installTextRunColors(style, sheet, installedRules) {
  for (const [property, prefix] of [
    ["foreground", "ansi-fg"],
    ["background", "ansi-bg"],
  ]) {
    const color = style[property];
    if (!color || installedRules.has(`${prefix}-${color}`)) continue;
    if (installedRules.size >= MAX_INSTALLED_COLOR_RULES) return;
    const rule = `.${prefix}-${color.slice(1)} { ${property === "foreground" ? "color" : "background-color"}: ${color}; }`;
    sheet.insertRule(rule, sheet.cssRules.length);
    installedRules.add(`${prefix}-${color}`);
  }
}
