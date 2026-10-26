# Gateway-Managed Configuration Plan

> This repository copy is the authoritative implementation tracker.

## Goal

Make the Gateway authoritative for all TFR configuration. Each named UI receives a scoped configuration view. Local UI state contains only what is required to connect to a Gateway.

## Decisions

- [x] Gateway owns shared, Gateway, world, plugin, and per-UI settings.
- [x] Every UI has a stable ID and a configurable display name.
- [x] Native and PWA clients use the same configuration API.
- [x] No routine JSONC editing after migration.
- [x] Local UI storage is limited to Gateway connection profiles and cached, non-authoritative data.
- [x] Existing clients receive a guided, reviewable migration.
- [x] Diverging shared settings require conflict resolution; per-UI settings remain separate.

## Configuration scopes

- **Shared:** intended to behave consistently across clients.
- **Gateway:** service, web, update, logging, and security behavior.
- **UI type:** defaults for native TUI or PWA clients.
- **Individual UI:** overrides for one stable named UI.
- **World:** connection and behavior for one world.
- **Secret:** protected value or reference; never returned as plaintext.
- **Bootstrap:** minimum information needed to start or reach the Gateway.

Effective UI precedence:

```text
built-in default → shared value → UI-type default → individual UI override
```

---

## Phase 1 — Stable UI identity

### Data model

- [x] Define a stable, opaque UI ID.
- [x] Store UI records on the Gateway.
- [x] Record display name, client type, platform, versions, capabilities, last seen, and applied config revision.
- [x] Generate default names such as `office-mac · Terminal` or `John’s iPhone · PWA`.
- [x] Resolve duplicate default names with a short suffix.

### Protocol and commands

- [x] Extend native UI registration with stable UI identity and capabilities.
- [x] Extend PWA pairing/device records with the same identity model.
- [x] Add `/ui list`.
- [x] Add `/ui show NAME`.
- [x] Add `/ui rename [NAME]` for the current UI.
- [x] Add admin form `/ui rename TARGET NAME`.
- [x] Add `/ui forget TARGET` with confirmation.
- [x] Use UI names in status, diagnostics, updates, and audit messages.

### Verification

- [x] IDs survive restart and rename.
- [x] Names are unique and safely bounded.
- [x] Reconnecting clients reuse their existing records.
- [x] Limited PWA credentials cannot rename or forget other clients.
- [x] Tests cover duplicate names, stale clients, and malformed registration.

**Phase completion:** Gateway can reliably identify and name every UI.

---

## Phase 2 — Configuration registry and read-only inventory

### Registry

- [x] Define typed setting descriptors: key, type, default, scope, validation, consumers, sensitivity, and restart requirement.
- [x] Register existing core settings without changing persistence.
- [x] Add plugin-provided bounded metadata and configuration schemas.
- [x] Track value provenance: default, JSONC, managed override, or runtime.

### Read-only interface

- [x] Add `/config` summary.
- [x] Add `/config list [CATEGORY|SCOPE]`.
- [x] Add `/config show KEY [--ui NAME]`.
- [x] Show effective value, source, scope, consumers, and restart requirement.
- [x] Add `/config status` showing revision and per-UI convergence.
- [x] Never display secret values.

### Verification

- [x] Inventory accounts for every supported setting.
- [x] Effective values match current runtime behavior.
- [x] Provenance clearly distinguishes local JSONC and managed overrides.
- [x] Unknown keys provide close matches and contextual help.

**Phase completion:** users can inspect all effective configuration from TFR.

---

## Phase 3 — Gateway configuration store

### Storage

- [x] Expand or replace `ManagedSettingsStore` with one versioned Gateway document/database.
- [x] Use atomic writes, validation, revision checks, backups, and audit records.
- [x] Store shared, Gateway, UI-type, individual UI, world, plugin, and secret-reference sections.
- [x] Add schema migrations.
- [x] Add export with secrets omitted by default.

### Mutation API

