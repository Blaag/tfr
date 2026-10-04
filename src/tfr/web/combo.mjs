export const COMBO_TIMEOUT_MS = 300_000;

const LEVELS = {
  3: ["> Speaking Spree! <", "#ffffff"],
  4: ["> Rampage! <", "#1eff00"],
  5: ["> Dominating! <", "#0070dd"],
  6: ["> Unstoppable! <", "#a335ee"],
  7: ["> GODLIKE! <", "#ff8000"],
};

const scalarLength = (text) => Array.from(text).length;

function startsWithSpeaker(text, speaker) {
  if (text.slice(0, speaker.length).toLocaleLowerCase() !== speaker.toLocaleLowerCase()) return false;
  const suffix = text.slice(speaker.length);
  return Boolean(suffix && (/\s/.test(suffix[0]) || ["'s", "’s"].includes(suffix.slice(0, 2).toLocaleLowerCase())));
}

function speakerAndBody(event, world) {
  const text = event?.text || "";
  let speaker = event?.provenance?.sender_name;
  if (["say", "speech"].includes(event?.kind)) {
    const match = /^(.*?)\bsays?,\s+["“](.*?)["”]?$/.exec(text);
    if (match) {
      const visible = match[1].trim();
      speaker ||= visible.toLocaleLowerCase() === "you" ? world?.character : visible;
      const bodyCodeUnitStart = match.index + match[0].indexOf(match[2]);
      return speaker && match[2].length ? {
        speaker,
        start: scalarLength(text.slice(0, bodyCodeUnitStart)),
        end: scalarLength(text.slice(0, bodyCodeUnitStart + match[2].length)),
      } : null;
    }
  }
  if (!["pose", "speech", "raw_output"].includes(event?.kind)) return null;
  if (!speaker) {
    if (world?.character && startsWithSpeaker(text, world.character)) {
      speaker = world.character;
    } else if (["bare", "generic"].includes(world?.server)) {
      const token = text.trimStart().split(/\s+/, 1)[0] || "";
      speaker = token.replace(/(?:'s|’s)$/i, "");
    }
  }
  if (!speaker || text.slice(0, speaker.length).toLocaleLowerCase() !== speaker.toLocaleLowerCase()) return null;
  let start = speaker.length;
  if (["'s", "’s"].includes(text.slice(start, start + 2).toLocaleLowerCase())) start += 2;
  while (/\s/.test(text[start] || "")) start += 1;
  return start < text.length
    ? { speaker, start: scalarLength(text.slice(0, start)), end: scalarLength(text) }
    : null;
}

export function observeCombo(streaks, event, now = Date.now(), world = null) {
  const parsed = speakerAndBody(event, world);
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
