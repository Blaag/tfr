from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tfr.config import PluginSource
from tfr.plugin_releases import (
    PluginReleaseError,
    PluginReleaseLayout,
    PluginReleaseManifest,
    PluginReleaseNetworkError,
    current_plugin_release,
    install_plugin_release,
    rollback_plugin_release,
)
from tfr.plugin_sources import load_plugin_sources


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write_project(repository: Path, version: str, *, marker: str = "one") -> None:
    package = repository / "src" / "fixture_plugin"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        f"marker = {marker!r}\n"
        "class Plugin:\n"
        "    api_version = 1\n"
        "    def register(self, registrar, config):\n"
        "        pass\n"
        "plugin = Plugin()\n",
        encoding="utf-8",
    )
    (repository / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fixture-plugin"\n'
        f'version = "{version}"\n\n'
        '[project.entry-points."tfr.plugins.v1"]\n'
        'fixture = "fixture_plugin:plugin"\n',
        encoding="utf-8",
    )


def release_commit(repository: Path, version: str, *, marker: str) -> str:
    write_project(repository, version, marker=marker)
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", f"release {version}")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "-a", f"v{version}", "-m", f"Release v{version}")
    return commit


def make_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "upstream"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "Plugin Tests")
    git(repository, "config", "user.email", "plugins@example.invalid")
    return repository, release_commit(repository, "0.1.0", marker="one")


def manifest(version: str, commit: str) -> PluginReleaseManifest:
    content = {
        "schema_version": 1,
        "project": "fixture-plugin",
        "channel": "stable",
        "version": version,
        "tag": f"v{version}",
        "commit": commit,
        "compatibility": {
            "tfr_minimum": "0.1.0",
            "tfr_maximum_exclusive": "0.2.0",
            "plugin_api_minimum": 1,
            "plugin_api_maximum": 1,
        },
        "plugins": ["fixture"],
        "release_url": f"https://example.invalid/releases/v{version}",
        "artifact": {
            "url": f"https://example.invalid/releases/v{version}/fixture.whl",
            "size": 5,
            "sha256": hashlib.sha256(b"wheel").hexdigest(),
        },
    }
    return PluginReleaseManifest.from_json(json.dumps(content).encode())


def install(
    layout: PluginReleaseLayout,
    release: PluginReleaseManifest,
    repository: Path,
) -> Path:
    return install_plugin_release(
        layout,
        release,
        repo_url=f"file://{repository}",
        manifest_url="https://example.invalid/plugin-manifest.json",
        source_path=".",
    )


def test_manifest_is_strict_and_checks_compatibility() -> None:
    parsed = manifest("0.1.0", "a" * 40)
    parsed.assert_compatible()
    value = json.loads(
        json.dumps(
            {
                "schema_version": 1,
                "project": "fixture-plugin",
                "channel": "stable",
                "version": "0.1.0",
                "tag": "v0.1.0",
                "commit": "a" * 40,
                "compatibility": {
                    "tfr_minimum": "0.1.0",
                    "tfr_maximum_exclusive": "0.2.0",
                    "plugin_api_minimum": 1,
                    "plugin_api_maximum": 1,
                },
                "plugins": ["fixture"],
                "release_url": "https://example.invalid/release",
                "artifact": {
                    "url": "https://example.invalid/artifact",
                    "size": 1,
                    "sha256": "0" * 64,
                },
            }
        )
    )
    value["unexpected"] = True
    with pytest.raises(PluginReleaseError, match="unexpected or missing"):
        PluginReleaseManifest.from_json(json.dumps(value).encode())


def test_install_verifies_tag_commit_project_and_activates(tmp_path: Path) -> None:
    repository, commit = make_repository(tmp_path)
    layout = PluginReleaseLayout(tmp_path / "managed")

    checkout = install(layout, manifest("0.1.0", commit), repository)

    assert git(checkout, "rev-parse", "HEAD") == commit
    current = current_plugin_release(
        layout, repo_url=f"file://{repository}", source_path="."
    )
    assert current is not None
    assert current[1].version == "0.1.0"
    assert layout.current.readlink() == Path(f"releases/0.1.0+stable.{commit}")


def test_install_rejects_lightweight_or_wrong_tag(tmp_path: Path) -> None:
    repository = tmp_path / "upstream"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "Plugin Tests")
    git(repository, "config", "user.email", "plugins@example.invalid")
    write_project(repository, "0.1.0")
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "release")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "v0.1.0")

    with pytest.raises(PluginReleaseError, match="not annotated"):
        install(PluginReleaseLayout(tmp_path / "managed"), manifest("0.1.0", commit), repository)


