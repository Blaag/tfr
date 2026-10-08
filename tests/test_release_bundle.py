from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tfr.release_bundle import (
    ReleaseBundleError,
    build_release_bundle,
    verify_release_bundle,
)

VERSION = "1.2.3"
COMMIT = "a" * 40


def wheel_bytes(*, version: str = VERSION, commit: str = COMMIT) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as wheel:
        wheel.writestr(
            f"tfr-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: tfr\nVersion: {version}\n",
        )
        wheel.writestr("tfr/_build.py", f'BUILD_COMMIT: str | None = "{commit}"\n')
    return output.getvalue()


def create_inputs(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / f"tfr-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(wheel_bytes())
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "dependency==4.5.6 --hash=sha256:" + "b" * 64 + "\n",
        encoding="utf-8",
    )
    return wheel, requirements


def build(tmp_path: Path, name: str = "bundle.zip") -> Path:
    wheel, requirements = create_inputs(tmp_path)
    output = tmp_path / name
    build_release_bundle(
        wheel_path=wheel,
        requirements_path=requirements,
        version=VERSION,
        commit=COMMIT,
        output=output,
    )
    return output


def rewrite_bundle(path: Path, transform: object) -> None:
    with zipfile.ZipFile(path) as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(path, "w") as archive:
        for info, content in members:
            new_name, new_content, mode = transform(info.filename, content)  # type: ignore[operator]
            rewritten = zipfile.ZipInfo(new_name, date_time=(1980, 1, 1, 0, 0, 0))
            rewritten.external_attr = mode << 16
            archive.writestr(rewritten, new_content)


def test_build_and_verify_release_bundle(tmp_path: Path) -> None:
    bundle_path = build(tmp_path)

    bundle = verify_release_bundle(
        bundle_path,
        expected_version=VERSION,
        expected_commit=COMMIT,
        expected_size=bundle_path.stat().st_size,
        expected_sha256=hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
    )

    assert bundle.version == VERSION
    assert bundle.commit == COMMIT
    assert bundle.wheel.name == f"tfr-{VERSION}-py3-none-any.whl"
    assert bundle.requirements.name == "requirements.txt"


def test_bundle_build_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    first = build(tmp_path, "first.zip")
    second = build(tmp_path, "second.zip")

    assert first.read_bytes() == second.read_bytes()


def test_bundle_rejects_manifest_digest_or_size_mismatch(tmp_path: Path) -> None:
    bundle = build(tmp_path)

    with pytest.raises(ReleaseBundleError, match="size does not match"):
        verify_release_bundle(bundle, expected_size=bundle.stat().st_size + 1)
    with pytest.raises(ReleaseBundleError, match="digest does not match"):
        verify_release_bundle(bundle, expected_sha256="0" * 64)


def test_bundle_verifies_the_same_bytes_used_for_outer_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build(tmp_path)
    substituted = b"not a ZIP archive"
    original_read_bytes = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        return substituted if path == bundle else original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(ReleaseBundleError, match="malformed"):
        verify_release_bundle(
            bundle,
            expected_size=len(substituted),
            expected_sha256=hashlib.sha256(substituted).hexdigest(),
        )


def test_bundle_rejects_tampered_member(tmp_path: Path) -> None:
    bundle = build(tmp_path)
    rewrite_bundle(
        bundle,
        lambda name, content: (
            name,
            content + b"tampered" if name == "requirements.txt" else content,
            0o100644,
        ),
    )

    with pytest.raises(ReleaseBundleError, match="size metadata|integrity check"):
        verify_release_bundle(bundle)


@pytest.mark.parametrize("unsafe_name", ("../escape", "directory/member"))
def test_bundle_rejects_path_traversal_and_nested_members(
    tmp_path: Path, unsafe_name: str
) -> None:
    bundle = build(tmp_path)
    rewrite_bundle(
        bundle,
        lambda name, content: (
            unsafe_name if name == "requirements.txt" else name,
            content,
            0o100644,
        ),
    )

    with pytest.raises(ReleaseBundleError, match="unsafe member"):
        verify_release_bundle(bundle)


def test_bundle_rejects_links(tmp_path: Path) -> None:
    bundle = build(tmp_path)
    rewrite_bundle(
        bundle,
        lambda name, content: (
            name,
            content,
            stat.S_IFLNK | 0o777 if name == "requirements.txt" else 0o100644,
        ),
    )

    with pytest.raises(ReleaseBundleError, match="unsafe member"):
        verify_release_bundle(bundle)


