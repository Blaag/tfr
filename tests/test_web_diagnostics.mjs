import assert from "node:assert/strict";
import test from "node:test";

import {
  clearDiagnostics,
  diagnosticEntries,
  recordDiagnostic,
} from "../src/tfr/web/diagnostics.mjs";

test("PWA diagnostics are bounded, structured, and redacted by construction", () => {
  clearDiagnostics();
  for (let index = 0; index < 105; index += 1) {
    recordDiagnostic({
      severity: "warning",
      component: "presentation",
      code: "client_rejected",
      message: `Presentation ${index} was rejected`,
    });
  }
  assert.equal(diagnosticEntries().length, 100);
  assert.equal(diagnosticEntries()[0].message, "Presentation 5 was rejected");
  assert.equal(recordDiagnostic({
    severity: "error",
    component: "web",
    code: "bad",
    message: "secret\nvalue",
  }), null);
  clearDiagnostics();
  assert.deepEqual(diagnosticEntries(), []);
});
