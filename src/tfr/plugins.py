from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import math
import re
import secrets
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib.metadata import entry_points
from types import MappingProxyType
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings

from tfr.ansi import terminal_plain_text
from tfr.borders import (
    BorderCellContext,
    BorderEffect,
    BorderFragment,
    validate_border_fragment,
)
from tfr.clear_effects import (
    MAX_SCREEN_CLEAR_DURATION_SECONDS,
    ScreenClearContext,
    ScreenClearEffect,
    screen_clear_fragment_limit,
    validate_screen_clear_frame,
)
from tfr.core import CommandBus, EventBus
from tfr.events import (
    Actor,
    ActorType,
    CommandRequest,
    Confidence,
    Direction,
    Event,
    EventKind,
    Provenance,
)
from tfr.text_effects import TextDecoration, validate_decorations

PLUGIN_API_VERSION = 1
PLUGIN_ENTRY_POINT_GROUP = "tfr.plugins.v1"
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_MAX_BOSS_EVENTS = 200
_MAX_BOSS_EVENT_TEXT = 500
_MAX_BOSS_METADATA_NODES = 200
_MAX_BOSS_RENDER_CHARACTERS = 100_000
_BOSS_STYLE = re.compile(r"^(?:class:[A-Za-z0-9_.-]+(?:\s+class:[A-Za-z0-9_.-]+)*)?$")


def _contains_unsafe_terminal_character(value: str, *, allow_layout: bool = False) -> bool:
    allowed = "\n\t" if allow_layout else ""
    return any(
        (unicodedata.category(character) in {"Cc", "Cf", "Cs"} and character not in allowed)
        for character in value
    )


def _safe_boss_system_field(value: str | None, *, maximum: int = 200) -> str | None:
    if value is None:
        return None
    safe = "".join(
        "_" if unicodedata.category(character) in {"Cc", "Cf", "Cs"} else character
        for character in value
    )
    return safe[:maximum]


def _boss_plugin_source(plugin: str) -> str:
    return f"plugin:{_safe_boss_system_field(plugin, maximum=193) or 'unknown'}"


def _freeze_boss_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    nodes = 0

    def freeze(value: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_BOSS_METADATA_NODES:
            raise ValueError("boss-view event metadata is too large")
        if depth > 4:
            raise ValueError("boss-view event metadata is too deeply nested")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("boss-view event metadata numbers must be finite")
            return value
        if isinstance(value, str):
            if len(value) > _MAX_BOSS_EVENT_TEXT:
                raise ValueError("boss-view event metadata strings cannot exceed 500 characters")
            if _contains_unsafe_terminal_character(value):
                raise ValueError("boss-view event metadata strings must be printable")
            return value
        if isinstance(value, Mapping):
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 100
                    or _contains_unsafe_terminal_character(key)
                ):
                    raise ValueError("boss-view event metadata keys must be short strings")
                frozen[key] = freeze(item, depth + 1)
            return MappingProxyType(frozen)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(freeze(item, depth + 1) for item in value)
        raise TypeError("boss-view event metadata contains an unsupported value")

    return freeze(metadata, 0)


class PluginRegistrationError(ValueError):
    pass


class DuplicatePluginRegistration(PluginRegistrationError):
    pass


class IncompatiblePluginApi(PluginRegistrationError):
    pass


class PluginNotFound(PluginRegistrationError):
    pass


@dataclass(frozen=True, slots=True)
class EventPatch:
    kind: EventKind | None = None
    provenance: Provenance | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    confidence: Confidence | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PluginLifecycleEvent:
    kind: str
    world: str | None = None
    state: str | None = None
    source: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class BossViewEvent:
    kind: str
    text: str | None = None
    world: str | None = None
    state: str | None = None
    source: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("boss-view event kind cannot be empty")
        if len(self.kind) > 100:
            raise ValueError("boss-view event kind cannot exceed 100 characters")
        if _contains_unsafe_terminal_character(self.kind):
            raise ValueError("boss-view event kind must be printable")
        if self.text is not None and not self.text:
            raise ValueError("boss-view event text cannot be empty")
        if self.text is not None and len(self.text) > _MAX_BOSS_EVENT_TEXT:
            raise ValueError(
                f"boss-view event text cannot exceed {_MAX_BOSS_EVENT_TEXT} characters"
            )
        if _contains_unsafe_terminal_character(self.text or ""):
            raise ValueError("boss-view event text must be a single printable line")
        for field_name, value in (
            ("world", self.world),
            ("state", self.state),
            ("source", self.source),
        ):
            if value is not None and (
                len(value) > 200 or _contains_unsafe_terminal_character(value)
            ):
                raise ValueError(f"boss-view event {field_name} must be a short printable string")
        if self.sequence < 0:
            raise ValueError("boss-view event sequence cannot be negative")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("boss-view event timestamp must include a timezone")
        object.__setattr__(self, "metadata", _freeze_boss_metadata(self.metadata))


@dataclass(frozen=True, slots=True)
class BossWorldStatus:
    world: str
    connection_state: str
    received_since_activation: int
    last_activity_at: datetime | None = None
    idle_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BossViewContext:
    world: str
    width: int
    height: int
    events: tuple[BossViewEvent, ...] = ()
    screen: str = ""
    activated_at: datetime | None = None
    elapsed_seconds: float = 0.0
    seed: int = 0
    gateway_connected: bool | None = None
    worlds: tuple[BossWorldStatus, ...] = ()

    def __post_init__(self) -> None:
        if not self.world:
            raise ValueError("boss-view world cannot be empty")
        if self.width < 1 or self.height < 1:
            raise ValueError("boss-view dimensions must be positive")
        if self.elapsed_seconds < 0:
            raise ValueError("boss-view elapsed time cannot be negative")
        if self.seed < 0:
            raise ValueError("boss-view seed cannot be negative")


