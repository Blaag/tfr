# Complete Backup and Restore

TFR has three deployment roles. Back up the role you actually run:

- **Gateway:** owns worlds, plugins, shared settings, named-UI settings, world
  passwords, provider/API keys, and other managed secrets.
- **UI-only host:** owns Gateway connection profiles, TLS trust material, stable
  UI identities, and Gateway authentication tokens. Its ordinary settings live
  on the Gateway.
- **All-in-one:** owns its configuration directly in local JSONC files. It has
  no separate Gateway connection token.

The complete procedures below intentionally include plaintext secrets. Keep
these backups on trusted private storage and delete them securely when they are
no longer needed. TFR creates its own private exports with mode `0600` and
refuses to overwrite an existing file.

These procedures back up **configuration and credentials**. Logs, recall data,
and transcript/message history are operational data, not configuration; archive
their configured directories separately if you also want those records.

## Gateway: Complete Configuration and Credential Backup

A complete Gateway recovery uses one private portable archive created with
`--include-secrets` through the Gateway's local owner socket. In addition to the
managed configuration, it contains the Gateway listener token and TLS keypair,
stable native-UI authority, PWA credentials and push subscriptions, the VAPID
private key, and the expected private web origin/route.

The portable archive includes all managed worlds, plugins and plugin sources,
shared/Gateway/UI settings, named-UI settings, and the **resolved plaintext value
of every managed secret**, including world passwords and provider/API keys. If
any secret reference cannot be resolved, export fails rather than silently
creating an incomplete private backup.

### Export from an attached TFR UI

Run this slash command inside an authenticated native UI:

```text
/config export /absolute/private/path/tfr-gateway-complete.json --include-secrets
```

The file is written on the UI host. The UI must have owner/admin authority.

### Export from a shell

From the Gateway host using its owner Unix socket:

```sh
tfr config export /absolute/private/path/tfr-gateway-complete.json --include-secrets
```

An authenticated remote profile can export the managed configuration and its
managed secrets, but cannot read Gateway host recovery state:

```sh
tfr config --gateway-profile default export \
  /absolute/private/path/tfr-gateway-complete.json --include-secrets
```

Use the local owner-socket form when the archive must rebuild the host without
re-pairing clients.

The export reports whether Gateway recovery state was included. Treat an export
that says `not included` as a managed-configuration archive, not a complete host
backup. Complete recovery export is intentionally unavailable through an
unprivileged remote connection.

### Restore a Gateway

1. Install or check out the desired current TFR release on the replacement host.
2. Ensure Tailscale is logged in as the same node identity when existing PWA URLs
   must remain valid.
3. Run setup with the private archive and choose **Gateway server**:

```sh
./scripts/setup-tfr --recovery /absolute/private/path/tfr-gateway-complete.json
```

While acceptance-testing a published candidate, check out that exact candidate
tag and add `--candidate`. Setup verifies that `HEAD` has exactly one candidate
tag and installs that checkout instead of silently downgrading to latest stable:

```sh
./scripts/setup-tfr --candidate \
  --recovery /absolute/private/path/tfr-gateway-complete.json
```

4. Approve the generated user service and start it. Setup restores private state,
   imports the managed configuration, verifies the private Tailscale Serve route,
   and refuses a different Tailscale origin rather than silently breaking PWAs.
5. Verify with `tfr config status` and `tfr-doctor`.

The lower-level preview/apply commands remain available when restoring managed
configuration without replacing the host:

From an attached native UI, preview and then apply:

```text
/config import /absolute/private/path/tfr-gateway-complete.json
/config import /absolute/private/path/tfr-gateway-complete.json confirm
```

From a shell, preview and then apply:

```sh
tfr config import /absolute/private/path/tfr-gateway-complete.json
tfr config import /absolute/private/path/tfr-gateway-complete.json --apply
```

For a remote stored profile, add `--gateway-profile NAME` before `import`.
Import writes every included managed secret into new private files and restores
managed references atomically. Host bootstrap and client-continuity state are
restored only by `setup-tfr --recovery`.

