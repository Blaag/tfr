from __future__ import annotations

import pytest

from tfr.text_effects import (
    TextDecoration,
    TextEffectKind,
    color_lightness,
    derive_bright_color,
    interpolate_color,
)


def decoration(
    effect: TextEffectKind,
    *,
    duration: float = 2.0,
    loop: bool = False,
    accent: str = "#ffffff",
) -> TextDecoration:
    return TextDecoration(
        start=0,
        end=5,
        effect=effect,
        base_color="#6f7782",
        accent_color=accent,
        interval_seconds=duration,
        repeat_seconds=10,
        loop=loop,
        effect_width=2,
        sparkle_count=2,
        seed=1234,
    )


def rendered(
    effect: TextEffectKind,
    elapsed_seconds: float,
    *,
    duration: float = 2.0,
) -> tuple[tuple[str, str], ...]:
    value = decoration(effect, duration=duration)
    return tuple(
        value.render_character(character, index, elapsed_seconds, True)
        for index, character in enumerate("alice")
    )


def test_age_decay_derives_a_brighter_equivalent_and_finishes_at_exact_color() -> None:
    end_color = "#6f7782"
    bright = derive_bright_color(end_color)
    value = decoration(TextEffectKind.AGE_DECAY, accent=bright)

    start_style, _character = value.render_character("W", 0, 0.0, True)
    middle_style, _character = value.render_character("W", 0, 1.0, True)
    end_style, _character = value.render_character("W", 0, 2.0, True)

    assert color_lightness(bright) > color_lightness(end_color)
    assert start_style == f"fg:{bright}"
    assert middle_style == f"fg:{interpolate_color(bright, end_color, 0.5)}"
    assert end_style == f"fg:{end_color}"
    assert value.frame_delay(2.0) is None


def test_rainbow_wave_uses_multiple_hues_then_returns_to_base_color() -> None:
    active = rendered(TextEffectKind.RAINBOW_WAVE, 0.5)
    completed = rendered(TextEffectKind.RAINBOW_WAVE, 2.0)

    assert len({style for style, _character in active}) > 3
    assert {style for style, _character in completed} == {"fg:#6f7782"}


def test_ember_moves_a_thermal_front_across_the_name() -> None:
    start = rendered(TextEffectKind.EMBER, 0.0)
    middle = rendered(TextEffectKind.EMBER, 1.0)
    completed = rendered(TextEffectKind.EMBER, 2.0)

    assert start[0][0] == "fg:#ffff5f"
    assert start[-1][0] == "fg:#6f7782"
    assert len({style for style, _character in middle}) > 1
    assert {style for style, _character in completed} == {"fg:#6f7782"}


@pytest.mark.parametrize(
    ("effect", "expected_attribute"),
    [
        (TextEffectKind.UNDERLINE_SWEEP, "underline"),
        (TextEffectKind.BOLD_SWEEP, "bold"),
        (TextEffectKind.REVERSE_SWEEP, "reverse"),
    ],
)
def test_style_sweeps_apply_only_terminal_attributes(
    effect: TextEffectKind,
    expected_attribute: str,
) -> None:
    active = rendered(effect, 1.0)

    assert any(expected_attribute in style for style, _character in active)
    assert "".join(character for _style, character in active) == "alice"


@pytest.mark.parametrize(
    "effect",
    [
        TextEffectKind.COMET,
        TextEffectKind.SPARKLE,
        TextEffectKind.FROST,
        TextEffectKind.COLOR_PULSE,
    ],
)
def test_color_effects_temporarily_move_away_from_the_base_color(
    effect: TextEffectKind,
) -> None:
    active = rendered(effect, 1.0)
    completed = rendered(effect, 2.0)

    assert any(style != "fg:#6f7782" for style, _character in active)
    assert {style for style, _character in completed} == {"fg:#6f7782"}


def test_case_wave_moves_a_group_of_capitals_without_changing_width() -> None:
    value = decoration(TextEffectKind.CASE_WAVE, duration=0.2)

    output = "".join(
        value.render_character(character, index, 0.0, True)[1]
        for index, character in enumerate("alice")
    )

    assert output == "ALice"


def test_loop_flag_repeats_or_permanently_completes_the_effect() -> None:
    looping = decoration(TextEffectKind.COLOR_PULSE, loop=True)
    once = decoration(TextEffectKind.COLOR_PULSE, loop=False)

    assert looping.render_character("W", 0, 10.5, True) == looping.render_character(
        "W", 0, 0.5, True
    )
    assert looping.frame_delay(3.0) == 7.0
    assert once.render_character("W", 0, 10.5, True) == ("fg:#6f7782", "W")
    assert once.frame_delay(10.5) is None


def test_sparkle_pulse_changes_brightness_at_one_frame_per_second() -> None:
    value = TextDecoration(
        start=0,
        end=1,
        effect=TextEffectKind.SPARKLE,
        base_color="#6f7782",
        accent_color="#ffffff",
        interval_seconds=4,
        frames_per_second=1,
        sparkle_count=1,
        loop=False,
    )

    start_style, _ = value.render_character("W", 0, 0.0, True)
    bright_style, _ = value.render_character("W", 0, 1.0, True)

    assert start_style != bright_style


