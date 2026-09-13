# TFR

TFR is a Python terminal client for concurrent MUD and MUSH world sessions.
It provides typed JSONC configuration, canonical event models, secret
redaction, append-only JSONL event logging, incremental Telnet parsing,
concurrent TCP or verified TLS world sessions, ANSI-safe RhostMUSH and TinyMUX
NOSPOOF provenance parsing, a persistent multi-client gateway, and a full-screen
multi-world terminal UI.

The complete design and implementation sequence are in [PLAN.md](PLAN.md).

## Quick Start

TFR requires Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/).
From the repository root, install the project and its development dependencies:

```console
uv sync
```

Create a private working configuration from the supplied examples:

```console
mkdir -p .tfr
cp examples/config.jsonc examples/worlds.jsonc examples/agents.jsonc .tfr/
chmod 600 .tfr/*.jsonc
```

Edit `.tfr/worlds.jsonc` with the world addresses and logins you want to
use. Edit `.tfr/agents.jsonc` if you want agent-controlled worlds. Check
the complete configuration before connecting:

```console
uv run tfr --check-config --config .tfr/config.jsonc
```

Start the persistent Gateway in one terminal:

```console
uv run tfr gateway --config .tfr/config.jsonc
```

Leave that command running. A successful startup reports its socket:

```text
TFR Gateway listening on /Users/YOU/.local/state/tfr/run/gateway.sock
Press Ctrl-C to stop the gateway.
```

Attach the UI from a second terminal:

```console
uv run tfr ui --config .tfr/config.jsonc
```

You can attach multiple UIs to the same Gateway. `Ctrl-Q` or `/quit` closes only
the current UI; world connections, logging, and agents continue in the Gateway.
Use `/reload` after changing UI or UI-plugin code. It replaces the UI process,
loads the latest code, and reconnects without stopping world sessions. Stop the
Gateway with `Ctrl-C` when you want all managed services to shut down.

The Gateway and UI use `$XDG_RUNTIME_DIR/tfr/gateway.sock` when
`XDG_RUNTIME_DIR` is set. Otherwise they use
`~/.local/state/tfr/run/gateway.sock`. To use another private socket directory,
pass the identical path to both commands:

```console
uv run tfr gateway --config .tfr/config.jsonc --socket /private/path/tfr.sock
uv run tfr ui --config .tfr/config.jsonc --socket /private/path/tfr.sock
```

The socket's existing parent directory must be owned by the current user and
must not grant group or other permissions. If the UI reports that the gateway
socket does not exist, confirm the Gateway is still running and that both
commands resolve to the same socket path.

For a single-process session without a persistent Gateway, use the legacy
combined mode:

```console
uv run tfr --config .tfr/config.jsonc
```

## Development

```console
uv sync
uv run tfr --help
uv run tfr --check-config --config examples/config.jsonc
uv run tfr --config examples/config.jsonc
uv run tfr gateway --config examples/config.jsonc
uv run tfr ui --config examples/config.jsonc
uv run tfr --replay ~/.local/state/tfr/logs/tfr-TIMESTAMP-PID.jsonl
uv run ruff check .
uv run pytest
```

Example configuration is under [`examples/`](examples/). The default main
configuration path is `~/.config/tfr/config.jsonc`. Credential-bearing files
should be readable only by their owner (`0600` on POSIX systems).

Running `tfr` without a mode starts the original combined client. For a
persistent connection process, start `tfr gateway` and attach one or more
terminals with `tfr ui`. Both use
`~/.local/state/tfr/run/gateway.sock` by default, or `$XDG_RUNTIME_DIR/tfr/gateway.sock`
when `XDG_RUNTIME_DIR` is set. Pass the same `--socket PATH` to both commands to
override it. Closing an attached UI does not disconnect worlds or stop agents.
The gateway retains configured per-world scrollback and sends it to newly
attached or reconnecting UIs before switching atomically to live events.
Within an attached UI, `/reload` (or `/restart`) replaces only the UI process
and reloads retained Gateway history to rebuild the display; the gateway and
world sessions continue running.

## Remote Gateway

The Gateway can keep its private Unix socket while also listening for remote UIs
over authenticated TLS. Network mode is opt-in and requires a certificate,
private key, and shared token. Bind `--listen-host` to the dedicated machine's
Tailscale address or another specific private address; wildcard addresses are
rejected.

Create a random token on the Gateway host and keep it owner-only:

