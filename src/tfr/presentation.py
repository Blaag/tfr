from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import regex

from tfr.text_effects import interpolate_color, validate_color

PRESENTATION_VERSION = 2
MAX_PRESENTATION_PROGRAMS = 4
MAX_PRESENTATION_VARIANTS = 4
MAX_PRESENTATION_KEYFRAMES = 3
MAX_PRESENTATION_TARGET_CHARACTERS = 4_096
MAX_PRESENTATION_DURATION_SECONDS = 30.0
MAX_PRESENTATION_REPEAT_SECONDS = 60.0
MAX_PRESENTATION_REPEAT_COUNT = 20
MAX_PRESENTATION_TOTAL_SECONDS = 300.0
MAX_PRESENTATION_SWEEP_GRAPHEMES = 100
MAX_PRESENTATION_TRAIL_WIDTH = 8


class PresentationCapability(StrEnum):
    FOREGROUND_COLOR = "foreground_color"
    BOLD = "bold"
    UNDERLINE = "underline"
    TIMELINE = "timeline"
    CHARACTER_FOREGROUND = "character_foreground"
    CHARACTER_CASE = "character_case"
    CHARACTER_STYLE = "character_style"
    CHARACTER_TEXT = "character_text"


class SweepTrailMode(StrEnum):
    SYMMETRIC = "symmetric"
    BEHIND = "behind"
    AHEAD = "ahead"


TUI_PRESENTATION_CAPABILITIES = frozenset(PresentationCapability)


@dataclass(frozen=True, slots=True)
class PresentationStyle:
    foreground: str | None = None
    bold: bool = False
    underline: bool = False
    reverse: bool = False

    def __post_init__(self) -> None:
        if self.foreground is not None:
            object.__setattr__(self, "foreground", validate_color(self.foreground))
        if not all(isinstance(value, bool) for value in (self.bold, self.underline, self.reverse)):
            raise ValueError("presentation style flags must be booleans")

    def as_style(self) -> str:
        values = []
        if self.foreground is not None:
            values.append(f"fg:{self.foreground}")
        if self.bold:
            values.append("bold")
        if self.underline:
            values.append("underline")
        if self.reverse:
            values.append("reverse")
        return " ".join(values)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.foreground is not None:
            value["foreground"] = self.foreground
        if self.bold:
            value["bold"] = True
        if self.underline:
            value["underline"] = True
        if self.reverse:
            value["reverse"] = True
        return value


@dataclass(frozen=True, slots=True)
class ForegroundKeyframe:
    at: float
    color: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.at, bool)
            or not isinstance(self.at, (int, float))
            or not math.isfinite(self.at)
            or not 0 <= self.at <= 1
        ):
            raise ValueError("presentation keyframe position must be between 0 and 1")
        object.__setattr__(self, "at", float(self.at))
        object.__setattr__(self, "color", validate_color(self.color))


@dataclass(frozen=True, slots=True)
class PositionKeyframe:
    at: float
    position: float

    def __post_init__(self) -> None:
        for name, value in (("position", self.position), ("keyframe position", self.at)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"presentation {name} must be between 0 and 1")
        object.__setattr__(self, "at", float(self.at))
        object.__setattr__(self, "position", float(self.position))


@dataclass(frozen=True, slots=True)
class CharacterSweepTrack:
    positions: tuple[PositionKeyframe, ...]
    base_color: str
    head_color: str
    trail_width: float = 2
    uppercase_head: bool = False
    trail_mode: SweepTrailMode = SweepTrailMode.SYMMETRIC
    start_padding: float = 0
    end_padding: float = 0
    fill_behind: bool = False

    def __post_init__(self) -> None:
        if any(type(frame) is not PositionKeyframe for frame in self.positions):
            raise ValueError("presentation character sweep positions are invalid")
        if not 2 <= len(self.positions) <= MAX_PRESENTATION_KEYFRAMES:
            raise ValueError("presentation character sweep must contain 2 or 3 positions")
        times = tuple(frame.at for frame in self.positions)
        if times[0] != 0 or times[-1] != 1:
            raise ValueError("presentation character sweep must start at 0 and end at 1")
        if any(current >= following for current, following in zip(times, times[1:], strict=False)):
            raise ValueError("presentation character sweep positions must increase")
        object.__setattr__(self, "base_color", validate_color(self.base_color))
        object.__setattr__(self, "head_color", validate_color(self.head_color))
        for name, value in (
            ("trail width", self.trail_width),
            ("start padding", self.start_padding),
            ("end padding", self.end_padding),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= MAX_PRESENTATION_TRAIL_WIDTH
                or (name == "trail width" and value < 0.5)
            ):
                lower = 0.5 if name == "trail width" else 0
                raise ValueError(
                    f"presentation character sweep {name} must be between {lower:g} and 8"
                )
            object.__setattr__(self, name.replace(" ", "_"), float(value))
        if not isinstance(self.uppercase_head, bool):
            raise ValueError("presentation character sweep uppercase flag must be boolean")
        if not isinstance(self.fill_behind, bool):
            raise ValueError("presentation character sweep fill flag must be boolean")
        if self.fill_behind and self.trail_mode is not SweepTrailMode.BEHIND:
            raise ValueError("presentation character sweep fill requires a behind trail")
        if not isinstance(self.trail_mode, SweepTrailMode):
            raise ValueError("presentation character sweep trail mode is invalid")


