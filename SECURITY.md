# Security

## Automated Security Checks

GitHub secret scanning and push protection inspect committed and newly pushed
content. CodeQL default setup scans Python and GitHub Actions with the extended
security query suite. Dependabot monitors the locked `uv` dependency graph for
known vulnerabilities, opens security updates without waiting for the routine
update schedule, and proposes grouped weekly updates for Python packages and
GitHub Actions.

Pull requests run dependency review and fail when they introduce a known
moderate-or-higher vulnerability in a runtime, development, or unknown-scope
dependency. Automated pull requests and security alerts require maintainer
review; TFR does not merge dependency changes automatically.

## Credentials

TFR permits world passwords and model API keys in JSONC by design. Files that
contain credentials should have mode `0600` on POSIX systems; configuration
loading warns when group or other permission bits are present. TFR does not
print loaded configuration objects or include provider keys in model context,
inspection views, plugin diagnostics, or event metadata.

Automatic login sends the real password only to the configured world socket.
The corresponding command audit event is redacted. Password-changing commands,
authorization-like metadata keys, tokens, secrets, and API-key fields are also
redacted before JSONL serialization.

## Connections

Direct TLS verifies certificates and hostnames by default. A custom CA file and
server hostname can be configured per world. Disabling verification is
supported for controlled development environments but permits interception.
Plain TCP provides no transport confidentiality.

## Gateway

Gateway mode listens only on a Unix-domain socket by default. TFR creates the
socket's parent directory with mode `0700` and the socket with mode `0600`; an
existing path is never removed automatically. Every attached process has full
command and connection-control authority.

An explicit `--listen-host` adds a TLS network listener without removing the
local Unix socket. Network mode requires a verified certificate and a shared
authentication token containing at least 32 ASCII bytes. Token and private-key
files must be regular, owner-only files and cannot be symlinks. Authentication
is checked before descriptors or retained events are sent. Wildcard binds,
plaintext TCP, unverified certificates, and disabled hostname checks are not
supported. Unix and TCP clients have separate quotas so unauthenticated network
connections cannot consume local UI capacity. Treat possession of the token as
full Gateway control, rotate it if exposed, and use Tailscale ACLs or a host
firewall as an additional restriction.

The gateway handshake exposes only world names and switch aliases, session IDs,
connection states, server types, encodings, and agent status. It does not
serialize world passwords, provider credentials, hosts, or complete
configuration objects.
Attached UIs read only the main UI/plugin configuration file; they do not open
the gateway's world or agent credential files.
Each event and command acknowledgement is bounded to 1 MiB. Slow clients are
detached when their event queue fills and can reconnect for retained backfill.

### Web Gateway

The optional mobile PWA is served by a separate HTTP/WebSocket listener that is
restricted to `127.0.0.1` or `::1`. Deploy it behind private Tailscale Serve,
never Tailscale Funnel, and configure one exact HTTPS origin. The service rejects
unexpected Host and Origin values and does not enable CORS. Safari may omit
`Origin` or send the opaque value `null` from the top-level pairing form; that
endpoint accepts those values but rejects any non-matching origin and still
requires the high-entropy code and Serve identity. Tailscale access controls
remain a second boundary rather than replacing application authentication. TFR
requires the `Tailscale-User-Login` header injected by Serve and binds each
paired credential to that identity. Funnel traffic does not carry the identity
header and is rejected.

Mobile devices pair through a high-entropy, single-credential, ten-minute code created
over the owner-only Gateway Unix socket. Concurrent or lost-response retries from
the same Tailscale identity return the same credential until the code expires or
the Gateway restarts; another identity is rejected. This accommodates browser
reloads and WebKit context changes, so the pairing URL remains a bearer secret
within that identity until expiry. The temporary plaintext recovery credential
is released from application-held references at expiry or revocation and is
never persisted. Redemption sets an opaque `Secure`, `HttpOnly`,
`SameSite=Strict`, host-only cookie. Only token
digests and non-secret device metadata are persisted, in an owner-only directory
and regular file with atomic replacement. `tfr devices` lists paired devices and
`tfr revoke-device` invalidates a credential and closes its active sockets.
Credentials expire after 180 days.

The browser removes the pairing fragment from history before a same-origin
top-level form submission. It temporarily retains the code in tab-scoped session
storage for at most three navigation attempts, then clears it after successful
session verification or terminal rejection. This avoids placing the code in a
query string or relying on WebKit to persist a cookie from a fetch response.

Paired devices have chat scope, not normal Gateway-client authority. The web
protocol accepts human world commands and ping requests only. It rejects
connection controls, agent controls, remote actor selection, arbitrary metadata,
sensitivity flags, and unknown fields. A bounded per-device request ledger makes
command retries idempotent while the Gateway process remains running. An
acknowledgement means the command entered the command bus, not that the remote
world executed it. Pairing grants the device only the non-agent worlds present at
that time; descriptors, history, live events, and commands are all filtered to
that fixed allowlist.

The browser receives an explicit projection rather than serialized internal
events. Telnet, plugin-internal, and agent audit events are excluded; arbitrary
metadata, model/provider details, session IDs, and canonical ANSI text are not
sent. Visible text is reduced to safe plain text and the PWA constructs DOM text
nodes rather than interpreting world output as HTML. Static responses use a
restrictive Content Security Policy and authenticated responses are marked
`no-store`. The service worker caches only the public application shell.

The PWA stores only non-command interface state such as the selected world and
Gateway identity in browser storage. Drafts, command history, transcript data,
and the active cursor remain in memory so command text is not retained across a
cold launch. A cold launch requests a fresh bounded retained-history snapshot;
an in-page reconnect resumes from its active cursor. Clear site data before
transferring or disposing of a paired phone. iOS can suspend background
WebSockets; the PWA reconnects and requests retained backfill when foregrounded
rather than sending commands offline.

