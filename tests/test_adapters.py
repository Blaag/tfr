from __future__ import annotations

import pytest

from tfr.adapters import (
    BareAdapter,
    GenericAdapter,
    RhostAdapter,
    TinyMushAdapter,
    TinyMuxAdapter,
    adapter_for,
)
from tfr.ansi import strip_ansi
from tfr.events import Confidence, EventKind


def test_tinymux_full_prefix_extracts_all_optional_fields() -> None:
    text = "[Widget(#42){Alice}<-(#7),saypose] Widget waves.\r\n"

    parsed = TinyMuxAdapter().parse(text)

    assert parsed.canonical_text == text
    assert parsed.plain_text == text
    assert parsed.display_text == "Widget waves.\r\n"
    assert parsed.message_text == "Widget waves.\r\n"
    assert parsed.kind is EventKind.POSE
    assert parsed.confidence is Confidence.INFERRED
    assert parsed.provenance is not None
    assert parsed.provenance.sender_name == "Widget"
    assert parsed.provenance.sender_dbref == 42
    assert parsed.provenance.owner_name == "Alice"
    assert parsed.provenance.owner_dbref is None
    assert parsed.provenance.enactor_dbref == 7
    assert parsed.provenance.server_source == "saypose"
    assert parsed.provenance.confidence is Confidence.HIGH


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("comsys", EventKind.CHANNEL),
        ("kill", EventKind.SYSTEM),
        ("give", EventKind.SYSTEM),
        ("page", EventKind.PAGE),
    ],
)
def test_tinymux_source_tags_classify_without_guessing(
    source: str, expected_kind: EventKind
) -> None:
    parsed = TinyMuxAdapter().parse(f"[Alice(#12),{source}] arbitrary text\r\n")

    assert parsed.kind is expected_kind
    assert parsed.confidence is Confidence.HIGH


def test_tinymux_saypose_does_not_claim_exact_type_without_evidence() -> None:
    parsed = TinyMuxAdapter().parse("[Alice(#12),saypose] localized speech text\r\n")

    assert parsed.kind is EventKind.SPEECH
    assert parsed.confidence is Confidence.HIGH


def test_tinymux_terse_prefix_has_provenance_only() -> None:
    parsed = TinyMuxAdapter().parse('[#12] Alice says, "Hi"\r\n')

    assert parsed.display_text == 'Alice says, "Hi"\r\n'
    assert parsed.kind is EventKind.SAY
    assert parsed.confidence is Confidence.INFERRED
    assert parsed.provenance is not None
    assert parsed.provenance.sender_dbref == 12
    assert parsed.provenance.sender_name is None
    assert parsed.provenance.server_source is None


def test_tinymux_absent_source_is_not_treated_as_emit() -> None:
    parsed = TinyMuxAdapter().parse("[Alice(#12)] A disembodied message\r\n")

    assert parsed.kind is EventKind.RAW_OUTPUT
    assert parsed.confidence is Confidence.HIGH
    assert parsed.provenance is not None
    assert parsed.provenance.server_source is None


def test_ansi_colored_prefix_is_removed_without_damaging_message_color() -> None:
    text = "\x1b[31m[Alice(#12),saypose]\x1b[0m \x1b[32mAlice waves.\x1b[0m\r\n"

    parsed = TinyMuxAdapter().parse(text)

    assert parsed.canonical_text == text
    assert parsed.plain_text == "[Alice(#12),saypose] Alice waves.\r\n"
    assert strip_ansi(parsed.display_text) == "Alice waves.\r\n"
    assert "\x1b[32m" in parsed.display_text
    assert parsed.provenance is not None
    assert parsed.provenance.prefix_span[0] == 0


def test_prefix_can_remain_in_display_without_changing_canonical_data() -> None:
    text = "[Alice(#12),page] Alice pages: Hi\r\n"

    parsed = TinyMuxAdapter().parse(text, show_prefix=True)

    assert parsed.display_text == text
    assert parsed.canonical_text == text
    assert parsed.kind is EventKind.PAGE


def test_rhost_prefix_extracts_owner_and_enactor() -> None:
    text = '[Widget(#42){Alice}<-(#7)] Alice says, "Hi"\r\n'

    parsed = RhostAdapter().parse(text)

    assert parsed.display_text == 'Alice says, "Hi"\r\n'
    assert parsed.kind is EventKind.SAY
    assert parsed.provenance is not None
    assert parsed.provenance.sender_name == "Widget"
    assert parsed.provenance.sender_dbref == 42
    assert parsed.provenance.owner_name == "Alice"
    assert parsed.provenance.enactor_dbref == 7
    assert parsed.provenance.server_source is None


def test_tinymush_prefix_is_logged_with_provenance_and_hidden_from_display() -> None:
    text = "[Widget(#42){Alice}<-(#7)] A disembodied message\r\n"

    parsed = TinyMushAdapter().parse(text)

    assert parsed.canonical_text == text
    assert parsed.display_text == "A disembodied message\r\n"
    assert parsed.provenance is not None
    assert parsed.provenance.sender_name == "Widget"
    assert parsed.provenance.sender_dbref == 42
    assert parsed.provenance.owner_name == "Alice"
    assert parsed.provenance.enactor_dbref == 7
    assert parsed.provenance.adapter == "tinymush"
    assert TinyMushAdapter().parse(text, show_prefix=True).display_text == text


def test_malformed_prefix_remains_ordinary_text() -> None:
    text = "[Alice(#oops),saypose] Alice waves.\r\n"

    parsed = TinyMuxAdapter().parse(text)

    assert parsed.canonical_text == text
    assert parsed.display_text == text
    assert parsed.provenance is None
    assert parsed.kind is EventKind.RAW_OUTPUT


def test_generic_adapter_only_uses_conservative_visible_heuristics() -> None:
    page = GenericAdapter().parse("From afar, Alice pages: Hello\r\n")
    emit = GenericAdapter().parse("A disembodied message\r\n")

    assert page.kind is EventKind.PAGE
    assert page.confidence is Confidence.INFERRED
    assert emit.kind is EventKind.RAW_OUTPUT
    assert emit.provenance is None


def test_adapter_factory_falls_back_to_generic() -> None:
    assert isinstance(adapter_for("bare"), BareAdapter)
    assert isinstance(adapter_for("tinymux"), TinyMuxAdapter)
    assert isinstance(adapter_for("tinymush"), TinyMushAdapter)
    assert isinstance(adapter_for("rhost"), RhostAdapter)
    assert isinstance(adapter_for("unknown"), GenericAdapter)
