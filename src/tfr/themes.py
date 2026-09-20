from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from tfr.config import ThemeConfig


@dataclass(frozen=True, slots=True)
class ThemePalette:
    background: str
    surface: str
    overlay: str
    text: str
    muted: str
    accent: str
    secondary: str
    info: str
    success: str
    warning: str
    error: str
    selection: str
    selected_text: str


@dataclass(frozen=True, slots=True)
class ResolvedTheme:
    name: str
    palette: ThemePalette
    styles: Mapping[str, str]
    output_color: str
    terminal_ansi_roles: frozenset[str] = frozenset()

    def ansi_text(self, role: str, text: str) -> str:
        if role not in {"success", "warning", "error"}:
            raise ValueError(f"unsupported ANSI theme role: {role}")
        if role in self.terminal_ansi_roles:
            code = {"success": 32, "warning": 33, "error": 31}[role]
            return f"\x1b[{code}m{text}\x1b[0m"
        color = getattr(self.palette, role)
        red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
        return f"\x1b[38;2;{red};{green};{blue}m{text}\x1b[0m"


_DEFAULT_PALETTE = ThemePalette(
    background="#000000",
    surface="#d7d7d7",
    overlay="#5f87af",
    text="#d7d7d7",
    muted="#87afaf",
    accent="#87afff",
    secondary="#ffaf00",
    info="#5fd7ff",
    success="#00af00",
    warning="#d7af00",
    error="#af0000",
    selection="#d7d7d7",
    selected_text="#000000",
)

_PALETTES = {
    "default": _DEFAULT_PALETTE,
    "catppuccin-latte": ThemePalette(
        background="#eff1f5",
        surface="#ccd0da",
        overlay="#9ca0b0",
        text="#4c4f69",
        muted="#6c6f85",
        accent="#1e66f5",
        secondary="#fe640b",
        info="#209fb5",
        success="#40a02b",
        warning="#df8e1d",
        error="#d20f39",
        selection="#acb0be",
        selected_text="#4c4f69",
    ),
    "catppuccin-frappe": ThemePalette(
        background="#303446",
        surface="#414559",
        overlay="#737994",
        text="#c6d0f5",
        muted="#a5adce",
        accent="#8caaee",
        secondary="#ef9f76",
        info="#85c1dc",
        success="#a6d189",
        warning="#e5c890",
        error="#e78284",
        selection="#626880",
        selected_text="#c6d0f5",
    ),
    "catppuccin-macchiato": ThemePalette(
        background="#24273a",
        surface="#363a4f",
        overlay="#6e738d",
        text="#cad3f5",
        muted="#a5adcb",
        accent="#8aadf4",
        secondary="#f5a97f",
        info="#7dc4e4",
        success="#a6da95",
        warning="#eed49f",
        error="#ed8796",
        selection="#5b6078",
        selected_text="#cad3f5",
    ),
    "catppuccin-mocha": ThemePalette(
        background="#1e1e2e",
        surface="#313244",
        overlay="#6c7086",
        text="#cdd6f4",
        muted="#a6adc8",
        accent="#89b4fa",
        secondary="#fab387",
        info="#74c7ec",
        success="#a6e3a1",
        warning="#f9e2af",
        error="#f38ba8",
        selection="#585b70",
        selected_text="#cdd6f4",
    ),
    "gruvbox": ThemePalette(
        background="#282828",
        surface="#3c3836",
        overlay="#665c54",
        text="#ebdbb2",
        muted="#a89984",
        accent="#83a598",
        secondary="#fe8019",
        info="#8ec07c",
        success="#b8bb26",
        warning="#fabd2f",
        error="#fb4934",
        selection="#504945",
        selected_text="#ebdbb2",
    ),
    "tokyo-night": ThemePalette(
        background="#1a1b26",
        surface="#24283b",
        overlay="#565f89",
        text="#c0caf5",
        muted="#a9b1d6",
        accent="#7aa2f7",
        secondary="#ff9e64",
        info="#7dcfff",
        success="#9ece6a",
        warning="#e0af68",
        error="#f7768e",
        selection="#33467c",
        selected_text="#c0caf5",
    ),
    "dracula": ThemePalette(
        background="#282a36",
        surface="#44475a",
        overlay="#6272a4",
        text="#f8f8f2",
        muted="#6272a4",
        accent="#bd93f9",
        secondary="#ffb86c",
        info="#8be9fd",
        success="#50fa7b",
        warning="#f1fa8c",
        error="#ff5555",
        selection="#44475a",
        selected_text="#f8f8f2",
    ),
    "nord": ThemePalette(
        background="#2e3440",
        surface="#3b4252",
        overlay="#4c566a",
        text="#d8dee9",
        muted="#81a1c1",
        accent="#88c0d0",
        secondary="#b48ead",
        info="#81a1c1",
        success="#a3be8c",
        warning="#ebcb8b",
        error="#bf616a",
        selection="#434c5e",
        selected_text="#eceff4",
    ),
    "solarized-dark": ThemePalette(
        background="#002b36",
        surface="#073642",
        overlay="#586e75",
        text="#839496",
        muted="#657b83",
        accent="#268bd2",
        secondary="#6c71c4",
        info="#2aa198",
        success="#859900",
        warning="#b58900",
        error="#dc322f",
        selection="#073642",
        selected_text="#93a1a1",
    ),
    "nightfly": ThemePalette(
        background="#011627",
        surface="#0e293f",
        overlay="#3b4451",
        text="#acb4c2",
        muted="#7c8f8f",
        accent="#82aaff",
        secondary="#c792ea",
        info="#7fdbca",
        success="#a1cd5e",
        warning="#e3d18a",
        error="#fc514e",
        selection="#1d3b53",
        selected_text="#bdc1c6",
    ),
    "kanagawa": ThemePalette(
        background="#1f1f28",
        surface="#2a2a37",
        overlay="#54546d",
        text="#dcd7ba",
        muted="#727169",
        accent="#7e9cd8",
        secondary="#ffa066",
        info="#7fb4ca",
        success="#98bb6c",
        warning="#e6c384",
        error="#e82424",
        selection="#2d4f67",
        selected_text="#dcd7ba",
    ),
    "1976": ThemePalette(
        background="#2b231d",
        surface="#3d3229",
        overlay="#705c49",
        text="#ebdcb9",
        muted="#ad9a78",
        accent="#e5a93c",
        secondary="#d35400",
        info="#e5a93c",
        success="#556b2f",
        warning="#e5a93c",
        error="#962d2d",
        selection="#556b2f",
        selected_text="#ebdcb9",
    ),
}

