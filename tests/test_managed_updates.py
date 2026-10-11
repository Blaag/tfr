from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import tfr.managed_updates
from tfr.config import UpdateConfig
from tfr.installations import ReleaseMetadata
from tfr.managed_updates import _stage_managed_update
from tfr.updates import ReleaseArtifact, ReleaseManifest


def manifest() -> ReleaseManifest:
    return ReleaseManifest(
        version="1.2.3",
        tag="v1.2.3",
        commit="a" * 40,
        protocol_minimum=1,
        protocol_maximum=99,
        release_url="https://github.com/Blaag/tfr/releases/tag/v1.2.3",
        artifact=ReleaseArtifact(
            url="https://github.com/Blaag/tfr/releases/download/v1.2.3/"
            "tfr-1.2.3-release.zip",
            size=123,
            sha256="b" * 64,
        ),
    )


def metadata() -> ReleaseMetadata:
    return ReleaseMetadata(
        release_id="1.2.3+stable." + "a" * 40,
        version="1.2.3",
        commit="a" * 40,
        source="stable-release",
        installed_at="2026-01-01T00:00:00+00:00",
        python_version="3.12.13",
        wheel_sha256="c" * 64,
        lock_sha256="d" * 64,
    )


def test_managed_update_uses_verified_bundle_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = manifest()
    installed = metadata()
    calls: list[tuple[object, ...]] = []
    current = SimpleNamespace(metadata=SimpleNamespace(python_version="3.12.13"))
    monkeypatch.setattr(
        tfr.managed_updates, "managed_installation", lambda: (tmp_path, current)
    )
    monkeypatch.setattr(
        tfr.managed_updates, "fetch_release_manifest", lambda *_args, **_kwargs: release
    )
    def install(*args: object, **kwargs: object) -> ReleaseMetadata:
        calls.append((*args, kwargs))
        return installed

    monkeypatch.setattr(
        tfr.managed_updates, "install_release_bundle", install
    )

    staged = _stage_managed_update(
        UpdateConfig(state_directory=tmp_path), expected_manifest=release
    )

    assert staged.release_id == installed.release_id
    assert len(calls) == 1
    assert calls[0][-1] == {"python": "3.12.13", "activate": False}


def test_managed_update_uses_tagged_source_for_legacy_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = replace(
        manifest(),
        artifact=ReleaseArtifact(
            url="https://github.com/Blaag/tfr/releases/download/v1.2.3/"
            "tfr-1.2.3-py3-none-any.whl",
            size=123,
            sha256="b" * 64,
        ),
    )
    installed = metadata()
    current = SimpleNamespace(metadata=SimpleNamespace(python_version="3.12.13"))
    monkeypatch.setattr(
        tfr.managed_updates, "managed_installation", lambda: (tmp_path, current)
    )
    monkeypatch.setattr(
        tfr.managed_updates, "fetch_release_manifest", lambda *_args, **_kwargs: release
    )
    monkeypatch.setattr(
        tfr.managed_updates,
        "install_release_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bundle installer must not run for legacy manifests")
        ),
    )
    calls: list[tuple[object, ...]] = []

    def install(*args: object, **kwargs: object) -> ReleaseMetadata:
        calls.append((*args, kwargs))
        return installed

    monkeypatch.setattr(tfr.managed_updates, "install_stable_manifest", install)

    _stage_managed_update(
        UpdateConfig(state_directory=tmp_path), expected_manifest=release
    )

    assert len(calls) == 1
