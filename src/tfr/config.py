from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import jsonc
import regex as safe_regex
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    ValidationError,
    field_validator,
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


HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


class ThemeColorsConfig(StrictModel):
    background: HexColor | None = None
    surface: HexColor | None = None
    overlay: HexColor | None = None
    text: HexColor | None = None
    muted: HexColor | None = None
    accent: HexColor | None = None
    secondary: HexColor | None = None
    info: HexColor | None = None
    success: HexColor | None = None
    warning: HexColor | None = None
    error: HexColor | None = None
    selection: HexColor | None = None
    selected_text: HexColor | None = None


class ThemeConfig(StrictModel):
    preset: Literal[
        "default",
        "catppuccin-latte",
        "catppuccin-frappe",
        "catppuccin-macchiato",
        "catppuccin-mocha",
        "gruvbox",
        "tokyo-night",
        "dracula",
        "nord",
        "solarized-dark",
        "nightfly",
        "kanagawa",
        "1976",
    ] = "default"
    colors: ThemeColorsConfig = Field(default_factory=ThemeColorsConfig)


class SpellcheckConfig(StrictModel):
    enabled: bool = False
    protected_words: tuple[str, ...] = Field(default=(), max_length=1_000)
    worlds: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_protected_words(self) -> SpellcheckConfig:
        groups = (self.protected_words, *self.worlds.values())
        if sum(len(group) for group in groups) > 5_000:
            raise ValueError("spellcheck cannot define more than 5000 protected words")
        for group in groups:
            for word in group:
                if (
                    not word.strip()
                    or len(word) > 64
                    or any(character.isspace() for character in word)
                ):
                    raise ValueError(
                        "spellcheck protected words must be 1-64 characters without whitespace"
                    )
        return self


class TypingGlowConfig(StrictModel):
    enabled: bool = True
    duration_seconds: float = Field(default=0.5, gt=0, le=60)
    highlight_color: HexColor = "#ffffff"
    bold: bool = True


class SpeakingStreaksConfig(StrictModel):
    enabled: bool = True


class UiConfig(StrictModel):
    scrollback_lines: PositiveInt = 20_000
    recent_input_lines: int = Field(default=3, ge=0, le=20)
    mouse_mode: Literal["auto", "terminal", "tfr"] = "tfr"
    output_color: HexColor | None = None
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    spellcheck: SpellcheckConfig = Field(default_factory=SpellcheckConfig)
    typing_glow: TypingGlowConfig = Field(default_factory=TypingGlowConfig)
    speaking_streaks: SpeakingStreaksConfig = Field(default_factory=SpeakingStreaksConfig)
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


_STABLE_UPDATE_MANIFEST_URL = (
    "https://github.com/Blaag/tfr/releases/latest/download/update-manifest.json"
)
_CANDIDATE_UPDATE_MANIFEST_URL = "https://api.github.com/repos/Blaag/tfr/releases?per_page=20"


