from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text

from tfr.borders import BorderCellContext, BorderEdge, BorderFragment
from tfr.boss import _FILENAMES
from tfr.clear_effects import ScreenClearContext
from tfr.core import CommandBus, EventBus
from tfr.events import ActorType, Direction, Event, EventKind
from tfr.plugins import (
    PLUGIN_ENTRY_POINT_GROUP,
    BossViewContext,
    BossViewEvent,
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


def connection_event(session_id: Any, sequence: int, state: str) -> Event:
    return Event(
        session_id=session_id,
        world="alpha",
        connection_generation=1,
        sequence=sequence,
        direction=Direction.INTERNAL,
        kind=EventKind.CONNECTION,
        metadata={"state": state},
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
    await manager.drain()
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
    assert [event.kind for event in lifecycle_events] == ["world_activity", "application_start"]
    assert lifecycle_events[0].world == "alpha"
    assert lifecycle_events[0].source == "world"


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
    assert set(values["ui"].registry.commands) == {"boss", "wave"}
    assert set(values["ui"].registry.display_transforms) == {"display"}
    assert set(values["ui"].registry.display_decorators) == {"decoration"}
    assert set(values["ui"].registry.status_segments) == {"status"}
    assert set(values["ui"].registry.border_effects) == {"border"}
    assert set(values["ui"].registry.screen_clear_effects) == {"fixture"}
    assert set(values["ui"].registry.boss_views) == {"build-dashboard"}
    assert values["ui"].registry.enrichers == {}
    assert values["gateway"].registry.border_effects == {}
    assert values["gateway"].registry.screen_clear_effects == {}
    assert values["gateway"].registry.boss_views == {}
    assert values["gateway"].registry.display_decorators == {}


async def test_boss_view_plugin_activates_renders_and_accepts_bounded_events() -> None:
    contexts: list[BossViewContext] = []

    class BossFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            handle = registrar.register_boss_view("operations", self.render)

            async def activate(context: Any, _arguments: tuple[str, ...]) -> None:
                await handle.activate(context.world)

            self.emit = registrar.emit_boss_event
            registrar.register_command("operations", activate)

        @staticmethod
        def render(context: BossViewContext) -> tuple[tuple[str, str], ...]:
            contexts.append(context)
            return (("", "\n".join(event.text for event in context.events)),)

    fixture = BossFixture()
    manager = await PluginManager.load(
        enabled=("operations",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("operations", fixture),),
        scope="ui",
    )
    state_changes: list[bool] = []
    manager.set_boss_state_handler(state_changes.append)

    assert fixture.emit(BossViewEvent(kind="ignored", text="not active")) is False
    assert await manager.execute_command("operations", (), "alpha") is True
    for index in range(205):
        assert fixture.emit(BossViewEvent(kind="fixture", text=f"event {index}")) is True

    assert manager.boss_active is True
    assert manager.render_boss(width=80, height=24)[0][1].endswith("event 204")
    assert len(contexts[-1].events) == 200
    assert contexts[-1].events[0].text == "event 5"
    assert contexts[-1].events[-1].source == "plugin:operations"
    assert state_changes[:2] == [False, True]

    manager.dismiss_boss()
    assert manager.boss_active is False
    assert state_changes[-1] is False


async def test_builtin_boss_dashboard_tracks_activity_and_alternates_views() -> None:
    session_id = uuid4()
    manager = await PluginManager.load(
        enabled=(),
        config={
            "tfr.boss": {
                "histogram": {
                    "seconds": 20,
                    "include_defaults": False,
                    "labels": ["Custom vertical", "Custom horizontal"],
                },
                "flow": {
                    "seconds": 15,
                    "include_defaults": False,
                    "labels": ["Custom stage A", "Custom stage B", "Custom stage C"],
                },
                "files": {
                    "include_defaults": False,
                    "names": ["custom-artifact.dat"],
                },
            }
        },
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={"alpha one": session_id},
        worlds={"alpha one": PluginWorldInfo(server="bare", encoding="utf-8")},
        discovered=(),
        scope="ui",
    )
    before = inbound_event(session_id, text="before activation")
    before = replace(before, world="alpha one")
    manager.observe_ui_event(before)
    await manager.activate_selected_boss("alpha one")
    for text in ("first", "second"):
        event = replace(inbound_event(session_id, text=text), world="alpha one")
        manager.observe_ui_event(event)

    histogram_fragments = manager.render_boss(width=100, height=30)
    histogram = fragment_list_to_text(histogram_fragments)

    assert "New events on /var/log/alpha_one: 2 lines" in histogram
    assert "Custom vertical" in histogram
    assert "Custom horizontal" in histogram
    assert "Load-bearing code" not in histogram
    assert "█" in histogram
    assert "#" not in histogram
    assert "\x1b" not in histogram
    assert any(legend in histogram for legend in ("500", "1000", "2000", "5000"))
    assert any(style == "class:boss.chart" and "█" in text for style, text in histogram_fragments)
    chart_lines = [
        line
        for style, text in histogram_fragments
        if style == "class:boss.chart"
        for line in text.splitlines()
    ]
    assert max(map(len, chart_lines)) <= 80
    assert any("████" in line for line in chart_lines)
    assert "custom-artifact.dat" in histogram
    assert "phrasing.xls" not in histogram
    assert len(histogram.split("$ ls -la /opt/build/artifacts", 1)[1].splitlines()) <= 5

    manager._boss_activated_monotonic -= 21
    flow = fragment_list_to_text(manager.render_boss(width=100, height=30))
    assert "Generated build sequence" in flow
    for label in ("Custom stage A", "Custom stage B", "Custom stage C", "Profit"):
        assert f"│ {label}" in flow
    assert "──►" in flow
    assert "╭" in flow
    assert "░" in flow
    assert "Profit │─┘" in flow


async def test_builtin_boss_defaults_include_only_approved_additional_filenames() -> None:
    approved = {
        "danger-zone.ini",
        "slightly-darker-black.css",
        "tactleneck.conf",
        "idiots-doing-idiot-things.yml",
        "vodka-gummy-bears.dat",
        "peppermint-patties.cache",
        "double-deuce.mov",
        "pampage.tmp",
    }
    rejected = {
        "crocodile-tears.pem",
        "lacrosse-camp.db",
        "fort-kickass.plan",
        "terms-of-enrampagement.log",
    }

    assert approved <= set(_FILENAMES)
    assert rejected.isdisjoint(_FILENAMES)


async def test_boss_command_cycles_and_locks_registered_screens() -> None:
    class ExtraBossFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_boss_view("extra-screen", lambda _context: (("", "extra"),))

    notices: list[tuple[str, str]] = []
    manager = await PluginManager.load(
        enabled=("extra",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("extra", ExtraBossFixture()),),
        scope="ui",
    )
    manager.set_notice_handler(lambda world, text: notices.append((world, text)))

    await manager.execute_command("boss", (), "alpha")
    assert manager.active_boss_screen == "build-dashboard"
    manager.dismiss_boss()
    await manager.execute_command("boss", (), "alpha")
    assert manager.active_boss_screen == "extra-screen"
    manager.dismiss_boss()

    await manager.execute_command("boss", ("lock", "build-dashboard"), "alpha")
    assert notices[-1][1] == "Boss screen locked to build-dashboard"
    await manager.execute_command("boss", (), "alpha")
    assert manager.active_boss_screen == "build-dashboard"


async def test_operational_events_are_mirrored_into_active_boss_feed() -> None:
    manager = await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        worlds={"alpha": PluginWorldInfo(server="bare", encoding="utf-8")},
        discovered=(),
        scope="ui",
    )
    await manager.activate_selected_boss("alpha")
    await manager.process_event(inbound_event(uuid4(), text="first"))
    await manager.process_event(inbound_event(uuid4(), text="second"))
    await manager.drain()

    activity = next(event for event in manager._boss_events if event.kind == "world_activity")
    assert activity.text is None
    assert activity.world == "alpha"
    assert activity.metadata["count"] == 2
    assert activity.sequence >= 0


async def test_boss_activation_event_is_immediate_and_cannot_cross_activations() -> None:
    class ExtraBossFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_boss_view("extra-screen", lambda _context: (("", "extra"),))

    manager = await PluginManager.load(
        enabled=("extra",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("extra", ExtraBossFixture()),),
        scope="ui",
    )

    await manager.activate_boss_view("build-dashboard", "alpha")
    assert [(event.kind, event.sequence) for event in manager._boss_events] == [
        ("boss_activated", 0)
    ]
    manager.emit_boss_event(BossViewEvent(kind="custom"))
    await manager.activate_boss_view("extra-screen", "beta")
    await manager.drain()

    assert [(event.kind, event.world, event.sequence) for event in manager._boss_events] == [
        ("boss_activated", "beta", 0)
    ]


async def test_coalesced_activity_is_split_across_boss_activations() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            async def block(event: PluginLifecycleEvent) -> None:
                if event.kind == "application_start":
                    started.set()
                    await release.wait()

            registrar.register_lifecycle_handler("block", block)

    manager = await PluginManager.load(
        enabled=("slow",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("slow", SlowFixture()),),
        scope="ui",
    )
    lifecycle = asyncio.create_task(
        manager.lifecycle(PluginLifecycleEvent(kind="application_start"))
    )
    await started.wait()
    await manager.process_event(inbound_event(uuid4(), text="before"))
    await manager.activate_selected_boss("alpha")
    await manager.process_event(inbound_event(uuid4(), text="after"))
    release.set()
    await lifecycle
    await manager.drain()

    activity = [event for event in manager._boss_events if event.kind == "world_activity"]
    assert len(activity) == 1
    assert activity[0].metadata["count"] == 1


async def test_long_world_alias_cannot_leave_boss_mode_partially_activated() -> None:
    world = "a" * 201
    manager = await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        worlds={world: PluginWorldInfo(server="bare", encoding="utf-8")},
        discovered=(),
        scope="ui",
    )
    states: list[bool] = []
    manager.set_boss_state_handler(states.append)

    await manager.activate_selected_boss(world)

    assert manager.boss_active is True
    assert states[-1] is True
    assert manager._boss_events[0].world == "a" * 200


