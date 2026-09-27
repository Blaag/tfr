import assert from "node:assert/strict";
import test from "node:test";

import { linkParts } from "../src/tfr/web/linkify.mjs";

test("finds safe HTTP links and preserves surrounding text", () => {
  assert.deepEqual(linkParts("See https://example.com/path?q=1 now"), [
    { type: "text", text: "See " },
    {
      type: "link",
      text: "https://example.com/path?q=1",
      href: "https://example.com/path?q=1",
    },
    { type: "text", text: " now" },
  ]);
});

test("keeps sentence punctuation outside links", () => {
  assert.deepEqual(linkParts("Open (https://example.com/a_(b)). Next."), [
    { type: "text", text: "Open (" },
    {
      type: "link",
      text: "https://example.com/a_(b)",
      href: "https://example.com/a_(b)",
    },
    { type: "text", text: ")." },
    { type: "text", text: " Next." },
  ]);
  assert.deepEqual(linkParts("See (https://example.com/foo.)"), [
    { type: "text", text: "See (" },
    { type: "link", text: "https://example.com/foo", href: "https://example.com/foo" },
    { type: "text", text: ".)" },
  ]);
});

test("does not turn non-HTTP schemes or HTML into links", () => {
  assert.deepEqual(linkParts('<img src=x> javascript:alert(1) ftp://example.com'), [
    { type: "text", text: '<img src=x> javascript:alert(1) ftp://example.com' },
  ]);
});

test("rejects deceptive credential-bearing and bidirectional URLs", () => {
  assert.deepEqual(linkParts("https://trusted.example@evil.example/"), [
    { type: "text", text: "https://trusted.example@evil.example/" },
  ]);
  assert.deepEqual(linkParts("https://example.com/\u202eevil"), [
    { type: "text", text: "https://example.com/\u202eevil" },
  ]);
});

test("trims many unmatched closing brackets in linear time", () => {
  const closers = ")".repeat(8_000);
  assert.deepEqual(linkParts(`https://example.com/${closers}`), [
    { type: "link", text: "https://example.com/", href: "https://example.com/" },
    { type: "text", text: closers },
  ]);
});
