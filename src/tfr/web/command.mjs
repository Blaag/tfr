export function normalizeWorldCommand(text) {
  return text.replace(/^[“”]/u, '"');
}

export function isMultilineWorldCommand(text) {
  return /[\r\n]/u.test(text);
}