_DEFAULT_STYLES = MappingProxyType(
    {
        "application": "",
        "world.active": "bold reverse",
        "world.inactive": "",
        "world.agent": "fg:#ffaf00",
        "world.unread": "bold fg:#5fd7ff",
        "input.prompt": "bold fg:#87afff",
        "input.recent": "fg:#87afaf",
        "status": "reverse",
        "status.more": "bold fg:#ffffff bg:#af0000",
        "status.lowbw": "bold fg:#000000 bg:#d7af00",
        "selection": "reverse",
        "border.output": "fg:#5f87af",
        "border.input": "fg:#87afff",
        "boss": "fg:#a8a8a8 bg:#1c1c1c",
        "boss.chart": "fg:#ffffff bg:#1c1c1c",
    }
)


def _palette_styles(palette: ThemePalette) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "application": f"fg:{palette.text} bg:{palette.background}",
            "world.active": f"bold fg:{palette.text} bg:{palette.surface}",
            "world.inactive": f"fg:{palette.muted}",
            "world.agent": f"fg:{palette.secondary}",
            "world.unread": f"bold fg:{palette.info}",
            "input.prompt": f"bold fg:{palette.accent}",
            "input.recent": f"fg:{palette.muted}",
            "status": f"fg:{palette.text} bg:{palette.surface}",
            "status.more": f"bold fg:{palette.background} bg:{palette.error}",
            "status.lowbw": f"bold fg:{palette.background} bg:{palette.warning}",
            "selection": f"fg:{palette.selected_text} bg:{palette.selection}",
            "border.output": f"fg:{palette.overlay}",
            "border.input": f"fg:{palette.accent}",
            "boss": f"fg:{palette.muted} bg:{palette.background}",
            "boss.chart": f"fg:{palette.text} bg:{palette.background}",
        }
    )


def _replace_style_color(style: str, target: str, color: str) -> str:
    prefix = f"{target}:"
    tokens = [token for token in style.split() if not token.startswith(prefix)]
    tokens.append(f"{target}:{color}")
    return " ".join(tokens)


def _legacy_styles(palette: ThemePalette, overridden: set[str]) -> Mapping[str, str]:
    styles = dict(_DEFAULT_STYLES)
    if "background" in overridden:
        for name in ("application", "boss", "boss.chart"):
            styles[name] = _replace_style_color(styles[name], "bg", palette.background)
        for name in ("status.more", "status.lowbw"):
            styles[name] = _replace_style_color(styles[name], "fg", palette.background)
    if "surface" in overridden:
        styles["world.active"] = f"bold fg:{palette.text} bg:{palette.surface}"
        styles["status"] = f"fg:{palette.text} bg:{palette.surface}"
    if "overlay" in overridden:
        styles["border.output"] = f"fg:{palette.overlay}"
    if "text" in overridden:
        styles["application"] = _replace_style_color(styles["application"], "fg", palette.text)
        styles["boss.chart"] = _replace_style_color(styles["boss.chart"], "fg", palette.text)
    if "muted" in overridden:
        styles["world.inactive"] = f"fg:{palette.muted}"
        styles["input.recent"] = f"fg:{palette.muted}"
        styles["boss"] = _replace_style_color(styles["boss"], "fg", palette.muted)
    if "accent" in overridden:
        styles["input.prompt"] = f"bold fg:{palette.accent}"
        styles["border.input"] = f"fg:{palette.accent}"
    if "secondary" in overridden:
        styles["world.agent"] = f"fg:{palette.secondary}"
    if "info" in overridden:
        styles["world.unread"] = f"bold fg:{palette.info}"
    if "warning" in overridden:
        styles["status.lowbw"] = _replace_style_color(
            styles["status.lowbw"], "bg", palette.warning
        )
    if "error" in overridden:
        styles["status.more"] = _replace_style_color(styles["status.more"], "bg", palette.error)
    if {"selection", "selected_text"} & overridden:
        styles["selection"] = f"fg:{palette.selected_text} bg:{palette.selection}"
    return MappingProxyType(styles)


def resolve_theme(
    config: ThemeConfig | None = None,
    *,
    output_color: str | None = None,
) -> ResolvedTheme:
    selected = config or ThemeConfig()
    overrides = selected.colors.model_dump(exclude_none=True)
    palette = replace(_PALETTES[selected.preset], **overrides)
    if selected.preset == "default":
        overridden = set(overrides)
        styles = _legacy_styles(palette, overridden) if overridden else _DEFAULT_STYLES
        terminal_ansi_roles = frozenset({"success", "warning", "error"} - overridden)
    else:
        styles = _palette_styles(palette)
        terminal_ansi_roles = frozenset()
    return ResolvedTheme(
        name=selected.preset,
        palette=palette,
        styles=styles,
        output_color=output_color or palette.text,
        terminal_ansi_roles=terminal_ansi_roles,
    )
