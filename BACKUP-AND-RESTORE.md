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

A complete Gateway recovery has two parts:

1. A portable Gateway configuration archive created with `--include-secrets`.
2. The Gateway host bootstrap credentials: listener token, TLS certificate and
   private key, and optionally PWA device state.

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

Or from a UI-only host using a stored authenticated profile:

```sh
tfr config --gateway-profile default export \
  /absolute/private/path/tfr-gateway-complete.json --include-secrets
```

### Back up Gateway bootstrap credentials

The portable configuration archive does not contain the token or TLS private
key used to reach the Gateway itself. On the Gateway host, inspect the generated
launcher to see the exact `--token-file`, `--tls-cert`, and `--tls-key` paths:

```sh
cat ~/.local/bin/tfr-gateway
```

Copy all three files to private backup storage, preserving permissions. Also
copy the Gateway bootstrap and web-device state:

```sh
mkdir -m 700 /absolute/private/path/tfr-gateway-host
cp -p /path/from/--token-file /absolute/private/path/tfr-gateway-host/
cp -p /path/from/--tls-cert /absolute/private/path/tfr-gateway-host/
cp -p /path/from/--tls-key /absolute/private/path/tfr-gateway-host/
cp -a ~/.config/tfr /absolute/private/path/tfr-gateway-host/config
cp -a ~/.local/state/tfr/web /absolute/private/path/tfr-gateway-host/web
```

The `web` directory preserves paired PWA credentials, push subscriptions, and
the VAPID private key. Omit it only if re-pairing every browser device is
acceptable.

### Restore a Gateway

1. Install TFR on the replacement Gateway host and choose **Gateway server** in
   `./scripts/setup-tfr`.
2. Restore the saved listener token, TLS certificate, and TLS private key to the
   paths used by the generated Gateway launcher. Keep tokens/private keys mode
   `0600`.
3. Restore `~/.local/state/tfr/web` before starting the Gateway if existing PWA
   devices must remain paired.
4. Start the Gateway and verify it with `tfr-doctor`.
5. Import the complete configuration archive using either interface below.

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
Import writes every included secret into new private files and restores managed
references atomically. It reports and restarts the Gateway when required.

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

- [ ] Gateway portable archive created with `--include-secrets`.
- [ ] Gateway listener token.
- [ ] Gateway TLS certificate and private key.
- [ ] Gateway `web` state if PWA devices must stay paired.
- [ ] One UI recovery bundle from each UI host whose profiles or stable identity
      must be recoverable. One bundle includes all profiles on that host.
- [ ] Any desired logs, recall database, or transcripts archived separately.
- [ ] Restore tested with `tfr-doctor` and `tfr config status`.

Never use the credential-free `/gateway profiles export` display or a Gateway
configuration export without `--include-secrets` as the only disaster-recovery
backup. Those forms are intentionally incomplete.
