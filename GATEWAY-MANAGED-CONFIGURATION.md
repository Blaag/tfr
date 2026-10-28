# Gateway-Managed Configuration Plan

> This repository copy is the authoritative implementation tracker.

## Goal

Make the Gateway authoritative for all TFR configuration. Each named UI receives a scoped configuration view. Local UI state contains only what is required to connect to a Gateway.

## Current status — 2026-10-28

- Phases 1–9 are implemented: the Gateway is authoritative, clients synchronize scoped
  configuration, connection setup and migration run inside TFR, plugins are centrally
  managed, contextual help is available, and ordinary legacy precedence is removed.
- The current corrective branch fixes issues discovered while testing the Phase 9 candidate:
  per-UI values are no longer placed in UI-type scope, world defaults no longer create a
  bogus world named `defaults`, and legacy world login passwords are moved atomically into
  private mode-0600 files referenced by managed configuration.
- The current branch also improves transcript readability for help and plugin catalog
  reports by showing the originating command, separating responses, removing the generic
  `-- ... --` wrapper, grouping plugin states, and omitting meaningless unknown versions.
- `/config set` is a complete low-level mutation interface for automation, diagnostics, and
  advanced use. It is not considered an adequate primary interface for people configuring
  nested values such as speaker-effect rules.
- Phase 10 is complete on the current branch: `/config` and `/settings` open the native
  keyboard-driven settings interface, bounded declarative metadata drives trusted controls,
  and structured edits commit through an atomic revision-checked batch API.

## Decisions

- [x] Gateway owns shared, Gateway, world, plugin, and per-UI settings.
- [x] Every UI has a stable ID and a configurable display name.
- [x] Native and PWA clients use the same configuration API.
- [x] No routine JSONC editing after migration.
- [x] Local UI storage is limited to Gateway connection profiles and cached, non-authoritative data.
- [x] Existing clients receive a guided, reviewable migration.
- [x] Diverging shared settings require conflict resolution; per-UI settings remain separate.
- [x] Text commands remain available as a stable configuration API for automation and
  advanced use.
- [x] Ordinary users configure TFR through a menu-driven settings interface rather than
  constructing JSON values or selecting scopes and targets manually.

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

- [x] Detect absence of a connection profile.
- [x] Present an interactive Gateway setup flow.
- [x] Collect Gateway address and required TLS identity.
- [x] Pair or authenticate without requiring JSONC editing.
- [x] Propose and allow editing the UI display name.
- [x] Store credentials in the OS keychain where available.
- [x] Store only Gateway endpoint, credential reference, and stable UI ID locally.

### Profile management

- [x] Add `/gateway setup`.
- [x] Add `/gateway profiles`.
- [x] Add profile switch, repair, rename, and forget flows.
- [x] Ensure exported profiles omit credentials.

### Verification

- [x] A fresh installation can connect without editing files.
- [x] Invalid TLS/authentication details give actionable recovery steps.
- [x] Losing local state does not expose Gateway secrets.

**Phase completion:** users can install and connect a UI entirely through TFR.

---

## Phase 6 — Guided migration

### Proposal collection

- [x] Detect legacy JSONC and managed overrides.
- [x] Add `/config migrate` and first-run migration prompt.
- [x] Have each UI submit a migration proposal tagged with its stable ID.
- [x] Classify every value as shared, Gateway, UI-specific, world, bootstrap, or secret.
- [x] Preserve source file and provenance for review.

### Reconciliation

- [x] Deduplicate matching shared values automatically.
- [x] Preserve divergent UI-specific values under each named UI.
- [x] Flag divergent shared/Gateway values for explicit selection.
- [x] Merge identical worlds.
- [x] Require choose, rename, or skip for conflicting same-name worlds.
- [x] Never display or compare secret plaintext.
- [x] Allow offline UIs to submit later proposals without silently overriding current values.

### Apply and rollback

- [x] Show one complete migration preview.
- [x] Validate the proposed effective configuration before writing.
- [x] Apply as one atomic Gateway revision.
- [x] Back up legacy files without modifying them.
- [x] Verify clients reconnect and apply the new revision.
- [x] Mark legacy files migrated so they stop participating in precedence.
- [x] Add `/config migration-status`.
- [x] Add `/config rollback-migration`.
- [x] Keep individual-UI values in individual-UI scope during Gateway legacy import.
- [x] Apply `worlds.defaults.*` keys to world defaults rather than a world named `defaults`.
- [x] Move legacy world login passwords into private mode-0600 files and commit only
  `file:` references alongside login character names in the atomic migration revision.

### Verification

- [x] Test matching and divergent multi-TUI configurations.
- [x] Test interrupted migration and rollback.
- [x] Test late migration from an offline UI.
- [x] Test conflicts against configuration changed after proposal creation.
- [x] Test mixed UI/UI-type import, world-default application, and private world-login
  reference creation.

