import assert from "node:assert/strict";
import test from "node:test";

import {
  activitySparkline,
  barGlyph,
  compactSummary,
  heatmapLegend,
  heatmapModel,
} from "../src/tfr/web/statistics.mjs";

test("renders the TUI heatmap scale across a bounded seven by twenty-four grid", () => {
  const matrix = Array.from({ length: 7 }, () => Array(24).fill(0));
  matrix[0][0] = 1;
  matrix[1][6] = 2;
  matrix[2][12] = 3;
  matrix[3][18] = 4;

  const model = heatmapModel(matrix);

  assert.equal(model.length, 7);
  assert.ok(model.every((day) => day.cells.length === 24));
  assert.equal(model[0].cells[0].glyph, "░");
  assert.equal(model[1].cells[6].glyph, "▒");
  assert.equal(model[2].cells[12].glyph, "▓");
  assert.equal(model[3].cells[18].glyph, "█");
  assert.equal(model[6].cells[23].glyph, "·");
  assert.equal(model[2].cells[12].label, "Wed 12:00 — 3 lines");
  assert.deepEqual(heatmapLegend.map((item) => item.glyph).join(""), "·░▒▓█");
});

test("normalizes malformed and very large heatmap cells without overflow", () => {
  const model = heatmapModel([[Infinity, -3, "5", Number.MAX_VALUE], null, [1]]);
  assert.equal(model[0].cells[0].value, 0);
  assert.equal(model[0].cells[1].value, 0);
  assert.equal(model[0].cells[2].value, 5);
  assert.equal(model[0].cells[3].glyph, "█");
  assert.equal(model[6].cells[23].value, 0);
});

test("builds compact shared-scale bars and activity lines", () => {
  assert.equal(barGlyph(5, 10, 10), "█████");
  assert.equal(barGlyph(1, 100, 10), "█");
  assert.equal(barGlyph(0, 100, 10), "");
  const line = activitySparkline([
    { label: "Mon", count: 0 },
    { label: "Tue", count: 5 },
    { label: "Wed", count: 10 },
  ]);
  assert.equal(line.plot, "▁▅█");
  assert.equal(line.maximum, 10);
  assert.equal(line.first, "Mon");
  assert.equal(line.last, "Wed");
});

test("normalizes the compact statistics summary", () => {
  assert.deepEqual(compactSummary({
    lines: 366,
    speakers: 6,
    unattributed_lines: 0,
    say_percent: 70.5,
    pose_percent: 29.5,
    classification_coverage: 100,
  }), {
    lines: 366,
    speakers: 6,
    unattributed: 0,
    sayPercent: 70.5,
    posePercent: 29.5,
    coverage: 100,
    first: null,
    last: null,
  });
});
