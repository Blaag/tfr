import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { webkit } from "playwright";

const webRoot = fileURLToPath(new URL("../../src/tfr/web/", import.meta.url));
const contentTypes = {
  ".css": "text/css",
  ".html": "text/html",
  ".mjs": "text/javascript",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};
let sessionChecks = 0;

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  if (url.pathname === "/api/session") {
    sessionChecks += 1;
    response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    response.end(
      sessionChecks === 1
        ? '{"paired":true,"device":{"id":"00000000-0000-0000-0000-000000000001"}}'
        : '{"paired":false,"device":null}',
    );
    return;
  }
  const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  try {
    const body = await readFile(join(webRoot, name));
    response.writeHead(200, {
      "Content-Type": contentTypes[extname(name)] || "application/octet-stream",
    });
    response.end(body);
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const { port } = server.address();

const browser = await webkit.launch();
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});
await context.addInitScript(() => {
  window.__socketCount = 0;
  class ExpiringWebSocket extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor() {
      super();
      window.__socketCount += 1;
      this.readyState = ExpiringWebSocket.CONNECTING;
      queueMicrotask(() => {
        this.readyState = ExpiringWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
        this.message({
          type: "hello",
          protocol: 1,
          gateway_id: "92716400-4bb9-43d2-845f-b8a0e51c9994",
          worlds: [
            {
              world: "dev-local",
              state: "connected",
              aliases: ["dev"],
              connection_generation: "1",
              history: {
                connection_generation: "1",
                available_count: "0",
                snapshot_count: 0,
                snapshot_gap: false,
              },
            },
          ],
          history_reset: false,
          history_truncated: false,
        });
        this.message({ type: "ready", protocol: 1, cursor: "0" });
        setTimeout(() => {
          this.readyState = ExpiringWebSocket.CLOSED;
          this.dispatchEvent(
            new CloseEvent("close", { code: 1_008, reason: "Device session expired" }),
          );
        }, 50);
      });
    }

    message(value) {
      this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
    }

    send() {}

    close() {
      this.readyState = ExpiringWebSocket.CLOSED;
    }
  }
  window.WebSocket = ExpiringWebSocket;
});

const page = await context.newPage();
try {
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.locator("#pairing:not([hidden])").waitFor();
  assert.equal(await page.locator("#console").isHidden(), true);
  assert.match(await page.locator("#pairing-message").innerText(), /session expired or was revoked/i);
  assert.equal(await page.evaluate(() => window.__socketCount), 1);
  assert.equal(await page.evaluate(() => localStorage.getItem("tfr.gatewayId")), null);
  assert.equal(await page.evaluate(() => localStorage.getItem("tfr.selectedWorld")), null);
  await page.waitForTimeout(800);
  assert.equal(await page.evaluate(() => window.__socketCount), 1, "expired session reconnected");
  console.log("Playwright WebKit expiration checks passed");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
