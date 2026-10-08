from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import tfr.updates
from tfr.config import UpdateConfig
from tfr.updates import (
    BuildIdentity,
    ReleaseManifest,
    UpdateChecker,
    UpdateError,
    fetch_release_manifest,
    format_update_status,
    update_available,
)


def manifest_data(*, version: str = "1.2.3", channel: str = "stable") -> dict[str, object]:
    tag = f"v{version}" if channel == "stable" else f"v{version}-candidate.1"
    return {
        "schema_version": 1,
        "project": "tfr",
        "channel": channel,
        "version": version,
        "tag": tag,
        "commit": "a" * 40,
        "protocol": {"minimum": 2, "maximum": 2},
        "release_url": f"https://github.com/Blaag/tfr/releases/tag/{tag}",
        "artifact": {
            "url": f"https://github.com/Blaag/tfr/releases/download/{tag}/tfr.whl",
            "size": 1234,
            "sha256": "b" * 64,
        },
    }


def manifest_bytes(*, version: str = "1.2.3") -> bytes:
    return json.dumps(manifest_data(version=version)).encode()


def test_manifest_is_strict_and_compares_stable_versions() -> None:
    manifest = ReleaseManifest.from_json(manifest_bytes())

    assert manifest.version == "1.2.3"
    assert manifest.supports(BuildIdentity("1.0.0", None)) is True
    assert update_available(BuildIdentity("1.2.2", None), manifest) is True
    assert update_available(BuildIdentity("1.2.3", "a" * 40), manifest) is False
    assert update_available(BuildIdentity("1.2.3", "c" * 40), manifest) is True
    assert update_available(BuildIdentity("2.0.0", None), manifest) is False
    assert update_available(BuildIdentity("1.2.3rc1", None), manifest) is True
    assert BuildIdentity("1.2.3+vendor.1", None).version == "1.2.3+vendor.1"
    assert update_available(BuildIdentity("1.2.3.post1", None), manifest) is False
    assert update_available(BuildIdentity("1.2.3.1", None), manifest) is False
    assert update_available(BuildIdentity("1!1.2.3", None), manifest) is False


def test_manifest_channel_must_match_the_selected_update_channel() -> None:
    candidate = json.dumps(manifest_data(channel="candidate")).encode()

    manifest = ReleaseManifest.from_json(candidate, expected_channel="candidate")

    assert manifest.channel == "candidate"
    with pytest.raises(UpdateError, match="stable channel"):
        ReleaseManifest.from_json(candidate)


def test_manifest_rejects_unknown_fields_and_insecure_urls() -> None:
    unknown = manifest_data()
    unknown["extra"] = True
    insecure = manifest_data()
    artifact = insecure["artifact"]
    assert isinstance(artifact, dict)
    artifact["url"] = "http://example.com/tfr.whl"
    injected = manifest_data()
    injected["release_url"] = "https://example.com/release\n-- forged --"
    reordered = manifest_data()
    reordered["release_url"] = "https://example.com/release\u202eexe"

    for value in (unknown, insecure, injected, reordered):
        try:
            ReleaseManifest.from_json(json.dumps(value).encode())
        except UpdateError:
            pass
        else:
            raise AssertionError("invalid manifest was accepted")


def test_live_manifest_fetch_does_not_use_cached_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None, float]] = []

    def fetch(url: str, etag: str | None, timeout: float) -> object:
        calls.append((url, etag, timeout))
        return SimpleNamespace(content=manifest_bytes(), etag='"release-1"')

    monkeypatch.setattr(tfr.updates, "_fetch_manifest", fetch)

    manifest = fetch_release_manifest("https://updates.example.com/stable.json", timeout=7)

    assert manifest.version == "1.2.3"
    assert calls == [("https://updates.example.com/stable.json", None, 7.0)]


def test_live_manifest_fetch_rejects_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tfr.updates,
        "_fetch_manifest",
        lambda _url, _etag, _timeout: SimpleNamespace(content=None, etag='"stale"'),
    )

    with pytest.raises(UpdateError, match="returned no manifest"):
        fetch_release_manifest("https://updates.example.com/stable.json")


