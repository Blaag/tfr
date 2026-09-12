from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from tfr.borders import BorderCellContext, BorderEdge, BorderFragment
from tfr.clear_effects import ScreenClearContext
from tfr.core import CommandBus, EventBus
from tfr.events import ActorType, Direction, Event, EventKind
from tfr.plugins import (
    PLUGIN_ENTRY_POINT_GROUP,
    EventPatch,
    PluginLifecycleEvent,
    PluginManager,
    PluginRegistrar,
    PluginRegistrationError,
    PluginRegistry,
    PluginWorldInfo,
)
from tfr.text_effects import TextDecoration, TextEffectKind


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        pass


@dataclass
class FakeEntryPoint:
    name: str
    plugin: object

    def load(self) -> object:
        return self.plugin


def inbound_event(session_id: Any, *, text: str = "hello") -> Event:
    return Event(
        session_id=session_id,
        world="alpha",
        connection_generation=1,
        sequence=1,
        direction=Direction.INBOUND,
        kind=EventKind.RAW_OUTPUT,
        canonical_text=text,
        plain_text=text,
        display_text=text,
    )


def test_screen_clear_context_rejects_styled_text_that_does_not_match_lines() -> None:
    with pytest.raises(ValueError, match="must match the snapshot text"):
        ScreenClearContext(
            lines=("hello",),
            width=5,
            progress=0,
            styled_lines=((("fg:#d70000", "other"),),),
        )


def test_screen_clear_registration_accepts_slow_effect_duration() -> None:
    registry = PluginRegistry()
    registrar = PluginRegistrar("fixture", registry, scope="ui")

    registrar.register_screen_clear_effect(
        lambda _context: (),
        duration_seconds=24,
        frames_per_second=24,
    )

    assert registry.screen_clear_effects["fixture"].duration_seconds == 24


def test_screen_clear_registration_rejects_duration_over_shared_limit() -> None:
    registrar = PluginRegistrar("fixture", PluginRegistry(), scope="ui")

    with pytest.raises(PluginRegistrationError, match="at most 60 seconds"):
        registrar.register_screen_clear_effect(
            lambda _context: (),
            duration_seconds=61,
            frames_per_second=24,
        )


async def test_fixture_plugin_registers_every_extension_and_submits_through_bus() -> None:
    lifecycle_events: list[PluginLifecycleEvent] = []

    class FixturePlugin:
        api_version = 1

        def register(self, registrar: Any, config: Any) -> None:
            assert config["label"] == "ready"

            async def wave(context: Any, arguments: tuple[str, ...]) -> None:
                await context.submit(f"say {' '.join(arguments)}")

            async def key(context: Any) -> None:
                await context.submit("look")

            registrar.register_command("wave", wave, help="wave at the world")
            registrar.register_enricher(
                "speech",
                lambda _event: EventPatch(
                    kind=EventKind.SAY,
                    parser_name="fixture",
                    metadata={"fixture": True},
                ),
            )
            registrar.register_display_transform("uppercase", lambda _event, text: text.upper())
            registrar.register_display_decorator(
                "highlight",
                lambda _event, _text: (
                    TextDecoration(
                        start=0,
                        end=5,
                        effect=TextEffectKind.SHIMMER,
                        base_color="#d70000",
                        accent_color="#ffffff",
                        interval_seconds=1.4,
                    ),
                ),
            )
            registrar.register_status_segment("ready", lambda world: f"plugin:{world}")
            registrar.register_screen_clear_effect(
                lambda context: (("reverse", context.lines[0]),),
                duration_seconds=1.5,
                frames_per_second=20,
            )
            registrar.register_key_binding("look", ("f7",), key)
            registrar.register_lifecycle_handler("capture", lifecycle_events.append)

    sink = MemorySink()
    event_bus = EventBus([sink])
    command_bus = CommandBus()
    session_id = uuid4()
    queue = command_bus.register(session_id)
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={"fixture": {"label": "ready"}},
        event_bus=event_bus,
        command_bus=command_bus,
        targets={"alpha": session_id},
        worlds={"alpha": PluginWorldInfo(server="tinymush", encoding="latin-1")},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
    )
    event_bus.add_processor(manager.process_event)
    subscriber = event_bus.subscribe()

    original = inbound_event(session_id)
    await event_bus.publish(original)
    enriched = subscriber.get_nowait()
    assert enriched.event_id == original.event_id
    assert enriched.canonical_text == original.canonical_text
    assert enriched.kind is EventKind.SAY
    assert enriched.metadata["fixture"] is True
    assert manager.transform_display(enriched) == "HELLO"
    assert manager.decorate_display(enriched, "HELLO")[0].end == 5
    assert manager.render_status("alpha") == ("plugin:alpha",)
    assert manager.render_screen_clear(
        "fixture",
        ScreenClearContext(lines=("hello",), width=5, progress=0.5, world="alpha"),
    ) == [("reverse", "hello")]
    assert manager.registry.command_help["wave"] == "wave at the world"

    assert await manager.execute_command("wave", ("hello", "there"), "alpha")
    command = queue.get_nowait()
    assert command.text == "say hello there"
    assert command.actor.type is ActorType.PLUGIN
    assert command.actor.id == "fixture"
    assert manager.context("fixture", "alpha").world_info == PluginWorldInfo(
        server="tinymush",
        encoding="latin-1",
    )

    await manager.invoke_key("look", "alpha")
    assert queue.get_nowait().text == "look"
    await manager.lifecycle(PluginLifecycleEvent(kind="application_start"))
    assert lifecycle_events == [PluginLifecycleEvent(kind="application_start")]


