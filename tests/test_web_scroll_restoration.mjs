import assert from "node:assert/strict";
import test from "node:test";

import { restoreTranscriptPosition } from "../src/tfr/web/scroll-restoration.mjs";

function liveFixture(scrollHeight) {
  const assignments = [];
  const transcript = {
    scrollHeight,
    scrollTop: 125,
    scrollTo(options) {
      assignments.push(options);
      this.scrollTop = options.top;
    },
  };
  const reading = { atLive: true, unseenLive: 3, scrollTop: 125 };
  const scheduled = [];
  const updates = [];
  restoreTranscriptPosition({
    transcript,
    reading,
    schedule: (callback) => scheduled.push(callback),
    onRestore: () => updates.push(transcript.scrollTop),
  });
  return { assignments, reading, scheduled, transcript, updates };
}

for (const [description, height] of [["longer", 1200], ["shorter", 80]]) {
  test(`restores a live ${description} transcript synchronously and corrects next frame`, () => {
    const fixture = liveFixture(height);

    assert.deepEqual(fixture.assignments, [{ top: height, behavior: "auto" }]);
    assert.equal(fixture.scheduled.length, 1);
    assert.deepEqual(fixture.updates, [height]);
    assert.deepEqual(fixture.reading, { atLive: true, unseenLive: 0, scrollTop: height });

    fixture.transcript.scrollHeight = height + 25;
    fixture.scheduled[0]();
    assert.deepEqual(fixture.assignments.at(-1), { top: height + 25, behavior: "auto" });
    assert.equal(fixture.reading.scrollTop, height + 25);
  });
}

test("restores a non-live saved position synchronously and preserves it for correction", () => {
  const assignments = [];
  const transcript = {
    scrollHeight: 1000,
    _scrollTop: 400,
    get scrollTop() { return this._scrollTop; },
    set scrollTop(value) { assignments.push(value); this._scrollTop = value; },
    scrollTo() { assert.fail("saved positions must not scroll to live"); },
  };
  const reading = { atLive: false, unseenLive: 2, scrollTop: 275 };
  const scheduled = [];

  restoreTranscriptPosition({
    transcript,
    reading,
    schedule: (callback) => scheduled.push(callback),
    onRestore() {},
  });
  scheduled[0]();

  assert.deepEqual(assignments, [275, 275]);
  assert.deepEqual(reading, { atLive: false, unseenLive: 2, scrollTop: 275 });
});
