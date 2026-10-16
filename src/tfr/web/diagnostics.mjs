const MAX_ENTRIES = 100;
const MAX_MESSAGE = 240;
const SAFE_NAME = /^[a-z][a-z0-9_.-]{0,63}$/;
const entries = [];

export function recordDiagnostic({ severity = "warning", component, code, message, operation, event_id }) {
  if (!["info", "warning", "error"].includes(severity)) return null;
  if (!SAFE_NAME.test(component || "") || !SAFE_NAME.test(code || "")) return null;
  if (typeof message !== "string" || !message || message.length > MAX_MESSAGE || /[\r\n\0]/.test(message)) return null;
  const entry = {
    timestamp: new Date().toISOString(),
    severity,
    component,
    code,
    message,
    client: "pwa",
    ...(typeof operation === "string" && operation.length <= 100 ? { operation } : {}),
    ...(typeof event_id === "string" && event_id.length <= 100 ? { event_id } : {}),
  };
  entries.push(entry);
  if (entries.length > MAX_ENTRIES) entries.splice(0, entries.length - MAX_ENTRIES);
  return entry;
}

export function diagnosticEntries() {
  return entries.map((entry) => ({ ...entry }));
}

export function clearDiagnostics() {
  entries.length = 0;
}

export async function reportDiagnostic(entry) {
  if (!entry) return false;
  try {
    const response = await fetch("/api/diagnostics", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        severity: entry.severity,
        component: entry.component,
        code: entry.code,
        ...(entry.operation ? { operation: entry.operation } : {}),
        ...(entry.event_id ? { event_id: entry.event_id } : {}),
      }),
    });
    return response.ok;
  } catch {
    return false;
  }
}
