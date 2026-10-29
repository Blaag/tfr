from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import heapq
import hmac
import inspect
import json
import os
import signal
import socket
import ssl
import stat
import sys
import tempfile
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from tfr.agents import AgentRuntime
from tfr.ansi import terminal_plain_text
from tfr.config import (
    ConfigurationBundle,
    ConfigurationError,
    GatewayBootstrapConfiguration,
    WorldsConfig,
    gateway_bootstrap_bundle,
    load_configuration,
)
from tfr.configuration_forms import with_builtin_form_metadata
from tfr.configuration_sync import (
    CONFIGURATION_SYNC_CAPABILITY,
    effective_client_configuration,
    validate_configuration_ack,
)
from tfr.configuration_values import (
    ConfigurationValues,
    SettingProvenance,
    configuration_values_from_bundle,
)
from tfr.core import CommandBus, EventBus, UnknownSessionError
from tfr.core_settings import core_configuration_registry
from tfr.eventlog import EventSink, JsonlEventSink
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
from tfr.gag import GagPolicy
from tfr.gateway_configuration import (
    ConfigurationMutation,
    GatewayConfigurationStore,
    apply_gateway_configuration,
)
from tfr.gateway_migration import (
    GatewayMigrationStore,
    collect_migration_settings,
    collect_omitted_migration_keys,
    new_proposal,
)
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SNAPSHOT_EVENTS,
    PROTOCOL_VERSION,
    GatewayProtocolError,
    encode_message,
    event_message,
    read_message,
    write_message,
)
from tfr.gateway_transport import (
    DEFAULT_GATEWAY_PORT,
    create_gateway_server_tls_context,
    load_gateway_token,
    validate_gateway_token,
    validate_tcp_endpoint,
)
from tfr.gateway_uis import UiRecord, UiStore
from tfr.installations import InstallationError, managed_restart_command
from tfr.managed_settings import ManagedSettingsStore, apply_managed_settings
from tfr.managed_updates import (
    StagedManagedUpdate,
    activate_managed_update,
    stage_managed_update,
)
from tfr.plugin_sources import PluginSourceNotice, PluginUpdateChecker, load_plugin_sources
from tfr.plugins import PluginLifecycleEvent, PluginManager, PluginWorldInfo
from tfr.presentation import effect_programs_as_dict
from tfr.sessions import (
    ConnectorSessionBridge,
    ConnectorWorldSession,
    SessionManager,
    SessionState,
    WorldSession,
)
from tfr.updates import (
    BuildIdentity,
    UpdateChecker,
    UpdateError,
    UpdateResult,
    current_build,
    format_update_status,
)
from tfr.world_connector import (
    GATEWAY_APPLICATION_RESTART_EXIT,
    connect_or_spawn_connector,
    connector_worlds,
)

MAX_COMMAND_CHARACTERS = 65_536
MAX_ACTOR_ID_CHARACTERS = 256
MAX_NATIVE_EFFECT_DEMOS_BYTES = 131_072
MAX_HELLO_BYTES = 16_384
HELLO_TIMEOUT_SECONDS = 5
UPDATE_PREPARE_TIMEOUT_SECONDS = 900
UPDATE_COMMIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    cursor: int
    event: Event


@dataclass(slots=True)
class _GatewayUpdateParticipant:
    connection_id: str
    ui_id: UUID | None
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock
    build: BuildIdentity | None
    managed_updates: bool
    update_checks: bool
    pending: dict[UUID, asyncio.Future[dict[str, Any] | None]]
    configuration_sync: bool = False
    configuration_status: str | None = None
    configuration_issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    events: tuple[SequencedEvent, ...]
    cursor: int
    oldest_cursor: int
    truncated: bool
    available_counts: Mapping[tuple[str, int], int]
    after_cursor_gaps: frozenset[tuple[str, int]]
    resumed: bool


@dataclass(eq=False, slots=True)
class HistorySubscription:
    snapshot: HistorySnapshot
    queue: asyncio.Queue[SequencedEvent | None]
    worlds: frozenset[str] | None = None
    event_filter: Callable[[Event], bool] | None = None


class EventHistory:
    def __init__(
        self,
        event_bus: EventBus,
        limits: Mapping[str, int],
        *,
        default_limit: int = 1_000,
        subscriber_queue_size: int = 1_000,
        ingress_queue_size: int = 1_000,
        projection_filter: Callable[[Event], bool] | None = None,
    ) -> None:
        if default_limit < 1 or subscriber_queue_size < 1 or ingress_queue_size < 1:
            raise ValueError("history and subscriber limits must be positive")
        if any(limit < 1 for limit in limits.values()):
            raise ValueError("world history limits must be positive")
        self.event_bus = event_bus
        self.limits = dict(limits)
        self.default_limit = default_limit
        self.subscriber_queue_size = subscriber_queue_size
        self.ingress_queue_size = ingress_queue_size
        self.projection_filter = projection_filter
        self._history: dict[str, deque[SequencedEvent]] = {}
        self._generation_counts: dict[
            str, tuple[int, Counter[tuple[EventKind, ActorType | None]]]
        ] = {}
        self._generation_dropped_through: dict[
            str, tuple[int, dict[tuple[EventKind, ActorType | None], int]]
        ] = {}
        self._subscribers: set[HistorySubscription] = set()
        self._queue: asyncio.Queue[Event] | None = None
        self._pump: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._cursor = 0
        self._dropped_through = 0

    def start(self) -> None:
        if self._pump is not None and not self._pump.done():
            return
        self._queue = self.event_bus.subscribe(
            maxsize=self.ingress_queue_size,
            backpressure=True,
        )
        self._pump = asyncio.create_task(self._run(), name="tfr-gateway-history")

    async def stop(self) -> None:
        if self._queue is not None:
            await self._queue.join()
            self.event_bus.unsubscribe(self._queue)
            self._queue = None
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        async with self._lock:
            for subscription in self._subscribers:
                self._close_subscription(subscription)
            self._subscribers.clear()

    async def flush(self) -> None:
        if self._queue is not None:
            await self._queue.join()

    async def subscribe(
        self,
        after_cursor: int | None,
        *,
        include_history: bool = True,
        worlds: frozenset[str] | None = None,
        maximum_events: int | None = None,
        maximum_events_per_world: int | None = None,
        event_filter: Callable[[Event], bool] | None = None,
        event_class_filter: Callable[[EventKind, ActorType | None], bool] | None = None,
        maximum_source_bytes: int | None = None,
        event_source_bytes: Callable[[Event], int] | None = None,
    ) -> HistorySubscription:
        if maximum_events is not None and maximum_events < 1:
            raise ValueError("history snapshot limit must be positive")
        if maximum_events_per_world is not None and maximum_events_per_world < 1:
            raise ValueError("per-world history snapshot limit must be positive")
        if maximum_source_bytes is not None and maximum_source_bytes < 1:
            raise ValueError("history snapshot byte limit must be positive")
        if (maximum_source_bytes is None) != (event_source_bytes is None):
            raise ValueError("history snapshot byte limit and estimator must be used together")
        async with self._lock:
            if after_cursor is not None and (after_cursor < 0 or after_cursor > self._cursor):
                raise ValueError("history cursor is outside the available range")
            candidate_count = 0
            candidate_counts: Counter[tuple[str, int]] = Counter()
            for world, history in self._history.items():
                if worlds is not None and world not in worlds:
                    continue
                for item in history:
                    if not self._event_allowed(item.event, event_filter):
                        continue
                    if after_cursor is None or item.cursor > after_cursor:
                        candidate_count += 1
                        candidate_counts[(world, item.event.connection_generation)] += 1
            available_counts = {
                (world, generation): sum(
                    count
                    for (kind, actor_type), count in counts.items()
                    if event_class_filter is None or event_class_filter(kind, actor_type)
                )
                for world, (generation, counts) in self._generation_counts.items()
                if (worlds is None or world in worlds)
                and any(
                    count
                    for (kind, actor_type), count in counts.items()
                    if event_class_filter is None or event_class_filter(kind, actor_type)
                )
            }
            candidates = (
                item
                for world, history in self._history.items()
                if worlds is None or world in worlds
                for item in history
                if self._event_allowed(item.event, event_filter)
                and (after_cursor is None or item.cursor > after_cursor)
            )
            if not include_history:
                retained: list[SequencedEvent] = []
                available_count = 0
            elif maximum_events_per_world is not None:
                iterators: dict[str, Iterator[SequencedEvent]] = {}
                newest: list[tuple[int, str, SequencedEvent]] = []
                for world, history in self._history.items():
                    if worlds is not None and world not in worlds:
                        continue
                    iterator = (
                        item
                        for item in reversed(history)
                        if self._event_allowed(item.event, event_filter)
                        and (after_cursor is None or item.cursor > after_cursor)
                    )
                    first = next(iterator, None)
                    if first is not None:
                        iterators[world] = iterator
                        heapq.heappush(newest, (-first.cursor, world, first))
                retained = []
                retained_by_world: Counter[str] = Counter()
                retained_source_bytes = 0
                while newest:
                    if maximum_events is not None and len(retained) >= maximum_events:
                        break
                    _cursor, world, item = heapq.heappop(newest)
                    source_bytes = event_source_bytes(item.event) if event_source_bytes else 0
                    if (
                        maximum_source_bytes is not None
                        and retained_source_bytes + source_bytes > maximum_source_bytes
                    ):
                        break
                    retained.append(item)
                    retained_source_bytes += source_bytes
                    retained_by_world[world] += 1
                    if retained_by_world[world] < maximum_events_per_world:
                        following = next(iterators[world], None)
                        if following is not None:
                            heapq.heappush(newest, (-following.cursor, world, following))
                retained.reverse()
                available_count = candidate_count
            elif maximum_events is None:
                retained = sorted(candidates, key=lambda item: item.cursor)
                available_count = candidate_count
            else:
                retained = sorted(
                    heapq.nlargest(maximum_events, candidates, key=lambda item: item.cursor),
                    key=lambda item: item.cursor,
                )
                available_count = candidate_count
            oldest = min(
                (
                    item.cursor
                    for world, history in self._history.items()
                    if worlds is None or world in worlds
                    for item in history
                    if self._event_allowed(item.event, None)
                ),
                default=self._cursor + 1,
            )
            retained_counts = Counter(
                (item.event.world, item.event.connection_generation) for item in retained
            )
            after_cursor_gaps = {
                key for key, count in candidate_counts.items() if count > retained_counts[key]
            }
            if after_cursor is not None:
                for world, (generation, dropped) in self._generation_dropped_through.items():
                    if worlds is not None and world not in worlds:
                        continue
                    if any(
                        cursor > after_cursor
                        and (event_class_filter is None or event_class_filter(kind, actor_type))
                        for (kind, actor_type), cursor in dropped.items()
                    ):
                        after_cursor_gaps.add((world, generation))
            else:
                after_cursor_gaps.clear()
            snapshot = HistorySnapshot(
                events=tuple(retained),
                cursor=self._cursor,
                oldest_cursor=oldest,
                truncated=(
                    include_history
                    and (
                        available_count > len(retained)
                        or (after_cursor is not None and after_cursor < self._dropped_through)
                    )
                ),
                available_counts=MappingProxyType(available_counts),
                after_cursor_gaps=frozenset(after_cursor_gaps),
                resumed=after_cursor is not None,
            )
            subscription = HistorySubscription(
                snapshot=snapshot,
                queue=asyncio.Queue(maxsize=self.subscriber_queue_size),
                worlds=worlds,
                event_filter=event_filter,
            )
            self._subscribers.add(subscription)
            return subscription

    async def unsubscribe(self, subscription: HistorySubscription) -> None:
        async with self._lock:
            self._subscribers.discard(subscription)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                async with self._lock:
                    self._cursor += 1
                    item = SequencedEvent(self._cursor, self._bounded_event(self._cursor, event))
                    bounded_event = item.event
                    projected = self._event_allowed(bounded_event, None)
                    generation_counts = self._generation_counts.get(bounded_event.world)
                    if (
                        generation_counts is None
                        or bounded_event.connection_generation > generation_counts[0]
                    ):
                        generation_counts = (bounded_event.connection_generation, Counter())
                        self._generation_counts[bounded_event.world] = generation_counts
                    if projected and bounded_event.connection_generation == generation_counts[0]:
                        actor_type = (
                            bounded_event.actor.type if bounded_event.actor is not None else None
                        )
                        generation_counts[1][(bounded_event.kind, actor_type)] += 1
                    history = self._history.get(event.world)
                    if history is None:
                        history = deque(maxlen=self.limits.get(event.world, self.default_limit))
                        self._history[event.world] = history
                    if len(history) == history.maxlen:
                        dropped_item = history[0]
                        dropped_event = dropped_item.event
                        if self._event_allowed(dropped_event, None):
                            self._dropped_through = max(self._dropped_through, dropped_item.cursor)
                            dropped = self._generation_dropped_through.get(dropped_event.world)
                            if dropped is None or dropped_event.connection_generation > dropped[0]:
                                dropped = (dropped_event.connection_generation, {})
                                self._generation_dropped_through[dropped_event.world] = dropped
                            if dropped_event.connection_generation == dropped[0]:
                                dropped_actor_type = (
                                    dropped_event.actor.type
                                    if dropped_event.actor is not None
                                    else None
                                )
                                event_class = (dropped_event.kind, dropped_actor_type)
                                dropped[1][event_class] = dropped_item.cursor
                    history.append(item)
                    if not projected:
                        continue
                    for subscription in tuple(self._subscribers):
                        if (
                            subscription.worlds is not None
                            and event.world not in subscription.worlds
                        ):
                            continue
                        if subscription.event_filter is not None and not subscription.event_filter(
                            bounded_event
                        ):
                            continue
                        if subscription.queue.full():
                            self._subscribers.discard(subscription)
                            self._close_subscription(subscription)
                        else:
                            subscription.queue.put_nowait(item)
            finally:
                self._queue.task_done()
            # Let active subscribers drain bursts before applying the slow-client bound.
            await asyncio.sleep(0)

    def _event_allowed(
        self,
        event: Event,
        event_filter: Callable[[Event], bool] | None,
    ) -> bool:
        return (self.projection_filter is None or self.projection_filter(event)) and (
            event_filter is None or event_filter(event)
        )

    @staticmethod
    def _close_subscription(subscription: HistorySubscription) -> None:
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        subscription.queue.put_nowait(None)

    @staticmethod
    def _bounded_event(cursor: int, event: Event) -> Event:
        try:
            encode_message(event_message(cursor, event))
            return event
        except GatewayProtocolError:
            notice = "[gateway omitted oversized event content]"
            replacement = replace(
                event,
                canonical_text=notice,
                plain_text=notice,
                display_text=notice,
                spoof=None,
                metadata={"gateway_omitted": "oversized_event"},
            )
            try:
                encode_message(event_message(cursor, replacement))
                return replacement
            except GatewayProtocolError:
                return Event(
                    session_id=event.session_id,
                    world=event.world[:256],
                    connection_generation=event.connection_generation,
                    sequence=event.sequence,
                    direction=event.direction,
                    kind=event.kind,
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                    monotonic_ns=event.monotonic_ns,
                    canonical_text=notice,
                    plain_text=notice,
                    display_text=notice,
                    redacted=event.redacted,
                    metadata={"gateway_omitted": "oversized_event"},
                )


