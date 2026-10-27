from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandHelp:
    name: str
    summary: str
    usage: str
    examples: tuple[str, ...]
    topic: str
    scope: str
    availability: str = "always"
    related: tuple[str, ...] = ()

    def render(self, *, unavailable: str | None = None) -> str:
        lines = [f"/{self.name} — {self.summary}", f"Usage: {self.usage}"]
        if unavailable:
            lines.append(f"Availability: unavailable — {unavailable}")
        else:
            lines.append(f"Availability: {self.scope}")
        if self.examples:
            lines.extend(("Examples:", *(f"  {item}" for item in self.examples)))
        if self.related:
            lines.append("Related: " + ", ".join(f"/{name}" for name in self.related))
        return "\n".join(lines)


def _command(
    name: str,
    summary: str,
    usage: str,
    topic: str,
    scope: str,
    *,
    examples: tuple[str, ...] = (),
    availability: str = "always",
    related: tuple[str, ...] = (),
) -> CommandHelp:
    return CommandHelp(name, summary, usage, examples, topic, scope, availability, related)


COMMAND_HELP = {
    item.name: item
    for item in (
        _command(
            "help",
            "Browse command and topic help",
            "/help [TOPIC|COMMAND]",
            "basics",
            "this UI",
            examples=("/help configuration", "/help config"),
        ),
        _command(
            "world",
            "Switch to a named world",
            "/world ALIAS",
            "worlds",
            "this UI",
            examples=("/world alpha",),
            related=("next", "previous"),
        ),
        _command(
            "next",
            "Switch to the next world",
            "/next",
            "worlds",
            "this UI",
            related=("world", "previous"),
        ),
        _command(
            "previous",
            "Switch to the previous world",
            "/previous",
            "worlds",
            "this UI",
            related=("world", "next"),
        ),
        _command("connect", "Connect the active world", "/connect", "worlds", "active world"),
        _command(
            "disconnect", "Disconnect the active world", "/disconnect", "worlds", "active world"
        ),
        _command("reconnect", "Reconnect the active world", "/reconnect", "worlds", "active world"),
        _command(
            "recall",
            "Show retained world lines",
            "/recall COUNT",
            "worlds",
            "active world",
            examples=("/recall 50",),
            related=("end",),
        ),
        _command("end", "Return to live output", "/end", "worlds", "this UI", related=("recall",)),
        _command(
            "gateway",
            "Manage Gateway connections and profiles",
            "/gateway reconnect|setup|profiles|switch|rename|repair|forget",
            "connections",
            "native Gateway UI",
            examples=("/gateway profiles", "/gateway repair home"),
            availability="gateway",
            related=("config", "ui"),
        ),
        _command(
            "reload",
            "Reload this UI",
            "/reload",
            "connections",
            "managed native UI",
            availability="restart",
        ),
        _command(
            "restart",
            "Restart this UI",
            "/restart",
            "connections",
            "managed native UI",
            availability="restart",
        ),
        _command(
            "config",
            "Inspect, change, and migrate Gateway configuration",
            "/config status|list|show|set|reset|changes|undo|migrate|"
            "migration-status|rollback-migration",
            "configuration",
            "Gateway-managed configuration",
            examples=("/config show ui.theme.preset", "/config status"),
            availability="gateway",
            related=("plugins", "ui"),
        ),
        _command(
            "plugins",
            "Discover and centrally manage plugins",
            "/plugins available|installed|enabled|failed|show|enable|disable",
            "plugins",
            "Gateway plugin catalog",
            examples=("/plugins show spotlight", "/plugins enable spotlight confirm"),
            availability="gateway",
            related=("config", "clear"),
        ),
        _command(
            "clear",
            "Clear output or select a screen-clear effect",
            "/clear [status|cycle|random|lock EFFECT]",
            "appearance",
            "this UI",
            examples=("/clear status", "/clear lock spotlight"),
            related=("plugins",),
        ),
        _command("effects", "Open the Effects Lab", "/effects", "appearance", "this UI"),
        _command(
            "animations",
            "Control continuous animations",
            "/animations on|off|status",
            "appearance",
            "this UI",
        ),
        _command(
            "lowbw",
            "Control low-bandwidth rendering",
            "/lowbw on|off|status",
            "appearance",
            "this UI",
        ),
        _command(
            "mouse",
            "Choose local mouse handling",
            "/mouse auto|terminal|tfr|status",
            "appearance",
            "this UI",
        ),
        _command(
            "nospoof",
            "Control NOSPOOF prefix visibility",
            "/nospoof show|hide|status",
            "appearance",
            "shared UI",
        ),
        _command(
            "spellcheck",
            "Manage shared submit-time spell checking",
            "/spellcheck on|off|status|undo",
            "configuration",
            "shared UI",
            availability="gateway",
        ),
        _command(
            "streaks",
            "Manage shared speaking streak effects",
            "/streaks on|off|status",
            "appearance",
            "shared UI",
            availability="gateway",
        ),
        _command(
            "gag",
            "Manage shared world gag patterns",
            "/gag list|add PATTERN|remove PATTERN|reset",
            "configuration",
            "active Gateway world",
            availability="gateway",
        ),
        _command(
            "ui",
            "Inspect and administer named UIs",
            "/ui list|show|rename|forget",
            "administration",
            "Gateway UI inventory",
            availability="gateway",
            related=("config", "gateway"),
        ),
        _command(
            "web",
            "Manage private PWA access",
            "/web status|repair|pair|devices|revoke",
            "administration",
            "Gateway PWA service",
            availability="gateway",
        ),
        _command(
            "diagnostics",
            "Inspect bounded runtime diagnostics",
            "/diagnostics [web|presentations|plugins|clear]",
            "administration",
            "Gateway and UI",
            availability="gateway",
        ),
        _command(
            "update",
            "Check or coordinate managed updates",
            "/update [status|check|force]",
            "administration",
            "Gateway and connected UIs",
            availability="gateway",
        ),
        _command(
            "agent",
            "Inspect and control an agent",
            "/agent status|inspect|pause|resume|trigger|close",
            "agents",
            "configured agent world",
            availability="agents",
        ),
        _command(
            "image",
            "Preview and send an image",
            "/image [--width N] [--ascii|--unicode] [--withcolor] [PATH]",
            "local",
            "native UI",
            examples=("/image --width 40 image.png",),
        ),
        _command("sh", "Temporarily open a local shell", "/sh", "local", "native UI"),
        _command("version", "Show UI and Gateway versions", "/version", "basics", "this UI"),
        _command("quit", "Exit TFR", "/quit", "basics", "this UI"),
    )
}

TOPIC_SUMMARIES = {
    "basics": "Essential navigation and session information",
    "connections": "Gateway profiles, reconnects, and UI reloads",
    "worlds": "World selection, connectivity, and retained output",
    "configuration": "Gateway-owned settings and guided migration",
    "plugins": "Plugin discovery, compatibility, and shared enablement",
    "appearance": "Rendering, effects, motion, and display controls",
    "administration": "UI, PWA, diagnostics, and update administration",
    "agents": "Agent inspection and control",
    "local": "Native UI facilities on this computer",
}


def suggest_help(value: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(difflib.get_close_matches(value.casefold(), names, n=3, cutoff=0.5))
