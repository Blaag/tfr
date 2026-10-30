# TFR

TFR is a Python terminal client for concurrent MUD and MUSH world sessions.
Why use it? It has a bunch of features:

* Support for TCP or verified TLS world sessions
* Automatic spoof detection
* A persistent multi-client gateway
* Full-screen multi-world terminal UI - run multiples against the same gateway!
* Loads of animations for screen clearing and customized speaker effects
* Cut/paste support that preserves formatting automatically
* Pasting images by automatically converting to ASCII
* Update notifications and coordinated updates across the gateway and all connected UI clients
* A progressive web app (PWA)
* Activity tracking in non-foregrounded worlds
* Typed JSONC configuration
* JSONL event logging with secret redaction

The complete design and implementation sequence are in [PLAN.md](PLAN.md).

## Quick Start

TFR requires `git` and network access. The setup assistant checks for
[`uv`](https://docs.astral.sh/uv/), offers to install it when missing, and uses
it to obtain Python 3.12 or newer. Clone the repository as a bootstrap checkout,
then run:

```console
git clone https://github.com/Blaag/tfr.git
cd tfr
./scripts/setup-tfr
```

The setup assistant recommends the no-Gateway, all-in-one mode for trying TFR,
but can also configure a local Gateway plus UI, a remote Gateway host, or a
remote UI host. It installs the latest verified stable release, creates private
configuration, can prompt for world connection details, enables the public
plugin catalog, generates profile launchers, and provides `tfr-doctor` checks.
Rerun it later to change profiles; it backs up files before approved replacement
and does not start TFR or install a background service.

If `uv` is missing, the assistant offers to run Astral's official installer or
waits while you install it yourself and re-checks, or exit cleanly. Remote setup
explains which token and public CA files a UI needs and which Gateway secrets
must never be copied. Public plugin options are documented at
<https://github.com/Blaag/tfr-plugins-public>.

The assistant prints the exact command for the selected profile:

```text
All-in-one:            ~/.local/bin/tfr-local
Local Gateway:         ~/.local/bin/tfr-gateway
Local attached UI:     ~/.local/bin/tfr-ui
Remote Gateway:        ~/.local/bin/tfr-gateway
Remote attached UI:    ~/.local/bin/tfr-remote-ui
Check selected setup:  ~/.local/bin/tfr-doctor
```

Nothing is started automatically. The recommended all-in-one profile does not
require a Gateway. The local split profile runs `tfr-gateway` in one terminal
and `tfr-ui` in another. Remote launchers include the selected TLS and token-file
options. Rerun `setup-tfr` later to change profiles.

The bootstrap checkout is not installed as `main`. The underlying installer
fetches the official stable manifest, verifies its immutable tag and commit,
builds that source with its committed lock file, and exposes it through
`~/.local/bin/tfr`.

## Manual Setup

To install without the setup assistant:

```console
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

Create the Gateway's pre-start bootstrap. It contains only the location of the
private Gateway-managed configuration store:

```console
tfr_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/tfr"
mkdir -p "$tfr_config_dir"
chmod 700 "$tfr_config_dir"
cp examples/gateway-bootstrap.jsonc "$tfr_config_dir/gateway.jsonc"
chmod 600 "$tfr_config_dir/gateway.jsonc"
~/.local/bin/tfr gateway --config "$tfr_config_dir/gateway.jsonc"
```

Run `~/.local/bin/tfr` for all-in-one mode. For persistent local connections,
run `~/.local/bin/tfr gateway` in one terminal and `~/.local/bin/tfr ui` in
another. They share `$XDG_RUNTIME_DIR/tfr/gateway.sock` when set, otherwise
`~/.local/state/tfr/run/gateway.sock`. Pass the same `--socket PATH` to both
commands to override it. `Ctrl-Q` or `/quit` closes only the attached UI; the
Gateway keeps worlds, logging, and agents running.

## Development

```console
uv sync
uv run tfr --help
uv run tfr --check-config --config examples/config.jsonc
uv run tfr --config examples/config.jsonc
uv run tfr gateway --config examples/gateway-bootstrap.jsonc
uv run tfr ui
uv run tfr --replay ~/.local/state/tfr/logs/tfr-TIMESTAMP-PID.jsonl
./scripts/check-security
uv run ruff check .
uv run pytest
```

Example configuration is under [`examples/`](examples/). In Gateway mode,
`--config PATH` selects the optional bootstrap shown in
`examples/gateway-bootstrap.jsonc`; normal settings are changed with `/config`,
and `/config status` reports the authoritative revision. Standalone mode still
accepts the legacy three-file examples. Credential-bearing bootstrap and state
files should be readable only by their owner (`0600` on POSIX systems).

Each Gateway-managed world may define command-style switching shortcuts, for
example `"aliases": ["g"]` makes `/g` switch to that world. Alias names are
case-insensitive, must use letters, digits, underscores, or hyphens, and must be
unique across worlds. Each world may define up to 32 aliases of at most 64
characters, with 256 aliases allowed across the configuration. Core and plugin
commands take precedence over conflicting world aliases; `/help` identifies
aliases that are shadowed.

Set aliases without the leading `/`. For example, this makes
`/d` switch to the world named `dragon`:

```jsonc
{
  "worlds": {
    "dragon": {
      "aliases": ["d"],
      "host": "dragon.example.org",
      "port": 4201,
    },
  },
}
```

Running `tfr` without a mode starts the original combined client. For a
persistent connection process, start `tfr gateway` and attach one or more
terminals with `tfr ui`. Both use
`~/.local/state/tfr/run/gateway.sock` by default, or `$XDG_RUNTIME_DIR/tfr/gateway.sock`
when `XDG_RUNTIME_DIR` is set. Pass the same `--socket PATH` to both commands to
override it. Closing an attached UI does not disconnect worlds or stop agents.
The gateway retains configured per-world scrollback and sends it to newly
attached or reconnecting UIs before switching atomically to live events.

`tfr gateway` remains one command and one systemd service. Internally, its
long-lived, lightweight supervisor owns world sockets while a replaceable
Gateway application process owns plugins, agents, UI clients, and web access.
Normal updates replace only the application process, so worlds stay connected.
The connector survives a failed application restart for 30 minutes and buffers
bounded replay data (1 MiB per world, 16 MiB globally). If either limit is
exceeded, the replacement Gateway and its UIs receive an explicit history-gap
warning. A clean service stop closes everything immediately.
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
by `current`. Activation changes new invocations only. A managed attached UI's
`/reload` re-enters through `current`, while bare `/update` coordinates verified
activation and process replacement for the Gateway and all connected native UIs.

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
match the manifest. It downloads the manifest-authenticated release bundle,
verifies its outer size and SHA-256 plus strict internal member hashes and
wheel identity, installs hash-locked binary dependencies, and installs the
exact commit-bearing wheel without rebuilding it. It never installs `main` or
modifies the bootstrap checkout. Verified stable releases use
`VERSION+stable.COMMIT` IDs so their
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
sources configured on the UI host. Periodic checks, `/update status`, and
`/update check` are notification-only. Bare `/update` is the explicit install
operation: it verifies and stages the configured stable release on the Gateway
and every native UI connected when the transaction begins, then activates and
restarts them only after every participant reports ready. Exact release links
are included in availability, preparation, and activation notices.

Automatic updates require every participating process to use a valid managed
installation and the same stable manifest. An unmanaged, incompatible, failed,
or disconnected native UI aborts preparation before activation. New native
connections retry while an update is in progress. A UI that was offline when
the transaction began must update after reconnecting. Protocol-changing releases
remain manual coordinated deployments because the running protocol requires an
exact version match.

Each participant invokes `uv` on its own host. Lookup checks that process's
`PATH`, then `~/.local/bin/uv` and `~/.cargo/bin/uv`. Errors identify whether
staging failed on the Gateway host or a native UI host; installing `uv` only on
the Gateway does not satisfy a separately hosted UI.

Browser clients do not install native code and do not participate in the
all-ready barrier. They receive a maintenance notice, reconnect after the
Gateway restarts, and use the existing service-worker update prompt for changed
PWA assets. Independently installed UI hosts that were not connected must run
`/update` themselves.

The TFR release manifest is cached with its HTTP `ETag` under
`~/.local/state/tfr/updates` by default. Plugin results are held in memory and
revalidated from each configured plugin manifest. Network and validation
failures do not interrupt startup or active sessions. Set `updates.enabled` to
`false` to disable periodic and `/update` checks, or configure the timing, HTTPS
manifest URL, and state directory under the top-level `updates` object. Only
stable `vMAJOR.MINOR.PATCH` releases participate; prereleases and moving Git tags
are not used.

Set `updates.channel` to `"candidate"` to opt into immutable candidate releases.
Candidate discovery uses GitHub prereleases named
`vMAJOR.MINOR.PATCH-candidate.N`; its cache is isolated from the stable channel.
The default remains `"stable"`. Stable publication promotes the exact wheel and
source archive previously exercised as a candidate rather than rebuilding them.

Candidate hosts can use `/update force` as a recovery path when ordinary update
coordination is blocked by forwarded-manifest or same-version candidate quirks.
Stable-channel processes reject this command. In forced mode, the Gateway and
every connected managed native UI independently fetch and verify their locally
configured candidate manifest, stage the exact same version and commit, and
abort unless all hosts agree before activation.

Bare `/update` and `./scripts/install-from-checkout --latest-stable` download,
verify, and activate the exact published release bundle. Both retain the
manifest-to-annotated-tag trust check, enforce bundle and member hashes, require
hash-locked binary dependencies, verify the installed version and embedded
commit, and activate only after staging succeeds. Background checks never start
installation.

Before staging, bare `/update` checks the Gateway and every connected native UI.
If their TFR builds and configured `stable-auto` plugins are current, it reports
that no update is available and leaves every process and world connection
running.

To replace only the Gateway application while preserving the supervisor,
connector, and world sockets, run on the Gateway host:

```console
tfr admin restart-gateway
```

Pass `--socket PATH` when the service uses a nondefault Gateway socket. Do not
use `systemctl restart` when preserving world connections matters; restarting
the systemd service stops the complete cgroup, including the connector.

From the bootstrap checkout, a normal update is:

```console
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

If a new release must be reverted, use
`./scripts/install-from-checkout --rollback`, then restart the affected process.
`--list` shows the exact release IDs selected by the `current` and `previous`
pointers.

Maintainers publish a release by updating `project.version` in `pyproject.toml`,
committing and pushing that change, previewing `./scripts/publish-release`, then
running `./scripts/publish-release --push`. The script validates a clean,
synchronized `main` and pushes only the matching immutable tag. The release
workflow tests the tag, embeds its exact commit in the wheel, and publishes the
wheel, source distribution, deterministic `tfr-VERSION-release.zip` bundle, and
checksummed `update-manifest.json` together. The bundle contains the wheel plus
hash-locked runtime requirements and strict identity/integrity metadata. Managed
updates install this bundle directly without rebuilding tagged source.
Configure the `release` GitHub environment to require maintainer approval, and
enable immutable releases plus protected release tags in the repository ruleset;
the workflow also rejects commits that are not on `main`. See
[MAINTAINER.md](MAINTAINER.md) for script responsibilities, release verification,
and failure recovery.

If release work or an exact-tag confirmation is interrupted, rerun the same
`./scripts/release-end-to-end VERSION` command. It detects the protected PR, tag,
workflow, approval, candidate, and stable-release state automatically; no separate
resume command or direct `publish-release` invocation is required.

## Remote Gateway

The Gateway can keep its private Unix socket while also listening for remote UIs
over authenticated TLS. Network mode is opt-in and requires a certificate,
private key, and shared token. Bind `--listen-host` to the dedicated machine's
Tailscale address or another specific private address; wildcard addresses are
rejected.

The recommended setup on the Gateway machine is:

```console
./scripts/setup-tfr
# Choose "Gateway server"
~/.local/bin/tfr-doctor
~/.local/bin/tfr-gateway
```

The assistant generates `~/.config/tfr/remote-ui-handoff.txt` with the TLS
hostname, port, token source path, public CA requirement, and safety guidance.
Give the UI operator that handoff through an authenticated channel. Copy only
the shared token and, for a private CA, its public certificate. Never copy the
Gateway TLS private key, `worlds.jsonc`, `agents.jsonc`, world passwords, or
provider keys. The commands below document the equivalent manual setup.

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
expects Tailscale Serve to provide its HTTPS origin.

The normal setup is a single command from an attached native UI:

```text
/web pair "My iPhone"
```

This explicitly authorizes TFR to discover the Gateway machine's MagicDNS
origin, start its loopback-only PWA backend, and add a private Tailscale Serve
root route when that route is unused. TFR reuses an exact existing route,
preserves unrelated path routes, refuses to replace a conflicting root route,
and never enables Funnel. Tailscale must already be installed, running, and
logged in; TFR does not start it or perform login.

No `web_gateway` configuration is required for on-demand setup. Advanced
deployments may specify an origin or disable the feature explicitly:

```jsonc
"web_gateway": {
  "mode": "on-demand", // or "disabled"
  // "origin": "https://gateway.example.ts.net",
  "listen_host": "127.0.0.1",
  "listen_port": 7348,
  "state_directory": "~/.local/state/tfr/web",
  "snapshot_events": 2000,
},
```

The legacy `enabled: true` setting still eagerly starts the listener and
requires an explicit origin. For manual provisioning, publish that loopback
service inside the tailnet with Tailscale Serve:

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

### Direct access without Tailscale

A Gateway that already has a public hostname, such as a cloud-hosted instance,
can serve the PWA directly over its own TLS instead of through Tailscale. Set
`web_gateway.transport` to `"direct"` and give the externally reachable hostname
the Gateway's certificate covers:

```jsonc
"web_gateway": {
  "mode": "on-demand",
  "transport": "direct",
  "listen_host": "0.0.0.0",
  "direct_host": "bsd.shutdown.com",
  "direct_port": 27047,
  // tls_cert/tls_key are optional; when omitted, direct mode reuses the
  // Gateway's own --tls-cert/--tls-key.
  // "tls_cert": "~/.config/tfr/gateway.crt",
  // "tls_key": "~/.config/tfr/gateway.key",
},
```

`listen_host` is the real local interface the socket binds — a wildcard
(`0.0.0.0`/`::`) or a specific bindable address such as a private VPC IP. It is
**not** the public address: on NAT-based hosts (for example an EC2 instance) the
public IP is not assigned to any local interface and cannot be bound.
`direct_host` is only the browser-facing hostname used for the origin and TLS
certificate matching.

In this mode the PWA listener terminates TLS itself and binds `listen_host`.
Then pair from any attached native UI, naming the public address to confirm it:

```text
/web pair "My iPhone" --gateway bsd.shutdown.com:27047
```

TFR verifies the `--gateway` address matches the Gateway's configured direct
origin before pairing. The same listener runs inside the Gateway process; no
separate web server, Tailscale, or reverse proxy is involved.

Requirements and caveats for direct mode:

- **Use a DNS hostname the certificate covers.** The browser validates TLS
  against `direct_host`, so it must appear in the certificate's subject
  alternative names. A bare IP address generally will not validate with a
  public certificate authority.
- **Use a dedicated port.** The PWA listener is its own HTTP/WebSocket socket,
  separate from the native remote-UI TLS listener (`tfr gateway --listen-host
  ... --listen-port`, default `7347`), which speaks TFR's own protocol and is
  not a web server. `direct_port` must be a free port that differs from the
  native `--listen-port`; the two cannot share one port. The examples here use
  `27047`.
- **The firewall is the outer boundary.** There is no Tailscale ACL in front of
  the listener, so restrict inbound access to `direct_port` to the intended
  devices with your host or cloud firewall.
- **`direct_host` must be a specific hostname, not a wildcard.** It names the
  browser-facing origin, so `0.0.0.0` or `::` are rejected. `listen_host` may be
  a wildcard, because it is the local bind interface guarded by the firewall.
  Public IPv6 literals are not currently supported as `direct_host`; use a DNS
  hostname (recommended) or an IPv4 address.
- **Changing transports requires re-pairing devices.** Tailscale credentials are
  bound to the Serve user identity, while direct credentials use the direct TLS
  bearer-token boundary. Existing credentials therefore fail closed after a
  transport change and each device must be paired again.

Direct mode replaces Tailscale Serve's identity header with possession of the
per-device bearer credential over authenticated TLS, the same trust model as the
Gateway's token-authenticated remote UI. See [SECURITY.md](SECURITY.md) for the
full boundary discussion.

From any attached native UI, create a ten-minute pairing link and scannable QR
code without logging into the Gateway host:

```text
/web pair "My iPhone"
```

Use `/web status` to show the canonical PWA and repair URLs, `/web devices` to
list paired devices, `/web repair` to show a scannable repair-page QR code, and
`/web revoke DEVICE_ID` to revoke one. These commands
travel over the existing authenticated native Gateway connection and are never
available to limited PWA credentials.

As an owner-only fallback on the Gateway host, the CLI provides the equivalent
administration commands through its Unix socket. Without `--socket`, it tries
both the runtime-directory and persistent-state defaults:

```console
tfr pair --device-name "My iPhone"
tfr devices
tfr revoke-device --device-id DEVICE_ID
```

A lost browser response may safely retry the exchange from the same
Tailscale identity without creating another device, provided the Gateway has not
restarted and the link has not expired:

Open the printed URL on the phone while Tailscale is connected. The pairing
secret is in the URL fragment, removed from browser history before submission,
and retained only in tab-scoped session storage for bounded recovery attempts.
The URL should still be treated as a short-lived secret and kept out of shell
transcripts and messages. After pairing, TFR shows instructions for the current
iOS browser. Use its Share menu and choose Add to Home Screen; if that action is
unavailable, open the page in Safari. **Reset and uninstall** revokes the device,
clears TFR-controlled browser data, unregisters its service worker, and then
explains the final iOS-required step for removing the Home Screen icon.
If an already-installed Home Screen app later needs to be paired again, open the
app and paste a new pairing link into its pairing screen. Safari and the installed
app can use separate cookie storage, so this avoids removing and reinstalling it.

List and revoke paired devices locally:

```console
tfr devices
tfr revoke-device --device-id eb4fd272-a20a-444e-8b4d-93f2e0ad2713
```

Add `--socket PATH` to these commands when the Gateway uses a non-default Unix
socket. Revocation invalidates the credential and closes any active PWA socket.
Deleting `web_gateway.state_directory` revokes every device, but normal
administration should use `revoke-device`. Device credentials expire after 180
days of inactivity. Successful authenticated use slides the Gateway record and
HttpOnly cookie out another 180 days, persisted at most once daily. Credentials
authorize only non-agent worlds that existed when the link was created.
Pair again after adding a world that the phone should access.

The PWA provides retained and live world output, validated ANSI-derived colors
and emphasis, world switching, unread counts, per-world drafts and bounded
command history, command submission, safe links, and reconnect/backfill. It
uses native iOS/browser spelling suggestions in the world composer by default;
the device-local Settings toggle controls browser `spellcheck`/`autocorrect` and
does not run or synchronize TFR's Python spellchecker. The composer also offers
a device-local, off-by-default setting that prefixes ordinary text with `say `;
explicit `:`, `;`, and `@` commands and the first tokens `p`, `po`, `pos`,
`pose`, `sa`, and `say` are left unchanged. Effects Lab command input keeps
native spelling disabled.

A single pasted line remains editable composer text and is never sent
automatically. A multiline paste opens a preview tied to the selected world and
requires explicit confirmation. The browser sends the source once; the Gateway
normalizes its newlines, escapes every line as literal `@emit` output, and sends
at most 100 lines and 64 KiB at a fixed pace. This is supported for `bare`,
`tinymush`, and `tinymux` worlds. The progress sheet can stop an active transfer.
Closing the initiating socket, revoking the device, disconnecting or replacing
the world connection, or stopping the Gateway cancels it. Lines already accepted
may have reached the world, and interrupted transfers never resume automatically;
retrying can duplicate those lines. The scoped browser protocol cannot submit
arbitrary command arrays or choose escaping, pacing, actors, or audit metadata.

The PWA supports the first bounded portable
plugin effects: foreground-color timelines
with static and reduced-motion fallbacks. It intentionally does not yet provide
agent controls, world connection controls, local shell access, transform-rich
plugin effects, arbitrary plugin-provided browser UI, or offline command queues.
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

The recommended setup on the UI machine is interactive and does not require a
JSONC file:

```console
git clone https://github.com/Blaag/tfr.git ~/tfr
cd ~/tfr
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr ui
```

On first run, TFR asks for a profile name, Gateway host and port, TLS certificate
identity, optional custom CA, authentication token, and proposed UI display
name. The profile stores only bootstrap metadata and a credential reference.
On macOS the default store uses Keychain when available; other platforms use a
private mode-0600 credential file. The token is never written into the profile
or an export.

Once connected, manage profiles from TFR with `/gateway setup`, `/gateway
profiles`, `/gateway switch NAME`, `/gateway rename NAME NEW-NAME`, `/gateway
repair NAME`, and `/gateway forget NAME --confirm`. Use `/gateway profiles
export` for a credential-free backup. TLS and authentication errors identify
the failed handshake; repair the selected profile and reconnect.

If TFR detects values in a legacy local UI configuration, it offers the
Gateway-managed migration flow. Run `/config migrate` on each named UI, inspect
the complete preview, resolve shared conflicts with `/config migrate choose KEY
UI-NAME` or `/config migrate skip KEY`, and apply with `/config migrate apply
confirm`. `/config migration-status` reports collected and applied revisions;
`/config rollback-migration confirm` restores the pre-migration Gateway document
when no later configuration change makes rollback unsafe. Legacy files are
copied before submission and are never rewritten; a separate marker records a
successful migration. Secret plaintext is omitted from proposals and previews.
After migration, legacy files no longer participate in precedence. Gateway
startup performs a one-time ordinary-value import only when its managed document
is empty, warns about every unmigrated legacy main file, and thereafter builds
runtime state exclusively from the Gateway document. Bootstrap-only and secret
values require separate setup.

Sensitive settings store references rather than plaintext. Use `env:VARIABLE`,
`file:/absolute/private/path`, or on macOS `keychain:SERVICE:ACCOUNT` as the
value of a secret-scoped `/config set`. Referenced files must not grant group or
other permissions. Exports and configuration synchronization omit these
references and resolved values.

Run `/config` or `/settings` in a connected native UI for the keyboard-driven
settings browser. It groups and searches editable settings, chooses the normal
scope and target, provides typed controls and a review step, and supports reset
and undo without requiring registry keys or JSON. Advanced users and automation
can continue using `/config show`, `/config set`, and the other explicit
subcommands; `/config summary` displays the aggregate inventory report. Use the
arrow keys to move, Enter to choose the focused row immediately, Space to mark
a row without closing the menu, Tab to move between buttons, and Escape to
cancel the current dialog. The keyboard cursor is shown as a bold reverse-video
row independently of the `(*)` or `[*]` marker for the chosen value.
Authenticated native Gateway connections use the
owner administration interface; restricted PWA devices do not.
Settings dialogs inherit the active theme's background, surface, text, accent,
selection, button, input, scrollbar, and error roles while retaining the
terminal-safe reverse-video keyboard cursor.

Structured plugin settings use trusted declarative forms. For example, open
`Plugin: Speaker Effects` to add, edit, reorder, delete, and preview speaker
rules with color, effect-specific, world, and message-kind controls. Saving
validates and commits the complete collection atomically, preserving unchanged
rules.

For scripts, provisioning, and assistive tools, use the non-interactive owner
interface. Mutations preview by default and require `--apply`; multiple
assignments are committed as one atomic batch:

```bash
tfr config list typing_glow
tfr config get ui.typing_glow.enabled
tfr config set ui.typing_glow.enabled off
tfr config set ui.typing_glow.enabled=off ui.typing_glow.duration_seconds=0.25 --apply
tfr config reset ui.typing_glow.enabled --apply
```

Use `--json` for stable machine-readable output, `--ui`, `--ui-type`, `--world`,
or `--shared` for explicit targeting, and `--gateway-profile NAME` for a stored
authenticated remote Gateway profile. Local commands use the owner Unix socket
and never place owner tokens in process arguments.

Plugin discovery and enablement are also Gateway-managed. Use `/plugins
available`, `/plugins installed`, `/plugins enabled`, or `/plugins failed` to
inspect the catalog; `/plugins show NAME` includes settings, capabilities,
portable state, and named incompatible clients. `/plugins enable NAME confirm`
and `/plugins disable NAME confirm` update the shared `plugins.enabled` setting
once for the Gateway, PWA, and native UIs. Clients reload when required. Unknown
screen-clear effects explain whether their plugin is unavailable, disabled,
failed, or incompatible instead of reporting only an unknown name.

The Gateway also compares verified plugin-source manifests with its known
catalog. Attached native UIs receive one concise notice when a release adds a
plugin; `/update` installs the verified source release. In `/config`, **Plugins
› Enabled** opens **Manage plugins**, a checkbox list of every installed plugin.
New plugins begin unchecked, Space toggles any number, and Apply atomically
saves the enabled set. Discovery never enables or executes plugin code.

An authenticated native owner UI can restart the Gateway application with
`/gateway restart`. The command acts immediately, world connections remain
active in the connector process, and attached UIs reconnect automatically.

In TFR, `/help` opens a concise topic index rather than a complete command wall.
Use `/help TOPIC` to browse relevant commands or `/help COMMAND` for exact usage,
examples, scope, availability, and related commands. `/config help` and
`/plugins help` open their command-specific pages. Help hides commands that do
not apply to the current session while a direct lookup explains what capability
or connection is missing. Misspelled commands, settings, and effects provide
close matches and a concrete next action.

The remaining steps in this section are the equivalent manual procedure.

Portable transcript presentation is Gateway-owned for remote sessions. Configure
plugins such as `speaker_effects` and `terminal_reveal` with `/plugins` and
`/config`; a remote native UI does not evaluate or override their policy from a
locally installed copy. Client-only plugin and UI settings are Gateway-managed
under UI-type or individual-UI scope. If a legacy local `config.jsonc` remains,
the UI warns that its normal settings are ignored and offers `/config migrate`.
The remote native Effects Lab uses Gateway-projected demos, so its speaker
previews match live remote presentation policy.
Standalone mode is unaffected because its process acts as both policy authority
and renderer.

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

On the UI host, manually install and activate the exact latest stable release.
This does not install the bootstrap checkout's branch:

```console
cd ~/tfr
./scripts/install-from-checkout --latest-stable
~/.local/bin/tfr --version
```

Do not create a UI preferences file. Run `~/.local/bin/tfr ui` and complete the
interactive connection setup; the generated profile contains only endpoint,
TLS identity, credential reference, and stable UI ID. Configure preferences with
`/config set ... --scope ui` for this UI or `--scope ui-type` for terminal
defaults. Plugin availability and enablement are managed with `/plugins`.

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
- `/image` reads an image from the local macOS clipboard, or from the Linux
  clipboard when `wl-paste` or `xclip` is installed. Windows clipboard images
  are not yet supported. It renders an aspect-correct 72-column preview and
  sends it only after Enter confirms. Use `+`/`-` to adjust its width up to 80
  columns, `A` for ASCII, `U` for Unicode Braille when enabled for that world,
  or Escape to cancel. `/image PATH` loads a BMP, GIF, ICO, JPEG, PNG, TIFF, or
  WebP file instead, including on Windows or an SSH host where the desktop
  clipboard is unavailable. `--width`, `--ascii`, and `--unicode` select the
  initial preview settings. ASCII output is uncolored grayscale by default;
  `--withcolor` restores xterm-256 ANSI color for worlds that preserve it.
- `F6`, `Ctrl+Right`, or `Option+Right`: switch to the next world.
- `F5`, `Ctrl+Left`, or `Option+Left`: switch to the previous world.
- `PageUp` and `PageDown`: navigate scrollback or advance `More` output.
- `Tab`: when the input is empty and more world output is below the current
  view, act as a second `PageDown`; otherwise retain normal input behavior.
- `End`: return to live output.
- `Ctrl+L`: clear visible input and output without deleting command history or
  retained scrollback.
- `Ctrl+R`: reconnect the active world.
- `Ctrl+Q`: quit cleanly.
- Left-click a world in the top bar to switch to it.
- Drag across world output to highlight it and copy the plain text to the clipboard.
- Click an underlined `http://` or `https://` link in world output to open it in
  your default browser.
- `ui.mouse_mode` controls mouse ownership. `tfr` (the default) enables TFR's
  world switching, links, selection, and clipboard copying. `terminal` leaves
  mouse handling to the terminal emulator. `auto` selects terminal handling
  under mosh and TFR handling otherwise. Use `/mouse auto|terminal|tfr|status`
  to inspect or change the mode for the running UI.

Input beginning with `/` is a client command. Available commands are `/world
ALIAS`, `/next` (`/n`), `/previous` (`/p`), `/connect`, `/disconnect`, `/reconnect`,
`/end`, `/nospoof show|hide|status`, `/animations on|off|status`,
`/lowbw on|off|status`, `/mouse auto|terminal|tfr|status`,
`/spellcheck on|off|status|undo`, `/clear`, `/boss`,
`/image`, `/sh`, `/reload`, `/restart`,
`/gateway reconnect`, `/help`, and `/quit`, plus commands registered by enabled
plugins and configured world-switch aliases such as `/g`. Begin input with `//`
to send a literal leading slash. `/help` displays commands, aliases and any
collisions, loaded plugins, world markers, and keybindings.

Optional local spell checking is disabled by default. Enable it in
`ui.spellcheck.enabled` or for the current process with `/spellcheck on`. It
checks only the conversational payload after a leading `"`, `say `, `:`, or
`pose ` (case-insensitive for word commands). Other input, including MUSH and
system commands, pages, channels, semiposes, and multiline pastes, is unchanged.
Corrections are conservative, English-only, and performed locally without
sending draft text to another service. URLs, paths, identifiers, capitalized
names, world names, observed speaker names, profanity, and configured protected
words are not replaced.

Corrected words appear bold, underlined, and bright in the matching speech echo
in world output. Echo matching is local, short-lived, and exact; if no matching
echo arrives, unrelated output is left unchanged. `/spellcheck undo` restores
the most recent original pre-correction draft for the active world; it cannot
retract text that was already sent. Set global or per-world vocabulary through
`/config`; the value shape is:

```jsonc
"ui": {
  "spellcheck": {
    "enabled": false,
    // Protected in every world.
    "protected_words": ["TFR", "MUSH", "NOSPOOF"],
    // Each key is an exact Gateway-managed world name. Its words are
    // additionally protected only while sending to that world.
    "worlds": {
      "my-world-name": ["Alice", "Blaag", "TinyMUX"],
    },
  },
}
```

Image output uses portable ASCII glyphs unless the target world explicitly
declares that it preserves Unicode with `worlds.NAME.capabilities.unicode`:

```jsonc
"capabilities": {
  "unicode": true,
}
```

This setting is a trust declaration about the world and its clients, not an
encoding autodetection mechanism. The configured world encoding must still be
able to encode every generated glyph. Browsers and other clients that strip
ANSI color retain the ASCII or Braille shape, although narrow displays may wrap
rows selected for a wider shared transcript.

After a laptop sleep or transient network loss, TFR verifies the Gateway
connection is still alive on a background interval, and automatically retries
`/gateway reconnect`'s own logic (bounded attempts, configurable interval)
before asking you to run it manually. This only replaces the stale transport;
it restores missed retained events and preserves the current UI. Configure
attempt count and timing under the Gateway-managed `ui.gateway_reconnect`
settings:

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

Newly typed input glows bright white and fades to normal text over half a second
by default. Newly inserted characters are bold during the fade. Configure
`ui.typing_glow.enabled`, `ui.typing_glow.duration_seconds`,
`ui.typing_glow.highlight_color`, and `ui.typing_glow.bold` to tune it.
`/animations off` and
`/lowbw on` suppress the typing effect. Spellcheck corrections remain bold and
underlined in recent input and matching world echoes; those highlights fade when
animations are enabled and remain static until expiry in low-bandwidth mode.

Ctrl-L clears the active draft and visible recent-input rows, then snapshots and
clears the visible output. Input history remains available with Up. UI plugins
can register finite output-only screen-clear transitions;
`ui.screen_clear.mode` chooses `cycle`, `random`, or `locked`, and locked mode
requires an `effect` matching an enabled plugin name.
Effect contexts include both plain snapshot lines and the corresponding styled
fragments so transitions can preserve the visible text colors.
Screen-clear effects may run for up to 60 seconds at no more than 30 frames per
second. Stateful effects can provide a completion check to keep the transition
visible past that nominal duration until their simulation finishes.
Use `/clear status`, `/clear cycle`, `/clear random`, or `/clear lock EFFECT` to
change selection for the running UI. `/clear` without arguments behaves like
Ctrl-L. With no available effect plugin, output clearing is immediate. The underlying
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
Unread worlds are also summarized in the output pane's bottom border near the
editor. After a two-cell inset, the border permanently reserves a ten-cell pager
field so activity always begins in the same column. When output remains below the
current view, that field contains a reversed `More` count (visually capped at
`9999`); otherwise it remains normal border glyphs. Narrow terminals show only
complete world entries followed by `...`.
Consecutive attributed says or poses from the same speaker form a five-minute
conversation combo. The third message starts a white `Speaking Spree`; subsequent
messages advance through green `Rampage`, blue `Dominating`, purple `Unstoppable`,
and orange `GODLIKE`. The message-body animation intensifies at each level, and
Godlike adds a bounded deterministic firework burst over the output viewport.
Further messages update the centered border/HUD notice as `GODLIKE x2`, `x3`, and
so on without repeating message effects or fireworks. Another speaker or more
than five minutes of inactivity resets the streak. Bare-world pose attribution
uses the same presentation-only first-word fallback as speaker effects. Combo
state continues counting while `/animations off`, `/lowbw`, or reduced motion is
active, but those modes suppress every combo visual and never replay it later.
Speaking streaks are enabled by default. Set `ui.speaking_streaks.enabled` to
`false` to disable tracking and visuals in native UIs and the Gateway-served
PWA, or use `/streaks on|off|status` to control the current native UI process.
Disabling or re-enabling clears existing streak state so a later streak always
starts fresh.
Use `/effects` (or `/demoeffects`) to open the local Effects Lab. `/teststreak all`
plays the complete combo sequence, `/teststreak 3` through `8` previews one tier,
and `/testspeaker NAME` renders the actual configured speaker decorator and
portable presentation program. `/effects effect NAME` previews one built-in text
effect; `/effects effect all` previews every effect as fictional speaker
`WilfordBrimley`. Effects Lab is a local entry in normal world navigation, with
its own preserved input and output. It appears in navigation only after
`/effects` explicitly opens it; `/effects close` removes it again. Switching to
another world leaves the lab, and returning restores it while it remains open.
Lab input is never sent to a world, logged as a canonical event, or provided to
an agent. Real worlds remain connected and continue buffering while it is open.
The paired web client offers the same lab from Settings, whether running as an
installed PWA or an ordinary browser tab.
The active world's recently sent commands remain directly above the editor.
These lines are separate from server output, so server speech echoes are not
duplicated. Configure their number with `ui.recent_input_lines` (default `3`, or
`0` to hide the pane). Blank Enter advances these visible rows without adding an
empty command to input history, so Up still recalls the last command. `PageUp`
after `Ctrl+L` reveals retained pre-clear output;
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
Plugins can register bounded `EffectDemo`/`EffectDemoSample` descriptors with
`register_effect_demo()`. Effects Lab renders those samples through the same
decorators and presentation programs used by live events; configured
`speaker_effects` rules receive compatible fallback demos automatically.

The display-decorator API remains terminal-local. The additive
`register_presentation_decorator()` API keeps plugins as the policy layer while
allowing them to emit bounded, declarative effect programs interpreted by both
the terminal UI and PWA. Version 1 supports static foreground, bold, underline,
foreground-color timelines, and bounded per-grapheme color/case sweeps with
explicit reduced-motion and unsupported-capability fallbacks. Browser-rich
transform variants remain planned. The API,
limits, pure-decorator requirement, and incremental migration from named
`TextEffectKind` recipes are documented in [`PRESENTATION.md`](PRESENTATION.md).
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

Enable entry-point names with `/plugins enable NAME confirm`. Arbitrary
plugin-owned settings belong under Gateway-managed `plugins.config.NAME`. A failed extension is
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
and new versions with the exact release link. Bare `/update` also activates
compatible `stable-auto` releases when each process restarts and imports its
plugins. `stable-notify` uses the same status integration but never activates a
newer release automatically.

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
For bare and generic output without sender provenance, the terminal presentation
pipeline offers the first visible word (with an optional possessive removed) as
an inferred pose speaker to display decorators. This lets configured speaker
effects cover lines such as `Hamilton smokes a cig`, at the cost of an occasional
visual false positive. The inference never changes event history, logs, agents,
spoof checks, or plugin event observers.
When NOSPOOF is enabled, TFR also compares apparent say and known pose speakers
with server attribution. Spoofed speaker names use a persistent inverse marker
in both the terminal UI and PWA, independent of normal text effects.

See [PRESENTATION.md](PRESENTATION.md) for the portable presentation direction,
[PWA-TESTING.md](PWA-TESTING.md) for the reproducible development Gateway,
mock-world, Playwright, and physical-device test workflow, [SECURITY.md](SECURITY.md)
for credential, TLS, logging, plugin, and agent security boundaries, and
[LICENSES.md](LICENSES.md) for the reviewed runtime dependency licenses. TFR is
distributed under the [MIT License](LICENSE).
