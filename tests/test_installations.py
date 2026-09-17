from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tfr.installations import (
    InstallationError,
    InstallationLayout,
    ReleaseMetadata,
    checkout_release_id,
    managed_restart_command,
    release_python,
)


def metadata(release_id: str, commit: str) -> ReleaseMetadata:
    return ReleaseMetadata(
        release_id=release_id,
        version="1.2.3",
        commit=commit,
        source="git-checkout",
        installed_at=datetime.now(UTC).isoformat(),
        python_version="3.12.13",
        wheel_sha256="a" * 64,
        lock_sha256="b" * 64,
    )


def stage_release(
    layout: InstallationLayout,
    release_id: str,
    commit: str,
) -> ReleaseMetadata:
    staging = layout.create_staging_directory(release_id)
    python = release_python(staging)
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    release_metadata = metadata(release_id, commit)
    layout.commit_staged_release(staging, release_metadata)
    return release_metadata


def test_checkout_release_id_includes_exact_commit() -> None:
    commit = "a" * 40

    assert checkout_release_id("1.2.3", commit) == f"1.2.3+git.{commit}"

    with pytest.raises(InstallationError):
        checkout_release_id("1.2.3", "short")


def test_release_metadata_is_strict() -> None:
    value = metadata(f"1.2.3+git.{'a' * 40}", "a" * 40).as_dict()

    assert ReleaseMetadata.from_mapping(value).commit == "a" * 40
    value["unexpected"] = True
    with pytest.raises(InstallationError, match="unexpected or missing"):
        ReleaseMetadata.from_mapping(value)


def test_checkout_metadata_requires_matching_release_id() -> None:
    with pytest.raises(InstallationError, match="does not match"):
        metadata(f"1.2.3+git.{'b' * 40}", "a" * 40)


def test_activation_and_rollback_swap_managed_pointers(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(
        root=tmp_path / "data" / "tfr",
        bin_directory=tmp_path / "bin",
    )
    first_id = f"1.2.3+git.{'a' * 40}"
    second_id = f"1.2.3+git.{'b' * 40}"
    stage_release(layout, first_id, "a" * 40)
    stage_release(layout, second_id, "b" * 40)

    layout.activate(first_id)
    layout.activate(second_id)

    releases = {release.metadata.release_id: release for release in layout.list_releases()}
    assert releases[second_id].current is True
    assert releases[first_id].previous is True
    assert os.readlink(layout.current) == f"releases/{second_id}"
    assert layout.launcher.stat().st_mode & 0o111
    launcher = layout.launcher.read_text(encoding="utf-8")
    assert "current/bin/python" in launcher
    assert " -I -m tfr " in launcher

    rolled_back = layout.rollback()

    assert rolled_back.metadata.release_id == first_id
    assert os.readlink(layout.current) == f"releases/{first_id}"
    assert os.readlink(layout.previous) == f"releases/{second_id}"


def test_activation_refuses_pointer_outside_releases(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")
    release_id = f"1.2.3+git.{'a' * 40}"
    stage_release(layout, release_id, "a" * 40)
    os.symlink("../outside", layout.current)

    with pytest.raises(InstallationError, match="escapes releases"):
        layout.activate(release_id)


def test_launcher_does_not_replace_unmanaged_file(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")
    layout.bin_directory.mkdir()
    layout.launcher.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")

    with pytest.raises(InstallationError, match="unmanaged launcher"):
        layout.write_launcher()


def test_activation_refusal_preserves_managed_pointers(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")
    first_id = f"1.2.3+git.{'a' * 40}"
    second_id = f"1.2.3+git.{'b' * 40}"
    stage_release(layout, first_id, "a" * 40)
    stage_release(layout, second_id, "b" * 40)
    layout.activate(first_id)
    layout.launcher.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")

    with pytest.raises(InstallationError, match="unmanaged launcher"):
        layout.activate(second_id)

    assert os.readlink(layout.current) == f"releases/{first_id}"
    assert not os.path.lexists(layout.previous)


def test_activation_failure_restores_previous_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")
    first_id = f"1.2.3+git.{'a' * 40}"
    second_id = f"1.2.3+git.{'b' * 40}"
    stage_release(layout, first_id, "a" * 40)
    stage_release(layout, second_id, "b" * 40)
    layout.activate(first_id)
    original_replace = os.replace

    def fail_current(source: Path | str, destination: Path | str) -> None:
        if Path(destination) == layout.current:
            raise OSError("simulated pointer failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_current)

    with pytest.raises(InstallationError, match="cannot activate"):
        layout.activate(second_id)

    assert os.readlink(layout.current) == f"releases/{first_id}"
    assert not os.path.lexists(layout.previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are required")
def test_prepare_rejects_writable_ancestor(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o777)
    unsafe.chmod(0o777)
    layout = InstallationLayout.defaults(root=unsafe / "tfr", bin_directory=tmp_path / "bin")

    with pytest.raises(InstallationError, match="ancestor is writable"):
        layout.prepare()


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership is required")
def test_prepare_rejects_untrusted_sticky_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o1777)
    actual_user = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual_user + 1)
    layout = InstallationLayout.defaults(root=unsafe / "tfr", bin_directory=tmp_path / "bin")

    with pytest.raises(InstallationError, match="untrusted owner"):
        layout.prepare()


def test_installation_lock_rejects_concurrent_writer(tmp_path: Path) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")

    with (
        layout.lock(),
        pytest.raises(InstallationError, match="another TFR installation"),
        layout.lock(),
    ):
        pass


def test_managed_restart_uses_current_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = InstallationLayout.defaults(root=tmp_path / "tfr", bin_directory=tmp_path / "bin")
    release_id = f"1.2.3+git.{'a' * 40}"
    stage_release(layout, release_id, "a" * 40)
    layout.activate(release_id)
    monkeypatch.setenv("TFR_MANAGED_ROOT", str(layout.root))

    command = managed_restart_command(["ui", "--socket", "/tmp/tfr.sock"])

    assert command == [
        str(release_python(layout.releases / release_id)),
        "-I",
        "-m",
        "tfr",
        "ui",
        "--socket",
        "/tmp/tfr.sock",
    ]
