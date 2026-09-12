from __future__ import annotations

import colorsys
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from string import ascii_letters, digits, punctuation

from prompt_toolkit.utils import get_cwidth

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ANSI_FOREGROUND_COLORS = {
    "ansiblack": "#000000",
    "ansired": "#800000",
    "ansigreen": "#008000",
    "ansiyellow": "#808000",
    "ansiblue": "#000080",
    "ansimagenta": "#800080",
    "ansicyan": "#008080",
    "ansiwhite": "#c0c0c0",
    "ansibrightblack": "#808080",
    "ansibrightred": "#ff0000",
    "ansibrightgreen": "#00ff00",
    "ansibrightyellow": "#ffff00",
    "ansibrightblue": "#0000ff",
    "ansibrightmagenta": "#ff00ff",
    "ansibrightcyan": "#00ffff",
    "ansibrightwhite": "#ffffff",
}


class TextEffectKind(StrEnum):
    SHIMMER = "shimmer"
    CAPITALIZATION_ROLL = "capitalization_roll"
    COMET = "comet"
    SPARKLE = "sparkle"
    UNDERLINE_SWEEP = "underline_sweep"
    BOLD_SWEEP = "bold_sweep"
    EMBER = "ember"
    FROST = "frost"
    RAINBOW_WAVE = "rainbow_wave"
    COLOR_PULSE = "color_pulse"
    CASE_WAVE = "case_wave"
    REVERSE_SWEEP = "reverse_sweep"
    AGE_DECAY = "age_decay"
    TERMINAL_REVEAL = "terminal_reveal"


def validate_color(value: str) -> str:
    if not isinstance(value, str) or not _COLOR.fullmatch(value):
        raise ValueError("text effect colors must use #RRGGBB notation")
    return value.lower()


def _style_foreground_color(style: str) -> str | None:
    for token in reversed(style.split()):
        color = token.removeprefix("fg:").casefold()
        if _COLOR.fullmatch(color):
            return color
        if color in _ANSI_FOREGROUND_COLORS:
            return _ANSI_FOREGROUND_COLORS[color]
    return None


def _srgb_to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * channel ** (1 / 2.4) - 0.055


@lru_cache(maxsize=512)
def _color_to_oklab(color: str) -> tuple[float, float, float]:
    red, green, blue = (
        _srgb_to_linear(int(color[index : index + 2], 16) / 255) for index in (1, 3, 5)
    )
    light = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue
    light_root = math.copysign(abs(light) ** (1 / 3), light)
    medium_root = math.copysign(abs(medium) ** (1 / 3), medium)
    short_root = math.copysign(abs(short) ** (1 / 3), short)
    return (
        0.2104542553 * light_root + 0.7936177850 * medium_root - 0.0040720468 * short_root,
        1.9779984951 * light_root - 2.4285922050 * medium_root + 0.4505937099 * short_root,
        0.0259040371 * light_root + 0.7827717662 * medium_root - 0.8086757660 * short_root,
    )


def _oklab_to_linear_rgb(
    lightness: float,
    green_red: float,
    blue_yellow: float,
) -> tuple[float, float, float]:
    light_root = lightness + 0.3963377774 * green_red + 0.2158037573 * blue_yellow
    medium_root = lightness - 0.1055613458 * green_red - 0.0638541728 * blue_yellow
    short_root = lightness - 0.0894841775 * green_red - 1.2914855480 * blue_yellow
    light = light_root**3
    medium = medium_root**3
    short = short_root**3
    return (
        4.0767416621 * light - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * light + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * light - 0.7034186147 * medium + 1.7076147010 * short,
    )


def _oklab_to_color(lightness: float, green_red: float, blue_yellow: float) -> str:
    chroma_scale = 1.0
    channels = _oklab_to_linear_rgb(lightness, green_red, blue_yellow)
    if any(channel < 0 or channel > 1 for channel in channels):
        lower = 0.0
        upper = 1.0
        for _ in range(16):
            chroma_scale = (lower + upper) / 2
            candidate = _oklab_to_linear_rgb(
                lightness,
                green_red * chroma_scale,
                blue_yellow * chroma_scale,
            )
            if all(0 <= channel <= 1 for channel in candidate):
                lower = chroma_scale
                channels = candidate
            else:
                upper = chroma_scale
    encoded = tuple(
        round(255 * min(1.0, max(0.0, _linear_to_srgb(channel)))) for channel in channels
    )
    return "#" + "".join(f"{channel:02x}" for channel in encoded)