```console
umask 077
openssl rand -hex 32 > ~/.config/tfr/gateway.token
```

Obtain a TLS certificate whose hostname is reachable by the UI. Tailscale HTTPS
certificates for a MagicDNS name work without a custom CA; a certificate from a
private CA works with the UI's `--tls-ca` option. Keep the TLS private key mode
`0600`, then start the dedicated Gateway using its Tailscale IP as the bind
address:

```console
uv run tfr gateway --config .tfr/config.jsonc \
  --listen-host 100.x.y.z --listen-port 7347 \
  --token-file ~/.config/tfr/gateway.token \
  --tls-cert ~/.config/tfr/gateway.crt \
  --tls-key ~/.config/tfr/gateway.key
```

Copy only the token to an owner-only file on the UI machine. The UI needs its
normal main configuration for local UI/plugin preferences, but it does not need
the Gateway's `worlds.jsonc`, `agents.jsonc`, world passwords, or provider keys.
Connect using the certificate's MagicDNS hostname:

```console
uv run tfr ui --config .tfr/config.jsonc \
  --gateway-host gateway.example.ts.net --gateway-port 7347 \
  --token-file ~/.config/tfr/gateway.token
```

When connecting to an IP while the certificate names a host, add
`--tls-server-name gateway.example.ts.net`. For a private CA, add
`--tls-ca ~/.config/tfr/gateway-ca.crt`. Certificate and hostname verification
cannot be disabled. Tailscale ACLs should additionally restrict port `7347` to
the intended UI devices. `/reload` preserves these command-line connection
options because it replaces the UI with the same arguments.

## Separate Linux UI Host

