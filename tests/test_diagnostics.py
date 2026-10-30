from __future__ import annotations

import pytest

from tfr.diagnostics import DiagnosticBuffer, bounded_diagnostic_message


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


def test_persistent_diagnostics_are_private_bounded_and_redacted(tmp_path) -> None:
    path = tmp_path / "diagnostics" / "runtime.jsonl"
    diagnostics = DiagnosticBuffer(3, path=path)
    diagnostics.record(
        severity="warning",
        component="gateway",
        code="connection_failed",
        message="token=abc123 in /Users/alice/.config/tfr",
    )
    diagnostics.record(
        severity="info", component="gateway", code="retry", message="retry 1"
    )
    diagnostics.record(
        severity="info", component="gateway", code="retry", message="retry 2"
    )

    loaded = DiagnosticBuffer(3, path=path)

    assert path.stat().st_mode & 0o777 == 0o600
    assert [entry.message for entry in loaded.entries()] == [
        "token=[redacted] in ~/.config/tfr",
        "retry 1",
        "retry 2",
    ]
    assert "abc123" not in path.read_text(encoding="utf-8")


def test_bounded_diagnostic_message_marks_truncation_and_removes_secrets() -> None:
    message = bounded_diagnostic_message("password=hunter2 " + "x" * 300)

    assert "hunter2" not in message
    assert message.endswith("… [truncated]")
    assert len(message) == 240
