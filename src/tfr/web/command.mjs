export function normalizeWorldCommand(text) {
  return text.replace(/^[“”]/u, '"');
}
