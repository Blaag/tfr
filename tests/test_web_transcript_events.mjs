import assert from "node:assert/strict";
import test from "node:test";

import { isTranscriptEvent } from "../src/tfr/web/transcript-events.mjs";

test("outbound commands are excluded from the transcript", () => {
  assert.equal(isTranscriptEvent({ direction: "outbound", kind: "command" }), false);
});

test("inbound commands and other outbound events remain visible", () => {
  assert.equal(isTranscriptEvent({ direction: "inbound", kind: "command" }), true);
  assert.equal(isTranscriptEvent({ direction: "outbound", kind: "connection" }), true);
  assert.equal(isTranscriptEvent({ direction: "inbound", kind: "say" }), true);
});
