from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
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


class DiscoveredPlugin(Protocol):
    name: str

    def load(self) -> object: ...


class PluginCommandContext:
    def __init__(
        self,
        *,
        plugin: str,
        world: str,
        targets: Mapping[str, UUID],
        command_bus: CommandBus,
        worlds: Mapping[str, PluginWorldInfo] | None = None,
    ) -> None:
        self.plugin = plugin
        self.world = world
        self._targets = targets
        self._command_bus = command_bus
        self._worlds = worlds or {}

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


class PluginRegistry:
    def __init__(self) -> None:
        self.commands: dict[str, tuple[str, CommandHandler]] = {}
        self.command_help: dict[str, str] = {}
        self.enrichers: dict[str, tuple[str, EventEnricher]] = {}
        self.display_transforms: dict[str, tuple[str, DisplayTransform]] = {}
        self.display_decorators: dict[str, tuple[str, DisplayDecorator]] = {}
        self.border_effects: dict[str, PluginBorderEffect] = {}
        self.screen_clear_effects: dict[str, PluginScreenClearEffect] = {}
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
    ) -> None:
        self.plugin = plugin
        self._registry = registry
        self.scope = scope

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
    ) -> None:
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.targets = MappingProxyType(dict(targets))
        self.worlds = MappingProxyType(dict(worlds or {}))
        self.registry = PluginRegistry()
        self._session_id = uuid4()
        self._sequence = 0
        self._failure_tasks: set[asyncio.Task[Any]] = set()

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
        manager = cls(
            event_bus=event_bus,
            command_bus=command_bus,
            targets=targets,
            worlds=worlds,
        )
        points = (
            discovered
            if discovered is not None
            else tuple(entry_points().select(group=PLUGIN_ENTRY_POINT_GROUP))
        )
        points = tuple(points) + tuple(extra_discovered)
        available: dict[str, DiscoveredPlugin] = {}
        duplicates: set[str] = set()
        for point in points:
            if point.name in available:
                duplicates.add(point.name)
            else:
                available[point.name] = point
        for name in dict.fromkeys(enabled):
            if name in duplicates:
                await manager.report_failure(name, "load", DuplicatePluginRegistration())
                continue
            point = available.get(name)
            if point is None:
                await manager.report_failure(name, "load", PluginNotFound())
                continue
            registrations = (
                manager.registry.commands,
                manager.registry.command_help,
                manager.registry.enrichers,
                manager.registry.display_transforms,
                manager.registry.display_decorators,
                manager.registry.border_effects,
                manager.registry.screen_clear_effects,
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
                    PluginRegistrar(name, manager.registry, scope),
                    MappingProxyType(dict(config.get(name, {}))),
                )
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                for registration, snapshot in zip(registrations, snapshots, strict=True):
                    registration.clear()
                    registration.update(snapshot)
                await manager.report_failure(name, "load", exc)
        return manager

    def context(self, plugin: str, world: str) -> PluginCommandContext:
        return PluginCommandContext(
            plugin=plugin,
            world=world,
            targets=self.targets,
            command_bus=self.command_bus,
            worlds=self.worlds,
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
        if event.kind is EventKind.CONNECTION:
            await self.lifecycle(
                PluginLifecycleEvent(
                    kind="session_state",
                    world=event.world,
                    state=event.metadata.get("state"),
                )
            )
        return event

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
        for name, (plugin, handler) in tuple(self.registry.lifecycle_handlers.items()):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
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
        while self._failure_tasks:
            await asyncio.gather(*tuple(self._failure_tasks), return_exceptions=True)
