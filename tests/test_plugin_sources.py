from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tfr.config import PluginSource, UpdateConfig
from tfr.plugin_sources import (
    PluginSourceError,
    PluginUpdateChecker,
    PluginUpdateResult,
    discover_source_entry_points,
    format_plugin_update_status,
    load_plugin_sources,
    normalize_repo_url,
    source_slug,
    stable_source_slug,
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


def test_stable_source_slug_preserves_default_and_isolates_monorepo_paths() -> None:
    root = PluginSource(
        repo="owner/plugins",
        policy="stable-auto",
        manifest_url="https://example.invalid/root.json",
    )
    nested = root.model_copy(update={"path": "packages/extra"})

    assert stable_source_slug(root) == source_slug(normalize_repo_url(root.repo))
    assert stable_source_slug(nested) != stable_source_slug(root)


def test_plugin_source_rejects_unsafe_repo_ref_and_path_values() -> None:
    with pytest.raises(ValidationError):
        PluginSource(repo="-not-a-flag")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", ref="-flag")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", path="../escape")
    with pytest.raises(ValidationError):
        PluginSource(repo="a/b", path="/absolute")
    with pytest.raises(ValidationError, match="cannot contain credentials"):
        PluginSource(repo="https://user:token@example.com/plugins.git")
    assert PluginSource(repo="ssh://git@example.com/plugins.git").repo.startswith("ssh://git@")
    assert PluginSource(repo="a/b", path="./plugins//extra").path == "plugins/extra"
    with pytest.raises(ValidationError, match="manifest_url cannot contain credentials"):
        PluginSource(
            repo="a/b",
            policy="stable-auto",
            manifest_url="https://token@example.com/plugin-manifest.json",
        )
    with pytest.raises(ValidationError, match="manifest_url cannot contain credentials"):
        UpdateConfig(manifest_url="https://token@example.com/update-manifest.json")


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


async def test_sync_plugin_source_rejects_tampered_pinned_checkout(tmp_path: Path) -> None:
    upstream = _make_upstream_repo(tmp_path)
    pinned_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=upstream,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = PluginSource(repo=f"file://{upstream}", ref=pinned_sha, policy="pinned")
    checkout = await sync_plugin_source(source, tmp_path / "plugins")
    (checkout / "pyproject.toml").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(PluginSourceError, match="local changes"):
        await sync_plugin_source(source, tmp_path / "plugins")


async def test_pinned_checkout_accepts_only_runtime_bytecode(tmp_path: Path) -> None:
    upstream = _make_upstream_repo(tmp_path)
    pinned_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=upstream,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = PluginSource(repo=f"file://{upstream}", ref=pinned_sha, policy="pinned")
    checkout = await sync_plugin_source(source, tmp_path / "plugins")
    points = discover_source_entry_points(checkout, source)
    points[0].load()
    assert tuple(checkout.rglob("*.pyc"))

    assert await sync_plugin_source(source, tmp_path / "plugins") == checkout
    assert not tuple(checkout.rglob("*.pyc"))


def test_plugin_source_policy_validation() -> None:
    commit = "a" * 40
    assert PluginSource(repo="owner/repo", ref=commit, policy="pinned").policy == "pinned"
    assert (
        PluginSource(
            repo="owner/repo",
            policy="stable-auto",
            manifest_url="https://example.invalid/plugin-manifest.json",
        ).policy
        == "stable-auto"
    )
    with pytest.raises(ValidationError):
        PluginSource(repo="owner/repo", policy="pinned", ref="main")
    with pytest.raises(ValidationError):
        PluginSource(repo="owner/repo", policy="stable-auto")
    with pytest.raises(ValidationError):
        PluginSource(
            repo="owner/repo",
            policy="stable-notify",
            ref=commit,
            manifest_url="https://example.invalid/plugin-manifest.json",
        )


