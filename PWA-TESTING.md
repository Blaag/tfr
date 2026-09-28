# PWA Development and Testing

This guide reproduces the isolated setup used to test a TFR branch through a
real development Gateway and deterministic mock world. It deliberately contains
no production world, account, device, certificate, or login details.

## Testing Policy

Use Playwright for every PWA test that does not require physical hardware. Use
WebKit with a mobile viewport by default because Safari on iPhone is the primary
target. Browser automation should cover pairing, the live WebSocket protocol,
command submission, transcript projection, portable presentation effects,
reduced motion, hostile text handling, mobile layout, keyboard-sized viewports,
reconnects, and bounded transcript behavior.

Keep the Python and Node unit suites as fast protocol and logic coverage; they
do not replace Playwright for browser behavior. Manual desktop browser testing
is exploratory only and is not release evidence when Playwright can make the
same assertion. CI and the protected release build install Playwright WebKit and
run the service-independent suite in `tests/e2e/pwa-ui.mjs`.

Reserve physical-device testing for behavior an emulator cannot establish:

- Safari Add to Home Screen installation and standalone launch.
- The real iOS keyboard, safe-area insets, rotation, selection, and copy behavior.
- App suspension during lock/backgrounding and recovery after network changes.
- VoiceOver, Dynamic Type, Reduce Motion integration, and touch ergonomics.
- Visual confirmation of animation quality on the actual display.

Record physical results separately without committing pairing links, cookies,
Tailscale identities, real world names, account names, or transcripts.

## Committed Harness

- `scripts/dev-pwa` manages the isolated mock world, branch Gateway, Tailscale
  Serve process, pairing, device cleanup, and live Playwright test.
- `scripts/mock-world` implements deterministic commands without a real account.
- `tests/fixtures/pwa-dev/` contains sanitized Gateway and world configuration.
- `tests/e2e/pwa-live.mjs` drives a fresh mobile WebKit context through the real
  HTTPS Gateway and revokes its temporary device when it finishes.
- `tests/e2e/pwa-ui.mjs` and `pwa-expiration.mjs` test mobile layout,
  keyboard-sized viewports, bounded transcript rendering, command input,
  pairing, and revoked-session behavior without external services.
- `.tfr/pwa-dev-harness/` is generated runtime state and remains ignored. It may contain
  device credentials and must never be committed.

The mock world understands these commands:

| Command | Purpose |
| --- | --- |
| `pulse` | Emit an attributed Alice `SAY` event for portable speaker effects. |
| `stress [COUNT]` | Emit deterministic scrollback, capped at 5,000 lines. |
| `long` | Emit one long wrapping line. |
| `links` | Emit safe HTTP and HTTPS link candidates. |
| `unicode` | Emit composed, combining, CJK, symbol, and emoji text. |
| `ansi` | Emit ANSI styles, an OSC 8 attempt, a clear-screen control, and HTML-like text. |

## Prerequisites

1. Check out the branch under test and run `uv sync --frozen`.
2. Install the Playwright package and WebKit browser:

   ```sh
   npm --prefix tests/e2e ci
   npm --prefix tests/e2e exec -- playwright install webkit
   ```

3. Install and connect Tailscale on the Gateway host.
4. Choose the host's exact private MagicDNS HTTPS origin. Do not enable Funnel.
5. Ensure no unrelated Tailscale Serve configuration is active. The harness
   refuses to replace one.

The harness imports TFR from the current checkout and refuses to run if Python
resolves another installation. This is what makes the test exercise the branch
rather than the last installed stable release.

Run the service-independent browser checks first:

```sh
npm --prefix tests/e2e test
```

## Run the Stack

Set the origin in every terminal used for this harness:

```sh
export TFR_DEV_ORIGIN=https://gateway-host.example.ts.net
```

Validate the generated configuration:

```sh
./scripts/dev-pwa check
```

Start the mock world, branch Gateway, and private HTTPS Serve endpoint in one
terminal:

```sh
./scripts/dev-pwa start
```

The Gateway listens on loopback port `7438`, the mock world listens on loopback
port `7421`, and all mutable state is isolated below `.tfr/pwa-dev-harness/`. `start`
runs in the foreground so Ctrl-C tears down its children and Tailscale Serve.

For Gateway-only debugging without browser access or Tailscale Serve, use:

```sh
./scripts/dev-pwa local
```

## Test a Plugin Branch

By default the fixture installs the latest compatible stable public-plugin
release. To test an unpublished `tfr-plugins-public` branch, push its reviewed
commit and pin that exact full commit for the development run:

```sh
export TFR_DEV_PLUGIN_REF=0123456789abcdef0123456789abcdef01234567
./scripts/dev-pwa start
```

Do not use a mutable branch name. The generated runtime configuration records
only the commit pin and remains ignored.

## Run Playwright

With the stack running, use a second terminal with the same
`TFR_DEV_ORIGIN` and optional `TFR_DEV_PLUGIN_REF`:

```sh
./scripts/dev-pwa playwright
```

The command creates a one-time device, passes the pairing URL only through the
child process environment, launches a fresh Playwright WebKit mobile context,
and tests the live stack. It verifies:

- Pairing and a live WebSocket connection through Tailscale Serve.
- Command delivery through the Gateway to the mock world.
- Speaker attribution and the serialized portable character-sweep program.
- Cylon traversal while preserving canonical `Alice` text.
- Static bold fallback after selecting reduced motion.
- Linkification, Unicode preservation, ANSI projection, and inert HTML-like text.
- A bounded transcript and live connection during a 5,000-event burst.
- Absence of browser console and page errors.

The test calls `/api/logout` in cleanup, which revokes its temporary device.
If the process is killed before cleanup, list and revoke it manually:

```sh
./scripts/dev-pwa devices
./scripts/dev-pwa revoke DEVICE_UUID
```

Never put a pairing URL on a command line, in a checked-in file, or in a test
report. Pairing links are short-lived bearer secrets.

## Physical Pass

After Playwright passes, pair the physical test device with
`./scripts/dev-pwa pair`. On the phone, verify installation/standalone launch,
keyboard and rotation behavior, selection/copy, background/foreground recovery,
accessibility settings, and animation quality. Revoke disposable devices after
the pass. A deliberately retained development phone may remain paired only to
this isolated state directory.

No real world is needed for this pass. If a maintainer separately tests a real
world, keep its configuration outside the repository and do not adapt this
fixture to include those credentials or identifiers.

## Teardown and Recovery

Use Ctrl-C in the foreground stack terminal. If it was launched by a supervisor
or the controlling terminal disappeared, run:

```sh
./scripts/dev-pwa stop
```

Before restarting, confirm ports `7421` and `7438` are free and inspect
`tailscale serve status`. The harness refuses an occupied port, active Serve
configuration, or stale Gateway socket instead of replacing unrelated state.

To revoke every development browser and reset only this harness, stop it and
remove `.tfr/pwa-dev-harness/`. Never remove an operator's normal TFR state
directory.
