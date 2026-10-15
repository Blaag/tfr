import assert from "node:assert/strict";
import test from "node:test";

import { iphoneInstallMode, repairAppShell } from "../src/tfr/web/bootstrap.mjs";

test("detects iPhone browser and installed display modes", () => {
  const iphone = {
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
    maxTouchPoints: 5,
    standalone: false,
    displayModeStandalone: false,
  };

  assert.equal(iphoneInstallMode(iphone), "browser");
  assert.equal(iphoneInstallMode({ ...iphone, standalone: true }), "installed");
  assert.equal(iphoneInstallMode({ ...iphone, displayModeStandalone: true }), "installed");
  assert.equal(iphoneInstallMode({ ...iphone, userAgent: "Firefox", maxTouchPoints: 0 }), "other");
});

test("repairs only TFR shell caches and unregisters service workers", async () => {
  const unregistered = [];
  const deleted = [];
  await repairAppShell({
    serviceWorker: {
      async getRegistrations() {
        return [1, 2].map((id) => ({ async unregister() { unregistered.push(id); } }));
      },
    },
    cacheStorage: {
      async keys() { return ["tfr-shell-web1-v49", "unrelated-cache"]; },
      async delete(key) { deleted.push(key); },
    },
  });

  assert.deepEqual(unregistered, [1, 2]);
  assert.deepEqual(deleted, ["tfr-shell-web1-v49"]);
});