@dataclass(frozen=True, slots=True)
class CharacterStyleSweepTrack:
    positions: tuple[PositionKeyframe, ...]
    base_style: PresentationStyle
    active_style: PresentationStyle
    width: float = 1
    trail_mode: SweepTrailMode = SweepTrailMode.SYMMETRIC
    start_padding: float = 0
    end_padding: float = 0
    uppercase_active: bool = False
    lowercase_inactive: bool = False
    stepped: bool = False

    def __post_init__(self) -> None:
        CharacterSweepTrack(
            positions=self.positions,
            base_color=self.base_style.foreground or "#ffffff",
            head_color=self.active_style.foreground or self.base_style.foreground or "#ffffff",
            trail_width=self.width,
            trail_mode=self.trail_mode,
            start_padding=self.start_padding,
            end_padding=self.end_padding,
        )
        if (
            type(self.base_style) is not PresentationStyle
            or type(self.active_style) is not PresentationStyle
        ):
            raise ValueError("presentation character style sweep styles are invalid")
        for name, value in (
            ("uppercase active", self.uppercase_active),
            ("lowercase inactive", self.lowercase_inactive),
            ("stepped", self.stepped),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"presentation character style sweep {name} flag must be boolean")


@dataclass(frozen=True, slots=True)
class CharacterSparkleTrack:
    base_color: str
    accent_color: str
    count: int = 2
    seed: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_color", validate_color(self.base_color))
        object.__setattr__(self, "accent_color", validate_color(self.accent_color))
        if (
            not isinstance(self.count, int)
            or isinstance(self.count, bool)
            or not 1 <= self.count <= 10
        ):
            raise ValueError("presentation sparkle count must be between 1 and 10")
        if (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not 0 <= self.seed <= 0xFFFFFFFF
        ):
            raise ValueError("presentation sparkle seed must be an unsigned 32-bit integer")


