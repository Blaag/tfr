import assert from "node:assert/strict";
import test from "node:test";

import { createConnectionLifecycle } from "../src/tfr/web/connection-lifecycle.mjs";

function fixture() {
  const timers = new Map();
  const reconnects = [];
  const timeouts = [];
  let nextTimer = 1;
  const lifecycle = createConnectionLifecycle({
    connectionTimeout: 8000,
    reconnectDelays: [500, 1000, 2000],
    setTimer(callback, delay) {
      const id = nextTimer++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    onConnectionTimeout(socket) {
      timeouts.push(socket);
    },
    onReconnect() {
      reconnects.push(true);
    },
  });
  return { lifecycle, timers, reconnects, timeouts };
}

function runTimer(timers, delay) {
  const match = [...timers].find(([, timer]) => timer.delay === delay);
  assert.ok(match, `missing ${delay}ms timer`);
  const [id, timer] = match;
  timers.delete(id);
  timer.callback();
}

test("connection deadline remains active until ready", () => {
  const { lifecycle, timers, timeouts, reconnects } = fixture();
  const socket = {};

  lifecycle.begin(socket);
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [8000]);
  runTimer(timers, 8000);

  assert.deepEqual(timeouts, [socket]);
  assert.equal(lifecycle.socket, null);
  assert.deepEqual([...timers.values()].map((timer) => timer.delay), [500]);
  runTimer(timers, 500);
  assert.equal(reconnects.length, 1);
});

test("ready cancels the deadline and resets reconnect backoff", () => {
  const { lifecycle, timers, timeouts } = fixture();
  const socket = {};

  lifecycle.reconnectAttempt = 2;
  lifecycle.begin(socket);
  assert.equal(lifecycle.markReady(socket), true);

  assert.equal(timers.size, 0);
  assert.equal(lifecycle.ready, true);
  assert.equal(lifecycle.reconnectAttempt, 0);
  assert.deepEqual(timeouts, []);
});

test("replacement connection invalidates stale close and probe work", () => {
  const { lifecycle } = fixture();
  const first = {};
  const second = {};

  lifecycle.begin(first);
  const closeToken = lifecycle.close(first);
  const probe = lifecycle.beginProbe();
  lifecycle.begin(second);

  assert.equal(lifecycle.closeIsCurrent(closeToken), false);
  assert.equal(lifecycle.probeIsCurrent(probe), false);
  assert.equal(lifecycle.socket, second);
});

test("stop cancels pending work and prevents reconnect", () => {
  const { lifecycle, timers } = fixture();
  const socket = {};

  lifecycle.begin(socket);
  lifecycle.scheduleReconnect();
  assert.equal(lifecycle.stop(), socket);

  assert.equal(lifecycle.socket, null);
  assert.equal(lifecycle.ready, false);
  assert.equal(lifecycle.intentionalClose, true);
  assert.equal(timers.size, 0);
  lifecycle.scheduleReconnect();
  assert.equal(timers.size, 0);
});
