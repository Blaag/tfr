# Portable Presentation Architecture

## Status

This document records the architecture and migration plan for plugin-driven
presentation across TFR clients. The first portable vertical slice is
implemented alongside the existing `TextDecoration` API. It provides typed
effect programs, terminal and PWA interpreters, a bounded browser projection,
explicit reduced-motion and unsupported-capability fallbacks, a
`color_pulse()` constructor, and a bounded `character_sweep()` primitive.

The model is intentionally still version 1 and narrow. Additional primitives
must preserve its validation, fallback, accessibility, and resource-boundary
rules rather than treating the current wire shape as a general animation API.

## Decision

TFR will evolve toward a bounded, declarative presentation DSL represented by
typed data. Python plugins remain the programmable policy layer. A plugin decides
when presentation applies, which visible text it targets, and which parameters
and fallbacks to use; it emits an `EffectProgram` rather than drawing directly.
TFR-owned renderers interpret that program using their own platform primitives.

```text
                         Python plugin or TFR core
                                    |
                                    | EffectProgram
                                    v
                         validated presentation model
                              /                 \
                             v                   v
                    terminal interpreter   browser projection
                                                   |
                                                   v
                                            PWA interpreter
```

In a terminal UI process, plugin policy and interpretation can occur locally. A
PWA cannot execute a trusted local Python package, so the Gateway executes the
portable plugin policy, validates and serializes its bounded result, and sends
that result with the approved browser event projection. The Gateway does not
draw the effect. The PWA remains responsible for interpretation, accessibility,
layout, animation, cleanup, and local motion preferences.

The DSL is the typed presentation model and its composition rules. It does not
need to be a textual language, and it must not contain arbitrary expressions,
loops, recursion, Python, JavaScript, CSS, terminal escapes, or DOM operations.
Typed Python constructors should be the normal plugin-authoring interface; JSON
is only the validated browser transport representation.

## Why This Model

A protocol containing named effects such as `color_pulse`, `jiggle`, or
`speaker_pop` would require every renderer to implement every name. It would
also duplicate each new effect in Python and JavaScript. A smaller set of
composable primitives lets plugins create new effects without client changes as
long as those effects use abilities the clients already implement.

The PWA must not be restricted to the terminal's least-common denominator. A
browser can scale, rotate, translate, fade, bounce, or visually duplicate text
in ways a character-cell terminal cannot. Portable programs therefore contain
capability requirements and explicit alternatives. A capable graphical client
may select a transform-rich variant while the terminal selects a color or
emphasis variant. A static or no-op fallback remains valid when neither animated
variant is available.

## Responsibility Boundaries

### Plugins

Plugins remain responsible for:

- Matching events, speakers, ranges, or semantic roles.
- Reading plugin configuration and maintaining policy state.
- Choosing effect parameters and ordered presentation variants.
- Supplying reduced-motion and unsupported-capability fallbacks.
- Emitting bounded, typed effect programs.

Plugins do not directly manipulate Prompt Toolkit fragments, browser DOM, CSS,
animation objects, screen-reader state, or rendering timers through the portable
contract.

The plugin system is not being retired. Enrichers, commands, key bindings,
lifecycle handlers, boss views, display transforms, border effects, screen-clear
effects, and UI-specific extensions remain separate capabilities. A plugin may
offer both portable presentation and client-specific functionality.

### Renderers

Each renderer remains responsible for:

- Advertising or internally selecting only capabilities it implements.
- Choosing the first supported variant according to defined selection rules.
- Giving each supported primitive the same intended meaning.
- Mapping primitives to platform mechanisms.
- Preserving readable canonical text, selection, copying, and accessibility.
- Applying local reduced-motion and animation preferences.
- Enforcing runtime bounds and deterministic cleanup.
- Ignoring an unsupported or invalid program without losing the underlying text.

Implementations differ by platform. For example, foreground interpolation may
use scheduled Prompt Toolkit redraws in the TUI and CSS or Web Animations in the
PWA. The semantic primitive remains foreground interpolation in both.

