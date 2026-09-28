import assert from "node:assert/strict";
import test from "node:test";

import {
  installTextRunColors,
  textRunClassNames,
  validatedTextRuns,
} from "../src/tfr/web/text-runs.mjs";

test("accepts exact text with allowlisted styles and speaker role", () => {
  const event = {
    text: "Alice waves",
    text_runs: [
      {
        text: "Alice",
        role: "speaker",
        style: { foreground: "#ff5555", bold: true },
      },
      { text: " waves", style: { italic: true, underline: true } },
    ],
  };

  assert.deepEqual(validatedTextRuns(event), [
    {
      text: "Alice",
      role: "speaker",
      style: { foreground: "#ff5555", bold: true },
    },
    {
      text: " waves",
      role: undefined,
      style: { italic: true, underline: true },
    },
  ]);
});

test("rejects text mismatches and arbitrary style data", () => {
  assert.equal(
    validatedTextRuns({ text: "safe", text_runs: [{ text: "other", style: {} }] }),
    null,
  );
  for (const style of [
    { foreground: "red" },
    { background: "url(javascript:alert(1))" },
    { bold: false },
    { position: "fixed" },
  ]) {
    assert.equal(validatedTextRuns({ text: "safe", text_runs: [{ text: "safe", style }] }), null);
  }
});

test("treats hostile HTML and Unicode as ordinary text", () => {
  const text = '<img src=x onerror="alert(1)"> 雪 👩🏽‍💻';
  assert.deepEqual(validatedTextRuns({ text, text_runs: [{ text, style: {} }] }), [
    { text, role: undefined, style: {} },
  ]);
});

test("maps validated styles to bounded classes and same-origin CSS rules", () => {
  const style = {
    foreground: "#ff5555",
    background: "#010203",
    bold: true,
    italic: true,
    underline: true,
  };
  const sheet = {
    cssRules: [],
    insertRule(rule) {
      this.cssRules.push(rule);
    },
  };
  const installed = new Set();

  installTextRunColors(style, sheet, installed);
  installTextRunColors(style, sheet, installed);

  assert.deepEqual(textRunClassNames(style), [
    "ansi-fg-ff5555",
    "ansi-bg-010203",
    "ansi-bold",
    "ansi-italic",
    "ansi-underline",
  ]);
  assert.deepEqual(sheet.cssRules, [
    ".ansi-fg-ff5555 { color: #ff5555; }",
    ".ansi-bg-010203 { background-color: #010203; }",
  ]);
});

test("maps ANSI bold red protocol output to a red browser rule", () => {
  const event = {
    text: "hi\r\n",
    text_runs: [
      { text: "hi", style: { foreground: "#aa0000", bold: true } },
      { text: "\r\n", style: {} },
    ],
  };
  const runs = validatedTextRuns(event);
  const sheet = {
    cssRules: [],
    insertRule(rule) {
      this.cssRules.push(rule);
    },
  };

  assert.ok(runs);
  installTextRunColors(runs[0].style, sheet, new Set());

  assert.deepEqual(textRunClassNames(runs[0].style), ["ansi-fg-aa0000", "ansi-bold"]);
  assert.deepEqual(sheet.cssRules, [".ansi-fg-aa0000 { color: #aa0000; }"]);
});

test("bounds installed truecolor rules", () => {
  const sheet = {
    cssRules: [],
    insertRule(rule) {
      this.cssRules.push(rule);
    },
  };
  const installed = new Set(Array.from({ length: 512 }, (_, index) => `existing-${index}`));

  installTextRunColors({ foreground: "#123456" }, sheet, installed);

  assert.deepEqual(sheet.cssRules, []);
  assert.equal(installed.size, 512);
});