async def test_plugin_scopes_partition_gateway_and_ui_capabilities() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("wave", lambda _context, _arguments: None)
            registrar.register_enricher("speech", lambda _event: None)
            registrar.register_display_transform("display", lambda _event, text: text)
            registrar.register_display_decorator("decoration", lambda _event, _text: ())
            registrar.register_status_segment("status", lambda _world: "ready")
            registrar.register_border_effect(
                "border",
                lambda _context, fragment: fragment,
                frames_per_second=8,
            )
            registrar.register_screen_clear_effect(
                lambda context: (("", context.lines[0]),),
                duration_seconds=1,
                frames_per_second=20,
            )

    values = {
        scope: await PluginManager.load(
            enabled=("fixture",),
            config={},
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
            scope=scope,
        )
        for scope in ("gateway", "ui")
    }

    assert set(values["gateway"].registry.enrichers) == {"speech"}
    assert values["gateway"].registry.commands == {}
    assert values["gateway"].registry.display_transforms == {}
    assert set(values["ui"].registry.commands) == {"wave"}
    assert set(values["ui"].registry.display_transforms) == {"display"}
    assert set(values["ui"].registry.display_decorators) == {"decoration"}
    assert set(values["ui"].registry.status_segments) == {"status"}
    assert set(values["ui"].registry.border_effects) == {"border"}
    assert set(values["ui"].registry.screen_clear_effects) == {"fixture"}
    assert values["ui"].registry.enrichers == {}
    assert values["gateway"].registry.border_effects == {}
    assert values["gateway"].registry.screen_clear_effects == {}
    assert values["gateway"].registry.display_decorators == {}


async def test_invalid_display_decoration_is_removed_and_reported() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_display_decorator(
                "invalid",
                lambda _event, _text: (
                    TextDecoration(
                        start=0,
                        end=20,
                        effect=TextEffectKind.SHIMMER,
                        base_color="#d70000",
                        accent_color="#ffffff",
                        interval_seconds=1.4,
                    ),
                ),
            )

    sink = MemorySink()
    event = inbound_event(uuid4())
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        scope="ui",
    )

    assert manager.decorate_display(event, "hello") == ()
    await manager.drain()

    assert manager.registry.display_decorators == {}
    assert sink.events[-1].metadata["error_type"] == "ValueError"


async def test_border_effects_transform_fragments_and_report_failures() -> None:
    calls = 0

    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_border_effect(
                "working",
                lambda _context, fragment: BorderFragment(
                    fragment.character,
                    f"{fragment.style} reverse",
                ),
                frames_per_second=12,
            )

            def fail(_context: BorderCellContext, _fragment: BorderFragment) -> None:
                nonlocal calls
                calls += 1
                raise RuntimeError("secret detail")

            registrar.register_border_effect("failure", fail, frames_per_second=6)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        scope="ui",
    )
    context = BorderCellContext(
        panel="output",
        world="alpha",
        edge=BorderEdge.TOP,
        edge_index=0,
        perimeter_index=0,
        perimeter_length=20,
        width=8,
        height=4,
        focused=True,
        elapsed_seconds=1.0,
    )

    result = manager.transform_border(context, BorderFragment("─", "class:border.output"))
    manager.transform_border(context, result)
    await manager.drain()

    assert result.style == "class:border.output reverse"
    assert calls == 1
    assert manager.border_frames_per_second == 12
    assert sink.events[-1].metadata["error_type"] == "RuntimeError"
    assert "secret detail" not in (sink.events[-1].canonical_text or "")


async def test_invalid_screen_clear_frame_disables_only_that_effect() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_screen_clear_effect(
                lambda _context: (("", "too wide"),),
                duration_seconds=1,
                frames_per_second=20,
            )

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        scope="ui",
    )

    frame = manager.render_screen_clear(
        "fixture",
        ScreenClearContext(lines=("ok",), width=2, progress=0.5, world="alpha"),
    )
    await manager.drain()

    assert frame == []
    assert manager.registry.screen_clear_effects == {}
    assert sink.events[-1].metadata["error_type"] == "ValueError"


