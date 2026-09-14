# Boss Views

Boss views are trusted UI plugins that replace TFR with a plausible full-screen
cover while connections, buffering, and event logging continue. TFR includes
the `build-dashboard` view and supports additional registered views. Press
Enter to dismiss the active view.

Boss mode is concealment, not an isolation boundary. The built-in dashboard
deliberately shows sanitized world aliases and operational counts disguised as
log-monitor output, but never displays world text, commands, connection error
messages, credentials, or hostnames.

## Selecting A View

`/boss` opens the selected view. The remaining forms change selection for the
running UI:

```text
/boss status
/boss cycle
/boss random
/boss lock SCREEN
```

The initial mode is configured under `ui.boss`:

```jsonc
"ui": {
  "boss": {
    "mode": "locked",
    "screen": "build-dashboard",
  },
}
```

`screen` is required only in `locked` mode. Cycle order follows plugin
registration order.

## Registering A View

Plugins import supported contracts only from `tfr.plugin_api`:

```python
from tfr.plugin_api import BossViewContext


class OperationsBoss:
    api_version = 1

    def register(self, registrar, config):
        self.handle = registrar.register_boss_view(
            "operations",
            self.render,
            refresh_interval_seconds=5,
        )

    def render(self, context: BossViewContext):
        lines = [
            f"Operations snapshot {context.elapsed_seconds:.0f}s",
            f"Gateway: {context.gateway_connected}",
        ]
        for world in context.worlds:
            lines.append(f"{world.world}: {world.received_since_activation} new events")
        return (("class:boss", "\n".join(lines)),)


plugin = OperationsBoss()
```

The view automatically joins cycle and random selection. A handle may activate
its own view directly with `await handle.activate(world)`. `handle.emit(event)`
publishes only while that handle's view is active. A plugin that is not the
view owner can publish into whichever view is active with
`registrar.emit_boss_event(event)`.

`refresh_interval_seconds` is optional and must be from 1 through 60 seconds.
The refresh task exists only while that view is active and is independent of
normal text and border animations.

## Render Context

Every render receives an immutable `BossViewContext`:

| Field | Meaning |
| --- | --- |
| `screen` | Registered name of the active boss view |
| `world` | World that was active when boss mode opened |
| `width`, `height` | Current terminal dimensions |
| `activated_at` | Time this boss activation began |
| `elapsed_seconds` | Monotonic elapsed time for phase changes |
| `seed` | Stable value for deterministic generated content |
| `gateway_connected` | `True`, `False`, or unknown (`None`) |
| `worlds` | Exact per-world operational snapshots |
| `events` | At most 200 transient operational or plugin events |

Each `BossWorldStatus` contains `world`, `connection_state`,
`received_since_activation`, `last_activity_at`, and `idle_seconds`.
`received_since_activation` counts inbound TFR frames. It resets on every boss
activation. The last-activity timestamp includes activity observed before boss
mode opened.

Renderers receive snapshots; they do not pop or acknowledge events. A stateful
renderer can retain the greatest `BossViewEvent.sequence` it has processed.
Sequences restart at zero for each activation.

## Operational Events

TFR automatically mirrors plugin-visible lifecycle events into the active boss
event ring:

| Kind | Important fields |
| --- | --- |
| `application_start` | Application lifecycle notification |
| `application_stop` | Application lifecycle notification |
| `gateway_connected` | `state="connected"`, `source="gateway"` |
| `gateway_disconnected` | `state="disconnected"`, `source="gateway"`, safe `intentional` metadata |
| `session_state` | `world`, detailed connection `state`, safe connection metadata |
| `world_connected` | `world`, `state`, `source="world"` |
| `world_disconnected` | `world`, `state`, `source="world"` |
| `world_activity` | `world`, latest event kind in `state`, coalesced activity metadata |
| `boss_activated` | `world`, selected screen in the boss context |

High-volume `world_activity` callbacks are coalesced per world. Its metadata
contains `count`, `connection_generation`, `first_timestamp`, and
`last_timestamp`. Use `context.worlds` rather than summing these events when an
exact since-activation total is required. No inbound message text is included.

Plugins can also emit presentation-safe custom events:

```python
from tfr.plugin_api import BossViewEvent

registrar.emit_boss_event(
    BossViewEvent(
        kind="index_complete",
        text="Search index rebuilt",
        metadata={"documents": 42},
    )
)
```

Plugin identity is assigned by TFR rather than trusted from `source`, using the
reserved `plugin:ENTRY_POINT` namespace so it cannot be confused with `ui`,
`world`, or `gateway` system sources. Events emitted while no boss view is
active are discarded. Metadata is recursively
frozen and limited to 200 scalar/container nodes with at most four levels of
nesting; use compact presentation data rather than payloads. Strings are
limited to 500 characters.

## Built-In Dashboard

The built-in view shows real per-world status as disguised log-watcher output,
then alternates between a fake vertical bar chart and generated sequence
diagram. The chart uses `plotext` with wide, filled white block bars on a canvas
capped at 80 columns, distinct axis labels selected from the histogram label
catalog, and generated numeric axis legends. A flow selects three through seven
component names and uses RetroFlow to draw rounded boxes, routed arrows, and a
cycle from `Profit` back to an earlier component. A short deterministic fake `ls -la`
listing follows the visualization.

Configure its catalogs under `plugins.config["tfr.boss"]`:

```jsonc
"plugins": {
  "config": {
    "tfr.boss": {
      "log_path_template": "/var/log/{world}",
      "histogram": {
        "seconds": 20,
        "include_defaults": true,
        "labels": ["Custom metric"],
      },
      "flow": {
        "seconds": 15,
        "include_defaults": true,
        "labels": ["Custom stage"],
      },
      "files": {
        "include_defaults": true,
        "names": ["custom-artifact.dat"],
      },
    },
  },
}
```

When `include_defaults` is `true`, configured values supplement the defaults.
When false, they replace them. Duplicate values are removed case-insensitively
while preserving order. A replacement histogram catalog needs at least two
axis labels, and a replacement flow catalog needs at least three component
names. The flow's terminal `Profit` node is fixed and is not part of the
configurable catalog. Flow labels cannot contain `->` or begin with `#`, which
RetroFlow reserves for edges and comments. They must use single-column ASCII
characters and cannot duplicate the fixed `Profit` node.

## Safety Limits

Boss events are single-line printable text limited to 500 characters. Rendered
output is limited to 2,000 fragments and 100,000 characters. Event fields
reject C0, DEL, C1, Unicode formatting, and surrogate controls. Rendered text
permits only newline and horizontal tab layout controls. Fragment styles may be
empty or contain only Prompt Toolkit `class:NAME` references. A renderer that
violates these limits is disabled and replaced by a static recovery cover
rather than sending unsafe output or revealing buffered world content.
