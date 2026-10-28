from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import ssl
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from tfr.agents import AgentInspection
from tfr.config import (
    GatewayReconnectConfig,
    MainConfig,
    UiConfiguration,
    UpdateConfig,
    WorldCapabilitiesConfig,
    WorldConfig,
    WorldDefaults,
)
from tfr.configuration_registry import SettingDescriptor
from tfr.configuration_sync import CONFIGURATION_SYNC_CAPABILITY, required_action
from tfr.core import EventBus
from tfr.core_settings import core_configuration_registry
from tfr.events import CommandRequest, Event
from tfr.gateway import default_gateway_socket
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SNAPSHOT_EVENTS,
    GatewayProtocolError,
    event_from_message,
    read_message,
    write_message,
)
from tfr.gateway_transport import (
    DEFAULT_GATEWAY_PORT,
    create_gateway_client_tls_context,
    load_gateway_token,
    validate_gateway_client_tls_context,
    validate_gateway_token,
    validate_tcp_endpoint,
)
from tfr.gateway_uis import UiRecord, UiRegistration
from tfr.installations import InstallationError, managed_restart_command
from tfr.managed_updates import (
    StagedManagedUpdate,
    activate_managed_update,
    stage_managed_update,
)
from tfr.plugin_sources import PluginUpdateChecker, load_plugin_sources
from tfr.plugins import (
    PluginLifecycleEvent,
    PluginManager,
    PluginWorldInfo,
    ProjectedEffectDemo,
)
from tfr.presentation import TUI_PRESENTATION_CAPABILITIES
from tfr.sessions import SessionManager, SessionState
from tfr.ui_identity import load_or_create_native_ui_id
from tfr.updates import (
    BuildIdentity,
    ReleaseManifest,
    UpdateChecker,
    UpdateError,
    current_build,
)

_RESTART_GATEWAY_ID = "TFR_RESTART_GATEWAY_ID"
_RESTART_CURSOR = "TFR_RESTART_CURSOR"
_RESTART_WORLD = "TFR_RESTART_WORLD"


_UPDATE_RESTART = "TFR_UPDATE_RESTART"


class GatewayDisconnectedError(ConnectionError):
    pass


def remote_speaker_effects_warning(
    plugins: PluginManager,
    *,
    gateway_loaded: bool | None,
    gateway_speakers: tuple[str, ...],
) -> str | None:
    rules = plugins.configured_speaker_effect_rules()
    if not rules:
        return None
    header = (
        "speaker_effects is configured in this remote UI's local config.jsonc, but in "
        "remote mode that configuration is Gateway-owned."
    )
    if gateway_loaded is None:
        return "\n\n".join(
            (
                header,
                "This Gateway cannot report the speaker effects configured in its config.jsonc. "
                "Check the Gateway's config.jsonc and move any missing speaker_effects settings "
                "there.\n\nThe speaker_effects configuration in this remote UI's local "
                "config.jsonc is ignored.",
            )
        )
    if not gateway_loaded:
        return "\n\n".join(
            (
                header,
                "speaker_effects is not enabled in the Gateway's config.jsonc.",
                "Move the following from this remote UI's config.jsonc to the Gateway's "
                'config.jsonc:\n  • The "speaker_effects" entry in plugins.enabled\n'
                "  • plugins.config.speaker_effects\n\nThe configuration in this remote "
                "UI's config.jsonc is ignored.",
            )
        )
    gateway_names = {speaker.casefold() for speaker in gateway_speakers}
    missing = tuple(
        (index, speaker) for index, speaker in rules if speaker.casefold() not in gateway_names
    )
    configured = tuple(
        dict.fromkeys(speaker for _index, speaker in rules if speaker.casefold() in gateway_names)
    )
    local_names = tuple(dict.fromkeys(speaker for _index, speaker in rules))
    if not missing:
        names = ", ".join(local_names)
        return "\n\n".join(
            (
                header,
                f"The Gateway's config.jsonc reports effects for {names}. Remove the local "
                "speaker_effects configuration from this remote UI's config.jsonc; it is "
                "ignored in remote mode.",
            )
        )
    move = "\n".join(
        f"  • plugins.config.speaker_effects.rules[{index}]: {speaker}"
        for index, speaker in missing
    )
    sections = [
        header,
        "Move these rules from this remote UI's config.jsonc to the Gateway's config.jsonc:\n"
        + move
        + "\n\nThe copies in this remote UI's config.jsonc are ignored.",
    ]
    if configured:
        present = "\n".join(f"  • {speaker}" for speaker in configured)
        sections.append(
            "Already configured in the Gateway's config.jsonc:\n"
            + present
            + "\n\nRemove the local configuration for these speakers from this remote UI's "
            "config.jsonc; it is ignored in remote mode."
        )
    return "\n\n".join(sections)


ReconnectFactory = Callable[[UUID, int], Awaitable["GatewayClient"]]
ConnectHandler = Callable[[], Awaitable[None]]
DisconnectHandler = Callable[[Exception], None]
UpdatePrepareHandler = Callable[[Mapping[str, Any] | None, bool], Awaitable[dict[str, Any]]]
UpdateCommitHandler = Callable[[str], Awaitable[None]]
UpdateCheckHandler = Callable[[], Awaitable[dict[str, Any]]]


class RemoteWorldSession:
    def __init__(
        self,
        client: GatewayClient,
        descriptor: Mapping[str, Any],
        *,
        show_nospoof_prefix: bool,
    ) -> None:
        self._client = client
        self.world = str(descriptor["world"])
        self.session_id = UUID(str(descriptor["session_id"]))
        self.state = SessionState(str(descriptor["state"]))
        connection_generation = descriptor.get("connection_generation", 0)
        if (
            not isinstance(connection_generation, int)
            or isinstance(connection_generation, bool)
            or connection_generation < 0
        ):
            raise ValueError("connection_generation must be a non-negative integer")
        self.connection_generation = connection_generation
        character_name = descriptor.get("character")
        if character_name is not None and (
            not isinstance(character_name, str) or not character_name.strip()
        ):
            raise ValueError("character must be a non-empty string or null")
        self.character_name = character_name
        self.config = WorldConfig(
            host="gateway.invalid",
            port=1,
            aliases=descriptor.get("aliases", ()),
            server=str(descriptor["server"]),
            encoding=str(descriptor["encoding"]),
            capabilities=self._capabilities(descriptor),
            reconnect=False,
            autoconnect=False,
        )
        self.defaults = WorldDefaults()
        configured_visibility = descriptor.get("show_nospoof_prefix", show_nospoof_prefix)
        if not isinstance(configured_visibility, bool):
            raise ValueError("show_nospoof_prefix must be a boolean")
        self.show_nospoof_prefix = configured_visibility
        scrollback_lines = descriptor.get("scrollback_lines", 20_000)
        if (
            not isinstance(scrollback_lines, int)
            or isinstance(scrollback_lines, bool)
            or scrollback_lines < 1
        ):
            raise ValueError("scrollback_lines must be a positive integer")
        self.scrollback_lines = scrollback_lines

    @staticmethod
    def _capabilities(descriptor: Mapping[str, Any]) -> WorldCapabilitiesConfig:
        value = descriptor.get("capabilities", {})
        if not isinstance(value, Mapping):
            raise ValueError("capabilities must be an object")
        unicode = value.get("unicode", False)
        if not isinstance(unicode, bool):
            raise ValueError("capabilities.unicode must be a boolean")
        return WorldCapabilitiesConfig(unicode=unicode)

    @property
    def encoding(self) -> str:
        assert self.config.encoding is not None
        return self.config.encoding

    async def start(self) -> None:
        await self._client.control(self.world, "connect")

    async def stop(self) -> None:
        await self._client.control(self.world, "disconnect")


class RemoteCommandBus:
    def __init__(self, client: GatewayClient) -> None:
        self.client = client

    async def submit(self, request: CommandRequest) -> None:
        await self.client.submit(request)


class RemoteAgentController:
    def __init__(
        self,
        session: RemoteWorldSession,
        descriptor: Mapping[str, Any],
    ) -> None:
        from types import SimpleNamespace

        self.name = str(descriptor["name"])
        self.session = session
        self.paused = bool(descriptor.get("paused", False))
        self.inspection = AgentInspection(state=str(descriptor.get("state", "unknown")))
        self.provider = SimpleNamespace(
            name=str(descriptor.get("provider", "unknown")),
            endpoint=str(descriptor.get("endpoint", "unknown")),
        )
        self.config = SimpleNamespace(model=str(descriptor.get("model", "unknown")))


class RemoteAgentRuntime:
    def __init__(self, client: GatewayClient, descriptors: list[Mapping[str, Any]]) -> None:
        self.client = client
        sessions = {session.world: session for session in client.sessions}
        self.controllers = {
            str(descriptor["name"]): RemoteAgentController(
                sessions[str(descriptor["world"])],
                descriptor,
            )
            for descriptor in descriptors
        }

    def for_world(self, world: str) -> RemoteAgentController | None:
        return next(
            (
                controller
                for controller in self.controllers.values()
                if controller.session.world == world
            ),
            None,
        )

    async def pause(self, name: str) -> bool:
        controller = self.controllers.get(name)
        if controller is None:
            return False
        await self.client.agent_control(name, "pause")
        controller.paused = True
        controller.inspection = AgentInspection(state="paused")
        return True

    async def resume(self, name: str) -> bool:
        controller = self.controllers.get(name)
        if controller is None:
            return False
        await self.client.agent_control(name, "resume")
        controller.paused = False
        controller.inspection = AgentInspection(state="idle")
        return True

    async def trigger(self, name: str) -> bool:
        if name not in self.controllers:
            return False
        await self.client.agent_control(name, "trigger")
        return True


