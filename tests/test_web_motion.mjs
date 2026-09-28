import assert from "node:assert/strict";
import test from "node:test";

import { motionAllowsAnimation } from "../src/tfr/web/motion.mjs";

test("system motion preference follows the operating system", () => {
  assert.equal(motionAllowsAnimation("system", false), true);
  assert.equal(motionAllowsAnimation("system", true), false);
});

test("explicit motion preferences override the operating system", () => {
  assert.equal(motionAllowsAnimation("reduced", false), false);
  assert.equal(motionAllowsAnimation("reduced", true), false);
  assert.equal(motionAllowsAnimation("full", false), true);
  assert.equal(motionAllowsAnimation("full", true), true);
});