Renderers must not silently reinterpret an unsupported operation. A terminal
cannot decide that `rotate` means underline. The plugin supplies underline,
color, static emphasis, or no effect as an explicit alternative.

### Gateway

For browser presentation, the Gateway:

- Executes only plugin policy registered for portable presentation.
- Validates output before it crosses the browser trust boundary.
- Projects only approved primitives and fields.
- Applies event, program, variant, track, keyframe, range, and size limits.
- Sends no executable plugin code and no arbitrary style language.

The Gateway does not choose a variant based on a browser's motion setting and
does not render presentation. Local client policy can change while a page is
open, and accessibility preferences belong to the client.

## Effect Program Shape

The implemented version 1 effect program contains:

- A version.
- One grapheme-aligned character-range target in the approved visible text.
- Ordered presentation variants.
- Capability requirements for each variant.
- Static foreground, bold, and underline style operations.
- A two- or three-keyframe foreground timeline with bounded duration, repeat
  interval, repeat count, and sampling rate.
- A two- or three-position per-grapheme sweep with bounded target length and
  trail width, optional uppercase head emphasis, and Oklab color interpolation.
- An explicit reduced-motion presentation.
- A final static or no-op fallback.

The following remains an illustrative future shape for a browser-rich speaker
variant and terminal-compatible alternative:

```json
{
  "version": 1,
  "target": {"role": "speaker"},
  "variants": [
    {
      "requires": ["timeline", "transform.translate_y", "transform.rotate"],
      "tracks": [
        {
          "property": "translate_y",
          "keyframes": [
            {"at": 0, "value": 0},
            {"at": 0.5, "value": -8},
            {"at": 1, "value": 0}
          ]
        },
        {
          "property": "rotate",
          "keyframes": [
            {"at": 0, "value": -3},
            {"at": 0.5, "value": 3},
            {"at": 1, "value": -3}
          ]
        }
      ]
    },
    {
      "requires": ["timeline", "foreground_color"],
      "tracks": [
        {
          "property": "foreground_color",
          "keyframes": [
            {"at": 0, "value": "#a9914a"},
            {"at": 0.5, "value": "#fff08a"},
            {"at": 1, "value": "#a9914a"}
          ]
        }
      ]
    }
  ],
  "reduced_motion": {
    "foreground_color": "#fff08a",
    "bold": true
  },
  "fallback": {
    "bold": true
  }
}
```

This transform example is not accepted by the current version 1 schema.

### Current Plugin API

Plugins register portable policy in every process scope with
`register_presentation_decorator()`. A decorator must be a pure function of the
immutable event and exact visible text because the Gateway or an attached UI may
first evaluate retained events during replay. TFR caches results by event and
visible projection within each process, but plugins must not depend on invocation
count, client count, or evaluation order.

```python
from tfr.plugin_api import PresentationStyle, color_pulse


class SpeakerPulse:
    api_version = 1

    def register(self, registrar, config):
        def decorate(event, text):
            if event.spoof is None or event.spoof.speaker.casefold() != "alice":
                return ()
            start, end = event.spoof.speaker_span
            return (
                color_pulse(
                    start,
                    end,
                    base_color="#a9914a",
                    accent_color="#fff08a",
                    duration_seconds=1.2,
                    repeat_seconds=6,
                    repeat_count=20,
                    reduced_motion=PresentationStyle(
                        foreground="#fff08a",
                        bold=True,
                    ),
                ),
            )

        registrar.register_presentation_decorator("speaker-pulse", decorate)


plugin = SpeakerPulse()
```

The Gateway executes this policy against the final sanitized and possibly
truncated browser-visible text. The TUI executes it against terminal-visible
plain text after display transforms. A decorator must therefore validate that
its selected range belongs to the supplied `text`, rather than assuming offsets
from canonical ANSI-bearing input remain valid.

## Capabilities And Fallbacks

