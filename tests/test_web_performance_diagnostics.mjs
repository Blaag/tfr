import assert from "node:assert/strict";
import test from "node:test";

import {
  createPerformanceInstrumentation,
  parsePerformancePreference,
  performanceInstrumentationStorageKey,
} from "../src/tfr/web/performance-diagnostics.mjs";

function harness() {
  let wallTime = 1_800_000_000_000;
  let monotonic = 0;
  let timerId = 0;
  let frameId = 0;
  const intervals = new Map();
  const frames = new Map();
  const storageValues = new Map();
  let observerCallback = null;
  let observed = false;
  let disconnected = false;
  class Observer {
    constructor(callback) {
      observerCallback = callback;
    }
    observe(options) {
      assert.deepEqual(options, { type: "longtask", buffered: true });
      observed = true;
    }
    disconnect() {
      disconnected = true;
    }
  }
  const browserWindow = {
    setInterval(callback) {
      const id = ++timerId;
      intervals.set(id, callback);
      return id;
    },
    clearInterval(id) {
      intervals.delete(id);
    },
    requestAnimationFrame(callback) {
      const id = ++frameId;
      frames.set(id, callback);
      return id;
    },
    cancelAnimationFrame(id) {
      frames.delete(id);
    },
  };
  const storage = {
    getItem: (key) => storageValues.get(key) ?? null,
    setItem: (key, value) => storageValues.set(key, value),
    removeItem: (key) => storageValues.delete(key),
  };
  const collector = createPerformanceInstrumentation({
    window: browserWindow,
    document: { visibilityState: "visible" },
    storage,
    dateNow: () => wallTime,
    monotonicNow: () => monotonic,
    Observer,
  });
  return {
    collector,
    frames,
    intervals,
    storageValues,
    advanceWall: (value) => { wallTime += value; },
    setMonotonic: (value) => { monotonic = value; },
    emitLongTasks: (durations) => observerCallback({
      getEntries: () => durations.map((duration) => ({ duration })),
    }),
    observed: () => observed,
    disconnected: () => disconnected,
  };
}

test("performance instrumentation is absent until explicitly enabled", () => {
  const testHarness = harness();
  assert.equal(testHarness.collector.restore(), false);
  assert.equal(testHarness.intervals.size, 0);
  assert.equal(testHarness.frames.size, 0);
  assert.deepEqual(testHarness.collector.report(), { status: "disabled" });
});

test("enable collects bounded local aggregate metrics and disable tears down", () => {
  const testHarness = harness();
  testHarness.collector.enable();
  assert.equal(testHarness.intervals.size, 2);
  assert.equal(testHarness.frames.size, 1);
  assert.equal(testHarness.observed(), true);
  assert.match(
    testHarness.storageValues.get(performanceInstrumentationStorageKey),
    /"version":1/,
  );

  testHarness.setMonotonic(5120);
  [...testHarness.intervals.values()][0]();
  testHarness.emitLongTasks([75, 125]);
  testHarness.collector.increment("events_processed", 4);
  testHarness.collector.recordDuration("transcript_render", 18.25);
  testHarness.collector.observeHighWater("event_nodes", 400);

  const report = testHarness.collector.report();
  assert.equal(report.status, "enabled");
  assert.equal(report.event_loop.max_delay_ms, 120);
  assert.equal(report.long_tasks.count, 2);
  assert.equal(report.long_tasks.total_ms, 200);
  assert.equal(report.operations.transcript_render.max_ms, 18.3);
  assert.equal(report.counters.events_processed, 4);
  assert.equal(report.high_water.event_nodes, 400);

  testHarness.collector.disable();
  assert.equal(testHarness.intervals.size, 0);
  assert.equal(testHarness.frames.size, 0);
  assert.equal(testHarness.disconnected(), true);
  assert.equal(testHarness.storageValues.size, 0);
});

test("stored opt-in expires and removes every sampler", () => {
  const testHarness = harness();
  testHarness.collector.enable();
  testHarness.advanceWall(24 * 60 * 60 * 1000 + 1);

  assert.deepEqual(testHarness.collector.report(), { status: "disabled" });
  assert.equal(testHarness.intervals.size, 0);
  assert.equal(testHarness.frames.size, 0);
  assert.equal(testHarness.storageValues.size, 0);
});

test("duration samples remain bounded", () => {
  const testHarness = harness();
  testHarness.collector.enable();
  for (let value = 0; value < 400; value += 1) {
    testHarness.collector.recordDuration("statistics_render", value);
  }
  assert.equal(testHarness.collector.report().operations.statistics_render.samples, 256);
});

test("preference parser rejects malformed, expired, and excessive grants", () => {
  const now = 1_800_000_000_000;
  assert.equal(parsePerformancePreference("not json", now), null);
  assert.equal(parsePerformancePreference('{"version":1,"expires_at":1}', now), null);
  assert.equal(parsePerformancePreference(JSON.stringify({
    version: 1,
    expires_at: now + 25 * 60 * 60 * 1000,
  }), now), null);
  assert.deepEqual(parsePerformancePreference(JSON.stringify({
    version: 1,
    expires_at: now + 1000,
  }), now), { expiresAt: now + 1000 });
});
