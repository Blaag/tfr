from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import regex

from tfr.text_effects import interpolate_color, validate_color

PRESENTATION_VERSION = 1
MAX_PRESENTATION_PROGRAMS = 1
MAX_PRESENTATION_VARIANTS = 4
MAX_PRESENTATION_KEYFRAMES = 3
MAX_PRESENTATION_TARGET_CHARACTERS = 2_048
MAX_PRESENTATION_DURATION_SECONDS = 3.0
MAX_PRESENTATION_REPEAT_SECONDS = 60.0
MAX_PRESENTATION_REPEAT_COUNT = 20
MAX_PRESENTATION_TOTAL_SECONDS = 300.0


class PresentationCapability(StrEnum):
    FOREGROUND_COLOR = "foreground_color"
    BOLD = "bold"
    UNDERLINE = "underline"
    TIMELINE = "timeline"


TUI_PRESENTATION_CAPABILITIES = frozenset(PresentationCapability)


@dataclass(frozen=True, slots=True)
class PresentationStyle:
    foreground: str | None = None
    bold: bool = False
    underline: bool = False

    def __post_init__(self) -> None:
        if self.foreground is not None:
            object.__setattr__(self, "foreground", validate_color(self.foreground))
        if not isinstance(self.bold, bool) or not isinstance(self.underline, bool):
            raise ValueError("presentation style flags must be booleans")

    def as_style(self) -> str:
        values = []
        if self.foreground is not None:
            values.append(f"fg:{self.foreground}")
        if self.bold:
            values.append("bold")
        if self.underline:
            values.append("underline")
        return " ".join(values)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.foreground is not None:
            value["foreground"] = self.foreground
        if self.bold:
            value["bold"] = True
        if self.underline:
            value["underline"] = True
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
class PresentationVariant:
    requires: tuple[PresentationCapability, ...]
    style: PresentationStyle = PresentationStyle()
    foreground_keyframes: tuple[ForegroundKeyframe, ...] = ()

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
                1.0,
                MAX_PRESENTATION_DURATION_SECONDS,
            ),
            ("repeat interval", self.repeat_seconds, 1.0, MAX_PRESENTATION_REPEAT_SECONDS),
            ("frame rate", self.frames_per_second, 1.0, 20.0),
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


def validate_effect_programs(text: str, programs: tuple[EffectProgram, ...]) -> None:
    if len(programs) > MAX_PRESENTATION_PROGRAMS:
        raise ValueError("too many presentation programs for one event")
    boundaries = {0}
    offset = 0
    for match in regex.finditer(r"\X", text):
        offset += len(match.group())
        boundaries.add(offset)
    previous_end = 0
    for program in sorted(programs, key=lambda item: item.start):
        if type(program) is not EffectProgram:
            raise ValueError("presentation decorator must return effect programs")
        if any(type(variant) is not PresentationVariant for variant in program.variants):
            raise ValueError("presentation program variants are invalid")
        if any(
            type(variant.style) is not PresentationStyle
            or any(
                type(frame) is not ForegroundKeyframe
                for frame in variant.foreground_keyframes
            )
            for variant in program.variants
        ) or type(program.reduced_motion) is not PresentationStyle or type(
            program.fallback
        ) is not PresentationStyle:
            raise ValueError("presentation program contains invalid typed values")
        if program.end > len(text):
            raise ValueError("presentation target extends beyond visible text")
        if program.start not in boundaries or program.end not in boundaries:
            raise ValueError("presentation target must align with grapheme boundaries")
        if program.start < previous_end:
            raise ValueError("presentation targets cannot overlap")
        previous_end = program.end


def select_presentation_variant(
    program: EffectProgram,
    capabilities: frozenset[PresentationCapability],
) -> PresentationVariant | None:
    return next(
        (
            variant
            for variant in program.variants
            if set(variant.requires) <= capabilities
        ),
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
    )


def presentation_frame_delay(program: EffectProgram, elapsed_seconds: float) -> float | None:
    variant = select_presentation_variant(program, TUI_PRESENTATION_CAPABILITIES)
    if variant is None or not variant.foreground_keyframes:
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
    return {
        "version": PRESENTATION_VERSION,
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
