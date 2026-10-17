from __future__ import annotations

import math

import pytest

from tfr.presentation import (
    EffectProgram,
    ForegroundKeyframe,
    PresentationCapability,
    PresentationStyle,
    PresentationVariant,
    SweepTrailMode,
    character_hue_wave,
    character_palette_front,
    character_reveal,
    character_sparkle,
    character_style_sweep,
    character_sweep,
    color_pulse,
    effect_programs_as_dict,
    foreground_transition,
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
    assert set(projected["character_sweep"]) == {
        "positions",
        "base_color",
        "head_color",
        "trail_width",
        "uppercase_head",
    }


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

    too_long = character_sweep(0, 101, base_color="#180000", head_color="#ff0000")
    with pytest.raises(ValueError, match="sweep target is too large"):
        validate_effect_programs("a" * 101, (too_long,))


def test_character_sweep_supports_one_way_padding_and_directional_trails() -> None:
    program = character_sweep(
        0,
        5,
        base_color="#000000",
        head_color="#ffffff",
        trail_width=1.5,
        trail_mode=SweepTrailMode.BEHIND,
        end_padding=3,
        return_to_start=False,
    )
    variant = program.variants[0]
    projected = effect_programs_as_dict((program,))["programs"][0]["variants"][0]["character_sweep"]

    assert len(variant.character_sweep.positions) == 2  # type: ignore[union-attr]
    assert projected["trail_mode"] == "behind"
    assert projected["end_padding"] == 3.0
    ahead, behind = (
        presentation_grapheme_at(
            program,
            "x",
            index,
            5,
            0.5,
            animations_enabled=True,
            capabilities=frozenset(PresentationCapability),
        )[0].foreground
        for index in (4, 1)
    )
    assert ahead == "#000000"
    assert behind != "#000000"
    with pytest.raises(ValueError, match="return flag must be boolean"):
        character_sweep(  # type: ignore[arg-type]
            0,
            1,
            base_color="#000000",
            head_color="#ffffff",
            return_to_start="yes",
        )


def test_character_hue_wave_and_palette_front_are_portable_and_bounded() -> None:
    rainbow = character_hue_wave(0, 5, base_color="#ffc9d7", repeat_count=2)
    ember = character_palette_front(
        0,
        5,
        colors=("#ffff5f", "#ffaf00", "#ff5f00", "#d70000", "#a9914a"),
        repeat_count=2,
    )
    capabilities = frozenset(PresentationCapability)

    rainbow_colors = {
        presentation_grapheme_at(
            rainbow, "x", index, 5, 0, animations_enabled=True, capabilities=capabilities
        )[0].foreground
        for index in range(5)
    }
    ember_start = presentation_grapheme_at(
        ember, "x", 0, 5, 0, animations_enabled=True, capabilities=capabilities
    )[0].foreground
    projected_rainbow = effect_programs_as_dict((rainbow,))["programs"][0]["variants"][0]
    projected_ember = effect_programs_as_dict((ember,))["programs"][0]["variants"][0]

    assert len(rainbow_colors) == 5
    assert ember_start == "#ffff5f"
    assert projected_rainbow["character_hue_wave"]["spatial_cycles"] == 1.0
    assert projected_ember["character_palette_front"]["colors"][-1] == "#a9914a"
    with pytest.raises(ValueError, match="2 to 8 colors"):
        character_palette_front(0, 1, colors=("#ffffff",))


def test_style_sweep_collapses_case_and_attribute_effects() -> None:
    case = character_style_sweep(
        0,
        5,
        base_style=PresentationStyle(foreground="#111111"),
        active_style=PresentationStyle(foreground="#eeeeee"),
        width=0.5,
        end_padding=1,
        uppercase_active=True,
        lowercase_inactive=True,
        stepped=True,
        duration_seconds=2.5,
    )
    reverse = character_style_sweep(
        0,
        5,
        base_style=PresentationStyle(foreground="#111111"),
        active_style=PresentationStyle(foreground="#111111", reverse=True),
        width=2,
    )
    capabilities = frozenset(PresentationCapability)

    rendered = [
        presentation_grapheme_at(
            case, character, index, 5, 1, animations_enabled=True, capabilities=capabilities
        )
        for index, character in enumerate("alice")
    ]

    assert "".join(text for _style, text in rendered) == "alIce"
    assert rendered[2][0].foreground == "#eeeeee"
    assert (
        presentation_grapheme_at(
            reverse, "x", 0, 5, 0, animations_enabled=True, capabilities=capabilities
        )[0].reverse
        is True
    )
    projected = effect_programs_as_dict((case,))
    assert projected["version"] == 2
    assert "character_style_sweep" in projected["programs"][0]["variants"][0]


def test_sparkle_is_deterministic_and_front_can_fill_behind() -> None:
    sparkle = character_sparkle(
        0,
        5,
        base_color="#000000",
        accent_color="#ffffff",
        count=2,
        seed=42,
    )
    frost = character_sweep(
        0,
        5,
        base_color="#102030",
        head_color="#d7ffff",
        trail_width=2,
        trail_mode=SweepTrailMode.BEHIND,
        end_padding=3,
        fill_behind=True,
    )
    capabilities = frozenset(PresentationCapability)
    first = [
        presentation_grapheme_at(
            sparkle, "x", index, 5, 0.05, animations_enabled=True, capabilities=capabilities
        )[0].foreground
        for index in range(5)
    ]
    repeated = [
        presentation_grapheme_at(
            sparkle, "x", index, 5, 0.05, animations_enabled=True, capabilities=capabilities
        )[0].foreground
        for index in range(5)
    ]

    assert first == repeated
    assert sum(color != "#000000" for color in first) == 2
    assert (
        presentation_grapheme_at(
            frost, "x", 0, 5, 1, animations_enabled=True, capabilities=capabilities
        )[0].foreground
        == "#d7ffff"
    )


def test_age_transition_and_terminal_reveal_are_bounded_portable_programs() -> None:
    decay = foreground_transition(
        0,
        5,
        start_color="#ffffff",
        end_color="#222222",
        duration_seconds=30,
    )
    reveal = character_reveal(
        0,
        5,
        interval_seconds=0.1,
        base_color="#d7d7d7",
        accent_color="#ffffff",
        glitch_width=2,
        settle_width=1,
        seed=7,
        glitch_characters="#%",
    )
    capabilities = frozenset(PresentationCapability)

    assert (
        presentation_style_at(
            decay, 0, animations_enabled=True, capabilities=capabilities
        ).foreground
        == "#ffffff"
    )
    hidden = presentation_grapheme_at(
        reveal, "a", 0, 5, 0, animations_enabled=True, capabilities=capabilities
    )
    glitch = presentation_grapheme_at(
        reveal, "a", 0, 5, 0.15, animations_enabled=True, capabilities=capabilities
    )
    complete = presentation_grapheme_at(
        reveal, "a", 0, 5, 1, animations_enabled=True, capabilities=capabilities
    )

    assert hidden[1] == " "
    assert glitch[1] in "#%"
    assert complete == (PresentationStyle(), "a")
    assert effect_programs_as_dict((decay,))["version"] == 2
    assert effect_programs_as_dict((reveal,))["version"] == 2


def test_overlapping_speaker_and_reveal_programs_compose() -> None:
    speaker = character_hue_wave(0, 5, base_color="#ffc9d7")
    reveal = character_reveal(
        0,
        19,
        interval_seconds=0.05,
        base_color="#d7d7d7",
        accent_color="#ffffff",
        glitch_width=2,
        settle_width=1,
        seed=7,
        glitch_characters="#%",
    )

    validate_effect_programs('Jarin says, "hello"', (speaker, reveal))
    assert len(effect_programs_as_dict((speaker, reveal))["programs"]) == 2


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


def test_effect_program_rejects_bad_keyframes_and_too_many_targets() -> None:
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

    programs = tuple(
        color_pulse(0, 3, base_color="#000000", accent_color="#ffffff")
        for _ in range(5)
    )
    with pytest.raises(ValueError, match="too many presentation programs"):
        validate_effect_programs("test", programs)


def test_effect_program_requires_integer_grapheme_aligned_targets() -> None:
    with pytest.raises(ValueError, match="ordered and non-empty"):
        color_pulse(0.5, 1, base_color="#000000", accent_color="#ffffff")  # type: ignore[arg-type]

    split_cluster = color_pulse(0, 1, base_color="#000000", accent_color="#ffffff")
    with pytest.raises(ValueError, match="grapheme boundaries"):
        validate_effect_programs("e\N{COMBINING ACUTE ACCENT}", (split_cluster,))
