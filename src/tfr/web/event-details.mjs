function displayValue(value) {
  return String(value).replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function spoofStatus(event) {
  if (event.direction !== "inbound") return "Not applicable";
  if (event.spoof_status === "not_spoofed") return "Not spoofed; NOSPOOF sender matches speaker";
  if (event.spoof_status === "spoofed") {
    if (event.spoof_reason === "missing_nospoof_prefix") {
      return "Spoofed; missing NOSPOOF prefix";
    }
    return "Spoofed; NOSPOOF source differs from speaker";
  }
  const confidence = event.provenance?.confidence;
  if (confidence === "authoritative" || confidence === "high") {
    return "Undetermined; source attribution available";
  }
  if (confidence === "inferred") return "Undetermined; attribution is inferred";
  return "Undetermined; no reliable attribution";
}

function transitionTime(value) {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.valueOf())) return "local unknown; UTC unknown";
  const local = timestamp.toLocaleString([], {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });
  const utc = timestamp.toISOString().slice(0, 19).replace("T", " ");
  return `local ${local}; UTC ${utc} UTC`;
}

export function connectionNotice(event) {
  const timestamp = transitionTime(event.timestamp);
  if (event.connection_state === "connected") return `Connected [${timestamp}]`;
  if (event.connection_state === "disconnected") {
    const reason = event.connection_error ? `: ${event.connection_error}` : "";
    return `Disconnected${reason} [${timestamp}]`;
  }
  if (event.connection_state === "reconnect_wait") {
    const delay = Number.isFinite(event.reconnect_delay_seconds)
      ? ` in ${event.reconnect_delay_seconds}s`
      : "";
    return `Reconnecting${delay} [${timestamp}]`;
  }
  return event.text || "";
}

export function eventDetailRows(event) {
  const provenance = event.provenance || {};
  const rows = [
    ["Time", event.timestamp],
    ["World", event.world],
    ["Direction", displayValue(event.direction || "unknown")],
    ["Type", displayValue(event.kind || "unknown")],
    ["Spoof status", spoofStatus(event)],
  ];
  if (provenance.sender_name) rows.push(["Sender", provenance.sender_name]);
  if (provenance.owner_name) rows.push(["Owner", provenance.owner_name]);
  if (provenance.server_source) rows.push(["Server source", provenance.server_source]);
  if (provenance.confidence) {
    rows.push(["Attribution confidence", displayValue(provenance.confidence)]);
  }
  if (event.spoof_sender) rows.push(["Likely sender", event.spoof_sender]);
  if (event.spoof_sender_confidence) {
    rows.push(["Spoof attribution confidence", displayValue(event.spoof_sender_confidence)]);
  }
  rows.push([
    "Text",
    [event.redacted ? "Redacted" : null, event.text_truncated ? "Truncated" : null]
      .filter(Boolean)
      .join("; ") || "Complete",
  ]);
  return rows;
}