Capabilities describe independently implementable operations, not client names.
Examples may include:

- `foreground_color`, `background_color`, `bold`, and `underline`.
- `timeline`, easing, finite repetition, and character stagger.
- `opacity`.
- `transform.scale`, `transform.rotate`, and `transform.translate`.
- A bounded, visual-only overlay or duplicate.

A program must not branch on `pwa` or `tui`. It supplies ordered variants with
requirements, and each renderer chooses the first variant whose complete
requirements it supports. Unknown capabilities make a variant unsupported, not
partially active. If no variant is supported, the renderer uses the explicit
fallback or leaves the original text unchanged.

Capability selection is versioned, bounded, and fails closed. Version 1 defines
the same static capability profile in both shipped interpreters:
`foreground_color`, `bold`, `underline`, and `timeline`. Dynamic advertisement
should be added only when multiple client versions or optional rendering
backends require it.

Named helpers such as `color_pulse()`, `bounce()`, or `jiggle()` may remain in
the public Python API. They are constructors that compile to primitives, not
wire-level operations that every renderer recognizes by name.

## Initial Primitive Set

The implemented first vertical slice supports:

- A grapheme-aligned character-range target.
- Static foreground color, bold, and underline.
- Foreground-color keyframes.
- A foreground-color timeline with two or three keyframes and Oklab
  interpolation.
- Event-time phase, 1–3 second active duration, 1–60 second repeat interval,
  1–20 repetitions, at most 20 sampled frames per second, and at most five
  minutes total lifetime.
- An explicit reduced-motion/static fallback.
- Deterministic behavior from event time rather than receipt-time drift where
  retained and live events must agree.

Version 1 accepts one grapheme-aligned target of at most 2,048 Unicode code
points per event. Character sweeps target at most 64 graphemes and use a trail
width of 1–8. It rejects overlapping or multiple programs. The PWA starts at
most 64 presentation animation-budget units in a rendered transcript; character
sweeps consume four units and simple timelines consume one. Older targets fall
back to the program's static style. Invalid or oversized presentation is omitted
without dropping readable event text.

After this works in both clients, a graphical extension can add opacity, scale,
rotation, and bounded translation. Those primitives are sufficient to compose
many larger-text, spin, jiggle, and bounce effects without adding named effect
implementations to the PWA.

Per-character substitution, reveal effects, overlays, appended indicators,
screen transitions, border animation, and particle systems should be added only
after their targeting, layout, accessibility, and resource semantics are clear.
They should not be forced into the first text-effect schema merely because the
current TUI supports similarly named behavior.

## Composition And Targeting

Programs operate on the approved visible-text projection, never canonical ANSI
offsets or raw server bytes. Range boundaries must align with the same text used
by static browser runs and terminal display projection. Semantic targets such as
`speaker` are preferable when TFR can validate them from trusted parsed data;
otherwise a plugin emits a bounded visible-text range.

Before multiple programs can overlap, the model must define deterministic
composition rules. At minimum:

- Text content must remain reconstructable and stable unless a future primitive
  explicitly declares visual-only substitution.
- Static ANSI-derived styles and effect properties need defined precedence.
- Two tracks writing the same property over the same range must have a stable
  winner or be rejected.
- Unsupported properties must not invalidate unrelated text.
- Program order must not depend on dictionary ordering or asynchronous arrival.

The first implementation rejects overlapping or multiple portable programs.
Supporting fewer combinations with clear semantics is preferable to accidental
layering.

## Motion And Accessibility

Motion policy is local to each client. In the PWA, `System` follows
`prefers-reduced-motion`, `Reduced` selects the reduced-motion presentation, and
`Full` permits the normal supported variant. The TUI similarly respects its
animation and low-bandwidth settings.

Movement, flashing, or continuous animation requires a bounded static or
reduced-motion presentation. The renderer may impose a safer fallback even when
the plugin requests motion.

Browser-rich effects must preserve the canonical transcript:

- The readable transcript text remains stable and selectable.
- Copying returns the original visible text.
- Screen readers encounter the content once.
- Visual duplicates are hidden from accessibility APIs.
- Effects do not capture input or obscure application controls.
- Translation, scale, rotation, blur, layers, duration, frame work, and
  repetitions are bounded.
- Off-screen or overlay effects expire and clean up deterministically.

A visual copy may bounce around a bounded transcript region while the canonical
line remains in place. Moving the canonical node around the viewport would harm
reading position, selection, layout, and accessibility and should not be the
default interpretation.

## Security And Resource Bounds

Plugins are trusted Python code in the process where they run, but projected
programs still cross into a less-trusted browser rendering boundary. The PWA
accepts only the versioned allowlist of data primitives. Programs cannot contain:

- CSS declarations, selectors, class names, or custom properties chosen by a
  plugin.
- HTML, SVG, DOM operations, scripts, URLs, or event handlers.
- Terminal control sequences.
- Filesystem, clipboard, camera, shell, socket, or network operations.
- Unbounded loops, recursion, expressions, timelines, or generated elements.

Validation must bound at least programs per event, variants per program, tracks
and keyframes, target length, colors and numeric ranges, duration, delay,
repetition, stagger, displacement, rotation, scale, overlays, and serialized
size. Invalid presentation fails open to readable ordinary text and can disable
the responsible registration without dropping canonical events.

## Commands And Client Actions

Presentation does not include client commands. A program that changes color or
moves a visual layer must not also switch worlds, submit commands, open dialogs,
read a clipboard, or reconnect a session.

Configured world-switch aliases are core client behavior and can be implemented
locally from the world descriptors already sent by the Gateway. Portable
plugin-defined commands may later use a separate, typed client-action model with
operations such as `select_world`, `focus_composer`, or `show_notice`. That model
would use similar capability and validation principles but has a different
authority boundary and requires a separate design decision.

Arbitrary custom browser components or interactions still require PWA-side code.
The presentation DSL provides broad composition inside its vocabulary; it is not
a general browser plugin runtime.

## Migration Plan

Migration remains incremental. The first four steps are complete:

1. [x] Define immutable Python types, strict validation, and a versioned browser
   DTO for the initial primitive set.
2. [x] Add an explicitly portable presentation registration available in
   Gateway and UI scopes.
3. [x] Implement TUI and PWA interpreters with semantic tests for timing, color,
   targeting, variant selection, and fallback behavior.
4. [x] Add a bounded `color_pulse()` helper that compiles to primitives.
5. Add graphical transform primitives and prove a PWA-rich effect with an
   explicit terminal fallback.
6. Migrate other suitable named `TextEffectKind` recipes as their required
   primitives become available.
7. Deprecate the old hard-coded rendering contract only after external plugins
   have a documented migration path and released compatibility window.

During migration, `TextDecoration` and `EffectProgram` coexist. Existing
TUI plugins must continue to work until an intentional plugin API transition.
Compatibility adapters should be added only where their semantics are exact;
client-specific effects can remain client-specific rather than receiving a
misleading portable translation.

## Testing Requirements

Every primitive and helper should have deterministic semantic fixtures that can
be consumed by Python and JavaScript tests. Validation should cover malformed,
unknown, oversized, overlapping, non-finite, and out-of-range programs.

Each complete effect path should test:

- Plugin policy and target selection.
- Gateway projection and exclusion of arbitrary plugin data.
- Browser schema rejection and readable-text fallback.
- Equivalent timing checkpoints in TUI and PWA interpreters where both advertise
  the same capability.
- Capability fallback selection.
- Reduced-motion and disabled-animation behavior.
- Retained-history and live-event timing.
- Selection, copying, screen-reader exposure, cleanup, and bounded DOM growth.
- Packaged PWA assets and protocol compatibility.

Pixel-identical output across platforms is not a goal. Equivalent primitive
semantics, explicit fallback selection, readable underlying text, and bounded
behavior are required.