## Logs

Complete inbound and outbound session logging is enabled by default. JSONL log
files are created with mode `0600` and flushed after each event. Redaction
protects configured credentials and recognized sensitive commands; arbitrary
secrets typed into ordinary world commands or received as world text cannot be
identified reliably and may be retained. Protect the log directory accordingly.

Transcript replay reads recorded event data without opening world or model
connections. It still renders ANSI styles contained in the transcript.

## Local Shell

The `/sh` command and `! command` input execute programs locally with the same
operating-system privileges, environment, and working directory as TFR. They
are explicit operator actions and are not sandboxed. Their commands and output
are not written to TFR's event log, but invoked programs can independently log,
modify files, access credentials, or communicate over the network.

## Clipboard

Dragging across world output copies the selected plain text to the clipboard.
TFR sends the selection through the terminal's OSC 52 clipboard protocol and
also invokes `pbcopy` for a local macOS UI. Clipboard access occurs only after
an explicit drag selection; ANSI control sequences are not copied.

## Plugins

Plugins are trusted local Python packages and execute with the same operating
system privileges as TFR. The supported API provides immutable events and
command-bus submission rather than sockets, but it is not a process sandbox.
Install and enable only reviewed plugins. Runtime extension failures are
reported without exception messages, then the failing parser, display, status,
or lifecycle extension is disabled.

`plugins.sources` supports immutable stable releases, exact commit pins, and a
legacy mutable Git mode. Stable manifests are fetched over HTTPS with bounded
size and strict fields. Before import, TFR verifies compatibility, rejects
downgrades and same-version commit changes, fetches only the declared tag,
requires that annotated tag to point directly to the manifest commit, and
matches the released project version and entry-point inventory. Stable releases
live in owner-controlled versioned directories and activate through atomic
relative pointers. The previous verified release is retained for rollback.
Stable policies never fall back to a branch. When offline they may reuse only
the current checkout after revalidating its origin, tag, commit, metadata, and
clean worktree.

An exact `pinned` source requires a lowercase 40-character commit and revalidates
its origin, `HEAD`, and clean worktree on every launch. Legacy `auto_update`
re-fetches a branch or tag and trades safety for convenience by design. Anyone
who can push to that ref controls code that runs with TFR's privileges on the
next launch. Reserve mutable sources for maintainers you trust with the account
running TFR. TFR never runs `pip install` or repository build steps for source
plugins, so dependencies must already exist in TFR's environment.

## Release Checks

Stable release checks fetch a bounded JSON manifest over HTTPS from the
configured URL. The default is the latest stable release asset in the official
GitHub repository. Checks run asynchronously, use HTTP `ETag` caching, and are
notification-only: TFR does not download or execute the advertised artifact.
Malformed, oversized, non-HTTPS, prerelease, or unexpected manifests are
rejected without interrupting startup or sessions.

The Gateway handshake shares only its TFR version, exact release commit when
available, and protocol version. The shared Gateway token grants no update,
package-management, filesystem, or restart operation. Gateway upgrades remain
explicit administrator actions followed by
a service restart. Treat a custom `updates.manifest_url` as a software supply
chain trust decision. The explicit source-based stable installer does not use
that configurable URL or the notification cache: it fetches the official live
manifest and only the manifest's fully qualified tag from the fixed official
repository. It requires an annotated tag that directly targets the declared
commit and verifies the tagged project version before executing candidate build
code. It never fetches or installs `main`, rejects normal stable downgrades and
same-version commit changes, and leaves direct verification and installation of
the manifest's wheel artifact as future work.
The release workflow builds without write credentials and transfers its outputs
to a separate write-capable job gated by the protected `release` environment.
Repository administrators must also enable immutable releases and protect stable
release tags; workflow `--verify-tag` alone does not make a Git tag immutable.

## Managed Installations

The checkout installer accepts only a clean Git `HEAD`; it builds, exports
dependencies, and hashes `uv.lock` from one `git archive` snapshot rather than
from mutable working-tree files. The exact commit is embedded in the wheel,
runtime dependencies must be hash-locked binary artifacts, and the installed
package must report the expected version and commit before activation. The
bootstrap uses an isolated environment instead of trusting the checkout's
ignored `.venv`. The wheel build backend is pinned and hash-constrained from
the same lock.

Managed installation roots and release directories must be owned by the current
user and must not be writable by group or other users. Path ancestors must be
owned by the current user or root; writable ancestors are accepted only when
the sticky bit protects their entries. Release metadata is owner-only, release
pointers must be relative symlinks into the managed `releases` directory,
concurrent modifications are prevented with an operating system lock, and
pointer and launcher replacements are atomic. The installer refuses to
overwrite an existing launcher it did not create. Do not run it as a more
privileged account than the account that runs TFR.

These checks protect against unsafe paths, cross-user modification, partial
installation, and accidental corruption. They do not protect against an
attacker who already controls the same operating-system account. Activating or
rolling back a release affects only later process launches. Gateway restart and
service configuration remain administrator responsibilities.

## Agents

Each agent is attached to a dedicated world session. World text is supplied to
the model as bounded, explicitly untrusted JSON context. Agents receive no
shell, filesystem, credential, arbitrary network, or general world-command
tools. Model output must validate as one `say`, `pose`, `page`, or `none` action;
newlines, page delimiters, oversized text, disallowed actions, stale responses,
and rate-limited actions are rejected before command submission.

Pausing or disconnecting cancels an in-flight model request. Accepted and
rejected decisions, selected event IDs, exact prompts, model responses, and
rendered commands are auditable. The inspector exposes only observable request
data and provider-supplied summaries, not hidden chain-of-thought.
