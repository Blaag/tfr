from __future__ import annotations

import json
from pathlib import Path

import pytest

from tfr.config import WorldsConfig
from tfr.setup_install import (
    SCHEMA_ROOT,
    _configure_worlds,
    _world_entry,
    generate_worlds_config,
    launcher_script,
)


def test_generated_worlds_config_is_valid_and_escapes_credentials() -> None:
    content = generate_worlds_config(
        {
            "example": {
                "aliases": ["ex"],
                "host": "world.example.org",
                "port": 4201,
                "server": "tinymux",
                "capabilities": {"unicode": True},
                "tls": {"enabled": True, "verify": True},
                "login": {"character": 'A "Name"', "password": "secret\\value"},
                "autoconnect": True,
                "provenance": {"nospoof": True, "show_prefix": False},
                "startup_commands": [],
            }
        }
    )

    value = json.loads(content)
    worlds = WorldsConfig.model_validate(value)

    assert worlds.worlds["example"].login is not None
    assert worlds.worlds["example"].login.password.get_secret_value() == "secret\\value"


def test_generated_launcher_quotes_paths_and_forwards_arguments(tmp_path: Path) -> None:
    executable = tmp_path / "TFR bin" / "tfr"
    token = tmp_path / "a token"
    script = launcher_script([str(executable), "ui", "--token-file", str(token)])

    assert f"'{executable}'" in script
    assert f"'{token}'" in script
    assert script.endswith('"$@"\n')


def test_rebuilding_worlds_confirms_each_replacement_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    examples = repository / "examples"
    examples.mkdir(parents=True)
    (examples / "worlds.jsonc").write_text(
        '{"$schema": "../schemas/worlds.schema.json", "schema_version": 1, '
        '"defaults": {}, "worlds": {}}\n',
        encoding="utf-8",
    )
    (examples / "agents.jsonc").write_text("{}\n", encoding="utf-8")
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    worlds = config_directory / "worlds.jsonc"
    worlds.write_text("old worlds\n", encoding="utf-8")

    answers = iter([True, False])
    prompts: list[str] = []

    def answer(prompt: str, *, default: bool = True) -> bool:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("tfr.setup_install._yes_no", answer)

    _configure_worlds(repository, config_directory)

    assert len(prompts) == 2
    assert prompts[0].startswith("Replace or rebuild existing worlds file")
    assert prompts[1] == "Configure one or more worlds interactively now?"
    document = json.loads(worlds.read_text(encoding="utf-8"))
    assert document["$schema"] == f"{SCHEMA_ROOT}/worlds.schema.json"
    assert list(config_directory.glob("worlds.jsonc.backup-*"))


def test_world_entry_reprompts_for_invalid_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(
        [
            "example",
            "world.example.org",
            "4201",
            "1",
            "1bad,EX,ex",
            "ex,other-world",
            "n",
            "",
            "n",
            "n",
            "y",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    name, entry = _world_entry(set())

    assert name == "example"
    assert entry["aliases"] == ["ex", "other-world"]
