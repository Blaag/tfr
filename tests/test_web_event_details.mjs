import assert from "node:assert/strict";
import test from "node:test";

import { connectionNotice, eventDetailRows } from "../src/tfr/web/event-details.mjs";

test("formats world connection transitions with local and UTC times", () => {
  const timestamp = "2026-09-28T18:34:56Z";
  const connected = connectionNotice({ timestamp, connection_state: "connected" });
  const disconnected = connectionNotice({
    timestamp,
    connection_state: "disconnected",
    connection_error: "remote closed",
  });
  const reconnecting = connectionNotice({
    timestamp,
    connection_state: "reconnect_wait",
    reconnect_delay_seconds: 5,
  });

  for (const notice of [connected, disconnected, reconnecting]) {
    assert.match(notice, /local .+; UTC 2026-09-28 18:34:56 UTC/);
  }
  assert.match(disconnected, /^Disconnected: remote closed/);
  assert.match(reconnecting, /^Reconnecting in 5s/);
});

test("shows a server-derived NOSPOOF verdict", () => {
  assert.deepEqual(
    eventDetailRows({
      timestamp: "2026-09-26T12:34:56Z",
      world: "global",
      direction: "inbound",
      kind: "raw_output",
      redacted: false,
      text_truncated: false,
      spoof_status: "not_spoofed",
      provenance: {
        sender_name: "Widget",
        owner_name: "Alice",
        server_source: "saypose",
        confidence: "high",
      },
    }),
    [
      ["Time", "2026-09-26T12:34:56Z"],
      ["World", "global"],
      ["Direction", "Inbound"],
      ["Type", "Raw output"],
      ["Spoof status", "Not spoofed; NOSPOOF sender matches speaker"],
      ["Sender", "Widget"],
      ["Owner", "Alice"],
      ["Server source", "saypose"],
      ["Attribution confidence", "High"],
      ["Text", "Complete"],
    ],
  );
});

test("shows a mismatched NOSPOOF sender as spoofed", () => {
  const rows = eventDetailRows({
    timestamp: "2026-09-26T12:34:56Z",
    world: "global",
    direction: "inbound",
    kind: "say",
    redacted: false,
    text_truncated: false,
    spoof_status: "spoofed",
    provenance: { sender_name: "Widget", confidence: "high" },
  });

  assert.equal(
    rows.find(([label]) => label === "Spoof status")[1],
    "Spoofed; NOSPOOF source differs from speaker",
  );
});

test("shows an inferred sender for a multiline spoof", () => {
  const rows = eventDetailRows({
    timestamp: "2026-09-26T12:34:56Z",
    world: "global",
    direction: "inbound",
    kind: "say",
    redacted: false,
    text_truncated: false,
    spoof_status: "spoofed",
    spoof_reason: "missing_nospoof_prefix",
    spoof_sender: "Black2",
    spoof_sender_confidence: "inferred",
  });

  assert.equal(
    rows.find(([label]) => label === "Spoof status")[1],
    "Spoofed; missing NOSPOOF prefix",
  );
  assert.deepEqual(rows.slice(-3), [
    ["Likely sender", "Black2"],
    ["Spoof attribution confidence", "Inferred"],
    ["Text", "Complete"],
  ]);
});

test("reports limited evidence and content handling", () => {
  const rows = eventDetailRows({
    timestamp: "invalid",
    world: "global",
    direction: "inbound",
    kind: "page",
    redacted: true,
    text_truncated: true,
  });

  assert.deepEqual(rows.at(-1), ["Text", "Redacted; Truncated"]);
  assert.equal(rows.find(([label]) => label === "Spoof status")[1], "Undetermined; no reliable attribution");
});

test("does not infer a verdict from high-confidence attribution alone", () => {
  const rows = eventDetailRows({
    timestamp: "2026-09-26T12:34:56Z",
    world: "global",
    direction: "inbound",
    kind: "raw_output",
    redacted: false,
    text_truncated: false,
    provenance: { sender_name: "Widget", confidence: "high" },
  });

  assert.equal(
    rows.find(([label]) => label === "Spoof status")[1],
    "Undetermined; source attribution available",
  );
});
