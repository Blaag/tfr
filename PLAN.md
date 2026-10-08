# TFR Implementation Plan

## Purpose

TFR is a Python terminal client inspired by TinyFugue. It supports concurrent
world connections, a TinyFugue-style pager, ANSI color, Unicode, TLS, semantic
event logging, server-provided message provenance, plugins, and independently
connected LLM characters.

The installed command is `tfr`.

## Product Decisions

- Use Python with `uv` for project and dependency management.
- Use `prompt_toolkit` for the terminal UI.
- Keep networking and sessions independent from the TUI.
- Support multiple active worlds in one process.
- Keep the operator-facing `tfr gateway` command and systemd service stable
  while an internal lightweight supervisor/connector owns world transports and
  restarts the full Gateway application independently.
- Log the complete session by default, not only conversation.
- Preserve canonical inbound text separately from its display projection.
- Enable and parse server NOSPOOF provenance where supported.
- Use JSONC for all user configuration.
- Permit plaintext world passwords and model API keys in JSONC files.
- Require sensitive configuration files to have restrictive permissions.
- Use append-only JSONL as the initial durable event store.
- Defer MongoDB versus DuckDB until real query and volume requirements exist.
- Support OpenAI and local Ollama models through an OpenAI-compatible API.
- Give an LLM its own world identity and connection. It never speaks through
  the human user's character.
- Do not implement a client lock in the first release.
- Use TinyFugue as design inspiration without copying GPLv2 implementation
  code.

## Initial Scope

### Connections

- Concurrent TCP world connections.
- Direct TLS with hostname and certificate verification enabled by default
  whenever TLS is configured.
- Incremental Telnet negotiation and text decoding.
- UTF-8 by default, with a per-world encoding override.
- Configurable reconnect behavior.
- Configurable per-world application-level idle command.
- Per-world startup commands.
- Automatic login when credentials are configured.
- Automatic NOSPOOF enablement after configured login when supported by the
  selected server adapter.

### Terminal UI

- Full-screen `prompt_toolkit` application.
- Output, status, and editable input regions.
- World switching without disconnecting inactive worlds.
- Independent command history and draft input per world.
- Bounded rendered scrollback per world.
- Unread counters for inactive worlds.
- ANSI-safe styled output.
- Correct combining-character and wide-character display behavior.
- TinyFugue-style paging based on newly received display rows.
- A visible `More` state that does not jump to the end while paging or while
  the user has scrolled back.
- Agent worlds shown alongside human worlds with an unmistakable agent marker.
- An agent inspector showing model context, request, response, validation,
  accepted or rejected action, and transmitted command.
- Immediate pause and resume controls for each agent.

### Logging

Complete session logging is enabled by default.

- Log outbound commands such as `look`, `say`, and movement.
- Log all inbound text, including room descriptions, errors, prompts, and
  system output.
- Add richer semantic fields to recognized says, poses, pages, emits, and
  other classified events.
- Redact passwords, API keys, authorization headers, and sensitive login
  commands.
- Record agent prompts, selected context, returned output, validation results,
  actions, and commands.
- Include world, session, actor, direction, timestamp, sequence number,
  parser, confidence, and correlation metadata.
- Retain the original ANSI/NOSPOOF-bearing canonical text even when the UI
  hides the NOSPOOF prefix.

JSONL is event data, not configuration, and therefore remains JSONL rather
than JSONC.

### Plugins

Plugins are trusted local Python packages discovered through
`importlib.metadata` entry points. The first plugin API supports:

- Client commands.
- Inbound parsers and semantic enrichers.
- Display transformations.
- Status-line segments.
- Key bindings.
- Session and application lifecycle events.

Plugins submit typed command requests through the command bus. They do not
write directly to sockets or mutate canonical events.

## Non-Goals For The First Release

- Taking over or replying through the human user's character with an LLM.
- General shell, filesystem, credential, or arbitrary network tools for LLMs.
- Arbitrary world commands from LLMs.
- A client lock screen.
- Embedded database selection or migration.
- Copying TinyFugue source code or macro implementations.
- Training, loading, or managing LoRA adapters. TFR selects a model exposed by
  Ollama or another configured model server.

## Architecture

The TUI and PWA are consumers and producers around the Gateway application
core. They do not own world sockets, connection tasks, event history, or model
calls. In split Gateway deployments, a lightweight supervisor/connector owns
all world sockets while the replaceable Gateway application owns protocol
projection, plugins, agents, UI clients, and web access.

```text
 systemd: tfr gateway
          |
 +--------v--------------------------+
 | lightweight supervisor/connector  |
 | TCP/TLS/Telnet, idle, replay      |
 +--------+--------------------------+
          | private owner-only Unix socket
 +--------v--------------------------+       +-------------------+
 | replaceable Gateway application   |<----->| native UIs / PWA  |
 | command bus, event bus, plugins,  |       +-------------------+
 | agents, history, web gateway      |
 +--------+--------------------------+
          |
          +-------------------------------> JSONL/plugin sinks

 connector TCP/TLS sockets
          |
     remote worlds
```