- [x] Add Gateway protocol requests for get/set/reset/undo.
- [x] Enforce owner/admin versus self-service permissions.
- [x] Add `/config set KEY VALUE [--scope ...] [--ui NAME]`.
- [x] Add `/config reset KEY [--ui NAME]`.
- [x] Add `/config changes` and `/config undo`.
- [x] Return required reload/restart actions before confirmation.

### Verification

- [x] Concurrent edits produce revision conflicts rather than lost updates.
- [x] Invalid documents never replace the active revision.
- [x] Rollback restores the previous effective configuration.
- [x] Audit logs identify the acting UI by stable ID and name.

**Phase completion:** Gateway-managed settings can be safely changed from TFR.

---

## Phase 4 — Client configuration synchronization

- [x] Send effective configuration and revision after UI registration.
- [x] Have clients acknowledge applied revisions.
- [x] Push changes to connected clients.
- [x] Queue current state for offline clients to receive on reconnect.
- [x] Report unsupported values or capabilities instead of silently ignoring them.
- [x] Apply safe settings live.
- [x] Coordinate reloads for plugin/config changes.
- [x] Coordinate restarts only when required.
- [x] Treat any local copy as a cache, never an authority.

### Verification

- [x] Shared changes converge across Gateway, native UIs, and PWA.
- [x] Per-UI overrides affect only the named UI.
- [x] Offline and outdated clients are clearly reported.
- [x] Restart interruption does not lose the desired revision.

**Phase completion:** named clients consume and report Gateway-owned configuration.

---

## Phase 5 — Interactive connection setup

### Native UI first run

- [ ] Detect absence of a connection profile.
- [ ] Present an interactive Gateway setup flow.
- [ ] Collect Gateway address and required TLS identity.
- [ ] Pair or authenticate without requiring JSONC editing.
- [ ] Propose and allow editing the UI display name.
- [ ] Store credentials in the OS keychain where available.
- [ ] Store only Gateway endpoint, credential reference, and stable UI ID locally.

### Profile management

- [ ] Add `/gateway setup`.
- [ ] Add `/gateway profiles`.
- [ ] Add profile switch, repair, rename, and forget flows.
- [ ] Ensure exported profiles omit credentials.

### Verification

- [ ] A fresh installation can connect without editing files.
- [ ] Invalid TLS/authentication details give actionable recovery steps.
- [ ] Losing local state does not expose Gateway secrets.

**Phase completion:** users can install and connect a UI entirely through TFR.

---

## Phase 6 — Guided migration

### Proposal collection

- [ ] Detect legacy JSONC and managed overrides.
- [ ] Add `/config migrate` and first-run migration prompt.
- [ ] Have each UI submit a migration proposal tagged with its stable ID.
- [ ] Classify every value as shared, Gateway, UI-specific, world, bootstrap, or secret.
- [ ] Preserve source file and provenance for review.

### Reconciliation

- [ ] Deduplicate matching shared values automatically.
- [ ] Preserve divergent UI-specific values under each named UI.
- [ ] Flag divergent shared/Gateway values for explicit selection.
- [ ] Merge identical worlds.
- [ ] Require choose, rename, or skip for conflicting same-name worlds.
- [ ] Never display or compare secret plaintext.
- [ ] Allow offline UIs to submit later proposals without silently overriding current values.

### Apply and rollback

- [ ] Show one complete migration preview.
- [ ] Validate the proposed effective configuration before writing.
- [ ] Apply as one atomic Gateway revision.
- [ ] Back up legacy files without modifying them.
- [ ] Verify clients reconnect and apply the new revision.
- [ ] Mark legacy files migrated so they stop participating in precedence.
- [ ] Add `/config migration-status`.
- [ ] Add `/config rollback-migration`.

### Verification

- [ ] Test matching and divergent multi-TUI configurations.
- [ ] Test interrupted migration and rollback.
- [ ] Test late migration from an offline UI.
- [ ] Test conflicts against configuration changed after proposal creation.

**Phase completion:** existing installations safely converge on Gateway-managed configuration.

---

## Phase 7 — Centralized plugin management and discovery

