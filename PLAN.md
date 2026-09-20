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

The TUI is one consumer and producer around an application core. It does not
own sockets, connection tasks, event history, or model calls.

```text
                         +-------------------+
                         | prompt_toolkit UI |
                         +---------+---------+
                                   |
                         commands  |  events
                                   |
                         +---------v---------+
                         |  application core |
                         +----+----------+----+
                              |          |
                    command bus          event bus
                              |          |
             +----------------+          +----------------+
             |                                            |
     +-------v-------+                            +-------v-------+
     | world sessions|                            | event sinks   |
     +-------+-------+                            | JSONL/plugins |
             |                                    +---------------+
       TCP/TLS/Telnet
             |
       remote worlds

     +---------------+
     | agent runtime |
     +-------+-------+
             | subscribes to its world's events
             | submits validated conversation actions
             +------------------------------------------> command bus
```

### Inbound Flow

1. A world session reads bytes from TCP or TLS.
2. The Telnet codec consumes negotiation bytes and emits application bytes.
3. An incremental decoder produces canonical text without losing partial
   multibyte characters.
4. The selected server adapter parses NOSPOOF and other server-specific
   provenance.
5. Semantic classifiers add event type and confidence without overwriting
   source data.
6. The canonical event is assigned a per-session sequence number and
   published to the event bus.
7. Event sinks append the canonical event to JSONL.
8. A display projector optionally removes a successfully parsed NOSPOOF
   prefix and produces styled output for the TUI.
9. Agent context builders receive selected, sanitized events from only their
   configured world session.

### Outbound Flow

1. Human input, a plugin, a startup action, an idle action, or an agent creates
   a typed command request.
2. The command request identifies its source actor and target session.
3. Agent policy validates agent actions before command rendering.
4. A redacted audit projection is written to JSONL.
5. The world session serializes the real command to its socket.
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
- [ ] Create a `/stats` plugin with per-world received-versus-sent counts, top
  speakers, message histograms, speech-length statistics by speaker, UI output,
  and an explicit option to emit selected statistics to the world.
- [x] Make display-affecting keys such as Page Up and Page Down immediately end
  an active screen-clear animation before performing the requested action.
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
- [ ] Add opt-in stable-release download and staging that verifies the manifest
  source, artifact size, and SHA-256 digest before activating the managed UI
  installation; keep Gateway activation and restart administrator-controlled.
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
- [ ] Add a configurable speaker combo-streak plugin for consecutive speech or
  poses from the same person. Show a short-lived animated progression such as
  `x2!`, `x3!`, `SUPER!`, `DOMINATING!`, and `UNSTOPPABLE!`; evaluate an
  end-of-line indicator after the speaker's text as the initial placement and
  extend the display-decoration API if animating appended text requires it.
- [ ] Build a mobile gateway client, evaluating a PWA before a native iOS app
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
  against a native Swift client before choosing the long-term platform.

## Test Strategy

- Pure unit tests for Telnet state, decoding, redaction, parser grammars,
  classification, pager state, rate limits, and context selection.
- Golden JSONL fixtures for event compatibility.
- Transcript fixtures containing ANSI, Unicode, malformed data, and NOSPOOF
  variants.
- Async integration tests with local fake TCP and TLS servers.
- Fake OpenAI-compatible HTTP responses for deterministic agent tests.
- TUI tests over pure viewport and action state where possible, keeping
  terminal integration tests focused.
- Memory tests that feed large transcripts and assert bounded retained state.

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
