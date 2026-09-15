# Security

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

The gateway handshake exposes only world aliases, session IDs, connection
states, server types, encodings, and agent status. It does not serialize world
passwords, provider credentials, hosts, or complete configuration objects.
Attached UIs read only the main UI/plugin configuration file; they do not open
the gateway's world or agent credential files.
Each event and command acknowledgement is bounded to 1 MiB. Slow clients are
detached when their event queue fills and can reconnect for retained backfill.

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

`plugins.sources` lets TFR fetch and run plugin code directly from a Git
repository, with an optional `auto_update` that re-fetches on every launch.
This trades safety for convenience by design, for small groups who trust each
other's repositories: TFR only runs `git clone`/`fetch`/`checkout` (never
`pip install` or a build step) and only imports the Python module a source's
own `pyproject.toml` declares, but that import still executes with TFR's full
privileges, identically to any other plugin. Anyone who can push to a
non-pinned source's branch, or compromise that account, controls code that
will run on this machine the next time TFR starts. Pin any source you have
not personally reviewed to a full commit hash (`ref`, 40 hex characters),
which is never auto-updated regardless of `auto_update`, and never enable
`auto_update` on a branch or tag you would not trust someone with a shell on
this machine to control.

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
chain trust decision; future managed installation must verify both the manifest
source and the artifact's declared size and SHA-256 digest before activation.
The release workflow builds without write credentials and transfers its outputs
to a separate write-capable job gated by the protected `release` environment.
Repository administrators must also enable immutable releases and protect stable
release tags; workflow `--verify-tag` alone does not make a Git tag immutable.

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