@dataclass(frozen=True, slots=True)
class CharacterRevealTrack:
    interval_seconds: float
    base_color: str
    accent_color: str
    glitch_width: int
    settle_width: int
    seed: int
    glitch_characters: str
    inline_glitch_chance: float = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_color", validate_color(self.base_color))
        object.__setattr__(self, "accent_color", validate_color(self.accent_color))
        if (
            not isinstance(self.interval_seconds, (int, float))
            or isinstance(self.interval_seconds, bool)
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds <= 0
        ):
            raise ValueError("presentation reveal interval must be positive")
        object.__setattr__(self, "interval_seconds", float(self.interval_seconds))
        for name, value, lower in (
            ("glitch width", self.glitch_width, 1),
            ("settle width", self.settle_width, 0),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= 20:
                raise ValueError(f"presentation reveal {name} must be between {lower} and 20")
        if (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or not 0 <= self.seed <= 0xFFFFFFFF
        ):
            raise ValueError("presentation reveal seed must be an unsigned 32-bit integer")
        if (
            not self.glitch_characters
            or len(self.glitch_characters) > 256
            or any(
                not character.isascii() or not character.isprintable() or character.isspace()
                for character in self.glitch_characters
            )
        ):
            raise ValueError("presentation reveal characters must be bounded visible ASCII")
        if (
            not isinstance(self.inline_glitch_chance, (int, float))
            or isinstance(self.inline_glitch_chance, bool)
            or not math.isfinite(self.inline_glitch_chance)
            or not 0 <= self.inline_glitch_chance <= 1
        ):
            raise ValueError("presentation reveal inline glitch chance must be between 0 and 1")
        object.__setattr__(self, "inline_glitch_chance", float(self.inline_glitch_chance))


@dataclass(frozen=True, slots=True)
class CharacterHueWaveTrack:
    base_color: str
    saturation: float = 0.78
    brightness: float = 1.0
    spatial_cycles: float = 1.0
    temporal_cycles: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_color", validate_color(self.base_color))
        for name, value, lower, upper in (
            ("saturation", self.saturation, 0.0, 1.0),
            ("brightness", self.brightness, 0.1, 1.0),
            ("spatial cycles", self.spatial_cycles, 0.1, 4.0),
            ("temporal cycles", self.temporal_cycles, 0.1, 4.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(
                    f"presentation hue wave {name} must be between {lower:g} and {upper:g}"
                )
            object.__setattr__(self, name.replace(" ", "_"), float(value))


@dataclass(frozen=True, slots=True)
class CharacterPaletteFrontTrack:
    colors: tuple[str, ...]
    spread: float = 0.55
    direction: str = "forward"

    def __post_init__(self) -> None:
        if not 2 <= len(self.colors) <= 8:
            raise ValueError("presentation palette front must contain 2 to 8 colors")
        object.__setattr__(self, "colors", tuple(validate_color(color) for color in self.colors))
        if (
            isinstance(self.spread, bool)
            or not isinstance(self.spread, (int, float))
            or not math.isfinite(self.spread)
            or not 0 <= self.spread <= 1
        ):
            raise ValueError("presentation palette front spread must be between 0 and 1")
        object.__setattr__(self, "spread", float(self.spread))
        if self.direction not in {"forward", "backward"}:
            raise ValueError("presentation palette front direction is invalid")


@dataclass(frozen=True, slots=True)
class PresentationVariant:
    requires: tuple[PresentationCapability, ...]
    style: PresentationStyle = PresentationStyle()
    foreground_keyframes: tuple[ForegroundKeyframe, ...] = ()
    character_sweep: CharacterSweepTrack | None = None
    character_hue_wave: CharacterHueWaveTrack | None = None
    character_palette_front: CharacterPaletteFrontTrack | None = None
    character_style_sweep: CharacterStyleSweepTrack | None = None
    character_sparkle: CharacterSparkleTrack | None = None
    character_reveal: CharacterRevealTrack | None = None

    def __post_init__(self) -> None:
        if not self.requires or len(self.requires) > len(PresentationCapability):
            raise ValueError("presentation variant must require bounded capabilities")
        if any(not isinstance(item, PresentationCapability) for item in self.requires):
            raise ValueError("presentation variant capability is invalid")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("presentation variant capabilities must be unique")
        used: set[PresentationCapability] = set()
        if self.style.foreground is not None:
            used.add(PresentationCapability.FOREGROUND_COLOR)
        if self.style.bold:
            used.add(PresentationCapability.BOLD)
        if self.style.underline:
            used.add(PresentationCapability.UNDERLINE)
        if self.style.reverse:
            used.add(PresentationCapability.CHARACTER_STYLE)
        if self.foreground_keyframes:
            used.update(
                {
                    PresentationCapability.FOREGROUND_COLOR,
                    PresentationCapability.TIMELINE,
                }
            )
            if not 2 <= len(self.foreground_keyframes) <= MAX_PRESENTATION_KEYFRAMES:
                raise ValueError("presentation timeline must contain 2 or 3 keyframes")
            positions = tuple(frame.at for frame in self.foreground_keyframes)
            if positions[0] != 0 or positions[-1] != 1:
                raise ValueError("presentation timeline must start at 0 and end at 1")
            if any(
                current >= following
                for current, following in zip(positions, positions[1:], strict=False)
            ):
                raise ValueError("presentation keyframe positions must increase")
            if self.style.foreground is None:
                raise ValueError("presentation timeline requires a static foreground style")
        if self.character_sweep is not None:
            used.update(
                {
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.TIMELINE,
                }
            )
            if self.character_sweep.uppercase_head:
                used.add(PresentationCapability.CHARACTER_CASE)
        for track in (self.character_hue_wave, self.character_palette_front):
            if track is not None:
                used.update(
                    {
                        PresentationCapability.CHARACTER_FOREGROUND,
                        PresentationCapability.TIMELINE,
                    }
                )
        if self.character_style_sweep is not None:
            used.update({PresentationCapability.CHARACTER_STYLE, PresentationCapability.TIMELINE})
            styles = (
                self.character_style_sweep.base_style,
                self.character_style_sweep.active_style,
            )
            if any(style.foreground is not None for style in styles):
                used.add(PresentationCapability.CHARACTER_FOREGROUND)
            if (
                self.character_style_sweep.uppercase_active
                or self.character_style_sweep.lowercase_inactive
            ):
                used.add(PresentationCapability.CHARACTER_CASE)
        if self.character_sparkle is not None:
            used.update(
                {PresentationCapability.CHARACTER_FOREGROUND, PresentationCapability.TIMELINE}
            )
        if self.character_reveal is not None:
            used.update(
                {
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.CHARACTER_TEXT,
                    PresentationCapability.TIMELINE,
                }
            )
        tracks = sum(
            track is not None
            for track in (
                self.character_sweep,
                self.character_hue_wave,
                self.character_palette_front,
                self.character_style_sweep,
                self.character_sparkle,
                self.character_reveal,
            )
        )
        if bool(self.foreground_keyframes) + tracks > 1:
            raise ValueError("presentation variant supports only one timeline track")
        if not used:
            raise ValueError("presentation variant must apply a style or timeline")
        if not used <= set(self.requires):
            raise ValueError("presentation variant does not declare all used capabilities")


@dataclass(frozen=True, slots=True)
class EffectProgram:
    start: int
    end: int
    variants: tuple[PresentationVariant, ...]
    reduced_motion: PresentationStyle = PresentationStyle()
    fallback: PresentationStyle = PresentationStyle()
    duration_seconds: float = 1.0
    repeat_seconds: float = 1.0
    repeat_count: int = 1
    frames_per_second: float = 20.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not isinstance(self.start, int)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end <= self.start
        ):
            raise ValueError("presentation target must be ordered and non-empty")
        if self.end - self.start > MAX_PRESENTATION_TARGET_CHARACTERS:
            raise ValueError("presentation target is too large")
        if not 1 <= len(self.variants) <= MAX_PRESENTATION_VARIANTS:
            raise ValueError("presentation program must contain 1 to 4 variants")
        for name, value, lower, upper in (
            (
                "duration",
                self.duration_seconds,
                0.1,
                MAX_PRESENTATION_DURATION_SECONDS,
            ),
            ("repeat interval", self.repeat_seconds, 0.1, MAX_PRESENTATION_REPEAT_SECONDS),
            ("frame rate", self.frames_per_second, 1.0, 30.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(f"presentation {name} must be between {lower:g} and {upper:g}")
        object.__setattr__(self, "duration_seconds", float(self.duration_seconds))
        object.__setattr__(self, "repeat_seconds", float(self.repeat_seconds))
        object.__setattr__(self, "frames_per_second", float(self.frames_per_second))
        if self.repeat_seconds < self.duration_seconds:
            raise ValueError("presentation repeat interval cannot be shorter than its duration")
        if (
            isinstance(self.repeat_count, bool)
            or not isinstance(self.repeat_count, int)
            or not 1 <= self.repeat_count <= MAX_PRESENTATION_REPEAT_COUNT
        ):
            raise ValueError("presentation repeat count must be between 1 and 20")
        if self.repeat_seconds * self.repeat_count > MAX_PRESENTATION_TOTAL_SECONDS:
            raise ValueError("presentation total lifetime cannot exceed 300 seconds")


@dataclass(frozen=True, slots=True)
class ActiveEffectProgram:
    program: EffectProgram
    phase_offset_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.phase_offset_seconds):
            raise ValueError("presentation phase offset must be finite")

    def style_at(self, elapsed_seconds: float, animations_enabled: bool) -> PresentationStyle:
        return presentation_style_at(
            self.program,
            elapsed_seconds + self.phase_offset_seconds,
            animations_enabled=animations_enabled,
            capabilities=TUI_PRESENTATION_CAPABILITIES,
        )

    def frame_delay(self, elapsed_seconds: float) -> float | None:
        return presentation_frame_delay(self.program, elapsed_seconds + self.phase_offset_seconds)

    def render_grapheme(
        self,
        grapheme: str,
        index: int,
        count: int,
        elapsed_seconds: float,
        animations_enabled: bool,
    ) -> tuple[PresentationStyle, str]:
        return presentation_grapheme_at(
            self.program,
            grapheme,
            index,
            count,
            elapsed_seconds + self.phase_offset_seconds,
            animations_enabled=animations_enabled,
            capabilities=TUI_PRESENTATION_CAPABILITIES,
        )


def validate_effect_programs(text: str, programs: tuple[EffectProgram, ...]) -> None:
    if len(programs) > MAX_PRESENTATION_PROGRAMS:
        raise ValueError("too many presentation programs for one event")
    boundaries = {0}
    offset = 0
    for match in regex.finditer(r"\X", text):
        offset += len(match.group())
        boundaries.add(offset)
    for program in sorted(programs, key=lambda item: item.start):
        if type(program) is not EffectProgram:
            raise ValueError("presentation decorator must return effect programs")
        if any(type(variant) is not PresentationVariant for variant in program.variants):
            raise ValueError("presentation program variants are invalid")
        if (
            any(
                type(variant.style) is not PresentationStyle
                or any(
                    type(frame) is not ForegroundKeyframe for frame in variant.foreground_keyframes
                )
                or (
                    variant.character_sweep is not None
                    and (
                        type(variant.character_sweep) is not CharacterSweepTrack
                        or any(
                            type(frame) is not PositionKeyframe
                            for frame in variant.character_sweep.positions
                        )
                    )
                )
                or (
                    variant.character_style_sweep is not None
                    and (
                        type(variant.character_style_sweep) is not CharacterStyleSweepTrack
                        or any(
                            type(frame) is not PositionKeyframe
                            for frame in variant.character_style_sweep.positions
                        )
                    )
                )
                or (
                    variant.character_sparkle is not None
                    and type(variant.character_sparkle) is not CharacterSparkleTrack
                )
                or (
                    variant.character_reveal is not None
                    and type(variant.character_reveal) is not CharacterRevealTrack
                )
                for variant in program.variants
            )
            or type(program.reduced_motion) is not PresentationStyle
            or type(program.fallback) is not PresentationStyle
        ):
            raise ValueError("presentation program contains invalid typed values")
        if program.end > len(text):
            raise ValueError("presentation target extends beyond visible text")
        if program.start not in boundaries or program.end not in boundaries:
            raise ValueError("presentation target must align with grapheme boundaries")
        if any(
            variant.character_sweep is not None
            or variant.character_style_sweep is not None
            or variant.character_sparkle is not None
            for variant in program.variants
        ):
            target = text[program.start : program.end]
            if len(tuple(regex.finditer(r"\X", target))) > MAX_PRESENTATION_SWEEP_GRAPHEMES:
                raise ValueError("presentation character sweep target is too large")


def select_presentation_variant(
    program: EffectProgram,
    capabilities: frozenset[PresentationCapability],
) -> PresentationVariant | None:
    return next(
        (variant for variant in program.variants if set(variant.requires) <= capabilities),
        None,
    )


def presentation_style_at(
    program: EffectProgram,
    elapsed_seconds: float,
    *,
    animations_enabled: bool,
    capabilities: frozenset[PresentationCapability],
) -> PresentationStyle:
    if not animations_enabled:
        return program.reduced_motion
    variant = select_presentation_variant(program, capabilities)
    if variant is None:
        return program.fallback
    elapsed = max(0.0, elapsed_seconds)
    cycle = int(elapsed // program.repeat_seconds)
    phase = elapsed % program.repeat_seconds
    if cycle >= program.repeat_count or phase >= program.duration_seconds:
        return variant.style
    if not variant.foreground_keyframes:
        return variant.style
    progress = phase / program.duration_seconds
    before = variant.foreground_keyframes[0]
    after = variant.foreground_keyframes[-1]
    for candidate in variant.foreground_keyframes[1:]:
        after = candidate
        if progress <= candidate.at:
            break
        before = candidate
    span = after.at - before.at
    local_progress = 1.0 if span == 0 else (progress - before.at) / span
    return PresentationStyle(
        foreground=interpolate_color(before.color, after.color, local_progress),
        bold=variant.style.bold,
        underline=variant.style.underline,
        reverse=variant.style.reverse,
    )


def _timeline_value(keyframes: tuple[Any, ...], progress: float, attribute: str) -> float:
    before = keyframes[0]
    after = keyframes[-1]
    for candidate in keyframes[1:]:
        after = candidate
        if progress <= candidate.at:
            break
        before = candidate
    span = after.at - before.at
    local = 1.0 if span == 0 else (progress - before.at) / span
    start = getattr(before, attribute)
    return start + (getattr(after, attribute) - start) * local


def _effect_seed_value(seed: int, frame: int, index: int) -> int:
    value = (seed ^ (frame * 0x9E3779B1) ^ (index * 0x85EBCA77)) & 0xFFFFFFFF
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    return value


def _style_sweep_selected(
    track: CharacterStyleSweepTrack, index: int, count: int, progress: float
) -> bool:
    position = _timeline_value(track.positions, progress, "position")
    head = -track.start_padding + position * (
        max(0, count - 1) + track.start_padding + track.end_padding
    )
    if track.stepped:
        head = math.floor(head + 1e-9)
    delta = index - head
    distance = (
        abs(delta)
        if track.trail_mode is SweepTrailMode.SYMMETRIC
        else -delta
        if track.trail_mode is SweepTrailMode.BEHIND and delta <= 0
        else delta
        if track.trail_mode is SweepTrailMode.AHEAD and delta >= 0
        else math.inf
    )
    return distance < track.width


def _reveal_glitch(track: CharacterRevealTrack, frame: int, index: int) -> str:
    value = _effect_seed_value(track.seed, frame, index)
    return track.glitch_characters[value % len(track.glitch_characters)]


def presentation_grapheme_at(
    program: EffectProgram,
    grapheme: str,
    index: int,
    count: int,
    elapsed_seconds: float,
    *,
    animations_enabled: bool,
    capabilities: frozenset[PresentationCapability],
) -> tuple[PresentationStyle, str]:
    if not animations_enabled:
        return program.reduced_motion, grapheme
    variant = select_presentation_variant(program, capabilities)
    if variant is None:
        return program.fallback, grapheme
    character_track = (
        variant.character_sweep
        or variant.character_hue_wave
        or variant.character_palette_front
        or variant.character_style_sweep
        or variant.character_sparkle
        or variant.character_reveal
    )
    if character_track is None:
        return presentation_style_at(
            program,
            elapsed_seconds,
            animations_enabled=True,
            capabilities=capabilities,
        ), grapheme
    elapsed = max(0.0, elapsed_seconds)
    cycle = int(elapsed // program.repeat_seconds)
    phase = elapsed % program.repeat_seconds
    if cycle >= program.repeat_count or phase >= program.duration_seconds:
        return program.fallback, grapheme
    progress = phase / program.duration_seconds
    if variant.character_sweep is not None:
        sweep = variant.character_sweep
        position = _timeline_value(sweep.positions, progress, "position")
        head = -sweep.start_padding + position * (
            max(0, count - 1) + sweep.start_padding + sweep.end_padding
        )
        active_head = round(head) if sweep.uppercase_head else head
        delta = index - active_head
        distance = (
            abs(delta)
            if sweep.trail_mode is SweepTrailMode.SYMMETRIC
            else -delta
            if sweep.trail_mode is SweepTrailMode.BEHIND and delta <= 0
            else delta
            if sweep.trail_mode is SweepTrailMode.AHEAD and delta >= 0
            else math.inf
        )
        intensity = max(0.0, 1.0 - distance / sweep.trail_width)
        if sweep.fill_behind and math.isfinite(distance):
            intensity = min(1.0, distance / sweep.trail_width)
        intensity = intensity * intensity * (3 - 2 * intensity)
        rendered = grapheme
        if sweep.uppercase_head and grapheme.isascii() and grapheme.isalpha():
            rendered = grapheme.upper() if index == active_head else grapheme.lower()
        return PresentationStyle(
            foreground=interpolate_color(sweep.base_color, sweep.head_color, intensity)
        ), rendered
    if variant.character_style_sweep is not None:
        track = variant.character_style_sweep
        selected = _style_sweep_selected(track, index, count, progress)
        rendered = grapheme
        if grapheme.isascii() and grapheme.isalpha():
            if selected and track.uppercase_active:
                rendered = grapheme.upper()
            elif not selected and track.lowercase_inactive:
                rendered = grapheme.lower()
        return track.active_style if selected else track.base_style, rendered
    if variant.character_sparkle is not None:
        sparkle = variant.character_sparkle
        phase_seconds = progress * program.duration_seconds
        flash_rate = program.frames_per_second / 2
        frame = int(phase_seconds * flash_rate)
        selected = sorted(
            range(count), key=lambda candidate: _effect_seed_value(sparkle.seed, frame, candidate)
        )[: min(sparkle.count, count)]
        flash_progress = (phase_seconds * flash_rate) % 1
        intensity = 0.25 + 0.75 * math.sin(math.pi * flash_progress) if index in selected else 0
        return PresentationStyle(
            foreground=interpolate_color(sparkle.base_color, sparkle.accent_color, intensity)
        ), grapheme
    if variant.character_reveal is not None:
        reveal = variant.character_reveal
        phase_seconds = progress * program.duration_seconds
        frame = int((phase_seconds + 1e-12) * program.frames_per_second)
        frontier = int(phase_seconds / reveal.interval_seconds)
        revealed = min(count, frontier)
        if index >= revealed:
            return PresentationStyle(), " "
        trail_position = frontier - 1 - index
        if trail_position < reveal.glitch_width:
            return PresentationStyle(foreground=reveal.accent_color), _reveal_glitch(
                reveal, frame, index
            )
        if trail_position < reveal.glitch_width + reveal.settle_width:
            settle_position = trail_position - reveal.glitch_width
            progress_to_base = (
                1.0 if reveal.settle_width <= 1 else settle_position / (reveal.settle_width - 1)
            )
            return PresentationStyle(
                foreground=interpolate_color(
                    reveal.accent_color, reveal.base_color, progress_to_base
                )
            ), grapheme
        fill_duration = count * reveal.interval_seconds
        selection = _effect_seed_value(reveal.seed ^ 0xA5A5A5A5, 0, index) / 0x100000000
        if reveal.inline_glitch_chance > 0 and selection < reveal.inline_glitch_chance:
            stable_at = (
                index + 1 + reveal.glitch_width + reveal.settle_width
            ) * reveal.interval_seconds
            pulse_duration = (1 + reveal.settle_width) * reveal.interval_seconds
            latest_start = fill_duration - pulse_duration
            if latest_start > stable_at:
                position = _effect_seed_value(reveal.seed ^ 0x5A5A5A5A, 0, index) / 0x100000000
                pulse_phase = phase_seconds - (stable_at + position * (latest_start - stable_at))
                if 0 <= pulse_phase < pulse_duration:
                    if pulse_phase < reveal.interval_seconds:
                        return PresentationStyle(
                            foreground=reveal.accent_color, bold=True
                        ), _reveal_glitch(reveal, frame, index)
                    settle = (pulse_phase - reveal.interval_seconds) / max(
                        reveal.interval_seconds, reveal.settle_width * reveal.interval_seconds
                    )
                    return PresentationStyle(
                        foreground=interpolate_color(reveal.accent_color, reveal.base_color, settle)
                    ), grapheme
        return PresentationStyle(), grapheme
    if variant.character_hue_wave is not None:
        wave = variant.character_hue_wave
        hue = (index / max(1, count) * wave.spatial_cycles + progress * wave.temporal_cycles) % 1
        red, green, blue = colorsys.hsv_to_rgb(hue, wave.saturation, wave.brightness)
        return PresentationStyle(
            foreground=f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"
        ), grapheme
    front = variant.character_palette_front
    if front is None:
        return program.fallback, grapheme
    normalized = index / max(1, count - 1)
    if front.direction == "backward":
        normalized = 1 - normalized
    local_progress = progress * (1 + front.spread) - normalized * front.spread
    if not 0 <= local_progress < 1:
        return PresentationStyle(foreground=front.colors[-1]), grapheme
    scaled = local_progress * (len(front.colors) - 1)
    section = min(len(front.colors) - 2, int(scaled))
    return PresentationStyle(
        foreground=interpolate_color(
            front.colors[section], front.colors[section + 1], scaled - section
        )
    ), grapheme


def presentation_frame_delay(program: EffectProgram, elapsed_seconds: float) -> float | None:
    variant = select_presentation_variant(program, TUI_PRESENTATION_CAPABILITIES)
    if variant is None or (
        not variant.foreground_keyframes
        and variant.character_sweep is None
        and variant.character_hue_wave is None
        and variant.character_palette_front is None
        and variant.character_style_sweep is None
        and variant.character_sparkle is None
        and variant.character_reveal is None
    ):
        return None
    elapsed = max(0.0, elapsed_seconds)
    cycle = int(elapsed // program.repeat_seconds)
    if cycle >= program.repeat_count:
        return None
    phase = elapsed % program.repeat_seconds
    if phase >= program.duration_seconds:
        if cycle + 1 >= program.repeat_count:
            return None
        return program.repeat_seconds - phase
    return min(1 / program.frames_per_second, program.duration_seconds - phase)


def effect_programs_as_dict(programs: tuple[EffectProgram, ...]) -> dict[str, Any]:
    version = (
        2
        if any(
            program.duration_seconds > 3
            or program.reduced_motion.reverse
            or program.fallback.reverse
            or any(
                variant.style.reverse
                or variant.character_style_sweep is not None
                or variant.character_sparkle is not None
                or variant.character_reveal is not None
                or bool(variant.character_sweep and variant.character_sweep.fill_behind)
                for variant in program.variants
            )
            for program in programs
        )
        else 1
    )
    return {
        "version": version,
        "programs": [
            {
                "start": program.start,
                "end": program.end,
                "duration_ms": round(program.duration_seconds * 1_000),
                "repeat_ms": round(program.repeat_seconds * 1_000),
                "repeat_count": program.repeat_count,
                "frames_per_second": program.frames_per_second,
                "variants": [
                    {
                        "requires": [capability.value for capability in variant.requires],
                        "style": variant.style.as_dict(),
                        "foreground_keyframes": [
                            {"at": frame.at, "color": frame.color}
                            for frame in variant.foreground_keyframes
                        ],
                        **(
                            {
                                "character_sweep": {
                                    "positions": [
                                        {"at": frame.at, "position": frame.position}
                                        for frame in variant.character_sweep.positions
                                    ],
                                    "base_color": variant.character_sweep.base_color,
                                    "head_color": variant.character_sweep.head_color,
                                    "trail_width": variant.character_sweep.trail_width,
                                    "uppercase_head": variant.character_sweep.uppercase_head,
                                    **(
                                        {"trail_mode": variant.character_sweep.trail_mode.value}
                                        if variant.character_sweep.trail_mode
                                        is not SweepTrailMode.SYMMETRIC
                                        else {}
                                    ),
                                    **(
                                        {"start_padding": variant.character_sweep.start_padding}
                                        if variant.character_sweep.start_padding
                                        else {}
                                    ),
                                    **(
                                        {"end_padding": variant.character_sweep.end_padding}
                                        if variant.character_sweep.end_padding
                                        else {}
                                    ),
                                    **(
                                        {"fill_behind": True}
                                        if variant.character_sweep.fill_behind
                                        else {}
                                    ),
                                }
                            }
                            if variant.character_sweep is not None
                            else {}
                        ),
                        **(
                            {
                                "character_style_sweep": {
                                    "positions": [
                                        {"at": frame.at, "position": frame.position}
                                        for frame in variant.character_style_sweep.positions
                                    ],
                                    "base_style": (
                                        variant.character_style_sweep.base_style.as_dict()
                                    ),
                                    "active_style": (
                                        variant.character_style_sweep.active_style.as_dict()
                                    ),
                                    "width": variant.character_style_sweep.width,
                                    "trail_mode": variant.character_style_sweep.trail_mode.value,
                                    "start_padding": variant.character_style_sweep.start_padding,
                                    "end_padding": variant.character_style_sweep.end_padding,
                                    "uppercase_active": (
                                        variant.character_style_sweep.uppercase_active
                                    ),
                                    "lowercase_inactive": (
                                        variant.character_style_sweep.lowercase_inactive
                                    ),
                                    "stepped": variant.character_style_sweep.stepped,
                                }
                            }
                            if variant.character_style_sweep is not None
                            else {}
                        ),
                        **(
                            {
                                "character_sparkle": {
                                    "base_color": variant.character_sparkle.base_color,
                                    "accent_color": variant.character_sparkle.accent_color,
                                    "count": variant.character_sparkle.count,
                                    "seed": variant.character_sparkle.seed,
                                }
                            }
                            if variant.character_sparkle is not None
                            else {}
                        ),
                        **(
                            {
                                "character_reveal": {
                                    "interval_ms": round(
                                        variant.character_reveal.interval_seconds * 1_000, 6
                                    ),
                                    "base_color": variant.character_reveal.base_color,
                                    "accent_color": variant.character_reveal.accent_color,
                                    "glitch_width": variant.character_reveal.glitch_width,
                                    "settle_width": variant.character_reveal.settle_width,
                                    "seed": variant.character_reveal.seed,
                                    "glitch_characters": variant.character_reveal.glitch_characters,
                                    "inline_glitch_chance": (
                                        variant.character_reveal.inline_glitch_chance
                                    ),
                                }
                            }
                            if variant.character_reveal is not None
                            else {}
                        ),
                        **(
                            {
                                "character_hue_wave": {
                                    "base_color": variant.character_hue_wave.base_color,
                                    "saturation": variant.character_hue_wave.saturation,
                                    "brightness": variant.character_hue_wave.brightness,
                                    "spatial_cycles": variant.character_hue_wave.spatial_cycles,
                                    "temporal_cycles": variant.character_hue_wave.temporal_cycles,
                                }
                            }
                            if variant.character_hue_wave is not None
                            else {}
                        ),
                        **(
                            {
                                "character_palette_front": {
                                    "colors": list(variant.character_palette_front.colors),
                                    "spread": variant.character_palette_front.spread,
                                    "direction": variant.character_palette_front.direction,
                                }
                            }
                            if variant.character_palette_front is not None
                            else {}
                        ),
                    }
                    for variant in program.variants
                ],
                "reduced_motion": program.reduced_motion.as_dict(),
                "fallback": program.fallback.as_dict(),
            }
            for program in programs
        ],
    }


def color_pulse(
    start: int,
    end: int,
    *,
    base_color: str,
    accent_color: str,
    duration_seconds: float = 1.2,
    repeat_seconds: float = 6.0,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    base = PresentationStyle(foreground=base_color)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(
                    PresentationCapability.FOREGROUND_COLOR,
                    PresentationCapability.TIMELINE,
                ),
                style=base,
                foreground_keyframes=(
                    ForegroundKeyframe(0, base_color),
                    ForegroundKeyframe(0.5, accent_color),
                    ForegroundKeyframe(1, base_color),
                ),
            ),
        ),
        reduced_motion=reduced_motion or PresentationStyle(foreground=accent_color, bold=True),
        fallback=base,
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
    )


def foreground_transition(
    start: int,
    end: int,
    *,
    start_color: str,
    end_color: str,
    duration_seconds: float,
    repeat_seconds: float | None = None,
    repeat_count: int = 1,
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    final = PresentationStyle(foreground=end_color)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(PresentationCapability.FOREGROUND_COLOR, PresentationCapability.TIMELINE),
                style=final,
                foreground_keyframes=(
                    ForegroundKeyframe(0, start_color),
                    ForegroundKeyframe(1, end_color),
                ),
            ),
        ),
        reduced_motion=reduced_motion or final,
        fallback=final,
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds or duration_seconds,
        repeat_count=repeat_count,
    )


def character_sweep(
    start: int,
    end: int,
    *,
    base_color: str,
    head_color: str,
    trail_width: float = 2,
    uppercase_head: bool = False,
    trail_mode: SweepTrailMode = SweepTrailMode.SYMMETRIC,
    start_padding: float = 0,
    end_padding: float = 0,
    fill_behind: bool = False,
    return_to_start: bool = True,
    duration_seconds: float = 2.0,
    repeat_seconds: float = 2.0,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    frames_per_second: float = 20.0,
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    if not isinstance(return_to_start, bool):
        raise ValueError("presentation character sweep return flag must be boolean")
    base = PresentationStyle(foreground=base_color)
    requires = [
        PresentationCapability.CHARACTER_FOREGROUND,
        PresentationCapability.TIMELINE,
    ]
    if uppercase_head:
        requires.append(PresentationCapability.CHARACTER_CASE)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=tuple(requires),
                character_sweep=CharacterSweepTrack(
                    positions=(
                        (PositionKeyframe(0, 0), PositionKeyframe(0.5, 1), PositionKeyframe(1, 0))
                        if return_to_start
                        else (PositionKeyframe(0, 0), PositionKeyframe(1, 1))
                    ),
                    base_color=base_color,
                    head_color=head_color,
                    trail_width=trail_width,
                    uppercase_head=uppercase_head,
                    trail_mode=trail_mode,
                    start_padding=start_padding,
                    end_padding=end_padding,
                    fill_behind=fill_behind,
                ),
            ),
        ),
        reduced_motion=reduced_motion or PresentationStyle(foreground=head_color, bold=True),
        fallback=base,
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
        frames_per_second=frames_per_second,
    )


