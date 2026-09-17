from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tfr.install_checkout import (
    _installed_build,
    _python_request_matches,
    _run,
    repository_identity,
    run,
)
from tfr.installations import InstallationError, InstallationLayout


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
