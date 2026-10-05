# TFR Architecture

TFR is organized around one authoritative event domain with multiple presentation clients.

## Dependency direction

```text
configuration / events / presentation models
                 ↓
       Gateway and plugin policy
                 ↓
     bounded protocol projections
          ↙                 ↘
 terminal capability      web capability
      renderer               renderer
```

- The Gateway owns connections, credentials, canonical events, history, and authorization.
- TUI and web code may render semantic effects but must not redefine canonical event meaning.
- Presentation models, combo state, and demo descriptors must not import TUI or web modules.
- Browser payloads are bounded, versioned projections and never contain passwords or provider keys.
- Client-only previews never enter command buses, event logs, histories, agents, or world sockets.

## Adding a cross-client effect

1. Add or reuse a semantic effect model in `presentation.py`, `text_effects.py`, or a focused
   feature module.
2. Add capability-specific terminal and web renderers.
3. Register a bounded `EffectDemo` so Effects Lab exercises the same compiled program.
4. Add shared conformance vectors plus TUI and WebKit integration coverage.
5. Update the protocol schema only when the semantic payload changes, not for renderer details.

Application shells should coordinate features rather than implement them. New substantial TUI
features belong in focused Python modules; new browser features belong in focused `.mjs` modules.
Large behavior-preserving extractions should be incremental instead of mixed with protocol changes.

## Language choices

Python remains the primary domain, Gateway, plugin, and terminal language. Browser rendering uses
JavaScript modules; a gradual TypeScript migration is appropriate as those modules gain additional
protocol and state contracts. Rewriting the Gateway or TUI in another systems language is not a
current architectural requirement.
