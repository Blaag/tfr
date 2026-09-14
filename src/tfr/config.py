from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

import jsonc
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    ValidationError,
    model_validator,
)


class ConfigurationError(ValueError):
    """Raised when configuration cannot be read or validated."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PagerConfig(StrictModel):
    enabled: bool = True
    overlap_lines: int = Field(default=1, ge=0)


class ScreenClearConfig(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "locked"}},
                        "required": ["mode"],
                    },
                    "then": {"required": ["effect"]},
                    "else": {"properties": {"effect": {"type": "null"}}},
                }
            ]
        },
    )
    mode: Literal["cycle", "random", "locked"] = "cycle"
    effect: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")

    @model_validator(mode="after")
    def locked_effect_is_selected(self) -> ScreenClearConfig:
        if self.mode == "locked" and self.effect is None:
            raise ValueError("ui.screen_clear.effect is required when mode is locked")
        if self.mode != "locked" and self.effect is not None:
            raise ValueError("ui.screen_clear.effect is only valid when mode is locked")
        return self


class BossConfig(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "locked"}},
                        "required": ["mode"],
                    },
                    "then": {"required": ["screen"]},
                    "else": {"properties": {"screen": {"type": "null"}}},
                }
            ]
        },
    )
    mode: Literal["cycle", "random", "locked"] = "cycle"
    screen: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")

    @model_validator(mode="after")
    def locked_screen_is_selected(self) -> BossConfig:
        if self.mode == "locked" and self.screen is None:
            raise ValueError("ui.boss.screen is required when mode is locked")
        if self.mode != "locked" and self.screen is not None:
            raise ValueError("ui.boss.screen is only valid when mode is locked")
        return self


class GatewayReconnectConfig(StrictModel):
    """UI-side liveness verification and bounded automatic reconnect.

    A background heartbeat actively verifies the Gateway connection is alive
    (rather than only reacting to errors), so a connection left silently
    stale after the UI machine sleeps and wakes is detected even though the
    underlying socket may not report an error on its own. On a failed
    verification, the UI retries `/gateway reconnect`'s own logic up to
    `max_attempts` times before giving up and asking the operator to run
    `/gateway reconnect` manually.
    """

    enabled: bool = True
    heartbeat_seconds: float = Field(default=20.0, gt=0)
    ping_timeout_seconds: float = Field(default=8.0, gt=0)
    max_attempts: PositiveInt = 5
    retry_interval_seconds: float = Field(default=3.0, gt=0)


class UiConfig(StrictModel):
    scrollback_lines: PositiveInt = 20_000
    recent_input_lines: int = Field(default=3, ge=0, le=20)
    output_color: str = Field(default="#d7d7d7", pattern=r"^#[0-9a-fA-F]{6}$")
    show_nospoof_prefix: bool = False
    animations_enabled: bool = True
    low_bandwidth: bool = False
    pager: PagerConfig = Field(default_factory=PagerConfig)
    screen_clear: ScreenClearConfig = Field(default_factory=ScreenClearConfig)
    boss: BossConfig = Field(default_factory=BossConfig)
    gateway_reconnect: GatewayReconnectConfig = Field(default_factory=GatewayReconnectConfig)


class LoggingConfig(StrictModel):
    enabled: bool = True
    mode: Literal["all"] = "all"
    directory: Path = Path("~/.local/state/tfr/logs")


_PLUGIN_SOURCE_REPO = re.compile(r"^(?!-)[A-Za-z0-9](?:[A-Za-z0-9._~:/@%+-]*[A-Za-z0-9])?$")
_PLUGIN_SOURCE_REF = re.compile(r"^(?!-)[A-Za-z0-9](?:[A-Za-z0-9._/+-]*[A-Za-z0-9])?$")
_PLUGIN_SOURCE_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/+-]*$")


class PluginSource(StrictModel):
    """A GitHub (or other Git host) repository TFR fetches plugin code from.

    ``repo`` accepts an ``OWNER/REPO`` GitHub shorthand, a full ``https://``
    URL, or an ``ssh://``/``git@`` URL for a private repository reachable with
    the operator's own SSH credentials. TFR never stores or transmits
    credentials for this feature; it only invokes the local ``git`` binary.
    """

    repo: str = Field(min_length=1, max_length=512, pattern=_PLUGIN_SOURCE_REPO)
    ref: str | None = Field(default=None, min_length=1, max_length=200, pattern=_PLUGIN_SOURCE_REF)
    path: str = Field(default=".", max_length=200, pattern=_PLUGIN_SOURCE_PATH)
    auto_update: bool = False


class PluginsConfig(StrictModel):
    enabled: tuple[str, ...] = ()
    config: dict[str, Any] = Field(default_factory=dict)
    sources: tuple[PluginSource, ...] = ()
    state_directory: Path = Path("~/.local/state/tfr/plugins")


class MainConfig(StrictModel):
    schema_url: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1] = 1
    worlds_file: Path = Path("worlds.jsonc")
    agents_file: Path = Path("agents.jsonc")
    ui: UiConfig = Field(default_factory=UiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)


class WorldDefaults(StrictModel):
    encoding: str = "utf-8"
    reconnect: bool = True
    scrollback_lines: PositiveInt = 20_000


class TlsConfig(StrictModel):
    enabled: bool = False
    verify: bool = True
    ca_file: Path | None = None
    server_hostname: str | None = None


class LoginConfig(StrictModel):
    character: str = Field(min_length=1)
    password: SecretStr


class ProvenanceConfig(StrictModel):
    nospoof: bool = True
    show_prefix: bool | None = None


class IdleConfig(StrictModel):
    after_seconds: PositiveInt
    command: str = Field(min_length=1)


class WorldConfig(StrictModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65_535)
    server: Literal["bare", "generic", "rhost", "tinymush", "tinymux"] = "generic"
    encoding: str | None = None
    reconnect: bool | None = None
    scrollback_lines: PositiveInt | None = None
    tls: TlsConfig = Field(default_factory=TlsConfig)
    login: LoginConfig | None = None
    autoconnect: bool = False
    provenance: ProvenanceConfig = Field(default_factory=ProvenanceConfig)
    idle: IdleConfig | None = None
    startup_commands: tuple[str, ...] = ()


class WorldsConfig(StrictModel):
    schema_url: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1] = 1
    defaults: WorldDefaults = Field(default_factory=WorldDefaults)
    worlds: dict[str, WorldConfig] = Field(default_factory=dict)


class ProviderConfig(StrictModel):
    base_url: AnyHttpUrl
    api_key: SecretStr


class TriggerConfig(StrictModel):
    speech: bool = True
    pages: bool = True
    manual: bool = True


class AgentContextConfig(StrictModel):
    maximum_events: PositiveInt = 100
    maximum_characters: int = Field(default=32_000, ge=1_000)
    include_recent_look: bool = True


class AgentLimitsConfig(StrictModel):
    maximum_messages_per_minute: PositiveInt = 6
    minimum_seconds_between_turns: float = Field(default=3.0, ge=0)


AgentAction = Literal["say", "pose", "page"]


class AgentConfig(StrictModel):
    world: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    triggers: TriggerConfig = Field(default_factory=TriggerConfig)
    allowed_actions: tuple[AgentAction, ...] = ("say", "pose", "page")
    context: AgentContextConfig = Field(default_factory=AgentContextConfig)
    limits: AgentLimitsConfig = Field(default_factory=AgentLimitsConfig)


class AgentsConfig(StrictModel):
    schema_url: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1] = 1
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def providers_exist(self) -> AgentsConfig:
        worlds: dict[str, str] = {}
        for name, agent in self.agents.items():
            if agent.provider not in self.providers:
                raise ValueError(f"agent {name!r} references unknown provider {agent.provider!r}")
            if agent.world in worlds:
                previous = worlds[agent.world]
                raise ValueError(
                    f"agents {previous!r} and {name!r} share world {agent.world!r}; "
                    "each agent requires a dedicated world"
                )
            worlds[agent.world] = name
        return self


class ConfigurationBundle(StrictModel):
    main_path: Path
    worlds_path: Path
    agents_path: Path
    main: MainConfig
    worlds: WorldsConfig
    agents: AgentsConfig
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def agent_worlds_exist(self) -> ConfigurationBundle:
        for name, agent in self.agents.agents.items():
            if agent.world not in self.worlds.worlds:
                raise ValueError(f"agent {name!r} references unknown world {agent.world!r}")
        return self


class UiConfiguration(StrictModel):
    main_path: Path
    main: MainConfig


def default_config_directory() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".config"
    return base / "tfr"


def default_config_path() -> Path:
    return default_config_directory() / "config.jsonc"


def _resolve_path(path: Path, *, relative_to: Path | None = None) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() and relative_to is not None:
        expanded = relative_to / expanded
    return expanded.resolve()


def _read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        with path.open(encoding="utf-8") as config_file:
            data = jsonc.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {path}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc

    try:
        return model_type.model_validate(data)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            errors.append(f"{location}: {error['msg']}")
        raise ConfigurationError(f"invalid {path}: {'; '.join(errors)}") from exc


def _resolve_world_paths(worlds: WorldsConfig, *, relative_to: Path) -> WorldsConfig:
    resolved_worlds: dict[str, WorldConfig] = {}
    for name, world in worlds.worlds.items():
        tls = world.tls
        if tls.ca_file is not None:
            tls = tls.model_copy(
                update={"ca_file": _resolve_path(tls.ca_file, relative_to=relative_to)}
            )
            world = world.model_copy(update={"tls": tls})
        resolved_worlds[name] = world
    return worlds.model_copy(update={"worlds": resolved_worlds})


def credential_permission_warning(path: Path, *, contains_credentials: bool) -> str | None:
    if not contains_credentials or os.name != "posix":
        return None

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return f"could not check permissions on {path}: {exc}"

    if mode & 0o077:
        return (
            f"{path} contains credentials and has mode {mode:04o}; "
            "use 0600 so only its owner can access it"
        )
    return None


def load_ui_configuration(main_path: Path | str | None = None) -> UiConfiguration:
    resolved_main = _resolve_path(Path(main_path) if main_path else default_config_path())
    main = _read_model(resolved_main, MainConfig)
    main = main.model_copy(
        update={
            "logging": main.logging.model_copy(
                update={
                    "directory": _resolve_path(
                        main.logging.directory,
                        relative_to=resolved_main.parent,
                    )
                }
            ),
            "plugins": main.plugins.model_copy(
                update={
                    "state_directory": _resolve_path(
                        main.plugins.state_directory,
                        relative_to=resolved_main.parent,
                    )
                }
            ),
        }
    )
    return UiConfiguration(main_path=resolved_main, main=main)


def load_configuration(main_path: Path | str | None = None) -> ConfigurationBundle:
    ui_configuration = load_ui_configuration(main_path)
    resolved_main = ui_configuration.main_path
    main = ui_configuration.main
    worlds_path = _resolve_path(main.worlds_file, relative_to=resolved_main.parent)
    agents_path = _resolve_path(main.agents_file, relative_to=resolved_main.parent)
    worlds = _resolve_world_paths(
        _read_model(worlds_path, WorldsConfig), relative_to=worlds_path.parent
    )
    agents = _read_model(agents_path, AgentsConfig)

    warning_values = (
        credential_permission_warning(
            worlds_path,
            contains_credentials=any(world.login is not None for world in worlds.worlds.values()),
        ),
        credential_permission_warning(
            agents_path,
            contains_credentials=bool(agents.providers),
        ),
    )

    try:
        return ConfigurationBundle(
            main_path=resolved_main,
            worlds_path=worlds_path,
            agents_path=agents_path,
            main=main,
            worlds=worlds,
            agents=agents,
            warnings=tuple(warning for warning in warning_values if warning),
        )
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            errors.append(f"{location}: {error['msg']}")
        raise ConfigurationError(f"invalid configuration references: {'; '.join(errors)}") from exc