- [ ] Add plugin catalog metadata: name, summary, category, version, settings, and client capabilities.
- [ ] Distinguish available, installed, enabled, loaded, failed, and portable states.
- [ ] Add `/plugins available|installed|enabled|failed`.
- [ ] Add `/plugins show NAME`.
- [ ] Add `/plugins enable|disable NAME` backed by Gateway configuration.
- [ ] Apply shared enablement to Gateway/PWA and all native UIs.
- [ ] Report incompatible or outdated clients by UI name.
- [ ] Announce newly available plugins once after update.
- [ ] Make unknown effects explain whether they are unavailable, uninstalled, disabled, or incompatible.

### Spotlight acceptance case

- [ ] Enabling `spotlight` once makes it available to native UIs and PWA.
- [ ] `/clear status` reports shared state and per-client exceptions.
- [ ] No duplicate `plugins.enabled` editing is required.

**Phase completion:** plugins and new capabilities are discoverable and centrally managed.

---

## Phase 8 — Contextual help

- [ ] Replace the `/help` wall with a topic index.
- [ ] Add `/help TOPIC` and `/help COMMAND`.
- [ ] Generate command help from structured descriptors.
- [ ] Include usage, examples, scope, availability, and related commands.
- [ ] Add command-specific help such as `/config help` and `/plugins help`.
- [ ] Suggest corrections and next actions for unknown commands/settings/effects.
- [ ] Show only relevant commands while explaining unavailable capabilities.

**Phase completion:** users can discover features without reading external documentation.

---

## Phase 9 — Remove legacy configuration scattering

- [ ] Move remaining ordinary settings into Gateway-managed storage.
- [ ] Reduce Gateway files to pre-start bootstrap only.
- [ ] Reduce UI files to generated connection profiles only.
- [ ] Remove legacy override precedence after a supported migration period.
- [ ] Warn clearly when unmigrated legacy files are found.
- [ ] Update setup, examples, diagnostics, and documentation.

**Phase completion:** the Gateway is the sole authority for normal TFR configuration.

---

## Per-phase working rules

- [ ] Implement only one phase or narrowly defined capability per working session.
- [ ] Update this checklist after each merged capability.
- [ ] Record important decisions or deviations below.
- [ ] Keep protocol changes backward-compatible until migration is complete.
- [ ] Add deterministic tests before removing old behavior.
- [ ] Do not release automatically; prepare and review releases separately.

## Decision log