Verify:

```sh
tfr config status
tfr-doctor
```

## UI-Only Host: Complete Connection Backup

A UI recovery bundle contains **every stored Gateway profile**, the active
profile, each endpoint and TLS server name, embedded custom CA certificates,
stable UI identities, display names, and every live Gateway authentication
token. It does not duplicate Gateway-owned worlds or settings.

Preserving the same stable UI identity reconnects the restored UI to its named
Gateway settings. Creating a new identity adds another independent UI.

### Export from the TFR UI

```text
/gateway profiles export /absolute/private/path/tfr-ui-recovery.json --include-token
```

### Export from a shell

```sh
tfr profiles export --path /absolute/private/path/tfr-ui-recovery.json --include-token
```

### Restore with the setup assistant

1. Install TFR from a fresh checkout.
2. Run `./scripts/setup-tfr` and choose **Remote UI**.
3. Enter the recovery-bundle path when prompted.
4. Choose **same UI identity** when rebuilding the original machine, or choose
   a new identity/name when adding another UI.
5. Run `~/.local/bin/tfr-doctor`, then start `tfr`.

The token is moved into the normal macOS Keychain or private credential-file
backend. Plugin sources and all ordinary UI settings are synchronized from the
Gateway.

### Restore from a shell

Restore the original stable UI identity:

```sh
tfr profiles restore --path /absolute/private/path/tfr-ui-recovery.json --same-ui
tfr ui
```

Create a distinct additional UI instead:

```sh
tfr profiles restore --path /absolute/private/path/tfr-ui-recovery.json \
  --new-ui-name "New Laptop"
tfr ui
```

Use `tfr profiles list` to verify restored profiles. The setup-assistant path is
recommended because it also writes deployment metadata and convenience
launchers; direct CLI restore requires launching `tfr ui` explicitly.

## All-in-One Host: Complete Backup

All-in-one mode has no separate Gateway token or portable Gateway archive. Its
JSONC files are the configuration authority and normally contain world
passwords and provider/API keys directly. Make a private copy of the entire
configuration directory and stable local UI identity:

```sh
mkdir -m 700 /absolute/private/path/tfr-all-in-one
cp -a ~/.config/tfr /absolute/private/path/tfr-all-in-one/config
mkdir -p /absolute/private/path/tfr-all-in-one/state/settings
cp -p ~/.local/state/tfr/settings/native-ui.json \
  /absolute/private/path/tfr-all-in-one/state/settings/ 2>/dev/null || true
```

If any JSONC value refers to an external secret file, certificate, or key, copy
that referenced file too. Environment variables and Keychain entries are not
files; record their values in a separate private secret store. Do not call an
all-in-one backup complete until every referenced secret can be supplied on the
replacement host.

### Restore all-in-one

1. Install TFR and choose **All-in-one** in `./scripts/setup-tfr`.
2. Before starting TFR, replace the generated `~/.config/tfr` with the backed-up
   directory.
3. Restore `native-ui.json` to `~/.local/state/tfr/settings/` when present.
4. Restore every external secret/certificate/key to its configured path or
   restore the corresponding environment/Keychain value.
5. Validate and start:

```sh
tfr --check-config
tfr
```

## Full Deployment Recovery Checklist

For a Gateway plus one or more UI hosts, a complete recoverable set is:

- [ ] Gateway portable archive created locally with `--include-secrets` and
      reporting `Gateway recovery state: included`.
- [ ] One UI recovery bundle from each UI host whose profiles or stable identity
      must be recoverable. One bundle includes all profiles on that host.
- [ ] Any desired logs, recall database, or transcripts archived separately.
- [ ] Restore tested with `tfr-doctor` and `tfr config status`.

Never use the credential-free `/gateway profiles export` display or a Gateway
configuration export without `--include-secrets` as the only disaster-recovery
backup. Those forms are intentionally incomplete.
