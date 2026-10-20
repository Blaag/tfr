import assert from "node:assert/strict";
import test from "node:test";

import {
  iphoneInstallMode,
  iosBrowserName,
  iosInstallInstructions,
} from "../src/tfr/web/bootstrap.mjs";
import { repairAppShell } from "../src/tfr/web/repair.mjs";

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

test("identifies the current iOS browser for installation guidance", () => {
  assert.equal(iosBrowserName("Version/18.0 Mobile/15E148 Safari/604.1"), "Safari");
  assert.equal(iosBrowserName("Mobile/15E148 CriOS/130.0.0.0"), "Chrome");
  assert.equal(iosBrowserName("Mobile/15E148 EdgiOS/130.0"), "Edge");
  assert.equal(iosBrowserName("Mobile/15E148 FxiOS/131.0"), "Firefox");
  assert.equal(iosBrowserName("Mobile browser"), "your browser");
});

test("provides browser-specific iOS installation steps and a Safari fallback", () => {
  assert.deepEqual(iosInstallInstructions("Chrome"), [
    "In Chrome, open the Share menu.",
    "Choose Add to Home Screen.",
    "Tap Add, then open TFR from its new Home Screen icon.",
    "If Add to Home Screen is unavailable, open this page in Safari.",
  ]);
  assert.equal(iosInstallInstructions("Safari").length, 3);
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