def character_hue_wave(
    start: int,
    end: int,
    *,
    base_color: str,
    duration_seconds: float = 1.8,
    repeat_seconds: float = 10.0,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    frames_per_second: float = 20.0,
    saturation: float = 0.78,
    brightness: float = 1.0,
    spatial_cycles: float = 1.0,
    temporal_cycles: float = 1.0,
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.TIMELINE,
                ),
                character_hue_wave=CharacterHueWaveTrack(
                    base_color, saturation, brightness, spatial_cycles, temporal_cycles
                ),
            ),
        ),
        reduced_motion=reduced_motion or PresentationStyle(foreground=base_color, bold=True),
        fallback=PresentationStyle(foreground=base_color),
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
        frames_per_second=frames_per_second,
    )


def character_palette_front(
    start: int,
    end: int,
    *,
    colors: tuple[str, ...],
    duration_seconds: float = 1.8,
    repeat_seconds: float = 10.0,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    frames_per_second: float = 20.0,
    spread: float = 0.55,
    direction: str = "forward",
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    track = CharacterPaletteFrontTrack(colors, spread, direction)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.TIMELINE,
                ),
                character_palette_front=track,
            ),
        ),
        reduced_motion=reduced_motion or PresentationStyle(foreground=track.colors[0], bold=True),
        fallback=PresentationStyle(foreground=track.colors[-1]),
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
        frames_per_second=frames_per_second,
    )