async def test_plugin_update_checker_reports_stable_sources(tmp_path: Path) -> None:
    source = PluginSource(
        repo="owner/plugins",
        policy="stable-notify",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    expected = PluginUpdateResult(
        repo=source.repo,
        policy=source.policy,
        checked_at=datetime.now(UTC),
        current_version="0.1.1",
        latest_version="0.1.2",
        release_url="https://example.invalid/releases/v0.1.2",
    )
    calls: list[tuple[PluginSource, Path, float]] = []

    def check(
        checked_source: PluginSource,
        plugins_directory: Path,
        timeout_seconds: float,
    ) -> PluginUpdateResult:
        calls.append((checked_source, plugins_directory, timeout_seconds))
        return expected

    checker = PluginUpdateChecker(
        (PluginSource(repo="owner/legacy"), source),
        plugins_directory=tmp_path,
        config=UpdateConfig(timeout_seconds=7),
        check_source=check,
    )

    assert checker.enabled
    assert checker.results[0].checked_at is None
    assert "not checked yet" in format_plugin_update_status(checker.results[0])

    results = await checker.check()

    assert results == (expected,)
    assert calls == [(source, tmp_path, 7)]
    assert results[0].available
    assert format_plugin_update_status(results[0]) == (
        "Plugin update available: owner/plugins 0.1.2 (current 0.1.1). "
        "https://example.invalid/releases/v0.1.2"
    )


async def test_periodic_plugin_updates_suppress_an_already_reported_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = PluginSource(
        repo="owner/plugins",
        policy="stable-notify",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    result = PluginUpdateResult(
        repo=source.repo,
        policy=source.policy,
        checked_at=datetime.now(UTC),
        current_version="0.1.1",
        latest_version="0.1.2",
        release_url="https://example.invalid/releases/v0.1.2",
    )
    checker = PluginUpdateChecker(
        (source,),
        plugins_directory=tmp_path,
        config=UpdateConfig(initial_delay_seconds=0, check_interval_seconds=300),
        notified_versions={source.repo: "0.1.2"},
        check_source=lambda _source, _directory, _timeout: result,
    )
    sleep_calls = 0

    async def sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("tfr.plugin_sources.asyncio.sleep", sleep)
    notices: list[PluginUpdateResult] = []

    with pytest.raises(asyncio.CancelledError):
        await checker.run_periodically(notices.append)

    assert notices == []


async def test_manual_plugin_check_can_suppress_the_next_periodic_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = PluginSource(
        repo="owner/plugins",
        policy="stable-notify",
        manifest_url="https://example.invalid/plugin-manifest.json",
    )
    result = PluginUpdateResult(
        repo=source.repo,
        policy=source.policy,
        checked_at=datetime.now(UTC),
        current_version="0.1.1",
        latest_version="0.1.2",
        release_url="https://example.invalid/releases/v0.1.2",
    )
    checker = PluginUpdateChecker(
        (source,),
        plugins_directory=tmp_path,
        config=UpdateConfig(initial_delay_seconds=0, check_interval_seconds=300),
        check_source=lambda _source, _directory, _timeout: result,
    )
    checker.mark_notified(await checker.check())
    sleep_calls = 0

    async def sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr("tfr.plugin_sources.asyncio.sleep", sleep)
    notices: list[PluginUpdateResult] = []

    with pytest.raises(asyncio.CancelledError):
        await checker.run_periodically(notices.append)

    assert notices == []


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

    discovered, failures, notices = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )

    assert failures == ()
    assert notices == ()
    assert len(discovered) == 1
    assert discovered[0].name == "fixture"
    plugin = discovered[0].load()
    assert plugin.api_version == 1


async def test_load_plugin_sources_reports_failures_without_raising(tmp_path: Path) -> None:
    source = PluginSource(repo=f"file://{tmp_path}/does-not-exist")

    discovered, failures, notices = await load_plugin_sources(
        (source,), plugins_directory=tmp_path / "plugins"
    )

    assert discovered == ()
    assert notices == ()
    assert len(failures) == 1
    assert failures[0].repo == source.repo