The UI can run on a Linux computer other than the Gateway host. Install the
same TFR revision on that computer and launch only `tfr ui`; the installation
contains both modes, but UI mode does not start world connections, agents, or a
network listener. Use a normal non-root account with Python 3.12 or newer,
[`uv`](https://docs.astral.sh/uv/getting-started/installation/), and private
network access to the Gateway. Keep the UI and Gateway on the same reviewed TFR
revision when upgrading them.

Until TFR is available as a packaged release, copy or clone a reviewed source
checkout onto the UI host. For example, from the repository root on an
administration machine:

```console
ssh USER@UI_HOST 'mkdir -p ~/tfr'
rsync -az \
  --exclude '.git/' \
  --exclude-from '.gitignore' \
  ./ USER@UI_HOST:~/tfr/
```

On the UI host, install the locked runtime environment without development
dependencies:

```console
cd ~/tfr
uv python install 3.12
uv sync --frozen --no-dev
uv run --frozen tfr --version
```

Create a private configuration directory on the UI host:

```console
mkdir -p ~/.config/tfr
chmod 700 ~/.config/tfr
```

Then create `~/.config/tfr/config.jsonc` for UI preferences. A remote UI needs
only the main configuration; the `worlds.jsonc` and `agents.jsonc` paths use
their defaults but are not opened in UI mode:

```jsonc
{
  "schema_version": 1,
  "ui": {
    "scrollback_lines": 20000,
    "recent_input_lines": 3,
    "output_color": "#d7d7d7",
    "show_nospoof_prefix": false,
    "animations_enabled": true,
    "low_bandwidth": false,
    "screen_clear": { "mode": "cycle" },
    "pager": { "enabled": true, "overlap_lines": 1 }
  },
  "plugins": {
    "enabled": [],
    "config": {}
  }
}
```

Protect the configuration even though this UI-only file should not contain
world or provider credentials:

```console
chmod 600 ~/.config/tfr/config.jsonc
```

Add entry-point names under `plugins.enabled` for whichever UI plugins you
want on this host. The easiest way to make plugin code available here without
separately copying a plugin repository is `plugins.sources`, described in
[Installing Plugins From a GitHub Repository](#installing-plugins-from-a-github-repository);
TFR fetches each configured repository into `plugins.state_directory` on this
host the first time it starts.

The general `--check-config` command also loads the Gateway's world and agent
files, so do not use it to validate this intentionally UI-only configuration.
UI fields are validated when `tfr ui` starts.

Transfer the shared token through an authenticated channel instead of pasting
it into a command or configuration file. For example, run this on the UI host
when it can reach the Gateway over SSH:

```console
scp GATEWAY_USER@GATEWAY_HOST:~/.config/tfr/gateway.token \
  ~/.config/tfr/gateway.token
chmod 600 ~/.config/tfr/gateway.token
```

If the Gateway uses a private CA, also transfer its public CA certificate. Never
copy the Gateway's TLS private key, world configuration, agent configuration,
world passwords, or provider keys to the UI host.

Connect from the UI host using the hostname in the Gateway certificate:

```console
cd ~/tfr
uv run --frozen tfr ui \
  --config ~/.config/tfr/config.jsonc \
  --gateway-host gateway.example.ts.net \
  --gateway-port 7347 \
  --token-file ~/.config/tfr/gateway.token
```

Add `--tls-ca ~/.config/tfr/gateway-ca.crt` for a private CA. When the network
target is an IP address but the certificate names a host, connect to the IP with
`--gateway-host` and add `--tls-server-name` with the certificate hostname.

The UI is interactive and should run in a terminal rather than as a system
service. It can be started directly over SSH with a pseudo-terminal:

```console
ssh -t USER@UI_HOST \
  'cd ~/tfr && uv run --frozen tfr ui \
    --config ~/.config/tfr/config.jsonc \
    --gateway-host gateway.example.ts.net \
    --gateway-port 7347 \
    --token-file ~/.config/tfr/gateway.token'
```

An SSH disconnect closes that UI only; the Gateway and world sessions continue.
Reconnect with the same command. UI plugins must be installed and enabled on
the UI host, and `/sh` or `! command` runs on that host. Anyone who can read the
shared token or control the UI has full Gateway command and connection-control
authority, so retain the TLS, Tailscale ACL, and firewall protections described
above.

## Terminal Controls

- `Enter`: send input to the active world.
- `F6`, `Ctrl+Right`, or `Option+Right`: switch to the next world.
- `F5`, `Ctrl+Left`, or `Option+Left`: switch to the previous world.
- `PageUp` and `PageDown`: navigate scrollback or advance `More` output.
- `End`: return to live output.
- `Ctrl+L`: burn away the active screen without deleting retained scrollback.
- `Ctrl+R`: reconnect the active world.
- `Ctrl+Q`: quit cleanly.
- Left-click a world in the top bar to switch to it.
- Drag across world output to highlight it and copy the plain text to the clipboard.
- Click an underlined `http://` or `https://` link in world output to open it in
  your default browser.

Input beginning with `/` is a client command. Available commands are `/world
ALIAS`, `/next` (`/n`), `/previous` (`/p`), `/connect`, `/disconnect`, `/reconnect`, `/more`,
`/end`, `/nospoof show|hide|status`, `/animations on|off|status`,
`/lowbw on|off|status`, `/clear`, `/boss`, `/sh`, `/reload`, `/restart`,
`/gateway reconnect`, `/help`, and `/quit`, plus commands registered by enabled
plugins. Begin input with `//` to send a literal
leading slash. `/help` displays commands, loaded plugins, world markers, and
keybindings.

After a laptop sleep or transient network loss, TFR verifies the Gateway
connection is still alive on a background interval, and automatically retries
`/gateway reconnect`'s own logic (bounded attempts, configurable interval)
before asking you to run it manually. This only replaces the stale transport;
it restores missed retained events and preserves the current UI. Configure
attempt count and timing under `ui.gateway_reconnect` in `config.jsonc`:

```jsonc
"ui": {
  "gateway_reconnect": {
    "enabled": true,
    "heartbeat_seconds": 20,
    "ping_timeout_seconds": 8,
    "max_attempts": 5,
    "retry_interval_seconds": 3,
  },
}
```

`heartbeat_seconds` controls how often TFR actively confirms the connection is
alive; `ping_timeout_seconds` bounds how long it waits for that confirmation
before treating the connection as lost. Set `enabled` to `false` to disable
automatic retry entirely and rely on `/gateway reconnect` manually. If the
Gateway itself restarted or changed its worlds, use `/reload` instead so the
UI can be rebuilt from the new Gateway state.

`/boss` replaces the entire interface with a subdued, static incremental-build
screen. World connections, buffering, and event logging continue in the
background without displaying new text. Press Enter to restore TFR and reveal
the accumulated output. Other ordinary input is ignored while the cover is
active.

The output and combined recent-input/editor regions have independent borders.
Enabled plugins can animate borders and attributed speaker names through TFR's
central redraw scheduler. Use `/animations off` to remove continuous border
effects and freeze text effects in their static colors; `/animations on` restores
them. Use `/lowbw on` to retain static styling but stop scheduled redraws. World
traffic and keyboard input still redraw normally. `ui.animations_enabled` and
`ui.low_bandwidth` select the initial state for each attached UI.

Ctrl-L snapshots and clears the visible output. UI plugins can register finite
screen-clear transitions; `ui.screen_clear.mode` chooses `cycle`, `random`, or
`locked`, and locked mode requires an `effect` matching an enabled plugin name.
Effect contexts include both plain snapshot lines and the corresponding styled
fragments so transitions can preserve the visible text colors.
Screen-clear effects may run for up to 60 seconds at no more than 30 frames per
second. Stateful effects can provide a completion check to keep the transition
visible past that nominal duration until their simulation finishes.
Use `/clear status`, `/clear cycle`, `/clear random`, or `/clear lock EFFECT` to
change selection for the running UI. `/clear` without arguments behaves like
Ctrl-L. With no available effect plugin, clearing is immediate. The underlying
scrollback is retained, and output arriving during a transition appears when it
completes.

Use `/recall X` to append the last X retained display lines from the active world.
Recall output begins with a yellow `-- Recall X` header and is not itself included
in later recalls.

Use `/sh` to temporarily open the local interactive shell and return to TFR by
exiting it. Input beginning with `!` runs a one-shot local shell command, such as
`! w`, and waits for Enter before restoring the full-screen UI. Begin input with
`!!` to send a literal leading `!` to the active world. Local shell commands are
not submitted to a world and are not included in TFR's event log.

The world bar marks human-operated worlds with `[H]` and configured agent worlds
with `[A]`, followed by `(Xs)`/`(Xm)`/`(Xh)`/`(Xd)` showing how long it has been
since that world last received inbound input, once it has received any. Each
world retains its own draft, command history, rendered scrollback,
pager state, and unread count while inactive.
The active world's recently sent commands remain directly above the editor.
These lines are separate from server output, so server speech echoes are not
duplicated. Configure their number with `ui.recent_input_lines` (default `3`, or
`0` to hide the pane). `PageUp` after `Ctrl+L` reveals retained pre-clear output;
pressing `PageUp`, `PageDown`, `End`, `/more`, or `/end` while a screen-clear
animation is still running ends that animation immediately so the requested
scrolling is visible right away, instead of silently changing scroll position
underneath the still-active animation.

## Plugins

Trusted local packages expose plugins through the versioned
`tfr.plugins.v1` entry-point group and import their supported types from
`tfr.plugin_api`:

```toml
[project.entry-points."tfr.plugins.v1"]
example = "example_plugin:plugin"
```

The loaded object can implement `register(registrar, config)` and set
`api_version = 1`. The registrar supports client commands, inbound event
enrichers, display transforms, structured display decorators, border effects,
screen-clear effects, status segments, key bindings, and lifecycle handlers.
Commands can include help text for `/help`. Enrichers return
`tfr.plugin_api.EventPatch`; they cannot replace event
identity or canonical text. Command and key handlers receive a
`PluginCommandContext` and submit typed commands with `await context.submit(...)`
rather than accessing sockets. `context.world_info` exposes the active world's
non-secret server type and effective text encoding.

Enable entry-point names under `plugins.enabled` in `config.jsonc`. Arbitrary
plugin-owned settings belong under `plugins.config.NAME`. A failed extension is
disabled and reported as a redacted internal plugin event without interrupting
world sessions or event logging.

Plugin packages are trusted native code and are not sandboxed: enabling one
runs its code with TFR's full process privileges. Only enable plugins you or
someone you trust wrote or reviewed.

### Installing Plugins From a GitHub Repository

The easiest way to add plugins is to point TFR at a Git repository directly in
`config.jsonc`; TFR clones it locally and discovers its `tfr.plugins.v1` entry
points without any separate packaging or installation step:

```jsonc
"plugins": {
  "enabled": ["cat", "some_friend_plugin"],
  "sources": [
    { "repo": "someone/tfr-plugins-fun" },
  ],
}
```

`repo` accepts an `OWNER/REPO` GitHub shorthand (expanded to
`https://github.com/OWNER/REPO.git`), a full `https://` URL, or a `git@`/`ssh://`
URL for a private repository reachable with your own SSH key or agent. TFR only
invokes your local `git` binary (`clone`, `fetch`, `checkout`); it never sends
your credentials anywhere itself and never runs `pip install` or any build
step, so a source's own third-party dependencies (if any) must already be
available in TFR's environment.

By default a source is fetched once and then left alone on later launches.
Two optional fields change that:

- `"ref": "BRANCH_TAG_OR_COMMIT"` checks out that branch, tag, or full 40-character
  commit hash instead of the repository's default branch.
- `"auto_update": true` re-fetches that source's branch or tag on every launch
  and fast-forwards the local checkout to its latest commit. A `ref` pinned to
  a full commit hash is **never** auto-updated, even with `auto_update: true`.

```jsonc
"sources": [
  // Convenient, but whoever can push to this branch controls code that
  // runs with your privileges the next time TFR starts.
  { "repo": "someone/tfr-plugins-fun", "auto_update": true },
  // Reproducible and reviewed once, then pinned; recommended for anything
  // you have not personally read.
  { "repo": "someone/tfr-plugins-serious", "ref": "b7f3c1b6a8e2d4c9f1a0b3e5d7c9a1f3e5d7c9a1" },
  // A monorepo plugin living in a subdirectory.
  { "repo": "someone/tfr-plugin-monorepo", "path": "plugins/cool-effect" },
],
```

This is intentionally convenient rather than maximally safe: it suits a small
group of friends who trust each other's repositories, not an untrusted or
adversarial source. Pin anything you have not personally reviewed to a full
commit hash, and never enable `auto_update` on a source whose maintainer you
would not trust with a shell on this machine. If a source cannot be fetched
(offline, renamed, deleted), TFR keeps using its last successful checkout, if
any, and prints a warning rather than failing to start.

Checkouts live under `plugins.state_directory` (default
`~/.local/state/tfr/plugins`). Delete a source's subdirectory there to force a
fresh clone.

### Installing Published Plugin Packages

Plugins can also be installed as ordinary Python packages in the same
environment as TFR, discovered through their `tfr.plugins.v1` entry points
without TFR downloading anything itself:

```toml
[project.entry-points."tfr.plugins.v1"]
example = "example_plugin:plugin"
```

Keep plugin source URLs and version pins in a deployment manifest rather than
relying on TFR to fetch them, and pin Git-based installations to a reviewed
commit or release tag.

## Agents

Each configured agent uses its own world alias and login identity. Providers
use the OpenAI-compatible chat-completions contract, so hosted OpenAI and local
Ollama endpoints share the same runtime. TFR sends bounded, plain-text world
context as explicitly untrusted data and requests one schema-constrained
`say`, `pose`, `page`, or `none` action. Responses are validated locally before
the command bus can receive them.

Use `/agent status`, `/agent inspect NAME`, `/agent pause NAME`, `/agent resume
NAME`, `/agent trigger NAME`, and `/agent close` from the TUI. `F8` toggles the
inspector for the agent attached to the active world. The inspector shows
selected event IDs, exact submitted messages, provider identity, response,
validation outcome, and rendered command; it does not expose hidden
chain-of-thought. Requests, responses, rejected decisions, accepted actions,
and resulting commands are also written to the event log without API keys.

## Protocol And Security Notes

Worlds use UTF-8 by default with per-world encoding overrides. Direct TLS
performs certificate and hostname verification unless explicitly disabled;
`tls.ca_file` and `tls.server_hostname` support private trust roots and unusual
hosting arrangements. The incremental Telnet codec handles negotiation, TTYPE,
NAWS, CHARSET, escaped IAC bytes, and fragmented input.

The bare and generic adapters classify visible text conservatively without
MUSH-specific protocol behavior. RhostMUSH, TinyMUSH, and TinyMUX have explicit
server types. TinyMUSH full prefixes and TinyMUX full or terse NOSPOOF forms
provide authoritative sender metadata when present; TinyMUX can also include a
source tag. Prefixes are retained in canonical logs even when hidden from the
display. Set the initial behavior with `provenance.show_prefix`, then use
`/nospoof show`, `/nospoof hide`, or `/nospoof status` at runtime. This affects
future display only and never removes provenance from canonical logs.

See [SECURITY.md](SECURITY.md) for credential, TLS, logging, plugin, and agent
security boundaries. See [LICENSES.md](LICENSES.md) for the reviewed runtime
dependency licenses. TFR is distributed under the [MIT License](LICENSE).
