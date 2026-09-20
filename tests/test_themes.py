from __future__ import annotations

import pytest

from tfr.config import ThemeColorsConfig, ThemeConfig
from tfr.themes import resolve_theme


@pytest.mark.parametrize(
    ("preset", "background", "text"),
    [
        ("catppuccin-latte", "#eff1f5", "#4c4f69"),
        ("catppuccin-frappe", "#303446", "#c6d0f5"),
        ("catppuccin-macchiato", "#24273a", "#cad3f5"),
        ("catppuccin-mocha", "#1e1e2e", "#cdd6f4"),
        ("gruvbox", "#282828", "#ebdbb2"),
        ("tokyo-night", "#1a1b26", "#c0caf5"),
        ("dracula", "#282a36", "#f8f8f2"),
        ("nord", "#2e3440", "#d8dee9"),
        ("solarized-dark", "#002b36", "#839496"),
        ("nightfly", "#011627", "#acb4c2"),
        ("kanagawa", "#1f1f28", "#dcd7ba"),
        ("1976", "#2b231d", "#ebdcb9"),
    ],
)
def test_resolves_named_presets(preset: str, background: str, text: str) -> None:
    theme = resolve_theme(ThemeConfig(preset=preset))  # type: ignore[arg-type]

    assert theme.palette.background == background
    assert theme.output_color == text
    assert theme.styles["application"] == f"fg:{text} bg:{background}"
    assert "reverse" not in theme.styles["status"]


def test_default_theme_preserves_existing_styles_and_ansi_colors() -> None:
    theme = resolve_theme()

    assert theme.output_color == "#d7d7d7"
    assert theme.styles["world.active"] == "bold reverse"
    assert theme.styles["status"] == "reverse"
    assert theme.ansi_text("warning", "notice") == "\x1b[33mnotice\x1b[0m"


def test_1976_theme_uses_the_requested_retro_palette() -> None:
    palette = resolve_theme(ThemeConfig(preset="1976")).palette

    assert palette.background == "#2b231d"
    assert palette.text == "#ebdcb9"
    assert palette.warning == "#e5a93c"
    assert palette.secondary == "#d35400"
    assert palette.success == "#556b2f"
    assert palette.error == "#962d2d"


def test_semantic_colors_override_the_selected_palette() -> None:
    theme = resolve_theme(
        ThemeConfig(
            preset="catppuccin-mocha",
            colors=ThemeColorsConfig(accent="#112233", warning="#aabbcc"),
        )
    )

    assert theme.styles["input.prompt"] == "bold fg:#112233"
    assert theme.styles["border.input"] == "fg:#112233"
    assert theme.styles["status.lowbw"] == "bold fg:#1e1e2e bg:#aabbcc"
    assert theme.ansi_text("warning", "notice") == "\x1b[38;2;170;187;204mnotice\x1b[0m"


def test_default_theme_override_changes_only_its_semantic_role() -> None:
    theme = resolve_theme(
        ThemeConfig(colors=ThemeColorsConfig(accent="#112233")),
    )

    assert theme.styles["input.prompt"] == "bold fg:#112233"
    assert theme.styles["border.input"] == "fg:#112233"
    assert theme.styles["status"] == "reverse"
    assert theme.styles["application"] == ""
    assert theme.ansi_text("warning", "notice") == "\x1b[33mnotice\x1b[0m"


def test_output_color_remains_an_explicit_plain_text_override() -> None:
    theme = resolve_theme(
        ThemeConfig(preset="catppuccin-mocha"),
        output_color="#010203",
    )

    assert theme.output_color == "#010203"


def test_ansi_text_rejects_non_status_roles() -> None:
    with pytest.raises(ValueError, match="unsupported ANSI theme role"):
        resolve_theme().ansi_text("accent", "notice")
