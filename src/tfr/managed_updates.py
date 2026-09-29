from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from tfr.config import UpdateConfig
from tfr.install_checkout import install_stable_manifest
from tfr.installations import InstallationError, managed_installation
from tfr.updates import (
    ReleaseManifest,
    UpdateError,
    current_build,
    fetch_release_manifest,
)


@dataclass(frozen=True, slots=True)
class StagedManagedUpdate:
    release_id: str
    version: str
    commit: str
    release_url: str
    manifest: ReleaseManifest


def _stage_managed_update(
    config: UpdateConfig,
    expected_manifest: ReleaseManifest | None,
) -> StagedManagedUpdate:
    if not config.enabled:
        raise InstallationError("stable updates are disabled")
    managed = managed_installation()
    if managed is None:
        raise InstallationError(
            "automatic updates require a valid managed TFR installation"
        )
    layout, current = managed
    manifest = fetch_release_manifest(
        str(config.manifest_url), timeout=config.timeout_seconds
    )
    if expected_manifest is not None and manifest.as_dict() != expected_manifest.as_dict():
        raise InstallationError(
            "this host's configured stable manifest does not match the Gateway release"
        )
    if not manifest.supports(current_build()):
        raise InstallationError(
            f"stable release {manifest.version} requires a manual protocol upgrade"
        )
    metadata = install_stable_manifest(
        layout,
        manifest,
        python=current.metadata.python_version,
        activate=False,
    )
    return StagedManagedUpdate(
        release_id=metadata.release_id,
        version=manifest.version,
        commit=manifest.commit,
        release_url=manifest.release_url,
        manifest=manifest,
    )


def _stage_managed_update_with_retry(
    config: UpdateConfig,
    expected_manifest: ReleaseManifest | None,
) -> StagedManagedUpdate:
    deadline = time.monotonic() + 900
    while True:
        try:
            return _stage_managed_update(config, expected_manifest)
        except InstallationError as exc:
            if (
                str(exc) != "another TFR installation operation is running"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.25)


async def stage_managed_update(
    config: UpdateConfig,
    *,
    expected_manifest: ReleaseManifest | None = None,
) -> StagedManagedUpdate:
    try:
        return await asyncio.to_thread(
            _stage_managed_update_with_retry, config, expected_manifest
        )
    except (OSError, UpdateError, ValueError) as exc:
        if isinstance(exc, InstallationError):
            raise
        raise InstallationError(f"cannot stage the stable update: {exc}") from exc


def activate_managed_update(release_id: str) -> None:
    deadline = time.monotonic() + 30
    while True:
        managed = managed_installation()
        if managed is None:
            raise InstallationError(
                "automatic updates require a valid managed TFR installation"
            )
        layout, _current = managed
        try:
            layout.activate(release_id, write_launcher=False)
            return
        except InstallationError as exc:
            if (
                str(exc) != "another TFR installation operation is running"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)