- 2026-10-24: Gateway chosen as authority for all normal configuration, including per-UI settings.
- 2026-10-24: Stable named UI identity must precede per-UI configuration.
- 2026-10-24: Local UI state is limited to connection bootstrap and non-authoritative cache.
- 2026-10-24: Multi-client migration preserves per-UI differences and requires review for shared conflicts.
- 2026-10-24: Gateway UI identity records are stored separately from PWA authentication credentials so native and PWA clients can share one identity model without coupling identity to authorization.
- 2026-10-24: Native clients persist only their opaque UI ID in local bootstrap state and send bounded platform, version, and capability metadata during the backward-compatible hello handshake.
- 2026-10-24: PWA credential records reference a separate stable UI ID; pairing registers that ID in the shared Gateway UI store while keeping authorization data in the device store.
- 2026-10-24: `/ui list` reads the shared Gateway UI store and reports stable identity, client type, platform, last-seen time, and applied configuration revision without exposing credentials.
- 2026-10-24: `/ui show NAME` resolves unique display names case-insensitively and reports the complete bounded, non-secret UI metadata record.
- 2026-10-24: `/ui rename NAME` is bound to the stable UI ID authenticated by the current connection handshake; callers cannot supply a target ID.
- 2026-10-24: Administrative renames resolve the target display name and apply the replacement atomically; names containing spaces are passed as quoted command arguments.
- 2026-10-24: `/ui forget TARGET` requires explicit confirmation and deletes only the shared identity record; separately stored PWA credentials are not revoked.
- 2026-10-24: Registered UI names annotate web-device status, PWA diagnostics, coordinated-update messages, and command audit actor/metadata; unregistered legacy update participants retain a bounded connection suffix fallback.
- 2026-10-24: Phase 1 completed with persistent shared identities for native and PWA clients, self-service and administrative naming commands, bounded metadata validation, and explicit stale-client retention.
- 2026-10-24: Phase 2 descriptors use serializable structured validation and explicit scope/restart enums so inventory can be shared with native and PWA clients without executing arbitrary validator code.
- 2026-10-24: The initial core registry inventories existing main, UI, Gateway, world, provider, agent, plugin, bootstrap, and secret settings as exact keys or dynamic `*` templates while leaving JSONC and managed-setting persistence unchanged.
- 2026-10-24: Plugins can register namespaced typed setting descriptors with bounded summary/category/version metadata; schemas are capped by count and encoded size, and sensitive descriptors cannot publish plaintext defaults.
- 2026-10-24: Read-only effective values retain layered default, JSONC, managed-override, and runtime provenance; sensitive layers retain only source/presence and expose a redacted value.
- 2026-10-24: `/config` returns only aggregate inventory, scope, provenance, sensitivity, template, and unset-required counts; it never includes setting values.
- 2026-10-24: `/config list` exposes sorted descriptor metadata and filters by top-level category or scope; sensitive descriptors always omit their defaults.
- 2026-10-24: `/config show` resolves concrete and template-backed keys and can bind inspection to a validated stable UI name; until per-UI overrides exist, the UI context does not alter precedence.
- 2026-10-24: Detailed setting output presents the effective value (or unset/redacted marker), provenance detail, declared scope and consumers, and exact live/reload/restart requirement together.
- 2026-10-24: `/config status` compares each known UI's last reported applied revision with the current managed revision and labels it current, behind, ahead, or unreported; active synchronization remains Phase 4 work.
- 2026-10-24: Read-only summary, list, show, protocol validation, and TUI rendering enforce secret omission or an explicit `<redacted>` marker; sensitive defaults and effective plaintext are never returned.
- 2026-10-24: Structural coverage walks fully populated main, world, provider, and agent models and requires every supported leaf to resolve to an exact or wildcard registry descriptor; intentionally aggregate plugin objects remain single settings.
- 2026-10-25: Runtime inventory records derived world encoding, reconnect, scrollback, and NOSPOOF-prefix behavior, preserves JSONC provenance for channel-derived update manifests, and updates managed provenance immediately after live gag or shared-UI mutations.
- 2026-10-25: Provenance output identifies JSONC by source path and managed values by revision; removing a managed override reveals the original JSONC value and source again.
- 2026-10-25: Unknown setting errors include deterministic close descriptor matches and direct users to the matching `/config list CATEGORY` view, or the complete list when the category is unknown.
- 2026-10-25: Phase 2 completed with typed core/plugin inventory, layered provenance, redacted read-only summary/list/show/status commands, runtime consistency checks, and contextual unknown-key guidance.
- 2026-10-25: Phase 3 uses a private atomic `gateway-configuration.json` document with scoped sections, optimistic revisions, previous-revision backup, bounded audit history, schema migration, secret references, preview/confirmation, reset, history, and undo. Legacy managed overrides are imported on first use while their compatibility path remains active.
- 2026-10-26: Phase 4 clients negotiate `configuration-sync-v1`; the Gateway sends a non-secret effective view after registration and after every committed mutation, with shared, UI-type, and individual-UI precedence resolved for the named client.
- 2026-10-26: Native and PWA clients acknowledge applied, reload-required, restart-required, or unsupported revisions. Applied revisions remain persistent UI identity metadata, while bounded issue reports remain runtime status and are refreshed on reconnect.
- 2026-10-26: Native clients overlay Gateway values only in memory before constructing plugins and the TUI. Safe shared spellcheck and speaking-streak changes apply live; other live values are explicitly reported unsupported, while declared reload/restart requirements produce user-visible coordination notices.
- 2026-10-26: PWA clients apply supported shared settings live and report unknown future settings rather than ignoring them. The desired revision remains in the atomic Gateway document, so offline clients and interrupted restarts receive current state on their next registration.
- 2026-10-26: Phase 4 completed with capability-negotiated native/PWA synchronization, scoped convergence, push delivery, bounded acknowledgements, unsupported-setting visibility, and no authoritative client-side configuration copy.
