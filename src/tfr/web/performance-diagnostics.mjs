const STORAGE_KEY = "tfr.performanceInstrumentation";
const ENABLE_DURATION_MS = 24 * 60 * 60 * 1000;
const EVENT_LOOP_INTERVAL_MS = 5000;
const FRAME_SAMPLE_INTERVAL_MS = 30000;
const FRAME_SAMPLE_DURATION_MS = 1000;
const MAX_SAMPLES = 256;
const DURATION_NAMES = new Set(["message_processing", "statistics_render", "transcript_render"]);
const COUNTER_NAMES = new Set([
  "events_processed",
  "reconnect_attempts",
  "screen_clear_animations",
  "statistics_renders",
  "transcript_renders",
]);
const HIGH_WATER_NAMES = new Set([
  "active_animations",
  "event_nodes",
  "presentation_characters",
  "transcript_nodes",
]);

function boundedNumber(value) {
  return Number.isFinite(value) && value >= 0 ? Math.min(value, Number.MAX_SAFE_INTEGER) : 0;
}

function round(value) {
  return Math.round(boundedNumber(value) * 10) / 10;
}

function percentile(values, fraction) {
  if (!values.length) return 0;
  const ordered = [...values].sort((left, right) => left - right);
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * fraction) - 1)];
}

function appendSample(values, value) {
  values.push(boundedNumber(value));
  if (values.length > MAX_SAMPLES) values.splice(0, values.length - MAX_SAMPLES);
}

export function parsePerformancePreference(raw, now = Date.now()) {
  try {
    const value = JSON.parse(raw);
    if (
      !value
      || value.version !== 1
      || !Number.isSafeInteger(value.expires_at)
      || value.expires_at <= now
      || value.expires_at > now + ENABLE_DURATION_MS
    ) return null;
    return { expiresAt: value.expires_at };
  } catch {
    return null;
  }
}

