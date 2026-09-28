from __future__ import annotations

import math

import pytest

from tfr.presentation import (
    EffectProgram,
    ForegroundKeyframe,
    PresentationCapability,
    PresentationStyle,
    PresentationVariant,
    character_sweep,
    color_pulse,
    effect_programs_as_dict,
    presentation_frame_delay,
    presentation_grapheme_at,
    presentation_style_at,
    validate_effect_programs,
)


def test_color_pulse_compiles_to_portable_timeline() -> None:
    program = color_pulse(
        0,
        5,
        base_color="#a9914a",
        accent_color="#fff08a",
        repeat_count=2,
    )

    projected = effect_programs_as_dict((program,))

    assert projected["version"] == 1
    assert projected["programs"][0]["variants"][0] == {
        "requires": ["foreground_color", "timeline"],
        "style": {"foreground": "#a9914a"},
        "foreground_keyframes": [
            {"at": 0.0, "color": "#a9914a"},
            {"at": 0.5, "color": "#fff08a"},
            {"at": 1.0, "color": "#a9914a"},
        ],
    }
    assert projected["programs"][0]["reduced_motion"] == {
        "foreground": "#fff08a",
        "bold": True,
    }


def test_timeline_interpolates_and_uses_explicit_reduced_motion_style() -> None:
    program = color_pulse(
        0,
        5,
        base_color="#000000",
        accent_color="#ffffff",
        duration_seconds=2,
        repeat_seconds=5,
        repeat_count=1,
    )
    capabilities = frozenset(PresentationCapability)

    active = presentation_style_at(
        program,
        1,
        animations_enabled=True,
        capabilities=capabilities,
    )
    completed = presentation_style_at(
        program,
        3,
        animations_enabled=True,
        capabilities=capabilities,
    )
    reduced = presentation_style_at(
        program,
        1,
        animations_enabled=False,
        capabilities=capabilities,
    )

    assert active.foreground == "#ffffff"
    assert completed == PresentationStyle(foreground="#000000")
    assert reduced == PresentationStyle(foreground="#ffffff", bold=True)
    assert presentation_frame_delay(program, 0) == 0.05
    assert presentation_frame_delay(program, 3) is None


def test_character_sweep_moves_red_uppercase_head_out_and_back() -> None:
    program = character_sweep(
        0,
        5,
        base_color="#180000",
        head_color="#ff0000",
        trail_width=2,
        uppercase_head=True,
        duration_seconds=2,
        repeat_seconds=2,
        repeat_count=2,
    )
    capabilities = frozenset(PresentationCapability)

    start = [
        presentation_grapheme_at(
            program,
            character,
            index,
            5,
            0,
            animations_enabled=True,
            capabilities=capabilities,
        )
        for index, character in enumerate("alice")
    ]
    far = [
        presentation_grapheme_at(
            program,
            character,
            index,
            5,
            1,
            animations_enabled=True,
            capabilities=capabilities,
        )
        for index, character in enumerate("alice")
    ]
    returned = presentation_grapheme_at(
        program,
        "a",
        0,
        5,
        2,
        animations_enabled=True,
        capabilities=capabilities,
    )

    assert [text for _style, text in start] == list("Alice")
    assert start[0][0].foreground == "#ff0000"
    assert start[1][0].foreground not in {"#180000", "#ff0000"}
    assert [text for _style, text in far] == list("alicE")
    assert far[-1][0].foreground == "#ff0000"
    assert returned[1] == "A"
    assert presentation_frame_delay(program, 0) == 0.05

    projected = effect_programs_as_dict((program,))["programs"][0]["variants"][0]
    assert projected["requires"] == [
        "character_foreground",
        "timeline",
        "character_case",
    ]
    assert projected["character_sweep"]["positions"] == [
        {"at": 0.0, "position": 0.0},
        {"at": 0.5, "position": 1.0},
        {"at": 1.0, "position": 0.0},
    ]


def test_character_sweep_has_bounded_target_and_static_reduced_motion() -> None:
    program = character_sweep(
        0,
        2,
        base_color="#180000",
        head_color="#ff0000",
        uppercase_head=True,
    )
    validate_effect_programs("e\N{COMBINING ACUTE ACCENT}x", (program,))
    style, text = presentation_grapheme_at(
        program,
        "e\N{COMBINING ACUTE ACCENT}",
        0,
        2,
        0,
        animations_enabled=False,
        capabilities=frozenset(PresentationCapability),
    )
    assert style == PresentationStyle(foreground="#ff0000", bold=True)
    assert text == "e\N{COMBINING ACUTE ACCENT}"

    _style, sharp_s = presentation_grapheme_at(
        program,
        "ß",
        0,
        1,
        0,
        animations_enabled=True,
        capabilities=frozenset(PresentationCapability),
    )
    assert sharp_s == "ß"

    too_long = character_sweep(0, 65, base_color="#180000", head_color="#ff0000")
    with pytest.raises(ValueError, match="sweep target is too large"):
        validate_effect_programs("a" * 65, (too_long,))


def test_unsupported_variant_uses_fallback_without_partial_interpretation() -> None:
    program = EffectProgram(
        start=0,
        end=1,
        variants=(
            PresentationVariant(
                requires=(PresentationCapability.BOLD, PresentationCapability.UNDERLINE),
                style=PresentationStyle(bold=True, underline=True),
            ),
        ),
        fallback=PresentationStyle(foreground="#112233"),
    )

    assert presentation_style_at(
        program,
        0,
        animations_enabled=True,
        capabilities=frozenset({PresentationCapability.BOLD}),
    ) == PresentationStyle(foreground="#112233")


@pytest.mark.parametrize("value", [math.inf, math.nan, -1, 0])
def test_effect_program_rejects_invalid_duration(value: float) -> None:
    with pytest.raises(ValueError, match="duration"):
        EffectProgram(
            start=0,
            end=1,
            variants=(
                PresentationVariant(
                    requires=(PresentationCapability.BOLD,),
                    style=PresentationStyle(bold=True),
                ),
            ),
            duration_seconds=value,
        )


def test_effect_program_rejects_bad_keyframes_and_overlapping_targets() -> None:
    with pytest.raises(ValueError, match="start at 0 and end at 1"):
        PresentationVariant(
            requires=(
                PresentationCapability.FOREGROUND_COLOR,
                PresentationCapability.TIMELINE,
            ),
            foreground_keyframes=(
                ForegroundKeyframe(0.2, "#000000"),
                ForegroundKeyframe(1, "#ffffff"),
            ),
        )

    first = color_pulse(0, 3, base_color="#000000", accent_color="#ffffff")
    second = color_pulse(2, 4, base_color="#000000", accent_color="#ffffff")
    with pytest.raises(ValueError, match="too many presentation programs"):
        validate_effect_programs("test", (first, second))


def test_effect_program_requires_integer_grapheme_aligned_targets() -> None:
    with pytest.raises(ValueError, match="ordered and non-empty"):
        color_pulse(0.5, 1, base_color="#000000", accent_color="#ffffff")  # type: ignore[arg-type]

    split_cluster = color_pulse(0, 1, base_color="#000000", accent_color="#ffffff")
    with pytest.raises(ValueError, match="grapheme boundaries"):
        validate_effect_programs("e\N{COMBINING ACUTE ACCENT}", (split_cluster,))
