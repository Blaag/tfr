# Preparing Installations for the TFR 0.1.122 Compatibility Floor

TFR currently reports whether a Gateway and all retained UIs are ready for
removal of pre-0.1.122 compatibility code. Do not delete old state or rerun
migration merely because a compatibility report has a blocker.

## Upgrade and reconnect

1. Upgrade the Gateway and every native UI to the current stable or candidate
   release.
2. Open every retained native UI at least once so it can submit a fresh local
   report.
3. Open every paired PWA once so its current web shell and protocol are visible.
4. Query the central report from any native UI with `/compatibility`, from a PWA
   under **Settings → Tools → Compatibility**, or from a shell:

   ```sh
   tfr-doctor --compatibility
   tfr-doctor --compatibility --json
   ```

## Automatically adopted explicit connections

Some older native UIs start with explicit flags such as:

```sh
tfr ui --config ~/tfr/.tfr/config.jsonc \
  --gateway-host 192.168.1.7 --gateway-port 7347 \
  --tls-server-name gateway.example.ts.net \
  --token-file ~/.config/tfr/gateway.token
```

After that connection authenticates successfully, a managed TFR installation
with no existing profile atomically adopts the verified endpoint, token, TLS
settings, stable UI identity, and Gateway-assigned UI name into the current
profile/credential store. It also writes current remote-UI deployment metadata.
The original token and script are retained for rollback.

After one successful adopted start, simplify the script to:

```sh
exec ~/.local/bin/tfr ui --config ~/tfr/.tfr/config.jsonc
```

Do not omit `--config` when that file retains current plugin-source bootstrap
settings. Do not re-pair the UI or delete `native-ui.json`; doing so would create
a different UI identity.

## Interpreting blockers

- `profile-store-missing`: reconnect once using the verified explicit flags on a
  managed installation.
- `deployment-metadata-missing`: normally repaired together with profile adoption.
- `legacy-configuration-detected`: complete `/config migrate`; do not simply
  delete the source file.
- `report-missing`: start that retained UI once on a reporting-capable release.
- `report-stale`: reconnect that UI; reports become stale after 30 days.
- `below-version-floor` or `protocol-mismatch`: upgrade that UI.
- `gateway:migration-pending`: review `/config migration-status` and resolve or
  apply the actual pending proposal. Applied migrations may retain historical
  proposals and are not pending.
- `gateway:legacy-plugin-sources`: replace branch/default Git sources with a
  verified stable policy or an immutable pin.
- `gateway:legacy-speaker-rules`: edit the reported legacy Page targeting into
  current say/pose targeting.

## Completion

The installation is ready only when the central report says:

```text
Ready to retire pre-floor compatibility: yes
```

Keep migration backups and recovery bundles until all retained clients report
current and the deployment has survived a reboot. Compatibility reports contain
no credentials, paths, endpoints, world names, or configuration values and are
safe to share for support.