export function createPerformanceInstrumentation({
  window: browserWindow,
  document: browserDocument,
  storage,
  dateNow = () => Date.now(),
  monotonicNow = () => performance.now(),
  Observer = globalThis.PerformanceObserver,
} = {}) {
  let enabled = false;
  let expiresAt = null;
  let startedAt = null;
  let eventLoopTimer = null;
  let frameTimer = null;
  let frameRequest = null;
  let observer = null;
  let expectedProbe = 0;
  let eventLoopDelays = [];
  let frameDurations = [];
  let longTaskDurations = [];
  let durations = {};
  let counters = {};
  let highWater = {};

  function readPreference() {
    try {
      return parsePerformancePreference(storage?.getItem(STORAGE_KEY), dateNow());
    } catch {
      return null;
    }
  }

  function storePreference() {
    try {
      storage?.setItem(STORAGE_KEY, JSON.stringify({ version: 1, expires_at: expiresAt }));
    } catch {
      // Persistent storage is optional.
    }
  }

  function removePreference() {
    try {
      storage?.removeItem(STORAGE_KEY);
    } catch {
      // Persistent storage is optional.
    }
  }

  function reset() {
    startedAt = enabled ? new Date(dateNow()).toISOString() : null;
    eventLoopDelays = [];
    frameDurations = [];
    longTaskDurations = [];
    durations = {};
    counters = {};
    highWater = {};
  }

  function sampleFrames() {
    if (
      !enabled
      || frameRequest !== null
      || browserDocument?.visibilityState !== "visible"
      || !browserWindow?.requestAnimationFrame
    ) return;
    const sampleStarted = monotonicNow();
    let previous = sampleStarted;
    const frame = () => {
      if (!enabled) return;
      const now = monotonicNow();
      appendSample(frameDurations, now - previous);
      previous = now;
      if (now - sampleStarted < FRAME_SAMPLE_DURATION_MS) {
        frameRequest = browserWindow.requestAnimationFrame(frame);
      } else {
        frameRequest = null;
      }
    };
    frameRequest = browserWindow.requestAnimationFrame(frame);
  }

  function start() {
    if (enabled) return;
    enabled = true;
    reset();
    expectedProbe = monotonicNow() + EVENT_LOOP_INTERVAL_MS;
    eventLoopTimer = browserWindow?.setInterval?.(() => {
      const now = monotonicNow();
      if (browserDocument?.visibilityState === "visible") {
        appendSample(eventLoopDelays, Math.max(0, now - expectedProbe));
      }
      expectedProbe = now + EVENT_LOOP_INTERVAL_MS;
    }, EVENT_LOOP_INTERVAL_MS) ?? null;
    frameTimer = browserWindow?.setInterval?.(sampleFrames, FRAME_SAMPLE_INTERVAL_MS) ?? null;
    sampleFrames();
    if (typeof Observer === "function") {
      try {
        observer = new Observer((list) => {
          for (const entry of list.getEntries()) appendSample(longTaskDurations, entry.duration);
        });
        observer.observe({ type: "longtask", buffered: true });
      } catch {
        observer = null;
      }
    }
  }

  function stop() {
    if (eventLoopTimer !== null) browserWindow?.clearInterval?.(eventLoopTimer);
    if (frameTimer !== null) browserWindow?.clearInterval?.(frameTimer);
    if (frameRequest !== null) browserWindow?.cancelAnimationFrame?.(frameRequest);
    observer?.disconnect();
    eventLoopTimer = null;
    frameTimer = null;
    frameRequest = null;
    observer = null;
    enabled = false;
  }

  function enable() {
    stop();
    expiresAt = dateNow() + ENABLE_DURATION_MS;
    storePreference();
    start();
  }

  function disable() {
    stop();
    expiresAt = null;
    removePreference();
    reset();
  }

  function restore() {
    const preference = readPreference();
    if (!preference) {
      removePreference();
      return false;
    }
    expiresAt = preference.expiresAt;
    start();
    return true;
  }

  function ensureCurrent() {
    if (enabled && expiresAt <= dateNow()) disable();
    return enabled;
  }

  function recordDuration(name, value) {
    if (!ensureCurrent() || !DURATION_NAMES.has(name)) return;
    durations[name] ||= [];
    appendSample(durations[name], value);
  }

  function increment(name, amount = 1) {
    if (!ensureCurrent() || !COUNTER_NAMES.has(name)) return;
    counters[name] = boundedNumber((counters[name] || 0) + amount);
  }

  function observeHighWater(name, value) {
    if (!ensureCurrent() || !HIGH_WATER_NAMES.has(name)) return;
    highWater[name] = Math.max(highWater[name] || 0, boundedNumber(value));
  }

  function durationReport() {
    return Object.fromEntries(Object.entries(durations).map(([name, values]) => [name, {
      samples: values.length,
      p95_ms: round(percentile(values, 0.95)),
      max_ms: round(Math.max(0, ...values)),
    }]));
  }

  function report() {
    if (!ensureCurrent()) return { status: "disabled" };
    return {
      status: "enabled",
      expires_at: new Date(expiresAt).toISOString(),
      window_started_at: startedAt,
      page_state: browserDocument?.visibilityState || "unknown",
      support: { long_tasks: observer !== null },
      event_loop: {
        samples: eventLoopDelays.length,
        p95_delay_ms: round(percentile(eventLoopDelays, 0.95)),
        max_delay_ms: round(Math.max(0, ...eventLoopDelays)),
      },
      frames: {
        samples: frameDurations.length,
        stalls_over_50_ms: frameDurations.filter((value) => value > 50).length,
        max_duration_ms: round(Math.max(0, ...frameDurations)),
      },
      long_tasks: {
        count: longTaskDurations.length,
        total_ms: round(longTaskDurations.reduce((sum, value) => sum + value, 0)),
        max_ms: round(Math.max(0, ...longTaskDurations)),
      },
      operations: durationReport(),
      counters: { ...counters },
      high_water: { ...highWater },
    };
  }

  return {
    disable,
    enable,
    increment,
    isEnabled: () => ensureCurrent(),
    observeHighWater,
    recordDuration,
    report,
    reset,
    restore,
  };
}

export const performanceInstrumentationStorageKey = STORAGE_KEY;