def interpolate_color(start: str, end: str, progress: float) -> str:
    progress = min(1.0, max(0.0, progress))
    start_channels = _color_to_oklab(validate_color(start))
    end_channels = _color_to_oklab(validate_color(end))
    return _oklab_to_color(
        *(
            source + (target - source) * progress
            for source, target in zip(
                start_channels,
                end_channels,
                strict=True,
            )
        )
    )


def derive_bright_color(end_color: str, *, strength: float = 0.65) -> str:
    if not 0 <= strength <= 1:
        raise ValueError("bright color strength must be between 0 and 1")
    lightness, green_red, blue_yellow = _color_to_oklab(validate_color(end_color))
    bright_lightness = lightness + (1 - lightness) * strength
    return _oklab_to_color(bright_lightness, green_red, blue_yellow)


def color_lightness(color: str) -> float:
    return _color_to_oklab(validate_color(color))[0]


def _rainbow_color(position: float) -> str:
    red, green, blue = colorsys.hsv_to_rgb(position % 1.0, 0.78, 1.0)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _seed_value(seed: int, frame: int, index: int) -> int:
    value = (seed ^ (frame * 0x9E3779B1) ^ (index * 0x85EBCA77)) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    return value


@lru_cache(maxsize=2_048)
def _sparkle_indexes(seed: int, frame: int, length: int, count: int) -> frozenset[int]:
    return frozenset(
        sorted(
            range(length),
            key=lambda candidate: _seed_value(seed, frame, candidate),
        )[: min(count, length)]
    )