class UpdateConfig(StrictModel):
    enabled: bool = True
    channel: Literal["stable", "candidate"] = "stable"
    manifest_url: AnyHttpUrl = Field(
        default=AnyHttpUrl(_STABLE_UPDATE_MANIFEST_URL),
        json_schema_extra={"pattern": "^https://"},
    )
    check_interval_seconds: float = Field(default=21_600.0, ge=300)
    initial_delay_seconds: float = Field(default=3.0, ge=0)
    jitter_seconds: float = Field(default=900.0, ge=0)
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    state_directory: Path = Path("~/.local/state/tfr/updates")

    @model_validator(mode="before")
    @classmethod
    def channel_manifest_default(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("channel") == "candidate"
            and "manifest_url" not in value
        ):
            value = {**value, "manifest_url": _CANDIDATE_UPDATE_MANIFEST_URL}
        return value

    @field_validator("manifest_url")
    @classmethod
    def manifest_uses_https(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("updates.manifest_url must use HTTPS")
        if value.username is not None or value.password is not None:
            raise ValueError("updates.manifest_url cannot contain credentials")
        return value


class WebGatewayConfig(StrictModel):
    enabled: bool = False
    mode: Literal["on-demand", "disabled"] = "on-demand"
    # "tailscale" (default): the backend binds loopback and Tailscale Serve
    # provides the HTTPS origin and the Tailscale-User-Login identity header.
    # "direct": the Gateway terminates TLS itself and binds a specific public
    # or private address, for a cloud-hosted Gateway that does not use
    # Tailscale. Direct mode replaces the Serve identity binding with
    # possession of the per-device bearer credential over authenticated TLS.
    transport: Literal["tailscale", "direct"] = "tailscale"
    origin: AnyHttpUrl | None = None
    # The local interface the PWA listener binds. In tailscale transport this
    # must be loopback (127.0.0.1 or ::1), since Tailscale Serve fronts it. In
    # direct transport it is the real, bindable interface address (for example
    # a private VPC IP) or a wildcard; it is NOT the public/NAT address, which
    # cannot be bound. The browser-facing address is direct_host.
    listen_host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    listen_port: int = Field(default=7348, ge=1, le=65_535)
    # Direct-mode only: the externally reachable host (DNS name recommended so
    # the browser can verify the TLS certificate) the PWA is advertised on and
    # the TLS key pair the Gateway presents. This is used only for the origin
    # and certificate matching, never to bind the socket. The port defaults to
    # listen_port when omitted.
    direct_host: str | None = Field(default=None, min_length=1, max_length=253)
    direct_port: int | None = Field(default=None, ge=1, le=65_535)
    tls_cert: Path | None = None
    tls_key: Path | None = None
    state_directory: Path = Path("~/.local/state/tfr/web")
    snapshot_events: int = Field(default=2_000, ge=1, le=5_000)

    @model_validator(mode="after")
    def valid_browser_origin(self) -> WebGatewayConfig:
        if self.mode == "disabled" and self.enabled:
            raise ValueError("web_gateway.enabled cannot be true when web_gateway.mode is disabled")
        if self.transport == "direct":
            return self._validate_direct()
        # Tailscale transport: direct-only fields must not be set.
        for field_name in ("direct_host", "direct_port", "tls_cert", "tls_key"):
            if getattr(self, field_name) is not None:
                raise ValueError(
                    f"web_gateway.{field_name} requires web_gateway.transport to be 'direct'"
                )
        if self.listen_host not in {"127.0.0.1", "::1"}:
            raise ValueError(
                "web_gateway.listen_host must be 127.0.0.1 or ::1 in tailscale transport"
            )
        if self.enabled and self.origin is None:
            raise ValueError("web_gateway.origin is required when the web gateway is enabled")
        if self.origin is None:
            return self
        self._validate_https_origin(str(self.origin))
        return self

    def _validate_direct(self) -> WebGatewayConfig:
        if self.origin is not None:
            raise ValueError(
                "web_gateway.origin must not be set in direct transport; it is derived "
                "from web_gateway.direct_host and direct_port"
            )
        if self.direct_host is None:
            raise ValueError("web_gateway.transport 'direct' requires web_gateway.direct_host")
        # tls_cert and tls_key are optional here: when omitted, direct mode
        # falls back to the Gateway's own --tls-cert/--tls-key. That fallback is
        # resolved and enforced where the listener is wired, because the CLI
        # arguments are not visible to this model. If one of the pair is given,
        # the other must be too.
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError(
                "web_gateway.tls_cert and web_gateway.tls_key must be set together"
            )
        host = str(self.direct_host).strip()
        if not host:
            raise ValueError("web_gateway.direct_host cannot be empty")
        if host in {"0.0.0.0", "::", "*"}:
            raise ValueError("web_gateway.direct_host must not be a wildcard address")
        if "/" in host or "@" in host or ":" in host:
            raise ValueError(
                "web_gateway.direct_host must be a bare hostname or IP without scheme or port"
            )
        # Deriving and re-validating the origin ensures the same HTTPS rules
        # that Tailscale-mode origins must satisfy.
        self._validate_https_origin(self.canonical_origin)
        return self

    @staticmethod
    def _validate_https_origin(origin: str) -> None:
        parsed = urlsplit(origin)
        if parsed.scheme != "https":
            raise ValueError("web_gateway.origin must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("web_gateway.origin cannot contain credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("web_gateway.origin must not contain a path, query, or fragment")

    @property
    def canonical_origin(self) -> str | None:
        if self.transport == "direct":
            if self.direct_host is None:
                return None
            port = self.direct_port if self.direct_port is not None else self.listen_port
            host = str(self.direct_host).strip()
            authority = host if port == 443 else f"{host}:{port}"
            return f"https://{authority}"
        return str(self.origin).rstrip("/") if self.origin is not None else None

    @property
    def direct_bind(self) -> tuple[str, int] | None:
        """The (host, port) the direct-mode PWA listener binds locally.

        The host is ``listen_host`` (a real, bindable interface such as a
        private VPC IP, or a wildcard), NOT ``direct_host`` — the public/NAT
        address a browser connects to usually cannot be bound. The port is the
        same ``direct_port`` the origin advertises.
        """
        if self.transport != "direct" or self.direct_host is None:
            return None
        port = self.direct_port if self.direct_port is not None else self.listen_port
        return self.listen_host.strip(), port


class ManagedSettingsConfig(StrictModel):
    state_directory: Path = Path("~/.local/state/tfr/settings")


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
    policy: Literal["legacy", "pinned", "stable-auto", "stable-notify"] = "legacy"
    manifest_url: AnyHttpUrl | None = None

    @field_validator("repo")
    @classmethod
    def repo_has_no_embedded_credentials(cls, value: str) -> str:
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.password is not None or (
                parsed.scheme in {"http", "https"} and parsed.username is not None
            ):
                raise ValueError("plugin repository URLs cannot contain credentials")
        return value

    @field_validator("path")
    @classmethod
    def normalize_source_path(cls, value: str) -> str:
        return PurePosixPath(value or ".").as_posix()

    @model_validator(mode="after")
    def validate_policy(self) -> PluginSource:
        full_commit = self.ref is not None and re.fullmatch(r"[0-9a-f]{40}", self.ref) is not None
        if self.policy == "pinned":
            if not full_commit:
                raise ValueError("pinned plugin sources require a full lowercase 40-character ref")
            if self.auto_update or self.manifest_url is not None:
                raise ValueError("pinned plugin sources cannot auto-update or use a manifest")
        elif self.policy in {"stable-auto", "stable-notify"}:
            if self.ref is not None or self.auto_update or self.manifest_url is None:
                raise ValueError(
                    "stable plugin sources require manifest_url and cannot set ref or auto_update"
                )
            if self.manifest_url.scheme != "https":
                raise ValueError("plugin source manifest_url must use HTTPS")
            if self.manifest_url.username is not None or self.manifest_url.password is not None:
                raise ValueError("plugin source manifest_url cannot contain credentials")
        elif self.manifest_url is not None:
            raise ValueError("legacy plugin sources cannot use manifest_url")
        return self


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
    updates: UpdateConfig = Field(default_factory=UpdateConfig)
    web_gateway: WebGatewayConfig = Field(default_factory=WebGatewayConfig)
    managed_settings: ManagedSettingsConfig = Field(default_factory=ManagedSettingsConfig)
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


class WorldCapabilitiesConfig(StrictModel):
    unicode: bool = False


class IdleConfig(StrictModel):
    after_seconds: PositiveInt
    command: str = Field(min_length=1)


class GagConfig(StrictModel):
    patterns: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("patterns")
    @classmethod
    def valid_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(patterns)) != len(patterns):
            raise ValueError("gag patterns must be unique")
        for pattern in patterns:
            if not pattern or len(pattern) > 512 or not pattern.isprintable():
                raise ValueError("gag patterns must contain 1-512 printable characters")
            try:
                safe_regex.compile(pattern, safe_regex.IGNORECASE)
            except safe_regex.error as exc:
                raise ValueError(f"invalid gag regular expression: {exc}") from exc
        return patterns


WorldSwitchAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    ),
]