@dataclass(frozen=True, slots=True)
class PluginKeyBinding:
    plugin: str
    name: str
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PluginWorldInfo:
    server: str
    encoding: str


@dataclass(frozen=True, slots=True)
class PluginBorderEffect:
    plugin: str
    name: str
    handler: BorderEffect
    frames_per_second: float | None
    frame_delay: Callable[[float], float | None] | None


@dataclass(frozen=True, slots=True)
class PluginScreenClearEffect:
    plugin: str
    handler: ScreenClearEffect
    duration_seconds: float
    frames_per_second: float
    is_complete: Callable[[], bool] | None


@dataclass(frozen=True, slots=True)
class PluginBossView:
    plugin: str
    name: str
    renderer: BossViewRenderer
    refresh_interval_seconds: float | None


class CommandHandler(Protocol):
    def __call__(
        self,
        context: PluginCommandContext,
        arguments: tuple[str, ...],
    ) -> object | Awaitable[object]: ...


class EventEnricher(Protocol):
    def __call__(self, event: Event) -> EventPatch | None: ...


class DisplayTransform(Protocol):
    def __call__(self, event: Event, text: str) -> str | None: ...


class DisplayDecorator(Protocol):
    def __call__(self, event: Event, text: str) -> Sequence[TextDecoration]: ...


class StatusSegment(Protocol):
    def __call__(self, world: str) -> str | None: ...


class KeyHandler(Protocol):
    def __call__(self, context: PluginCommandContext) -> object | Awaitable[object]: ...


class LifecycleHandler(Protocol):
    def __call__(self, event: PluginLifecycleEvent) -> object | Awaitable[object]: ...


class BossViewRenderer(Protocol):
    def __call__(self, context: BossViewContext) -> Sequence[tuple[str, str]]: ...


class DiscoveredPlugin(Protocol):
    name: str

    def load(self) -> object: ...


@dataclass(frozen=True, slots=True)
class BossViewHandle:
    plugin: str
    name: str
    _activate: Callable[[str, str, str], Awaitable[None]]
    _emit: Callable[[str, str, BossViewEvent], bool]

    async def activate(self, world: str) -> None:
        await self._activate(self.plugin, self.name, world)

    def emit(self, event: BossViewEvent) -> bool:
        return self._emit(self.plugin, self.name, event)


