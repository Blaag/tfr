export const COMBO_TIMEOUT_MS = 300_000;

const LEVELS = {
  3: ["> Speaking Spree! <", "#ffffff"],
  4: ["> Rampage! <", "#1eff00"],
  5: ["> Dominating! <", "#0070dd"],
  6: ["> Unstoppable! <", "#a335ee"],
  7: ["> GODLIKE! <", "#ff8000"],
};

const scalarLength = (text) => Array.from(text).length;

function speakerAndBody(event) {
  const text = event?.text || "";
  let speaker = event?.provenance?.sender_name;
  if (event?.kind === "say") {
    const match = /^(.*?)\bsays?,\s+["“](.*?)["”]?$/.exec(text);
    if (!match) return null;
    speaker ||= match[1].trim();
    const bodyCodeUnitStart = match.index + match[0].indexOf(match[2]);
    return speaker ? {
      speaker,
      start: scalarLength(text.slice(0, bodyCodeUnitStart)),
      end: scalarLength(text.slice(0, bodyCodeUnitStart + match[2].length)),
    } : null;
  }
  if (event?.kind !== "pose" && !(event?.kind === "raw_output" && event?.presentation)) return null;
  if (!speaker) {
    const token = text.trimStart().split(/\s+/, 1)[0] || "";
    speaker = token.replace(/(?:'s|’s)$/i, "");
  }
  if (!speaker || text.slice(0, speaker.length).toLocaleLowerCase() !== speaker.toLocaleLowerCase()) return null;
  let start = speaker.length;
  if (["'s", "’s"].includes(text.slice(start, start + 2).toLocaleLowerCase())) start += 2;
  while (/\s/.test(text[start] || "")) start += 1;
  return start < text.length
    ? { speaker, start: scalarLength(text.slice(0, start)), end: scalarLength(text) }
    : null;
}

export function observeCombo(streaks, event, now = Date.now()) {
  const parsed = speakerAndBody(event);
  if (!parsed) return null;
  const identity = parsed.speaker.toLocaleLowerCase();
  const previous = streaks.get(event.world);
  const count = previous && previous.identity === identity && now - previous.at <= COMBO_TIMEOUT_MS
    ? previous.count + 1
    : 1;
  streaks.set(event.world, { identity, count, at: now });
  if (count < 3) return null;
  const [notice, color] = LEVELS[count] || [`> GODLIKE x${count - 6} <`, "#ff8000"];
  return { count, notice, color, body_start: parsed.start, body_end: parsed.end };
}
