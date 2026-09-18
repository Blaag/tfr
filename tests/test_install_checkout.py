from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tfr.install_checkout
from tfr.install_checkout import (
    _installed_build,
    _python_request_matches,
    _reject_stable_downgrade_locked,
    _run,
    install_latest_stable,
    repository_identity,
    run,
    stable_release_checkout,
)
from tfr.installations import (
    InstallationError,
    InstallationLayout,
    ReleaseMetadata,
    stable_release_id,
)
from tfr.updates import ReleaseArtifact, ReleaseManifest, UpdateError


def create_repository(path: Path) -> None:
    path.mkdir()
    (path / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "pyproject.toml"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=TFR Tests",
            "-c",
            "user.email=tfr@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def release_manifest(version: str, commit: str) -> ReleaseManifest:
    return ReleaseManifest(
        version=version,
        tag=f"v{version}",
        commit=commit,
        protocol_minimum=1,
        protocol_maximum=1,
        release_url=f"https://github.com/Blaag/tfr/releases/tag/v{version}",
        artifact=ReleaseArtifact(
            url=f"https://github.com/Blaag/tfr/releases/download/v{version}/tfr.whl",
            size=1,
            sha256="a" * 64,
        ),
    )


def create_released_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    create_repository(repository)
    released_commit = git(repository, "rev-parse", "HEAD")
    git(
        repository,
        "-c",
        "user.name=TFR Tests",
        "-c",
        "user.email=tfr@example.invalid",
        "tag",
        "-a",
        "v1.2.3",
        "-m",
        "Release v1.2.3",
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "1.2.4"\n',
        encoding="utf-8",
    )
    git(repository, "add", "pyproject.toml")
    git(
        repository,
        "-c",
        "user.name=TFR Tests",
        "-c",
        "user.email=tfr@example.invalid",
        "commit",
        "-m",
        "advance main",
    )
    main_commit = git(repository, "rev-parse", "HEAD")
    return repository, released_commit, main_commit


def test_repository_identity_uses_clean_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)

    identity = repository_identity(repository)

    assert identity.version == "1.2.3"
    assert len(identity.commit) == 40
    assert identity.release_id == f"1.2.3+git.{identity.commit}"


def test_repository_identity_rejects_dirty_checkout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    (repository / "untracked.txt").write_text("not committed", encoding="utf-8")

    with pytest.raises(InstallationError, match="uncommitted or untracked"):
        repository_identity(repository)


def test_repository_identity_rejects_local_archive_attributes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    (repository / ".git" / "info" / "attributes").write_text(
        "pyproject.toml export-ignore\n", encoding="utf-8"
    )

    with pytest.raises(InstallationError, match="unsupported local Git metadata"):
        repository_identity(repository)


def test_stable_checkout_uses_annotated_tag_when_main_has_advanced(tmp_path: Path) -> None:
    repository, released_commit, main_commit = create_released_repository(tmp_path)
    assert main_commit != released_commit

    with stable_release_checkout(
        release_manifest("1.2.3", released_commit),
        source_url=str(repository),
    ) as identity:
        assert identity.commit == released_commit
        assert identity.commit != main_commit
        assert identity.version == "1.2.3"
        assert git(identity.root, "cat-file", "-t", "refs/tags/v1.2.3") == "tag"


def test_stable_checkout_rejects_lightweight_tag(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "v1.2.3")

    with pytest.raises(InstallationError, match="not annotated"), stable_release_checkout(
        release_manifest("1.2.3", commit),
        source_url=str(repository),
    ):
        pass


def test_stable_checkout_rejects_manifest_commit_mismatch(tmp_path: Path) -> None:
    repository, released_commit, main_commit = create_released_repository(tmp_path)
    assert released_commit != main_commit

    with pytest.raises(
        InstallationError, match="does not match the release manifest"
    ), stable_release_checkout(
        release_manifest("1.2.3", main_commit),
        source_url=str(repository),
    ):
        pass


def test_stable_checkout_rejects_tagged_project_version_mismatch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    commit = git(repository, "rev-parse", "HEAD")
    git(
        repository,
        "-c",
        "user.name=TFR Tests",
        "-c",
        "user.email=tfr@example.invalid",
        "tag",
        "-a",
        "v1.2.4",
        "-m",
        "Mismatched release",
    )

    with pytest.raises(
        InstallationError, match="version does not match"
    ), stable_release_checkout(
        release_manifest("1.2.4", commit),
        source_url=str(repository),
    ):
        pass


def test_latest_stable_installs_verified_checkout_with_stable_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, released_commit, _main_commit = create_released_repository(tmp_path)
    manifest = release_manifest("1.2.3", released_commit)
    installed: dict[str, object] = {}

    def fake_install(
        source_repository: Path,
        _layout: InstallationLayout,
        *,
        python: str,
        activate: bool,
        provenance: str,
    ) -> ReleaseMetadata:
        identity = repository_identity(source_repository)
        installed.update(
            commit=identity.commit,
            version=identity.version,
            python=python,
            activate=activate,
            source=provenance,
        )
        return ReleaseMetadata(
            release_id=stable_release_id(identity.version, identity.commit),
            version=identity.version,
            commit=identity.commit,
            source=provenance,
            installed_at=datetime.now(UTC).isoformat(),
            python_version="3.12.13",
            wheel_sha256="b" * 64,
            lock_sha256="c" * 64,
        )

    monkeypatch.setattr(tfr.install_checkout, "install_checkout", fake_install)
    layout = InstallationLayout.defaults(
        root=tmp_path / "managed",
        bin_directory=tmp_path / "bin",
    )

    metadata = install_latest_stable(
        layout,
        python="3.12.13",
        activate=False,
        source_url=str(repository),
        fetch_manifest=lambda _url, *, timeout: manifest,
    )

    assert metadata.commit == released_commit
    assert installed == {
        "commit": released_commit,
        "version": "1.2.3",
        "python": "3.12.13",
        "activate": False,
        "source": "stable-release",
    }


