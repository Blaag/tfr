import assert from "node:assert/strict";

import { webkit } from "playwright";

const origin = process.env.TFR_PWA_ORIGIN?.replace(/\/$/, "");
const pairingUrl = process.env.TFR_PAIRING_URL;
assert.ok(origin, "TFR_PWA_ORIGIN is required");
assert.ok(pairingUrl, "TFR_PAIRING_URL is required");

const pairing = new URL(pairingUrl);
assert.equal(pairing.origin, origin, "pairing URL must use TFR_PWA_ORIGIN");
const code = new URLSearchParams(pairing.hash.slice(1)).get("pair");
assert.ok(code, "pairing URL has no pair code");

const browser = await webkit.launch();
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});
const page = await context.newPage();
const protocolMessages = [];
const browserErrors = [];
let paired = false;

page.on("console", (message) => {
  if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
});
page.on("pageerror", (error) => browserErrors.push(`page: ${error.message}`));
page.on("websocket", (socket) => {
  socket.on("framereceived", ({ payload }) => {
    if (typeof payload !== "string") return;
    try {
      protocolMessages.push(JSON.parse(payload));
    } catch {
      browserErrors.push("WebSocket delivered invalid JSON");
    }
  });
});

async function send(command, expectedText) {
  await page.locator("#command-input").fill(command);
  await page.locator("#composer").evaluate((form) => form.requestSubmit());
  await page.waitForFunction(
    (text) => document.querySelector("#event-list .event:last-child")?.innerText.includes(text),
    expectedText,
  );
}

try {
  const response = await context.request.post(`${origin}/api/pair`, {
    headers: { Origin: origin },
    data: { code },
  });
  assert.equal(response.ok(), true, `pairing failed with ${response.status()}`);
  paired = true;

  await page.goto(origin);
  await page.locator("#connection-state").getByText("Live").waitFor({ timeout: 30_000 });
  await page.locator("#world-name").getByText("dev-local").waitFor();

  await send("pulse", "portable pulse");
  const event = protocolMessages.find(
    (message) => message.type === "event" && message.event?.text.includes("portable pulse"),
  );
  assert.ok(event, "speaker event was not captured from the WebSocket");
  assert.equal(event.event.provenance.sender_name, "Alice");
  assert.equal(event.event.presentation.version, 1);
  const program = event.event.presentation.programs[0];
  assert.deepEqual([program.start, program.end], [0, 5]);
  assert.deepEqual(program.variants[0].requires, [
    "character_foreground",
    "timeline",
    "character_case",
  ]);
  assert.deepEqual(program.variants[0].character_sweep.positions, [
    { at: 0, position: 0 },
    { at: 0.5, position: 1 },
    { at: 1, position: 0 },
  ]);

  const renderedEvent = page.locator("#event-list .event").filter({ hasText: "portable pulse" });
  const target = renderedEvent.locator(".presentation-target");
  const characters = target.locator(".presentation-character");
  await target.waitFor();
  assert.equal(await target.innerText(), "Alice");
  assert.equal(await characters.count(), 5);
  assert.equal(await target.evaluate((element) => element.getAnimations().length), 1);

  const seen = new Set();
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline && !(seen.has(0) && seen.has(4))) {
    const frame = await characters.evaluateAll((nodes) => {
      const colors = nodes.map((node) => getComputedStyle(node).color);
      return {
        head: colors.findIndex((color) => color === "rgb(255, 0, 0)"),
        text: nodes.map((node) => node.textContent).join(""),
      };
    });
    if (frame.head >= 0) {
      seen.add(frame.head);
      assert.equal(frame.text, "Alice");
    }
    await page.waitForTimeout(40);
  }
  assert.ok(seen.has(0) && seen.has(4), "Cylon head did not traverse the complete name");

  await page.locator("#settings-button").click();
  await page.locator("#motion-preference").selectOption("reduced");
  await page.locator("#settings-dialog .close-button").click();
  assert.equal(await target.evaluate((element) => element.getAnimations().length), 0);
  assert.equal(await target.innerText(), "Alice");
  assert.equal(await target.evaluate((element) => getComputedStyle(element).fontWeight), "700");

  await send("links", "https://example.com/path?q=1");
  const linksEvent = page.locator("#event-list .event").filter({ hasText: "links:" });
  assert.equal(await linksEvent.locator('a[href="https://example.com/path?q=1"]').count(), 1);
  assert.equal(await linksEvent.locator('a[href="http://example.org/end"]').count(), 1);

  await send("unicode", "unicode:");
  assert.match(await page.locator("#event-list .event:last-child").innerText(), /café 雪 ☃ 👩🏽‍💻 é/);

  await send("ansi", "<script>text only</script>");
  const ansiEvent = page.locator("#event-list .event:last-child");
  assert.match(await ansiEvent.innerText(), /red styled safe-link-text <script>text only<\/script>/);
  assert.equal(await ansiEvent.locator("script").count(), 0);

  await send("stress 5000", "stress 5000:");
  assert.equal(await page.locator("#event-list .event").count(), 500);
  assert.equal((await page.locator("#connection-state").innerText()).toLowerCase(), "live");
  assert.doesNotMatch(await page.locator("#toast").innerText(), /overflow/i);

  assert.deepEqual(browserErrors, []);
  console.log(
    JSON.stringify({
      origin,
      engine: "webkit",
      mobileViewport: true,
      cylonHeads: [...seen].sort(),
      reducedMotion: true,
      safeText: true,
      boundedBurst: 5_000,
    }),
  );
} finally {
  if (paired) {
    await context.request
      .post(`${origin}/api/logout`, { headers: { Origin: origin } })
      .catch(() => {});
  }
  await browser.close();
}
