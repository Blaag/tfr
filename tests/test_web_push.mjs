import assert from "node:assert/strict";
import test from "node:test";

import {
  applicationServerKeyBytes,
  enablePush,
  normalizePushPreferences,
  pushSubscriptionJSON,
} from "../src/tfr/web/push.mjs";

test("normalizes push preferences to authorized worlds", () => {
  assert.deepEqual(
    normalizePushPreferences(
      { any_activity: true, worlds: ["alpha", "other", "alpha"], mentions: false, pages: true },
      ["alpha", "beta"],
    ),
    { any_activity: true, worlds: ["alpha"], mentions: false, pages: true },
  );
  assert.deepEqual(normalizePushPreferences(null), {
    any_activity: false,
    worlds: [],
    mentions: true,
    pages: true,
  });
});

test("decodes the URL-safe VAPID application server key", () => {
  assert.deepEqual([...applicationServerKeyBytes("AQID-_8")], [1, 2, 3, 251, 255]);
});

test("validates browser push subscription serialization", () => {
  assert.deepEqual(
    pushSubscriptionJSON({
      toJSON: () => ({ endpoint: "https://push.example/1", keys: { p256dh: "key", auth: "auth" } }),
    }),
    { endpoint: "https://push.example/1", keys: { p256dh: "key", auth: "auth" } },
  );
  assert.throws(() => pushSubscriptionJSON({ toJSON: () => ({}) }), /invalid/);
});

test("failed Gateway registration rolls back a newly created browser subscription", async () => {
  let unsubscribed = false;
  const subscription = {
    toJSON: () => ({ endpoint: "https://push.example/1", keys: { p256dh: "key", auth: "auth" } }),
    async unsubscribe() { unsubscribed = true; },
  };
  const registration = {
    pushManager: {
      async getSubscription() { return null; },
      async subscribe() { return subscription; },
    },
  };
  const originalNotification = globalThis.Notification;
  globalThis.Notification = { requestPermission: async () => "granted" };
  try {
    await assert.rejects(
      enablePush({
        registration,
        publicKey: "AQID",
        preferences: normalizePushPreferences(null),
        fetchImpl: async () => ({ ok: false, status: 400, text: async () => "rejected" }),
      }),
      /rejected/,
    );
    assert.equal(unsubscribed, true);
  } finally {
    globalThis.Notification = originalNotification;
  }
});

test("permission is requested before service-worker push readiness is checked", async () => {
  let permissionRequested = false;
  const originalNotification = globalThis.Notification;
  globalThis.Notification = {
    requestPermission: async () => {
      permissionRequested = true;
      return "granted";
    },
  };
  try {
    await assert.rejects(
      enablePush({
        registration: null,
        publicKey: "AQID",
        preferences: normalizePushPreferences(null),
      }),
      /unavailable/,
    );
    assert.equal(permissionRequested, true);
  } finally {
    globalThis.Notification = originalNotification;
  }
});
