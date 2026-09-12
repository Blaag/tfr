from __future__ import annotations

import pytest

from tfr.plugin_api import MAX_SCREEN_CLEAR_DURATION_SECONDS, PLUGIN_API_VERSION, escape_world_text


def test_public_plugin_api_exposes_version_and_world_text_escaping() -> None:
    text = " a\t[%]{x}(),;#\\"

    assert PLUGIN_API_VERSION == 1
    assert MAX_SCREEN_CLEAR_DURATION_SECONDS == 60
    assert escape_world_text(text, "bare") == text
    assert escape_world_text(text, "tinymux") == r"%ba%t\[\%\]\{x\}\(\)\,\;\#\\"
    assert escape_world_text(text, "tinymush") == r"%ba%t\[\%\]\{x\}(),\;#\\"


def test_world_text_escaping_rejects_unsupported_servers() -> None:
    with pytest.raises(ValueError, match="bare, tinymush, or tinymux"):
        escape_world_text("hello", "generic")
