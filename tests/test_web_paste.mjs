import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_PASTE_LINES,
  preparePastePreview,
  validatedPasteUpdate,
} from "../src/tfr/web/paste.mjs";

test("paste preview normalizes counts while retaining exact source", () => {
  const source = "one\r\ntwo\r☃\n";
  const preview = preparePastePreview(source, "alpha");
  assert.equal(preview.text, source);
  assert.equal(preview.world, "alpha");
  assert.equal(preview.lineCount, 3);
  assert.equal(preview.byteCount, new TextEncoder().encode("one\ntwo\n☃").byteLength);
  assert.equal(preview.preview, "one\ntwo\n☃");
});

test("paste preview is bounded and truncates rendering", () => {
  const preview = preparePastePreview(Array.from({ length: 11 }, (_, index) => `<b>${index}</b>`).join("\n"), "alpha");
  assert.equal(preview.previewTruncated, true);
  assert.equal(preview.preview.split("\n").length, 10);
  assert.throws(
    () => preparePastePreview(Array.from({ length: MAX_PASTE_LINES + 1 }, () => "x").join("\n"), "alpha"),
    /100 lines/,
  );
});

test("paste progress accepts only bounded updates for the active request", () => {
  const id = "active";
  assert.deepEqual(validatedPasteUpdate({ type: "paste_progress", request_id: id, sent_lines: 2, total_lines: 3 }, id), {
    type: "paste_progress", sentLines: 2, totalLines: 3, reason: undefined,
  });
  assert.equal(validatedPasteUpdate({ type: "paste_progress", request_id: "stale", sent_lines: 2, total_lines: 3 }, id), null);
  assert.equal(validatedPasteUpdate({ type: "paste_progress", request_id: id, sent_lines: 4, total_lines: 3 }, id), null);
  assert.equal(validatedPasteUpdate({ type: "paste_cancelled", request_id: id, sent_lines: 1, total_lines: 3, reason: "secret failure" }, id), null);
});