The connector is one stdlib-focused process for all worlds, not one process per
world and not a separately installed service. It stays in the existing systemd
control group, survives Gateway application replacement for up to 30 minutes,
and exits immediately on a clean service shutdown. Its exact implementation
hash is the compatibility boundary: matching hashes preserve connections;
different hashes trigger one controlled connector replacement rather than a
version-compatibility matrix.

### Inbound Flow

1. The world connector reads bytes from TCP or TLS.
2. Its Telnet codec consumes negotiation bytes and emits application bytes.
3. Its incremental decoder produces framed canonical text without losing partial
   multibyte characters.
4. Connector frames receive per-world transport sequence numbers and remain in
   bounded replay storage until acknowledged by the Gateway application.
5. The selected server adapter parses NOSPOOF and other server-specific
   provenance.
6. Semantic classifiers add event type and confidence without overwriting
   source data.
7. The canonical event is assigned a per-session sequence number and
   published to the event bus.
8. Event sinks append the canonical event to JSONL.
9. A display projector optionally removes a successfully parsed NOSPOOF
   prefix and produces styled output for the TUI.
10. Agent context builders receive selected, sanitized events from only their
   configured world session.

If bounded replay overflows while the Gateway is unavailable or slow, the
connector drops only complete oldest frames and sends a persistent structured
gap notice containing world, sequence range, frame count, and byte count. The
Gateway logs and visibly displays that notice before acknowledging it.

### Outbound Flow

1. Human input, a plugin, a startup action, an idle action, or an agent creates
   a typed command request.
2. The command request identifies its source actor and target session.
3. Agent policy validates agent actions before command rendering.
4. A redacted audit projection is written to JSONL.
5. The Gateway session sends the validated command over the private connector
   protocol, and the connector serializes it to the world socket.
6. Correlation metadata links the request, outbound event, model decision,
   and resulting inbound traffic where possible.

No producer writes directly to a socket.

## Core Models

Use frozen dataclasses and narrow protocols unless runtime schema validation
is required.

### Event

A canonical event contains at least:

- Event ID.
- Session ID and configured world alias.
- Connection generation, so reconnects are distinguishable.
- Per-session monotonic sequence number.
- UTC wall-clock timestamp and monotonic receive time.
- Direction: inbound, outbound, or internal.
- Event kind: raw output, command, say, pose, page, emit, system, connection,
  Telnet, agent request, agent response, agent action, or plugin event.
- Canonical decoded text, including ANSI and NOSPOOF text.
- Plain visible text with ANSI controls removed.
- Optional display projection.
- Optional structured provenance.
- Parser name, parser version, and confidence.
- Actor metadata for human, agent, plugin, startup, idle, or system sources.
- Correlation and causation IDs.
- Redaction marker where applicable.

### Provenance

Provenance fields are optional and independent:

- Sender name or moniker.
- Sender dbref.
- Owner name or moniker.
- Owner dbref when supplied by a server format.
- Enactor dbref.
- Server source class.
- Parsed prefix span.
- Adapter and confidence.

Missing fields remain unknown. The client does not infer a stronger claim
than the server format provides.

### Command Request

A command request contains:

- Target session.
- Source actor type and ID.
- Unmodified command text.
- Sensitivity classification.
- Correlation and causation IDs.
- Optional agent action metadata.

### Session

A session represents one authenticated connection identity. Human and agent
sessions use the same connection implementation but have different
controllers. Two aliases may connect to the same host with different
credentials, for example `example-me` and `example-agent`.

## Server Adapters And NOSPOOF

Start with these adapters:

- Generic Telnet/MUD fallback.
- RhostMUSH.
- TinyMUX.

PennMUSH-specific detailed parsing can be added only after confirmed fixtures
or source behavior are available. Generic parsing must never present inferred
provenance as authoritative.

### RhostMUSH

Known full prefix form:

```text
[<sender-name>(#<sender-dbref>){<owner-name>}<-(#<enactor-dbref>)] <message>
```

Owner and enactor portions are conditional. Preserve the prefix canonically
and remove it only in the display projection when configured.

### TinyMUX

Enable with:

```text
@set me=NOSPOOF
```

Full prefix grammar:

```text
[<sender-moniker>(#<sender-dbref>){<owner-moniker>}<-(#<enactor-dbref>),<source>] <message>
```

The owner segment appears only when sender and owner differ. The enactor
segment appears only when sender differs from the current enactor. Source is
one of `comsys`, `kill`, `give`, `page`, or `saypose`, or is absent.

When server-wide `terse_nospoof` is enabled, say and pose output uses:

```text
[#<sender-dbref>] <message>
```

Parsing rules:

- Parse an ANSI-stripped visible projection while retaining raw text and
  styled spans.
- Treat sender dbref as high-confidence server provenance.
- Treat owner, enactor, and source as optional independent fields.
- Map `saypose` only to a broad speech class, not specifically say or pose.
- Do not infer `@emit` from an absent source.
- Do not claim `@force` detection. An enactor can show a different triggering
  object, but the prefix does not identify the command that caused it.
- Treat terse prefixes as provenance-only. Names and exact speech type remain
  unknown unless separately inferred at lower confidence.
- Strip only the exact successfully parsed prefix from the display text.

## Configuration

All user configuration uses JSONC: JSON syntax with `//` and `/* */` comments
and trailing comma support. Use the small `json-with-comments` package rather
than a handwritten comment stripper.

The default configuration directory is `$XDG_CONFIG_HOME/tfr` when
`XDG_CONFIG_HOME` is set, and `~/.config/tfr` otherwise. It contains:

- `config.jsonc`
- `worlds.jsonc`
- `agents.jsonc`

`config.jsonc` is the entry point and refers to the other files. Relative
paths are resolved relative to the file containing them.

Configuration precedence is:

1. Built-in defaults.
2. Main client configuration.
3. World defaults.
4. Individual world or agent configuration.
5. Explicit CLI arguments.

The client does not rewrite configuration files, preserving comments and
formatting. Unknown keys are errors except inside explicitly plugin-owned
configuration objects. Validation errors identify the file and configuration
path, and line and column where the parser can supply them.

The repository will contain commented example files and JSON Schemas for
editor completion and validation.

### Main Configuration Example

```jsonc
{
  "schema_version": 1,
  "worlds_file": "worlds.jsonc",
  "agents_file": "agents.jsonc",

  "ui": {
    "scrollback_lines": 20000,
    "show_nospoof_prefix": false,
    "pager": {
      "enabled": true,
      "overlap_lines": 1
    }
  },

  "logging": {
    "enabled": true,
    "mode": "all",
    "directory": "~/.local/state/tfr/logs"
  },

  "plugins": {
    "enabled": [],
    "config": {}
  }
}
```

### Worlds Configuration Example

```jsonc
{
  "schema_version": 1,

  "defaults": {
    "encoding": "utf-8",
    "reconnect": true,
    "scrollback_lines": 20000
  },

  "worlds": {
    "example-me": {
      "aliases": ["me"],
      "host": "mux.example.org",
      "port": 4201,
      "server": "tinymux",

      "tls": {
        "enabled": true,
        "verify": true
      },

      "login": {
        "character": "Alice",
        "password": "secret"
      },

      "autoconnect": true,

      "provenance": {
        "nospoof": true,
        "show_prefix": false
      },

      "idle": {
        "after_seconds": 300,
        "command": "IDLE"
      },

      "startup_commands": []
    }
  }
}
```

Idle commands are disabled when the command is absent. TFR does not assume
that every server implements the same application-level no-op command.

### Agents Configuration Example

```jsonc
{
  "schema_version": 1,

  "providers": {
    "openai": {
      "base_url": "https://api.openai.com/v1",
      "api_key": "secret"
    },

    "ollama": {
      "base_url": "http://localhost:11434/v1",
      "api_key": "ollama"
    }
  },

  "agents": {
    "example-bot": {
      "world": "example-agent-world",
      "provider": "ollama",
      "model": "my-character-lora",
      "system_prompt": "Play this character consistently...",

      "triggers": {
        "speech": true,
        "pages": true,
        "manual": true
      },

      "allowed_actions": [
        "say",
        "pose",
        "page"
      ],

      "context": {
        "maximum_events": 100,
        "include_recent_look": true
      },

      "limits": {
        "maximum_messages_per_minute": 6,
        "minimum_seconds_between_turns": 3
      }
    }
  }
}
```

### Configuration Security

- Create files containing credentials with mode `0600` on POSIX systems.
- Warn when a credential-bearing file is readable by group or other users.
- Never print complete loaded configuration objects.
- Redact passwords, API keys, login commands, and authorization headers from
  logs and errors.
- Never put world credentials or model API keys into model context.
- Keep real outbound text separate from its redacted audit projection.

## LLM Agent Design

### Provider Interface

Define a narrow asynchronous provider protocol. The first implementation uses
the OpenAI-compatible `/v1/chat/completions` API with a configurable base URL,
API key, and model name. This works with OpenAI and Ollama while leaving room
for later provider adapters.

Do not depend on server-side conversational state. TFR builds each request
from its own selected event context, which is compatible with local and
hosted models and makes the exact model input auditable.

### Structured Action

Request a JSON Schema-constrained response and validate it locally. One model
turn produces at most one action:

```json
{
  "action": "say",
  "text": "Hello there.",
  "target": null
}
```

Allowed values are `say`, `pose`, `page`, and `none`. `target` is required only
for `page`. Invalid, oversized, disallowed, or rate-limited actions are logged
and rejected rather than repaired into commands silently.

### Context

- Context is selected only from the agent's configured session.
- ANSI controls and rendered NOSPOOF decoration are removed from model text.
- Parsed speaker and provenance fields are supplied separately.
- Recent conversation and pages are included by default.
- The most recent useful `look` output may be included as environmental
  context but does not trigger a turn.
- Context is bounded by configured event count and provider token limits.
- Every selected event ID and the exact submitted prompt are audited.
- World text is clearly delimited as untrusted content.

### Triggering And Loop Prevention

- Trigger on attributed speech from another speaker.
- Trigger on pages directed to the agent.
- Allow a manual turn from the inspector.
- Do not trigger merely because a room description or system line arrived.
- Batch bursts of related inbound lines before one model call.
- Ignore confidently identified self output.
- Apply a cooldown and per-minute rate limit.
- Permit only one in-flight model call per agent.
- Cancel or disregard stale calls after disconnect, pause, or connection
  generation change.
- Log ambiguous self-attribution and suppress rapid feedback loops.

### Agent Inspector

The inspector exposes observable data, not hidden chain-of-thought:

- Selected event context.
- Constructed system and user messages.
- Provider, base URL identity without credentials, model, and request ID.
- Returned content and any provider-supplied reasoning summary.
- Parsed structured action.
- Validation or policy rejection.
- Rendered world command.
- Agent state, cooldown, queue, and current request.

Operator commands sent manually through an agent world are attributed to the
human operator, not to the model.

## Paging And Buffer Ownership

Canonical events do not live in `prompt_toolkit` buffers. Each world owns a
bounded application-level display model. The UI renders a viewport over that
model.

Paging state tracks:

- Current top display row.
- Last acknowledged display row.
- Rows added while paging or scrolled back.
- Terminal width and height used for wrapping.
- Unread event and display-row counts.

Resizing recalculates wrapped display rows from retained styled lines without
changing canonical events. New output follows automatically only when the
viewport was already following the end and the pager has not stopped it.

## Storage Boundary

Define an asynchronous `EventSink` protocol with JSONL as the only initial
implementation. The rest of the application publishes events without knowing
whether a later sink is DuckDB, MongoDB, or something else.

Use one append-only session log or a documented per-session layout. Flush
connection, credential-redacted login, agent action, and shutdown events
promptly. A crash may lose only a small bounded buffer of ordinary events.

Do not introduce a database abstraction beyond the narrow event sink until a
second real sink exists.

## Suggested Package Layout

Keep the initial layout small and split modules only at behavioral boundaries:

```text
src/tfr/
  __init__.py
  cli.py
  config.py
  events.py
  core.py
  sessions.py
  telnet.py
  adapters.py
  eventlog.py
  plugins.py
  agents.py
  tui.py
  pager.py
```

If adapters or TUI components become large, convert those modules into
packages then. Do not begin with deep package nesting.

## Dependencies

Keep runtime dependencies narrow:

- `prompt-toolkit` for terminal input, layout, styling, and Unicode-aware
  rendering.
- `json-with-comments` for JSONC parsing.
- `openai` for asynchronous OpenAI-compatible model requests.
- `pydantic` for configuration and structured model response validation.

Use the standard library for asyncio networking, TLS, dataclasses, logging,
JSONL writing, entry-point discovery, paths, and ring buffers where practical.

Development dependencies:

- `pytest`
- `pytest-asyncio`
- `ruff`

Resolve and lock current compatible versions with `uv` during project
scaffolding rather than hard-coding versions in this plan.

## Implementation Phases

### Phase 1: Project Foundation

Status: complete.

- Initialize a packaged `uv` application with a `src/` layout and `tfr` entry
  point.
- Add Ruff and pytest configuration.
- Implement JSONC loading, path resolution, schema models, precedence, and
  permission warnings.
- Add commented example configuration without real credentials.
- Define canonical event, provenance, command request, actor, and session ID
  models.
- Implement redaction primitives and JSONL event serialization.

Acceptance criteria:

- `uv run tfr --help` succeeds.
- Valid example JSONC loads into typed models.
- Invalid and unknown keys produce useful errors.
- Credential file permissions are checked.
- Sensitive fields never appear in serialized audit fixtures.

### Phase 2: Connection Core

Status: complete.

- Implement world session lifecycle and command bus.
- Implement TCP and verified direct TLS connections.
- Implement incremental Telnet negotiation and text decoding.
- Add login, startup, NOSPOOF enablement, idle, reconnect, and clean shutdown.
- Publish all connection and traffic events independently of the TUI.

Acceptance criteria:

- Two fake worlds exchange concurrent traffic without cross-session leakage.
- Fragmented Telnet sequences and fragmented UTF-8 decode correctly.
- TLS trust and hostname failures are surfaced and logged safely.
- Login passwords are transmitted but redacted from logs.
- Idle timers reset on outbound activity and stop on disconnect.

### Phase 3: Provenance And Classification

Status: complete.

- Add generic, RhostMUSH, and TinyMUX adapters.
- Parse ANSI-bearing full and terse NOSPOOF fixtures.
- Add conservative semantic classification and confidence.
- Implement display projection with optional prefix hiding.

Acceptance criteria:

- Canonical text remains byte-for-byte equivalent at the decoded text layer.
- Prefix hiding changes only display output.
- Malformed prefixes remain ordinary text.
- TinyMUX source tags and optional fields follow the confirmed server format.
- No parser claims exact command types unsupported by the wire format.

### Phase 4: Interactive TUI

Status: complete.

- Build world list, output viewport, status line, and input editor.
- Add switching, per-world drafts and history, reconnect controls, and unread
  counts.
- Add bounded scrollback and the pager state machine.
- Render ANSI styles and Unicode correctly.

Acceptance criteria:

- Multiple live worlds remain responsive during switching and paging.
- New output does not move a manually scrolled viewport.
- `More` advances predictably by terminal rows with configured overlap.
- Resizing reflows retained output without corrupting state.
- Wide and combining characters keep cursor and status alignment.

### Phase 5: Plugin API

Status: complete.

- Discover plugins through a versioned entry-point group.
- Add command, parser, display, status, key binding, and lifecycle registration.
- Isolate plugin exceptions and report them as internal events.
- Prevent plugins from mutating canonical events or bypassing the command bus
  through supported APIs.

Acceptance criteria:

- A fixture plugin can register each supported extension.
- One failing plugin does not disconnect worlds or stop event logging.
- Duplicate commands and incompatible API versions fail clearly.

### Phase 6: Agent Runtime

Status: complete.

- Implement provider, agent, trigger, context, policy, and rate-limit models.
- Add OpenAI-compatible asynchronous chat completion requests.
- Validate structured outputs for conversation-only actions.
- Add dedicated agent world controllers and model audit events.
- Add inspector, manual trigger, pause, and resume controls.

Acceptance criteria:

- OpenAI and a local Ollama test endpoint use the same provider contract.
- Agent context contains only events from its configured session.
- Prompts and decisions are fully auditable without secrets.
- Invalid model output cannot reach the world socket.
- Agents cannot submit arbitrary commands.
- Pausing prevents queued or in-flight stale actions from being sent.
- Human and agent sessions can connect concurrently to the same server using
  different aliases and credentials.

### Phase 7: Hardening And Documentation

Status: complete.

- Exercise long-running sessions and bounded-memory behavior.
- Add graceful signal handling and crash-safe log flushing.
- Document config, TLS, login security, logging, adapters, plugins, and agents.
- Add transcript replay fixtures for parser and UI debugging.
- Review dependency licenses and distribute the project under the MIT License.

Acceptance criteria:

- Long synthetic sessions remain within configured scrollback bounds.
- Recorded transcripts reproduce parser and display outcomes deterministically.
- Shutdown closes sessions, cancels model calls, and flushes logs.
- `uv run ruff check .` and `uv run pytest` pass.

## Future Work

- [x] Convert `/boss` into a plugin with dynamic display content and an API for
  other components or plugins to emit events into the active boss view.
- [x] Publish gateway- and UI-detected operational activity as plugin-visible
  events, including gateway connection and disconnection, world activity,
  world connection and disconnection, and boss-mode activation.
- [x] Add a per-world last-activity indicator showing the elapsed time since
  inbound input was most recently received.
- [x] Fix screen-clear geometry after `/recall`: clearing after `/recall 10`
  must use the true bottom of the pane rather than the last recalled text row as
  the animation floor.
- [x] Add UI-side gateway connection verification and bounded automatic retry
  after laptop sleep, using configurable or fixed retry counts and intervals so
  `/gateway reconnect` is not normally required.
- [x] Add `/n` and `/p` shortcuts for switching to the next and previous worlds.
- [x] Add configurable world-switch aliases alongside each world in
  `worlds.jsonc`, allowing shortcuts such as `/g` for `grapefruit` and `/j` for
  `juicyfruit`. Validate alias syntax and reject duplicate world aliases;
  detect collisions with core and plugin commands, with system commands always
  taking priority rather than being shadowed by a world alias.
- [ ] Create a `/stats` plugin with per-world received-versus-sent counts, top
  speakers, message histograms, speech-length statistics by speaker, UI output,
  and an explicit option to emit selected statistics to the world.
- [x] Make display-affecting keys such as Page Up and Page Down immediately end
  an active screen-clear animation before performing the requested action.
