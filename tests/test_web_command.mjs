import assert from "node:assert/strict";
import test from "node:test";

import { normalizeWorldCommand } from "../src/tfr/web/command.mjs";

test("normalizes an iOS smart quote used as a MUSH say prefix", () => {
  assert.equal(normalizeWorldCommand('“test'), '"test');
  assert.equal(normalizeWorldCommand('”test'), '"test');
});

test("does not alter quotes outside the command prefix", () => {
  assert.equal(normalizeWorldCommand('say “test”'), 'say “test”');
  assert.equal(normalizeWorldCommand(':waves'), ':waves');
});
