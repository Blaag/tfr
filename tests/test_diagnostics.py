from __future__ import annotations

import pytest

from tfr.diagnostics import DiagnosticBuffer


def test_diagnostics_are_bounded_structured_and_clearable() -> None:
    diagnostics = DiagnosticBuffer(2)
    for index in range(3):
        diagnostics.record(
            severity="warning",
            component="presentation",
            code="program_rejected",
            message=f"Program rejected at variant {index}",
            client="pwa",
        )

    assert [entry.message for entry in diagnostics.entries()] == [
        "Program rejected at variant 1",
        "Program rejected at variant 2",
    ]
    diagnostics.clear()
    assert diagnostics.entries() == ()


@pytest.mark.parametrize(
    "values",
    [
        {"component": "bad component", "code": "failure", "message": "safe"},
        {"component": "web", "code": "failure", "message": "secret\nvalue"},
        {"component": "web", "code": "failure", "message": "x" * 241},
    ],
)
def test_diagnostics_reject_unbounded_or_multiline_values(values: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="diagnostic"):
        DiagnosticBuffer().record(severity="error", **values)  # type: ignore[arg-type]