async def test_locked_boss_selection_requires_a_registered_screen() -> None:
    manager = await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(),
        scope="ui",
    )

    with pytest.raises(ValueError, match="unknown boss screen: missing"):
        manager.initialize_boss_selection("locked", "missing")


async def test_builtin_boss_configuration_rejects_empty_replacement_catalog() -> None:
    with pytest.raises(ValueError, match="histogram.labels cannot be empty"):
        await PluginManager.load(
            enabled=(),
            config={
                "tfr.boss": {
                    "histogram": {"include_defaults": False, "labels": []},
                }
            },
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(),
            scope="ui",
        )


async def test_builtin_boss_configuration_rejects_non_object_values() -> None:
    with pytest.raises(ValueError, match="plugins.config.tfr.boss must be an object"):
        await PluginManager.load(
            enabled=(),
            config={"tfr.boss": []},
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(),
            scope="ui",
        )


@pytest.mark.parametrize(
    ("section", "labels", "message"),
    [
        ("histogram", ["Only one"], "requires at least two axis labels"),
        ("flow", ["One", "Two"], "requires at least three component names"),
    ],
)
async def test_builtin_boss_configuration_requires_enough_visualization_labels(
    section: str,
    labels: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await PluginManager.load(
            enabled=(),
            config={
                "tfr.boss": {
                    section: {"include_defaults": False, "labels": labels},
                }
            },
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(),
            scope="ui",
        )


@pytest.mark.parametrize("label", ["Broken -> stage", "#comment"])
async def test_builtin_boss_configuration_rejects_retroflow_syntax_in_labels(
    label: str,
) -> None:
    with pytest.raises(ValueError, match="cannot contain '->' or start with '#'"):
        await PluginManager.load(
            enabled=(),
            config={
                "tfr.boss": {
                    "flow": {
                        "include_defaults": False,
                        "labels": [label, "Second", "Third"],
                    },
                }
            },
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(),
            scope="ui",
        )


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ("   ", "entries must be safe text"),
        ("Profit", "reserved Profit node"),
        ("Données", "single-column ASCII"),
    ],
)
async def test_builtin_boss_configuration_rejects_ambiguous_retroflow_labels(
    label: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await PluginManager.load(
            enabled=(),
            config={
                "tfr.boss": {
                    "flow": {
                        "include_defaults": False,
                        "labels": [label, "Second", "Third"],
                    },
                }
            },
            event_bus=EventBus(),
            command_bus=CommandBus(),
            targets={},
            discovered=(),
            scope="ui",
        )


async def test_boss_event_metadata_is_deeply_immutable_and_bounded() -> None:
    event = BossViewEvent(kind="fixture", metadata={"nested": {"items": [1, 2]}})

    assert event.metadata["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        event.metadata["nested"]["items"] = ()
    with pytest.raises(ValueError, match="metadata is too large"):
        BossViewEvent(kind="fixture", metadata={str(index): index for index in range(201)})


async def test_builtin_flow_keeps_profit_visible_in_a_tiny_terminal() -> None:
    manager = await PluginManager.load(
        enabled=(),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        worlds={
            "alpha": PluginWorldInfo(server="bare", encoding="utf-8"),
            "beta": PluginWorldInfo(server="bare", encoding="utf-8"),
        },
        discovered=(),
        scope="ui",
    )
    await manager.activate_selected_boss("alpha")
    manager._boss_activated_monotonic -= 21

    text = fragment_list_to_text(manager.render_boss(width=40, height=3))
    assert "Profit" in text
    assert "(cycle)" in text


async def test_operational_lifecycle_events_publish_activity_and_connection_transitions() -> None:
    captured: list[PluginLifecycleEvent] = []

    class LifecycleFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            registrar.register_lifecycle_handler("capture-operational", captured.append)

    session_id = uuid4()
    manager = await PluginManager.load(
        enabled=("lifecycle",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={"alpha": session_id},
        discovered=(FakeEntryPoint("lifecycle", LifecycleFixture()),),
    )

    await manager.process_event(inbound_event(session_id))
    await manager.process_event(connection_event(session_id, 2, "connected"))
    await manager.process_event(connection_event(session_id, 3, "reconnect_wait"))
    await manager.process_event(connection_event(session_id, 4, "disconnected"))
    await manager.process_event(connection_event(session_id, 5, "stopped"))
    await manager.drain()

    assert [event.kind for event in captured].count("world_activity") == 1
    assert [event.kind for event in captured].count("world_connected") == 1
    assert [event.kind for event in captured].count("world_disconnected") == 1
    activity = next(event for event in captured if event.kind == "world_activity")
    assert activity.world == "alpha"
    assert activity.state == "raw_output"
    assert activity.source == "world"
    assert "hello" not in repr(activity.metadata)


async def test_world_activity_is_coalesced_without_blocking_event_processing() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    captured: list[PluginLifecycleEvent] = []

    class SlowLifecycleFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            async def capture(event: PluginLifecycleEvent) -> None:
                if event.kind == "world_activity":
                    captured.append(event)
                    started.set()
                    await release.wait()

            registrar.register_lifecycle_handler("slow-activity", capture)

    session_id = uuid4()
    manager = await PluginManager.load(
        enabled=("slow",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={"alpha": session_id},
        discovered=(FakeEntryPoint("slow", SlowLifecycleFixture()),),
    )

    await manager.process_event(inbound_event(session_id, text="first"))
    await asyncio.wait_for(started.wait(), timeout=1)
    for index in range(100):
        await manager.process_event(inbound_event(session_id, text=f"line {index}"))

    assert len(manager._queued_world_activity) == 1
    release.set()
    await manager.drain()
    assert len(captured) == 2


async def test_activity_lifecycle_finishes_before_world_disconnect() -> None:
    activity_started = asyncio.Event()
    release_activity = asyncio.Event()
    order: list[str] = []

    class OrderedLifecycleFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            async def capture(event: PluginLifecycleEvent) -> None:
                order.append(event.kind)
                if event.kind == "world_activity":
                    activity_started.set()
                    await release_activity.wait()

            registrar.register_lifecycle_handler("ordered-activity", capture)

    session_id = uuid4()
    manager = await PluginManager.load(
        enabled=("ordered",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={"alpha": session_id},
        discovered=(FakeEntryPoint("ordered", OrderedLifecycleFixture()),),
    )
    await manager.publish_session_state("alpha", "connected")
    order.clear()
    await manager.process_event(inbound_event(session_id))
    disconnect = asyncio.create_task(manager.publish_session_state("alpha", "disconnected"))
    await asyncio.wait_for(activity_started.wait(), timeout=1)
    assert disconnect.done() is False
    release_activity.set()
    await disconnect

    assert order == ["world_activity", "session_state", "world_disconnected"]


async def test_lifecycle_handler_can_activate_a_boss_view_without_deadlocking() -> None:
    class LifecycleBossFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            handle = registrar.register_boss_view("lifecycle-boss", lambda _context: ())

            async def activate(event: PluginLifecycleEvent) -> None:
                if event.kind == "application_start":
                    await asyncio.gather(handle.activate("alpha"))

            registrar.register_lifecycle_handler("activate-boss", activate)

    manager = await PluginManager.load(
        enabled=("lifecycle-boss",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("lifecycle-boss", LifecycleBossFixture()),),
        scope="ui",
    )

    await asyncio.wait_for(
        manager.lifecycle(PluginLifecycleEvent(kind="application_start")),
        timeout=1,
    )
    await manager.drain()
    assert manager.boss_active is True


async def test_cancelled_lifecycle_handler_does_not_strand_dispatcher_waiters() -> None:
    class CancelledLifecycleFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            async def cancel(_event: PluginLifecycleEvent) -> None:
                raise asyncio.CancelledError

            registrar.register_lifecycle_handler("cancel", cancel)

    sink = MemorySink()
    manager = await PluginManager.load(
        enabled=("cancelled",),
        config={},
        event_bus=EventBus([sink]),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("cancelled", CancelledLifecycleFixture()),),
    )

    await asyncio.wait_for(
        manager.lifecycle(PluginLifecycleEvent(kind="application_start")),
        timeout=1,
    )
    await asyncio.wait_for(
        manager.lifecycle(PluginLifecycleEvent(kind="application_stop")),
        timeout=1,
    )
    assert any(event.metadata.get("plugin") == "cancelled" for event in sink.events)


async def test_boss_view_rejects_terminal_control_fragments() -> None:
    class UnsafeBossFixture:
        def register(self, registrar: Any, _config: Any) -> None:
            handle = registrar.register_boss_view(
                "unsafe",
                lambda _context: (("class:boss [ZeroWidthEscape]", "payload"),),
            )

            async def activate(context: Any, _arguments: tuple[str, ...]) -> None:
                await handle.activate(context.world)

            registrar.register_command("unsafe", activate)

    manager = await PluginManager.load(
        enabled=("unsafe",),
        config={},
        event_bus=EventBus(),
        command_bus=CommandBus(),
        targets={},
        discovered=(FakeEntryPoint("unsafe", UnsafeBossFixture()),),
        scope="ui",
    )

    assert await manager.execute_command("unsafe", (), "alpha") is True
    text = fragment_list_to_text(manager.render_boss(width=80, height=24))
    assert "display subsystem recovering" in text
    assert manager.boss_active is True
    assert "unsafe" not in manager.registry.boss_views
    await manager.drain()


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

    assert set(manager.registry.commands) == {"boss"}
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

    assert set(manager.registry.commands) == {"boss"}
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
    assert set(manager.registry.commands) == {"boss", "wave", "greet"}


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

    assert set(manager.registry.commands) == {"boss"}
    assert sink.events[0].metadata["error_type"] == "DuplicatePluginRegistration"
