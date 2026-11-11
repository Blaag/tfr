import assert from "node:assert/strict";
import test from "node:test";

import { runAtWindowLoad, workerDescriptor } from "../src/tfr/web/service-worker.mjs";

test("starts service-worker registration immediately after a dynamic import misses load", () => {
  let calls = 0;
  const result = runAtWindowLoad(
    { readyState: "complete" },
    { addEventListener() { throw new Error("load listener must not be used"); } },
    () => { calls += 1; },
  );
  assert.equal(result, "immediate");
  assert.equal(calls, 1);
});

test("defers service-worker registration while the document is loading", () => {
  let listener = null;
  let options = null;
  let calls = 0;
  const result = runAtWindowLoad(
    { readyState: "loading" },
    { addEventListener(_name, callback, value) { listener = callback; options = value; } },
    () => { calls += 1; },
  );
  assert.equal(result, "deferred");
  assert.deepEqual(options, { once: true });
  assert.equal(calls, 0);
  listener();
  assert.equal(calls, 1);
});

test("worker diagnostics expose only state and same-origin script path", () => {
  assert.deepEqual(
    workerDescriptor({ state: "activated", scriptURL: "https://tfr.example/sw.js?secret=no" }, "https://tfr.example"),
    { state: "activated", script_path: "/sw.js" },
  );
  assert.deepEqual(
    workerDescriptor({ state: "installing", scriptURL: "https://other.example/sw.js" }, "https://tfr.example"),
    { state: "installing", script_path: "cross-origin" },
  );
});