def test_live_candidate_fetch_uses_release_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(manifest_data(channel="candidate")).encode()
    calls: list[tuple[str, str | None, float]] = []

    def fetch(url: str, etag: str | None, timeout: float) -> object:
        calls.append((url, etag, timeout))
        return SimpleNamespace(content=content, etag='"candidate"')

    monkeypatch.setattr(tfr.updates, "_fetch_candidate_manifest", fetch)

    manifest = fetch_release_manifest(
        "https://api.github.com/repos/Blaag/tfr/releases?per_page=20",
        timeout=7,
        expected_channel="candidate",
    )

    assert manifest.channel == "candidate"
    assert calls == [
        (
            "https://api.github.com/repos/Blaag/tfr/releases?per_page=20",
            None,
            7.0,
        )
    ]


def test_candidate_feed_selects_newest_usable_prerelease_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases = [
        {
            "tag_name": "v1.2.3-candidate.1",
            "draft": False,
            "prerelease": True,
            "assets": [
                {
                    "name": "update-manifest.json",
                    "browser_download_url": "https://example.com/candidate-1.json",
                }
            ],
        },
        {
            "tag_name": "v1.2.2-candidate.9",
            "draft": False,
            "prerelease": True,
            "assets": [],
        },
    ]

    class Response:
        headers = {"ETag": '"feed-2"'}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def geturl(self) -> str:
            return "https://api.github.com/repos/Blaag/tfr/releases?per_page=20"

        def read(self, _maximum: int) -> bytes:
            return json.dumps(releases).encode()

    class Opener:
        def open(self, _request: object, *, timeout: float) -> Response:
            assert timeout == 7
            return Response()

    monkeypatch.setattr(tfr.updates.urllib.request, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(
        tfr.updates,
        "_fetch_manifest",
        lambda url, etag, timeout: SimpleNamespace(
            content=json.dumps(manifest_data(channel="candidate")).encode(),
            etag=None,
        )
        if (url, etag, timeout) == ("https://example.com/candidate-1.json", None, 7)
        else pytest.fail("unexpected candidate manifest request"),
    )

    result = tfr.updates._fetch_candidate_manifest(
        "https://api.github.com/repos/Blaag/tfr/releases?per_page=20", None, 7
    )

    assert result.content is not None
    assert result.etag == '"feed-2"'


async def test_checker_fetches_caches_and_reuses_304_manifest(tmp_path: Path) -> None:
    calls: list[str | None] = []

    def fetch(_url: str, etag: str | None, _timeout: float) -> object:
        calls.append(etag)
        if etag is None:
            return SimpleNamespace(content=manifest_bytes(), etag='"release-1"')
        return SimpleNamespace(content=None, etag=etag)

    config = UpdateConfig(state_directory=tmp_path, initial_delay_seconds=0)
    build = BuildIdentity("1.0.0", "c" * 40)
    checker = UpdateChecker(config, build=build, fetch=fetch)  # type: ignore[arg-type]

    first = await checker.check()
    second = await checker.check()

    assert first.available_for(build) is True
    assert second.error is None
    assert calls == [None, '"release-1"']
    assert checker.cache_path.is_file()
    restored = UpdateChecker(config, build=build, fetch=fetch)  # type: ignore[arg-type]
    assert restored.result.manifest == first.manifest
    assert restored.result.checked_at == second.checked_at


async def test_candidate_checker_uses_isolated_cache_and_rejects_stable_manifest(
    tmp_path: Path,
) -> None:
    def candidate_fetch(_url: str, _etag: str | None, _timeout: float) -> object:
        content = json.dumps(manifest_data(channel="candidate")).encode()
        return SimpleNamespace(content=content, etag='"candidate-1"')

    config = UpdateConfig(channel="candidate", state_directory=tmp_path)
    checker = UpdateChecker(config, fetch=candidate_fetch)  # type: ignore[arg-type]
    result = await checker.check()

    assert result.error is None
    assert result.manifest is not None
    assert result.manifest.channel == "candidate"
    assert checker.cache_path == tmp_path / "candidate.json"
    assert str(config.manifest_url).startswith(
        "https://api.github.com/repos/Blaag/tfr/releases?"
    )

    stable_content = manifest_bytes()
    checker = UpdateChecker(
        config,
        fetch=lambda _url, _etag, _timeout: SimpleNamespace(  # type: ignore[arg-type]
            content=stable_content, etag=None
        ),
    )
    assert (await checker.check()).error == "release manifest is not for TFR's candidate channel"


async def test_checker_preserves_cached_release_on_network_failure(tmp_path: Path) -> None:
    def successful(_url: str, _etag: str | None, _timeout: float) -> object:
        return SimpleNamespace(content=manifest_bytes(), etag=None)

    config = UpdateConfig(state_directory=tmp_path)
    checker = UpdateChecker(config, fetch=successful)  # type: ignore[arg-type]
    await checker.check()

    def failed(_url: str, _etag: str | None, _timeout: float) -> object:
        raise UpdateError("offline")

    checker = UpdateChecker(config, fetch=failed)  # type: ignore[arg-type]
    result = await checker.check()

    assert result.manifest is not None
    assert result.error == "offline"
    assert "update check failed: offline" in format_update_status("UI", checker.build, result)


async def test_cache_is_bound_to_manifest_url(tmp_path: Path) -> None:
    def first_fetch(_url: str, _etag: str | None, _timeout: float) -> object:
        return SimpleNamespace(content=manifest_bytes(), etag='"first"')

    first = UpdateChecker(UpdateConfig(state_directory=tmp_path), fetch=first_fetch)  # type: ignore[arg-type]
    await first.check()
    seen_etags: list[str | None] = []

    def second_fetch(_url: str, etag: str | None, _timeout: float) -> object:
        seen_etags.append(etag)
        return SimpleNamespace(content=manifest_bytes(), etag='"second"')

    second = UpdateChecker(
        UpdateConfig(
            state_directory=tmp_path,
            manifest_url="https://updates.example.com/stable.json",
        ),
        fetch=second_fetch,  # type: ignore[arg-type]
    )

    assert second.result.manifest is None
    await second.check()
    assert seen_etags == [None]


async def test_cache_write_failure_does_not_hide_live_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fetch(_url: str, _etag: str | None, _timeout: float) -> object:
        return SimpleNamespace(content=manifest_bytes(), etag=None)

    checker = UpdateChecker(
        UpdateConfig(state_directory=tmp_path),
        build=BuildIdentity("1.0.0", None),
        fetch=fetch,  # type: ignore[arg-type]
    )

    def fail() -> None:
        raise OSError("read-only cache")

    monkeypatch.setattr(checker, "_write_cache", fail)
    result = await checker.check()

    assert result.error is None
    assert result.available_for(checker.build) is True


def test_cache_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    cache_directory = tmp_path / "cache"
    cache_directory.mkdir(mode=0o700)
    os.symlink(target, cache_directory / "stable.json")

    checker = UpdateChecker(UpdateConfig(state_directory=cache_directory))

    assert checker.result.manifest is None


def test_release_manifest_builder_uses_protocol_and_artifact_digest(tmp_path: Path) -> None:
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    artifact = tmp_path / f"tfr-{version}-py3-none-any.whl"
    artifact.write_bytes(b"wheel")
    output = tmp_path / "manifest.json"

    subprocess.run(
        [
            sys.executable,
            "scripts/build_update_manifest.py",
            "--repository",
            "Blaag/tfr",
            "--tag",
            f"v{version}",
            "--commit",
            "a" * 40,
            "--artifact",
            str(artifact),
            "--output",
            str(output),
        ],
        check=True,
    )
    manifest = ReleaseManifest.from_json(output.read_bytes())

    assert manifest.protocol_minimum == 2
    assert manifest.protocol_maximum == 2
    assert manifest.artifact.size == 5
    assert manifest.artifact.sha256 == hashlib.sha256(b"wheel").hexdigest()


def test_release_manifest_builder_supports_candidate_channel(tmp_path: Path) -> None:
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    artifact = tmp_path / f"tfr-{version}-py3-none-any.whl"
    artifact.write_bytes(b"candidate-wheel")
    output = tmp_path / "candidate.json"

    subprocess.run(
        [
            sys.executable,
            "scripts/build_update_manifest.py",
            "--repository",
            "Blaag/tfr",
            "--tag",
            f"v{version}-candidate.1",
            "--commit",
            "a" * 40,
            "--artifact",
            str(artifact),
            "--channel",
            "candidate",
            "--output",
            str(output),
        ],
        check=True,
    )
    manifest = ReleaseManifest.from_json(
        output.read_bytes(), expected_channel="candidate"
    )

    assert manifest.channel == "candidate"
    assert manifest.tag == f"v{version}-candidate.1"
