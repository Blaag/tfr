from __future__ import annotations

import pytest

from tfr.spellcheck import LocalSpellChecker


@pytest.fixture(scope="module")
def checker() -> LocalSpellChecker:
    return LocalSpellChecker()


@pytest.mark.parametrize("prefix", ['"', "say ", ":", "POSE "])
def test_corrects_only_explicit_speech_and_pose_forms(
    checker: LocalSpellChecker, prefix: str
) -> None:
    result = checker.correct(f"{prefix}I liek teh fox.")

    assert result.text == f"{prefix}I like the fox."
    assert [(item.original, item.replacement) for item in result.corrections] == [
        ("liek", "like"),
        ("teh", "the"),
    ]
    assert all(
        result.text[item.start : item.end] == item.replacement for item in result.corrections
    )


@pytest.mark.parametrize(
    "text",
    [
        "look tehre",
        "@emit tehre",
        "+mail Bob=tehre",
        "/help tehre",
        "! echo tehre",
        ";waves tehre",
    ],
)
def test_leaves_non_conversational_commands_unchanged(
    checker: LocalSpellChecker, text: str
) -> None:
    assert checker.correct(text).text == text


def test_preserves_urls_identifiers_names_and_protected_vocabulary(
    checker: LocalSpellChecker,
) -> None:
    text = '"Visit https://example.com/tehre with Bob, foo_bar, asshole, fuk, and MUX.'

    assert checker.correct(text, protected_words={"tehre"}).text == text


def test_preserves_punctuation_and_transfers_initial_casing(checker: LocalSpellChecker) -> None:
    result = checker.correct('"Teh fox says, “liek!”')

    assert result.text == '"The fox says, “like!”'


def test_preserves_a_capitalized_name_at_the_start_of_speech(
    checker: LocalSpellChecker,
) -> None:
    assert checker.correct('"Blaag waves.').text == '"Blaag waves.'


def test_leaves_multiline_text_unchanged(checker: LocalSpellChecker) -> None:
    text = '"I liek this.\n"I liek that.'

    assert checker.correct(text).text == text
