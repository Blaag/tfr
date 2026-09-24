import assert from "node:assert/strict";
import test from "node:test";

import { createPairingSubmission, submitPairing } from "../src/tfr/web/pairing.mjs";

test("pairing uses a hidden top-level form post", () => {
  const created = [];
  const body = {
    children: [],
    append(element) {
      this.children.push(element);
    },
  };
  const document = {
    body,
    createElement(tagName) {
      const element = {
        tagName,
        children: [],
        append(child) {
          this.children.push(child);
        },
        submit() {
          this.submitted = true;
        },
      };
      created.push(element);
      return element;
    },
  };

  submitPairing(document, "fragment-secret");

  const [form, input] = created;
  assert.equal(form.method, "post");
  assert.equal(form.action, "/pair");
  assert.equal(form.hidden, true);
  assert.equal(form.submitted, true);
  assert.deepEqual(body.children, [form]);
  assert.deepEqual(form.children, [input]);
  assert.equal(input.type, "hidden");
  assert.equal(input.name, "code");
  assert.equal(input.value, "fragment-secret");
});

test("concurrent lifecycle events schedule one pairing navigation", () => {
  const submitted = [];
  const scheduled = [];
  const submission = createPairingSubmission(
    (code) => submitted.push(code),
    (callback, delay) => scheduled.push({ callback, delay }),
  );

  assert.equal(submission.start("secret", 5000), true);
  assert.equal(submission.start("secret", 0), false);
  assert.deepEqual(submitted, []);
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 5000);

  scheduled[0].callback();
  assert.deepEqual(submitted, ["secret"]);
  submission.reset();
  assert.equal(submission.start("secret", 0), true);
  assert.deepEqual(submitted, ["secret", "secret"]);
});

test("reset invalidates a delayed pairing navigation", () => {
  const submitted = [];
  const scheduled = [];
  const submission = createPairingSubmission(
    (code) => submitted.push(code),
    (callback) => scheduled.push(callback),
  );

  assert.equal(submission.start("stale-secret", 5000), true);
  submission.reset();
  assert.equal(submission.start("current-secret", 0), true);
  scheduled[0]();

  assert.deepEqual(submitted, ["current-secret"]);
});
