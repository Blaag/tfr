import assert from "node:assert/strict";
import test from "node:test";

import {
  generationIsNewer,
  historyNoticeDecision,
  historyNoticeMessage,
  historySnapshotHasGap,
  recordLiveHistoryEvent,
} from "../src/tfr/web/history-notice.mjs";

function world(generation, available, snapshot = available) {
  return {
    world: "alpha",
    connection_generation: generation,
    history: {
      connection_generation: generation,
      available_count: available,
      snapshot_count: snapshot,
      snapshot_gap: false,
    },
  };
}

test("history notice reports loaded current-generation messages", () => {
  const events = [
    { connection_generation: "6" },
    { connection_generation: "7" },
    { connection_generation: "7" },
  ];

  assert.equal(historyNoticeMessage(world("7", "5", 2), events), "Only the last 2 messages are shown.");
});

test("history notice ignores complete, stale, and invalid metadata", () => {
  const events = [{ connection_generation: "7" }, { connection_generation: "7" }];

  assert.equal(historyNoticeMessage(world("7", "2"), events), null);
  assert.equal(historyNoticeMessage({ ...world("7", "3"), connection_generation: "8" }, events), null);
  assert.equal(historyNoticeMessage(world("7", "invalid"), events), null);
});

test("generation comparison supports integer strings without number precision loss", () => {
  assert.equal(generationIsNewer("9007199254740993", "9007199254740992"), true);
  assert.equal(generationIsNewer("7", "7"), false);
  assert.equal(generationIsNewer("6", "7"), false);
  assert.equal(generationIsNewer("invalid", "7"), false);
});

test("history notice is shown once per world generation", () => {
  const events = [{ connection_generation: "7" }, { connection_generation: "7" }];
  const selected = world("7", "5", 2);

  const first = historyNoticeDecision(selected, events, undefined, null);
  assert.deepEqual(first, {
    action: "show",
    key: "alpha:7",
    generation: "7",
    message: "Only the last 2 messages are shown.",
  });
  assert.deepEqual(historyNoticeDecision(selected, events, "7", first.key), {
    action: "keep",
    key: "alpha:7",
  });
  assert.deepEqual(historyNoticeDecision(selected, events, "7", "beta:3"), {
    action: "hide",
    key: null,
  });
});

test("new world generation resets notice eligibility", () => {
  const events = [{ connection_generation: "8" }];

  assert.deepEqual(historyNoticeDecision(world("8", "3", 1), events, "7", null), {
    action: "show",
    key: "alpha:8",
    generation: "8",
    message: "Only the last 1 messages are shown.",
  });
});

test("snapshot gap detection distinguishes complete reconnects from missing backfill", () => {
  const events = Array.from({ length: 70 }, () => ({ connection_generation: "7" }));

  assert.equal(historySnapshotHasGap(world("7", "80", 10), events), false);
  const gap = world("7", "100", 10);
  gap.history.snapshot_gap = true;
  assert.equal(historySnapshotHasGap(gap, events), true);
  assert.equal(
    historySnapshotHasGap({ ...world("7", "100", 10), connection_generation: "8" }, events),
    false,
  );
});

test("live events preserve the snapshot omission deficit", () => {
  const selected = world("7", "5", 2);
  const events = [
    { connection_generation: "7" },
    { connection_generation: "7" },
  ];

  assert.equal(recordLiveHistoryEvent(selected, { connection_generation: "7" }), true);
  events.push({ connection_generation: "7" });
  assert.equal(historyNoticeMessage(selected, events), "Only the last 3 messages are shown.");
  assert.equal(recordLiveHistoryEvent(selected, { connection_generation: "8" }), false);
});
