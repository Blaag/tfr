export function normalizeWorldCommand(text) {
  return text.replace(/^[“”]/u, '"');
}

export function commandWithAutomaticSay(text, enabled) {
  if (!enabled) return text;
  const trimmed = text.trimStart();
  if (!trimmed) return text;
  if (/^[:;@]/u.test(trimmed)) return text;
  const firstToken = /^\S+/u.exec(trimmed)?.[0].toLocaleLowerCase();
  if (["p", "po", "pos", "pose", "sa", "say"].includes(firstToken)) return text;
  return `say ${text}`;
}

export function isMultilineWorldCommand(text) {
  return /[\r\n]/u.test(text);
}