def test_terminal_reveal_draws_a_glitching_frontier_then_settles() -> None:
    value = TextDecoration(
        start=0,
        end=5,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7ff5f",
        accent_color="#d7ff5f",
        interval_seconds=0.1,
        frames_per_second=20,
        loop=False,
        effect_width=2,
        glitch_characters="#",
        seed=1234,
    )

    def text_at(elapsed_seconds: float, animations_enabled: bool = True) -> str:
        return "".join(
            value.render_character(character, index, elapsed_seconds, animations_enabled)[1]
            for index, character in enumerate("alice")
        )

    assert text_at(0.0) == "     "
    assert text_at(0.25) == "##   "
    assert text_at(0.299) == "##   "
    assert text_at(0.35) == "a##  "
    assert text_at(0.55) == "ali##"
    assert text_at(0.7) == "alice"
    assert text_at(0.0, animations_enabled=False) == "alice"
    assert value.frame_delay(0.35) == pytest.approx(0.05)
    assert value.frame_delay(0.7) is None


def test_terminal_reveal_fades_from_white_to_the_existing_foreground() -> None:
    value = TextDecoration(
        start=0,
        end=7,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#a8a8a8",
        accent_color="#ffffff",
        interval_seconds=0.1,
        frames_per_second=20,
        loop=False,
        effect_width=3,
        glitch_characters="#",
        settle_width=3,
    )

    settled_style, settled_character = value.render_character("a", 0, 0.65, True, "fg:#204060")
    trail_style, trail_character = value.render_character("b", 1, 0.65, True, "fg:#204060")
    white_style, white_character = value.render_character("c", 2, 0.65, True, "fg:#204060")
    glitching = tuple(
        value.render_character(character, index, 0.65, True, "fg:#204060")
        for index, character in enumerate("def", start=3)
    )

    assert (settled_style, settled_character) == ("", "a")
    assert (trail_style, trail_character) == (
        f"fg:{interpolate_color('#ffffff', '#204060', 0.5)}",
        "b",
    )
    assert (white_style, white_character) == ("fg:#ffffff", "c")
    assert glitching == (("bold fg:#ffffff", "#"),) * 3


def test_terminal_reveal_glitches_settled_characters_only_while_filling() -> None:
    value = TextDecoration(
        start=0,
        end=40,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7d7d7",
        accent_color="#ffffff",
        interval_seconds=0.05,
        frames_per_second=20,
        loop=False,
        effect_width=3,
        settle_width=3,
        glitch_characters="#",
        inline_glitch_chance=1,
        seed=1234,
    )

    pulse = [
        value.render_character("a", 0, elapsed, True, "fg:#204060")
        for elapsed in (step / 20 for step in range(7, 40))
    ]

    assert ("bold fg:#ffffff", "#") in pulse
    assert any(style.startswith("fg:#") and character == "a" for style, character in pulse)
    assert value.render_character("a", 0, 2.0, True, "fg:#204060") == ("", "a")


def test_terminal_reveal_conceals_wide_and_zero_width_characters_one_for_one() -> None:
    value = TextDecoration(
        start=0,
        end=3,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7ff5f",
        accent_color="#d7ff5f",
        interval_seconds=0.1,
        frames_per_second=20,
        loop=False,
        effect_width=1,
        glitch_characters="#",
    )

    hidden = tuple(
        value.render_character(character, index, 0.0, True)[1]
        for index, character in enumerate("界e\N{COMBINING ACUTE ACCENT}")
    )

    assert hidden == ("\N{IDEOGRAPHIC SPACE}", " ", "\N{WORD JOINER}")
    assert [len(character) for character in hidden] == [1, 1, 1]


def test_terminal_reveal_completes_on_time_below_its_frame_rate() -> None:
    value = TextDecoration(
        start=0,
        end=3,
        effect=TextEffectKind.TERMINAL_REVEAL,
        base_color="#d7ff5f",
        accent_color="#d7ff5f",
        interval_seconds=0.1,
        frames_per_second=1,
        loop=False,
        effect_width=1,
    )

    active = "".join(
        value.render_character(character, index, 0.3, True)[1]
        for index, character in enumerate("abc")
    )
    completed = "".join(
        value.render_character(character, index, 0.5, True)[1]
        for index, character in enumerate("abc")
    )

    assert active == "   "
    assert completed == "abc"
    assert value.frame_delay(0.3) == pytest.approx(0.1)
    assert value.frame_delay(0.5) is None


def test_terminal_reveal_rejects_non_cell_glitch_characters() -> None:
    with pytest.raises(ValueError, match="visible single-cell"):
        TextDecoration(
            start=0,
            end=1,
            effect=TextEffectKind.TERMINAL_REVEAL,
            base_color="#d7ff5f",
            accent_color="#d7ff5f",
            interval_seconds=0.1,
            loop=False,
            glitch_characters=" ",
        )


def test_terminal_reveal_rejects_looping() -> None:
    with pytest.raises(ValueError, match="one-shot"):
        TextDecoration(
            start=0,
            end=1,
            effect=TextEffectKind.TERMINAL_REVEAL,
            base_color="#d7ff5f",
            accent_color="#d7ff5f",
            interval_seconds=0.1,
        )


def test_terminal_reveal_rejects_invalid_inline_glitch_chance() -> None:
    with pytest.raises(ValueError, match="inline glitch chance"):
        TextDecoration(
            start=0,
            end=1,
            effect=TextEffectKind.TERMINAL_REVEAL,
            base_color="#d7ff5f",
            accent_color="#d7ff5f",
            interval_seconds=0.1,
            loop=False,
            inline_glitch_chance=1.01,
        )