class PluginCommandContext:
    def __init__(
        self,
        *,
        plugin: str,
        world: str,
        targets: Mapping[str, UUID],
        command_bus: CommandBus,
        worlds: Mapping[str, PluginWorldInfo] | None = None,
        notice: Callable[[str, str], None] | None = None,
        activate_boss: Callable[[str], Awaitable[None]] | None = None,
        configure_boss: Callable[[str, str | None], str] | None = None,
        boss_status: Callable[[], str] | None = None,
    ) -> None:
        self._plugin = plugin
        self.world = world
        self._targets = targets
        self._command_bus = command_bus
        self._worlds = worlds or {}
        self._notice = notice
        self._activate_boss = activate_boss
        self._configure_boss = configure_boss
        self._boss_status = boss_status

    @property
    def plugin(self) -> str:
        return self._plugin

    @property
    def world_info(self) -> PluginWorldInfo:
        return self._worlds.get(
            self.world,
            PluginWorldInfo(server="generic", encoding="utf-8"),
        )

    async def submit(
        self,
        text: str,
        *,
        world: str | None = None,
        sensitive: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> CommandRequest:
        target = world or self.world
        try:
            session_id = self._targets[target]
        except KeyError as exc:
            raise ValueError(f"unknown world: {target}") from exc
        request = CommandRequest(
            session_id=session_id,
            world=target,
            actor=Actor(ActorType.PLUGIN, self.plugin),
            text=text,
            sensitive=sensitive,
            metadata=dict(metadata or {}),
        )
        await self._command_bus.submit(request)
        return request

    def notice(self, text: str) -> None:
        if self._notice is None:
            raise RuntimeError("UI notices are unavailable in this plugin scope")
        self._notice(self.world, text)

    async def activate_boss(self) -> None:
        if self._activate_boss is None:
            raise RuntimeError("boss views are unavailable in this plugin scope")
        await self._activate_boss(self.world)

    def configure_boss(self, mode: str, screen: str | None = None) -> str:
        if self._configure_boss is None:
            raise RuntimeError("boss views are unavailable in this plugin scope")
        return self._configure_boss(mode, screen)

    def boss_status(self) -> str:
        if self._boss_status is None:
            raise RuntimeError("boss views are unavailable in this plugin scope")
        return self._boss_status()


class PluginRegistry:
    def __init__(self) -> None:
        self.commands: dict[str, tuple[str, CommandHandler]] = {}
        self.command_help: dict[str, str] = {}
        self.enrichers: dict[str, tuple[str, EventEnricher]] = {}
        self.display_transforms: dict[str, tuple[str, DisplayTransform]] = {}
        self.display_decorators: dict[str, tuple[str, DisplayDecorator]] = {}
        self.border_effects: dict[str, PluginBorderEffect] = {}
        self.screen_clear_effects: dict[str, PluginScreenClearEffect] = {}
        self.boss_views: dict[str, PluginBossView] = {}
        self.status_segments: dict[str, tuple[str, StatusSegment]] = {}
        self.key_bindings: dict[str, tuple[PluginKeyBinding, KeyHandler]] = {}
        self.lifecycle_handlers: dict[str, tuple[str, LifecycleHandler]] = {}

    @staticmethod
    def _add(registrations: dict[str, Any], name: str, value: Any) -> None:
        if name in registrations:
            raise DuplicatePluginRegistration(f"duplicate plugin registration: {name}")
        registrations[name] = value


class PluginRegistrar:
    def __init__(
        self,
        plugin: str,
        registry: PluginRegistry,
        scope: Literal["all", "gateway", "ui"] = "all",
        boss_activate: Callable[[str, str, str], Awaitable[None]] | None = None,
        boss_emit: Callable[[BossViewEvent], bool] | None = None,
        boss_view_emit: Callable[[str, str, BossViewEvent], bool] | None = None,
    ) -> None:
        self._plugin = plugin
        self._registry = registry
        self.scope = scope
        self._boss_activate = boss_activate or self._boss_unavailable
        self._boss_emit = (
            (lambda event: boss_emit(replace(event, source=_boss_plugin_source(plugin))))
            if boss_emit is not None
            else (lambda _event: False)
        )
        self._boss_view_emit = boss_view_emit or (lambda _plugin, _name, _event: False)

    @property
    def plugin(self) -> str:
        return self._plugin

    @staticmethod
    async def _boss_unavailable(_plugin: str, _name: str, _world: str) -> None:
        raise RuntimeError("boss views are unavailable in this plugin scope")

    def register_command(
        self,
        name: str,
        handler: CommandHandler,
        *,
        help: str | None = None,
    ) -> None:
        if self.scope == "gateway":
            return
        normalized = name.casefold()
        if not _COMMAND_NAME.fullmatch(normalized):
            raise PluginRegistrationError(f"invalid client command name: {name}")
        PluginRegistry._add(self._registry.commands, normalized, (self.plugin, handler))
        if help:
            self._registry.command_help[normalized] = help

    def register_enricher(self, name: str, handler: EventEnricher) -> None:
        if self.scope == "ui":
            return
        PluginRegistry._add(self._registry.enrichers, name, (self.plugin, handler))

    def register_display_transform(self, name: str, handler: DisplayTransform) -> None:
        if self.scope == "gateway":
            return
        PluginRegistry._add(self._registry.display_transforms, name, (self.plugin, handler))

    def register_display_decorator(self, name: str, handler: DisplayDecorator) -> None:
        if self.scope == "gateway":
            return
        PluginRegistry._add(self._registry.display_decorators, name, (self.plugin, handler))

    def register_border_effect(
        self,
        name: str,
        handler: BorderEffect,
        *,
        frames_per_second: float | None = None,
        frame_delay: Callable[[float], float | None] | None = None,
    ) -> None:
        if self.scope == "gateway":
            return
        if frames_per_second is not None and not 0 < frames_per_second <= 30:
            raise PluginRegistrationError("border effect frame rate must be between 0 and 30")
        PluginRegistry._add(
            self._registry.border_effects,
            name,
            PluginBorderEffect(
                plugin=self.plugin,
                name=name,
                handler=handler,
                frames_per_second=frames_per_second,
                frame_delay=frame_delay,
            ),
        )

    def register_screen_clear_effect(
        self,
        handler: ScreenClearEffect,
        *,
        duration_seconds: float,
        frames_per_second: float,
        is_complete: Callable[[], bool] | None = None,
    ) -> None:
        if self.scope == "gateway":
            return
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or not 0 < duration_seconds <= MAX_SCREEN_CLEAR_DURATION_SECONDS
        ):
            raise PluginRegistrationError(
                "screen-clear duration must be greater than 0 and at most "
                f"{MAX_SCREEN_CLEAR_DURATION_SECONDS:g} seconds"
            )
        if (
            isinstance(frames_per_second, bool)
            or not isinstance(frames_per_second, (int, float))
            or not math.isfinite(frames_per_second)
            or not 0 < frames_per_second <= 30
        ):
            raise PluginRegistrationError("screen-clear frame rate must be between 0 and 30")
        if is_complete is not None and not callable(is_complete):
            raise PluginRegistrationError("screen-clear completion check must be callable")
        PluginRegistry._add(
            self._registry.screen_clear_effects,
            self.plugin,
            PluginScreenClearEffect(
                plugin=self.plugin,
                handler=handler,
                duration_seconds=float(duration_seconds),
                frames_per_second=float(frames_per_second),
                is_complete=is_complete,
            ),
        )

    def register_boss_view(
        self,
        name: str,
        renderer: BossViewRenderer,
        *,
        refresh_interval_seconds: float | None = None,
    ) -> BossViewHandle:
        normalized = name.casefold()
        if not _COMMAND_NAME.fullmatch(normalized):
            raise PluginRegistrationError(f"invalid boss-view name: {name}")
        if refresh_interval_seconds is not None and (
            isinstance(refresh_interval_seconds, bool)
            or not isinstance(refresh_interval_seconds, (int, float))
            or not math.isfinite(refresh_interval_seconds)
            or not 1 <= refresh_interval_seconds <= 60
        ):
            raise PluginRegistrationError(
                "boss-view refresh interval must be between 1 and 60 seconds"
            )
        handle = BossViewHandle(
            plugin=self.plugin,
            name=normalized,
            _activate=self._boss_activate,
            _emit=self._boss_view_emit,
        )
        if self.scope != "gateway":
            PluginRegistry._add(
                self._registry.boss_views,
                normalized,
                PluginBossView(
                    plugin=self.plugin,
                    name=normalized,
                    renderer=renderer,
                    refresh_interval_seconds=(
                        float(refresh_interval_seconds)
                        if refresh_interval_seconds is not None
                        else None
                    ),
                ),
            )
        return handle

    def emit_boss_event(self, event: BossViewEvent) -> bool:
        return self._boss_emit(event)

    def register_status_segment(self, name: str, handler: StatusSegment) -> None:
        if self.scope == "gateway":
            return
        PluginRegistry._add(self._registry.status_segments, name, (self.plugin, handler))

    def register_key_binding(
        self,
        name: str,
        keys: Sequence[str],
        handler: KeyHandler,
    ) -> None:
        if self.scope == "gateway":
            return
        binding = PluginKeyBinding(self.plugin, name, tuple(keys))
        if not binding.keys:
            raise PluginRegistrationError("plugin key binding cannot be empty")
        if any(
            registered.keys == binding.keys
            for registered, _handler in self._registry.key_bindings.values()
        ):
            raise DuplicatePluginRegistration(
                f"duplicate plugin key binding: {' '.join(binding.keys)}"
            )
        try:
            KeyBindings().add(*binding.keys)(lambda _event: None)
        except ValueError as exc:
            raise PluginRegistrationError(f"invalid key binding: {' '.join(binding.keys)}") from exc
        PluginRegistry._add(self._registry.key_bindings, name, (binding, handler))

    def register_lifecycle_handler(self, name: str, handler: LifecycleHandler) -> None:
        PluginRegistry._add(self._registry.lifecycle_handlers, name, (self.plugin, handler))