def test_latest_stable_fails_closed_when_manifest_fetch_fails(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(
        root=tmp_path / "managed",
        bin_directory=tmp_path / "bin",
    )

    def fail(_url: str, *, timeout: float) -> ReleaseManifest:
        raise UpdateError("offline")

    with pytest.raises(InstallationError, match="cannot resolve.*offline"):
        install_latest_stable(layout, fetch_manifest=fail)


def stage_stable_release(
    layout: InstallationLayout,
    version: str,
    commit: str,
) -> None:
    release_id = stable_release_id(version, commit)
    staging = layout.create_staging_directory(release_id)
    python = staging / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    layout.commit_staged_release(
        staging,
        ReleaseMetadata(
            release_id=release_id,
            version=version,
            commit=commit,
            source="stable-release",
            installed_at=datetime.now(UTC).isoformat(),
            python_version="3.12.13",
            wheel_sha256="b" * 64,
            lock_sha256="c" * 64,
        ),
    )


def test_stable_install_rejects_downgrade_from_inactive_release(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(
        root=tmp_path / "managed",
        bin_directory=tmp_path / "bin",
    )
    stage_stable_release(layout, "2.0.0", "a" * 40)

    with layout.lock(), pytest.raises(InstallationError, match="older than installed"):
        _reject_stable_downgrade_locked(layout, "1.9.9", "b" * 40)


def test_stable_install_rejects_same_version_with_different_commit(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(
        root=tmp_path / "managed",
        bin_directory=tmp_path / "bin",
    )
    stage_stable_release(layout, "1.2.3", "a" * 40)

    with layout.lock(), pytest.raises(InstallationError, match="different commit"):
        _reject_stable_downgrade_locked(layout, "1.2.3", "b" * 40)


def test_stable_install_allows_exact_reinstall_and_newer_release(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(
        root=tmp_path / "managed",
        bin_directory=tmp_path / "bin",
    )
    stage_stable_release(layout, "1.2.3", "a" * 40)

    with layout.lock():
        _reject_stable_downgrade_locked(layout, "1.2.3", "a" * 40)
        _reject_stable_downgrade_locked(layout, "1.2.4", "b" * 40)


def test_python_request_matches_version_prefix() -> None:
    assert _python_request_matches("3.12", "3.12.13") is True
    assert _python_request_matches("cpython-3.12.13", "3.12.13") is True
    assert _python_request_matches("3.13", "3.12.13") is False


def test_subprocesses_do_not_inherit_python_module_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/untrusted/modules")
    monkeypatch.setenv("PYTHONHOME", "/untrusted/python")
    monkeypatch.setenv("GIT_DIR", "/untrusted/repository")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/untrusted/objects")

    result = _run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('PYTHONPATH'), os.environ.get('PYTHONHOME'), "
            "os.environ.get('GIT_DIR'), os.environ.get('GIT_OBJECT_DIRECTORY'))",
        ],
        capture=True,
    )

    assert result.stdout.strip() == "None None None None"


def test_installed_build_probe_ignores_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow = tmp_path / "tfr"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "updates.py").write_text(
        "raise RuntimeError('working-directory package was imported')\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    build = _installed_build(Path(sys.executable))

    assert build["version"] == "0.1.0"


def test_list_operation_does_not_require_a_repository(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "tfr"
    bin_directory = tmp_path / "bin"

    result = run(["--root", str(root), "--bin-dir", str(bin_directory), "--list"])

    assert result == 0
    assert capsys.readouterr().out == ""
    assert InstallationLayout.defaults(root=root, bin_directory=bin_directory).releases.is_dir()


def test_latest_stable_cli_does_not_install_bootstrap_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: dict[str, object] = {}

    def fake_install(
        layout: InstallationLayout,
        *,
        python: str,
        activate: bool,
    ) -> ReleaseMetadata:
        called.update(root=layout.root, python=python, activate=activate)
        return ReleaseMetadata(
            release_id="1.2.3+stable." + "a" * 40,
            version="1.2.3",
            commit="a" * 40,
            source="stable-release",
            installed_at=datetime.now(UTC).isoformat(),
            python_version="3.12.13",
            wheel_sha256="b" * 64,
            lock_sha256="c" * 64,
        )

    monkeypatch.setattr(tfr.install_checkout, "install_latest_stable", fake_install)
    root = tmp_path / "managed"
    bin_directory = tmp_path / "bin"

    result = run(
        [
            "--root",
            str(root),
            "--bin-dir",
            str(bin_directory),
            "--python",
            "3.12.13",
            "--no-activate",
            "--latest-stable",
        ],
        repository=tmp_path / "must-not-be-used",
    )

    assert result == 0
    assert called == {"root": root, "python": "3.12.13", "activate": False}
    assert "Installed TFR 1.2.3+stable." in capsys.readouterr().out