**Phase completion:** existing installations safely converge on Gateway-managed configuration.

---

## Phase 7 — Centralized plugin management and discovery

- [x] Add plugin catalog metadata: name, summary, category, version, settings, and client capabilities.
- [x] Distinguish available, installed, enabled, loaded, failed, and portable states.
- [x] Add `/plugins available|installed|enabled|failed`.
- [x] Add `/plugins show NAME`.
- [x] Add `/plugins enable|disable NAME` backed by Gateway configuration.
- [x] Apply shared enablement to Gateway/PWA and all native UIs.
- [x] Report incompatible or outdated clients by UI name.
- [x] Announce newly available plugins once after update.
- [x] Make unknown effects explain whether they are unavailable, uninstalled, disabled, or incompatible.

### Spotlight acceptance case

- [x] Enabling `spotlight` once makes it available to native UIs and PWA.
- [x] `/clear status` reports shared state and per-client exceptions.
- [x] No duplicate `plugins.enabled` editing is required.

**Phase completion:** plugins and new capabilities are discoverable and centrally managed.

---

## Phase 8 — Contextual help

- [x] Replace the `/help` wall with a topic index.
- [x] Add `/help TOPIC` and `/help COMMAND`.
- [x] Generate command help from structured descriptors.
- [x] Include usage, examples, scope, availability, and related commands.
- [x] Add command-specific help such as `/config help` and `/plugins help`.
- [x] Suggest corrections and next actions for unknown commands/settings/effects.
- [x] Show only relevant commands while explaining unavailable capabilities.
- [x] Show the originating help request and visually separate consecutive help responses.
- [x] Remove the generic `-- ... --` notice wrapper from multi-line help output.
- [x] Make topic and related-command drill-down explicit with `/help COMMAND` examples.
- [x] Render plugin catalog reports as labeled command results grouped by loaded, disabled,
  and failed state; omit meaningless `unknown` versions.

**Phase completion:** users can discover features without reading external documentation.

---

## Phase 9 — Remove legacy configuration scattering

- [x] Move remaining ordinary settings into Gateway-managed storage.
- [x] Reduce Gateway files to pre-start bootstrap only.
- [x] Reduce UI files to generated connection profiles only.
- [x] Remove legacy override precedence after a supported migration period.
- [x] Warn clearly when unmigrated legacy files are found.
- [x] Update setup, examples, diagnostics, and documentation.

**Phase completion:** the Gateway is the sole authority for normal TFR configuration.

---

## Phase 10 — Interactive settings interface

### Product decisions

- [x] Make `/config` with no arguments open the settings interface; retain the existing
  subcommands, and consider `/settings` as a discoverable alias.
- [x] Treat `/config show|set|reset|changes|undo` as the low-level API, not the expected
  workflow for routine human configuration.
- [x] Default scope and target from setting metadata and current context. Hide them during
  normal editing and expose them only through an advanced view.
- [x] Use one keyboard-complete interaction model that does not require a mouse.
- [x] Start with the native TUI and use the same bounded declarative form model for a later
  PWA implementation.
- [x] Never load arbitrary plugin-provided UI code. Plugins publish bounded declarative
  field and form metadata that clients render using trusted controls.

### Settings browser

- [x] Add a full-screen category browser for General, Appearance, Worlds, Plugins, Gateway,
  Updates, individual UIs, and other registry-backed categories.
- [x] Add search by setting name, label, description, category, scope, and plugin.
- [x] Show the effective value, provenance, target, validation summary, and required action
  without exposing secret plaintext.
- [x] Generate controls for booleans, choices, strings, numbers, colors, paths, lists, and
  nullable values from configuration descriptors.
- [x] Offer contextual world, UI, UI-type, and plugin selectors only when the setting needs
  one and no unambiguous context is available.
- [x] Provide review, cancel, reset-to-inherited, and undo paths from the same interface.

### Declarative structured forms

- [x] Extend plugin setting metadata with bounded labels, descriptions, groups, field types,
  defaults, constraints, conditional visibility, and collection-item schemas.
- [x] Add reusable add/edit/delete/reorder controls for structured lists and objects.
- [x] Add palette-based color selection with validated custom hexadecimal input.
- [x] Add multi-select controls for worlds, event kinds, capabilities, and other registry
  enumerations.
- [x] Permit trusted previews through bounded existing facilities such as Effects Lab rather
  than plugin-supplied executable UI code.

### Atomic editing

- [x] Add a revision-checked batch mutation API so a form can stage and validate related
  values as one complete effective configuration before committing one audited revision.
- [x] Show a change review containing old and new non-secret values, affected targets, and
  required live/reload/restart action.
- [x] Reject stale form submissions with a clear refresh-and-review flow rather than losing
  concurrent changes.
