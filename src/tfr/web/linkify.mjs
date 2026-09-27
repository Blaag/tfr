const URL_PATTERN = /https?:\/\/[^\s<>"']+/gi;
const BIDI_CONTROL_PATTERN = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/u;

function trimTrailingPunctuation(value) {
  let end = value.length;
  const counts = { "(": 0, ")": 0, "[": 0, "]": 0, "{": 0, "}": 0 };
  for (const character of value) {
    if (Object.hasOwn(counts, character)) counts[character] += 1;
  }
  const opening = { ")": "(", "]": "[", "}": "{" };
  while (end > 0) {
    const last = value[end - 1];
    if (".,!?;:".includes(last)) {
      end -= 1;
      continue;
    }
    if (Object.hasOwn(opening, last) && counts[last] > counts[opening[last]]) {
      counts[last] -= 1;
      end -= 1;
      continue;
    }
    break;
  }
  return value.slice(0, end);
}

export function linkParts(text) {
  const parts = [];
  let offset = 0;
  for (const match of text.matchAll(URL_PATTERN)) {
    if (match.index > offset) parts.push({ type: "text", text: text.slice(offset, match.index) });
    const candidate = trimTrailingPunctuation(match[0]);
    try {
      const url = new URL(candidate);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        url.username ||
        url.password ||
        BIDI_CONTROL_PATTERN.test(candidate)
      ) {
        throw new Error();
      }
      parts.push({ type: "link", text: candidate, href: url.href });
    } catch {
      parts.push({ type: "text", text: candidate });
    }
    if (candidate.length < match[0].length) {
      parts.push({ type: "text", text: match[0].slice(candidate.length) });
    }
    offset = match.index + match[0].length;
  }
  if (offset < text.length) parts.push({ type: "text", text: text.slice(offset) });
  return parts;
}
