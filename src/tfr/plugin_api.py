from __future__ import annotations

from tfr.ansi import terminal_plain_text
from tfr.borders import BorderCellContext, BorderEdge, BorderFragment
from tfr.clear_effects import (
    MAX_SCREEN_CLEAR_DURATION_SECONDS,
    ScreenClearContext,
    terminal_cell_width,
)
from tfr.events import Direction, Event, EventKind, Provenance
from tfr.plugins import (
    PLUGIN_API_VERSION,
    BossViewContext,
    BossViewEvent,
    BossViewHandle,
    BossWorldStatus,
    DuplicatePluginRegistration,
    EffectDemo,
    EffectDemoSample,
    EventPatch,
    PluginCommandContext,
    PluginLifecycleEvent,
    PluginRegistrar,
    PluginRegistrationError,
    PluginWorldInfo,
)
from tfr.presentation import (
    CharacterSweepTrack,
    EffectProgram,
    ForegroundKeyframe,
    PositionKeyframe,
    PresentationCapability,
    PresentationStyle,
    PresentationVariant,
    character_sweep,
    color_pulse,
)
from tfr.text_effects import (
    TextDecoration,
    TextEffectKind,
    derive_bright_color,
    validate_color,
)
from tfr.world_text import escape_world_text

__all__ = (
    "PLUGIN_API_VERSION",
    "MAX_SCREEN_CLEAR_DURATION_SECONDS",
    "BorderCellContext",
    "BorderEdge",
    "BorderFragment",
    "BossViewContext",
    "BossViewEvent",
    "BossViewHandle",
    "BossWorldStatus",
    "CharacterSweepTrack",
    "Direction",
    "DuplicatePluginRegistration",
    "EffectProgram",
    "EffectDemo",
    "EffectDemoSample",
    "Event",
    "EventKind",
    "EventPatch",
    "ForegroundKeyframe",
    "PluginCommandContext",
    "PluginLifecycleEvent",
    "PluginRegistrar",
    "PluginRegistrationError",
    "PluginWorldInfo",
    "PositionKeyframe",
    "PresentationCapability",
    "PresentationStyle",
    "PresentationVariant",
    "Provenance",
    "ScreenClearContext",
    "TextDecoration",
    "TextEffectKind",
    "derive_bright_color",
    "character_sweep",
    "color_pulse",
    "escape_world_text",
    "terminal_plain_text",
    "terminal_cell_width",
    "validate_color",
)