class GatewayClient:
    def __init__(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_id: UUID,
        gateway_id: UUID,
        cursor: int,
        oldest_cursor: int,
        history_truncated: bool,
        history_reset: bool,
        event_bus: EventBus,
        sessions: list[RemoteWorldSession],
        initial_events: tuple[Event, ...],
        agent_worlds: set[str],
        spellcheck_enabled: bool = False,
        speaking_streaks_enabled: bool = True,
        effect_demos: tuple[ProjectedEffectDemo, ...] = (),
        gateway_speaker_effects_loaded: bool | None = None,
        gateway_speaker_effects: tuple[str, ...] = (),
        gateway_build: BuildIdentity | None = None,
        ui_record: UiRecord | None = None,
        configuration_revision: int | None = None,
        effective_configuration: dict[str, object] | None = None,
        configuration_requirements: dict[str, str] | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.client_id = client_id
        self.gateway_id = gateway_id
        self.cursor = cursor
        self.oldest_cursor = oldest_cursor
        self.history_truncated = history_truncated
        self.history_reset = history_reset
        self.event_bus = event_bus
        self.sessions = sessions
        self.initial_events = initial_events
        self.agent_worlds = agent_worlds
        self.spellcheck_enabled = spellcheck_enabled
        self.speaking_streaks_enabled = speaking_streaks_enabled
        self.effect_demos = effect_demos
        self.gateway_speaker_effects_loaded = gateway_speaker_effects_loaded
        self.gateway_speaker_effects = gateway_speaker_effects
        self.gateway_build = gateway_build
        self.ui_record = ui_record
        self.configuration_revision = configuration_revision
        self.effective_configuration = effective_configuration or {}
        self.configuration_requirements = configuration_requirements or {}
        self.command_bus = RemoteCommandBus(self)
        self.agents: RemoteAgentRuntime | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[UUID, asyncio.Future[dict[str, Any] | None]] = {}
        self._pump: asyncio.Task[None] | None = None
        self._reconnect_factory: ReconnectFactory | None = None
        self.connect_handler: ConnectHandler | None = None
        self.disconnect_handler: DisconnectHandler | None = None
        self.update_prepare_handler: UpdatePrepareHandler | None = None
        self.update_commit_handler: UpdateCommitHandler | None = None
        self.update_check_handler: UpdateCheckHandler | None = None
        self.update_restart_handler: Callable[[], None] | None = None
        self.update_abort_handler: Callable[[str], None] | None = None
        self._reconnect_lock = asyncio.Lock()
        self._gateway_request_tasks: set[asyncio.Task[None]] = set()
        self.ui_settings_handler: Callable[[bool, bool], None] | None = None
        self.configuration_handler: (
            Callable[[dict[str, object], dict[str, str]], tuple[str, tuple[str, ...]]] | None
        ) = None
        self._initial_configuration_ack_pending = configuration_revision is not None
        (
            self._initial_configuration_status,
            self._initial_configuration_issues,
        ) = self._configuration_application_result(())

    @classmethod
    async def connect(
        cls,
        path: Path | str,
        *,
        show_nospoof_prefix: bool = False,
        client_id: UUID | None = None,
        gateway_id: UUID | None = None,
        after_cursor: int | None = None,
        ui_registration: UiRegistration | None = None,
    ) -> GatewayClient:
        resolved_client_id = client_id or uuid4()
        socket_path = Path(path).expanduser()
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(socket_path),
                limit=MAX_MESSAGE_BYTES,
            )
        except FileNotFoundError:
            raise GatewayDisconnectedError(
                f"gateway socket not found: {socket_path}; start `tfr gateway` first"
            ) from None
        except ConnectionRefusedError:
            raise GatewayDisconnectedError(
                f"gateway is not accepting connections at {socket_path}"
            ) from None
        client = await cls._connect_stream(
            reader,
            writer,
            show_nospoof_prefix=show_nospoof_prefix,
            client_id=resolved_client_id,
            gateway_id=gateway_id,
            after_cursor=after_cursor,
            ui_registration=ui_registration,
        )

        async def reconnect_factory(gateway: UUID, cursor: int) -> GatewayClient:
            return await cls.connect(
                socket_path,
                show_nospoof_prefix=show_nospoof_prefix,
                client_id=resolved_client_id,
                gateway_id=gateway,
                after_cursor=cursor,
                ui_registration=ui_registration,
            )

        client._reconnect_factory = reconnect_factory
        return client

    @classmethod
    async def connect_tcp(
        cls,
        host: str,
        port: int,
        *,
        auth_token: str,
        tls_context: ssl.SSLContext,
        server_hostname: str | None = None,
        show_nospoof_prefix: bool = False,
        client_id: UUID | None = None,
        gateway_id: UUID | None = None,
        after_cursor: int | None = None,
        ui_registration: UiRegistration | None = None,
    ) -> GatewayClient:
        host, port = validate_tcp_endpoint(host, port)
        validate_gateway_token(auth_token)
        validate_gateway_client_tls_context(tls_context)
        resolved_client_id = client_id or uuid4()
        try:
            reader, writer = await asyncio.open_connection(
                host,
                port,
                ssl=tls_context,
                server_hostname=server_hostname or host,
                limit=MAX_MESSAGE_BYTES,
            )
        except (ConnectionError, OSError) as exc:
            raise GatewayDisconnectedError(
                f"cannot establish a verified gateway connection to {host}:{port}"
            ) from exc
        client = await cls._connect_stream(
            reader,
            writer,
            show_nospoof_prefix=show_nospoof_prefix,
            client_id=resolved_client_id,
            gateway_id=gateway_id,
            after_cursor=after_cursor,
            auth_token=auth_token,
            ui_registration=ui_registration,
        )

        async def reconnect_factory(gateway: UUID, cursor: int) -> GatewayClient:
            return await cls.connect_tcp(
                host,
                port,
                auth_token=auth_token,
                tls_context=tls_context,
                server_hostname=server_hostname,
                show_nospoof_prefix=show_nospoof_prefix,
                client_id=resolved_client_id,
                gateway_id=gateway,
                after_cursor=cursor,
                ui_registration=ui_registration,
            )

        client._reconnect_factory = reconnect_factory
        return client

    @classmethod
    async def _connect_stream(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        show_nospoof_prefix: bool,
        client_id: UUID,
        gateway_id: UUID | None,
        after_cursor: int | None,
        auth_token: str | None = None,
        ui_registration: UiRegistration | None = None,
    ) -> GatewayClient:
        try:
            hello_request: dict[str, Any] = {
                "type": "hello",
                "client_id": str(client_id),
                "gateway_id": str(gateway_id) if gateway_id is not None else None,
                "after_cursor": after_cursor,
                "build": current_build().as_dict(),
                "managed_updates": managed_restart_command(()) is not None,
                "update_checks": True,
            }
            if auth_token is not None:
                hello_request["auth_token"] = auth_token
            if ui_registration is not None:
                hello_request["ui"] = ui_registration.as_dict()
            await write_message(
                writer,
                hello_request,
            )
            hello = await asyncio.wait_for(read_message(reader), timeout=10)
            if hello is None:
                raise GatewayDisconnectedError("gateway disconnected during handshake")
            if hello.get("type") == "error":
                raise GatewayProtocolError(str(hello.get("message", "gateway rejected handshake")))
            if hello.get("type") != "hello":
                raise GatewayProtocolError("gateway did not send a hello response")
            gateway_uuid = UUID(str(hello["gateway_id"]))
            cursor = cls._non_negative_integer(hello.get("cursor"), "cursor")
            oldest_cursor = cls._non_negative_integer(hello.get("oldest_cursor"), "oldest_cursor")
            snapshot_count = cls._non_negative_integer(
                hello.get("snapshot_count"), "snapshot_count"
            )
            if snapshot_count > MAX_SNAPSHOT_EVENTS:
                raise GatewayProtocolError("gateway snapshot count exceeds the client limit")
            history_truncated = hello.get("history_truncated")
            history_reset = hello.get("history_reset")
            if not isinstance(history_truncated, bool) or not isinstance(history_reset, bool):
                raise GatewayProtocolError("gateway history flags must be booleans")
            worlds = hello.get("worlds")
            agents = hello.get("agents")
            ui_settings = hello.get(
                "ui_settings",
                {
                    "spellcheck_enabled": False,
                    "speaking_streaks_enabled": True,
                },
            )
            if not isinstance(worlds, list) or not worlds:
                raise GatewayProtocolError("gateway hello contains no worlds")
            if not isinstance(agents, list):
                raise GatewayProtocolError("gateway agents must be a list")
            if not isinstance(ui_settings, dict) or not all(
                isinstance(ui_settings.get(name), bool)
                for name in ("spellcheck_enabled", "speaking_streaks_enabled")
            ):
                raise GatewayProtocolError("gateway UI settings are invalid")
            effect_demos = cls._effect_demos(hello.get("effect_demos", []))
            speaker_effects_loaded, gateway_speaker_effects = cls._speaker_effects(
                hello.get("speaker_effects")
            )
            try:
                gateway_build = (
                    BuildIdentity.from_mapping(hello["build"]) if "build" in hello else None
                )
            except UpdateError as exc:
                raise GatewayProtocolError(f"invalid gateway build identity: {exc}") from exc
            ui_record: UiRecord | None = None
            if "ui" in hello:
                try:
                    ui_record = UiRecord.from_dict(hello["ui"])
                except ValueError as exc:
                    raise GatewayProtocolError(f"gateway UI record is invalid: {exc}") from exc
                if ui_record.ui_id != client_id:
                    raise GatewayProtocolError("gateway returned a different UI ID")
            configuration_revision, effective_configuration, configuration_requirements = (
                cls._configuration(hello.get("configuration"))
            )
            event_bus = EventBus()
            client = cls(
                reader=reader,
                writer=writer,
                client_id=client_id,
                gateway_id=gateway_uuid,
                cursor=cursor,
                oldest_cursor=oldest_cursor,
                history_truncated=history_truncated,
                history_reset=history_reset,
                event_bus=event_bus,
                sessions=[],
                initial_events=(),
                agent_worlds={
                    str(agent["world"])
                    for agent in agents
                    if isinstance(agent, dict) and "world" in agent
                },
                spellcheck_enabled=ui_settings["spellcheck_enabled"],
                speaking_streaks_enabled=ui_settings["speaking_streaks_enabled"],
                effect_demos=effect_demos,
                gateway_speaker_effects_loaded=speaker_effects_loaded,
                gateway_speaker_effects=gateway_speaker_effects,
                gateway_build=gateway_build,
                ui_record=ui_record,
                configuration_revision=configuration_revision,
                effective_configuration=effective_configuration,
                configuration_requirements=configuration_requirements,
            )
            try:
                client.sessions.extend(
                    RemoteWorldSession(
                        client,
                        descriptor,
                        show_nospoof_prefix=show_nospoof_prefix,
                    )
                    for descriptor in worlds
                    if isinstance(descriptor, dict)
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayProtocolError(f"invalid world descriptor: {exc}") from exc
            if len(client.sessions) != len(worlds):
                raise GatewayProtocolError("invalid world descriptor")
            try:
                client.agents = RemoteAgentRuntime(
                    client,
                    [descriptor for descriptor in agents if isinstance(descriptor, dict)],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayProtocolError(f"invalid agent descriptor: {exc}") from exc
            client.initial_events = await asyncio.wait_for(
                cls._read_snapshot(reader, snapshot_count, cursor),
                timeout=30,
            )
            return client
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    @staticmethod
    def _effect_demos(value: object) -> tuple[ProjectedEffectDemo, ...]:
        if not isinstance(value, list) or len(value) > 100:
            raise GatewayProtocolError("gateway effect demos must be a bounded list")
        output = []
        for descriptor in value:
            if not isinstance(descriptor, dict) or set(descriptor) != {
                "id",
                "label",
                "category",
                "samples",
                "worlds",
            }:
                raise GatewayProtocolError("gateway effect demo descriptor is invalid")
            demo_id = descriptor["id"]
            label = descriptor["label"]
            category = descriptor["category"]
            samples = descriptor["samples"]
            worlds = descriptor["worlds"]
            if (
                not isinstance(demo_id, str)
                or not 0 < len(demo_id) <= 100
                or not isinstance(label, str)
                or not 0 < len(label) <= 100
                or category not in {"speaker", "text", "border", "screen", "combo"}
                or not isinstance(samples, list)
                or not 1 <= len(samples) <= 10
                or not isinstance(worlds, list)
                or len(worlds) > 100
                or any(not isinstance(world, str) or not 0 < len(world) <= 100 for world in worlds)
            ):
                raise GatewayProtocolError("gateway effect demo descriptor is invalid")
            events = []
            for sample in samples:
                if not isinstance(sample, dict):
                    raise GatewayProtocolError("gateway effect demo sample is invalid")
                _cursor, event = event_from_message(sample)
                events.append(event)
            output.append(
                ProjectedEffectDemo(
                    id=demo_id,
                    label=label,
                    category=category,
                    samples=tuple(events),
                    worlds=tuple(worlds),
                )
            )
        return tuple(output)

    @staticmethod
    def _speaker_effects(value: object) -> tuple[bool | None, tuple[str, ...]]:
        if value is None:
            return None, ()
        if not isinstance(value, dict) or set(value) != {"loaded", "speakers"}:
            raise GatewayProtocolError("gateway speaker effects summary is invalid")
        loaded = value["loaded"]
        speakers = value["speakers"]
        if (
            not isinstance(loaded, bool)
            or not isinstance(speakers, list)
            or len(speakers) > 1_000
            or any(
                not isinstance(speaker, str) or not 0 < len(speaker) <= 100 for speaker in speakers
            )
        ):
            raise GatewayProtocolError("gateway speaker effects summary is invalid")
        return loaded, tuple(dict.fromkeys(speakers))

    def start(self) -> None:
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run(), name="tfr-gateway-client-events")
        if self._initial_configuration_ack_pending and self.configuration_revision is not None:
            self._initial_configuration_ack_pending = False
            self._spawn_gateway_request(
                self._acknowledge_configuration(
                    self.configuration_revision,
                    self._initial_configuration_status,
                    self._initial_configuration_issues,
                )
            )

    def _configuration_application_result(
        self, plugin_descriptors: Sequence[SettingDescriptor]
    ) -> tuple[str, tuple[str, ...]]:
        registry = core_configuration_registry()
        for descriptor in plugin_descriptors:
            registry.register(descriptor)
        issues = tuple(
            f"unsupported setting: {key}"
            for key in sorted(self.effective_configuration)
            if registry.get(key) is None
        )[:100]
        return ("unsupported", issues) if issues else ("applied", ())

    def prepare_initial_configuration_ack(
        self, plugin_descriptors: Sequence[SettingDescriptor]
    ) -> None:
        (
            self._initial_configuration_status,
            self._initial_configuration_issues,
        ) = self._configuration_application_result(plugin_descriptors)

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        self._fail_pending(GatewayDisconnectedError("gateway client stopped"))
        for task in self._gateway_request_tasks:
            task.cancel()
        if self._gateway_request_tasks:
            await asyncio.gather(*tuple(self._gateway_request_tasks), return_exceptions=True)
        self._gateway_request_tasks.clear()
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()

    async def reconnect(self) -> int:
        if self._reconnect_factory is None:
            raise GatewayDisconnectedError("gateway connection cannot be re-established")
        async with self._reconnect_lock:
            if self._pump is not None and not self._pump.done():
                self._pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump
            self._pump = None
            self._fail_pending(GatewayDisconnectedError("gateway connection interrupted"))
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

            replacement = await self._reconnect_factory(self.gateway_id, self.cursor)
            current_sessions = {session.world: session for session in self.sessions}
            replacement_sessions = {session.world: session for session in replacement.sessions}
            session_identity_changed = (
                current_sessions.keys() != replacement_sessions.keys()
                or any(
                    current_sessions[world].session_id != replacement_sessions[world].session_id
                    for world in current_sessions.keys() & replacement_sessions.keys()
                )
            )
            aliases_changed = any(
                current_sessions[world].config.aliases != replacement_sessions[world].config.aliases
                for world in current_sessions.keys() & replacement_sessions.keys()
            )
            current_agents = set(self.agents.controllers) if self.agents is not None else set()
            replacement_agents = (
                set(replacement.agents.controllers) if replacement.agents is not None else set()
            )
            if (
                replacement.history_reset
                or session_identity_changed
                or aliases_changed
                or current_agents != replacement_agents
            ):
                await replacement.stop()
                await replacement.event_bus.close()
                raise GatewayDisconnectedError(
                    "gateway state changed; use /reload to rebuild the UI"
                )

            for world, session in current_sessions.items():
                updated = replacement_sessions[world]
                session.state = updated.state
                session.connection_generation = updated.connection_generation
                session.config = updated.config
                session.defaults = updated.defaults
                session.scrollback_lines = updated.scrollback_lines
            if self.agents is not None and replacement.agents is not None:
                for name, controller in self.agents.controllers.items():
                    updated = replacement.agents.controllers[name]
                    controller.paused = updated.paused
                    controller.inspection = updated.inspection

            self.reader = replacement.reader
            self.writer = replacement.writer
            self.gateway_id = replacement.gateway_id
            self.cursor = replacement.cursor
            self.oldest_cursor = replacement.oldest_cursor
            self.history_truncated = replacement.history_truncated
            self.history_reset = False
            self.initial_events = replacement.initial_events
            self.agent_worlds = replacement.agent_worlds
            self.spellcheck_enabled = replacement.spellcheck_enabled
            self.speaking_streaks_enabled = replacement.speaking_streaks_enabled
            self.effect_demos = replacement.effect_demos
            self.gateway_speaker_effects_loaded = replacement.gateway_speaker_effects_loaded
            self.gateway_speaker_effects = replacement.gateway_speaker_effects
            self.gateway_build = replacement.gateway_build
            self._write_lock = asyncio.Lock()
            await replacement.event_bus.close()
            if self.connect_handler is not None:
                await self.connect_handler()
            for event in self.initial_events:
                self._update_session(event)
                await self.event_bus.publish(event)
            self.start()
            return len(self.initial_events)

    async def submit(self, request: CommandRequest) -> None:
        session = self._session_for(request.world)
        if request.session_id != session.session_id:
            raise ValueError("command request does not belong to the remote session")
        await self._request(
            {
                "type": "command",
                "request_id": str(request.request_id),
                "world": request.world,
                "text": request.text,
                "sensitive": request.sensitive,
                "actor": {"type": request.actor.type.value, "id": request.actor.id},
                "correlation_id": (
                    str(request.correlation_id) if request.correlation_id is not None else None
                ),
                "causation_id": (
                    str(request.causation_id) if request.causation_id is not None else None
                ),
                "expected_connection_generation": request.expected_connection_generation,
                "metadata": dict(request.metadata),
            },
            request.request_id,
        )

    async def control(self, world: str, action: str) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "control",
                "request_id": str(request_id),
                "world": world,
                "action": action,
            },
            request_id,
        )

    async def agent_control(self, name: str, action: str) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "agent_control",
                "request_id": str(request_id),
                "agent": name,
                "action": action,
            },
            request_id,
        )

    async def ping(self) -> None:
        request_id = uuid4()
        await self._request({"type": "ping", "request_id": str(request_id)}, request_id)

    async def update(self, *, force: bool = False) -> dict[str, Any]:
        request_id = uuid4()
        message: dict[str, Any] = {"type": "update", "request_id": str(request_id)}
        if force:
            message["force"] = True
        result = await self._request(message, request_id)
        if result is None:
            raise GatewayProtocolError("Gateway returned no update result")
        return result

    async def web_status(self) -> dict[str, Any]:
        request_id = uuid4()
        result = await self._request(
            {"type": "web_status", "request_id": str(request_id)}, request_id
        )
        if result is None:
            raise GatewayProtocolError("Gateway returned no web status")
        return result

    async def config_summary(self) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "config_summary", "request_id": str(request_id)}, request_id
        )
        if result is None:
            raise GatewayProtocolError("Gateway returned no configuration summary")
        counts = (
            result.get("setting_count"),
            result.get("template_count"),
            result.get("sensitive_count"),
            result.get("unset_required_count"),
        )
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in counts
            )
            or not isinstance(result.get("sources"), dict)
            or not isinstance(result.get("scopes"), dict)
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration summary")
        return result

    async def config_list(self, filter_value: str | None = None) -> list[dict[str, object]]:
        request_id = uuid4()
        message: dict[str, object] = {
            "type": "config_list",
            "request_id": str(request_id),
        }
        if filter_value is not None:
            message["filter"] = filter_value
        result = await self._request(message, request_id)
        settings = result.get("settings") if result is not None else None
        if not isinstance(settings, list) or not all(
            isinstance(setting, dict)
            and isinstance(setting.get("key"), str)
            and isinstance(setting.get("type"), str)
            and isinstance(setting.get("scope"), str)
            and isinstance(setting.get("category"), str)
            and isinstance(setting.get("sensitive"), bool)
            for setting in settings
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration list")
        if any(setting["sensitive"] and "default" in setting for setting in settings):
            raise GatewayProtocolError("Gateway exposed a sensitive configuration default")
        return settings

    async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
        request_id = uuid4()
        message: dict[str, object] = {
            "type": "config_show",
            "request_id": str(request_id),
            "key": key,
        }
        if ui is not None:
            message["ui"] = ui
        result = await self._request(message, request_id)
        if (
            result is None
            or result.get("key") != key
            or not isinstance(result.get("descriptor_key"), str)
            or not isinstance(result.get("type"), str)
            or not isinstance(result.get("scope"), str)
            or not isinstance(result.get("consumers"), list)
            or not isinstance(result.get("restart"), str)
            or not isinstance(result.get("sensitive"), bool)
            or not isinstance(result.get("source"), str)
            or not isinstance(result.get("source_detail"), str)
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration record")
        if result["sensitive"] is True and result.get("value") is not None:
            raise GatewayProtocolError("Gateway exposed a sensitive configuration value")
        if ui is not None:
            ui_value = result.get("ui")
            if (
                not isinstance(ui_value, dict)
                or not isinstance(ui_value.get("id"), str)
                or not isinstance(ui_value.get("name"), str)
            ):
                raise GatewayProtocolError("Gateway returned an invalid configuration UI context")
        return result

    async def config_status(self) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "config_status", "request_id": str(request_id)}, request_id
        )
        revision = result.get("revision") if result is not None else None
        uis = result.get("uis") if result is not None else None
        counts = result.get("counts") if result is not None else None
        states = {"current", "behind", "ahead", "unreported"}
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(uis, list)
            or not isinstance(counts, dict)
            or set(counts) != states
            or any(
                not isinstance(counts[state], int)
                or isinstance(counts[state], bool)
                or counts[state] < 0
                for state in states
            )
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("ui_id"), str)
                or not isinstance(item.get("display_name"), str)
                or item.get("client_type") not in {"terminal", "pwa"}
                or item.get("convergence") not in states
                or (
                    item.get("applied_revision") is not None
                    and (
                        not isinstance(item["applied_revision"], int)
                        or isinstance(item["applied_revision"], bool)
                        or item["applied_revision"] < 0
                    )
                )
                for item in uis
            )
            or sum(counts.values()) != len(uis)
            or any(
                counts[state] != sum(item.get("convergence") == state for item in uis)
                for state in states
            )
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration status")
        return result

    async def config_mutate(
        self,
        operation: str,
        key: str,
        *,
        value: object = None,
        scope: str,
        target: str | None,
        expected_revision: int,
        confirm: bool,
    ) -> dict[str, object]:
        if operation not in {"set", "reset"}:
            raise ValueError("configuration operation must be set or reset")
        request_id = uuid4()
        message: dict[str, object] = {
            "type": f"config_{operation}",
            "request_id": str(request_id),
            "key": key,
            "scope": scope,
            "target": target,
            "expected_revision": expected_revision,
            "confirm": confirm,
        }
        if operation == "set":
            message["value"] = value
        result = await self._request(message, request_id)
        if (
            result is None
            or not isinstance(result.get("revision"), int)
            or not isinstance(result.get("restart"), str)
            or not isinstance(result.get("requires_confirmation"), bool)
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration mutation")
        return result

    async def config_batch(
        self,
        changes: list[dict[str, object]],
        *,
        expected_revision: int,
        confirm: bool,
    ) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_batch",
                "request_id": str(request_id),
                "changes": changes,
                "expected_revision": expected_revision,
                "confirm": confirm,
            },
            request_id,
        )
        if (
            result is None
            or not isinstance(result.get("revision"), int)
            or not isinstance(result.get("restart"), str)
            or not isinstance(result.get("requires_confirmation"), bool)
            or not isinstance(result.get("changes"), list)
        ):
            raise GatewayProtocolError("Gateway returned an invalid configuration batch")
        return result

    async def config_changes(self, limit: int = 50) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "config_changes", "request_id": str(request_id), "limit": limit},
            request_id,
        )
        if (
            result is None
            or not isinstance(result.get("revision"), int)
            or not isinstance(result.get("changes"), list)
        ):
            raise GatewayProtocolError("Gateway returned invalid configuration history")
        return result

    async def config_undo(self, *, expected_revision: int, confirm: bool) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_undo",
                "request_id": str(request_id),
                "expected_revision": expected_revision,
                "confirm": confirm,
            },
            request_id,
        )
        if result is None or not isinstance(result.get("requires_confirmation"), bool):
            raise GatewayProtocolError("Gateway returned an invalid configuration undo")
        return result

    async def config_migrate(
        self,
        values: dict[str, object],
        source_files: tuple[str, ...],
    ) -> dict[str, object]:
        self._backup_migration_sources(source_files)
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_migrate",
                "request_id": str(request_id),
                "base_revision": self.configuration_revision or 0,
                "values": values,
                "source_files": list(source_files),
            },
            request_id,
        )
        if result is None or not isinstance(result.get("conflicts"), list):
            raise GatewayProtocolError("Gateway returned an invalid migration preview")
        return result

    async def config_migration_preview(
        self, choices: dict[str, str] | None = None
    ) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_migration_preview",
                "request_id": str(request_id),
                "choices": choices or {},
            },
            request_id,
        )
        if result is None or not isinstance(result.get("accepted"), list):
            raise GatewayProtocolError("Gateway returned an invalid migration preview")
        return result

    async def config_migration_apply(
        self, *, expected_revision: int, choices: dict[str, str], confirm: bool
    ) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_migration_apply",
                "request_id": str(request_id),
                "expected_revision": expected_revision,
                "choices": choices,
                "confirm": confirm,
            },
            request_id,
        )
        if result is None or result.get("applied") is not True:
            raise GatewayProtocolError("Gateway returned an invalid migration result")
        for source in getattr(self, "migration_source_files", ()):
            path = Path(source)
            if path.is_file() and not path.is_symlink():
                marker = path.with_name(f"{path.name}.tfr-migrated")
                marker.write_text(f"revision {result.get('revision')}\n", encoding="utf-8")
                os.chmod(marker, 0o600)
        return result

    async def config_migration_status(self) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "config_migration_status", "request_id": str(request_id)}, request_id
        )
        if result is None or not isinstance(result.get("proposal_count"), int):
            raise GatewayProtocolError("Gateway returned invalid migration status")
        return result

    async def config_migration_rollback(
        self, *, expected_revision: int, confirm: bool
    ) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "config_migration_rollback",
                "request_id": str(request_id),
                "expected_revision": expected_revision,
                "confirm": confirm,
            },
            request_id,
        )
        if result is None or result.get("rolled_back") is not True:
            raise GatewayProtocolError("Gateway returned invalid migration rollback")
        return result

    async def plugins_catalog(self, state: str | None = None) -> list[dict[str, object]]:
        request_id = uuid4()
        message: dict[str, object] = {
            "type": "plugins_catalog",
            "request_id": str(request_id),
        }
        if state is not None:
            message["state"] = state
        result = await self._request(message, request_id)
        plugins = result.get("plugins") if result else None
        if not isinstance(plugins, list) or not all(
            isinstance(plugin, dict) and isinstance(plugin.get("name"), str) for plugin in plugins
        ):
            raise GatewayProtocolError("Gateway returned an invalid plugin catalog")
        return plugins

    async def plugin_show(self, name: str) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "plugin_show", "request_id": str(request_id), "name": name}, request_id
        )
        plugin = result.get("plugin") if result else None
        clients = result.get("clients") if result else None
        if not isinstance(plugin, dict) or not isinstance(clients, list):
            raise GatewayProtocolError("Gateway returned invalid plugin details")
        return {"plugin": plugin, "clients": clients}

    async def plugin_enablement(
        self, name: str, *, enabled: bool, expected_revision: int, confirm: bool
    ) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "plugin_enable" if enabled else "plugin_disable",
                "request_id": str(request_id),
                "name": name,
                "expected_revision": expected_revision,
                "confirm": confirm,
            },
            request_id,
        )
        if result is None or result.get("name") != name:
            raise GatewayProtocolError("Gateway returned invalid plugin enablement")
        return result

    @staticmethod
    def _backup_migration_sources(source_files: tuple[str, ...]) -> None:
        for source in source_files:
            path = Path(source).expanduser()
            if not path.is_file() or path.is_symlink():
                continue
            backup = path.with_name(f"{path.name}.pre-tfr-migration")
            if not backup.exists():
                shutil.copyfile(path, backup)
                os.chmod(backup, 0o600)

    async def pair_web_device(self, label: str) -> str:
        request_id = uuid4()
        result = await self._request(
            {"type": "pair_device", "request_id": str(request_id), "label": label},
            request_id,
        )
        pairing_url = result.get("pairing_url") if result is not None else None
        if not isinstance(pairing_url, str):
            raise GatewayProtocolError("Gateway returned an invalid pairing URL")
        return pairing_url

    async def update_ui_capabilities(self, capabilities: tuple[str, ...]) -> None:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "ui_capabilities",
                "request_id": str(request_id),
                "capabilities": list(capabilities),
            },
            request_id,
        )
        value = result.get("ui") if result else None
        try:
            self.ui_record = UiRecord.from_dict(value)
        except ValueError as exc:
            raise GatewayProtocolError("Gateway returned invalid UI capabilities") from exc

    async def list_web_devices(self) -> list[dict[str, Any]]:
        request_id = uuid4()
        result = await self._request(
            {"type": "list_devices", "request_id": str(request_id)}, request_id
        )
        devices = result.get("devices") if result is not None else None
        if not isinstance(devices, list) or not all(isinstance(item, dict) for item in devices):
            raise GatewayProtocolError("Gateway returned an invalid web device list")
        return devices

    async def list_uis(self) -> list[dict[str, object]]:
        request_id = uuid4()
        result = await self._request(
            {"type": "list_uis", "request_id": str(request_id)}, request_id
        )
        values = result.get("uis") if result is not None else None
        if not isinstance(values, list):
            raise GatewayProtocolError("Gateway returned an invalid UI list")
        try:
            return [UiRecord.from_dict(value).as_dict() for value in values]
        except ValueError as exc:
            raise GatewayProtocolError(f"Gateway returned an invalid UI list: {exc}") from exc

    async def show_ui(self, name: str) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "show_ui", "request_id": str(request_id), "name": name},
            request_id,
        )
        value = result.get("ui") if result is not None else None
        try:
            return UiRecord.from_dict(value).as_dict()
        except ValueError as exc:
            raise GatewayProtocolError(f"Gateway returned an invalid UI record: {exc}") from exc

    async def rename_ui(self, name: str) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {"type": "rename_ui", "request_id": str(request_id), "name": name},
            request_id,
        )
        value = result.get("ui") if result is not None else None
        try:
            record = UiRecord.from_dict(value)
        except ValueError as exc:
            raise GatewayProtocolError(f"Gateway returned an invalid UI record: {exc}") from exc
        if self.ui_record is None or record.ui_id != self.ui_record.ui_id:
            raise GatewayProtocolError("Gateway renamed a different UI")
        self.ui_record = record
        return record.as_dict()

    async def rename_other_ui(self, target: str, name: str) -> dict[str, object]:
        request_id = uuid4()
        result = await self._request(
            {
                "type": "rename_ui_target",
                "request_id": str(request_id),
                "target": target,
                "name": name,
            },
            request_id,
        )
        value = result.get("ui") if result is not None else None
        try:
            return UiRecord.from_dict(value).as_dict()
        except ValueError as exc:
            raise GatewayProtocolError(f"Gateway returned an invalid UI record: {exc}") from exc

    async def forget_ui(self, target: str, *, confirm: bool) -> dict[str, object]:
        if not confirm:
            raise ValueError("forgetting a UI requires explicit confirmation")
        request_id = uuid4()
        result = await self._request(
            {
                "type": "forget_ui",
                "request_id": str(request_id),
                "target": target,
                "confirm": True,
            },
            request_id,
        )
        value = result.get("ui") if result is not None else None
        try:
            record = UiRecord.from_dict(value)
        except ValueError as exc:
            raise GatewayProtocolError(f"Gateway returned an invalid UI record: {exc}") from exc
        if self.ui_record is not None and record.ui_id == self.ui_record.ui_id:
            self.ui_record = None
        return record.as_dict()

    async def revoke_web_device(self, device_id: UUID) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "revoke_device",
                "request_id": str(request_id),
                "device_id": str(device_id),
            },
            request_id,
        )

    async def diagnostics(self, category: str | None = None) -> list[dict[str, Any]]:
        request_id = uuid4()
        message: dict[str, Any] = {"type": "diagnostics", "request_id": str(request_id)}
        if category is not None:
            message["category"] = category
        result = await self._request(message, request_id)
        entries = result.get("entries") if result is not None else None
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise GatewayProtocolError("Gateway returned invalid diagnostics")
        return entries

    async def clear_diagnostics(self) -> None:
        request_id = uuid4()
        await self._request(
            {"type": "clear_diagnostics", "request_id": str(request_id)}, request_id
        )

    async def manage_gag(
        self,
        world: str,
        operation: str,
        *,
        pattern: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        request_id = uuid4()
        message: dict[str, Any] = {
            "type": "gag_settings",
            "request_id": str(request_id),
            "world": world,
            "operation": operation,
        }
        if pattern is not None:
            message["pattern"] = pattern
        if expected_revision is not None:
            message["expected_revision"] = expected_revision
        result = await self._request(message, request_id)
        if result is None:
            raise GatewayProtocolError("Gateway returned no gag settings result")
        revision = result.get("revision")
        patterns = result.get("patterns")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(patterns, list)
            or not all(isinstance(item, str) for item in patterns)
        ):
            raise GatewayProtocolError("Gateway returned invalid gag settings")
        return result

    async def manage_shared_ui_setting(
        self,
        setting: str,
        operation: str,
        *,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        request_id = uuid4()
        message: dict[str, Any] = {
            "type": "managed_ui_setting",
            "request_id": str(request_id),
            "setting": setting,
            "operation": operation,
        }
        if enabled is not None:
            message["enabled"] = enabled
        if expected_revision is not None:
            message["expected_revision"] = expected_revision
        result = await self._request(message, request_id)
        if (
            result is None
            or not isinstance(result.get("revision"), int)
            or not isinstance(result.get("enabled"), bool)
        ):
            raise GatewayProtocolError("Gateway returned invalid managed UI settings")
        return result

    async def verify_connection(self, *, timeout: float) -> bool:
        """Actively confirm the connection is alive with a bounded ping round trip.

        Unlike waiting for a read error, this detects a connection left
        silently stale (for example, after the UI machine sleeps and wakes
        with a socket that never reports an error on its own).
        """
        if self._pump is None or self._pump.done():
            return False
        try:
            await asyncio.wait_for(self.ping(), timeout=timeout)
        except (TimeoutError, ConnectionError, OSError, GatewayProtocolError, ValueError):
            return False
        return True

    async def _request(self, message: dict[str, Any], request_id: UUID) -> dict[str, Any] | None:
        if self._pump is None or self._pump.done():
            raise GatewayDisconnectedError("gateway client is not running")
        future: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await write_message(self.writer, message, lock=self._write_lock)
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _run(self) -> None:
        error: Exception = GatewayDisconnectedError("gateway disconnected")
        try:
            while message := await read_message(self.reader):
                message_type = message["type"]
                if message_type == "event":
                    cursor, event = event_from_message(message)
                    if cursor <= self.cursor:
                        continue
                    self.cursor = cursor
                    self._update_session(event)
                    await self.event_bus.publish(event)
                elif message_type == "ack":
                    self._handle_ack(message)
                elif message_type == "update_prepare":
                    self._spawn_gateway_request(self._handle_update_prepare(message))
                elif message_type == "update_check":
                    self._spawn_gateway_request(self._handle_update_check(message))
                elif message_type == "update_commit":
                    self._spawn_gateway_request(self._handle_update_commit(message))
                elif message_type == "update_abort":
                    error = message.get("error")
                    if not isinstance(error, str):
                        raise GatewayProtocolError("Gateway update abort message is invalid")
                    if self.update_abort_handler is not None:
                        self.update_abort_handler(error)
                elif message_type == "ui_settings":
                    spellcheck = message.get("spellcheck_enabled")
                    streaks = message.get("speaking_streaks_enabled")
                    if not isinstance(spellcheck, bool) or not isinstance(streaks, bool):
                        raise GatewayProtocolError("Gateway UI settings update is invalid")
                    self.spellcheck_enabled = spellcheck
                    self.speaking_streaks_enabled = streaks
                    if self.ui_settings_handler is not None:
                        self.ui_settings_handler(spellcheck, streaks)
                elif message_type == "configuration":
                    revision, settings, requirements = self._configuration(message)
                    if revision is None:
                        raise GatewayProtocolError("Gateway configuration revision is missing")
                    changed = {
                        key: value
                        for key, value in settings.items()
                        if self.effective_configuration.get(key) != value
                    }
                    changed_requirements = {
                        key: requirements[key] for key in changed if key in requirements
                    }
                    issues: tuple[str, ...] = ()
                    status = required_action(changed_requirements)
                    if self.configuration_handler is not None:
                        status, issues = self.configuration_handler(changed, changed_requirements)
                    self.configuration_revision = revision
                    self.effective_configuration = settings
                    self.configuration_requirements = requirements
                    self._spawn_gateway_request(
                        self._acknowledge_configuration(revision, status, issues)
                    )
                elif message_type == "error":
                    raise GatewayProtocolError(str(message.get("message", "gateway error")))
                else:
                    raise GatewayProtocolError(f"unexpected gateway message: {message_type}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self._fail_pending(error)
            if self.disconnect_handler is not None:
                self.disconnect_handler(error)

    def _handle_ack(self, message: Mapping[str, Any]) -> None:
        try:
            request_id = UUID(str(message["request_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayProtocolError("gateway acknowledgement has an invalid request_id") from exc
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if message.get("ok") is True:
            result = message.get("result")
            if result is not None and not isinstance(result, dict):
                raise GatewayProtocolError("gateway acknowledgement result must be an object")
            future.set_result(result)
        else:
            future.set_exception(ValueError(str(message.get("error", "gateway request failed"))))

    async def _acknowledge_configuration(
        self, revision: int, status: str, issues: tuple[str, ...]
    ) -> None:
        request_id = uuid4()
        await self._request(
            {
                "type": "configuration_applied",
                "request_id": str(request_id),
                "revision": revision,
                "status": status,
                "issues": list(issues),
            },
            request_id,
        )

    @staticmethod
    def _configuration(
        value: object,
    ) -> tuple[int | None, dict[str, object], dict[str, str]]:
        if value is None:
            return None, {}, {}
        if not isinstance(value, dict):
            raise GatewayProtocolError("Gateway configuration envelope is invalid")
        revision = value.get("revision")
        settings = value.get("settings")
        requirements = value.get("requirements")
        allowed_requirements = {"live", "client-reload", "client-restart", "gateway-restart"}
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(settings, dict)
            or not all(isinstance(key, str) for key in settings)
            or not isinstance(requirements, dict)
            or set(requirements) != set(settings)
            or not all(
                isinstance(key, str) and requirement in allowed_requirements
                for key, requirement in requirements.items()
            )
        ):
            raise GatewayProtocolError("Gateway configuration envelope is invalid")
        registry = core_configuration_registry()
        for key, item in settings.items():
            descriptor = registry.get(key)
            if descriptor is None:
                # Plugin descriptors are loaded after the transport handshake.
                continue
            if descriptor.sensitivity or "native-ui" not in descriptor.consumers:
                raise GatewayProtocolError(f"Gateway sent unsupported configuration setting: {key}")
            try:
                descriptor.validate(item)
            except ValueError as exc:
                raise GatewayProtocolError(str(exc)) from exc
        return revision, dict(settings), dict(requirements)

    async def _handle_update_prepare(self, message: Mapping[str, Any]) -> None:
        await self._handle_gateway_update_request(message, prepare=True)

    async def _handle_update_check(self, message: Mapping[str, Any]) -> None:
        try:
            request_id = UUID(str(message["request_id"]))
            if self.update_check_handler is None:
                raise ValueError("this UI cannot check managed update availability")
            result = await self.update_check_handler()
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            await write_message(
                self.writer,
                {
                    "type": "ack",
                    "request_id": str(message.get("request_id", "")),
                    "ok": False,
                    "error": str(exc),
                },
                lock=self._write_lock,
            )
            return
        await write_message(
            self.writer,
            {"type": "ack", "request_id": str(request_id), "ok": True, "result": result},
            lock=self._write_lock,
        )

    async def _handle_update_commit(self, message: Mapping[str, Any]) -> None:
        await self._handle_gateway_update_request(message, prepare=False)

    def _spawn_gateway_request(self, coroutine: Awaitable[None]) -> None:
        task = asyncio.create_task(coroutine, name="tfr-gateway-update-request")
        self._gateway_request_tasks.add(task)
        task.add_done_callback(self._gateway_request_done)

    def _gateway_request_done(self, task: asyncio.Task[None]) -> None:
        self._gateway_request_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _handle_gateway_update_request(
        self, message: Mapping[str, Any], *, prepare: bool
    ) -> None:
        try:
            request_id = UUID(str(message["request_id"]))
            if prepare:
                if self.update_prepare_handler is None:
                    raise ValueError("this UI cannot stage managed updates")
                force = message.get("force", False)
                if not isinstance(force, bool):
                    raise ValueError("Gateway update force marker is invalid")
                manifest = message.get("manifest")
                if not force and not isinstance(manifest, dict):
                    raise ValueError("Gateway update manifest is invalid")
                result = await self.update_prepare_handler(
                    manifest if isinstance(manifest, dict) else None,
                    force,
                )
            else:
                if self.update_commit_handler is None:
                    raise ValueError("this UI cannot activate managed updates")
                release_id = message.get("release_id")
                if not isinstance(release_id, str) or not release_id:
                    raise ValueError("Gateway update release ID is invalid")
                await self.update_commit_handler(release_id)
                result = None
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            await write_message(
                self.writer,
                {
                    "type": "ack",
                    "request_id": str(message.get("request_id", "")),
                    "ok": False,
                    "error": str(exc),
                },
                lock=self._write_lock,
            )
            return
        acknowledgement: dict[str, Any] = {
            "type": "ack",
            "request_id": str(request_id),
            "ok": True,
        }
        if result is not None:
            acknowledgement["result"] = result
        await write_message(self.writer, acknowledgement, lock=self._write_lock)
        if not prepare and self.update_restart_handler is not None:
            self.update_restart_handler()

    def _update_session(self, event: Event) -> None:
        if event.kind.value != "connection":
            return
        state = event.metadata.get("state")
        with contextlib.suppress(ValueError, KeyError):
            session = self._session_for(event.world)
            session.state = SessionState(str(state))
            session.connection_generation = event.connection_generation

    def _session_for(self, world: str) -> RemoteWorldSession:
        try:
            return next(session for session in self.sessions if session.world == world)
        except StopIteration as exc:
            raise ValueError(f"unknown world: {world}") from exc

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    @staticmethod
    def _non_negative_integer(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise GatewayProtocolError(f"gateway {name} must be a non-negative integer")
        return value

    @staticmethod
    async def _read_snapshot(
        reader: asyncio.StreamReader,
        count: int,
        maximum_cursor: int,
    ) -> tuple[Event, ...]:
        events: list[Event] = []
        previous_cursor = 0
        for _ in range(count):
            message = await read_message(reader)
            if message is None:
                raise GatewayDisconnectedError("gateway disconnected during history snapshot")
            event_cursor, event = event_from_message(message)
            if event_cursor <= previous_cursor or event_cursor > maximum_cursor:
                raise GatewayProtocolError("snapshot event cursors are not ordered")
            previous_cursor = event_cursor
            events.append(event)
        return tuple(events)


class GatewayUiRuntime:
    def __init__(
        self,
        client: GatewayClient,
        plugins: PluginManager,
        *,
        reconnect_config: GatewayReconnectConfig | None = None,
        update_config: UpdateConfig | None = None,
        plugin_update_checker: PluginUpdateChecker | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.plugins = plugins
        self.reconnect_config = reconnect_config or GatewayReconnectConfig()
        self.notify = notify or (lambda _text: None)
        self.update_config = update_config
        self.plugin_update_checker = plugin_update_checker
        self._staged_update: StagedManagedUpdate | None = None
        self._update_transaction_active = False
        self._update_restart_expected = False
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._gateway_connected = False
        self._gateway_state_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self.client.connect_handler = self._handle_connected
        self.client.disconnect_handler = self._handle_disconnect
        self.client.update_prepare_handler = self._prepare_update
        self.client.update_check_handler = self._check_update
        self.client.update_commit_handler = self._commit_update
        self.client.update_abort_handler = self._abort_update
        self.client.event_bus.add_processor(self.plugins.process_event)

    async def start(self) -> None:
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_start"))
        await self._set_gateway_connected(True)
        await self._reconcile_world_states()
        self.client.start()
        if self.reconnect_config.enabled:
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="tfr-gateway-reconnect-supervisor"
            )

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor_task
            self._supervisor_task = None
        await self._set_gateway_connected(False, intentional=True)
        await self.client.stop()
        if self._connection_tasks:
            await asyncio.gather(*tuple(self._connection_tasks), return_exceptions=True)
        await self.plugins.drain()
        await self.plugins.lifecycle(PluginLifecycleEvent(kind="application_stop"))
        await self.plugins.drain()
        await self.client.event_bus.close()

    async def reconnect(self) -> str:
        reconnect_in_progress = self._reconnect_lock.locked()
        async with self._reconnect_lock:
            if (
                reconnect_in_progress
                and self._gateway_connected
                and await self.client.verify_connection(
                    timeout=self.reconnect_config.ping_timeout_seconds
                )
            ):
                return "Gateway connection is already healthy"
            await self._set_gateway_connected(False, intentional=True)
            restored = await self.client.reconnect()
            await self._set_gateway_connected(True)
            await self._reconcile_world_states()
            return self._format_reconnect_message(restored)

    async def request_update(self, *, force: bool = False) -> dict[str, Any]:
        self._update_transaction_active = True
        try:
            result = await self.client.update(force=force)
            if result.get("updated") is False or result.get("restart_required") is False:
                self._update_transaction_active = False
            return result
        except BaseException:
            self._update_transaction_active = False
            raise

    async def web_status(self) -> dict[str, Any]:
        return await self.client.web_status()

    async def config_summary(self) -> dict[str, object]:
        return await self.client.config_summary()

    async def config_list(self, filter_value: str | None = None) -> list[dict[str, object]]:
        return await self.client.config_list(filter_value)

    async def config_show(self, key: str, *, ui: str | None = None) -> dict[str, object]:
        return await self.client.config_show(key, ui=ui)

    async def config_status(self) -> dict[str, object]:
        return await self.client.config_status()

    async def config_mutate(
        self,
        operation: str,
        key: str,
        *,
        value: object = None,
        scope: str,
        target: str | None,
        expected_revision: int,
        confirm: bool,
    ) -> dict[str, object]:
        return await self.client.config_mutate(
            operation,
            key,
            value=value,
            scope=scope,
            target=target,
            expected_revision=expected_revision,
            confirm=confirm,
        )

    async def config_changes(self, limit: int = 50) -> dict[str, object]:
        return await self.client.config_changes(limit)

    async def config_batch(
        self,
        changes: list[dict[str, object]],
        *,
        expected_revision: int,
        confirm: bool,
    ) -> dict[str, object]:
        return await self.client.config_batch(
            changes,
            expected_revision=expected_revision,
            confirm=confirm,
        )

    async def config_undo(self, *, expected_revision: int, confirm: bool) -> dict[str, object]:
        return await self.client.config_undo(
            expected_revision=expected_revision,
            confirm=confirm,
        )

    async def config_migrate(self) -> dict[str, object]:
        values = getattr(self.client, "migration_values", {})
        sources = getattr(self.client, "migration_source_files", ())
        return await self.client.config_migrate(values, sources)

    async def config_migration_preview(
        self, choices: dict[str, str] | None = None
    ) -> dict[str, object]:
        return await self.client.config_migration_preview(choices)

    async def config_migration_apply(
        self, *, expected_revision: int, choices: dict[str, str], confirm: bool
    ) -> dict[str, object]:
        return await self.client.config_migration_apply(
            expected_revision=expected_revision,
            choices=choices,
            confirm=confirm,
        )

    async def config_migration_status(self) -> dict[str, object]:
        return await self.client.config_migration_status()

    async def config_migration_rollback(
        self, *, expected_revision: int, confirm: bool
    ) -> dict[str, object]:
        return await self.client.config_migration_rollback(
            expected_revision=expected_revision,
            confirm=confirm,
        )

    async def plugins_catalog(self, state: str | None = None) -> list[dict[str, object]]:
        return await self.client.plugins_catalog(state)

    async def plugin_show(self, name: str) -> dict[str, object]:
        return await self.client.plugin_show(name)

    async def plugin_enablement(
        self, name: str, *, enabled: bool, expected_revision: int, confirm: bool
    ) -> dict[str, object]:
        return await self.client.plugin_enablement(
            name,
            enabled=enabled,
            expected_revision=expected_revision,
            confirm=confirm,
        )

    async def pair_web_device(self, label: str) -> str:
        return await self.client.pair_web_device(label)

    async def list_web_devices(self) -> list[dict[str, Any]]:
        return await self.client.list_web_devices()

    async def list_uis(self) -> list[dict[str, object]]:
        return await self.client.list_uis()

    async def show_ui(self, name: str) -> dict[str, object]:
        return await self.client.show_ui(name)

    async def rename_ui(self, name: str) -> dict[str, object]:
        return await self.client.rename_ui(name)

    async def rename_other_ui(self, target: str, name: str) -> dict[str, object]:
        return await self.client.rename_other_ui(target, name)

    async def forget_ui(self, target: str, *, confirm: bool) -> dict[str, object]:
        return await self.client.forget_ui(target, confirm=confirm)

    async def revoke_web_device(self, device_id: UUID) -> None:
        await self.client.revoke_web_device(device_id)

    async def diagnostics(self, category: str | None = None) -> list[dict[str, Any]]:
        return await self.client.diagnostics(category)

    async def clear_diagnostics(self) -> None:
        await self.client.clear_diagnostics()

    async def manage_gag(
        self,
        world: str,
        operation: str,
        *,
        pattern: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self.client.manage_gag(
            world,
            operation,
            pattern=pattern,
            expected_revision=expected_revision,
        )

    async def manage_shared_ui_setting(
        self,
        setting: str,
        operation: str,
        *,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self.client.manage_shared_ui_setting(
            setting,
            operation,
            enabled=enabled,
            expected_revision=expected_revision,
        )

    async def _check_update(self) -> dict[str, Any]:
        available = (
            await self.plugin_update_checker.stable_auto_update_available()
            if self.plugin_update_checker is not None
            else False
        )
        return {"available": available}

    async def _prepare_update(
        self,
        manifest_value: Mapping[str, Any] | None,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.update_config is None:
            raise ValueError("TFR updates are unavailable")
        self._update_transaction_active = True
        if force and self.update_config.channel != "candidate":
            raise ValueError("forced updates are available only on the candidate channel")
        manifest: ReleaseManifest | None = None
        if not force:
            try:
                if manifest_value is None:
                    raise ValueError("manifest is missing")
                manifest = ReleaseManifest.from_json(
                    json.dumps(dict(manifest_value), separators=(",", ":")).encode(),
                    expected_channel=self.update_config.channel,
                )
            except (TypeError, ValueError, UpdateError) as exc:
                raise ValueError(f"Gateway update manifest is invalid: {exc}") from exc
            self.notify(f"Preparing UI update {manifest.version}. {manifest.release_url}")
        else:
            self.notify("Force-preparing the locally configured candidate update.")
        try:
            self._staged_update = await stage_managed_update(
                self.update_config, expected_manifest=manifest
            )
        except InstallationError as exc:
            raise ValueError(f"This UI host update staging failed: {exc}") from exc
        return {
            "version": self._staged_update.version,
            "commit": self._staged_update.commit,
            "release_url": self._staged_update.release_url,
        }

    async def _commit_update(self, release_id: str) -> None:
        if self._staged_update is None or self._staged_update.release_id != release_id:
            raise ValueError("UI has not staged the requested release")
        await asyncio.to_thread(activate_managed_update, release_id)
        self._update_restart_expected = True
        self.notify(
            f"Activated UI {self._staged_update.version}. {self._staged_update.release_url}"
        )

    def _abort_update(self, error: str) -> None:
        self._update_transaction_active = False
        self._staged_update = None
        self.notify(error)

    async def _handle_connected(self) -> None:
        await self._set_gateway_connected(True)

    def _handle_disconnect(self, _error: Exception) -> None:
        if self._update_restart_expected and not self._stopping:
            task = asyncio.create_task(
                self._recover_after_update(), name="tfr-gateway-update-reconnect"
            )
            self._connection_tasks.add(task)
            task.add_done_callback(self._connection_task_done)
            return
        self._update_transaction_active = False
        task = asyncio.create_task(
            self._set_gateway_connected(False, intentional=self._stopping),
            name="tfr-gateway-disconnected-event",
        )
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_task_done)

    async def _recover_after_update(self) -> None:
        await self._set_gateway_connected(False)
        last_error: Exception | None = None
        for _attempt in range(120):
            if self._stopping:
                return
            try:
                restored = await self.client.reconnect()
            except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc
                await asyncio.sleep(1)
                continue
            self._update_restart_expected = False
            self._update_transaction_active = False
            await self._set_gateway_connected(True)
            await self._reconcile_world_states()
            self.notify(self._format_reconnect_message(restored))
            return
        self._update_restart_expected = False
        self._update_transaction_active = False
        self.notify(
            "Gateway did not return after the update"
            + (f": {last_error}" if last_error is not None else "")
        )

    def _connection_task_done(self, task: asyncio.Task[None]) -> None:
        self._connection_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _set_gateway_connected(self, connected: bool, *, intentional: bool = False) -> None:
        async with self._gateway_state_lock:
            if self._gateway_connected is connected:
                return
            self._gateway_connected = connected
            await self.plugins.lifecycle(
                PluginLifecycleEvent(
                    kind="gateway_connected" if connected else "gateway_disconnected",
                    state="connected" if connected else "disconnected",
                    source="gateway",
                    metadata={"intentional": intentional},
                )
            )

    async def _reconcile_world_states(self) -> None:
        for session in self.client.sessions:
            await self.plugins.publish_session_state(session.world, session.state.value)

    def _format_reconnect_message(self, restored: int) -> str:
        if self.client.history_truncated:
            return (
                f"Reconnected to Gateway; restored {restored} retained events after a history gap"
            )
        if restored:
            return f"Reconnected to Gateway; restored {restored} missed events"
        return "Reconnected to Gateway"

    async def _supervise(self) -> None:
        """Periodically verify the Gateway connection and recover from silence.

        A read error alone cannot detect a connection left stale by the UI
        machine sleeping and waking: the underlying socket may never report
        an error, so `_run`'s read loop would simply hang. This task instead
        actively pings the Gateway on a fixed interval; a failed or timed-out
        ping is treated the same as a hard disconnect and triggers a bounded
        automatic reconnect, reusing the same `GatewayClient.reconnect()`
        logic `/gateway reconnect` already uses.
        """
        config = self.reconnect_config
        try:
            while True:
                await asyncio.sleep(config.heartbeat_seconds)
                if self._stopping:
                    return
                if self._update_transaction_active:
                    continue
                alive = await self.client.verify_connection(timeout=config.ping_timeout_seconds)
                if alive or self._stopping:
                    continue
                await self._recover_connection()
        except asyncio.CancelledError:
            pass

    async def _recover_connection(self) -> None:
        async with self._reconnect_lock:
            config = self.reconnect_config
            if self._gateway_connected and await self.client.verify_connection(
                timeout=config.ping_timeout_seconds
            ):
                return
            await self._set_gateway_connected(False)
            for attempt in range(1, config.max_attempts + 1):
                if self._stopping:
                    return
                self.notify(
                    f"Gateway connection lost; reconnecting (attempt "
                    f"{attempt}/{config.max_attempts})..."
                )
                try:
                    restored = await self.client.reconnect()
                except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
                    if attempt == config.max_attempts:
                        failure = (
                            f"Gateway reconnect failed after {config.max_attempts} attempts: "
                            f"{exc}. Use /gateway reconnect to retry manually."
                        )
                        self.notify(failure)
                        return
                    await asyncio.sleep(config.retry_interval_seconds)
                else:
                    await self._set_gateway_connected(True)
                    await self._reconcile_world_states()
                    self.notify(self._format_reconnect_message(restored))
                    return


def _apply_gateway_ui_configuration(
    configuration: UiConfiguration, settings: Mapping[str, object]
) -> UiConfiguration:
    """Overlay the Gateway view without ever writing it to local configuration."""

    value = configuration.main.model_dump(mode="python", by_alias=True, exclude_none=True)
    allowed_roots = {"ui", "updates", "plugins"}
    for key, item in settings.items():
        parts = key.split(".")
        if not parts or parts[0] not in allowed_roots:
            continue
        target: dict[str, Any] = value
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = item
    return configuration.model_copy(update={"main": MainConfig.model_validate(value)})


def _legacy_migration_values(
    configuration: UiConfiguration, plugin_descriptors: Sequence[SettingDescriptor] = ()
) -> dict[str, object]:
    marker = configuration.main_path.with_name(f"{configuration.main_path.name}.tfr-migrated")
    if marker.is_file():
        return {}
    registry = core_configuration_registry()
    for descriptor in plugin_descriptors:
        registry.register(descriptor)
    output: dict[str, object] = {}

    def record_plugin(prefix: str, value: object) -> None:
        descriptor = registry.get(prefix)
        if descriptor is not None:
            output[prefix] = None if descriptor.sensitivity else value
            return
        if isinstance(value, dict):
            for name, item in value.items():
                record_plugin(f"{prefix}.{name}", item)

    def visit(model: BaseModel, prefix: str = "") -> None:
        serialized = model.model_dump(mode="json")
        for field in model.model_fields_set:
            if field == "schema_url":
                continue
            item = getattr(model, field)
            key = f"{prefix}.{field}" if prefix else field
            if isinstance(item, BaseModel):
                visit(item, key)
            else:
                descriptor = registry.get(key)
                if key == "plugins.config" and isinstance(serialized[field], dict):
                    for plugin, plugin_value in serialized[field].items():
                        record_plugin(f"plugins.config.{plugin}", plugin_value)
                elif descriptor is not None:
                    output[key] = None if descriptor.sensitivity else serialized[field]

    visit(configuration.main)
    return output


async def run_gateway_ui(
    configuration: UiConfiguration,
    path: Path | str | None = None,
    *,
    gateway_host: str | None = None,
    gateway_port: int = DEFAULT_GATEWAY_PORT,
    token_file: Path | str | None = None,
    tls_ca: Path | str | None = None,
    tls_server_name: str | None = None,
    client_id: UUID | None = None,
    gateway_profiles: object | None = None,
    auth_token: str | None = None,
    desired_ui_name: str | None = None,
) -> int:
    from tfr.tui import TfrTui
    resume_gateway_id, resume_world, update_restart = _restart_state_from_environment()
    build = current_build()
    platform_name = socket.gethostname().strip()
    if not platform_name or not platform_name.isprintable():
        platform_name = "Unknown device"
    ui_registration = UiRegistration(
        client_type="terminal",
        platform=platform_name[:80],
        versions=tuple(
            sorted(
                {
                    "protocol": str(build.protocol),
                    "tfr": build.version,
                    **({"commit": build.commit} if build.commit is not None else {}),
                }.items()
            )
        ),
        capabilities=tuple(
            sorted(
                {capability.value for capability in TUI_PRESENTATION_CAPABILITIES}
                | {CONFIGURATION_SYNC_CAPABILITY}
            )
        ),
    )
    connection_options = {
        "show_nospoof_prefix": configuration.main.ui.show_nospoof_prefix,
        "client_id": client_id
        or load_or_create_native_ui_id(configuration.main.managed_settings.state_directory),
        "ui_registration": ui_registration,
        "gateway_id": resume_gateway_id,
        # A replacement UI needs retained history to rebuild its in-memory display.
        "after_cursor": None,
    }

    async def connect() -> GatewayClient:
        if gateway_host is None:
            return await GatewayClient.connect(
                path or default_gateway_socket(),
                **connection_options,
            )
        validate_tcp_endpoint(gateway_host, gateway_port)
        if token_file is None and auth_token is None:
            raise ValueError("network gateway connection requires --token-file")
        return await GatewayClient.connect_tcp(
            gateway_host,
            gateway_port,
            auth_token=auth_token or load_gateway_token(token_file),
            tls_context=create_gateway_client_tls_context(tls_ca),
            server_hostname=tls_server_name,
            **connection_options,
        )

    maximum_attempts = 120 if update_restart else 30 if resume_gateway_id is not None else 120
    for attempt in range(maximum_attempts):
        try:
            client = await connect()
            break
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            update_in_progress = "Gateway update in progress" in str(exc)
            if (
                not update_restart
                and resume_gateway_id is None
                and not update_in_progress
                or attempt + 1 == maximum_attempts
            ):
                raise
            await asyncio.sleep(1)
    manager = SessionManager(client.sessions)  # type: ignore[arg-type]
    client.migration_values = {}
    client.migration_source_files = ()
    configuration = _apply_gateway_ui_configuration(
        configuration, getattr(client, "effective_configuration", {})
    )
    try:
        extra_plugins, plugin_source_failures, plugin_source_notices = await load_plugin_sources(
            configuration.main.plugins.sources,
            plugins_directory=configuration.main.plugins.state_directory,
        )
        plugin_source_messages = [
            f"Plugin source {failure.source_id}: {failure.error}"
            for failure in plugin_source_failures
        ]
        plugin_source_messages.extend(
            f"Plugin source {notice.source_id}: {notice.message}"
            for notice in plugin_source_notices
        )
        plugins = await PluginManager.load(
            enabled=configuration.main.plugins.enabled,
            config=configuration.main.plugins.config,
            event_bus=client.event_bus,
            command_bus=client.command_bus,  # type: ignore[arg-type]
            targets={session.world: session.session_id for session in client.sessions},
            worlds={
                session.world: PluginWorldInfo(
                    server=session.config.server,
                    encoding=session.encoding,
                )
                for session in client.sessions
            },
            extra_discovered=extra_plugins,
            scope="ui",
        )
        if hasattr(client, "prepare_initial_configuration_ack"):
            descriptors = (
                plugins.registry.configuration_descriptors() if hasattr(plugins, "registry") else ()
            )
            client.prepare_initial_configuration_ack(descriptors)
            client.migration_values = _legacy_migration_values(configuration, descriptors)
            client.migration_source_files = (
                (str(configuration.main_path),) if configuration.main_path.is_file() else ()
            )
            client.start()
            await client.update_ui_capabilities(
                tuple(
                    sorted(
                        {capability.value for capability in TUI_PRESENTATION_CAPABILITIES}
                        | {CONFIGURATION_SYNC_CAPABILITY}
                        | {f"plugin:{name}" for name in plugins.loaded_plugins}
                    )
                )
            )
        if (
            desired_ui_name is not None
            and client.ui_record is not None
            and client.ui_record.display_name != desired_ui_name
        ):
            await client.rename_ui(desired_ui_name)
        plugins.initialize_boss_selection(
            configuration.main.ui.boss.mode,
            configuration.main.ui.boss.screen,
        )
    except BaseException:
        try:
            await client.stop()
        finally:
            await client.event_bus.close()
        raise
    update_checker = UpdateChecker(configuration.main.updates)
    plugin_update_checker = PluginUpdateChecker(
        configuration.main.plugins.sources,
        plugins_directory=configuration.main.plugins.state_directory,
        config=configuration.main.updates,
        notified_versions={
            notice.source_id: notice.available_version
            for notice in plugin_source_notices
            if notice.available_version is not None
        },
    )
    runtime = GatewayUiRuntime(
        client,
        plugins,
        reconnect_config=configuration.main.ui.gateway_reconnect,
        update_config=configuration.main.updates,
        plugin_update_checker=plugin_update_checker,
    )
    # Remote UIs keep no authoritative preference file. Per-UI values arrive
    # in the Gateway configuration view and are changed with /config set.
    local_ui_settings = None
    mouse_mode = configuration.main.ui.mouse_mode
    tui = TfrTui(
        sessions=client.sessions,  # type: ignore[arg-type]
        manager=manager,
        event_bus=client.event_bus,
        command_bus=client.command_bus,  # type: ignore[arg-type]
        scrollback_lines={session.world: session.scrollback_lines for session in client.sessions},
        agent_worlds=client.agent_worlds,
        pager_enabled=configuration.main.ui.pager.enabled,
        pager_overlap=configuration.main.ui.pager.overlap_lines,
        recent_input_lines=configuration.main.ui.recent_input_lines,
        mouse_mode=mouse_mode,
        local_ui_settings=local_ui_settings,
        spellcheck=configuration.main.ui.spellcheck.model_copy(
            update={
                "enabled": getattr(
                    client,
                    "spellcheck_enabled",
                    configuration.main.ui.spellcheck.enabled,
                )
            }
        ),
        typing_glow=getattr(configuration.main.ui, "typing_glow", None),
        animations_enabled=configuration.main.ui.animations_enabled,
        low_bandwidth=configuration.main.ui.low_bandwidth,
        speaking_streaks_enabled=getattr(
            client,
            "speaking_streaks_enabled",
            configuration.main.ui.speaking_streaks.enabled,
        ),
        output_color=configuration.main.ui.output_color,
        theme=configuration.main.ui.theme,
        screen_clear_mode=configuration.main.ui.screen_clear.mode,
        screen_clear_effect=configuration.main.ui.screen_clear.effect,
        boss_screen_mode=configuration.main.ui.boss.mode,
        boss_screen=configuration.main.ui.boss.screen,
        plugins=plugins,
        agents=client.agents,  # type: ignore[arg-type]
        service_runtime=runtime,
        gateway_reconnect=runtime.reconnect,
        gateway_effect_demos=lambda: getattr(client, "effect_demos", ()),
        gateway_profiles=gateway_profiles,
        restart_supported=True,
        update_checker=update_checker,
        plugin_update_checker=plugin_update_checker,
        gateway_build=client.gateway_build,
        initial_events=client.initial_events,
        initial_scroll_to_end=True,
    )
    runtime.notify = lambda text: tui.add_notice(tui.active_alias, text)
    if hasattr(tui, "apply_shared_ui_settings"):
        client.ui_settings_handler = tui.apply_shared_ui_settings

    def apply_configuration_update(
        changed: dict[str, object], requirements: dict[str, str]
    ) -> tuple[str, tuple[str, ...]]:
        live_shared = {"ui.spellcheck.enabled", "ui.speaking_streaks.enabled"}
        unsupported = tuple(
            f"live application is unsupported for {key}"
            for key in sorted(changed)
            if requirements.get(key) == "live" and key not in live_shared
        )
        if unsupported:
            runtime.notify(
                "Gateway configuration revision received, but this UI cannot apply: "
                + "; ".join(unsupported)
            )
            return "unsupported", unsupported
        if "ui.spellcheck.enabled" in changed:
            client.spellcheck_enabled = bool(changed["ui.spellcheck.enabled"])
        if "ui.speaking_streaks.enabled" in changed:
            client.speaking_streaks_enabled = bool(changed["ui.speaking_streaks.enabled"])
        if changed.keys() & live_shared and hasattr(tui, "apply_shared_ui_settings"):
            tui.apply_shared_ui_settings(client.spellcheck_enabled, client.speaking_streaks_enabled)
        action = required_action(requirements)
        if action == "reload-required":
            runtime.notify("Gateway configuration changed; use /reload to apply this revision.")
        elif action == "restart-required":
            runtime.notify("Gateway configuration changed; restart this UI to apply this revision.")
        return action, ()

    client.configuration_handler = apply_configuration_update

    def restart_after_update() -> None:
        tui.update_restart_requested = True
        if tui.application.is_running:
            tui.application.exit(result=0)

    client.update_restart_handler = restart_after_update
    if resume_world in tui.views:
        tui.switch_world(resume_world)
    for message in plugin_source_messages:
        tui.queue_startup_notice(tui.active_alias, message)
    speaker_effects_warning = (
        remote_speaker_effects_warning(
            plugins,
            gateway_loaded=getattr(client, "gateway_speaker_effects_loaded", None),
            gateway_speakers=getattr(client, "gateway_speaker_effects", ()),
        )
        if hasattr(plugins, "configured_speaker_effect_rules")
        else None
    )
    if speaker_effects_warning is not None:
        tui.queue_startup_notice(tui.active_alias, speaker_effects_warning)
    if client.migration_values:
        tui.queue_startup_notice(
            tui.active_alias,
            "Legacy local configuration was detected. Submit and review it with "
            "/config migrate, then resolve conflicts before applying.",
        )
    if client.history_reset:
        tui.queue_startup_notice(
            tui.active_alias,
            "Gateway restarted; loaded retained history",
        )
    elif client.history_truncated:
        tui.queue_startup_notice(
            tui.active_alias,
            "Earlier gateway history is no longer retained",
        )
    result = await tui.run()
    if tui.restart_requested or getattr(tui, "update_restart_requested", False):
        os.environ[_RESTART_GATEWAY_ID] = str(client.gateway_id)
        os.environ[_RESTART_WORLD] = tui.active_alias
        if getattr(tui, "update_restart_requested", False):
            os.environ[_UPDATE_RESTART] = "1"
        restart = managed_restart_command(sys.argv[1:])
        if restart is None:
            restart = [sys.executable, "-I", "-m", "tfr", *sys.argv[1:]]
        os.execv(restart[0], restart)
    return result


def _restart_state_from_environment() -> tuple[UUID | None, str | None, bool]:
    gateway_value = os.environ.pop(_RESTART_GATEWAY_ID, None)
    os.environ.pop(_RESTART_CURSOR, None)
    world = os.environ.pop(_RESTART_WORLD, None)
    update_restart = os.environ.pop(_UPDATE_RESTART, None) == "1"
    if gateway_value is None:
        return None, None, update_restart
    try:
        gateway_id = UUID(gateway_value)
    except ValueError:
        return None, None, update_restart
    return gateway_id, world, update_restart
