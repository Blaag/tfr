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
    details: tuple[str, ...] = ()

    def render(self, *, unavailable: str | None = None) -> str:
        lines = [f"/{self.name} — {self.summary}", f"Usage: {self.usage}"]
        if unavailable:
            lines.append(f"Availability: unavailable — {unavailable}")
        else:
            lines.append(f"Availability: {self.scope}")
        if self.examples:
            lines.extend(("Examples:", *(f"  {item}" for item in self.examples)))
        if self.details:
            lines.extend(("Options and behavior:", *(f"  {item}" for item in self.details)))
        if self.related:
            lines.append("More help: " + ", ".join(f"/help {name}" for name in self.related))
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
    details: tuple[str, ...] = (),
) -> CommandHelp:
    return CommandHelp(name, summary, usage, examples, topic, scope, availability, related, details)


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
            "/world [ALIAS]",
            "worlds",
            "this UI",
            examples=("/world alpha",),
            related=("next", "previous"),
            details=("Without ALIAS, list available worlds.",),
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
            "Show retained lines or search the Gateway recall database",
            "/recall COUNT | status | [all] [s:SPEAKER] [w:WORDS|r:REGEXP] [OPTIONS]",
            "worlds",
            "active world",
            examples=(
                "/recall 50",
                '/recall s:Alice w:"red * dragon" c:10',
                '/recall all w:"red dragon"',
                "/recall w:butts l:5 c:5",
            ),
            related=("end",),
            details=(
                "Search defaults to the active world; all searches every enabled world.",
                "l:/limit: limits matches; c:/context: adds events before and after each match.",
                "ct:/context-time: adds time-bounded context; cs:matched limits context speakers.",
                "s:/speaker:, w:/words:, r:/regexp:, in:/world:, k:/kind: filter matches.",
                "after:, before:, on:, and since: bound time; words and regexp are exclusive.",
            ),
        ),
        _command(
            "releases",
            "Inspect retained managed releases on the Gateway",
            "/releases [status|list]",
            "gateway",
            "Gateway host",
            related=("diagnostics", "update", "version"),
        ),
        _command(
            "stats",
            "Explore line, speaker, word, world, and time activity from recall",
            "/stats [all] [summary|speakers|worlds|words|activity|hours|weekdays|heatmap] "
            "[since:7d] [speaker:NAME] [top:10]",
            "worlds",
            "active world or all recall-enabled worlds",
            examples=(
                "/stats speakers since:30d",
                "/stats all words by:world top:10",
                "/stats speaker:Wedge heatmap since:90d",
            ),
            related=("recall", "diagnostics"),
            details=(
                "Defaults to the active world and last 7 days; all searches all enabled worlds.",
                "Time: since:, on:, after:, before:. Scope: in:/world:, speaker:.",
                "Limits: top:, groups:, min:. Words: by:speaker|world, stop:on|off.",
                "Activity: bucket:hour|day|month. Display: graph:auto|bar|line|heatmap|none.",
            ),
        ),
        _command("end", "Return to live output", "/end", "worlds", "this UI", related=("recall",)),
        _command(
            "gateway",
            "Manage Gateway connections and profiles",
            "/gateway restart|reconnect|setup|profiles [export [PATH --include-token]]|"
            "switch NAME|rename NAME NEW|"
            "repair NAME|forget NAME --confirm",
            "connections",
            "native Gateway UI",
            examples=("/gateway profiles", "/gateway repair home"),
            availability="gateway",
            related=("config", "ui"),
            details=(
                "Switching or repairing the active profile reloads this UI.",
                "profiles export is credential-free; PATH --include-token writes a private",
                "recovery bundle for setup on a replacement or additional UI.",
            ),
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
            "Open settings or manage Gateway configuration",
            "/config [summary|status|list|show|set|reset|changes|undo|migrate|"
            "migration-status|rollback-migration|export|import|import-status|"
            "rollback-import|ui-profiles|claim-ui-profile|remove-ui-profile]",
            "configuration",
            "Gateway-managed configuration",
            examples=(
                "/config",
                "/config show ui.theme.preset",
                "/config export ~/tfr-backup.json",
                "/config ui-profiles",
            ),
            availability="gateway",
            related=("plugins", "ui"),
            details=(
                "list [FILTER]; show KEY [--ui NAME]; changes [LIMIT]; undo [confirm].",
                "set KEY VALUE|reset KEY support --scope, --ui, --target, and --confirm.",
                "migrate supports preview, choose, skip, rename, and apply.",
                "export PATH [--include-secrets]; import PATH [confirm].",
                "Import/migration rollback and UI-profile operations require confirmation.",
            ),
        ),
        _command(
            "settings",
            "Open the interactive settings browser",
            "/settings",
            "configuration",
            "Gateway-managed configuration",
            availability="gateway",
            related=("config",),
        ),
        _command(
            "plugins",
            "Discover and centrally manage plugins",
            "/plugins [available|installed|enabled|failed|show NAME|enable NAME [confirm]|"
            "disable NAME [confirm]]",
            "plugins",
            "Gateway plugin catalog",
            examples=("/plugins show spotlight", "/plugins enable spotlight confirm"),
            availability="gateway",
            related=("config", "clear"),
            details=("Without an action, list enabled plugins.",),
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
        _command(
            "effects",
            "Open and control the Effects Lab",
            "/effects [open|close|list|combos|speakers|all|effect NAME|effect all|force on|off]",
            "appearance",
            "this UI",
            details=(
                "/teststreak [all|3..7] previews combo tiers.",
                "/testspeaker [NAME] lists or previews configured speaker effects.",
            ),
        ),
        _command(
            "animations",
            "Control continuous animations",
            "/animations [on|off|status]",
            "appearance",
            "this UI",
            details=("Without an option, toggle continuous animations.",),
        ),
        _command(
            "lowbw",
            "Control low-bandwidth rendering",
            "/lowbw [on|off|status]",
            "appearance",
            "this UI",
            details=("Without an option, toggle low-bandwidth rendering.",),
        ),
        _command(
            "mouse",
            "Choose local mouse handling",
            "/mouse [auto|terminal|tfr|status]",
            "appearance",
            "this UI",
            details=("Without an option, show current and effective mouse modes.",),
        ),
        _command(
            "nospoof",
            "Control NOSPOOF prefix visibility",
            "/nospoof [show|hide|status]",
            "appearance",
            "shared UI",
            details=("Without an option, show current prefix visibility.",),
        ),
        _command(
            "spellcheck",
            "Manage shared submit-time spell checking",
            "/spellcheck [on|off|status|undo]",
            "configuration",
            "shared UI",
            availability="gateway",
            details=("Without an option, show status; undo restores the last corrected draft.",),
        ),
        _command(
            "streaks",
            "Manage shared speaking streak effects",
            "/streaks [on|off|status]",
            "appearance",
            "shared UI",
            availability="gateway",
            details=("Without an option, show current Gateway-managed status.",),
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
            '/ui list|pair|show NAME|rename "NAME"|rename "TARGET" "NAME"|'
            'forget "TARGET" [confirm]',
            "administration",
            "Gateway UI inventory",
            availability="gateway",
            related=("config", "gateway"),
        ),
        _command(
            "web",
            "Manage private PWA access",
            '/web [status]|repair|pair "DEVICE" [--gateway HOST:PORT]|devices|revoke DEVICE_ID',
            "administration",
            "Gateway PWA service",
            availability="gateway",
        ),
        _command(
            "diagnostics",
            "Inspect or manually export local, bounded runtime diagnostics",
            "/diagnostics [CATEGORY|report|clear|copy [CATEGORY]|export PATH]",
            "administration",
            "Gateway and UI",
            availability="gateway",
            details=(
                "Categories: gateway, config, plugins, presentations, notifications, web,",
                "updates, agents, connector, recall, releases.",
                "copy and export include the complete bounded report; display shows the newest 25.",
            ),
        ),
        _command(
            "update",
            "Check or coordinate managed updates",
            "/update [status|check|force]",
            "administration",
            "Gateway and connected UIs",
            availability="gateway",
            details=(
                "No option coordinates an update; check refreshes availability; status uses cache.",
                "force is available only on the candidate channel.",
            ),
        ),
        _command(
            "agent",
            "Inspect and control an agent",
            "/agent status|inspect [NAME]|pause [NAME]|resume [NAME]|trigger [NAME]|close",
            "agents",
            "configured agent world",
            availability="agents",
            details=("NAME defaults to the agent assigned to the active world.",),
        ),
        _command(
            "image",
            "Preview and send an image",
            "/image [--width 1-80] [--ascii|--unicode] [--withcolor] [PATH]",
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


def command_usage(name: str) -> str:
    return f"Usage: {COMMAND_HELP[name].usage}"


def suggest_help(value: str, names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(difflib.get_close_matches(value.casefold(), names, n=3, cutoff=0.5))