class WorldConfig(StrictModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65_535)
    aliases: tuple[WorldSwitchAlias, ...] = Field(default=(), max_length=32)
    server: Literal["bare", "generic", "rhost", "tinymush", "tinymux"] = "generic"
    encoding: str | None = None
    reconnect: bool | None = None
    scrollback_lines: PositiveInt | None = None
    tls: TlsConfig = Field(default_factory=TlsConfig)
    login: LoginConfig | None = None
    autoconnect: bool = False
    provenance: ProvenanceConfig = Field(default_factory=ProvenanceConfig)
    capabilities: WorldCapabilitiesConfig = Field(default_factory=WorldCapabilitiesConfig)
    idle: IdleConfig | None = None
    gag: GagConfig = Field(default_factory=GagConfig)
    startup_commands: tuple[str, ...] = ()

    @field_validator("aliases")
    @classmethod
    def valid_world_switch_aliases(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(alias.casefold() for alias in aliases)


class WorldsConfig(StrictModel):
    schema_url: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1] = 1
    defaults: WorldDefaults = Field(default_factory=WorldDefaults)
    worlds: dict[str, WorldConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def aliases_are_unique(self) -> WorldsConfig:
        owners: dict[str, str] = {}
        for world, config in self.worlds.items():
            for alias in config.aliases:
                previous = owners.get(alias)
                if previous is not None:
                    raise ValueError(
                        f"duplicate world-switch alias {alias!r} for worlds "
                        f"{previous!r} and {world!r}"
                    )
                owners[alias] = world
        if len(owners) > 256:
            raise ValueError("world configuration cannot define more than 256 switch aliases")
        return self


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


def _absolute_path_without_symlink_resolution(
    path: Path, *, relative_to: Path | None = None
) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() and relative_to is not None:
        expanded = relative_to / expanded
    return Path(os.path.abspath(expanded))


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
            "updates": main.updates.model_copy(
                update={
                    "state_directory": _resolve_path(
                        main.updates.state_directory,
                        relative_to=resolved_main.parent,
                    )
                }
            ),
            "web_gateway": main.web_gateway.model_copy(
                update={
                    "state_directory": _resolve_path(
                        main.web_gateway.state_directory,
                        relative_to=resolved_main.parent,
                    )
                }
            ),
            "managed_settings": main.managed_settings.model_copy(
                update={
                    "state_directory": _absolute_path_without_symlink_resolution(
                        main.managed_settings.state_directory,
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