- [x] Make Tab act as a second Page Down only when the active world's input
  buffer is empty, normal world output is active rather than another panel such
  as the agent inspector, and the output pager has rows below the current view
  (`pager.more_rows > 0`). Reuse the existing Page Down behavior for both paused
  and manually scrolled output, including ending an active screen-clear effect;
  otherwise preserve normal Tab input behavior. Test paused output, manually
  scrolled output, bottom-of-output, nonempty input, and alternate-panel cases.
- [x] Make low-bandwidth mode disable every animation, including screen-clear
  plugin animations. Clearing the screen while `lowbw` is enabled must complete
  immediately without rendering or scheduling animated effect frames.
- [x] Add a configurable input typing-fade effect where each newly typed
  character appears in a bright highlight and independently fades to the normal
  input color over a configurable duration. Preserve cursor movement, selection,
  editing, paste, and Unicode grapheme behavior; render immediately in the normal
  color when animations or low-bandwidth mode are disabled, and keep redraw work
  bounded while typing rapidly.
- [x] Add optional submit-time spell checking controlled by
  `/spellcheck on|off|status`. When the user presses Enter, automatically apply
  only high-confidence corrections before sending, while preserving commands,
  URLs, names, punctuation, and world-specific terms. Briefly animate or
  highlight every corrected word in the matching world-output echo so the
  changes are unmistakable, retain an undo path, and perform correction locally
  without sending draft text to an external service.
- [x] Detect HTTP and HTTPS URLs in chat output, render them underlined and
  clickable, and open them in a new tab through the operating system's default
  browser.
- [x] Add root `README.md` instructions for configuring and running the TFR UI
  on a separate Linux host, complementing the existing remote gateway setup.
- [x] Let `plugins.sources` in `config.jsonc` fetch plugin code directly from a
  Git repository (GitHub shorthand, full URL, or SSH URL), with optional
  branch/tag/commit pinning and optional per-launch `auto_update`, without a
  separate packaging or installation step.
- [x] Notify at UI startup when stable updates are available for the Gateway or
  UI, using a checksummed immutable-release manifest and exact packaged build
  identity without fetching or applying artifacts automatically.
- [x] Add a managed versioned installation layout with isolated release
  environments, exact checkout build identity, atomic activation, a stable
  launcher, retained previous release, and rollback, plus a script that builds
  and installs the current clean Git `HEAD` using `uv.lock`.
- [x] Add a bare `/update` that stages the exact verified stable source release,
  requires every connected managed native UI to prepare successfully, atomically
  activates each UI and the Gateway, and re-execs every participating process.
  Preserve browser service-worker updates and require manual deployment for
  protocol-changing releases.
- [x] Add stable and candidate update channels. Publish immutable numbered
  `vMAJOR.MINOR.PATCH-candidate.N` prereleases for opt-in testing, isolate
  candidate and stable caches, and require stable publication to promote the
  exact candidate wheel and source archive from the same commit without
  rebuilding either artifact.
- [x] Keep world connections alive across normal Gateway updates and failed
  Gateway application restarts. Run one lightweight internal connector beneath
  the unchanged `tfr gateway` service; retain it for 30 minutes after an
  unexpected application loss; preserve TCP/TLS/Telnet state, reconnect policy,
  login/startup and idle behavior; and replay acknowledged, per-world sequenced
  frames through bounded 1 MiB-per-world and 16 MiB-global buffers. Surface any
  dropped range as a visible, logged overflow warning.
- [x] Use an exact connector implementation fingerprint rather than a release
  compatibility matrix. Reuse the connector only when the new Gateway expects
  the same connector/Telnet implementation hash; otherwise perform one explicit
  controlled connector replacement and world reconnect.
- [x] Improve coordinated-update diagnostics by resolving `uv` independently
  on each participating host, identifying Gateway-versus-UI staging failures,
  and keeping PWA/native UI update and spelling behavior distinct.
- [ ] Add direct stable-release artifact installation that verifies artifact
  size and SHA-256 before activating, without rebuilding the verified tag.
- [x] Include stable Git-sourced plugin releases in `/update status|check` and
  the UI's periodic update schedule without applying code while the UI is
  running. Report successful `stable-auto` startup upgrades, preserve
  notification-only `stable-notify`, and suppress duplicate availability
  notices for the same release.
- [x] Support UI theming with the legacy appearance, all four Catppuccin
  flavors, Gruvbox, Tokyo Night, Dracula, Nord, Solarized Dark, Nightfly, and
  Kanagawa, and the warm earth-toned 1976 palette as named presets plus
  semantic color overrides for backgrounds, borders, status bars, world
  markers, selection, prompts, notices, and plain output. Preserve
  world-provided ANSI and explicit plugin effect styles; a future plugin API
  version can add namespaced plugin theme roles if needed.
