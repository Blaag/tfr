from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from tfr.config import PluginSource
from tfr.plugin_sources import (
    PluginSourceError,
    discover_source_entry_points,
    load_plugin_sources,
    normalize_repo_url,
    source_slug,
    sync_plugin_source,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_upstream_repo(tmp_path: Path, *, entry_point_name: str = "greet") -> Path:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "--quiet", "--initial-branch=main", cwd=upstream)
    _git("config", "user.email", "test@example.invalid", cwd=upstream)
    _git("config", "user.name", "Test", cwd=upstream)

    package_dir = upstream / "src" / "fixture_plugin"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        "class _Plugin:\n"
        "    api_version = 1\n\n"
        "    def register(self, registrar, config):\n"
        "        pass\n\n\n"
        "plugin = _Plugin()\n",
        encoding="utf-8",
    )
    (upstream / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fixture-plugin"\n'
        'version = "0.1.0"\n\n'
        '[project.entry-points."tfr.plugins.v1"]\n'
        f'{entry_point_name} = "fixture_plugin:plugin"\n',
        encoding="utf-8",
    )
    _git("add", "-A", cwd=upstream)
    _git("commit", "--quiet", "-m", "initial", cwd=upstream)
    return upstream


def test_normalize_repo_url_expands_github_shorthand() -> None:
    assert (
        normalize_repo_url("someone/tfr-plugins-fun")
        == "https://github.com/someone/tfr-plugins-fun.git"
    )
    assert normalize_repo_url("https://example.test/x.git") == "https://example.test/x.git"
    assert normalize_repo_url("git@github.com:someone/x.git") == "git@github.com:someone/x.git"


def test_source_slug_is_stable_and_ignores_git_suffix() -> None:
    a = source_slug("https://github.com/someone/tfr-plugins-fun.git")
    b = source_slug("https://github.com/someone/tfr-plugins-fun.git")
    assert a == b
    assert a.startswith("tfr-plugins-fun-")


def test_plugin_source_rejects_unsafe_repo_ref_and_path_values() -> None:
    with pytest.raises(ValidationError):
        PluginSource(repo="-not-a-flag")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", ref="-flag")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", path="../escape")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", path="/absolute")


async def test_sync_plugin_source_clones_a_local_repository(tmp_path: Path) -> None:
    upstream = _make_upstream_repo(tmp_path)
    source = PluginSource(repo=f"file://{upstream}")

    checkout = await sync_plugin_source(source, tmp_path / "plugins")

    assert (checkout / ".git").exists()
    assert (checkout / "pyproject.toml").is_file()


async def test_sync_plugin_source_reuses_existing_checkout_without_auto_update(
    tmp_path: Path,
) -> None:
    upstream = _make_upstream_repo(tmp_path)
    source = PluginSource(repo=f"file://{upstream}")
    plugins_directory = tmp_path / "plugins"

    first = await sync_plugin_source(source, plugins_directory)
    marker = first / "local-only.txt"
    marker.write_text("kept", encoding="utf-8")

    second = await sync_plugin_source(source, plugins_directory)

    assert second == first
    assert marker.exists()


async def test_sync_plugin_source_auto_update_pulls_new_commits(tmp_path: Path) -> None:
    upstream = _make_upstream_repo(tmp_path)
    source = PluginSource(repo=f"file://{upstream}", auto_update=True)
    plugins_directory = tmp_path / "plugins"

    checkout = await sync_plugin_source(source, plugins_directory)
    assert not (checkout / "new-file.txt").exists()

    (upstream / "new-file.txt").write_text("new", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "--quiet", "-m", "second", cwd=upstream)

    updated = await sync_plugin_source(source, plugins_directory)

    assert updated == checkout
    assert (checkout / "new-file.txt").exists()


async def test_sync_plugin_source_pinned_commit_is_never_auto_updated(tmp_path: Path) -> None:
    upstream = _make_upstream_repo(tmp_path)
    pinned_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=upstream,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = PluginSource(repo=f"file://{upstream}", ref=pinned_sha, auto_update=True)
    plugins_directory = tmp_path / "plugins"

    checkout = await sync_plugin_source(source, plugins_directory)

    (upstream / "new-file.txt").write_text("new", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "--quiet", "-m", "second", cwd=upstream)

    updated = await sync_plugin_source(source, plugins_directory)

    assert updated == checkout
    assert not (checkout / "new-file.txt").exists()


async def test_sync_plugin_source_keeps_existing_checkout_when_fetch_fails(
    tmp_path: Path,
) -> None:
    upstream = _make_upstream_repo(tmp_path)
    source = PluginSource(repo=f"file://{upstream}", auto_update=True)
    plugins_directory = tmp_path / "plugins"

    checkout = await sync_plugin_source(source, plugins_directory)
    marker = checkout / "still-here.txt"
    marker.write_text("kept", encoding="utf-8")

    import shutil

    shutil.rmtree(upstream)

    updated = await sync_plugin_source(source, plugins_directory)

    assert updated == checkout
    assert marker.exists()


def test_discover_source_entry_points_parses_pyproject_and_extends_sys_path(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    package_dir = checkout / "src" / "greet_plugin"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("value = 42\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "greet-plugin"\nversion = "0.1.0"\n\n'
        '[project.entry-points."tfr.plugins.v1"]\n'
        'greet = "greet_plugin:value"\n',
        encoding="utf-8",
    )
    source = PluginSource(repo="local/greet-plugin")

    points = discover_source_entry_points(checkout, source)

    assert len(points) == 1
    assert points[0].name == "greet"
    assert points[0].load() == 42


def test_discover_source_entry_points_rejects_symlink_that_escapes_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (checkout / "escape").symlink_to(outside)
    source = PluginSource(repo="local/x", path="escape")

    with pytest.raises(PluginSourceError, match="escapes"):
        discover_source_entry_points(checkout, source)


def test_discover_source_entry_points_requires_pyproject_toml(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = PluginSource(repo="local/x")

    with pytest.raises(PluginSourceError, match="does not exist"):
        discover_source_entry_points(checkout, source)


def test_discover_source_entry_points_returns_empty_when_no_plugin_entry_points(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "no-plugins"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    source = PluginSource(repo="local/x")

    assert discover_source_entry_points(checkout, source) == ()


async def test_load_plugin_sources_discovers_a_working_plugin_end_to_end(
    tmp_path: Path,
) -> None:
    upstream = _make_upstream_repo(tmp_path, entry_point_name="fixture")
    source = PluginSource(repo=f"file://{upstream}")

    discovered, failures = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )

    assert failures == ()
    assert len(discovered) == 1
    assert discovered[0].name == "fixture"
    plugin = discovered[0].load()
    assert plugin.api_version == 1


async def test_load_plugin_sources_reports_failures_without_raising(tmp_path: Path) -> None:
    source = PluginSource(repo=f"file://{tmp_path}/does-not-exist")

    discovered, failures = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )

    assert discovered == ()
    assert len(failures) == 1
    assert failures[0].repo == source.repo
