import assert from "node:assert/strict";
import test from "node:test";

import {
  commandWithAutomaticSay,
  isMultilineWorldCommand,
  normalizeWorldCommand,
} from "../src/tfr/web/command.mjs";

test("normalizes an iOS smart quote used as a MUSH say prefix", () => {
  assert.equal(normalizeWorldCommand('“test'), '"test');
  assert.equal(normalizeWorldCommand('”test'), '"test');
});

test("does not alter quotes outside the command prefix", () => {
  assert.equal(normalizeWorldCommand('say “test”'), 'say “test”');
  assert.equal(normalizeWorldCommand(':waves'), ':waves');
});

test("automatic say prefixes ordinary text and preserves explicit commands", () => {
  assert.equal(commandWithAutomaticSay("I love my cat", true), "say I love my cat");
  assert.equal(commandWithAutomaticSay("please stay", true), "say please stay");
  for (const command of [
    ":waves", ";waves", "@pemit me=hello", "p waves", "po waves", "pos waves",
    "pose waves", "sa hello", "say hello",
  ]) {
    assert.equal(commandWithAutomaticSay(command, true), command);
  }
  assert.equal(commandWithAutomaticSay("  :waves", true), "  :waves");
  assert.equal(commandWithAutomaticSay("hello", false), "hello");
});

test("detects multiline command paste across newline conventions", () => {
  assert.equal(isMultilineWorldCommand("one line"), false);
  assert.equal(isMultilineWorldCommand("one\ntwo"), true);
  assert.equal(isMultilineWorldCommand("one\rtwo"), true);
  assert.equal(isMultilineWorldCommand("one\r\ntwo"), true);
});