- [x] Add a `sandstorm` screen-clear plugin that breaks the visible text into
  wind-driven particles and sweeps them across the pane without changing the
  retained scrollback.
- [x] Add a `doom_fire` screen-clear plugin with a bottom-fed cellular flame
  simulation that consumes the display upward, distinct from the existing
  per-character `flame` burn-and-smoke effect.
- [x] Add a `water_ripple` screen-clear plugin that dissolves text outward from
  a central impact in expanding glyph-density rings, distinct from the existing
  falling and sloshing `water` particle effect.
- [x] Add an `acid_rain` screen-clear plugin whose falling corrosive streaks
  progressively dissolve the visible text while preserving retained scrollback.
- [x] Add a `gag` plugin that loads persistent regular expressions per exact
  world alias from plugin configuration, exposes `/gag [list]` to show the
  active world's expressions, and suppresses matching inbound output from the
  display without removing the canonical event from logging or replay. Bound
  expression count and length, and time-limit matching so expensive expressions
  fail open instead of stalling display processing.
- [x] Add bounded speaker combo streaks for consecutive attributed speech or
  poses from the same person. Progress through `Speaking Spree`, `Rampage`,
  `Dominating`, `Unstoppable`, and `GODLIKE`; animate the message body and a
  short-lived bottom-border HUD, add viewport-scaled Godlike fireworks, continue
  counting while motion is suppressed without replaying missed visuals, and
  avoid repeating effects for higher Godlike multipliers.
- [x] Add a cross-client local Effects Lab. Let terminal users explicitly open
  it with `/effects` as an opt-in navigable pseudo-world with isolated preserved
  input/output, real-world unread tracking, local-only command validation,
  configured speaker previews, complete combo playback, reduced-motion override,
  and `/effects effect all` samples from fictional speaker `WilfordBrimley`.
  Provide the corresponding safe PWA lab through Settings and remove the Lab
  from normal navigation again on `/effects close`.
- [x] Separate recent human command feedback from world output, keep drafts and
  command history per world, render fixed-width `More` and cross-world activity
  summaries in the output border, and ensure short-lived combo sweeps render
  above those persistent labels.
- [x] Detect bracketed multiline text pasted into the active world's input and
  automatically send it as paced `@emit` commands, using the same server-aware
  escaping and preflight validation as `/cat` so spaces, tabs, blank lines, and
  other formatting survive without allowing pasted text to become unintended
  world commands. Keep single-line paste editable and reject unsupported server
  adapters before sending any line.
- [x] Let the terminal UI explicitly read a clipboard image or local path and
  convert it locally into aspect-correct ASCII or per-world-gated Unicode
  Braille art, defaulting non-Unicode ASCII output to uncolored grayscale with
  explicit ANSI color opt-in. Bound source size, output dimensions, palette,
  conversion work, and emitted line length; provide an adjustable preview and
  explicit confirmation before sending paced, server-aware `@emit` lines, and
  never upload the source image to an external service.
- [ ] Add optional authenticated file sharing backed by a private S3 bucket,
  with inline previews for supported image formats.
  Users sign in through an Amazon Cognito user pool and present the resulting
  JWT to an API Gateway endpoint. After authorizing the user and requested
  operation, the API returns narrowly scoped, short-lived URLs: an S3 presigned
  PUT for uploading a bounded file and a CloudFront signed URL for viewing or
  downloading it through a private distribution (or an S3 presigned GET when
  intentionally bypassing CloudFront). Keep the bucket non-public, use
  CloudFront origin access control, generate unguessable object keys, validate
  ownership and sharing policy server-side, constrain allowed file types and
  size, force safe download handling for non-previewable content, expire URLs
  promptly, and define malware scanning, deletion, retention, abuse-reporting,
  and orphan-cleanup behavior.
- [x] Build a mobile gateway client, evaluating a PWA before a native iOS app
  to avoid App Store fees and approval overhead while retaining an installable
  home-screen experience. The gateway already exchanges newline-delimited JSON
  over authenticated TLS TCP and includes canonical ANSI-bearing text plus an
  ANSI-stripped `plain_text` projection; browsers cannot use that raw socket
  transport, so add an authenticated WebSocket transport (or justify gRPC-Web)
  that preserves protocol versioning, cursors, reconnect/backfill, command
  acknowledgements, and Tailscale-only deployment guidance. Define structured
  display spans or a shared safe ANSI-to-style projection so mobile rendering
  does not depend on terminal escape codes. Prototype swipe-left/right world
  switching, unread markers, touch-friendly command history, dynamic type,
  safe-area and keyboard handling, compact provenance, virtualized per-world
  scrollback, explicit return-to-live behavior, and responsive layouts for
  narrow screens. Compare PWA background/reconnect and notification limits
  against a native Swift client before choosing the long-term platform. The PWA
  implementation, structured ANSI-derived text runs, and initial iPhone
  validation are complete; deeper accessibility testing and the longer platform
  pilot remain follow-up work.