def test_bundle_rejects_unexpected_member(tmp_path: Path) -> None:
    bundle = build(tmp_path)
    with zipfile.ZipFile(bundle, "a") as archive:
        archive.writestr("unexpected.txt", "no")

    with pytest.raises(ReleaseBundleError, match="unexpected or missing"):
        verify_release_bundle(bundle)


def test_bundle_rejects_duplicate_members(tmp_path: Path) -> None:
    bundle = build(tmp_path)
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(bundle, "a") as archive,
    ):
        archive.writestr("requirements.txt", "duplicate")

    with pytest.raises(ReleaseBundleError, match="duplicate members"):
        verify_release_bundle(bundle)


def test_bundle_rejects_compressed_outer_members(tmp_path: Path) -> None:
    bundle = build(tmp_path)
    with zipfile.ZipFile(bundle) as archive:
        members = [(info.filename, archive.read(info.filename)) for info in archive.infolist()]
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)

    with pytest.raises(ReleaseBundleError, match="unsafe member"):
        verify_release_bundle(bundle)


def test_bundle_rejects_mismatched_wheel_identity(tmp_path: Path) -> None:
    wheel, requirements = create_inputs(tmp_path)
    wheel.write_bytes(wheel_bytes(commit="c" * 40))

    with pytest.raises(ReleaseBundleError, match="wheel commit identity"):
        build_release_bundle(
            wheel_path=wheel,
            requirements_path=requirements,
            version=VERSION,
            commit=COMMIT,
            output=tmp_path / "bundle.zip",
        )


def test_bundle_rejects_unlocked_requirements(tmp_path: Path) -> None:
    wheel, requirements = create_inputs(tmp_path)
    requirements.write_text("dependency==4.5.6\n", encoding="utf-8")

    with pytest.raises(ReleaseBundleError, match="not hash-locked"):
        build_release_bundle(
            wheel_path=wheel,
            requirements_path=requirements,
            version=VERSION,
            commit=COMMIT,
            output=tmp_path / "bundle.zip",
        )


def test_bundle_rejects_any_unhashed_requirement(tmp_path: Path) -> None:
    wheel, requirements = create_inputs(tmp_path)
    requirements.write_text(
        "first==1 --hash=sha256:" + "b" * 64 + "\nsecond==2\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseBundleError, match="not hash-locked"):
        build_release_bundle(
            wheel_path=wheel,
            requirements_path=requirements,
            version=VERSION,
            commit=COMMIT,
            output=tmp_path / "bundle.zip",
        )


def test_bundle_rejects_malformed_requirement_hash(tmp_path: Path) -> None:
    wheel, requirements = create_inputs(tmp_path)
    requirements.write_text(
        "dependency==4.5.6 --hash=sha256:not-a-digest\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseBundleError, match="not hash-locked"):
        build_release_bundle(
            wheel_path=wheel,
            requirements_path=requirements,
            version=VERSION,
            commit=COMMIT,
            output=tmp_path / "bundle.zip",
        )


def test_bundle_metadata_is_strict(tmp_path: Path) -> None:
    bundle = build(tmp_path)

    def add_field(name: str, content: bytes) -> tuple[str, bytes, int]:
        if name == "bundle.json":
            metadata = json.loads(content)
            metadata["unexpected"] = True
            content = json.dumps(metadata).encode()
        return name, content, 0o100644

    rewrite_bundle(bundle, add_field)

    with pytest.raises(ReleaseBundleError, match="metadata fields"):
        verify_release_bundle(bundle)


def test_bundle_rejects_duplicate_metadata_keys(tmp_path: Path) -> None:
    bundle = build(tmp_path)

    def duplicate_key(name: str, content: bytes) -> tuple[str, bytes, int]:
        if name == "bundle.json":
            content = content.replace(
                b'"schema_version": 1,',
                b'"schema_version": 1,"schema_version": 1,',
            )
        return name, content, 0o100644

    rewrite_bundle(bundle, duplicate_key)

    with pytest.raises(ReleaseBundleError, match="duplicate metadata"):
        verify_release_bundle(bundle)


def test_bundle_verifier_script_reports_verified_identity(tmp_path: Path) -> None:
    bundle = build(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_release_bundle.py",
            str(bundle),
            "--version",
            VERSION,
            "--commit",
            COMMIT,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["version"] == VERSION
    assert report["commit"] == COMMIT
    assert report["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