def default_gateway_socket() -> Path:
    runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_directory:
        return Path(runtime_directory).expanduser() / "tfr" / "gateway.sock"
    return Path.home() / ".local" / "state" / "tfr" / "run" / "gateway.sock"


def remove_stale_gateway_socket(path: Path) -> bool:
    """Remove an owner-controlled dead Unix socket, refusing active or unsafe paths."""
    if not os.path.lexists(path):
        return False
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISSOCK(current.st_mode):
        raise RuntimeError(f"gateway socket path is not a socket: {path}")
    if os.name == "posix" and current.st_uid != os.geteuid():
        raise RuntimeError(f"gateway socket is not owned by this user: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise RuntimeError(f"cannot validate existing gateway socket: {path}: {exc}") from exc
    else:
        raise RuntimeError(f"an active Gateway already owns socket: {path}")
    finally:
        probe.close()
    if not os.path.lexists(path):
        return False
    confirmed = path.stat(follow_symlinks=False)
    if (confirmed.st_dev, confirmed.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeError(f"gateway socket changed during stale validation: {path}")
    path.unlink()
    print(f"tfr: removed stale Gateway socket: {path}", file=sys.stderr, flush=True)
    return True


def _event_log_path(directory: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory.expanduser() / f"tfr-{timestamp}-{os.getpid()}.jsonl"


def scrollback_for(bundle: ConfigurationBundle, alias: str) -> int:
    world = bundle.worlds.worlds[alias]
    if world.scrollback_lines is not None:
        return world.scrollback_lines
    if "scrollback_lines" in bundle.worlds.defaults.model_fields_set:
        return bundle.worlds.defaults.scrollback_lines
    return bundle.main.ui.scrollback_lines


class GatewayRuntime:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        command_bus: CommandBus,
        sessions: list[WorldSession],
        manager: SessionManager,
        plugins: PluginManager,
        agents: AgentRuntime,
        history: EventHistory,
        build: BuildIdentity | None = None,
        update_checker: UpdateChecker | None = None,
        plugin_update_checker: PluginUpdateChecker | None = None,
        plugin_source_messages: tuple[str, ...] = (),
        plugin_source_notices: tuple[PluginSourceNotice, ...] = (),
        connector_bridge: ConnectorSessionBridge | None = None,
        managed_settings: ManagedSettingsStore | None = None,
        gag_policy: GagPolicy | None = None,
        screen_clear_mode: str = "cycle",
        screen_clear_effect: str | None = None,
        configuration_values: ConfigurationValues | None = None,
        gateway_configuration: GatewayConfigurationStore | None = None,
        configuration_bundle: ConfigurationBundle | None = None,
        gateway_migration: GatewayMigrationStore | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.command_bus = command_bus
        self.sessions = sessions
        self.manager = manager
        self.plugins = plugins
        self.agents = agents
        self.history = history
        self.build = build or current_build()
        self.update_checker = update_checker
        self.plugin_update_checker = plugin_update_checker
        self.plugin_source_messages = plugin_source_messages
        self.plugin_source_notices = plugin_source_notices
        self.connector_bridge = connector_bridge
        self.managed_settings = managed_settings
        self.gag_policy = gag_policy
        self.screen_clear_mode = screen_clear_mode
        self.screen_clear_effect = screen_clear_effect
        self.configuration_values = configuration_values
        self.gateway_configuration = gateway_configuration
        self.configuration_bundle = configuration_bundle
        self.gateway_migration = gateway_migration
        self.gateway_id = uuid4()
        self._started = False
        self._update_task: asyncio.Task[None] | None = None
        self._control_locks = {session.world: asyncio.Lock() for session in sessions}
        self._agent_locks = {name: asyncio.Lock() for name in agents.controllers}
        self._managed_settings_lock = asyncio.Lock()

    @classmethod
    async def from_configuration(
        cls,
        bundle: ConfigurationBundle,
        *,
        plugin_scope: Literal["all", "gateway"] = "gateway",
        connector_bridge: ConnectorSessionBridge | None = None,
        migration_source_bundle: ConfigurationBundle | None = None,
        legacy_precedence: bool = True,
    ) -> GatewayRuntime:
        source_bundle = migration_source_bundle or bundle
        managed_settings: ManagedSettingsStore | None
        if legacy_precedence:
            bundle, managed_settings = apply_managed_settings(bundle)
        else:
            managed_settings = None
        initial_registry = core_configuration_registry()
        gateway_configuration = GatewayConfigurationStore(
            bundle.main.managed_settings.state_directory,
            initial_registry,
            defer_plugin_validation=True,
        )
        missing_login_keys = {
            f"worlds.{name}.login.password"
            for name, world in source_bundle.worlds.worlds.items()
            if world.login is not None
            and not gateway_configuration.get(
                f"worlds.{name}.login.password", scope="secret"
            )[0]
        }
        if missing_login_keys:
            references = _stage_legacy_world_login_references(
                source_bundle,
                bundle.main.managed_settings.state_directory,
                only_keys=missing_login_keys,
            )
            for key, reference in references.items():
                gateway_configuration.set(
                    key,
                    reference,
                    scope="secret",
                    target=None,
                    expected_revision=gateway_configuration.revision,
                    actor_ui_id="migration",
                    actor_ui_name="Automatic legacy secret migration",
                )
        gateway_document = gateway_configuration.document
        if (
            gateway_configuration.revision == 0
            and not any(
                section
                for _scope, _target, section in gateway_configuration._sections(gateway_document)
            )
            and managed_settings is not None
            and managed_settings.revision > 0
        ):
            legacy_values: list[tuple[str, object, str, str | None]] = []
            if managed_settings.document.ui.spellcheck is not None:
                legacy_values.append(
                    (
                        "ui.spellcheck.enabled",
                        managed_settings.document.ui.spellcheck.enabled,
                        "shared",
                        None,
                    )
                )
            if managed_settings.document.ui.speaking_streaks is not None:
                legacy_values.append(
                    (
                        "ui.speaking_streaks.enabled",
                        managed_settings.document.ui.speaking_streaks.enabled,
                        "shared",
                        None,
                    )
                )
            legacy_values.extend(
                (
                    f"worlds.{world}.gag.patterns",
                    list(override.gag.patterns),
                    "world",
                    world,
                )
                for world, override in managed_settings.document.worlds.items()
            )
            for key, value, scope, target in legacy_values:
                gateway_configuration.set(
                    key,
                    value,
                    scope=scope,
                    target=target,
                    expected_revision=gateway_configuration.revision,
                    actor_ui_id="migration",
                    actor_ui_name="Legacy managed-settings migration",
                )
        bundle = apply_gateway_configuration(bundle, gateway_configuration.document)
        gag_policy = GagPolicy.from_configuration(bundle.worlds)
        sinks: list[EventSink] = []
        if bundle.main.logging.enabled:
            sinks.append(JsonlEventSink(_event_log_path(bundle.main.logging.directory)))
        event_bus = EventBus(sinks)
        command_bus = CommandBus()
        session_type = ConnectorWorldSession if connector_bridge is not None else WorldSession
        sessions = [
            session_type(
                world=alias,
                config=config,
                defaults=bundle.worlds.defaults,
                event_bus=event_bus,
                command_bus=command_bus,
                show_nospoof_prefix=bundle.main.ui.show_nospoof_prefix,
                **({"bridge": connector_bridge} if connector_bridge is not None else {}),
            )
            for alias, config in bundle.worlds.worlds.items()
        ]
        manager = SessionManager(sessions)
        extra_plugins, plugin_source_failures, plugin_source_notices = await load_plugin_sources(
            bundle.main.plugins.sources,
            plugins_directory=bundle.main.plugins.state_directory,
        )
        for failure in plugin_source_failures:
            print(f"tfr: plugin source {failure.source_id}: {failure.error}", file=sys.stderr)
        for notice in plugin_source_notices:
            print(f"tfr: plugin source {notice.source_id}: {notice.message}", file=sys.stderr)
        plugin_source_messages = tuple(
            [
                f"Plugin source {failure.source_id}: {failure.error}"
                for failure in plugin_source_failures
            ]
            + [
                f"Plugin source {notice.source_id}: {notice.message}"
                for notice in plugin_source_notices
            ]
        )
        plugins = await PluginManager.load(
            enabled=bundle.main.plugins.enabled,
            config=bundle.main.plugins.config,
            event_bus=event_bus,
            command_bus=command_bus,
            targets={session.world: session.session_id for session in sessions},
            worlds={
                session.world: PluginWorldInfo(
                    server=session.config.server,
                    encoding=session.encoding,
                )
                for session in sessions
            },
            extra_discovered=extra_plugins,
            scope=plugin_scope,
            event_visibility=gag_policy.allows,
        )
        configuration_registry = core_configuration_registry()
        for descriptor in plugins.registry.configuration_descriptors():
            if not any(
                item.key == descriptor.key for item in configuration_registry.descriptors()
            ):
                configuration_registry.register(with_builtin_form_metadata(descriptor))
        gateway_configuration.registry = configuration_registry
        gateway_configuration.validate_current(defer_plugin_validation=True)
        gateway_migration = GatewayMigrationStore(
            bundle.main.managed_settings.state_directory,
            gateway_configuration,
        )
        configuration_values = configuration_values_from_bundle(
            bundle if not legacy_precedence else source_bundle,
            configuration_registry,
            managed=(managed_settings.document if managed_settings is not None else None),
        )
        gateway_source_files = tuple(
            str(path)
            for path in (
                source_bundle.main_path,
                source_bundle.worlds_path,
                source_bundle.agents_path,
            )
            if path.is_file()
        )
        migration_values = configuration_values_from_bundle(
            source_bundle,
            configuration_registry,
            managed=(managed_settings.document if managed_settings is not None else None),
        )
        gateway_values = migration_values.jsonc_proposal_values()
        if gateway_values:
            gateway_migration.submit(
                new_proposal(
                    ui_id=UUID(int=0),
                    ui_name="Gateway legacy configuration",
                    base_revision=gateway_configuration.revision,
                    settings=collect_migration_settings(
                        gateway_values,
                        configuration_registry,
                        ui_id=UUID(int=0),
                        source="Gateway JSONC",
                        ui_scope="ui-type",
                        ui_target="terminal",
                    ),
                    source_files=gateway_source_files,
                )
            )
        for scope, _target, section in gateway_configuration._sections(
            gateway_configuration.document
        ):
            if scope not in {"shared", "gateway", "world", "plugin", "secret"}:
                continue
            for key, value in section.items():
                configuration_values.record(
                    key,
                    value,
                    SettingProvenance.MANAGED,
                    source_detail=(
                        f"Gateway configuration revision {gateway_configuration.revision}"
                    ),
                )
        for session in sessions:
            runtime_values = {
                f"worlds.{session.world}.encoding": session.encoding,
                f"worlds.{session.world}.reconnect": session.reconnect,
                f"worlds.{session.world}.scrollback_lines": scrollback_for(bundle, session.world),
                f"worlds.{session.world}.provenance.show_prefix": (session.show_nospoof_prefix),
            }
            for key, value in runtime_values.items():
                current = configuration_values.resolve(key)
                if current is None or current.value != value:
                    configuration_values.record_runtime(
                        key,
                        value,
                        detail="effective world session configuration",
                    )
        manifest_key = "updates.manifest_url"
        manifest_value = str(bundle.main.updates.manifest_url)
        current_manifest = configuration_values.resolve(manifest_key)
        if current_manifest is None or current_manifest.value != manifest_value:
            configuration_values.record_runtime(
                manifest_key,
                manifest_value,
                detail="validated update channel configuration",
            )
        gag_policy.set_diagnostic_handler(
            lambda code, world, index: plugins.diagnostics.record(
                severity="warning",
                component="gag",
                code=code,
                message=(
                    f"Gag matching {code.replace('_', ' ')} for world {world[:64]!r}, "
                    f"pattern index {index}. The event was shown."
                )[:240],
                operation=f"pattern_index:{index}",
            )
        )
        if managed_settings is not None:
            managed_settings.set_audit_handler(
                lambda entry: plugins.diagnostics.record(
                    severity="info" if entry["result"] != "failed" else "error",
                    component="managed-settings",
                    code=str(entry["result"]),
                    message=(
                        f"Managed setting {entry['operation']} for world {entry['world']!r}: "
                        f"{entry['result']} at revision {entry['revision']}"
                    )[:240],
                    operation=str(entry["operation"])[:100],
                )
            )
        event_bus.add_processor(plugins.process_event)
        agents = AgentRuntime.from_configuration(bundle, sessions, event_bus, command_bus)
        history = EventHistory(
            event_bus,
            {alias: scrollback_for(bundle, alias) for alias in bundle.worlds.worlds},
            projection_filter=gag_policy.allows,
        )
        return cls(
            event_bus=event_bus,
            command_bus=command_bus,
            sessions=sessions,
            manager=manager,
            plugins=plugins,
            agents=agents,
            history=history,
            update_checker=(
                UpdateChecker(bundle.main.updates) if plugin_scope == "gateway" else None
            ),
            plugin_update_checker=(
                PluginUpdateChecker(
                    bundle.main.plugins.sources,
                    plugins_directory=bundle.main.plugins.state_directory,
                    config=bundle.main.updates,
                    notified_versions={
                        notice.source_id: notice.available_version
                        for notice in plugin_source_notices
                        if notice.available_version is not None
                    },
                )
                if plugin_scope == "gateway"
                else None
            ),
            plugin_source_messages=plugin_source_messages,
            plugin_source_notices=plugin_source_notices,
            connector_bridge=connector_bridge,
            managed_settings=managed_settings,
            gag_policy=gag_policy,
            screen_clear_mode=bundle.main.ui.screen_clear.mode,
            screen_clear_effect=bundle.main.ui.screen_clear.effect,
            configuration_values=configuration_values,
            gateway_configuration=gateway_configuration,
            configuration_bundle=bundle,
            gateway_migration=gateway_migration,
        )

    async def start(self) -> None:
        if self._started:
            return
        self.history.start()
        if self.connector_bridge is not None:
            self.connector_bridge.start()
        try:
            await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_start"))
            self.agents.start()
            await self.manager.start_autoconnect()
        except BaseException:
            await self.stop()
            raise
        self._started = True
        if self.update_checker is not None and self.update_checker.config.enabled:
            self._update_task = asyncio.create_task(
                self.update_checker.run_periodically(self._notify_update),
                name="tfr-gateway-updates",
            )

    async def manage_gag(
        self,
        world: str,
        operation: Literal["list", "add", "remove", "reset"],
        *,
        pattern: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if self.managed_settings is None and self.gateway_configuration is not None:
            if self.gag_policy is None or self.configuration_bundle is None:
                raise ValueError("managed gag settings are unavailable")
            key = f"worlds.{world}.gag.patterns"
            if world not in self.configuration_bundle.worlds.worlds:
                raise ValueError(f"unknown world: {world}")
            present, stored = self.gateway_configuration.get(key, scope="world", target=world)
            patterns = list(stored) if present and isinstance(stored, list) else list(
                self.configuration_bundle.worlds.worlds[world].gag.patterns
            )
            if operation == "list":
                if pattern is not None or expected_revision is not None:
                    raise ValueError("list gag operation does not accept mutation fields")
                return {
                    "revision": self.gateway_configuration.revision,
                    "patterns": patterns,
                    "changed": False,
                }
            if expected_revision is None:
                raise ValueError("gag mutation requires expected_revision")
            proposed = list(patterns)
            if operation == "add" and pattern is not None:
                if pattern not in proposed:
                    proposed.append(pattern)
            elif operation == "remove" and pattern is not None:
                proposed = [item for item in proposed if item != pattern]
            elif operation == "reset" and pattern is None:
                proposed = []
            else:
                raise ValueError(f"{operation} gag operation has invalid fields")
            changed = proposed != patterns
            if changed:
                mutation = await asyncio.to_thread(
                    self.gateway_configuration.set,
                    key,
                    proposed,
                    scope="world",
                    target=world,
                    expected_revision=expected_revision,
                    actor_ui_id="legacy-command",
                    actor_ui_name="Gag command",
                )
                if self.configuration_values is not None:
                    self.configuration_values.record(
                        key,
                        proposed,
                        SettingProvenance.MANAGED,
                        source_detail=f"Gateway configuration revision {mutation.revision}",
                    )
                effective = apply_gateway_configuration(
                    self.configuration_bundle, self.gateway_configuration.document
                )
                self.gag_policy.replace_configuration(effective.worlds)
            return {
                "revision": self.gateway_configuration.revision,
                "patterns": proposed,
                "changed": changed,
                "restart_required": False,
            }
        if self.managed_settings is None or self.gag_policy is None:
            raise ValueError("managed gag settings are unavailable")
        if operation == "list":
            if pattern is not None or expected_revision is not None:
                raise ValueError("list gag operation does not accept mutation fields")
            listed = await asyncio.to_thread(self.managed_settings.list_gag_patterns, world)
            return listed.model_dump(mode="json")
        if expected_revision is None:
            raise ValueError("gag mutation requires expected_revision")
        async with self._managed_settings_lock:
            inventory_key = f"worlds.{world}.gag.patterns"
            previous_inventory = (
                self.configuration_values.resolve(inventory_key)
                if self.configuration_values is not None
                else None
            )
            if operation == "add":
                if pattern is None:
                    raise ValueError("add gag operation requires a pattern")
                result = await asyncio.to_thread(
                    self.managed_settings.add_gag_pattern,
                    world,
                    pattern,
                    expected_revision,
                )
            elif operation == "remove":
                if pattern is None:
                    raise ValueError("remove gag operation requires a pattern")
                result = await asyncio.to_thread(
                    self.managed_settings.remove_gag_pattern,
                    world,
                    pattern,
                    expected_revision,
                )
            elif operation == "reset":
                if pattern is not None:
                    raise ValueError("reset gag operation does not accept a pattern")
                result = await asyncio.to_thread(
                    self.managed_settings.reset_gag_patterns,
                    world,
                    expected_revision,
                )
            else:
                raise ValueError("unsupported gag operation")
            restart_required = False
            if result.changed:
                try:
                    self.gag_policy.replace_configuration(self.managed_settings.effective_worlds)
                except Exception:
                    restart_required = True
                    self.plugins.diagnostics.record(
                        severity="error",
                        component="gag",
                        code="runtime_swap_failed",
                        message=(
                            f"Persisted gag revision {result.revision} for world {world!r}; "
                            "restart the Gateway to apply it"
                        )[:240],
                    )
            if self.configuration_values is not None:
                if world in self.managed_settings.document.worlds:
                    self.configuration_values.record(
                        inventory_key,
                        list(result.patterns),
                        SettingProvenance.MANAGED,
                        source_detail=f"managed override revision {result.revision}",
                    )
                else:
                    self.configuration_values.clear(inventory_key, SettingProvenance.MANAGED)
                if restart_required and previous_inventory is not None:
                    self.configuration_values.record_runtime(
                        inventory_key,
                        previous_inventory.value,
                        detail="active runtime configuration; Gateway restart required",
                    )
                else:
                    self.configuration_values.clear(inventory_key, SettingProvenance.RUNTIME)
            return {
                **result.model_dump(mode="json"),
                "restart_required": restart_required,
            }

    def shared_ui_settings(self) -> dict[str, bool]:
        if self.managed_settings is None:
            settings = {"spellcheck_enabled": False, "speaking_streaks_enabled": True}
        else:
            ui = self.managed_settings.effective_ui
            settings = {
                "spellcheck_enabled": ui.spellcheck.enabled,
                "speaking_streaks_enabled": ui.speaking_streaks.enabled,
            }
        if self.gateway_configuration is not None:
            for key, output in (
                ("ui.spellcheck.enabled", "spellcheck_enabled"),
                ("ui.speaking_streaks.enabled", "speaking_streaks_enabled"),
            ):
                present, value = self.gateway_configuration.get(key, scope="shared")
                if present and isinstance(value, bool):
                    settings[output] = value
        return settings

    async def manage_shared_ui_setting(
        self,
        setting: Literal["spellcheck", "speaking_streaks"],
        operation: Literal["get", "set"],
        *,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if self.managed_settings is None and self.gateway_configuration is not None:
            key = f"ui.{setting}.enabled"
            current = self.shared_ui_settings()[f"{setting}_enabled"]
            if operation == "get":
                if enabled is not None or expected_revision is not None:
                    raise ValueError("get UI setting operation does not accept mutation fields")
                return {
                    "revision": self.gateway_configuration.revision,
                    "enabled": current,
                    "changed": False,
                }
            if enabled is None or expected_revision is None:
                raise ValueError("set UI setting operation requires enabled and expected_revision")
            if enabled == current:
                return {
                    "revision": self.gateway_configuration.revision,
                    "enabled": enabled,
                    "changed": False,
                }
            mutation = await asyncio.to_thread(
                self.gateway_configuration.set,
                key,
                enabled,
                scope="shared",
                target=None,
                expected_revision=expected_revision,
                actor_ui_id="legacy-command",
                actor_ui_name="UI setting command",
            )
            if self.configuration_values is not None:
                self.configuration_values.record(
                    key,
                    enabled,
                    SettingProvenance.MANAGED,
                    source_detail=f"Gateway configuration revision {mutation.revision}",
                )
            return {"revision": mutation.revision, "enabled": enabled, "changed": True}
        if self.managed_settings is None:
            raise ValueError("managed UI settings are unavailable")
        getter = (
            self.managed_settings.get_spellcheck_enabled
            if setting == "spellcheck"
            else self.managed_settings.get_speaking_streaks_enabled
        )
        if operation == "get":
            if enabled is not None or expected_revision is not None:
                raise ValueError("get UI setting operation does not accept mutation fields")
            result = await asyncio.to_thread(getter)
            return result.model_dump(mode="json")
        if enabled is None or expected_revision is None:
            raise ValueError("set UI setting operation requires enabled and expected_revision")
        setter = (
            self.managed_settings.set_spellcheck_enabled
            if setting == "spellcheck"
            else self.managed_settings.set_speaking_streaks_enabled
        )
        async with self._managed_settings_lock:
            result = await asyncio.to_thread(setter, enabled, expected_revision)
        if self.configuration_values is not None:
            self.configuration_values.record(
                f"ui.{setting}.enabled",
                result.enabled,
                SettingProvenance.MANAGED,
                source_detail=f"managed override revision {result.revision}",
            )
        return result.model_dump(mode="json")

    async def stop(self, *, preserve_worlds: bool = False) -> None:
        if not self._started and self.history._pump is None:
            if self.connector_bridge is not None:
                await self.connector_bridge.close(preserve_worlds=preserve_worlds)
            await self.event_bus.close()
            return
        self._started = False
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None
        await self.agents.stop()
        if preserve_worlds:
            await self.manager.detach_all()
        else:
            await self.manager.stop_all()
        if self.connector_bridge is not None:
            await self.connector_bridge.close(preserve_worlds=preserve_worlds)
        await self.plugins.drain()
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_stop"))
        await self.plugins.drain()
        await self.history.stop()
        await self.event_bus.close()

    def _notify_update(self, result: UpdateResult) -> None:
        channel = self.update_checker.config.channel if self.update_checker is not None else None
        print(
            format_update_status("Gateway", self.build, result, channel=channel),
            file=sys.stderr,
            flush=True,
        )

    def world_descriptors(self) -> list[dict[str, Any]]:
        agent_worlds = {controller.session.world for controller in self.agents.controllers.values()}
        return [
            {
                "world": session.world,
                "session_id": str(session.session_id),
                "state": session.state.value,
                "connection_generation": session.connection_generation,
                "server": session.config.server,
                "encoding": session.encoding,
                "character": session.character_name,
                "capabilities": {
                    "unicode": session.config.capabilities.unicode,
                },
                "aliases": list(session.config.aliases),
                "agent": session.world in agent_worlds,
                "scrollback_lines": self.history.limits[session.world],
                "show_nospoof_prefix": session.show_nospoof_prefix,
            }
            for session in self.sessions
        ]

    def agent_descriptors(self) -> list[dict[str, Any]]:
        return [
            {
                "name": controller.name,
                "world": controller.session.world,
                "state": controller.inspection.state,
                "paused": controller.paused,
                "provider": controller.provider.name,
                "endpoint": controller.provider.endpoint,
                "model": controller.config.model,
            }
            for controller in self.agents.controllers.values()
        ]

    def session_for(self, world: str) -> WorldSession:
        try:
            return self.manager.sessions[world]
        except KeyError as exc:
            raise ValueError(f"unknown world: {world}") from exc

    async def submit_command(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
        sensitive: bool = False,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        expected_connection_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        ui_id: UUID | None = None,
        ui_name: str | None = None,
    ) -> None:
        request = self._command_request(
            world=world,
            text=text,
            client_id=client_id,
            request_id=request_id,
            sensitive=sensitive,
            actor=actor,
            correlation_id=correlation_id,
            causation_id=causation_id,
            expected_connection_generation=expected_connection_generation,
            metadata=metadata,
            ui_id=ui_id,
            ui_name=ui_name,
        )
        await self.command_bus.submit(request)

    def submit_command_nowait(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
        expected_connection_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        ui_id: UUID | None = None,
        ui_name: str | None = None,
    ) -> None:
        request = self._command_request(
            world=world,
            text=text,
            client_id=client_id,
            request_id=request_id,
            actor=actor,
            correlation_id=correlation_id,
            expected_connection_generation=expected_connection_generation,
            metadata=metadata,
            ui_id=ui_id,
            ui_name=ui_name,
        )
        self.command_bus.submit_nowait(request)

    def _command_request(
        self,
        *,
        world: str,
        text: str,
        client_id: str,
        request_id: UUID,
        sensitive: bool = False,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        expected_connection_generation: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        ui_id: UUID | None = None,
        ui_name: str | None = None,
    ) -> CommandRequest:
        session = self.session_for(world)
        if len(text) > MAX_COMMAND_CHARACTERS:
            raise ValueError("command text exceeds the gateway limit")
        if any(character in text for character in "\r\n\0"):
            raise ValueError("command text cannot contain CR, LF, or NUL")
        if (
            expected_connection_generation is not None
            and expected_connection_generation != session.connection_generation
        ):
            raise ValueError("world connection changed before command submission")
        actor_identity = ui_name or client_id
        request_actor = actor or Actor(ActorType.HUMAN, f"ui:{actor_identity}")
        if len(request_actor.id) > MAX_ACTOR_ID_CHARACTERS:
            raise ValueError("remote actor ID exceeds the gateway limit")
        if request_actor.type not in {ActorType.HUMAN, ActorType.PLUGIN}:
            raise ValueError("remote commands must have a human or plugin actor")
        if request_actor.type is ActorType.HUMAN:
            request_actor = Actor(ActorType.HUMAN, f"ui:{actor_identity}")
        request_metadata = {**dict(metadata or {}), "gateway_connection_id": client_id}
        if ui_id is not None:
            request_metadata["gateway_ui_id"] = str(ui_id)
        if ui_name is not None:
            request_metadata["gateway_ui_name"] = ui_name
        return CommandRequest(
            session_id=session.session_id,
            world=world,
            actor=request_actor,
            text=text,
            request_id=request_id,
            sensitive=sensitive,
            correlation_id=correlation_id,
            causation_id=causation_id,
            expected_connection_generation=expected_connection_generation,
            metadata=request_metadata,
        )

    async def control(self, *, world: str, action: str) -> None:
        session = self.session_for(world)
        async with self._control_locks[world]:
            if action == "connect":
                if session.state is not SessionState.STOPPED:
                    raise ValueError(f"world {world!r} is already {session.state.value}")
                await session.start()
            elif action == "disconnect":
                await session.stop()
            elif action == "reconnect":
                await session.stop()
                await session.start()
            else:
                raise ValueError(f"unknown control action: {action}")

    async def agent_control(self, *, name: str, action: str) -> None:
        try:
            controller = self.agents.controllers[name]
            lock = self._agent_locks[name]
        except KeyError as exc:
            raise ValueError(f"unknown agent: {name}") from exc
        async with lock:
            if action == "pause":
                await controller.pause()
            elif action == "resume":
                controller.resume()
            elif action == "trigger":
                if not controller.manual_turn():
                    raise ValueError(f"agent {name!r} cannot be triggered")
            else:
                raise ValueError(f"unknown agent control action: {action}")


class GatewayServer:
    def __init__(
        self,
        runtime: GatewayRuntime,
        path: Path | str,
        *,
        maximum_clients: int = 16,
        maximum_tcp_clients: int = 8,
        maximum_pending_tcp_clients: int = 16,
        maximum_pending_tcp_clients_per_host: int = 2,
        tcp_host: str | None = None,
        tcp_port: int = DEFAULT_GATEWAY_PORT,
        tcp_auth_token: str | None = None,
        tcp_ssl_context: ssl.SSLContext | None = None,
        pairing_url_factory: Callable[
            [str], str | Mapping[str, Any] | Awaitable[str | Mapping[str, Any]]
        ]
        | None = None,
        device_list_factory: Callable[[], list[dict[str, Any]]] | None = None,
        device_revoke_factory: Callable[[UUID], Awaitable[bool]] | None = None,
        web_origin: str | None = None,
        web_status_factory: Callable[[], Mapping[str, Any]] | None = None,
        diagnostic_list_factory: Callable[[str | None], list[dict[str, Any]]] | None = None,
        diagnostic_clear_factory: Callable[[], None] | None = None,
        update_notice: Callable[[str], Awaitable[None]] | None = None,
        ui_settings_notice: Callable[[Mapping[str, bool]], Awaitable[None]] | None = None,
        configuration_notice: Callable[[], Awaitable[None]] | None = None,
        ui_store: UiStore | None = None,
    ) -> None:
        if (
            maximum_clients < 1
            or maximum_tcp_clients < 1
            or maximum_pending_tcp_clients < 1
            or maximum_pending_tcp_clients_per_host < 1
        ):
            raise ValueError("client limits must be positive")
        self.runtime = runtime
        self.path = Path(path).expanduser()
        self.maximum_clients = maximum_clients
        self.maximum_tcp_clients = maximum_tcp_clients
        self.maximum_pending_tcp_clients = maximum_pending_tcp_clients
        self.maximum_pending_tcp_clients_per_host = maximum_pending_tcp_clients_per_host
        if tcp_host is None:
            if tcp_auth_token is not None or tcp_ssl_context is not None:
                raise ValueError("gateway TCP authentication and TLS require a TCP host")
            self.tcp_host = None
            self.tcp_port = tcp_port
        else:
            self.tcp_host, self.tcp_port = validate_tcp_endpoint(
                tcp_host,
                tcp_port,
                allow_zero=True,
            )
            if tcp_auth_token is None:
                raise ValueError("gateway TCP listener requires an authentication token")
            if tcp_ssl_context is None:
                raise ValueError("gateway TCP listener requires TLS")
            validate_gateway_token(tcp_auth_token)
        self.tcp_auth_token = tcp_auth_token
        self.tcp_ssl_context = tcp_ssl_context
        self.pairing_url_factory = pairing_url_factory
        self.device_list_factory = device_list_factory
        self.device_revoke_factory = device_revoke_factory
        self.web_origin = web_origin
        self.web_status_factory = web_status_factory
        self.diagnostic_list_factory = diagnostic_list_factory
        self.diagnostic_clear_factory = diagnostic_clear_factory
        self.update_notice = update_notice
        self.ui_settings_notice = ui_settings_notice
        self.configuration_notice = configuration_notice
        self.ui_store = ui_store
        self._server: asyncio.Server | None = None
        self._tcp_server: asyncio.Server | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[Any]] = set()
        self._tcp_clients: set[asyncio.Task[Any]] = set()
        self._pending_tcp_clients: dict[asyncio.Task[Any], str] = {}
        self._update_participants: dict[str, _GatewayUpdateParticipant] = {}
        self._update_participants_lock = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._accepting_update_participants = True
        self._staged_update: StagedManagedUpdate | None = None
        self._update_transaction_participants: tuple[_GatewayUpdateParticipant, ...] = ()
        self._update_gateway_restart = False
        self._update_force = False
        self._update_task: asyncio.Task[None] | None = None
        self._update_restart = asyncio.Event()

    async def start(self, *, start_serving: bool = True) -> None:
        if self._server is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_socket_parent()
        remove_stale_gateway_socket(self.path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            socket_stat = self.path.stat(follow_symlinks=False)
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self.path, 0o600)
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._accept_unix_client,
                sock=listener,
                limit=MAX_MESSAGE_BYTES,
                start_serving=start_serving,
            )
            if self.tcp_host is not None:
                self._tcp_server = await asyncio.start_server(
                    self._accept_tcp_client,
                    self.tcp_host,
                    self.tcp_port,
                    ssl=self.tcp_ssl_context,
                    ssl_handshake_timeout=5,
                    limit=MAX_MESSAGE_BYTES,
                    start_serving=start_serving,
                )
        except BaseException:
            listener.close()
            await self.stop()
            raise

    async def start_serving(self) -> None:
        if self._server is None:
            raise RuntimeError("gateway server has not been started")
        await self._server.start_serving()
        if self._tcp_server is not None:
            await self._tcp_server.start_serving()

    async def stop(self) -> None:
        if (
            self._update_task is not None
            and self._update_task is not asyncio.current_task()
            and not self._update_task.done()
        ):
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            self._tcp_server = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in tuple(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*tuple(self._clients), return_exceptions=True)
        self._clients.clear()
        self._tcp_clients.clear()
        self._pending_tcp_clients.clear()
        self._remove_owned_socket()

    async def wait_update_restart(self) -> None:
        await self._update_restart.wait()

    def _validate_socket_parent(self) -> None:
        if os.name != "posix":
            return
        parent = self.path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode):
            raise RuntimeError(f"gateway socket parent is not a directory: {self.path.parent}")
        if parent.st_uid != os.geteuid():
            raise RuntimeError(
                f"gateway socket parent is not owned by this user: {self.path.parent}"
            )
        if stat.S_IMODE(parent.st_mode) & 0o077:
            raise RuntimeError(f"gateway socket parent must have mode 0700: {self.path.parent}")

    def _remove_owned_socket(self) -> None:
        if self._socket_identity is not None:
            with contextlib.suppress(FileNotFoundError):
                current = self.path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == self._socket_identity and stat.S_ISSOCK(
                    current.st_mode
                ):
                    self.path.unlink()
        self._socket_identity = None

    @property
    def tcp_addresses(self) -> tuple[tuple[Any, ...], ...]:
        if self._tcp_server is None:
            return ()
        return tuple(sock.getsockname() for sock in self._tcp_server.sockets or ())

    def _accept_unix_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._accept_client(reader, writer, auth_token=None)

    def _accept_tcp_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        if (
            len(self._pending_tcp_clients) >= self.maximum_pending_tcp_clients
            or sum(pending_host == peer_host for pending_host in self._pending_tcp_clients.values())
            >= self.maximum_pending_tcp_clients_per_host
        ):
            writer.close()
            return
        assert self.tcp_auth_token is not None
        task = self._accept_client(reader, writer, auth_token=self.tcp_auth_token)
        if task is not None:
            self._pending_tcp_clients[task] = peer_host

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        auth_token: str | None,
    ) -> asyncio.Task[Any] | None:
        unix_clients = len(self._clients) - len(self._tcp_clients) - len(self._pending_tcp_clients)
        if auth_token is None and unix_clients >= self.maximum_clients:
            writer.close()
            return None
        task = asyncio.create_task(
            self._handle_client(reader, writer, auth_token=auth_token),
            name="tfr-gateway-client",
        )
        self._clients.add(task)
        task.add_done_callback(self._client_done)
        return task

    def _client_done(self, task: asyncio.Task[Any]) -> None:
        self._clients.discard(task)
        self._tcp_clients.discard(task)
        self._pending_tcp_clients.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        auth_token: str | None,
    ) -> None:
        subscription: HistorySubscription | None = None
        write_lock = asyncio.Lock()
        sender: asyncio.Task[None] | None = None
        receiver: asyncio.Task[None] | None = None
        participant: _GatewayUpdateParticipant | None = None
        try:
            hello = await asyncio.wait_for(
                read_message(reader, maximum_bytes=MAX_HELLO_BYTES),
                timeout=HELLO_TIMEOUT_SECONDS,
            )
            if hello is None or hello.get("type") != "hello":
                raise GatewayProtocolError("first message must be hello")
            if auth_token is not None:
                supplied_token = hello.get("auth_token")
                if not isinstance(supplied_token, str) or not hmac.compare_digest(
                    supplied_token,
                    auth_token,
                ):
                    raise GatewayProtocolError("gateway authentication failed")
                task = asyncio.current_task()
                if task is None or task not in self._pending_tcp_clients:
                    raise GatewayProtocolError("gateway authentication state is invalid")
                if len(self._tcp_clients) >= self.maximum_tcp_clients:
                    raise GatewayProtocolError("gateway remote client limit reached")
                self._pending_tcp_clients.pop(task)
                self._tcp_clients.add(task)
            client_id = self._client_id(hello.get("client_id"))
            admin = hello.get("admin", False)
            if not isinstance(admin, bool):
                raise GatewayProtocolError("admin must be a boolean")
            if admin and auth_token is not None:
                raise GatewayProtocolError("gateway administration requires the local socket")
            connection_id = str(uuid4())
            client_build: BuildIdentity | None = None
            if "build" in hello:
                try:
                    client_build = BuildIdentity.from_mapping(hello["build"])
                except UpdateError as exc:
                    raise GatewayProtocolError(f"invalid client build identity: {exc}") from exc
            managed_updates = hello.get("managed_updates", False)
            if not isinstance(managed_updates, bool):
                raise GatewayProtocolError("managed_updates must be a boolean")
            update_checks = hello.get("update_checks", False)
            if not isinstance(update_checks, bool):
                raise GatewayProtocolError("update_checks must be a boolean")
            requested_gateway = hello.get("gateway_id")
            if requested_gateway is not None and not isinstance(requested_gateway, str):
                raise GatewayProtocolError("gateway_id must be a string or null")
            after_cursor = hello.get("after_cursor")
            if after_cursor is not None and (
                not isinstance(after_cursor, int)
                or isinstance(after_cursor, bool)
                or after_cursor < 0
            ):
                raise GatewayProtocolError("after_cursor must be a non-negative integer or null")
            history_reset = requested_gateway not in {None, str(self.runtime.gateway_id)} or (
                requested_gateway is None and after_cursor is not None
            )
            subscription = await self.runtime.history.subscribe(
                None if history_reset else after_cursor,
                include_history=not admin,
            )
            snapshot = subscription.snapshot
            if len(snapshot.events) > MAX_SNAPSHOT_EVENTS:
                raise GatewayProtocolError("retained history exceeds the protocol snapshot limit")
            ui_record: UiRecord | None = None
            if "ui" in hello:
                if admin:
                    raise GatewayProtocolError("administrative clients cannot register as a UI")
                if self.ui_store is None:
                    raise GatewayProtocolError("gateway UI registration is unavailable")
                try:
                    ui_record = await self.ui_store.register_metadata(UUID(client_id), hello["ui"])
                except ValueError as exc:
                    raise GatewayProtocolError(f"invalid UI registration: {exc}") from exc
            hello_response: dict[str, Any] = {
                "type": "hello",
                "protocol": PROTOCOL_VERSION,
                "gateway_id": str(self.runtime.gateway_id),
                "client_id": client_id,
                "connection_id": connection_id,
                "cursor": snapshot.cursor,
                "oldest_cursor": snapshot.oldest_cursor,
                "snapshot_count": len(snapshot.events),
                "history_truncated": snapshot.truncated,
                "history_reset": history_reset,
                "worlds": self.runtime.world_descriptors(),
                "agents": self.runtime.agent_descriptors(),
                "ui_settings": (
                    self.runtime.shared_ui_settings()
                    if hasattr(self.runtime, "shared_ui_settings")
                    else {
                        "spellcheck_enabled": False,
                        "speaking_streaks_enabled": True,
                    }
                ),
                "effect_demos": self._native_effect_demos(),
                "speaker_effects": self._speaker_effects_summary(),
                "build": getattr(self.runtime, "build", current_build()).as_dict(),
            }
            if ui_record is not None:
                hello_response["ui"] = ui_record.as_dict()
                if CONFIGURATION_SYNC_CAPABILITY in ui_record.capabilities:
                    hello_response["configuration"] = effective_client_configuration(
                        self.runtime.configuration_values,
                        self.runtime.gateway_configuration,
                        client_type=ui_record.client_type,
                        ui_id=ui_record.ui_id,
                    ).as_dict()
            await write_message(
                writer,
                hello_response,
                lock=write_lock,
            )
            for item in snapshot.events:
                await write_message(
                    writer,
                    self._native_event_message(item.cursor, item.event),
                    lock=write_lock,
                )
            if not admin:
                participant = _GatewayUpdateParticipant(
                    connection_id=connection_id,
                    ui_id=ui_record.ui_id if ui_record is not None else None,
                    writer=writer,
                    write_lock=write_lock,
                    build=client_build,
                    managed_updates=managed_updates,
                    update_checks=update_checks,
                    pending={},
                    configuration_sync=(
                        ui_record is not None
                        and CONFIGURATION_SYNC_CAPABILITY in ui_record.capabilities
                    ),
                )
                async with self._update_participants_lock:
                    if not self._accepting_update_participants:
                        raise GatewayProtocolError("Gateway update in progress; retry shortly")
                    self._update_participants[connection_id] = participant
            sender = asyncio.create_task(
                self._send_events(subscription, writer, write_lock),
                name=f"tfr-gateway-events-{client_id}",
            )
            receiver = asyncio.create_task(
                self._receive_requests(
                    reader,
                    connection_id,
                    writer,
                    write_lock,
                    allow_admin=True,
                    ui_id=ui_record.ui_id if ui_record is not None else None,
                    # Native Unix-socket and authenticated TLS clients use the owner
                    # administration protocol. PWA devices use a separate restricted API.
                    configuration_admin=True,
                ),
                name=f"tfr-gateway-requests-{connection_id}",
            )
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (GatewayProtocolError, TimeoutError, ValueError) as exc:
            with contextlib.suppress(Exception):
                await write_message(
                    writer,
                    {"type": "error", "message": str(exc)},
                    lock=write_lock,
                )
        finally:
            if participant is not None:
                async with self._update_participants_lock:
                    self._update_participants.pop(participant.connection_id, None)
                for future in participant.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("UI disconnected during update"))
            for task in (sender, receiver):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (sender, receiver) if task is not None),
                return_exceptions=True,
            )
            if subscription is not None:
                await self.runtime.history.unsubscribe(subscription)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_events(
        self,
        subscription: HistorySubscription,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        while True:
            item = await subscription.queue.get()
            if item is None:
                raise GatewayProtocolError("event stream overflowed; reconnect for backfill")
            await write_message(
                writer,
                self._native_event_message(item.cursor, item.event),
                lock=write_lock,
            )

    def _native_event_message(self, cursor: int, event: Event) -> dict[str, Any]:
        plugins = getattr(self.runtime, "plugins", None)
        if not isinstance(plugins, PluginManager):
            return event_message(cursor, event)
        text = terminal_plain_text(
            event.display_text or event.plain_text or event.canonical_text or ""
        )
        groups = plugins.presentation_programs_by_plugin(event, text)
        presentations = [
            {"source": source, "presentation": effect_programs_as_dict(programs)}
            for source, programs in groups
        ]
        message = event_message(
            cursor,
            event,
            presentations=presentations,
            presentation_text=text,
        )
        try:
            encode_message(message)
        except GatewayProtocolError:
            return event_message(cursor, event)
        return message

    def _speaker_effects_summary(self) -> dict[str, Any] | None:
        plugins = getattr(self.runtime, "plugins", None)
        if not isinstance(plugins, PluginManager):
            return None
        loaded = "speaker_effects" in plugins.loaded_plugins
        return {
            "loaded": loaded,
            "speakers": list(plugins.configured_speaker_effects()) if loaded else [],
        }

    def _native_effect_demos(self) -> list[dict[str, Any]]:
        plugins = getattr(self.runtime, "plugins", None)
        if not isinstance(plugins, PluginManager) or not hasattr(self.runtime, "session_for"):
            return []
        worlds = [str(item["world"]) for item in self.runtime.world_descriptors()]
        output: list[dict[str, Any]] = []
        encoded_bytes = 2
        for demo in plugins.effect_demos()[:100]:
            world = next(
                (candidate for candidate in demo.worlds if candidate in worlds),
                worlds[0] if worlds and not demo.worlds else None,
            )
            if world is None:
                continue
            session = self.runtime.session_for(world)
            samples = []
            for sample in demo.samples:
                event = Event(
                    session_id=session.session_id,
                    world=world,
                    connection_generation=session.connection_generation,
                    sequence=0,
                    direction=Direction.INBOUND,
                    kind=sample.kind,
                    event_id=uuid4(),
                    timestamp=datetime.now(UTC),
                    canonical_text=sample.text,
                    plain_text=sample.text,
                    display_text=sample.text,
                    parser_name="effects-lab",
                    confidence=Confidence.INFERRED,
                    provenance=Provenance(
                        sender_name=demo.label if demo.category == "speaker" else None,
                        confidence=Confidence.INFERRED,
                    ),
                )
                samples.append(self._native_event_message(1, event))
            descriptor = {
                "id": demo.id,
                "label": demo.label,
                "category": demo.category,
                "samples": samples,
                "worlds": list(demo.worlds),
            }
            descriptor_bytes = len(json.dumps(descriptor, ensure_ascii=True).encode("utf-8"))
            if encoded_bytes + descriptor_bytes > MAX_NATIVE_EFFECT_DEMOS_BYTES:
                break
            output.append(descriptor)
            encoded_bytes += descriptor_bytes
        return output

    async def _receive_requests(
        self,
        reader: asyncio.StreamReader,
        client_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
        *,
        allow_admin: bool,
        ui_id: UUID | None,
        configuration_admin: bool,
    ) -> None:
        update_requests: set[asyncio.Task[None]] = set()
        try:
            while message := await read_message(reader):
                request = self._handle_request(
                    message,
                    client_id,
                    writer,
                    write_lock,
                    allow_admin=allow_admin,
                    ui_id=ui_id,
                    configuration_admin=configuration_admin,
                )
                if message.get("type") != "update":
                    await request
                    continue
                task = asyncio.create_task(request, name="tfr-gateway-update-request")
                update_requests.add(task)
                task.add_done_callback(self._update_request_done)
                task.add_done_callback(update_requests.discard)
        finally:
            for task in update_requests:
                task.cancel()
            await asyncio.gather(*update_requests, return_exceptions=True)

    @staticmethod
    def _update_request_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _handle_request(
        self,
        message: dict[str, Any],
        client_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
        *,
        allow_admin: bool,
        ui_id: UUID | None,
        configuration_admin: bool,
    ) -> None:
        request_id_value = message.get("request_id")
        result: dict[str, Any] | None = None
        start_update = False
        try:
            request_id = UUID(str(request_id_value))
        except (TypeError, ValueError, AttributeError):
            raise GatewayProtocolError("request_id must be a UUID") from None
        try:
            message_type = message["type"]
            if message_type == "ack":
                self._handle_update_ack(client_id, request_id, message)
                return
            if message_type == "configuration_applied":
                if ui_id is None or self.ui_store is None:
                    raise ValueError("configuration acknowledgement requires a registered UI")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "revision",
                    "status",
                    "issues",
                }:
                    raise ValueError("configuration acknowledgement has invalid fields")
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway configuration is unavailable")
                revision, status, issues = validate_configuration_ack(
                    {
                        "revision": message.get("revision"),
                        "status": message.get("status"),
                        "issues": message.get("issues"),
                    },
                    maximum_revision=store.revision,
                )
                participant = self._update_participants.get(client_id)
                if participant is not None:
                    participant.configuration_status = status
                    participant.configuration_issues = issues
                await self.ui_store.report_configuration(ui_id, revision, status, issues)
                result = {"revision": revision, "status": status}
            elif message_type == "ui_capabilities":
                if ui_id is None or self.ui_store is None:
                    raise ValueError("capability update requires a registered UI")
                if set(message) != {"type", "protocol", "request_id", "capabilities"}:
                    raise ValueError("ui_capabilities has invalid fields")
                capabilities = message.get("capabilities")
                if not isinstance(capabilities, list):
                    raise ValueError("UI capabilities must be a list")
                record = await self.ui_store.update_capabilities(ui_id, capabilities)
                result = {"ui": record.as_dict()}
            elif message_type == "command":
                world = self._world(message)
                text = message.get("text")
                sensitive = message.get("sensitive", False)
                if not isinstance(text, str) or not text:
                    raise ValueError("command text must be a non-empty string")
                if not isinstance(sensitive, bool):
                    raise ValueError("sensitive must be a boolean")
                actor_value = message.get("actor")
                if actor_value is None:
                    actor = None
                elif isinstance(actor_value, dict):
                    actor = Actor(
                        ActorType(actor_value.get("type")), str(actor_value.get("id", ""))
                    )
                else:
                    raise ValueError("actor must be an object or null")
                correlation_id = self._optional_uuid(
                    message.get("correlation_id"), "correlation_id"
                )
                causation_id = self._optional_uuid(message.get("causation_id"), "causation_id")
                expected_generation = message.get("expected_connection_generation")
                if expected_generation is not None and (
                    not isinstance(expected_generation, int)
                    or isinstance(expected_generation, bool)
                    or expected_generation < 0
                ):
                    raise ValueError(
                        "expected_connection_generation must be a non-negative integer or null"
                    )
                metadata = message.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise ValueError("metadata must be an object")
                current_ui = (
                    self.ui_store.get(ui_id)
                    if self.ui_store is not None and ui_id is not None
                    else None
                )
                await self.runtime.submit_command(
                    world=world,
                    text=text,
                    client_id=client_id,
                    request_id=request_id,
                    sensitive=sensitive,
                    actor=actor,
                    correlation_id=correlation_id,
                    causation_id=causation_id,
                    expected_connection_generation=expected_generation,
                    metadata=metadata,
                    ui_id=ui_id,
                    ui_name=current_ui.display_name if current_ui is not None else None,
                )
            elif message_type == "control":
                world = self._world(message)
                action = message.get("action")
                if not isinstance(action, str):
                    raise ValueError("control action must be a string")
                await self.runtime.control(world=world, action=action)
            elif message_type == "agent_control":
                name = message.get("agent")
                action = message.get("action")
                if not isinstance(name, str) or not name:
                    raise ValueError("agent must be a non-empty string")
                if not isinstance(action, str):
                    raise ValueError("agent control action must be a string")
                await self.runtime.agent_control(name=name, action=action)
            elif message_type == "ping":
                pass
            elif message_type == "gag_settings":
                if not allow_admin:
                    raise ValueError("managed gag settings require native Gateway access")
                allowed_fields = {
                    "type",
                    "protocol",
                    "request_id",
                    "world",
                    "operation",
                    "pattern",
                    "expected_revision",
                }
                if not set(message) <= allowed_fields:
                    raise ValueError("gag_settings has invalid fields")
                world = self._world(message)
                operation = message.get("operation")
                if operation not in {"list", "add", "remove", "reset"}:
                    raise ValueError("gag operation is invalid")
                pattern = message.get("pattern")
                if pattern is not None and not isinstance(pattern, str):
                    raise ValueError("gag pattern must be a string")
                expected_revision = message.get("expected_revision")
                if expected_revision is not None and (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 0
                ):
                    raise ValueError("expected_revision must be a non-negative integer")
                result = await self.runtime.manage_gag(
                    world,
                    operation,
                    pattern=pattern,
                    expected_revision=expected_revision,
                )
            elif message_type == "managed_ui_setting":
                if not allow_admin:
                    raise ValueError("managed UI settings require native Gateway access")
                if not set(message) <= {
                    "type",
                    "protocol",
                    "request_id",
                    "setting",
                    "operation",
                    "enabled",
                    "expected_revision",
                }:
                    raise ValueError("managed_ui_setting has invalid fields")
                setting = message.get("setting")
                operation = message.get("operation")
                if setting not in {"spellcheck", "speaking_streaks"}:
                    raise ValueError("managed UI setting is invalid")
                if operation not in {"get", "set"}:
                    raise ValueError("managed UI setting operation is invalid")
                enabled = message.get("enabled")
                if enabled is not None and not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                expected_revision = message.get("expected_revision")
                if expected_revision is not None and (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 0
                ):
                    raise ValueError("expected_revision must be a non-negative integer")
                result = await self.runtime.manage_shared_ui_setting(
                    setting,
                    operation,
                    enabled=enabled,
                    expected_revision=expected_revision,
                )
                if operation == "set":
                    await self._broadcast_ui_settings()
            elif message_type == "update":
                if set(message) not in (
                    {"type", "protocol", "request_id"},
                    {"type", "protocol", "request_id", "force"},
                ):
                    raise ValueError("update has invalid fields")
                force = message.get("force", False)
                if not isinstance(force, bool):
                    raise ValueError("update force marker must be a boolean")
                staged = await self._stage_gateway_update(force=force)
                if staged is None:
                    result = {"updated": False}
                else:
                    result = {
                        "updated": True,
                        "version": staged.version,
                        "commit": staged.commit,
                        "release_url": staged.release_url,
                        "restart_required": any(
                            participant.connection_id == client_id
                            for participant in self._update_transaction_participants
                        ),
                    }
                    start_update = True
            elif message_type == "pair_device":
                if not allow_admin:
                    raise ValueError(
                        "web device pairing requires an attached authenticated native UI or "
                        "the local Gateway administration socket"
                    )
                if self.pairing_url_factory is None:
                    raise ValueError(
                        "web device pairing is disabled on the Gateway; set web_gateway.mode "
                        "to on-demand in the Gateway's active config.jsonc, then restart the "
                        "Gateway"
                    )
                if set(message) != {"type", "protocol", "request_id", "label"}:
                    raise ValueError("pair_device has invalid fields")
                label = message.get("label")
                if not isinstance(label, str):
                    raise ValueError("device label must be a string")
                pairing_result = self.pairing_url_factory(label)
                if inspect.isawaitable(pairing_result):
                    pairing_result = await pairing_result
                result = (
                    {"pairing_url": pairing_result}
                    if isinstance(pairing_result, str)
                    else dict(pairing_result)
                )
            elif message_type == "web_status":
                if not allow_admin:
                    raise ValueError("web device administration is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("web_status has invalid fields")
                if self.web_status_factory is not None:
                    result = dict(self.web_status_factory())
                else:
                    devices = self.device_list_factory() if self.device_list_factory else []
                    result = {
                        "enabled": self.web_origin is not None,
                        "origin": self.web_origin,
                        "device_count": len(devices),
                    }
            elif message_type == "diagnostics":
                if not allow_admin or self.diagnostic_list_factory is None:
                    raise ValueError("diagnostics are unavailable")
                if set(message) not in (
                    {"type", "protocol", "request_id"},
                    {"type", "protocol", "request_id", "category"},
                ):
                    raise ValueError("diagnostics has invalid fields")
                category = message.get("category")
                if category is not None and category not in {
                    "web",
                    "presentations",
                    "plugins",
                }:
                    raise ValueError("diagnostic category is invalid")
                result = {"entries": self.diagnostic_list_factory(category)}
            elif message_type == "clear_diagnostics":
                if not allow_admin or self.diagnostic_clear_factory is None:
                    raise ValueError("diagnostics are unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("clear_diagnostics has invalid fields")
                self.diagnostic_clear_factory()
                result = {"cleared": True}
            elif message_type == "list_devices":
                if not allow_admin or self.device_list_factory is None:
                    raise ValueError("web device administration is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("list_devices has invalid fields")
                result = {"devices": self.device_list_factory()}
            elif message_type == "list_uis":
                if not allow_admin or self.ui_store is None:
                    raise ValueError("UI inventory is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("list_uis has invalid fields")
                result = {"uis": [record.as_dict() for record in self.ui_store.records()]}
            elif message_type == "config_summary":
                if self.runtime.configuration_values is None:
                    raise ValueError("configuration inventory is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("config_summary has invalid fields")
                result = self.runtime.configuration_values.summary()
            elif message_type == "config_list":
                if self.runtime.configuration_values is None:
                    raise ValueError("configuration inventory is unavailable")
                if set(message) not in (
                    {"type", "protocol", "request_id"},
                    {"type", "protocol", "request_id", "filter"},
                ):
                    raise ValueError("config_list has invalid fields")
                filter_value = message.get("filter")
                if filter_value is not None and (
                    not isinstance(filter_value, str)
                    or not 1 <= len(filter_value) <= 64
                    or not filter_value.isprintable()
                ):
                    raise ValueError("configuration filter is invalid")
                settings = self.runtime.configuration_values.list_descriptors(filter_value)
                if filter_value is not None and not settings:
                    raise ValueError(f"unknown configuration category or scope: {filter_value}")
                result = {"settings": settings}
            elif message_type == "config_show":
                if self.runtime.configuration_values is None:
                    raise ValueError("configuration inventory is unavailable")
                if set(message) not in (
                    {"type", "protocol", "request_id", "key"},
                    {"type", "protocol", "request_id", "key", "ui"},
                ):
                    raise ValueError("config_show has invalid fields")
                key = message.get("key")
                if not isinstance(key, str) or not 1 <= len(key) <= 240 or not key.isprintable():
                    raise ValueError("configuration key is invalid")
                ui_name = message.get("ui")
                if ui_name is not None:
                    if not isinstance(ui_name, str):
                        raise ValueError("UI name must be a string")
                    if self.ui_store is None:
                        raise ValueError("UI inventory is unavailable")
                    ui_record = self.ui_store.find_by_name(ui_name)
                    if ui_record is None:
                        raise ValueError(f"unknown UI: {ui_name}")
                else:
                    ui_record = None
                result = self.runtime.configuration_values.show(key)
                if ui_record is not None:
                    store = getattr(self.runtime, "gateway_configuration", None)
                    if store is not None:
                        present, override = store.get(
                            key,
                            scope="ui",
                            target=str(ui_record.ui_id),
                        )
                        if not present:
                            present, override = store.get(
                                key,
                                scope="ui-type",
                                target=ui_record.client_type,
                            )
                        if present:
                            descriptor = store.registry.get(key)
                            result["value"] = (
                                None if descriptor and descriptor.sensitivity else override
                            )
                            result["source"] = "managed"
                            result["source_detail"] = (
                                f"Gateway configuration revision {store.revision}"
                            )
                            if descriptor and descriptor.sensitivity:
                                result["redacted"] = True
                    result["ui"] = {
                        "id": str(ui_record.ui_id),
                        "name": ui_record.display_name,
                    }
            elif message_type == "config_status":
                if self.runtime.configuration_values is None:
                    raise ValueError("configuration inventory is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("config_status has invalid fields")
                gateway_configuration = getattr(self.runtime, "gateway_configuration", None)
                revision = (
                    gateway_configuration.revision
                    if gateway_configuration is not None
                    else getattr(getattr(self.runtime, "managed_settings", None), "revision", 0)
                )
                records = self.ui_store.records() if self.ui_store is not None else ()
                uis = []
                for record in records:
                    applied = record.applied_config_revision
                    convergence = (
                        "unreported"
                        if applied is None
                        else "current"
                        if applied == revision
                        else "behind"
                        if applied < revision
                        else "ahead"
                    )
                    item: dict[str, object] = {
                        "ui_id": str(record.ui_id),
                        "display_name": record.display_name,
                        "client_type": record.client_type,
                        "applied_revision": applied,
                        "convergence": convergence,
                    }
                    report = (
                        self.ui_store.configuration_report(record.ui_id) if self.ui_store else None
                    )
                    if report is not None:
                        item["desired_revision"] = report[0]
                        item["application_status"] = report[1]
                        item["issues"] = list(report[2])
                    uis.append(item)
                result = {
                    "revision": revision,
                    "uis": uis,
                    "counts": {
                        state: sum(item["convergence"] == state for item in uis)
                        for state in ("current", "behind", "ahead", "unreported")
                    },
                }
            elif message_type in {"config_set", "config_reset"}:
                store = self.runtime.gateway_configuration
                if store is None or self.runtime.configuration_values is None:
                    raise ValueError("Gateway configuration mutation is unavailable")
                allowed = {
                    "type",
                    "protocol",
                    "request_id",
                    "key",
                    "scope",
                    "target",
                    "expected_revision",
                    "confirm",
                }
                if message_type == "config_set":
                    allowed.add("value")
                if set(message) != allowed:
                    raise ValueError(f"{message_type} has invalid fields")
                key = message.get("key")
                scope = message.get("scope")
                target = message.get("target")
                expected_revision = message.get("expected_revision")
                confirm = message.get("confirm")
                if not isinstance(key, str) or not isinstance(scope, str):
                    raise ValueError("configuration key and scope must be strings")
                if target is not None and not isinstance(target, str):
                    raise ValueError("configuration target must be a string or null")
                if (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 0
                ):
                    raise ValueError("expected_revision must be a non-negative integer")
                if not isinstance(confirm, bool):
                    raise ValueError("configuration confirmation must be a boolean")
                self_service = scope == "ui" and target is None and ui_id is not None
                if not configuration_admin and not self_service:
                    raise ValueError(
                        "configuration mutation requires owner/admin access; remote UIs may "
                        "change only their own UI-scoped settings"
                    )
                resolved_target = self._configuration_target(scope, target, ui_id=ui_id)
                value = message.get("value")
                preview = store.preview(
                    key,
                    value,
                    scope=scope,
                    target=resolved_target,
                    reset=message_type == "config_reset",
                )
                if not confirm:
                    result = preview
                else:
                    actor = self.ui_store.get(ui_id) if self.ui_store and ui_id else None
                    actor_id = str(ui_id) if ui_id is not None else "local-admin"
                    actor_name = actor.display_name if actor is not None else "Local administrator"
                    mutation = await asyncio.to_thread(
                        store.set if message_type == "config_set" else store.reset,
                        key,
                        **({"value": value} if message_type == "config_set" else {}),
                        scope=scope,
                        target=resolved_target,
                        expected_revision=expected_revision,
                        actor_ui_id=actor_id,
                        actor_ui_name=actor_name,
                    )
                    self._sync_gateway_configuration_value(mutation)
                    self._apply_live_gateway_configuration(mutation)
                    await self._broadcast_configuration()
                    if key in {
                        "ui.spellcheck.enabled",
                        "ui.speaking_streaks.enabled",
                    }:
                        await self._broadcast_ui_settings()
                    result = {
                        **mutation.as_dict(
                            redact=(store.registry.get(key).sensitivity)  # type: ignore[union-attr]
                        ),
                        "requires_confirmation": False,
                    }
            elif message_type == "config_batch":
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway configuration mutation is unavailable")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "changes",
                    "expected_revision",
                    "confirm",
                }:
                    raise ValueError("config_batch has invalid fields")
                changes = message.get("changes")
                expected_revision = message.get("expected_revision")
                confirm = message.get("confirm")
                if not isinstance(changes, list):
                    raise ValueError("configuration batch changes must be a list")
                if (
                    not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool)
                    or expected_revision < 0
                    or not isinstance(confirm, bool)
                ):
                    raise ValueError("configuration batch revision or confirmation is invalid")
                resolved_changes = []
                for raw in changes:
                    if not isinstance(raw, dict):
                        raise ValueError("configuration batch change must be an object")
                    change = dict(raw)
                    scope = change.get("scope")
                    target = change.get("target")
                    if not isinstance(scope, str):
                        raise ValueError("configuration batch scope must be a string")
                    if target is not None and not isinstance(target, str):
                        raise ValueError("configuration batch target must be a string or null")
                    self_service = scope == "ui" and target is None and ui_id is not None
                    if not configuration_admin and not self_service:
                        raise ValueError(
                            "configuration mutation requires owner/admin access; remote UIs may "
                            "change only their own UI-scoped settings"
                        )
                    change["target"] = self._configuration_target(scope, target, ui_id=ui_id)
                    resolved_changes.append(change)
                preview = store.preview_batch(resolved_changes)
                if not confirm:
                    result = preview
                else:
                    actor = self.ui_store.get(ui_id) if self.ui_store and ui_id else None
                    mutations = await asyncio.to_thread(
                        store.batch,
                        resolved_changes,
                        expected_revision=expected_revision,
                        actor_ui_id=str(ui_id) if ui_id is not None else "local-admin",
                        actor_ui_name=(
                            actor.display_name if actor is not None else "Local administrator"
                        ),
                    )
                    for mutation in mutations:
                        self._sync_gateway_configuration_value(mutation)
                        self._apply_live_gateway_configuration(mutation)
                    await self._broadcast_configuration()
                    if any(
                        mutation.key
                        in {"ui.spellcheck.enabled", "ui.speaking_streaks.enabled"}
                        for mutation in mutations
                    ):
                        await self._broadcast_ui_settings()
                    result = {
                        **preview,
                        "revision": store.revision,
                        "requires_confirmation": False,
                    }
            elif message_type == "config_changes":
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway configuration history is unavailable")
                if not configuration_admin:
                    raise ValueError("configuration history requires owner/admin access")
                if set(message) not in (
                    {"type", "protocol", "request_id"},
                    {"type", "protocol", "request_id", "limit"},
                ):
                    raise ValueError("config_changes has invalid fields")
                limit = message.get("limit", 50)
                if not isinstance(limit, int) or isinstance(limit, bool):
                    raise ValueError("configuration change limit must be an integer")
                result = {"revision": store.revision, "changes": store.changes(limit=limit)}
            elif message_type == "config_undo":
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway configuration undo is unavailable")
                if not configuration_admin:
                    raise ValueError("configuration undo requires owner/admin access")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "expected_revision",
                    "confirm",
                }:
                    raise ValueError("config_undo has invalid fields")
                expected_revision = message.get("expected_revision")
                confirm = message.get("confirm")
                if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
                    raise ValueError("expected_revision must be an integer")
                if confirm is not True:
                    result = {
                        "revision": store.revision,
                        "operation": "undo",
                        "requires_confirmation": True,
                    }
                else:
                    actor = self.ui_store.get(ui_id) if self.ui_store and ui_id else None
                    undone_mutations = store.mutations_at_revision(expected_revision)
                    mutation = await asyncio.to_thread(
                        store.undo,
                        expected_revision=expected_revision,
                        actor_ui_id=str(ui_id) if ui_id else "local-admin",
                        actor_ui_name=(actor.display_name if actor else "Local administrator"),
                    )
                    affected = undone_mutations if mutation.key == "batch" else (mutation,)
                    for affected_mutation in affected:
                        self._sync_gateway_configuration_value(affected_mutation)
                        self._apply_live_gateway_configuration(affected_mutation)
                    await self._broadcast_configuration()
                    if any(
                        item.key
                        in {"ui.spellcheck.enabled", "ui.speaking_streaks.enabled"}
                        for item in affected
                    ):
                        await self._broadcast_ui_settings()
                    descriptor = store.registry.get(mutation.key)
                    result = {
                        **mutation.as_dict(redact=descriptor.sensitivity if descriptor else True),
                        "requires_confirmation": False,
                    }
            elif message_type == "config_export":
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway configuration export is unavailable")
                if not configuration_admin:
                    raise ValueError("configuration export requires owner/admin access")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("config_export has invalid fields")
                result = {"document": store.export()}
            elif message_type == "config_migrate":
                migration = self.runtime.gateway_migration
                store = self.runtime.gateway_configuration
                if migration is None or store is None or self.ui_store is None or ui_id is None:
                    raise ValueError("configuration migration requires a registered UI")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "base_revision",
                    "values",
                    "source_files",
                }:
                    raise ValueError("config_migrate has invalid fields")
                base_revision = message.get("base_revision")
                values = message.get("values")
                source_files = message.get("source_files")
                if (
                    not isinstance(base_revision, int)
                    or isinstance(base_revision, bool)
                    or not isinstance(values, dict)
                    or not all(isinstance(key, str) for key in values)
                    or not isinstance(source_files, list)
                    or len(source_files) > 16
                    or any(
                        not isinstance(source, str) or not 1 <= len(source) <= 1_024
                        for source in source_files
                    )
                ):
                    raise ValueError("configuration migration proposal is invalid")
                record = self.ui_store.get(ui_id)
                if record is None:
                    raise ValueError("current UI identity is unavailable")
                settings = collect_migration_settings(
                    values,
                    store.registry,
                    ui_id=ui_id,
                    source=f"UI {record.display_name}",
                )
                omitted_keys = collect_omitted_migration_keys(values, store.registry)
                migration.submit(
                    new_proposal(
                        ui_id=ui_id,
                        ui_name=record.display_name,
                        base_revision=base_revision,
                        settings=settings,
                        source_files=tuple(source_files),
                        omitted_keys=omitted_keys,
                    )
                )
                result = migration.preview()
            elif message_type == "config_migration_preview":
                migration = self.runtime.gateway_migration
                if migration is None:
                    raise ValueError("configuration migration is unavailable")
                if set(message) != {"type", "protocol", "request_id", "choices"}:
                    raise ValueError("config_migration_preview has invalid fields")
                choices = message.get("choices")
                if not isinstance(choices, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in choices.items()
                ):
                    raise ValueError("migration choices must be strings")
                result = migration.preview(choices)
            elif message_type == "config_migration_apply":
                migration = self.runtime.gateway_migration
                if migration is None:
                    raise ValueError("configuration migration is unavailable")
                if not configuration_admin:
                    raise ValueError("applying migration requires owner/admin access")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "expected_revision",
                    "choices",
                    "confirm",
                }:
                    raise ValueError("config_migration_apply has invalid fields")
                if message.get("confirm") is not True:
                    raise ValueError("config_migration_apply requires explicit confirmation")
                expected = message.get("expected_revision")
                choices = message.get("choices")
                if (
                    not isinstance(expected, int)
                    or isinstance(expected, bool)
                    or not isinstance(choices, dict)
                    or not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in choices.items()
                    )
                ):
                    raise ValueError("configuration migration apply is invalid")
                actor = self.ui_store.get(ui_id) if self.ui_store and ui_id else None
                preview = migration.preview(choices)
                revision = await asyncio.to_thread(
                    migration.apply,
                    expected_revision=expected,
                    choices=choices,
                    actor_ui_id=str(ui_id) if ui_id else "local-admin",
                    actor_ui_name=actor.display_name if actor else "Local administrator",
                )
                await self._broadcast_configuration()
                result = {
                    "revision": revision,
                    "applied": True,
                    "restart": preview.get("restart", "live"),
                }
            elif message_type == "config_migration_status":
                migration = self.runtime.gateway_migration
                if migration is None:
                    raise ValueError("configuration migration is unavailable")
                if set(message) != {"type", "protocol", "request_id"}:
                    raise ValueError("config_migration_status has invalid fields")
                result = migration.status()
            elif message_type == "config_migration_rollback":
                migration = self.runtime.gateway_migration
                if migration is None:
                    raise ValueError("configuration migration is unavailable")
                if not configuration_admin:
                    raise ValueError("migration rollback requires owner/admin access")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "expected_revision",
                    "confirm",
                }:
                    raise ValueError("config_migration_rollback has invalid fields")
                if message.get("confirm") is not True:
                    raise ValueError("migration rollback requires explicit confirmation")
                expected = message.get("expected_revision")
                if not isinstance(expected, int) or isinstance(expected, bool):
                    raise ValueError("migration rollback revision is invalid")
                actor = self.ui_store.get(ui_id) if self.ui_store and ui_id else None
                revision = await asyncio.to_thread(
                    migration.rollback,
                    expected_revision=expected,
                    actor_ui_id=str(ui_id) if ui_id else "local-admin",
                    actor_ui_name=actor.display_name if actor else "Local administrator",
                )
                await self._broadcast_configuration()
                result = {"revision": revision, "rolled_back": True}
            elif message_type in {"plugins_catalog", "plugin_show"}:
                catalog = self.runtime.plugins.catalog()
                if message_type == "plugins_catalog":
                    if set(message) not in (
                        {"type", "protocol", "request_id"},
                        {"type", "protocol", "request_id", "state"},
                    ):
                        raise ValueError("plugins_catalog has invalid fields")
                    state = message.get("state")
                    if state not in {None, "available", "installed", "enabled", "failed"}:
                        raise ValueError("plugin catalog state is invalid")
                    if state is not None:
                        catalog = [item for item in catalog if item[state] is True]
                    result = {"plugins": catalog}
                else:
                    if set(message) != {"type", "protocol", "request_id", "name"}:
                        raise ValueError("plugin_show has invalid fields")
                    name = message.get("name")
                    if not isinstance(name, str):
                        raise ValueError("plugin name must be a string")
                    plugin = next((item for item in catalog if item["name"] == name), None)
                    if plugin is None:
                        raise ValueError(f"plugin {name!r} is unavailable or not installed")
                    compatible = []
                    if self.ui_store is not None:
                        for record in self.ui_store.records():
                            supported = (
                                plugin["portable"] is True and record.client_type == "pwa"
                            ) or f"plugin:{name}" in record.capabilities
                            compatible.append(
                                {
                                    "ui_id": str(record.ui_id),
                                    "ui_name": record.display_name,
                                    "compatible": supported,
                                    "reason": None
                                    if supported
                                    else "plugin capability not reported",
                                }
                            )
                    result = {"plugin": plugin, "clients": compatible}
            elif message_type in {"plugin_enable", "plugin_disable"}:
                store = self.runtime.gateway_configuration
                if store is None:
                    raise ValueError("Gateway plugin configuration is unavailable")
                if not configuration_admin:
                    raise ValueError("plugin enablement requires owner/admin access")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "name",
                    "expected_revision",
                    "confirm",
                }:
                    raise ValueError(f"{message_type} has invalid fields")
                name = message.get("name")
                expected = message.get("expected_revision")
                if not isinstance(name, str) or not isinstance(expected, int):
                    raise ValueError("plugin name and revision are invalid")
                catalog = self.runtime.plugins.catalog()
                known = any(item["name"] == name and item["available"] for item in catalog)
                if not known:
                    raise ValueError(f"plugin {name!r} is unavailable or not installed")
                present, configured = store.get("plugins.enabled", scope="shared")
                enabled = (
                    list(configured)
                    if present and isinstance(configured, list)
                    else list(self.runtime.plugins.requested_plugins)
                )
                desired = message_type == "plugin_enable"
                updated = (
                    list(dict.fromkeys([*enabled, name]))
                    if desired
                    else [item for item in enabled if item != name]
                )
                if message.get("confirm") is not True:
                    result = {
                        "name": name,
                        "enabled": desired,
                        "revision": store.revision,
                        "requires_confirmation": True,
                        "restart": "client-reload",
                    }
                else:
                    mutation = await asyncio.to_thread(
                        store.set,
                        "plugins.enabled",
                        updated,
                        scope="shared",
                        target=None,
                        expected_revision=expected,
                        actor_ui_id=str(ui_id) if ui_id else "local-admin",
                        actor_ui_name=(
                            self.ui_store.get(ui_id).display_name
                            if self.ui_store and ui_id and self.ui_store.get(ui_id)
                            else "Local administrator"
                        ),
                    )
                    self._sync_gateway_configuration_value(mutation)
                    await self._broadcast_configuration()
                    result = {
                        "name": name,
                        "enabled": desired,
                        "revision": mutation.revision,
                        "requires_confirmation": False,
                        "restart": mutation.restart,
                    }
            elif message_type == "show_ui":
                if not allow_admin or self.ui_store is None:
                    raise ValueError("UI inventory is unavailable")
                if set(message) != {"type", "protocol", "request_id", "name"}:
                    raise ValueError("show_ui has invalid fields")
                name = message.get("name")
                if not isinstance(name, str):
                    raise ValueError("UI name must be a string")
                record = self.ui_store.find_by_name(name)
                if record is None:
                    raise ValueError(f"unknown UI: {name}")
                result = {"ui": record.as_dict()}
            elif message_type == "rename_ui":
                if self.ui_store is None or ui_id is None:
                    raise ValueError("current UI identity is unavailable")
                if set(message) != {"type", "protocol", "request_id", "name"}:
                    raise ValueError("rename_ui has invalid fields")
                name = message.get("name")
                if not isinstance(name, str):
                    raise ValueError("UI name must be a string")
                result = {"ui": (await self.ui_store.rename(ui_id, name)).as_dict()}
            elif message_type == "rename_ui_target":
                if not allow_admin or self.ui_store is None:
                    raise ValueError("UI administration is unavailable")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "target",
                    "name",
                }:
                    raise ValueError("rename_ui_target has invalid fields")
                target = message.get("target")
                name = message.get("name")
                if not isinstance(target, str) or not isinstance(name, str):
                    raise ValueError("UI target and name must be strings")
                result = {"ui": (await self.ui_store.rename_named(target, name)).as_dict()}
            elif message_type == "forget_ui":
                if not allow_admin or self.ui_store is None:
                    raise ValueError("UI administration is unavailable")
                if set(message) != {
                    "type",
                    "protocol",
                    "request_id",
                    "target",
                    "confirm",
                }:
                    raise ValueError("forget_ui has invalid fields")
                target = message.get("target")
                if not isinstance(target, str):
                    raise ValueError("UI target must be a string")
                if message.get("confirm") is not True:
                    raise ValueError("forget_ui requires explicit confirmation")
                result = {"ui": (await self.ui_store.forget_named(target)).as_dict()}
            elif message_type == "revoke_device":
                if not allow_admin or self.device_revoke_factory is None:
                    raise ValueError("web device administration is unavailable")
                if set(message) != {"type", "protocol", "request_id", "device_id"}:
                    raise ValueError("revoke_device has invalid fields")
                try:
                    device_id = UUID(str(message.get("device_id")))
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("device_id must be a UUID") from None
                if not await self.device_revoke_factory(device_id):
                    raise ValueError(f"unknown web device: {device_id}")
                result = {"device_id": str(device_id)}
            else:
                raise ValueError(f"unsupported request type: {message_type}")
        except (TypeError, UnknownSessionError, RuntimeError, ValueError) as exc:
            await write_message(
                writer,
                {
                    "type": "ack",
                    "request_id": str(request_id),
                    "ok": False,
                    "error": str(exc),
                },
                lock=write_lock,
            )
            return
        acknowledgement: dict[str, Any] = {
            "type": "ack",
            "request_id": str(request_id),
            "ok": True,
        }
        if result is not None:
            acknowledgement["result"] = result
        await write_message(writer, acknowledgement, lock=write_lock)
        if start_update:
            self._update_task = asyncio.create_task(
                self._coordinate_update(), name="tfr-gateway-coordinated-update"
            )

    async def _broadcast_ui_settings(self) -> None:
        settings = self.runtime.shared_ui_settings()
        message = {"type": "ui_settings", **settings}
        async with self._update_participants_lock:
            participants = tuple(self._update_participants.values())
        await asyncio.gather(
            *(
                write_message(participant.writer, message, lock=participant.write_lock)
                for participant in participants
            ),
            return_exceptions=True,
        )
        if self.ui_settings_notice is not None:
            with contextlib.suppress(Exception):
                await self.ui_settings_notice(settings)

    async def _broadcast_configuration(self) -> None:
        values = self.runtime.configuration_values
        store = self.runtime.gateway_configuration
        if values is None or store is None:
            return
        async with self._update_participants_lock:
            participants = tuple(self._update_participants.values())
        messages = []
        for participant in participants:
            if not participant.configuration_sync or participant.ui_id is None:
                continue
            record = self.ui_store.get(participant.ui_id) if self.ui_store is not None else None
            if record is None:
                continue
            payload = effective_client_configuration(
                values,
                store,
                client_type=record.client_type,
                ui_id=record.ui_id,
            ).as_dict()
            messages.append(
                write_message(
                    participant.writer,
                    {"type": "configuration", **payload},
                    lock=participant.write_lock,
                )
            )
        await asyncio.gather(*messages, return_exceptions=True)
        if self.configuration_notice is not None:
            with contextlib.suppress(Exception):
                await self.configuration_notice()

    def _configuration_target(
        self, scope: str, target: str | None, *, ui_id: UUID | None
    ) -> str | None:
        if scope != "ui":
            return target
        if target is None:
            if ui_id is None:
                raise ValueError("UI scope requires a registered current UI or --ui target")
            return str(ui_id)
        if self.ui_store is None:
            raise ValueError("UI inventory is unavailable")
        record = self.ui_store.find_by_name(target)
        if record is None:
            raise ValueError(f"unknown UI: {target}")
        return str(record.ui_id)

    def _sync_gateway_configuration_value(self, mutation: ConfigurationMutation) -> None:
        values = self.runtime.configuration_values
        store = self.runtime.gateway_configuration
        if values is None or store is None:
            return
        if mutation.scope in {"ui", "ui-type"}:
            return
        present, value = store.get(
            mutation.key,
            scope=mutation.scope,
            target=mutation.target,
        )
        if present:
            values.record(
                mutation.key,
                value,
                SettingProvenance.MANAGED,
                source_detail=f"Gateway configuration revision {store.revision}",
            )
        else:
            values.clear(mutation.key, SettingProvenance.MANAGED)

    def _apply_live_gateway_configuration(self, mutation: ConfigurationMutation) -> None:
        if (
            mutation.key.endswith(".gag.patterns")
            and mutation.scope == "world"
            and mutation.target is not None
            and self.runtime.managed_settings is not None
            and self.runtime.gag_policy is not None
            and self.runtime.gateway_configuration is not None
        ):
            worlds = self.runtime.managed_settings.effective_worlds.model_dump(mode="python")
            present, patterns = self.runtime.gateway_configuration.get(
                mutation.key,
                scope="world",
                target=mutation.target,
            )
            if present:
                worlds["worlds"][mutation.target]["gag"] = {"patterns": patterns}
            self.runtime.gag_policy.replace_configuration(WorldsConfig.model_validate(worlds))

    async def _stage_gateway_update(self, *, force: bool = False) -> StagedManagedUpdate | None:
        async with self._update_lock:
            if self._staged_update is not None or (
                self._update_task is not None and not self._update_task.done()
            ):
                raise ValueError("a coordinated update is already in progress")
            self._update_gateway_restart = False
            self._update_force = force
            if self.runtime.update_checker is None:
                raise ValueError("TFR updates are unavailable")
            channel = self.runtime.update_checker.config.channel
            if force and self.runtime.update_checker.config.channel != "candidate":
                raise ValueError("forced updates are available only on the candidate channel")
            async with self._update_participants_lock:
                self._accepting_update_participants = False
                participants = tuple(self._update_participants.values())
                self._update_transaction_participants = participants
            incompatible = [
                participant for participant in participants if participant.build is None
            ]
            if incompatible:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise ValueError(
                    "all connected native UIs must report their build; "
                    f"cannot evaluate {self._participant_names(incompatible)}"
                )
            try:
                update_result = await self.runtime.update_checker.check()
                if update_result.error is not None:
                    raise ValueError(f"cannot check {channel} TFR updates: {update_result.error}")
                manifest = update_result.manifest
                if manifest is None:
                    raise ValueError(f"{channel} TFR update check returned no release manifest")
                gateway_build = getattr(self.runtime, "build", current_build())
                gateway_core_update = force or update_result.available_for(gateway_build)
                participant_core_updates = {
                    participant.connection_id: force
                    or update_result.available_for(participant.build)
                    for participant in participants
                    if participant.build is not None
                }
                current_participants = tuple(
                    participant
                    for participant in participants
                    if not participant_core_updates[participant.connection_id]
                )
                unsupported = [
                    participant
                    for participant in current_participants
                    if not participant.update_checks
                ]
                if unsupported:
                    raise ValueError(
                        "all current native UIs must support update availability checks; "
                        f"cannot check {self._participant_names(unsupported)}"
                    )
                plugin_checker = getattr(self.runtime, "plugin_update_checker", None)
                gateway_plugin_check: Awaitable[bool] | None = (
                    plugin_checker.stable_auto_update_available()
                    if plugin_checker is not None and not gateway_core_update
                    else None
                )
                participant_plugin_checks = tuple(
                    self._request_update_participant(
                        participant,
                        "update_check",
                        timeout=UPDATE_PREPARE_TIMEOUT_SECONDS,
                    )
                    for participant in current_participants
                )
                plugin_results = await asyncio.gather(
                    *(
                        ((gateway_plugin_check,) if gateway_plugin_check is not None else ())
                        + participant_plugin_checks
                    )
                )
                result_offset = 1 if gateway_plugin_check is not None else 0
                gateway_plugin_update = (
                    bool(plugin_results[0]) if gateway_plugin_check is not None else False
                )
                participant_plugin_updates = {
                    participant.connection_id: (
                        isinstance(result, dict) and result.get("available") is True
                    )
                    for participant, result in zip(
                        current_participants,
                        plugin_results[result_offset:],
                        strict=True,
                    )
                }
                selected_participants = tuple(
                    participant
                    for participant in participants
                    if participant_core_updates[participant.connection_id]
                    or participant_plugin_updates.get(participant.connection_id, False)
                )
                unmanaged = [
                    participant
                    for participant in selected_participants
                    if not participant.managed_updates
                ]
                if unmanaged:
                    raise ValueError(
                        "all connected native UIs must support managed updates when they require "
                        "an update; "
                        f"cannot update {self._participant_names(unmanaged)}"
                    )
                update_gateway = gateway_core_update or gateway_plugin_update
                if not update_gateway and not selected_participants:
                    async with self._update_participants_lock:
                        self._accepting_update_participants = True
                        self._update_transaction_participants = ()
                    self._update_gateway_restart = False
                    self._update_force = False
                    return None
                try:
                    staged = await stage_managed_update(
                        self.runtime.update_checker.config,
                        expected_manifest=None if force else manifest,
                    )
                except InstallationError as exc:
                    raise ValueError(f"Gateway host update staging failed: {exc}") from exc
            except BaseException:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise
            incompatible = [
                participant
                for participant in selected_participants
                if participant.build is None or not staged.manifest.supports(participant.build)
            ]
            if incompatible:
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                raise ValueError(
                    "all connected native UIs must be protocol-compatible; "
                    f"cannot update {self._participant_names(incompatible)}"
                )
            self._staged_update = staged
            self._update_transaction_participants = selected_participants
            self._update_gateway_restart = update_gateway
            return staged

    async def _coordinate_update(self) -> None:
        staged = self._staged_update
        if staged is None:
            return
        participants = self._update_transaction_participants
        if self.update_notice is not None:
            await self.update_notice(
                f"Gateway update {staged.version} is being prepared for "
                f"{self._participant_names(participants)}. {staged.release_url}"
            )
        try:
            prepare_results = await asyncio.gather(
                *(
                    self._request_update_participant(
                        participant,
                        "update_prepare",
                        timeout=UPDATE_PREPARE_TIMEOUT_SECONDS,
                        manifest=(None if self._update_force else staged.manifest.as_dict()),
                        force=self._update_force,
                    )
                    for participant in participants
                )
            )
            mismatched = [
                participant
                for participant, result in zip(participants, prepare_results, strict=True)
                if not isinstance(result, dict)
                or result.get("version") != staged.version
                or result.get("commit") != staged.commit
            ]
            if mismatched:
                raise ValueError(
                    f"{self._participant_names(mismatched)} staged a different candidate release"
                )
        except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
            await self._broadcast_update_message(
                participants, "update_abort", error=f"Update aborted: {exc}"
            )
            if self.update_notice is not None:
                await self.update_notice(f"Gateway update aborted: {exc}")
            print(f"tfr: coordinated update aborted: {exc}", file=sys.stderr, flush=True)
            self._staged_update = None
            self._update_gateway_restart = False
            self._update_force = False
            async with self._update_participants_lock:
                self._accepting_update_participants = True
                self._update_transaction_participants = ()
            return

        commit_results = await asyncio.gather(
            *(
                self._request_update_participant(
                    participant,
                    "update_commit",
                    timeout=UPDATE_COMMIT_TIMEOUT_SECONDS,
                    release_id=staged.release_id,
                )
                for participant in participants
            ),
            return_exceptions=True,
        )
        for participant, result in zip(participants, commit_results, strict=True):
            if isinstance(result, BaseException):
                print(
                    f"tfr: {self._participant_name(participant)} update commit "
                    f"acknowledgement failed: {result}",
                    file=sys.stderr,
                    flush=True,
                )
        if self._update_gateway_restart:
            try:
                await asyncio.to_thread(activate_managed_update, staged.release_id)
            except (InstallationError, OSError) as exc:
                print(f"tfr: Gateway update activation failed: {exc}", file=sys.stderr, flush=True)
                if self.update_notice is not None:
                    await self.update_notice(f"Gateway update activation failed: {exc}")
                async with self._update_participants_lock:
                    self._accepting_update_participants = True
                    self._update_transaction_participants = ()
                return
            print(
                f"tfr: activated Gateway {staged.version}. {staged.release_url}",
                file=sys.stderr,
                flush=True,
            )
            self._update_restart.set()
            return

        self._staged_update = None
        self._update_gateway_restart = False
        self._update_force = False
        async with self._update_participants_lock:
            self._accepting_update_participants = True
            self._update_transaction_participants = ()

    async def _request_update_participant(
        self,
        participant: _GatewayUpdateParticipant,
        message_type: str,
        *,
        timeout: float,
        **values: Any,
    ) -> dict[str, Any] | None:
        request_id = uuid4()
        future: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        participant.pending[request_id] = future
        try:
            await write_message(
                participant.writer,
                {"type": message_type, "request_id": str(request_id), **values},
                lock=participant.write_lock,
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            participant.pending.pop(request_id, None)

    def _participant_name(self, participant: _GatewayUpdateParticipant) -> str:
        record = (
            self.ui_store.get(participant.ui_id)
            if self.ui_store is not None and participant.ui_id is not None
            else None
        )
        return (
            record.display_name
            if record is not None
            else f"unregistered UI {participant.connection_id[-8:]}"
        )

    def _participant_names(self, participants: Sequence[_GatewayUpdateParticipant]) -> str:
        return (
            ", ".join(self._participant_name(participant) for participant in participants)
            or "no UIs"
        )

    def _handle_update_ack(
        self, connection_id: str, request_id: UUID, message: Mapping[str, Any]
    ) -> None:
        participant = self._update_participants.get(connection_id)
        future = participant.pending.get(request_id) if participant is not None else None
        if future is None or future.done():
            return
        if message.get("ok") is not True:
            future.set_exception(ValueError(str(message.get("error", "UI update failed"))))
            return
        result = message.get("result")
        if result is not None and not isinstance(result, dict):
            future.set_exception(ValueError("UI update result must be an object"))
            return
        future.set_result(result)

    async def _broadcast_update_message(
        self,
        participants: tuple[_GatewayUpdateParticipant, ...],
        message_type: str,
        **values: Any,
    ) -> None:
        await asyncio.gather(
            *(
                write_message(
                    participant.writer,
                    {"type": message_type, **values},
                    lock=participant.write_lock,
                )
                for participant in participants
            ),
            return_exceptions=True,
        )

    @staticmethod
    def _client_id(value: Any) -> str:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            raise GatewayProtocolError("client_id must be a UUID") from None

    @staticmethod
    def _optional_uuid(value: Any, name: str) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"{name} must be a UUID or null") from None

    @staticmethod
    def _world(message: Mapping[str, Any]) -> str:
        world = message.get("world")
        if not isinstance(world, str) or not world:
            raise ValueError("world must be a non-empty string")
        return world


