export const MAX_PASTE_SOURCE_BYTES = 65_536;
export const MAX_PASTE_LINES = 100;
export const PASTE_PREVIEW_LINES = 10;

export function normalizePastedText(text) {
  return text.replace(/\r\n/gu, "\n").replace(/\r/gu, "\n");
}

export function preparePastePreview(text, world) {
  const normalized = normalizePastedText(text);
  const lines = normalized.split("\n");
  if (lines.length && lines.at(-1) === "") lines.pop();
  const byteCount = new TextEncoder().encode(lines.join("\n")).byteLength;
  if (!lines.length) throw new Error("Pasted text is empty");
  if (byteCount > MAX_PASTE_SOURCE_BYTES) {
    throw new Error(`Pasted text exceeds ${MAX_PASTE_SOURCE_BYTES} bytes`);
  }
  if (lines.length > MAX_PASTE_LINES) {
    throw new Error(`Pasted text exceeds ${MAX_PASTE_LINES} lines`);
  }
  return Object.freeze({
    world,
    text,
    lineCount: lines.length,
    byteCount,
    preview: lines.slice(0, PASTE_PREVIEW_LINES).join("\n"),
    previewTruncated: lines.length > PASTE_PREVIEW_LINES,
  });
}

export function validatedPasteUpdate(message, requestId) {
  if (!message || message.request_id !== requestId) return null;
  if (!["paste_started", "paste_progress", "paste_complete", "paste_cancelled"].includes(message.type)) {
    return null;
  }
  if (!Number.isSafeInteger(message.total_lines) || message.total_lines < 1 || message.total_lines > MAX_PASTE_LINES) {
    return null;
  }
  const sentLines = message.type === "paste_started" ? 0 : message.sent_lines;
  if (!Number.isSafeInteger(sentLines) || sentLines < 0 || sentLines > message.total_lines) return null;
  if (message.type === "paste_cancelled" && !["user", "disconnected", "revoked", "world_changed", "world_disconnected", "gateway_stopping", "submission_failed"].includes(message.reason)) {
    return null;
  }
  return { type: message.type, sentLines, totalLines: message.total_lines, reason: message.reason };
}