async def test_unbounded_screen_clear_frame_is_stopped_before_materialization() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            def render(_context: ScreenClearContext) -> Any:
                while True:
                    yield ("", "")

            registrar.register_screen_clear_effect(
                render,
                duration_seconds=1,
                frames_per_second=20,
            )

    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        scope="ui",
    )

    frame = manager.render_screen_clear(
        "fixture",
        ScreenClearContext(lines=("ok",), width=2, progress=0.5),
    )
    await manager.drain()

    assert frame == []
    assert manager.registry.screen_clear_effects == {}


async def test_screen_clear_frame_limits_zero_width_text() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_screen_clear_effect(
                lambda _context: (("", "\u0301" * 9),),
                duration_seconds=1,
                frames_per_second=20,
            )

    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        scope="ui",
    )

    frame = manager.render_screen_clear(
        "fixture",
        ScreenClearContext(lines=("x",), width=1, progress=0.5),
    )
    await manager.drain()

    assert frame == []
    assert manager.registry.screen_clear_effects == {}


async def test_failing_plugin_is_reported_without_blocking_events() -> None:
    class FailingPlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            def fail(_event: Event) -> EventPatch:
                raise RuntimeError("secret detail")

            registrar.register_enricher("failure", fail)

    sink = MemorySink()
    event_bus = EventBus([sink])
    manager = await PluginManager.load(
        enabled=("failing",),
        config={},
        event_bus=event_bus,
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("failing", FailingPlugin()),),
    )
    event_bus.add_processor(manager.process_event)

    source = inbound_event(uuid4())
    await event_bus.publish(source)
    await event_bus.publish(inbound_event(uuid4(), text="second"))

    failures = [event for event in sink.events if event.kind is EventKind.PLUGIN]
    assert len(failures) == 1
    assert source in sink.events
    failure = failures[0]
    assert failure.kind is EventKind.PLUGIN
    assert failure.metadata["error_type"] == "RuntimeError"
    assert "secret detail" not in (failure.canonical_text or "")


async def test_failed_registration_rolls_back_and_later_plugins_still_load() -> None:
    class DuplicatePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("same", lambda _context, _arguments: None)
            registrar.register_command("same", lambda _context, _arguments: None)

    class GoodPlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("good", lambda _context, _arguments: None)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("duplicate", "good"),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(
            FakeEntryPoint("duplicate", DuplicatePlugin()),
            FakeEntryPoint("good", GoodPlugin()),
        ),
    )

    assert "same" not in manager.registry.commands
    assert "good" in manager.registry.commands
    assert sink.events[0].metadata["error_type"] == "DuplicatePluginRegistration"


async def test_discovery_uses_versioned_entry_point_group(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: list[str] = []

    class EntryPoints:
        def select(self, *, group: str) -> tuple[FakeEntryPoint, ...]:
            selected.append(group)
            return ()

    monkeypatch.setattr("tfr.plugins.entry_points", EntryPoints)

    await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
    )

    assert selected == [PLUGIN_ENTRY_POINT_GROUP]


async def test_incompatible_plugin_api_is_rejected() -> None:
    class FuturePlugin:
        api_version = 2

        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("future", lambda _context, _arguments: None)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("future",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("future", FuturePlugin()),),
    )

    assert manager.registry.commands == {}
    assert sink.events[0].metadata["error_type"] == "IncompatiblePluginApi"


async def test_duplicate_discovered_entry_point_is_rejected() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("fixture", lambda _context, _arguments: None)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(
            FakeEntryPoint("fixture", FixturePlugin()),
            FakeEntryPoint("fixture", FixturePlugin()),
        ),
    )

    assert manager.registry.commands == {}
    assert sink.events[0].metadata["error_type"] == "DuplicatePluginRegistration"


async def test_extra_discovered_plugins_merge_with_installed_entry_points() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("wave", lambda _context, _arguments: None)

    class GitSourcedPlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("greet", lambda _context, _arguments: None)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("fixture", "greet-source"),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        extra_discovered=(FakeEntryPoint("greet-source", GitSourcedPlugin()),),
    )

    assert sink.events == []
    assert set(manager.registry.commands) == {"wave", "greet"}


async def test_extra_discovered_plugin_name_colliding_with_installed_is_rejected() -> None:
    class FixturePlugin:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_command("wave", lambda _context, _arguments: None)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("fixture",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
        extra_discovered=(FakeEntryPoint("fixture", FixturePlugin()),),
    )

    assert manager.registry.commands == {}
    assert sink.events[0].metadata["error_type"] == "DuplicatePluginRegistration"