def test_update_retains_previous_and_can_roll_back(tmp_path: Path) -> None:
    repository, first_commit = make_repository(tmp_path)
    layout = PluginReleaseLayout(tmp_path / "managed")
    install(layout, manifest("0.1.0", first_commit), repository)
    second_commit = release_commit(repository, "0.1.1", marker="two")

    second = install(layout, manifest("0.1.1", second_commit), repository)

    assert git(second, "rev-parse", "HEAD") == second_commit
    assert layout.previous.readlink() == Path(f"releases/0.1.0+stable.{first_commit}")
    rolled_back = rollback_plugin_release(
        layout, repo_url=f"file://{repository}", source_path="."
    )
    assert git(rolled_back, "rev-parse", "HEAD") == first_commit


def test_rejects_downgrade_same_version_rewrite_and_tampering(tmp_path: Path) -> None:
    repository, first_commit = make_repository(tmp_path)
    layout = PluginReleaseLayout(tmp_path / "managed")
    first = install(layout, manifest("0.1.0", first_commit), repository)
    with pytest.raises(PluginReleaseError, match="different commit"):
        install(layout, manifest("0.1.0", "a" * 40), repository)

    second_commit = release_commit(repository, "0.1.1", marker="two")
    install(layout, manifest("0.1.1", second_commit), repository)
    with pytest.raises(PluginReleaseError, match="downgrade"):
        install(layout, manifest("0.1.0", first_commit), repository)

    (first / "pyproject.toml").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(PluginReleaseError, match="local changes"):
        layout.validate_release(
            f"0.1.0+stable.{first_commit}", repo_url=f"file://{repository}", source_path="."
        )


def test_install_rejects_symlinks_that_escape_release(tmp_path: Path) -> None:
    repository, _commit = make_repository(tmp_path)
    (repository / "src" / "fixture_plugin" / "outside.py").symlink_to(
        tmp_path / "outside.py"
    )
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "unsafe symlink")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "-a", "v0.1.1", "-m", "Release v0.1.1")

    with pytest.raises(PluginReleaseError, match="symlink escapes|broken symlink"):
        install(
            PluginReleaseLayout(tmp_path / "managed"),
            manifest("0.1.1", commit),
            repository,
        )


async def test_stable_auto_and_notify_use_only_verified_releases(
    tmp_path: Path, monkeypatch
) -> None:
    repository, first_commit = make_repository(tmp_path)
    source = PluginSource(
        repo=f"file://{repository}",
        policy="stable-auto",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    monkeypatch.setattr(
        "tfr.plugin_sources.fetch_plugin_release_manifest",
        lambda _url: manifest("0.1.0", first_commit),
    )
    discovered, failures, notices = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )
    assert [entry.name for entry in discovered] == ["fixture"]
    assert failures == notices == ()

    second_commit = release_commit(repository, "0.1.1", marker="two")
    notify = source.model_copy(update={"policy": "stable-notify"})
    monkeypatch.setattr(
        "tfr.plugin_sources.fetch_plugin_release_manifest",
        lambda _url: manifest("0.1.1", second_commit),
    )
    _discovered, failures, notices = await load_plugin_sources(
        (notify,), plugins_directory=tmp_path / "plugins"
    )
    assert failures == ()
    assert len(notices) == 1
    assert "0.1.1 is available" in notices[0].message


async def test_stable_source_uses_verified_current_release_when_offline(
    tmp_path: Path, monkeypatch
) -> None:
    repository, commit = make_repository(tmp_path)
    source = PluginSource(
        repo=f"file://{repository}",
        policy="stable-auto",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    monkeypatch.setattr(
        "tfr.plugin_sources.fetch_plugin_release_manifest",
        lambda _url: manifest("0.1.0", commit),
    )
    await load_plugin_sources((source,), plugins_directory=tmp_path / "plugins")

    def offline(_url: str) -> PluginReleaseManifest:
        raise PluginReleaseNetworkError("offline")

    monkeypatch.setattr("tfr.plugin_sources.fetch_plugin_release_manifest", offline)
    discovered, failures, notices = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )
    assert [entry.name for entry in discovered] == ["fixture"]
    assert failures == ()
    assert len(notices) == 1
    assert "update check failed" in notices[0].message