class PluginManager:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        command_bus: CommandBus,
        targets: Mapping[str, UUID],
        worlds: Mapping[str, PluginWorldInfo] | None = None,
        scope: Literal["all", "gateway", "ui"] = "all",
        builtin_boss_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.targets = MappingProxyType(dict(targets))
        self.worlds = MappingProxyType(dict(worlds or {}))
        self.registry = PluginRegistry()
        self.scope = scope
        self._active_boss_view: str | None = None
        self._active_boss_world: str | None = None
        self._boss_events: deque[BossViewEvent] = deque(maxlen=_MAX_BOSS_EVENTS)
        self._boss_state_handler: Callable[[bool], None] | None = None
        self._notice_handler: Callable[[str, str], None] | None = None
        self._boss_selection_mode = "cycle"
        self._boss_selected_screen: str | None = None
        self._boss_last_screen: str | None = None
        self._boss_activated_at: datetime | None = None
        self._boss_activated_monotonic = 0.0
        self._boss_seed = 0
        self._boss_event_sequence = 0
        self._world_received: dict[str, int] = dict.fromkeys(self.worlds, 0)
        self._world_last_activity: dict[str, datetime] = {}
        self._world_states: dict[str, str] = dict.fromkeys(self.worlds, "unknown")
        self._boss_received_baseline: dict[str, int] = {}
        self._gateway_connected: bool | None = None
        self._connected_worlds: set[str] = set()
        self._queued_world_activity: dict[tuple[str, int | None], PluginLifecycleEvent] = {}
        self._lifecycle_queue: deque[
            tuple[
                PluginLifecycleEvent | None,
                tuple[str, int | None] | None,
                asyncio.Future[None] | None,
                int | None,
            ]
        ] = deque()
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._session_id = uuid4()
        self._sequence = 0
        self._failure_tasks: set[asyncio.Task[Any]] = set()
        self.requested_plugins: tuple[str, ...] = ()
        self._loaded_plugins: list[str] = []
        self.load_failures: dict[str, str] = {}
        if scope != "gateway":
            from tfr.boss import BuiltinBossPlugin

            BuiltinBossPlugin().register(
                self._registrar("tfr.boss"),
                MappingProxyType(dict(builtin_boss_config or {})),
            )

    def _registrar(self, plugin: str) -> PluginRegistrar:
        return PluginRegistrar(
            plugin,
            self.registry,
            self.scope,
            boss_activate=self._activate_boss,
            boss_emit=self.emit_boss_event,
            boss_view_emit=self._emit_for_boss_view,
        )

    @property
    def loaded_plugins(self) -> tuple[str, ...]:
        return tuple(self._loaded_plugins)

    @classmethod
    async def load(
        cls,
        *,
        enabled: Sequence[str],
        config: Mapping[str, Any],
        event_bus: EventBus,
        command_bus: CommandBus,
        targets: Mapping[str, UUID],
        worlds: Mapping[str, PluginWorldInfo] | None = None,
        discovered: Sequence[DiscoveredPlugin] | None = None,
        extra_discovered: Sequence[DiscoveredPlugin] = (),
        scope: Literal["all", "gateway", "ui"] = "all",
    ) -> PluginManager:
        builtin_boss_config = config.get("tfr.boss", {})
        if scope != "gateway" and not isinstance(builtin_boss_config, Mapping):
            raise ValueError("plugins.config.tfr.boss must be an object")
        manager = cls(
            event_bus=event_bus,
            command_bus=command_bus,
            targets=targets,
            worlds=worlds,
            scope=scope,
            builtin_boss_config=builtin_boss_config,
        )
        points = (
            discovered
            if discovered is not None
            else tuple(entry_points().select(group=PLUGIN_ENTRY_POINT_GROUP))
        )
        points = tuple(points) + tuple(extra_discovered)
        manager.requested_plugins = tuple(dict.fromkeys(enabled))
        available: dict[str, DiscoveredPlugin] = {}
        duplicates: set[str] = set()
        for point in points:
            if point.name in available:
                duplicates.add(point.name)
            else:
                available[point.name] = point
        for name in dict.fromkeys(enabled):
            if name in duplicates:
                manager.load_failures[name] = "duplicate plugin entry points"
                await manager.report_failure(name, "load", DuplicatePluginRegistration())
                continue
            point = available.get(name)
            if point is None:
                error = PluginNotFound(f"no {PLUGIN_ENTRY_POINT_GROUP} entry point named {name}")
                manager.load_failures[name] = str(error)
                await manager.report_failure(name, "load", error)
                continue
            registrations = (
                manager.registry.commands,
                manager.registry.command_help,
                manager.registry.enrichers,
                manager.registry.display_transforms,
                manager.registry.display_decorators,
                manager.registry.border_effects,
                manager.registry.screen_clear_effects,
                manager.registry.boss_views,
                manager.registry.status_segments,
                manager.registry.key_bindings,
                manager.registry.lifecycle_handlers,
            )
            snapshots = [dict(registration) for registration in registrations]
            try:
                plugin = point.load()
                api_version = getattr(plugin, "api_version", PLUGIN_API_VERSION)
                if api_version != PLUGIN_API_VERSION:
                    raise IncompatiblePluginApi(
                        f"plugin API {api_version} is incompatible with {PLUGIN_API_VERSION}"
                    )
                register = getattr(plugin, "register", plugin)
                result = register(
                    manager._registrar(name),
                    MappingProxyType(dict(config.get(name, {}))),
                )
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                for registration, snapshot in zip(registrations, snapshots, strict=True):
                    registration.clear()
                    registration.update(snapshot)
                manager.load_failures[name] = type(exc).__name__
                await manager.report_failure(name, "load", exc)
            else:
                manager._loaded_plugins.append(name)
        return manager

    def context(self, plugin: str, world: str) -> PluginCommandContext:
        return PluginCommandContext(
            plugin=plugin,
            world=world,
            targets=self.targets,
            command_bus=self.command_bus,
            worlds=self.worlds,
            notice=self._notice_handler,
            activate_boss=self.activate_selected_boss,
            configure_boss=self.configure_boss_selection,
            boss_status=self.boss_status,
        )

    async def report_failure(
        self,
        plugin: str,
        operation: str,
        error: Exception,
        *,
        world: str = "tfr",
    ) -> None:
        error_type = type(error).__name__
        event = Event(
            session_id=self._session_id,
            world=world,
            connection_generation=0,
            sequence=self._sequence,
            direction=Direction.INTERNAL,
            kind=EventKind.PLUGIN,
            canonical_text=f"Plugin {plugin} failed during {operation}: {error_type}",
            plain_text=f"Plugin {plugin} failed during {operation}: {error_type}",
            display_text=f"Plugin {plugin} failed during {operation}: {error_type}",
            actor=Actor(ActorType.PLUGIN, plugin),
            metadata={
                "plugin": plugin,
                "operation": operation,
                "error_type": error_type,
            },
        )
        self._sequence += 1
        await self.event_bus.publish(event)

    async def process_event(self, event: Event) -> Event:
        if event.direction is Direction.INBOUND:
            for name, (plugin, enricher) in tuple(self.registry.enrichers.items()):
                try:
                    patch = enricher(event)
                    if patch is not None:
                        event = replace(
                            event,
                            kind=patch.kind or event.kind,
                            provenance=patch.provenance or event.provenance,
                            parser_name=patch.parser_name or event.parser_name,
                            parser_version=patch.parser_version or event.parser_version,
                            confidence=patch.confidence or event.confidence,
                            metadata={**event.metadata, **patch.metadata},
                        )
                except Exception as exc:
                    self.registry.enrichers.pop(name, None)
                    await self.report_failure(plugin, f"enricher:{name}", exc, world=event.world)
            self._queue_world_activity(
                PluginLifecycleEvent(
                    kind="world_activity",
                    world=event.world,
                    state=event.kind.value,
                    source="world",
                    metadata={
                        "connection_generation": event.connection_generation,
                        "count": 1,
                        "first_timestamp": event.timestamp.isoformat(),
                        "last_timestamp": event.timestamp.isoformat(),
                    },
                )
            )
        if event.kind is EventKind.CONNECTION:
            await self.publish_session_state(
                event.world,
                event.metadata.get("state"),
                metadata=event.metadata,
            )
        return event

    async def publish_session_state(
        self,
        world: str,
        state: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        state_text = str(state) if state is not None else None
        if state_text is not None:
            self._world_states[world] = state_text
        await self.lifecycle(
            PluginLifecycleEvent(
                kind="session_state",
                world=world,
                state=state_text,
                source="world",
                metadata=self._connection_metadata(metadata),
            )
        )
        if state_text == "connected" and world not in self._connected_worlds:
            self._connected_worlds.add(world)
            await self.lifecycle(
                PluginLifecycleEvent(
                    kind="world_connected",
                    world=world,
                    state=state_text,
                    source="world",
                )
            )
        elif (
            state_text
            in {
                "connecting",
                "disconnected",
                "reconnect_wait",
                "stopped",
            }
            and world in self._connected_worlds
        ):
            self._connected_worlds.remove(world)
            await self.lifecycle(
                PluginLifecycleEvent(
                    kind="world_disconnected",
                    world=world,
                    state=state_text,
                    source="world",
                    metadata=self._connection_metadata(metadata),
                )
            )

    @staticmethod
    def _connection_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        allowed = {"intentional", "error_type", "delay_seconds", "tls", "tls_verified"}
        return {key: metadata[key] for key in allowed if key in metadata}

    def _queue_world_activity(self, event: PluginLifecycleEvent) -> None:
        assert event.world is not None
        activation = self._boss_seed if self.boss_active else None
        key = (event.world, activation)
        existing = self._queued_world_activity.get(key)
        if existing is None:
            self._lifecycle_queue.append((None, key, None, activation))
        else:
            event = replace(
                event,
                metadata={
                    **event.metadata,
                    "count": int(existing.metadata.get("count", 1))
                    + int(event.metadata.get("count", 1)),
                    "first_timestamp": existing.metadata.get("first_timestamp"),
                },
            )
        self._queued_world_activity[key] = event
        self._ensure_lifecycle_task()

    def observe_ui_event(self, event: Event) -> None:
        if event.kind is EventKind.CONNECTION:
            state = event.metadata.get("state")
            if isinstance(state, str):
                self._world_states[event.world] = state
        if event.direction is not Direction.INBOUND:
            return
        self._world_received[event.world] = self._world_received.get(event.world, 0) + 1
        self._world_last_activity[event.world] = event.timestamp

    @property
    def boss_active(self) -> bool:
        return self._active_boss_view is not None

    @property
    def active_boss_screen(self) -> str | None:
        return self._active_boss_view

    def set_boss_state_handler(self, handler: Callable[[bool], None]) -> None:
        self._boss_state_handler = handler
        handler(self.boss_active)

    def set_notice_handler(self, handler: Callable[[str, str], None]) -> None:
        self._notice_handler = handler

    @property
    def active_boss_refresh_interval(self) -> float | None:
        if self._active_boss_view is None:
            return None
        view = self.registry.boss_views.get(self._active_boss_view)
        return view.refresh_interval_seconds if view is not None else None

    def configure_boss_selection(self, mode: str, screen: str | None = None) -> str:
        normalized_mode = mode.casefold()
        if normalized_mode not in {"cycle", "random", "locked"}:
            raise ValueError("boss mode must be cycle, random, or locked")
        selected: str | None = None
        if normalized_mode == "locked":
            requested = (screen or "").casefold()
            selected = next(
                (name for name in self.registry.boss_views if name.casefold() == requested),
                None,
            )
            if selected is None:
                raise ValueError(f"unknown boss screen: {screen or ''}")
        elif screen is not None:
            raise ValueError("boss screen can only be set in locked mode")
        self._boss_selection_mode = normalized_mode
        self._boss_selected_screen = selected
        if selected is not None:
            return f"Boss screen locked to {selected}"
        return f"Boss-screen mode is {normalized_mode}"

    def initialize_boss_selection(self, mode: str, screen: str | None = None) -> None:
        normalized_mode = mode.casefold()
        if normalized_mode not in {"cycle", "random", "locked"}:
            raise ValueError("boss mode must be cycle, random, or locked")
        if normalized_mode == "locked" and screen is None:
            raise ValueError("locked boss mode requires a screen")
        if normalized_mode != "locked" and screen is not None:
            raise ValueError("boss screen can only be set in locked mode")
        if normalized_mode == "locked":
            requested = screen.casefold() if screen is not None else ""
            if requested not in self.registry.boss_views:
                raise ValueError(f"unknown boss screen: {screen or ''}")
        self._boss_selection_mode = normalized_mode
        self._boss_selected_screen = screen.casefold() if screen is not None else None

    def boss_status(self) -> str:
        selected = (
            f"locked to {self._boss_selected_screen}"
            if self._boss_selection_mode == "locked"
            else self._boss_selection_mode
        )
        screens = ", ".join(self.registry.boss_views) or "none"
        return f"Boss-screen mode is {selected}; screens: {screens}"

    def _select_boss_view(self) -> str | None:
        available = tuple(self.registry.boss_views)
        if not available:
            return None
        if self._boss_selection_mode == "locked":
            return self._boss_selected_screen if self._boss_selected_screen in available else None
        if self._boss_selection_mode == "random":
            return secrets.choice(available)
        if self._boss_last_screen in available:
            selected = available[(available.index(self._boss_last_screen) + 1) % len(available)]
        else:
            selected = available[0]
        self._boss_last_screen = selected
        return selected

    async def activate_selected_boss(self, world: str) -> None:
        selected = self._select_boss_view()
        if selected is None:
            raise ValueError("configured boss screen is unavailable")
        view = self.registry.boss_views[selected]
        await self._activate_boss(view.plugin, selected, world)

    async def activate_boss_view(self, name: str, world: str) -> None:
        view = self.registry.boss_views.get(name.casefold())
        if view is None:
            raise ValueError(f"unknown boss view: {name}")
        await self._activate_boss(view.plugin, view.name, world)

    async def _activate_boss(self, plugin: str, name: str, world: str) -> None:
        view = self.registry.boss_views.get(name)
        if view is None or view.plugin != plugin:
            raise ValueError(f"plugin {plugin} does not own boss view {name}")
        if not world:
            raise ValueError("boss-view world cannot be empty")
        if self._active_boss_view == name and self._active_boss_world == world:
            return
        self._active_boss_view = name
        self._active_boss_world = world
        self._boss_events.clear()
        self._boss_activated_at = datetime.now(UTC)
        self._boss_activated_monotonic = time.monotonic()
        self._boss_received_baseline = dict(self._world_received)
        self._boss_seed += 1
        self._boss_event_sequence = 0
        self.emit_boss_event(
            BossViewEvent(
                kind="boss_activated",
                world=_safe_boss_system_field(world),
                source="ui",
                metadata={"screen": name},
            )
        )
        self._queue_lifecycle(PluginLifecycleEvent(kind="boss_activated", world=world, source="ui"))

    def dismiss_boss(self) -> None:
        if self._active_boss_view is None:
            return
        self._active_boss_view = None
        self._active_boss_world = None
        self._boss_events.clear()
        self._boss_activated_at = None
        if self._boss_state_handler is not None:
            self._boss_state_handler(False)

    def emit_boss_event(self, event: BossViewEvent) -> bool:
        if self._active_boss_view is None:
            return False
        event = replace(event, sequence=self._boss_event_sequence)
        self._boss_event_sequence += 1
        self._boss_events.append(event)
        if self._boss_state_handler is not None:
            self._boss_state_handler(True)
        return True

    def _emit_for_boss_view(self, plugin: str, name: str, event: BossViewEvent) -> bool:
        view = self.registry.boss_views.get(name)
        if self._active_boss_view != name or view is None or view.plugin != plugin:
            return False
        return self.emit_boss_event(replace(event, source=_boss_plugin_source(plugin)))

    def render_boss(self, *, width: int, height: int) -> StyleAndTextTuples:
        if self._active_boss_view is None or self._active_boss_world is None:
            return []
        view = self.registry.boss_views.get(self._active_boss_view)
        if view is None:
            return self._boss_failure_cover(width=width, height=height)
        now = datetime.now(UTC)
        statuses = tuple(
            BossWorldStatus(
                world=world,
                connection_state=self._world_states.get(world, "unknown"),
                received_since_activation=max(
                    0,
                    self._world_received.get(world, 0) - self._boss_received_baseline.get(world, 0),
                ),
                last_activity_at=self._world_last_activity.get(world),
                idle_seconds=(
                    max(0.0, (now - self._world_last_activity[world]).total_seconds())
                    if world in self._world_last_activity
                    else None
                ),
            )
            for world in self.worlds
        )
        context = BossViewContext(
            world=self._active_boss_world,
            width=width,
            height=height,
            events=tuple(self._boss_events),
            screen=view.name,
            activated_at=self._boss_activated_at,
            elapsed_seconds=max(0.0, time.monotonic() - self._boss_activated_monotonic),
            seed=self._boss_seed,
            gateway_connected=self._gateway_connected,
            worlds=statuses,
        )
        try:
            fragments = tuple(itertools.islice(view.renderer(context), 2_001))
            if len(fragments) > 2_000:
                raise ValueError("boss view produced too many fragments")
            total_characters = 0
            for style, text in fragments:
                if not isinstance(style, str) or not isinstance(text, str):
                    raise TypeError("boss-view fragments must contain style and text strings")
                if not _BOSS_STYLE.fullmatch(style):
                    raise ValueError("boss views may only use prompt-toolkit class styles")
                if _contains_unsafe_terminal_character(text, allow_layout=True):
                    raise ValueError("boss-view text contains terminal control characters")
                total_characters += len(style) + len(text)
                if total_characters > _MAX_BOSS_RENDER_CHARACTERS:
                    raise ValueError("boss view produced too much text")
            return list(fragments)
        except Exception as exc:
            self.registry.boss_views.pop(view.name, None)
            self._schedule_failure(view.plugin, f"boss-view:{view.name}", exc, world=context.world)
            return self._boss_failure_cover(width=width, height=height)

    @staticmethod
    def _boss_failure_cover(*, width: int, height: int) -> StyleAndTextTuples:
        lines = (
            "System Operations Dashboard",
            "",
            "Status: display subsystem recovering",
            "Press Enter to return",
        )
        text = "\n".join(line[:width] for line in lines[:height])
        return [("class:boss", text)]

    def transform_display(self, event: Event) -> str | None:
        text = event.display_text
        if text is None:
            return None
        for name, (plugin, transform) in tuple(self.registry.display_transforms.items()):
            try:
                text = transform(event, text)
            except Exception as exc:
                self.registry.display_transforms.pop(name, None)
                self._schedule_failure(plugin, f"display:{name}", exc, world=event.world)
            if text is None:
                break
        return text

    def decorate_display(self, event: Event, text: str) -> tuple[TextDecoration, ...]:
        decorations: list[TextDecoration] = []
        for name, (plugin, decorator) in tuple(self.registry.display_decorators.items()):
            try:
                additions = tuple(decorator(event, text))
                combined = tuple((*decorations, *additions))
                validate_decorations(len(terminal_plain_text(text)), combined)
                decorations.extend(additions)
            except Exception as exc:
                self.registry.display_decorators.pop(name, None)
                self._schedule_failure(plugin, f"display-decoration:{name}", exc, world=event.world)
        return tuple(decorations)

    @property
    def border_frames_per_second(self) -> float | None:
        rates = [
            effect.frames_per_second
            for effect in self.registry.border_effects.values()
            if effect.frames_per_second is not None
        ]
        return max(rates, default=None)

    @property
    def has_animated_border_effects(self) -> bool:
        return any(
            effect.frames_per_second is not None or effect.frame_delay is not None
            for effect in self.registry.border_effects.values()
        )

    def border_frame_delay(self, elapsed_seconds: float) -> float | None:
        delays: list[float] = []
        for name, effect in tuple(self.registry.border_effects.items()):
            try:
                if effect.frame_delay is not None:
                    delay = effect.frame_delay(elapsed_seconds)
                elif effect.frames_per_second is not None:
                    delay = 1 / effect.frames_per_second
                else:
                    continue
                if delay is None:
                    continue
                if (
                    not isinstance(delay, (int, float))
                    or isinstance(delay, bool)
                    or not math.isfinite(delay)
                    or delay < 0
                ):
                    raise ValueError("border frame delay must be a non-negative finite number")
                delays.append(max(1 / 30, float(delay)))
            except Exception as exc:
                self.registry.border_effects.pop(name, None)
                self._schedule_failure(
                    effect.plugin,
                    f"border-schedule:{name}",
                    exc,
                    world="tfr",
                )
        return min(delays, default=None)

    def transform_border(
        self,
        context: BorderCellContext,
        fragment: BorderFragment,
    ) -> BorderFragment:
        for name, effect in tuple(self.registry.border_effects.items()):
            try:
                transformed = effect.handler(context, fragment)
                if transformed is not None:
                    validate_border_fragment(transformed)
                    fragment = transformed
            except Exception as exc:
                self.registry.border_effects.pop(name, None)
                self._schedule_failure(
                    effect.plugin,
                    f"border:{name}",
                    exc,
                    world=context.world,
                )
        return fragment

    def render_screen_clear(
        self,
        name: str,
        context: ScreenClearContext,
    ) -> StyleAndTextTuples:
        effect = self.registry.screen_clear_effects.get(name)
        if effect is None:
            return []
        try:
            maximum = screen_clear_fragment_limit(context)
            fragments = tuple(itertools.islice(effect.handler(context), maximum + 1))
            return validate_screen_clear_frame(context, fragments)
        except Exception as exc:
            self.registry.screen_clear_effects.pop(name, None)
            self._schedule_failure(effect.plugin, f"screen-clear:{name}", exc, world=context.world)
            return []

    def screen_clear_is_complete(self, name: str, *, world: str = "") -> bool:
        effect = self.registry.screen_clear_effects.get(name)
        if effect is None or effect.is_complete is None:
            return True
        try:
            return bool(effect.is_complete())
        except Exception as exc:
            self.registry.screen_clear_effects.pop(name, None)
            self._schedule_failure(effect.plugin, f"screen-clear:{name}", exc, world=world)
            return True

    def render_status(self, world: str) -> tuple[str, ...]:
        segments: list[str] = []
        for name, (plugin, render) in tuple(self.registry.status_segments.items()):
            try:
                value = render(world)
                if value:
                    segments.append(value)
            except Exception as exc:
                self.registry.status_segments.pop(name, None)
                self._schedule_failure(plugin, f"status:{name}", exc, world=world)
        return tuple(segments)

    async def execute_command(
        self,
        name: str,
        arguments: tuple[str, ...],
        world: str,
    ) -> bool:
        registered = self.registry.commands.get(name.casefold())
        if registered is None:
            return False
        plugin, handler = registered
        try:
            result = handler(self.context(plugin, world), arguments)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            await self.report_failure(plugin, f"command:{name}", exc, world=world)
        return True

    async def invoke_key(self, name: str, world: str) -> None:
        binding, handler = self.registry.key_bindings[name]
        try:
            result = handler(self.context(binding.plugin, world))
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            await self.report_failure(binding.plugin, f"key:{name}", exc, world=world)

    async def lifecycle(self, event: PluginLifecycleEvent) -> None:
        completion = asyncio.get_running_loop().create_future()
        activation = self._boss_seed if self.boss_active else None
        self._lifecycle_queue.append((event, None, completion, activation))
        self._ensure_lifecycle_task()
        await completion

    def _queue_lifecycle(self, event: PluginLifecycleEvent) -> None:
        activation = self._boss_seed if self.boss_active else None
        self._lifecycle_queue.append((event, None, None, activation))
        self._ensure_lifecycle_task()

    def _ensure_lifecycle_task(self) -> None:
        if self._lifecycle_task is None or self._lifecycle_task.done():
            self._lifecycle_task = asyncio.create_task(
                self._dispatch_lifecycle_events(),
                name="tfr-plugin-lifecycle",
            )
            self._lifecycle_task.add_done_callback(self._lifecycle_task_done)

    async def _dispatch_lifecycle_events(self) -> None:
        completion: asyncio.Future[None] | None = None
        try:
            while self._lifecycle_queue:
                event, activity_key, completion, activation = self._lifecycle_queue.popleft()
                if activity_key is not None:
                    event = self._queued_world_activity.pop(activity_key)
                assert event is not None
                await self._deliver_lifecycle(event, activation)
                if completion is not None and not completion.done():
                    completion.set_result(None)
                completion = None
        except BaseException as exc:
            if completion is not None and not completion.done():
                completion.set_exception(exc)
            for _event, _activity_key, queued_completion, _activation in self._lifecycle_queue:
                if queued_completion is not None and not queued_completion.done():
                    queued_completion.set_exception(exc)
            self._lifecycle_queue.clear()
            self._queued_world_activity.clear()
            raise
        finally:
            self._lifecycle_task = None

    @staticmethod
    def _lifecycle_task_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _deliver_lifecycle(self, event: PluginLifecycleEvent, activation: int | None) -> None:
        if event.kind == "gateway_connected":
            self._gateway_connected = True
        elif event.kind == "gateway_disconnected":
            self._gateway_connected = False
        if self.boss_active and activation == self._boss_seed and event.kind != "boss_activated":
            self.emit_boss_event(
                BossViewEvent(
                    kind=event.kind,
                    world=_safe_boss_system_field(event.world),
                    state=_safe_boss_system_field(event.state),
                    source=_safe_boss_system_field(event.source),
                    metadata=event.metadata,
                )
            )
        for name, (plugin, handler) in tuple(self.registry.lifecycle_handlers.items()):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self.registry.lifecycle_handlers.pop(name, None)
                await self.report_failure(
                    plugin,
                    f"lifecycle:{name}",
                    RuntimeError("lifecycle handler cancelled"),
                    world=event.world or "tfr",
                )
            except Exception as exc:
                self.registry.lifecycle_handlers.pop(name, None)
                await self.report_failure(
                    plugin,
                    f"lifecycle:{name}",
                    exc,
                    world=event.world or "tfr",
                )

    def _schedule_failure(
        self,
        plugin: str,
        operation: str,
        error: Exception,
        *,
        world: str,
    ) -> None:
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(
                self.report_failure(plugin, operation, error, world=world)
            )
            self._failure_tasks.add(task)
            task.add_done_callback(self._failure_task_done)

    def _failure_task_done(self, task: asyncio.Task[Any]) -> None:
        self._failure_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def drain(self) -> None:
        if self._lifecycle_task is not None:
            await self._lifecycle_task
        while self._failure_tasks:
            tasks = tuple(self._failure_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._failure_tasks.difference_update(tasks)
