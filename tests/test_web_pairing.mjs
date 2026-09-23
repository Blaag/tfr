import assert from "node:assert/strict";
import test from "node:test";

import { createPairingCoordinator, pairingResponse } from "../src/tfr/web/pairing.mjs";

test("startup and resume share one pairing request", async () => {
  let finishPairing;
  const calls = [];
  const coordinator = createPairingCoordinator((code) => {
    calls.push(code);
    return new Promise((resolve) => {
      finishPairing = resolve;
    });
  });
  coordinator.setCode("single-use-secret");

  assert.equal(await coordinator.resume(), "initializing");
  const startup = coordinator.attempt();
  coordinator.finishInitialization();
  const resumed = coordinator.resume();

  assert.deepEqual(calls, ["single-use-secret"]);
  finishPairing("paired");
  assert.equal(await startup, "paired");
  assert.equal(await resumed, "paired");
  assert.equal(coordinator.code, null);
});

test("an unreachable pairing remains available for retry", async () => {
  const results = ["unreachable", "paired"];
  const coordinator = createPairingCoordinator(async () => results.shift());
  coordinator.setCode("retry-secret");
  coordinator.finishInitialization();

  assert.equal(await coordinator.resume(), "unreachable");
  assert.equal(coordinator.code, "retry-secret");
  assert.equal(await coordinator.resume(), "paired");
  assert.equal(coordinator.code, null);
});

test("a resume during initialization is drained after an unreachable attempt", async () => {
  const results = ["unreachable", "paired"];
  const calls = [];
  const coordinator = createPairingCoordinator(async (code) => {
    calls.push(code);
    return results.shift();
  });
  coordinator.setCode("pending-resume-secret");

  assert.equal(await coordinator.resume(), "initializing");
  assert.equal(await coordinator.attempt(), "unreachable");
  assert.equal(coordinator.finishInitialization(), true);
  assert.equal(await coordinator.resume(), "paired");
  assert.deepEqual(calls, ["pending-resume-secret", "pending-resume-secret"]);
});

test("a truncated successful response remains retryable", async () => {
  const outcome = await pairingResponse({
    ok: true,
    async json() {
      throw new SyntaxError("truncated response");
    },
  });

  assert.deepEqual(outcome, { result: "unreachable" });
});

test("temporary failures and unexpected JSON remain controlled", async () => {
  assert.deepEqual(
    await pairingResponse({ ok: false, status: 429, async json() { return null; } }),
    { result: "unreachable" },
  );
  assert.deepEqual(
    await pairingResponse({ ok: false, status: 401, async json() { return null; } }),
    { result: "rejected", message: "Pairing failed" },
  );
});
