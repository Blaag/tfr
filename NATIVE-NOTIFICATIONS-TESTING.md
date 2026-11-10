# Native Notification Testing

## Expected behavior

Notifications are individual-UI settings under `/config` → **Notifications**.
They are disabled by default. TFR sends nothing until the terminal reports that
the TFR surface is unfocused, then sends at most one notification until focus
returns. Activity in another world while TFR remains focused never produces a
desktop notification. **Private** preview says only `TFR has activity`;
**Full** identifies the world and activity class but never includes chat text or
sender names.

Triggers can be enabled independently for all activity, selected worlds,
mentions of that world's configured character, and pages. The menu provides a
transport test and an atomic **Reset to defaults** action.

## Recommended macOS setup

Use Ghostty and enable it under **System Settings → Notifications → Ghostty**.
TFR's `auto` transport chooses BEL-terminated OSC 9 for detected
Ghostty/iTerm2/Kitty/WezTerm/Warp sessions. The terminal—not TFR—posts the
macOS notification. A successful TFR handoff confirms that the request was
written or accepted; only visual acceptance testing confirms that macOS
displayed it.

Apple Terminal and Alacritty receive BEL in automatic mode. Depending on the
terminal profile this may sound, flash, mark a tab/Dock icon, or do nothing. It
is not expected to create a Notification Center banner. TFR deliberately has no
`osascript` or direct macOS-notification fallback.

Example owner CLI configuration for a named UI:

```bash
tfr config set \
  ui.notifications.enabled=true \
  ui.notifications.transport=auto \
  --ui "Office Mac · Terminal" --apply

tfr config set ui.notifications.worlds='["alien","mush"]' \
  --ui "Office Mac · Terminal" --apply

tfr config set ui.notifications.enabled=false \
  --ui "Office Mac · Terminal" --apply
```

Automated coverage verifies focus parsing, one-notification-per-unfocused-period,
policy matching, privacy, OSC 9 encoding, tmux wrapping, Herdr invocation,
configuration reset, diagnostics, and singular-page speaker effects.

## Optional tools

- Ghostty or iTerm2 for macOS acceptance testing.
- `brew install tmux` for the tmux matrix.
- Herdr 0.9.5 or newer for the Herdr matrix.

## Ghostty direct

1. Allow Ghostty notifications in macOS System Settings.
2. In TFR `/config`, enable notifications and use **Send test notification**.
3. Focus another tab or application and generate matching world activity.
4. Confirm one notification appears, further activity is silent, and focusing then
   unfocusing TFR permits one new notification.
5. Confirm `/diagnostics notifications` reports Ghostty, OSC 9, and focus changes.

## tmux

Configure tmux:

```tmux
set -g focus-events on
set -g allow-passthrough on
```

A new tmux server loads `~/.tmux.conf` automatically. Run `tmux source-file
~/.tmux.conf` only after editing the file while a server is already running.
Repeat the direct matrix inside tmux. Diagnostics must report `multiplexer=tmux`.
When tmux sets `TERM_PROGRAM=tmux`, inherited `GHOSTTY_SURFACE_ID`,
`GHOSTTY_RESOURCES_DIR`, or `GHOSTTY_BIN_DIR` markers identify the outer terminal;
diagnostics should report `terminal=ghostty` and `resolved=osc-9`. Without an
outer-terminal marker, automatic mode safely falls back to BEL and the transport
must be set to **OSC 9** explicitly after live passthrough testing.

For Ghostty → mosh/SSH → tmux → TFR, both OSC 9 output and focus reports must
survive every layer. If remote environment variables do not identify Ghostty,
set this UI's transport to **OSC 9** explicitly. Mosh support varies by version,
so verify with **Send test notification**, then verify real focus gating by
switching local Ghostty tabs/apps and checking `/diagnostics notifications`.

## Herdr

On macOS with Herdr 0.9.5, configure system-owned notifications:

```toml
[ui.toast]
delivery = "system"
```

Run TFR in a Herdr pane and repeat the focus matrix. Diagnostics must report
`multiplexer=herdr` and `resolved=herdr`. Herdr owns pane and outer-terminal
focus and forwards focus-in/focus-out events to pane applications that enable
terminal focus reporting. TFR uses those events as the notification authority;
it must not assume every Herdr pane is unfocused or rely on `herdr notification
show` to suppress a request from the focused pane. Herdr's JSON response tells
TFR only whether it accepted or suppressed the notification request. Herdr
0.9.5's `terminal` backend emits ST-terminated OSC 9, which affected
Ghostty/macOS combinations can accept without displaying; `system` bypasses
that path. A future Herdr release that emits BEL-terminated OSC 9 may use
`terminal` after live acceptance testing. Raw OSC 9 or OSC 777 written inside a
Herdr pane is consumed by Herdr and is not an end-to-end test; use TFR's menu
test or `herdr notification show`.

TFR sends Herdr a stable `TFR` title and the notification text as a nonempty
body. This is required for Herdr's macOS `system` backend: `terminal-notifier`
rejects an empty `-message`, which would otherwise make Herdr fall back to an
AppleScript notification owned by Script Editor.

## Privacy

With preview set to **Private**, the exact notification text must be `TFR has
activity`. With **Full**, it may identify the world and activity class, but never
sender names or message content. Redacted, outbound, and internal events must not
notify.

Singular pages plus Alien (`Wedge pages (black Blood): message`) and MUSH
(`To (Black, wedge), Blood pages: message`) multipages are covered. Unknown MU*
page formats use deliberately permissive bounded speaker attribution so configured
speaker effects are more likely to apply; additional representative formats should
be added as fixtures when encountered.