- [x] Apply safe changes live and reuse existing synchronization, convergence, reload, and
  restart coordination after commit.

### Speaker-effects acceptance case

- [x] Add `Plugins → Speaker effects` with enablement status and a readable rule list.
- [x] Add an editor for speaker name, effect, base/accent colors, effect-specific parameters,
  worlds, and event kinds.
- [x] Show only fields relevant to the selected effect; for `comet`, include duration and
  trail width without requiring JSON knowledge.
- [x] Add preview through Effects Lab before saving.
- [x] Allow a user to add Barnaby with comet and `#ffdd76`, preserve all existing rules, and
  apply the complete rule collection without knowing the registry key, JSON shape, scope, or
  plugin target.

### Verification

- [x] Test keyboard-only navigation, cancellation, validation errors, and narrow terminals.
- [x] Test generated controls for every supported descriptor type and scope.
- [x] Test structured collection editing without dropping unchanged entries.
- [x] Test atomic multi-field validation, revision conflicts, audit output, and undo.
- [x] Test that sensitive values and references are not rendered in forms, reviews, logs, or
  synchronized client views.

**Phase completion:** complete. Routine configuration is discoverable and safely editable by
a person without JSON, registry-key, scope, or target knowledge.

---

## Per-phase working rules

- [x] Implement and close one complete phase per working session unless explicitly scoped
  otherwise by the maintainer.
- [x] Update this checklist when the phase is complete rather than treating partial
  capabilities as phase completion.