def character_style_sweep(
    start: int,
    end: int,
    *,
    base_style: PresentationStyle,
    active_style: PresentationStyle,
    width: float = 1,
    trail_mode: SweepTrailMode = SweepTrailMode.SYMMETRIC,
    start_padding: float = 0,
    end_padding: float = 0,
    uppercase_active: bool = False,
    lowercase_inactive: bool = False,
    stepped: bool = False,
    return_to_start: bool = False,
    duration_seconds: float = 1,
    repeat_seconds: float = 10,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    frames_per_second: float = 20,
    reduced_motion: PresentationStyle | None = None,
) -> EffectProgram:
    positions = (
        (PositionKeyframe(0, 0), PositionKeyframe(0.5, 1), PositionKeyframe(1, 0))
        if return_to_start
        else (PositionKeyframe(0, 0), PositionKeyframe(1, 1))
    )
    requires = [PresentationCapability.CHARACTER_STYLE, PresentationCapability.TIMELINE]
    if base_style.foreground is not None or active_style.foreground is not None:
        requires.append(PresentationCapability.CHARACTER_FOREGROUND)
    if uppercase_active or lowercase_inactive:
        requires.append(PresentationCapability.CHARACTER_CASE)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=tuple(requires),
                character_style_sweep=CharacterStyleSweepTrack(
                    positions=positions,
                    base_style=base_style,
                    active_style=active_style,
                    width=width,
                    trail_mode=trail_mode,
                    start_padding=start_padding,
                    end_padding=end_padding,
                    uppercase_active=uppercase_active,
                    lowercase_inactive=lowercase_inactive,
                    stepped=stepped,
                ),
            ),
        ),
        reduced_motion=reduced_motion or active_style,
        fallback=base_style,
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
        frames_per_second=frames_per_second,
    )


