# TFR

TFR is a Python terminal client for concurrent MUD and MUSH world sessions.
It provides typed JSONC configuration, canonical event models, secret
redaction, append-only JSONL event logging, incremental Telnet parsing,
concurrent TCP or verified TLS world sessions, ANSI-safe RhostMUSH and TinyMUX
NOSPOOF provenance parsing, a persistent multi-client gateway, and a full-screen
multi-world terminal UI.

The complete design and implementation sequence are in [PLAN.md](PLAN.md).

## Quick Start

TFR requires `git`, Python 3.12 or newer, and
[`uv`](https://docs.astral.sh/uv/). Clone the repository as a bootstrap checkout,
then use its installer to resolve, verify, build, and activate the exact latest
stable release:

```console
git clone https://github.com/Blaag/tfr.git
cd tfr
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

The bootstrap checkout is not installed as `main`. The installer fetches the
official stable release manifest, verifies its immutable annotated tag and exact
commit, builds that tagged source with its committed lock file, and exposes it
through `~/.local/bin/tfr`. Add `~/.local/bin` to `PATH` if you want to invoke
`tfr` without its full path.

Create a private working configuration from the supplied examples:

```console
tfr_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/tfr"
mkdir -p "$tfr_config_dir"
chmod 700 "$tfr_config_dir"
cp examples/config.jsonc examples/worlds.jsonc examples/agents.jsonc "$tfr_config_dir/"
chmod 600 "$tfr_config_dir"/*.jsonc
```

TFR reads `config.jsonc` from that standard directory by default, regardless of
the current directory or where the bootstrap repository was cloned. Edit
`worlds.jsonc` there with the world addresses and logins you want to use. Edit
`agents.jsonc` if you want agent-controlled worlds. Check the complete
configuration before connecting:

```console
~/.local/bin/tfr --check-config
```

Start the persistent Gateway in one terminal:

```console
~/.local/bin/tfr gateway
```

Leave that command running. A successful startup reports its socket:

```text
TFR Gateway listening on /Users/YOU/.local/state/tfr/run/gateway.sock
Press Ctrl-C to stop the gateway.
```

Attach the UI from a second terminal:

```console
~/.local/bin/tfr ui
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
~/.local/bin/tfr gateway --socket /private/path/tfr.sock
~/.local/bin/tfr ui --socket /private/path/tfr.sock
```

The socket's existing parent directory must be owned by the current user and
must not grant group or other permissions. If the UI reports that the gateway
socket does not exist, confirm the Gateway is still running and that both
commands resolve to the same socket path.

For a single-process session without a persistent Gateway, use the legacy
combined mode:

```console
~/.local/bin/tfr
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
configuration path is `$XDG_CONFIG_HOME/tfr/config.jsonc` when
`XDG_CONFIG_HOME` is set, otherwise `~/.config/tfr/config.jsonc`. Use
`--config PATH` only to select a nonstandard main configuration. Referenced
`worlds_file` and `agents_file` paths are resolved relative to that file.
Credential-bearing files should be readable only by their owner (`0600` on
POSIX systems).

Each world may define command-style switching shortcuts in `worlds.jsonc`, for
example `"aliases": ["g"]` makes `/g` switch to that world. Alias names are
case-insensitive, must use letters, digits, underscores, or hyphens, and must be
unique across worlds. Each world may define up to 32 aliases of at most 64
characters, with 256 aliases allowed across the configuration. Core and plugin
commands take precedence over conflicting world aliases; `/help` identifies
aliases that are shadowed.

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

## Managed Release Installation

The recommended user installation resolves the latest stable release from the
official manifest rather than installing the bootstrap checkout's `main`
branch:

```console
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

The installer requires `git` and `uv`. It verifies that the manifest's fully
qualified annotated tag points directly to the declared commit, checks that the
tagged project version agrees, embeds that full commit in the wheel, exports
hash-locked runtime dependencies from the tagged `uv.lock`, creates an isolated
relocatable environment, verifies the installed build identity, and only then
activates it.

Developers can instead package the current clean checkout explicitly. This mode
rejects uncommitted and untracked files rather than silently including them:

```console
./scripts/install-from-checkout
```

Using the managed installation is optional. Running the installer does not
remove or modify the checkout, its `.venv`, configuration files, logs, plugin
state, or any running TFR process. It writes only the managed release tree under
`~/.local/share/tfr` and the stable `~/.local/bin/tfr` launcher as persistent TFR
installation outputs; `uv` can also use its cache and managed Python storage. It
refuses to replace a launcher it did not create.

Managed files use this layout:

```text
~/.local/share/tfr/
├── releases/VERSION+git.COMMIT/
├── releases/VERSION+stable.COMMIT/
├── current -> releases/RELEASE_ID
├── previous -> releases/PREVIOUS_RELEASE_ID
└── install.lock
~/.local/bin/tfr
```

`~/.local/bin/tfr` is a stable launcher that executes the environment selected
by `current`. Activation changes new invocations only; existing UI and Gateway
processes continue running their original code. A managed attached UI's
`/reload` re-enters through `current`. Restart a managed Gateway through its
normal systemd, launchd, or other administrator-controlled service operation.

The checkout script also manages installed releases:

```console
./scripts/install-from-checkout --list
./scripts/install-from-checkout --latest-stable
./scripts/install-from-checkout --latest-stable --no-activate
./scripts/install-from-checkout --activate RELEASE_ID
./scripts/install-from-checkout --rollback
```

`--latest-stable` performs an explicit, fail-closed stable installation. It
fetches the live official release manifest without using the notification
cache, fetches only its fully qualified annotated Git tag from the official
repository, and requires the tag's direct commit and tagged project version to
match the manifest. It builds that temporary detached checkout with its
committed lock file; it never installs `main` or modifies the bootstrap
checkout. Verified stable releases use `VERSION+stable.COMMIT` IDs so their
provenance remains distinct from local checkout builds. A lower version than an
already installed stable release, or the same version with a different commit,
is rejected. Use `--no-activate` to stage the verified release without changing
`current`.

`--rollback` swaps `current` and `previous`, so it can be reversed by running it
again. Installed release directories are retained until removed manually. Use
`--root` and `--bin-dir` to place a test or nonstandard installation elsewhere,
and `--python` to select the Python 3.12-or-newer interpreter that `uv` should
use. Ensure `~/.local/bin` is in `PATH` before using `tfr` directly.

The installer does not copy configuration. Managed commands automatically use
the standard configuration path described above. For a nonstandard location,
continue passing its absolute path. Relative `worlds_file` and `agents_file`
values are resolved relative to that main configuration file:

```console
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr gateway --config /private/path/config.jsonc
~/.local/bin/tfr ui --config /private/path/config.jsonc
```

There is no required migration. Existing `uv run tfr ...` commands continue to
use the checkout environment. To adopt the managed installation, start future
Gateway and UI processes through `~/.local/bin/tfr`; installing alone does not
replace a process that is already running. Once an attached UI was started that
way, `/reload` follows the managed `current` release.

## Stable Release Updates

TFR checks the latest stable GitHub Release in the background after startup and
then approximately every six hours. The same schedule checks stable plugin
sources configured on the UI host. These periodic checks are notification-only:
they never download, install, or execute an artifact. Use `/update status` to
inspect TFR and plugin results or `/update check` to refresh them immediately.
An attached UI reports its own build and the remote Gateway build separately;
update the Gateway through its administrator-controlled deployment and restart
process.

The TFR release manifest is cached with its HTTP `ETag` under
`~/.local/state/tfr/updates` by default. Plugin results are held in memory and
revalidated from each configured plugin manifest. Network and validation
failures do not interrupt startup or active sessions. Set `updates.enabled` to
`false` to disable periodic and `/update` checks, or configure the timing, HTTPS
manifest URL, and state directory under the top-level `updates` object. Only
stable `vMAJOR.MINOR.PATCH` releases participate; prereleases and moving Git tags
are not used.

Release notifications do not download or install the advertised artifact.
Operators may explicitly run `./scripts/install-from-checkout --latest-stable`
to rebuild and activate the exact tagged source release. This command validates
the manifest-to-tag trust chain independently and is never started by the
background checker. Direct verified installation of the advertised wheel
remains future work.

From the bootstrap checkout, a normal update is:

```console
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

Activation affects new processes only. Restart a managed Gateway through its
service manager and use `/reload` or restart each managed UI. If the new release
must be reverted, use `./scripts/install-from-checkout --rollback`, then restart
the affected process again. `--list` shows the exact release IDs selected by the
`current` and `previous` pointers.

Maintainers publish a release by updating `project.version` in `pyproject.toml`,
committing and pushing that change, previewing `./scripts/publish-release`, then
running `./scripts/publish-release --push`. The script validates a clean,
synchronized `main` and pushes only the matching immutable tag. The release
workflow tests the tag, embeds its exact commit in the wheel, and publishes the
wheel, source distribution, and checksummed `update-manifest.json` together.
Configure the `release` GitHub environment to require maintainer approval, and
enable immutable releases plus protected release tags in the repository ruleset;
the workflow also rejects commits that are not on `main`. See
[MAINTAINER.md](MAINTAINER.md) for script responsibilities, release verification,
and failure recovery.

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
~/.local/bin/tfr gateway \
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
~/.local/bin/tfr ui \
  --gateway-host gateway.example.ts.net --gateway-port 7347 \
  --token-file ~/.config/tfr/gateway.token
```

When connecting to an IP while the certificate names a host, add
`--tls-server-name gateway.example.ts.net`. For a private CA, add
`--tls-ca ~/.config/tfr/gateway-ca.crt`. Certificate and hostname verification
cannot be disabled. Tailscale ACLs should additionally restrict port `7347` to
the intended UI devices. `/reload` preserves these command-line connection
options because it replaces the UI with the same arguments.

## Mobile PWA

The Gateway can serve an installable, iPhone-oriented web client through
[Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve). The PWA uses a
separate chat-only protocol and revocable device credentials; it never receives
the shared full-control Gateway token. The backend listens only on loopback and
expects Tailscale Serve to provide the configured HTTPS origin.

Choose the Gateway machine's HTTPS MagicDNS URL and add this top-level block to
`config.jsonc`:

```jsonc
"web_gateway": {
  "enabled": true,
  "origin": "https://gateway.example.ts.net",
  "listen_host": "127.0.0.1",
  "listen_port": 7348,
  "state_directory": "~/.local/state/tfr/web",
  "snapshot_events": 2000,
},
```

Start or restart `tfr gateway` normally. It will report the loopback web
listener in addition to the private Unix socket. Publish that loopback service
inside the tailnet with Tailscale Serve:

```console
tailscale serve --bg http://127.0.0.1:7348
tailscale serve status
```

Use `tailscale serve`, not `tailscale funnel`; Funnel would make the endpoint
public. The configured `origin` must exactly match the HTTPS URL reported by
Serve, including a non-default port if one is used. Keep a Tailscale grant or ACL
that limits access to the intended phone and administrator identities. TFR also
requires the `Tailscale-User-Login` identity header added by Serve and binds each
device credential to the identity that redeemed its pairing link; Funnel traffic
does not carry this header and is rejected.

Create a ten-minute, single-use pairing link through the Gateway's owner-only
Unix socket:

```console
tfr pair --device-name "My iPhone"
```

Open the printed URL on the phone while Tailscale is connected. The pairing
secret is in the URL fragment and is removed before the PWA makes a request, but
the URL should still be treated as a short-lived secret and kept out of shell
transcripts and messages. In Safari, use Share > Add to Home Screen after
pairing.

List and revoke paired devices locally:

```console
tfr devices
tfr revoke-device --device-id eb4fd272-a20a-444e-8b4d-93f2e0ad2713
```

Add `--socket PATH` to these commands when the Gateway uses a non-default Unix
socket. Revocation invalidates the credential and closes any active PWA socket.
Deleting `web_gateway.state_directory` revokes every device, but normal
administration should use `revoke-device`. Device credentials expire after 180
days and authorize only non-agent worlds that existed when the link was created.
Pair again after adding a world that the phone should access.

The PWA provides retained and live world output, world switching, unread counts,
per-world drafts and bounded command history, command submission, safe links,
and reconnect/backfill. It intentionally does not provide agent controls, world
connection controls, local shell access, plugin UI, or offline command queues.
iOS may suspend its WebSocket in the background; returning to the app reconnects
from the last committed cursor. If retained history no longer covers the gap,
the app reports that truncation instead of pretending the transcript is
complete.

## Separate Linux UI Host

The UI can run on a Linux computer other than the Gateway host. Install the
same TFR revision on that computer and launch only `tfr ui`; the installation
contains both modes, but UI mode does not start world connections, agents, or a
network listener. Use a normal non-root account with Python 3.12 or newer,
[`uv`](https://docs.astral.sh/uv/getting-started/installation/), and private
network access to the Gateway. Keep the UI and Gateway on the same reviewed TFR
revision when upgrading them.

Clone the repository on the UI host as an installer bootstrap:

```console
git clone https://github.com/Blaag/tfr.git ~/tfr
```

Alternatively, copy an existing bootstrap checkout from an administration
machine:

```console
ssh USER@UI_HOST 'mkdir -p ~/tfr'
rsync -az \
  --exclude '.git/' \
  --exclude-from '.gitignore' \
  ./ USER@UI_HOST:~/tfr/
```

On the UI host, install and activate the exact latest stable release. This does
not install the bootstrap checkout's branch:

```console
cd ~/tfr
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

Create a private configuration directory on the UI host:

```console
tfr_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/tfr"
mkdir -p "$tfr_config_dir"
chmod 700 "$tfr_config_dir"
```

Then create `config.jsonc` in that directory for UI preferences. A remote UI
needs only the main configuration; the `worlds.jsonc` and `agents.jsonc` paths
use their defaults but are not opened in UI mode:

```jsonc
{
  "schema_version": 1,
  "ui": {
    "scrollback_lines": 20000,
    "recent_input_lines": 3,
    "theme": { "preset": "catppuccin-mocha" },
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
chmod 600 "${XDG_CONFIG_HOME:-$HOME/.config}/tfr/config.jsonc"
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
~/.local/bin/tfr ui \
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
  '~/.local/bin/tfr ui \
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

## Themes

Available `ui.theme.preset` values are:

- `default`
- `catppuccin-latte`
- `catppuccin-frappe`
- `catppuccin-macchiato`
- `catppuccin-mocha`
- `gruvbox`
- `tokyo-night`
- `dracula`
- `nord`
- `solarized-dark`
- `nightfly`
- `kanagawa`
- `1976`

When `ui.theme` is omitted, TFR selects `default`, preserving its original
terminal-dependent appearance. Every other preset applies an explicit
foreground and background to the full UI, including world markers, borders,
prompts, status bars, selection, boss views, and TFR-generated notices.
The `1976` preset uses dark walnut and parchment neutrals with harvest gold,
burnt orange, avocado green, and terracotta accents.

Override any semantic role without redefining the component style map:

```jsonc
"ui": {
  "theme": {
    "preset": "catppuccin-mocha",
    "colors": {
      "accent": "#89b4fa",
      "warning": "#f9e2af",
      "selection": "#585b70",
    },
  },
}
```

Available roles are `background`, `surface`, `overlay`, `text`, `muted`,
`accent`, `secondary`, `info`, `success`, `warning`, `error`, `selection`, and
`selected_text`. Colors must use `#RRGGBB` notation. The optional legacy
`ui.output_color` setting remains supported and overrides only the default
foreground of otherwise unstyled world output; when omitted, that output uses
the theme's `text` color.

World-provided ANSI colors and explicit plugin effect colors take precedence
over the theme. This preserves server color semantics and plugin artwork while
the surrounding TFR interface remains consistent. Catppuccin works best in a
terminal and multiplexer configured for true color; Prompt Toolkit will
otherwise approximate the RGB colors at the terminal's available color depth.

## Terminal Controls

- `Enter`: send input to the active world.
- Pasting multiple lines sends them to the active `bare`, TinyMUSH, or TinyMUX
  world as preflighted, server-escaped, paced `@emit` commands. Internal blank
  lines and indentation are preserved; a single pasted line remains editable.
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
ALIAS`, `/next` (`/n`), `/previous` (`/p`), `/connect`, `/disconnect`, `/reconnect`,
`/end`, `/nospoof show|hide|status`, `/animations on|off|status`,
`/lowbw on|off|status`, `/clear`, `/boss`, `/sh`, `/reload`, `/restart`,
`/gateway reconnect`, `/help`, and `/quit`, plus commands registered by enabled
plugins and configured world-switch aliases such as `/g`. Begin input with `//`
to send a literal leading slash. `/help` displays commands, aliases and any
collisions, loaded plugins, world markers, and keybindings.

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

`/boss` opens a plugin-provided full-screen cover. The built-in
`build-dashboard` disguises real per-world received counts and last-activity
times as log-monitor output, alternates fake histogram and flow diagrams, and
shows a fake artifact listing. World connections, buffering, and event logging
continue without exposing world text. Press Enter to restore TFR. Use `/boss
status`, `/boss cycle`, `/boss random`, or `/boss lock SCREEN` to select among
registered screens; `ui.boss` configures the startup mode. See
[`BOSS-VIEWS.md`](BOSS-VIEWS.md) for dashboard configuration and the complete
custom-screen API.

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
pressing `PageUp`, `PageDown`, `End`, or `/end` while a screen-clear
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
screen-clear effects, boss views, status segments, key bindings, and lifecycle
handlers.
Commands can include help text for `/help`. Enrichers return
`tfr.plugin_api.EventPatch`; they cannot replace event
identity or canonical text. Command and key handlers receive a
`PluginCommandContext` and submit typed commands with `await context.submit(...)`
rather than accessing sockets. `context.world_info` exposes the active world's
non-secret server type and effective text encoding.

Lifecycle handlers receive `PluginLifecycleEvent` values. In addition to
`application_start`, `application_stop`, and `session_state`, TFR publishes
`gateway_connected`, `gateway_disconnected`, `world_activity`,
`world_connected`, `world_disconnected`, and `boss_activated`. Operational
events identify their `source` and may include non-secret metadata; notably,
`world_activity` does not include the received message text. Plugins should
ignore lifecycle kinds they do not use so future additions remain compatible.

UI plugins can register multiple dynamic covers with
`registrar.register_boss_view(NAME, renderer)`. Renderers receive exact
per-world since-activation counters and timestamps plus at most 200 recent,
privacy-safe operational or plugin events. Other plugins can publish into the
active cover with `registrar.emit_boss_event(event)`. See
[`BOSS-VIEWS.md`](BOSS-VIEWS.md) for the complete context, event, selection,
refresh, and safety contracts.

Enable entry-point names under `plugins.enabled` in `config.jsonc`. Arbitrary
plugin-owned settings belong under `plugins.config.NAME`. A failed extension is
disabled and reported as a redacted internal plugin event without interrupting
world sessions or event logging.

Plugin packages are trusted native code and are not sandboxed: enabling one
runs its code with TFR's full process privileges. Only enable plugins you or
someone you trust wrote or reviewed.

### Installing Plugins From a Git Repository

For a repository that publishes TFR's stable plugin manifest, use an immutable
stable policy. `stable-auto` checks the live HTTPS manifest before plugin import,
installs only a compatible annotated tag at its exact commit, and atomically
activates it while retaining the previous release:

```jsonc
"plugins": {
  "enabled": ["cat", "film_burn"],
  "sources": [
    {
      "repo": "Blaag/tfr-plugins-public",
      "policy": "stable-auto",
      "manifest_url": "https://github.com/Blaag/tfr-plugins-public/releases/latest/download/plugin-manifest.json",
    },
  ],
}
```

The manifest binds the semantic version to the full commit, release artifact,
supported TFR and plugin API ranges, and exact entry-point inventory. TFR then
fetches only the manifest's tag, requires an annotated tag pointing directly to
that commit, and verifies the released `pyproject.toml` before import. It rejects
downgrades and a different commit for an already installed version. It never
falls back to `main`. If the manifest service is temporarily unreachable, TFR
may continue with the current release only after revalidating its metadata,
origin, tag, commit, project metadata, and clean checkout.

When `stable-auto` installs a newer release during startup, TFR reports the old
and new versions. Later releases discovered while the UI is running appear in
the periodic update notice and `/update status|check`; they are activated only
on a later startup or reload, before plugin import. `stable-notify` uses the same
status integration but never activates a newer release automatically.

Use `"policy": "stable-notify"` to bootstrap the current stable release but only
report later compatible releases. To roll back, first change `stable-auto` to
`stable-notify` so the next launch does not immediately update again, then run:

```sh
tfr --rollback-plugin Blaag/tfr-plugins-public
```

For multiple stable projects in one repository, qualify the source with its configured path,
for example `tfr --rollback-plugin owner/plugins:packages/extra`.

For a repository without a release manifest, `pinned` selects and revalidates
one exact lowercase 40-character commit:

```jsonc
{
  "repo": "someone/tfr-plugins-serious",
  "policy": "pinned",
  "ref": "b7f3c1b6a8e2d4c9f1a0b3e5d7c9a1f3e5d7c9a1",
}
```

The default `legacy` policy preserves the original convenience behavior: TFR
clones the repository directly and discovers its `tfr.plugins.v1` entry points
without a packaging or installation step.

`repo` accepts an `OWNER/REPO` GitHub shorthand (expanded to
`https://github.com/OWNER/REPO.git`), a full `https://` URL, or a `git@`/`ssh://`
URL for a private repository reachable with your own SSH key or agent. TFR only
invokes your local `git` binary (`clone`, `fetch`, `checkout`); it never sends
your credentials anywhere itself and never runs `pip install` or any build
step, so a source's own third-party dependencies (if any) must already be
available in TFR's environment.

Under `legacy`, a source is fetched once and then left alone on later launches.
Two optional fields change that behavior:

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
  // Legacy exact commits remain supported; explicit policy: pinned is clearer.
  { "repo": "someone/tfr-plugins-serious", "ref": "b7f3c1b6a8e2d4c9f1a0b3e5d7c9a1f3e5d7c9a1" },
  // A monorepo plugin living in a subdirectory.
  { "repo": "someone/tfr-plugin-monorepo", "path": "plugins/cool-effect" },
],
```

Legacy mutation is intentionally convenient rather than maximally safe. It
suits repositories whose maintainers you trust with a shell on this machine.
Prefer stable policies, use `pinned` for reviewed repositories without a stable
channel, and reserve legacy `auto_update` for development.

Checkouts live under `plugins.state_directory` (default
`~/.local/state/tfr/plugins`). Managed stable releases use versioned directories
under `managed/`, with atomic `current` and `previous` pointers. Legacy and
pinned clones remain outside that managed namespace.

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