- [ ] Add a Gateway/PWA administration helper that discovers the running
  Gateway's Unix socket, validates or configures Tailscale Serve, reports
  actionable service-status failures, and wraps device pairing, listing, and
  revocation without requiring users to coordinate runtime paths manually.
- [ ] Generate smart, guided installation scripts for both Gateway and UI
  hosts. Detect the host environment, install and configure the appropriate
  service, paths, permissions, and update mechanism, and optionally prompt for
  initial world definitions and credentials without exposing secrets in shell
  history or generated logs.
- [x] Add the first bounded portable presentation slice documented in
  `PRESENTATION.md`. Keep Python plugins as the policy layer; let them emit typed,
  composable effect programs interpreted by the TUI and PWA. Select variants by
  supported capabilities, require explicit reduced-motion and unsupported-client
  fallbacks, and begin with a bounded foreground-color timeline plus static bold
  and underline. Migrate existing named text effects incrementally rather than
  replacing the plugin API in one release. Browser-only scale, rotation,
  translation, opacity, and overlay primitives remain follow-up work and must not
  constrain the PWA to terminal capabilities. Keep client commands and actions
  outside the presentation language.
- [ ] Finish migrating display plugins from named terminal-only effects to the
  bounded portable presentation mini-DSL. Inventory remaining plugins, express
  every portable effect with typed programs and explicit reduced-motion and
  unsupported-client fallbacks, implement the corresponding safe PWA renderers,
  and retain terminal-only behavior only where the shared language cannot
  represent it without weakening its bounds.
- [x] Fix speaker effects not working on poses.
- [x] Harden protected release orchestration with prompt-aware exact-tag
  confirmations, guarded resume of version-only release PRs, exact-head check
  registration/completion, stale GitHub head-propagation tolerance, transient
  neutral/skipped CodeQL handling, and immutable final asset verification.

## Test Strategy

- Pure unit tests for Telnet state, decoding, redaction, parser grammars,
  classification, pager state, rate limits, and context selection.
- Golden JSONL fixtures for event compatibility.
- Transcript fixtures containing ANSI, Unicode, malformed data, and NOSPOOF
  variants.
- Async integration tests with local fake TCP and TLS servers.
- Connector state-machine tests for leases, exact build identity, framing,
  sequence acknowledgements, bounded replay, overflow persistence, and
  configuration mismatch; real Unix-socket/subprocess tests for attachment,
  replacement, crash recovery, clean shutdown, idle traffic, and preserving a
  fake world's single TCP connection while the Gateway application is killed
  and restarted.
- Fake OpenAI-compatible HTTP responses for deterministic agent tests.
- TUI tests over pure viewport and action state where possible, keeping
  terminal integration tests focused.
- Memory tests that feed large transcripts and assert bounded retained state.
- Release-orchestration fixtures that model candidate publication, exact-byte
  stable promotion, protected check registration, head propagation, deployment
  approval, confirmation timeouts, and safe resume failures without creating
  real tags or releases.

## Reference Sources

- prompt_toolkit full-screen applications:
  https://python-prompt-toolkit.readthedocs.io/en/stable/pages/full_screen_apps.html
- prompt_toolkit asyncio integration:
  https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/asyncio.html
- JSONC draft specification:
  https://jsonc.org/
- Python entry points:
  https://docs.python.org/3/library/importlib.metadata.html#entry-points
- TinyMUX notification implementation:
  https://github.com/brazilofmux/tinymux/blob/3e0ad0ae06b81289dcb27e1aaae4974136565857/mux/modules/engine/engine.cpp#L686-L815
- TinyMUX help:
  https://github.com/brazilofmux/tinymux/blob/3e0ad0ae06b81289dcb27e1aaae4974136565857/mux/game/text/help.txt
- TinyMUX message source flags:
  https://github.com/brazilofmux/tinymux/blob/3e0ad0ae06b81289dcb27e1aaae4974136565857/mux/include/externs.h#L1240-L1254
- OpenAI structured outputs:
  https://platform.openai.com/docs/guides/structured-outputs
- Ollama structured outputs:
  https://docs.ollama.com/capabilities/structured-outputs
- Ollama OpenAI compatibility:
  https://docs.ollama.com/api/openai-compatibility

## Next Step

Begin Phase 1 with the smallest complete vertical foundation:

1. Initialize the packaged `uv` project and `tfr` CLI.
2. Add typed JSONC configuration for the three files.
3. Define canonical event and command request models.
4. Implement redacted JSONL serialization.
5. Add focused tests for configuration, permissions, and secret redaction.

This establishes the contracts used by networking, the TUI, plugins, and
agents before any of those components create incompatible assumptions.
