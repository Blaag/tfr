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

const server = createServer(async (request, response) => {
  const pathname = new URL(request.url, "http://127.0.0.1").pathname;
  const name = pathname === "/" ? "index.html" : pathname.slice(1);
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
const origin = `http://127.0.0.1:${port}`;

const browser = await webkit.launch();
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});
await context.addInitScript(() => {
  const events = Array.from({ length: 700 }, (_, index) => ({
    type: "event",
    protocol: 1,
    cursor: String(index + 1),
    event: {
      id: `event-${index}`,
      timestamp: new Date(1_700_000_000_000 + index * 1_000).toISOString(),
      world: "dev-local",
      connection_generation: "1",
      direction: "inbound",
      kind: "raw_output",
      text: `output ${index} ${"x".repeat(80)}`,
      redacted: false,
      text_truncated: false,
    },
  }));
  window.__sentCommands = [];
  class FakeWebSocket extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor() {
      super();
      this.readyState = FakeWebSocket.CONNECTING;
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.dispatchEvent(new Event("open"));
        this.message({
          type: "hello",
          protocol: 1,
          gateway_id: "92716400-4bb9-43d2-845f-b8a0e51c9994",
          build: { version: "test", commit: "a".repeat(40), protocol: 2 },
          worlds: [
            {
              world: "dev-local",
              state: "connected",
              aliases: ["dev"],
              connection_generation: "1",
              history: {
                connection_generation: "1",
                available_count: "700",
                snapshot_count: 700,
                snapshot_gap: false,
              },
            },
          ],
          history_reset: false,
          history_truncated: false,
        });
        for (const event of events) this.message(event);
        this.message({ type: "ready", protocol: 1, cursor: "700" });
      });
    }

    message(value) {
      this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) }));
    }

    send(serialized) {
      const value = JSON.parse(serialized);
      this.message({ type: "ack", protocol: 1, request_id: value.request_id, ok: true });
      if (value.type !== "command") return;
      window.__sentCommands.push(value);
      this.message({
        type: "event",
        protocol: 1,
        cursor: "701",
        event: {
          id: `sent-${value.request_id}`,
          timestamp: new Date().toISOString(),
          world: value.world,
          connection_generation: "1",
          direction: "outbound",
          kind: "command",
          text: value.text,
          redacted: false,
          text_truncated: false,
        },
      });
    }

    close() {
      this.readyState = FakeWebSocket.CLOSED;
      this.dispatchEvent(new CloseEvent("close", { code: 1_000 }));
    }
  }
  window.WebSocket = FakeWebSocket;
});

const page = await context.newPage();
await page.route("**/api/session", (route) =>
  route.fulfill({ status: 200, contentType: "application/json", body: '{"paired":true}' }),
);

async function geometry() {
  return page.evaluate(() => {
    const composer = document.querySelector("#composer").getBoundingClientRect();
    const transcript = document.querySelector("#transcript").getBoundingClientRect();
    return {
      composerHeight: composer.height,
      composerTop: composer.top,
      composerBottom: composer.bottom,
      transcriptBottom: transcript.bottom,
      viewportHeight: window.visualViewport?.height || window.innerHeight,
    };
  });
}

function assertGeometry(value, label) {
  assert.ok(value.composerHeight > 35, `${label}: composer has no height`);
  assert.ok(
    value.composerTop >= value.transcriptBottom - 1,
    `${label}: composer overlaps transcript`,
  );
  assert.ok(value.composerBottom <= value.viewportHeight + 1, `${label}: composer leaves viewport`);
}

try {
  await page.goto(origin);
  await page.locator("#connection-state").getByText("Live").waitFor();
  await page.waitForFunction(() => document.querySelectorAll("#event-list .event").length === 500);
  assertGeometry(await geometry(), "portrait");
  assert.equal(await page.locator("#event-list .event").count(), 500);
  assert.equal(
    await page.locator("#event-list .event").first().getAttribute("data-event-id"),
    "event-200",
  );

  await page.setViewportSize({ width: 390, height: 520 });
  await page.locator("#command-input").focus();
  await page.waitForTimeout(350);
  assertGeometry(await geometry(), "keyboard-sized portrait");

  await page.setViewportSize({ width: 844, height: 240 });
  await page.locator("#command-input").focus();
  await page.waitForTimeout(350);
  assertGeometry(await geometry(), "keyboard-sized landscape");
  assert.equal(await page.locator(".topbar").evaluate((node) => getComputedStyle(node).display), "none");
  assert.equal(
    await page.locator("#history-button").evaluate((node) => getComputedStyle(node).display),
    "none",
  );

  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#command-input").fill("“test");
  await page.locator("#command-input").press("Enter");
  assert.equal(await page.evaluate(() => window.__sentCommands.at(-1).text), '"test');

  await page.locator("#command-input").fill("draft");
  await page.locator("#command-input").evaluate((input) => {
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: { getData: () => "one\ntwo" } });
    input.dispatchEvent(event);
  });
  assert.equal(await page.locator("#command-input").inputValue(), "draft");
  assert.match(await page.locator("#toast").innerText(), /Multiline paste blocked/);

  const unpairedContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  const unpairedPage = await unpairedContext.newPage();
  await unpairedPage.route("**/api/session", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"paired":false}' }),
  );
  let pairingPost = null;
  await unpairedPage.route("**/pair", async (route) => {
    pairingPost = route.request().postData();
    await route.fulfill({ status: 200, contentType: "text/html", body: "paired" });
  });
  await unpairedPage.goto(origin);
  await unpairedPage.locator("#pairing:not([hidden])").waitFor();
  await unpairedPage.locator("#pairing-link").fill(`${origin}/#pair=installed-app-secret`);
  await unpairedPage.locator("#pairing-form button").click();
  await unpairedPage.waitForURL(`${origin}/pair`);
  assert.equal(pairingPost, "code=installed-app-secret");
  await unpairedContext.close();

  console.log("Playwright WebKit mobile UI checks passed");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