def _stage_legacy_world_login_references(
    bundle: ConfigurationBundle,
    state_directory: Path,
    *,
    only_keys: set[str] | None = None,
) -> dict[str, str]:
    directory = state_directory / "migration-secrets"
    references: dict[str, str] = {}
    for name, world in bundle.worlds.worlds.items():
        key = f"worlds.{name}.login.password"
        if world.login is None or (only_keys is not None and key not in only_keys):
            continue
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        identifier = hashlib.sha256(name.encode()).hexdigest()
        destination = directory / f"{identifier}.password"
        descriptor, temporary = tempfile.mkstemp(prefix=".password.", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(world.login.password.get_secret_value() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        finally:
            Path(temporary).unlink(missing_ok=True)
        references[key] = f"file:{destination}"
    return references


async def run_gateway(
    bundle: ConfigurationBundle,
    path: Path | str | None = None,
    *,
    listen_host: str | None = None,
    listen_port: int = DEFAULT_GATEWAY_PORT,
    token_file: Path | str | None = None,
    tls_certificate: Path | str | None = None,
    tls_private_key: Path | str | None = None,
) -> int:
    migration_source = bundle if bundle.worlds.worlds or bundle.agents.agents else None
    if migration_source is None and bundle.main_path.is_file():
        try:
            migration_source = load_configuration(bundle.main_path)
        except ConfigurationError:
            # A canonical bootstrap intentionally has no worlds/agents files.
            migration_source = None
    bootstrap = GatewayBootstrapConfiguration(
        main_path=bundle.main_path,
        state_directory=bundle.main.managed_settings.state_directory,
        warnings=bundle.warnings,
    )
    bundle = gateway_bootstrap_bundle(bootstrap)
    startup_store = GatewayConfigurationStore(
        bundle.main.managed_settings.state_directory,
        core_configuration_registry(),
        defer_plugin_validation=True,
    )
    startup_document = startup_store.document
    if migration_source is not None and not any(
        section for _scope, _target, section in startup_store._sections(startup_document)
    ):
        # Phase-9 compatibility performs a one-time import, not a legacy
        # precedence overlay. From this commit onward the managed document is
        # the only source used to construct runtime state.
        startup_values = configuration_values_from_bundle(
            migration_source, startup_store.registry
        ).jsonc_proposal_values()
        # Credential-bearing aggregates cannot be imported partially after
        # plaintext omission. Keep worlds usable without login, and require
        # explicit protected references before providers/agents are enabled.
        startup_values = {
            key: value
            for key, value in startup_values.items()
            if not key.endswith(".login.password")
            and not key.startswith("providers.")
            and not key.startswith("agents.")
        }
        login_references = _stage_legacy_world_login_references(
            migration_source,
            bundle.main.managed_settings.state_directory,
        )
        migration = GatewayMigrationStore(
            bundle.main.managed_settings.state_directory, startup_store
        )
        migration.submit(
            new_proposal(
                ui_id=UUID(int=0),
                ui_name="Gateway legacy configuration",
                base_revision=startup_store.revision,
                settings=collect_migration_settings(
                    startup_values,
                    startup_store.registry,
                    ui_id=UUID(int=0),
                    source="Gateway JSONC one-time import",
                    ui_scope="ui-type",
                    ui_target="terminal",
                ),
                source_files=tuple(
                    str(path)
                    for path in (
                        migration_source.main_path,
                        migration_source.worlds_path,
                        migration_source.agents_path,
                    )
                    if path.is_file()
                ),
            )
        )
        migration.apply(
            expected_revision=startup_store.revision,
            choices={},
            actor_ui_id="migration",
            actor_ui_name="Legacy one-time import",
            secret_references=login_references,
        )
    bundle = apply_gateway_configuration(bundle, startup_store.document)
    gateway_path = Path(path).expanduser() if path is not None else default_gateway_socket()
    connector_path = gateway_path.with_name(f"{gateway_path.name}.worlds")
    connector_client = await connect_or_spawn_connector(
        connector_path,
        connector_worlds(bundle),
    )
    connector_bridge = ConnectorSessionBridge(connector_client)
    try:
        runtime = await GatewayRuntime.from_configuration(
            bundle,
            connector_bridge=connector_bridge,
            migration_source_bundle=migration_source,
            legacy_precedence=False,
        )
    except BaseException:
        await connector_client.close()
        raise
    if runtime.configuration_bundle is not None:
        bundle = runtime.configuration_bundle
    tcp_auth_token: str | None = None
    tcp_ssl_context: ssl.SSLContext | None = None
    if listen_host is not None:
        validate_tcp_endpoint(listen_host, listen_port)
        if token_file is None or tls_certificate is None or tls_private_key is None:
            raise ValueError("network gateway requires --token-file, --tls-cert, and --tls-key")
        tcp_auth_token = load_gateway_token(token_file)
        tcp_ssl_context = create_gateway_server_tls_context(tls_certificate, tls_private_key)
    ui_store = UiStore(bundle.main.managed_settings.state_directory)
    web_manager = None
    if bundle.main.web_gateway.mode != "disabled":
        from tfr.gateway_web import WebGatewayManager

        web_config = bundle.main.web_gateway
        web_transport = web_config.transport
        web_ssl_context: ssl.SSLContext | None = None
        web_host = web_config.listen_host
        web_port = web_config.listen_port
        if web_transport == "direct":
            # Resolve the TLS key pair the PWA listener presents. Prefer the
            # web_gateway-specific certificate, otherwise reuse the Gateway's
            # own --tls-cert/--tls-key so an operator already running a network
            # Gateway does not have to provision a second certificate.
            certificate = web_config.tls_cert or tls_certificate
            private_key = web_config.tls_key or tls_private_key
            if certificate is None or private_key is None:
                raise ValueError(
                    "web_gateway.transport 'direct' requires web_gateway.tls_cert and "
                    "web_gateway.tls_key, or the Gateway's --tls-cert and --tls-key"
                )
            web_ssl_context = create_gateway_server_tls_context(certificate, private_key)
            direct_bind = web_config.direct_bind
            assert direct_bind is not None  # guaranteed by direct-mode validation
            web_host, web_port = direct_bind

        web_manager = WebGatewayManager(
            runtime,
            configured_origin=web_config.canonical_origin,
            host=web_host,
            port=web_port,
            state_directory=web_config.state_directory,
            snapshot_events=web_config.snapshot_events,
            speaking_streaks_enabled=bundle.main.ui.speaking_streaks.enabled,
            ui_store=ui_store,
            transport=web_transport,
            ssl_context=web_ssl_context,
        )
    server = GatewayServer(
        runtime,
        gateway_path,
        tcp_host=listen_host,
        tcp_port=listen_port,
        tcp_auth_token=tcp_auth_token,
        tcp_ssl_context=tcp_ssl_context,
        pairing_url_factory=(web_manager.pair_device if web_manager is not None else None),
        device_list_factory=(web_manager.device_descriptors if web_manager is not None else None),
        device_revoke_factory=(web_manager.revoke_device if web_manager is not None else None),
        web_origin=(web_manager.origin if web_manager is not None else None),
        web_status_factory=(web_manager.status if web_manager is not None else None),
        diagnostic_list_factory=(
            web_manager.diagnostic_entries if web_manager is not None else None
        ),
        diagnostic_clear_factory=(
            web_manager.clear_diagnostics if web_manager is not None else None
        ),
        update_notice=(web_manager.notify_update if web_manager is not None else None),
        ui_settings_notice=(web_manager.notify_ui_settings if web_manager is not None else None),
        configuration_notice=(
            web_manager.notify_configuration if web_manager is not None else None
        ),
        ui_store=ui_store,
    )
    stop = asyncio.Event()
    application_restart = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    restart_requested = False
    try:
        await server.start(start_serving=False)
        await runtime.start()
        if web_manager is not None:
            await web_manager.start_existing(eager=bundle.main.web_gateway.enabled)
        await server.start_serving()
        print(f"TFR Gateway listening on {server.path}", flush=True)
        for address in server.tcp_addresses:
            print(f"TFR Gateway listening with TLS on {address[0]}:{address[1]}", flush=True)
        if web_manager is not None and web_manager.active:
            print(
                "TFR Web Gateway listening on "
                f"http://{web_manager.host}:{web_manager.port} for {web_manager.origin}",
                flush=True,
            )
        print("Press Ctrl-C to stop the gateway.", flush=True)
        for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(handled_signal, stop.set)
                installed_signals.append(handled_signal)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        if hasattr(signal, "SIGUSR1"):
            try:
                loop.add_signal_handler(signal.SIGUSR1, application_restart.set)
                installed_signals.append(signal.SIGUSR1)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        signal_stop = asyncio.create_task(stop.wait(), name="tfr-gateway-signal-stop")
        update_stop = asyncio.create_task(
            server.wait_update_restart(), name="tfr-gateway-update-stop"
        )
        connector_stop = asyncio.create_task(
            connector_bridge.wait_closed(), name="tfr-gateway-connector-stop"
        )
        application_restart_stop = asyncio.create_task(
            application_restart.wait(), name="tfr-gateway-application-restart-stop"
        )
        done, pending = await asyncio.wait(
            {signal_stop, update_stop, connector_stop, application_restart_stop},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
        restart_requested = update_stop in done
    finally:
        for handled_signal in installed_signals:
            loop.remove_signal_handler(handled_signal)
        if web_manager is not None:
            await web_manager.stop()
        await server.stop()
        await runtime.stop(preserve_worlds=restart_requested or application_restart.is_set())
    if application_restart.is_set():
        return GATEWAY_APPLICATION_RESTART_EXIT
    if restart_requested:
        restart = managed_restart_command(sys.argv[1:])
        if restart is None:
            raise RuntimeError("activated Gateway release cannot be restarted")
        os.execv(restart[0], restart)
    return 0
