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
  const events = Array.from({ length: 700 }, (_, index) => {
    const text = `output ${index} ${"x".repeat(80)}`;
    return {
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
      text,
      redacted: false,
      text_truncated: false,
      presentation: {
        version: 2,
        programs: [{
          start: 0,
          end: Array.from(text).length,
          duration_ms: 3000,
          repeat_ms: 3000,
          repeat_count: 1,
          frames_per_second: 20,
          variants: [{
            requires: ["character_foreground", "character_text", "timeline"],
            style: {},
            foreground_keyframes: [],
            character_reveal: {
              interval_ms: 20,
              base_color: "#d7d7d7",
              accent_color: "#ffffff",
              glitch_width: 3,
              settle_width: 3,
              seed: index,
              glitch_characters: "#%",
              inline_glitch_chance: 0.05,
            },
          }],
          reduced_motion: {},
          fallback: {},
        }],
      },
      },
    };
  });
  window.__sentCommands = [];
  class FakeWebSocket extends EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor() {
      super();
      this.readyState = FakeWebSocket.CONNECTING;
      window.__fakeSocket = this;
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
              server: "bare",
              character: "Black",
              connection_generation: "1",
              history: {
                connection_generation: "1",
                available_count: "700",
                snapshot_count: 700,
                snapshot_gap: false,
              },
            },
            {
              world: "quiet-world",
              state: "connected",
              aliases: ["quiet"],
              server: "bare",
              character: "Black",
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
          effect_demos: [
            {
              id: "speaker:bob",
              label: "Bob",
              category: "speaker",
              samples: [
                {
                  id: "demo-bob",
                  timestamp: new Date().toISOString(),
                  world: "dev-local",
                  connection_generation: "1",
                  direction: "inbound",
                  kind: "say",
                  text: 'Bob says, "Configured effect preview."',
                  redacted: false,
                  text_truncated: false,
                },
              ],
            },
            {
              id: "speaker:wedge",
              label: "Wedge",
              category: "speaker",
              samples: [
                {
                  id: "demo-wedge",
                  timestamp: new Date().toISOString(),
                  world: "dev-local",
                  connection_generation: "1",
                  direction: "inbound",
                  kind: "say",
                  text: 'Wedge says, "Portable ember preview."',
                  redacted: false,
                  text_truncated: false,
                  presentation: {
                    version: 2,
                    programs: [
                      {
                        start: 0,
                        end: 5,
                        duration_ms: 3000,
                        repeat_ms: 10000,
                        repeat_count: 2,
                        frames_per_second: 20,
                        variants: [
                          {
                            requires: ["character_foreground", "timeline"],
                            style: {},
                            foreground_keyframes: [],
                            character_palette_front: {
                              colors: ["#ffff5f", "#ffaf00", "#ff5f00", "#d70000", "#a9914a"],
                              spread: 0.55,
                              direction: "forward",
                            },
                          },
                        ],
                        reduced_motion: { foreground: "#ffaf00", bold: true },
                        fallback: { foreground: "#a9914a" },
                      },
                      {
                        start: 0,
                        end: Array.from('Wedge says, "Portable ember preview."').length,
                        duration_ms: 1200,
                        repeat_ms: 1200,
                        repeat_count: 1,
                        frames_per_second: 20,
                        variants: [{
                          requires: ["character_foreground", "character_text", "timeline"],
                          style: {},
                          foreground_keyframes: [],
                          character_reveal: {
                            interval_ms: 20,
                            base_color: "#d7d7d7",
                            accent_color: "#ffffff",
                            glitch_width: 3,
                            settle_width: 3,
                            seed: 17,
                            glitch_characters: "#%",
                            inline_glitch_chance: 0.05,
                          },
                        }],
                        reduced_motion: {},
                        fallback: {},
                      },
                    ],
                  },
                },
              ],
            },
            {
              id: "speaker:jarin",
              label: "Jarin",
              category: "speaker",
              samples: [
                {
                  id: "demo-jarin",
                  timestamp: new Date().toISOString(),
                  world: "dev-local",
                  connection_generation: "1",
                  direction: "inbound",
                  kind: "say",
                  text: 'Jarin says, "Portable rainbow preview."',
                  text_runs: [
                    { text: "Jarin", style: { foreground: "#ff0000" }, role: "speaker" },
                    { text: ' says, "Portable rainbow preview."', style: {} },
                  ],
                  redacted: false,
                  text_truncated: false,
                  presentation: {
                    version: 2,
                    programs: [
                      {
                        start: 0,
                        end: 5,
                        duration_ms: 1800,
                        repeat_ms: 10000,
                        repeat_count: 2,
                        frames_per_second: 20,
                        variants: [
                          {
                            requires: ["character_foreground", "timeline"],
                            style: {},
                            foreground_keyframes: [],
                            character_hue_wave: {
                              base_color: "#ffc9d7",
                              saturation: 0.78,
                              brightness: 1,
                              spatial_cycles: 1,
                              temporal_cycles: 1,
                            },
                          },
                        ],
                        reduced_motion: { foreground: "#ffc9d7", bold: true },
                        fallback: { foreground: "#ffc9d7" },
                      },
                      {
                        start: 0,
                        end: Array.from('Jarin says, "Portable rainbow preview."').length,
                        duration_ms: 1200,
                        repeat_ms: 1200,
                        repeat_count: 1,
                        frames_per_second: 20,
                        variants: [{
                          requires: ["character_foreground", "character_text", "timeline"],
                          style: {},
                          foreground_keyframes: [],
                          character_reveal: {
                            interval_ms: 20,
                            base_color: "#d7d7d7",
                            accent_color: "#ffffff",
                            glitch_width: 3,
                            settle_width: 3,
                            seed: 18,
                            glitch_characters: "#%",
                            inline_glitch_chance: 0.05,
                          },
                        }],
                        reduced_motion: {},
                        fallback: {},
                      },
                    ],
                  },
                },
              ],
            },
          ],
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
      if (value.type === "paste") {
        window.__pasteRequests = [...(window.__pasteRequests || []), value];
        const lines = value.text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
        if (lines.at(-1) === "") lines.pop();
        this.message({ type: "paste_started", protocol: 1, request_id: value.request_id, total_lines: lines.length });
        lines.forEach((line, index) => this.message({
          type: "paste_progress",
          protocol: 1,
          request_id: value.request_id,
          sent_lines: index + 1,
          total_lines: lines.length,
        }));
        this.message({
          type: "paste_complete",
          protocol: 1,
          request_id: value.request_id,
          sent_lines: lines.length,
          total_lines: lines.length,
        });
        return;
      }
      if (value.type !== "command" && value.type !== "ping") return;
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
  assert.equal(await page.locator("#event-list .presentation-character").count(), 0);
  assert.deepEqual(
    await page.locator("#transcript").evaluate((node) => ({
      behavior: getComputedStyle(node).scrollBehavior,
      atBottom: node.scrollHeight - node.scrollTop - node.clientHeight < 2,
    })),
    { behavior: "auto", atBottom: true },
  );
  assert.equal(
    await page.locator("#event-list .event").first().getAttribute("data-event-id"),
    "event-200",
  );
  await page.setViewportSize({ width: 390, height: 520 });
  await page.evaluate(() => {
    window.__fakeSocket.message({
      type: "event",
      protocol: 1,
      cursor: "details-scroll",
      event: {
        id: "details-scroll-event",
        timestamp: new Date().toISOString(),
        world: "dev-local",
        connection_generation: "1",
        direction: "inbound",
        kind: "say",
        text: "A detailed event for testing the mobile event sheet",
        redacted: false,
        text_truncated: false,
        spoof_status: "spoofed",
        spoof_sender: "A likely sender with a deliberately long display name",
        spoof_sender_confidence: "inferred",
        provenance: {
          sender_name: "A sender with a deliberately long display name",
          owner_name: "An owner with a deliberately long display name",
          server_source: "A deliberately long server source that wraps in portrait",
          confidence: "high",
        },
      },
    });
  });
  const detailedEvent = page.locator('[data-event-id="details-scroll-event"]');
  await detailedEvent.waitFor();
  await detailedEvent.click();
  await page.locator("#event-dialog[open]").waitFor();
  const eventSheetBody = page.locator("#event-dialog .event-sheet-body");
  assert.equal(
    await eventSheetBody.evaluate((node) => node.scrollHeight > node.clientHeight),
    true,
  );
  await eventSheetBody.evaluate((node) => { node.scrollTop = node.scrollHeight; });
  assert.equal(
    await page.locator("#event-dialog .close-button").evaluate((node) => {
      const bounds = node.getBoundingClientRect();
      return bounds.top >= 0 && bounds.bottom <= window.innerHeight;
    }),
    true,
  );
  await page.locator("#event-dialog .close-button").click();
  assert.equal(await page.locator("#event-dialog").evaluate((node) => node.open), false);
  await detailedEvent.click();
  await page.locator("#event-dialog[open]").waitFor();
  assert.equal(await eventSheetBody.evaluate((node) => node.scrollTop), 0);
  await page.locator("#event-dialog .close-button").click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator("#command-input").getAttribute("spellcheck"), "true");
  assert.equal(await page.locator("#command-input").getAttribute("autocorrect"), "on");
  await page.evaluate(() => {
    window.__fakeSocket.message({
      type: "event",
      protocol: 1,
      cursor: "activity-1",
      event: {
        id: "other-world-activity",
        timestamp: new Date().toISOString(),
        world: "quiet-world",
        connection_generation: "1",
        direction: "inbound",
        kind: "raw_output",
        text: "activity elsewhere",
        redacted: false,
        text_truncated: false,
      },
    });
  });
  await page.waitForFunction(() => document.querySelectorAll("#world-activity .unread").length === 1);
  assert.match(await page.locator("#world-activity").getAttribute("aria-label"), /1 unread message/);
  assert.equal((await page.request.get(`${origin}/combo.mjs`)).status(), 200);
  await page.locator("#motion-preference").evaluate((select) => {
    select.value = "full";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  if (await page.locator("#return-live").isVisible()) await page.locator("#return-live").click();
  await page.waitForTimeout(100);

  await page.evaluate(() => {
    for (const [index, [kind, text]] of [
      ["say", 'Black says, "warmup one"'],
      ["raw_output", "Black poses warmup two"],
    ].entries()) {
      window.__fakeSocket.message({
        type: "event",
        protocol: 1,
        cursor: String(701 + index),
        event: {
          id: `combo-warmup-${index}`,
          timestamp: new Date().toISOString(),
          world: "dev-local",
          connection_generation: "1",
          direction: "inbound",
          kind,
          text,
          redacted: false,
          text_truncated: false,
        },
      });
    }
  });
  const comboEvents = [
    ["say", 'Black says, "combo three"', "Speaking Spree!"],
    ["raw_output", "Black poses combo four", "Rampage!"],
    ["say", 'Black says, "combo five"', "Dominating!"],
    ["raw_output", "Black's pose combo six", "Unstoppable!"],
    ["say", 'Black says, "combo seven"', "GODLIKE!"],
  ];
  for (const [index, [kind, text, notice]] of comboEvents.entries()) {
    if (await page.locator("#return-live").isVisible()) await page.locator("#return-live").click();
    await page.waitForTimeout(100);
    await page.evaluate(({ index, kind, text }) => {
      window.__fakeSocket.message({
        type: "event",
        protocol: 1,
        cursor: String(703 + index),
        event: {
          id: `combo-level-${index + 3}`,
          timestamp: new Date().toISOString(),
          world: "dev-local",
          connection_generation: "1",
          direction: "inbound",
          kind,
          text,
          redacted: false,
          text_truncated: false,
        },
      });
    }, { index, kind, text });
    await page.waitForFunction(
      (expected) => document.querySelector("#combo-notice")?.textContent.includes(expected),
      notice,
    );
  }
  const streakPosition = await page.evaluate(() => {
    const notice = document.querySelector("#combo-notice").getBoundingClientRect();
    const transcript = document.querySelector("#transcript").getBoundingClientRect();
    return { noticeTop: notice.top, transcriptTop: transcript.top, transcriptBottom: transcript.bottom };
  });
  assert.ok(streakPosition.noticeTop >= streakPosition.transcriptTop);
  assert.ok(streakPosition.noticeTop < streakPosition.transcriptTop + (streakPosition.transcriptBottom - streakPosition.transcriptTop) / 2);
  const sentBeforeLab = await page.evaluate(() => window.__sentCommands.length);
  await page.locator("#settings-button").click();
  await page.locator("#effects-lab-button").click();
  await page.locator("#effects-lab[open]").waitFor();
  assert.equal(
    await page.locator("#effects-lab-command-input").getAttribute("spellcheck"),
    "false",
  );
  await page.locator("#effects-lab-combos button").getByText("Godlike", { exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#effects-lab-notice")?.textContent.includes("GODLIKE"));
  await page.locator("#effects-lab-speakers button").getByText("Bob").click();
  assert.match(await page.locator("#effects-lab-message").innerText(), /Bob says/);
  assert.match(await page.locator("#effects-lab-notice").innerText(), /Gateway did not provide/);
  await page.locator("#effects-lab-speakers button").getByText("Wedge").click();
  await page.waitForTimeout(1300);
  const emberColors = await page.locator("#effects-lab-message .presentation-character").evaluateAll(
    (nodes) => nodes.slice(0, 5).map((node) => getComputedStyle(node).color),
  );
  assert.ok(new Set(emberColors).size > 1);
  await page.waitForTimeout(500);
  const movedEmberColors = await page.locator("#effects-lab-message .presentation-character").evaluateAll(
    (nodes) => nodes.slice(0, 5).map((node) => getComputedStyle(node).color),
  );
  assert.notDeepEqual(movedEmberColors, emberColors);
  await page.waitForTimeout(1400);
  assert.deepEqual(
    await page.locator("#effects-lab-message .presentation-character").evaluateAll(
      (nodes) => nodes.slice(0, 5).map((node) => getComputedStyle(node).color),
    ),
    Array(5).fill("rgb(169, 145, 74)"),
  );
  await page.locator("#effects-lab-speakers button").getByText("Jarin").click();
  await page.waitForTimeout(1900);
  assert.deepEqual(
    await page.locator("#effects-lab-message .presentation-character").evaluateAll(
      (nodes) => nodes.slice(0, 5).map((node) => getComputedStyle(node).color),
    ),
    Array(5).fill("rgb(255, 201, 215)"),
  );
  await page.locator("#effects-lab-command-input").fill("/teststreak 4");
  await page.locator("#effects-lab-command button").click();
  assert.match(await page.locator("#effects-lab-notice").innerText(), /Rampage/);
  assert.equal(await page.evaluate(() => window.__sentCommands.length), sentBeforeLab);
  await page.locator("#effects-lab-close").click();
  await page.locator("#settings-button").click();
  await page.locator("#diagnostics-button").click();
  const diagnostics = await page.locator("#diagnostics-output").innerText();
  assert.match(diagnostics, /gateway_omitted/);
  assert.match(diagnostics, /lab_presentation_trace/);
  assert.match(diagnostics, /tracks=palette_front\+reveal/);
  assert.match(diagnostics, /tracks=hue_wave\+reveal/);
  await page.locator("#diagnostics-close").click();
  await page.locator("#settings-button").click();
  await page.locator("#spelling-suggestions").uncheck();
  assert.equal(await page.locator("#command-input").getAttribute("spellcheck"), "false");
  assert.equal(await page.locator("#command-input").getAttribute("autocorrect"), "off");
  assert.equal(
    await page.evaluate(() => localStorage.getItem("tfr.spellingSuggestions")),
    "off",
  );
  await page.locator("#automatic-say").check();
  assert.equal(await page.evaluate(() => localStorage.getItem("tfr.automaticSay")), "on");
  await page.locator("#settings-dialog .close-button").click();

  await page.locator("#command-input").fill("I love my cat");
  await page.locator("#command-input").press("Enter");
  assert.equal(await page.evaluate(() => window.__sentCommands.at(-1).text), "say I love my cat");
  await page.locator("#settings-button").click();
  await page.locator("#automatic-say").uncheck();
  await page.locator("#settings-dialog .close-button").click();

  const portraitFonts = await page.evaluate(() => ({
    transcript: getComputedStyle(document.querySelector(".event-text")).fontSize,
    input: getComputedStyle(document.querySelector("#command-input")).fontSize,
  }));
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
  assert.deepEqual(await page.evaluate(() => ({
    transcript: getComputedStyle(document.querySelector(".event-text")).fontSize,
    input: getComputedStyle(document.querySelector("#command-input")).fontSize,
  })), portraitFonts);
  await page.locator("#command-input").fill("“test");
  await page.locator("#command-input").press("Enter");
  assert.equal(await page.evaluate(() => window.__sentCommands.at(-1).text), '"test');
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#event-list .event:text-is("\\"test")').count(), 0);
  await page.locator("#history-button").click();
  assert.equal(await page.locator("#history-list button").getByText('"test', { exact: true }).count(), 1);
  await page.locator("#history-list button").getByText('"test', { exact: true }).click();
  await page.locator("#command-input").press("Enter");
  await page.locator("#history-button").dispatchEvent("pointerdown", { pointerType: "touch" });
  await page.locator("#history-dialog[open]").waitFor();
  await page.keyboard.press("Escape");

  await page.locator("#command-input").fill("draft");
  await page.locator("#command-input").evaluate((input) => {
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: { getData: () => "one\ntwo" } });
    input.dispatchEvent(event);
  });
  assert.equal(await page.locator("#command-input").inputValue(), "draft");
  await page.locator("#paste-dialog[open]").waitFor();
  assert.match(await page.locator("#paste-summary").innerText(), /2 lines/);
  assert.equal(await page.locator("#paste-preview").textContent(), "one\ntwo");
  await page.locator("#paste-confirm").click();
  await page.waitForFunction(() => document.querySelector("#paste-status")?.textContent.includes("accepted all 2"));
  assert.deepEqual(
    await page.evaluate(() => ({
      world: window.__pasteRequests.at(-1).world,
      text: window.__pasteRequests.at(-1).text,
    })),
    { world: "dev-local", text: "one\ntwo" },
  );
  await page.locator("#paste-cancel").click();

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

  const chromePairingContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/130.0.0.0 Mobile/15E148 Safari/604.1",
  });
  await chromePairingContext.addInitScript(() => {
    localStorage.setItem("tfr.installHintDismissed", "yes");
  });
  const chromePairingPage = await chromePairingContext.newPage();
  await chromePairingPage.route("**/api/session", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"paired":true}' }),
  );
  await chromePairingPage.goto(`${origin}/?pairing=complete`);
  await chromePairingPage.locator("#install-onboarding:not([hidden])").waitFor();
  assert.match(
    await chromePairingPage.locator("#install-onboarding-steps").innerText(),
    /In Chrome, open the Share menu/,
  );
  assert.equal(await chromePairingPage.locator("#console").isHidden(), true);
  await chromePairingContext.close();

  const recoveryContext = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
  });
  const recoveryPage = await recoveryContext.newPage();
  await recoveryPage.route("**/app.mjs", (route) =>
    route.fulfill({ status: 503, contentType: "text/javascript", body: "unavailable" }),
  );
  await recoveryPage.goto(origin);
  await recoveryPage.locator("#startup-actions:not([hidden])").waitFor();
  assert.match(await recoveryPage.locator("#startup-status").innerText(), /could not load/);
  assert.equal(await recoveryPage.locator("#startup-repair").getAttribute("href"), "/repair.html");
  assert.equal(await recoveryPage.locator("#ios-install-help").getAttribute("hidden"), null);
  assert.equal(await recoveryPage.locator("#ios-install-notice").getAttribute("hidden"), null);
  await recoveryContext.close();

  console.log("Playwright WebKit mobile UI checks passed");
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
