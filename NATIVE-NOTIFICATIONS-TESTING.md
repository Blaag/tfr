# Native Notification Testing

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

Repeat the direct matrix inside tmux. Diagnostics must report `multiplexer=tmux`.

## Herdr

Configure Herdr terminal-owned notifications:

```toml
[ui.toast]
delivery = "terminal"
```

Run TFR in a Herdr pane and repeat the focus matrix. Diagnostics must report
`multiplexer=herdr` and `resolved=herdr`. Switching away from the TFR pane must
produce a focus-out diagnostic before any notification is eligible.

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