@dataclass(frozen=True, slots=True)
class TextDecoration:
    start: int
    end: int
    effect: TextEffectKind
    base_color: str
    accent_color: str
    interval_seconds: float
    repeat_seconds: float = 10.0
    phase_offset_seconds: float = 0.0
    frames_per_second: float = 20.0
    shimmer_width: float = 1.5
    loop: bool = True
    effect_width: int = 2
    sparkle_count: int = 2
    seed: int = 0
    glitch_characters: str = ascii_letters + digits + punctuation
    settle_width: int = 0
    inline_glitch_chance: float = 0.0

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("text decoration span must be ordered and non-empty")
        object.__setattr__(self, "base_color", validate_color(self.base_color))
        object.__setattr__(self, "accent_color", validate_color(self.accent_color))
        if self.interval_seconds <= 0:
            raise ValueError("text effect interval must be positive")
        if self.repeat_seconds <= 0:
            raise ValueError("text effect repeat interval must be positive")
        if self.loop and self.repeat_seconds <= self.burst_duration_seconds:
            raise ValueError("text effect repeat interval must exceed its burst duration")
        if not 0 < self.frames_per_second <= 30:
            raise ValueError("text effect frame rate must be between 0 and 30")
        if self.shimmer_width <= 0:
            raise ValueError("text shimmer width must be positive")
        if self.effect_width < 1:
            raise ValueError("text effect width must be positive")
        if self.settle_width < 0:
            raise ValueError("text settle width cannot be negative")
        if not 0 <= self.inline_glitch_chance <= 1:
            raise ValueError("inline glitch chance must be between 0 and 1")
        if self.sparkle_count < 1:
            raise ValueError("text sparkle count must be positive")
        if self.effect is TextEffectKind.TERMINAL_REVEAL and (
            not self.glitch_characters
            or any(
                character.isspace() or get_cwidth(character) != 1
                for character in self.glitch_characters
            )
        ):
            raise ValueError("terminal reveal glitch characters must be visible single-cell glyphs")
        if self.effect is TextEffectKind.TERMINAL_REVEAL and self.loop:
            raise ValueError("terminal reveal effects must be one-shot")

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def burst_duration_seconds(self) -> float:
        if self.effect is TextEffectKind.TERMINAL_REVEAL:
            return self.interval_seconds * (self.length + self.effect_width + self.settle_width)
        if self.effect in {
            TextEffectKind.CAPITALIZATION_ROLL,
            TextEffectKind.CASE_WAVE,
        }:
            return self.interval_seconds * self.length
        return self.interval_seconds

    def cycle_phase(self, elapsed_seconds: float) -> float:
        phase = max(0.0, elapsed_seconds + self.phase_offset_seconds)
        return phase % self.repeat_seconds if self.loop else phase

    def _base_style(self, *attributes: str) -> str:
        return " ".join((f"fg:{self.base_color}", *attributes)).strip()

    @staticmethod
    def _concealed_character(character: str) -> str:
        width = get_cwidth(character)
        if width == 2:
            return "\N{IDEOGRAPHIC SPACE}"
        if width == 1:
            return " "
        return "\N{WORD JOINER}"

    def _glitch_character(self, character: str, frame: int, index: int) -> tuple[str, str]:
        if get_cwidth(character) != 1:
            return f"bold fg:{self.accent_color}", character
        replacement = self.glitch_characters[
            _seed_value(self.seed, frame, index) % len(self.glitch_characters)
        ]
        return f"bold fg:{self.accent_color}", replacement

    def _inline_glitch_phase(self, index: int, phase_seconds: float) -> float | None:
        fill_duration = self.length * self.interval_seconds
        if self.inline_glitch_chance <= 0 or phase_seconds >= fill_duration:
            return None
        selection = _seed_value(self.seed ^ 0xA5A5A5A5, 0, index) / 0x100000000
        if selection >= self.inline_glitch_chance:
            return None
        stable_at = (index + 1 + self.effect_width + self.settle_width) * self.interval_seconds
        pulse_duration = (1 + self.settle_width) * self.interval_seconds
        latest_start = fill_duration - pulse_duration
        if latest_start <= stable_at:
            return None
        position = _seed_value(self.seed ^ 0x5A5A5A5A, 0, index) / 0x100000000
        start = stable_at + position * (latest_start - stable_at)
        pulse_phase = phase_seconds - start
        return pulse_phase if 0 <= pulse_phase < pulse_duration else None

    def render_character(
        self,
        character: str,
        index: int,
        elapsed_seconds: float,
        animations_enabled: bool,
        source_style: str = "",
    ) -> tuple[str, str]:
        if self.effect is TextEffectKind.TERMINAL_REVEAL:
            if not animations_enabled:
                return "", character
            elapsed_phase = self.cycle_phase(elapsed_seconds)
            if elapsed_phase >= self.burst_duration_seconds or math.isclose(
                elapsed_phase,
                self.burst_duration_seconds,
                abs_tol=1e-9,
            ):
                return "", character
            frame = int((elapsed_phase + 1e-12) * self.frames_per_second)
            phase_seconds = frame / self.frames_per_second
            frontier = int(phase_seconds / self.interval_seconds)
            revealed = min(self.length, frontier)
            if index >= revealed:
                return "", self._concealed_character(character)
            trail_position = frontier - 1 - index
            if trail_position >= self.effect_width + self.settle_width or character.isspace():
                inline_phase = self._inline_glitch_phase(index, phase_seconds)
                if inline_phase is None or character.isspace():
                    return "", character
                if inline_phase < self.interval_seconds:
                    return self._glitch_character(character, frame, index)
                target_color = _style_foreground_color(source_style) or self.base_color
                fade_duration = self.settle_width * self.interval_seconds
                if fade_duration <= 0:
                    return "", character
                progress = (inline_phase - self.interval_seconds) / fade_duration
                color = interpolate_color(self.accent_color, target_color, progress)
                return f"fg:{color}", character
            if trail_position >= self.effect_width:
                settle_position = trail_position - self.effect_width
                if self.settle_width <= 1 or settle_position + 1 >= self.settle_width:
                    return "", character
                target_color = _style_foreground_color(source_style) or self.base_color
                progress = settle_position / (self.settle_width - 1)
                color = interpolate_color(self.accent_color, target_color, progress)
                return f"fg:{color}", character
            return self._glitch_character(character, frame, index)
        if not animations_enabled:
            return self._base_style(), character
        phase_seconds = self.cycle_phase(elapsed_seconds)
        if phase_seconds >= self.burst_duration_seconds:
            return self._base_style(), character
        progress = phase_seconds / self.burst_duration_seconds
        if self.effect is TextEffectKind.CAPITALIZATION_ROLL:
            active = int(phase_seconds / self.interval_seconds) % self.length
            replacement = character.upper() if index == active else character.lower()
            if get_cwidth(replacement) != get_cwidth(character):
                replacement = character
            color = self.accent_color if index == active else self.base_color
            return f"fg:{color}", replacement
        if self.effect is TextEffectKind.CASE_WAVE:
            active = int(phase_seconds / self.interval_seconds) % self.length
            selected = active <= index < active + self.effect_width
            replacement = character.upper() if selected else character.lower()
            if get_cwidth(replacement) != get_cwidth(character):
                replacement = character
            color = self.accent_color if selected else self.base_color
            return f"fg:{color}", replacement
        if self.effect is TextEffectKind.AGE_DECAY:
            return (
                f"fg:{interpolate_color(self.accent_color, self.base_color, progress)}",
                character,
            )
        if self.effect is TextEffectKind.RAINBOW_WAVE:
            color = _rainbow_color(index / max(1, self.length) + progress)
            return f"fg:{color}", character
        if self.effect is TextEffectKind.EMBER:
            spread = 0.55
            local_progress = progress * (1 + spread) - index / max(1, self.length - 1) * spread
            if not 0 <= local_progress < 1:
                return self._base_style(), character
            palette = ("#ffff5f", "#ffaf00", "#ff5f00", "#d70000", self.base_color)
            scaled = local_progress * (len(palette) - 1)
            section = min(len(palette) - 2, int(scaled))
            color = interpolate_color(palette[section], palette[section + 1], scaled - section)
            return f"fg:{color}", character
        if self.effect is TextEffectKind.COLOR_PULSE:
            intensity = math.sin(math.pi * progress)
            return (
                f"fg:{interpolate_color(self.base_color, self.accent_color, intensity)}",
                character,
            )
        if self.effect is TextEffectKind.FROST:
            extent = (
                progress * 2 * (self.length + self.effect_width)
                if progress <= 0.5
                else (1 - progress) * 2 * (self.length + self.effect_width)
            )
            intensity = min(1.0, max(0.0, (extent - index) / self.effect_width))
            return (
                f"fg:{interpolate_color(self.base_color, self.accent_color, intensity)}",
                character,
            )
        if self.effect is TextEffectKind.SPARKLE:
            flash_rate = self.frames_per_second / 2
            frame = int(phase_seconds * flash_rate)
            selected = _sparkle_indexes(self.seed, frame, self.length, self.sparkle_count)
            flash_progress = (phase_seconds * flash_rate) % 1
            intensity = (
                0.25 + 0.75 * math.sin(math.pi * flash_progress) if index in selected else 0.0
            )
            return (
                f"fg:{interpolate_color(self.base_color, self.accent_color, intensity)}",
                character,
            )

        if self.effect is TextEffectKind.SHIMMER:
            position = progress * (self.length + 2 * self.shimmer_width) - self.shimmer_width
            distance = abs(index - position)
            intensity = max(0.0, 1.0 - distance / self.shimmer_width)
        else:
            position = progress * (self.length - 1 + self.effect_width)
            if self.effect is TextEffectKind.COMET:
                distance = position - index
                intensity = max(0.0, 1.0 - distance / self.effect_width) if distance >= 0 else 0.0
            else:
                distance = abs(index - position)
                intensity = max(0.0, 1.0 - distance / self.effect_width)
        intensity = intensity * intensity * (3 - 2 * intensity)
        if self.effect is TextEffectKind.UNDERLINE_SWEEP:
            return self._base_style("underline" if intensity > 0 else ""), character
        if self.effect is TextEffectKind.BOLD_SWEEP:
            return self._base_style("bold" if intensity > 0 else ""), character
        if self.effect is TextEffectKind.REVERSE_SWEEP:
            return self._base_style("reverse" if intensity > 0 else ""), character
        color = interpolate_color(self.base_color, self.accent_color, intensity)
        return f"fg:{color}", character

    def frame_delay(self, elapsed_seconds: float) -> float | None:
        phase_seconds = self.cycle_phase(elapsed_seconds)
        if self.effect is TextEffectKind.TERMINAL_REVEAL:
            if phase_seconds >= self.burst_duration_seconds or math.isclose(
                phase_seconds,
                self.burst_duration_seconds,
                abs_tol=1e-9,
            ):
                return None
            frame = int((phase_seconds + 1e-12) * self.frames_per_second)
            return max(
                1e-9,
                min(
                    (frame + 1) / self.frames_per_second - phase_seconds,
                    self.burst_duration_seconds - phase_seconds,
                ),
            )
        if phase_seconds >= self.burst_duration_seconds or math.isclose(
            phase_seconds,
            self.burst_duration_seconds,
            abs_tol=1e-9,
        ):
            if not self.loop:
                return None
            return self.repeat_seconds - phase_seconds
        if self.effect in {TextEffectKind.CAPITALIZATION_ROLL, TextEffectKind.CASE_WAVE}:
            step_phase = phase_seconds % self.interval_seconds
            remaining = self.interval_seconds - step_phase
            return min(remaining, self.burst_duration_seconds - phase_seconds)
        else:
            return min(1 / self.frames_per_second, self.burst_duration_seconds - phase_seconds)


def validate_decorations(text_length: int, decorations: tuple[TextDecoration, ...]) -> None:
    for reveal_layer in (False, True):
        previous_end = 0
        layer = (
            decoration
            for decoration in decorations
            if (decoration.effect is TextEffectKind.TERMINAL_REVEAL) is reveal_layer
        )
        for decoration in sorted(layer, key=lambda item: item.start):
            if decoration.end > text_length:
                raise ValueError("text decoration extends beyond visible text")
            if decoration.start < previous_end:
                raise ValueError("text decorations in the same layer cannot overlap")
            previous_end = decoration.end