def character_sparkle(
    start: int,
    end: int,
    *,
    base_color: str,
    accent_color: str,
    count: int,
    seed: int,
    duration_seconds: float = 1.2,
    repeat_seconds: float = 10,
    repeat_count: int = MAX_PRESENTATION_REPEAT_COUNT,
    frames_per_second: float = 20,
) -> EffectProgram:
    base = PresentationStyle(foreground=base_color)
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.TIMELINE,
                ),
                character_sparkle=CharacterSparkleTrack(base_color, accent_color, count, seed),
            ),
        ),
        reduced_motion=PresentationStyle(foreground=accent_color, bold=True),
        fallback=base,
        duration_seconds=duration_seconds,
        repeat_seconds=repeat_seconds,
        repeat_count=repeat_count,
        frames_per_second=frames_per_second,
    )


def character_reveal(
    start: int,
    end: int,
    *,
    interval_seconds: float,
    base_color: str,
    accent_color: str,
    glitch_width: int,
    settle_width: int,
    seed: int,
    glitch_characters: str,
    inline_glitch_chance: float = 0,
    frames_per_second: float = 20,
) -> EffectProgram:
    duration = interval_seconds * ((end - start) + glitch_width + settle_width)
    duration = max(0.1, min(MAX_PRESENTATION_DURATION_SECONDS, duration))
    return EffectProgram(
        start=start,
        end=end,
        variants=(
            PresentationVariant(
                requires=(
                    PresentationCapability.CHARACTER_FOREGROUND,
                    PresentationCapability.CHARACTER_TEXT,
                    PresentationCapability.TIMELINE,
                ),
                character_reveal=CharacterRevealTrack(
                    interval_seconds=interval_seconds,
                    base_color=base_color,
                    accent_color=accent_color,
                    glitch_width=glitch_width,
                    settle_width=settle_width,
                    seed=seed,
                    glitch_characters=glitch_characters,
                    inline_glitch_chance=inline_glitch_chance,
                ),
            ),
        ),
        reduced_motion=PresentationStyle(),
        fallback=PresentationStyle(),
        duration_seconds=duration,
        repeat_seconds=duration,
        repeat_count=1,
        frames_per_second=frames_per_second,
    )