- [x] Record important decisions or deviations below.
- [x] Keep protocol changes backward-compatible until migration is complete.
- [x] Add deterministic tests before removing old behavior.
- [x] Do not release automatically; prepare and review releases separately.

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
- 2026-10-26: Phase 5 native bootstrap uses a private versioned connection-profile document containing endpoint/TLS identity, an opaque credential reference, stable UI ID, and the proposed display name; it does not require a main JSONC file on a fresh remote UI.
- 2026-10-26: Gateway tokens use the macOS Keychain for the default profile location when available, with a private mode-0600 credential file fallback for unsupported platforms and isolated/test profile stores. Profile exports omit both credential references and token values.
- 2026-10-26: `/gateway setup`, `/gateway profiles [export]`, `/gateway switch`, `/gateway rename`, `/gateway repair`, and confirmed `/gateway forget` provide profile lifecycle management. Active-profile changes restart the UI so bootstrap state is re-read rather than mutating an established authenticated transport.
- 2026-10-26: Phase 5 completed with first-run interactive setup, editable stable UI naming, TLS/authentication bootstrap, private credential storage, profile recovery and switching, and credential-free exports.
- 2026-10-26: Phase 6 migration proposals are tagged with stable UI identity and their starting Gateway revision, retain bounded source provenance, classify registry-backed values by managed scope, and replace sensitive plaintext with presence-only metadata before submission.
- 2026-10-26: Reconciliation automatically deduplicates identical shared/world/plugin values, preserves UI-specific differences under stable IDs, and requires explicit choose-or-skip decisions for divergent shared, Gateway, world, or plugin values. Stale late proposals cannot overwrite newer configuration.
- 2026-10-26: Applying a migration validates the complete proposed Gateway document and commits it as one audited atomic revision. Source files are copied without modification, migrated files receive a separate marker, connected clients receive the new desired revision, and the pre-migration document supports revision-checked rollback.
- 2026-10-26: `/config migrate`, migration preview/choose/skip/apply flows, `/config migration-status`, and `/config rollback-migration` expose the complete guided workflow from TFR.
- 2026-10-26: Phase 6 completed with deterministic multi-UI reconciliation, protected secret handling, source backups, stale-proposal rejection, atomic apply, convergence reporting, and rollback.
- 2026-10-26: Phase 7 catalogs discovered and requested plugins with bounded name, summary, category, version, settings, capabilities, availability, installation, enablement, load failure, and portability state. Native clients refresh stable UI capabilities after plugin loading.
- 2026-10-26: `/plugins available|installed|enabled|failed`, `/plugins show NAME`, and confirmed `/plugins enable|disable NAME` use the Gateway catalog and the shared `plugins.enabled` managed setting. Configuration synchronization distributes the desired list to Gateway, PWA, and every native UI for coordinated reload.
- 2026-10-26: Plugin details identify incompatible/outdated clients by stable display name; periodic source notices remain de-duplicated by source/version, and unknown screen-clear effects explain unavailable, disabled, failed, or incompatible state with a discovery next action.
- 2026-10-26: The spotlight acceptance case uses one shared enablement, marks the effect portable for PWA projection, and extends `/clear status` with shared state and named client exceptions.
- 2026-10-26: Phase 7 completed with centralized discovery, Gateway-backed enablement, compatibility reporting, portable spotlight convergence, and contextual unavailable-effect guidance.
- 2026-10-27: Phase 8 replaces the static help wall with structured command descriptors containing summary, usage, examples, topic, scope, availability requirements, and related commands. Loaded plugin commands are folded into the same help model at runtime.
- 2026-10-27: `/help` lists only topics with currently available commands; `/help TOPIC` filters commands by context, while `/help COMMAND`, `/config help`, and `/plugins help` provide detailed command guidance and explicitly explain unavailable capabilities.
- 2026-10-27: Unknown commands and help queries use deterministic close matches and next actions. Unknown settings retain registry suggestions and category links, while unknown effects now report plugin availability/enablement/failure/compatibility and point to plugin details.
- 2026-10-27: Phase 8 completed with contextual topic/command discovery, structured generated help, dynamic plugin command help, availability filtering, and actionable correction guidance.
- 2026-10-27: Phase 9 Gateway startup reads only an optional versioned bootstrap containing the managed state-directory location. It constructs ordinary defaults and overlays the Gateway document before creating connectors, sessions, plugins, agents, logging, updates, or web services.
- 2026-10-27: Legacy JSONC and the former managed-overrides store no longer participate in Gateway runtime precedence. An empty Gateway document receives one atomic ordinary-value import with migration backups; later legacy files are detection/proposal inputs only and produce explicit ignored/unmigrated warnings. Bootstrap values and secrets other than migrated world logins remain separate setup concerns.
- 2026-10-27: Remote native UIs use generated connection profiles and the synchronized effective view. They no longer persist mouse preferences in a separate local authority; temporary `/mouse` changes direct users to an individual-UI `/config set` for persistence.
- 2026-10-27: Setup, Doctor, examples, schemas, and README now distinguish standalone legacy input from Gateway bootstrap and managed configuration. Phase 9 completed with the Gateway as sole authority for normal split/remote TFR configuration.
- 2026-10-28: Candidate testing exposed three Phase 9 defects: individual-UI legacy values were incorrectly mapped to UI-type scope, `worlds.defaults.*` was applied as a world named `defaults`, and world login credentials were omitted. Corrective work separates UI scopes, applies world defaults to the correct model location, and atomically stages legacy world passwords in private files referenced by the managed document.
- 2026-10-28: Help and plugin catalog output use dedicated multi-line transcript blocks with the originating request or command, whitespace separation, no generic dash wrapper, explicit help drill-down, grouped plugin state, and omission of unknown version placeholders.
- 2026-10-28: The existing configuration commands are designated the stable low-level API rather than the final human interface. Routine configuration will use a menu-driven settings browser with inferred scope/target, trusted schema-generated controls, staged review, and atomic batch commit.
- 2026-10-28: Structured plugin configuration uses bounded declarative form metadata rather than arbitrary plugin UI code. The first acceptance case is a speaker-effects rule editor that can add, edit, reorder, preview, and remove rules without exposing their JSON representation.
- 2026-10-28: Phase 10 makes bare `/config` and `/settings` open the native keyboard-driven settings browser; `/config summary` retains the former aggregate text report, and all existing explicit subcommands remain the stable automation and advanced-user API.
- 2026-10-28: Settings are grouped and searchable from registry and form metadata. Scalar, choice, nullable, color, string-list, world-template, current-UI, UI-type, named-UI, plugin, and reusable structured-collection controls infer normal targets while retaining an explicit advanced target path.
- 2026-10-28: Setting descriptors may carry at most 32 KiB of JSON-only declarative form metadata. Clients render that metadata with trusted controls; no plugin-provided executable UI code crosses the configuration protocol.
- 2026-10-28: `config_batch` validates up to 64 related changes against one expected revision, validates the complete proposed document, writes and audits one revision atomically, synchronizes clients once, and supports atomic undo of every change in that revision.
- 2026-10-28: The speaker-effects acceptance editor reports plugin state, preserves and reorders existing rules, provides effect-aware comet fields, palette/custom colors, world and event-kind selection, and an unsaved Effects Lab preview. Adding Barnaby with comet and `#ffdd76` requires no JSON, registry key, scope, or target knowledge.
- 2026-10-28: Phase 10 completed with deterministic registry, store, protocol, client, TUI, structured-form, sensitive-omission, batch-conflict, audit, undo, and Barnaby/comet coverage. Future PWA settings rendering can consume the same bounded form metadata without changing the authority or mutation model.
- 2026-10-28: Settings dialogs suspend the main Prompt Toolkit renderer while their own asynchronous application is active, preventing interleaved screen artifacts. Arrow keys move, Enter selects, Tab changes buttons, and Escape consistently cancels the current dialog.
- 2026-10-28: The authenticated native TLS token is an owner credential and grants the same configuration administration available on the local Unix socket. Restricted PWA device credentials remain on their separate limited protocol and cannot use owner configuration mutations.
