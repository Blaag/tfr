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
