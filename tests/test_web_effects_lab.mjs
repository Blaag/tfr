import assert from "node:assert/strict";
import test from "node:test";

import {
  comboLabDefinition,
  parseEffectsLabCommand,
  validatedEffectDemos,
} from "../src/tfr/web/effects-lab.mjs";

test("parses local Effects Lab commands without accepting arbitrary input", () => {
  assert.deepEqual(parseEffectsLabCommand("/teststreak 7"), { action: "streak", target: "7" });
  assert.deepEqual(parseEffectsLabCommand("/testspeaker Bob"), { action: "speaker", target: "Bob" });
  assert.equal(parseEffectsLabCommand("say hello"), null);
});

test("provides the bounded combo catalog", () => {
  assert.equal(comboLabDefinition(3).notice, "> Speaking Spree! <");
  assert.equal(comboLabDefinition(7).color, "#ff8000");
  assert.equal(comboLabDefinition(9), null);
});

test("rejects malformed effect demo descriptors", () => {
  const valid = { id: "speaker:bob", label: "Bob", category: "speaker", samples: [{ text: "Bob waves." }] };
  assert.deepEqual(validatedEffectDemos([valid]), [{ ...valid, worlds: [] }]);
  assert.deepEqual(validatedEffectDemos([{ ...valid, category: "unsafe" }]), []);
});
