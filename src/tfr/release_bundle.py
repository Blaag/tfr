from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAXIMUM_BUNDLE_BYTES = 256 * 1024 * 1024
_MAXIMUM_REQUIREMENTS_BYTES = 4 * 1024 * 1024
_MAXIMUM_WHEEL_BYTES = 128 * 1024 * 1024
_MAXIMUM_WHEEL_EXPANDED_BYTES = 256 * 1024 * 1024
_MAXIMUM_METADATA_BYTES = 64 * 1024
_BUNDLE_METADATA = "bundle.json"
_REQUIREMENTS = "requirements.txt"


class ReleaseBundleError(ValueError):
    """Raised when a release bundle is malformed or fails integrity checks."""


@dataclass(frozen=True, slots=True)
class BundleMember:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedReleaseBundle:
    version: str
    commit: str
    wheel: BundleMember
    requirements: BundleMember
    bundle_size: int
    bundle_sha256: str


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _member(value: object, field: str) -> BundleMember:
    if not isinstance(value, dict) or set(value) != {"name", "size", "sha256"}:
        raise ReleaseBundleError(f"{field} metadata is invalid")
    name = value["name"]
    size = value["size"]
    digest = value["sha256"]
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ReleaseBundleError(f"{field} name is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ReleaseBundleError(f"{field} size is invalid")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ReleaseBundleError(f"{field} digest is invalid")
    return BundleMember(name=name, size=size, sha256=digest)


def _verify_wheel(content: bytes, *, member: BundleMember, version: str, commit: str) -> None:
    expected_name = f"tfr-{version}-py3-none-any.whl"
    if member.name != expected_name:
        raise ReleaseBundleError(f"wheel must be named {expected_name}")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as wheel:
            infos = wheel.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ReleaseBundleError("wheel contains duplicate members")
            if any(
                info.flag_bits & 0x1
                or Path(info.filename).is_absolute()
                or ".." in Path(info.filename).parts
                or stat.S_ISLNK(info.external_attr >> 16)
                for info in infos
            ):
                raise ReleaseBundleError("wheel contains an unsafe member")
            if sum(info.file_size for info in infos) > _MAXIMUM_WHEEL_EXPANDED_BYTES:
                raise ReleaseBundleError("wheel expanded size exceeds the limit")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1 or "tfr/_build.py" not in names:
                raise ReleaseBundleError("wheel identity files are missing or ambiguous")
            metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
            build = wheel.read("tfr/_build.py").decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseBundleError("wheel is malformed") from exc
    if metadata.get("Name") != "tfr" or metadata.get("Version") != version:
        raise ReleaseBundleError("wheel project identity does not match the bundle")
    if re.search(rf'^BUILD_COMMIT(?::[^=]+)?\s*=\s*"{re.escape(commit)}"\s*$', build, re.M) is None:
        raise ReleaseBundleError("wheel commit identity does not match the bundle")


def _verify_requirements(content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseBundleError("requirements are not valid UTF-8") from exc
    statements: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        statements.append(pending)
        pending = ""
    if pending:
        raise ReleaseBundleError("requirements end with an incomplete continuation")
    valid_hash = re.compile(r"(?<!\S)--hash=sha256:[0-9a-f]{64}(?=\s|$)")
    if not statements or any(
        statement.startswith(("-e ", "--editable "))
        or valid_hash.search(statement) is None
        or "--hash=" in valid_hash.sub("", statement)
        for statement in statements
    ):
        raise ReleaseBundleError("requirements are not hash-locked")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseBundleError(f"duplicate metadata key: {key}")
        result[key] = value
    return result


def build_release_bundle(
    *,
    wheel_path: Path,
    requirements_path: Path,
    version: str,
    commit: str,
    output: Path,
) -> VerifiedReleaseBundle:
    if _VERSION.fullmatch(version) is None:
        raise ReleaseBundleError("bundle version must be stable semantic version")
    commit = commit.casefold()
    if _COMMIT.fullmatch(commit) is None:
        raise ReleaseBundleError("bundle commit must be a full lowercase Git commit")
    try:
        wheel_content = wheel_path.read_bytes()
        requirements_content = requirements_path.read_bytes()
    except OSError as exc:
        raise ReleaseBundleError(f"cannot read release input: {exc}") from exc
    if not wheel_content or len(wheel_content) > _MAXIMUM_WHEEL_BYTES:
        raise ReleaseBundleError("wheel size is invalid")
    if not requirements_content or len(requirements_content) > _MAXIMUM_REQUIREMENTS_BYTES:
        raise ReleaseBundleError("requirements size is invalid")
    _verify_requirements(requirements_content)
    wheel = BundleMember(wheel_path.name, len(wheel_content), _sha256(wheel_content))
    requirements = BundleMember(
        _REQUIREMENTS,
        len(requirements_content),
        _sha256(requirements_content),
    )
    _verify_wheel(wheel_content, member=wheel, version=version, commit=commit)
    metadata = json.dumps(
        {
            "schema_version": 1,
            "project": "tfr",
            "version": version,
            "commit": commit,
            "wheel": {"name": wheel.name, "size": wheel.size, "sha256": wheel.sha256},
            "requirements": {
                "name": requirements.name,
                "size": requirements.size,
                "sha256": requirements.sha256,
            },
        },
        indent=2,
        sort_keys=True,
    ).encode() + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in (
            (_BUNDLE_METADATA, metadata),
            (_REQUIREMENTS, requirements_content),
            (wheel.name, wheel_content),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, content)
    return verify_release_bundle(output, expected_version=version, expected_commit=commit)


def verify_release_bundle(
    path: Path,
    *,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> VerifiedReleaseBundle:
    try:
        bundle_content = path.read_bytes()
    except OSError as exc:
        raise ReleaseBundleError(f"cannot read release bundle: {exc}") from exc
    if not bundle_content or len(bundle_content) > _MAXIMUM_BUNDLE_BYTES:
        raise ReleaseBundleError("release bundle size is invalid")
    bundle_digest = _sha256(bundle_content)
    if expected_size is not None and len(bundle_content) != expected_size:
        raise ReleaseBundleError("release bundle size does not match the manifest")
    if expected_sha256 is not None and bundle_digest != expected_sha256:
        raise ReleaseBundleError("release bundle digest does not match the manifest")
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_content)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ReleaseBundleError("release bundle contains duplicate members")
            if any(
                info.is_dir()
                or Path(info.filename).name != info.filename
                or info.flag_bits & 0x1
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.compress_type != zipfile.ZIP_STORED
                for info in infos
            ):
                raise ReleaseBundleError("release bundle contains an unsafe member")
            if _BUNDLE_METADATA not in names:
                raise ReleaseBundleError("release bundle metadata is missing")
            info_by_name = {info.filename: info for info in infos}
            if info_by_name[_BUNDLE_METADATA].file_size > _MAXIMUM_METADATA_BYTES:
                raise ReleaseBundleError("release bundle metadata exceeds the size limit")
            metadata_content = archive.read(_BUNDLE_METADATA)
            try:
                metadata: Any = json.loads(
                    metadata_content,
                    object_pairs_hook=_strict_json_object,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReleaseBundleError("release bundle metadata is invalid") from exc
            if not isinstance(metadata, dict) or set(metadata) != {
                "schema_version",
                "project",
                "version",
                "commit",
                "wheel",
                "requirements",
            }:
                raise ReleaseBundleError("release bundle metadata fields are invalid")
            if metadata["schema_version"] != 1 or metadata["project"] != "tfr":
                raise ReleaseBundleError("release bundle identity is invalid")
            version = metadata["version"]
            commit = metadata["commit"]
            if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
                raise ReleaseBundleError("release bundle version is invalid")
            if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
                raise ReleaseBundleError("release bundle commit is invalid")
            wheel = _member(metadata["wheel"], "wheel")
            requirements = _member(metadata["requirements"], "requirements")
            if requirements.name != _REQUIREMENTS:
                raise ReleaseBundleError("requirements filename is invalid")
            if set(names) != {_BUNDLE_METADATA, wheel.name, requirements.name}:
                raise ReleaseBundleError("release bundle contains unexpected or missing members")
            if wheel.size > _MAXIMUM_WHEEL_BYTES or requirements.size > _MAXIMUM_REQUIREMENTS_BYTES:
                raise ReleaseBundleError("release bundle member exceeds its size limit")
            if (
                info_by_name[wheel.name].file_size != wheel.size
                or info_by_name[requirements.name].file_size != requirements.size
            ):
                raise ReleaseBundleError("release bundle member size metadata is inconsistent")
            wheel_content = archive.read(wheel.name)
            requirements_content = archive.read(requirements.name)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseBundleError("release bundle is malformed") from exc
    for member, content in ((wheel, wheel_content), (requirements, requirements_content)):
        if len(content) != member.size or _sha256(content) != member.sha256:
            raise ReleaseBundleError(f"{member.name} integrity check failed")
    _verify_requirements(requirements_content)
    if expected_version is not None and version != expected_version:
        raise ReleaseBundleError("release bundle version does not match the expected version")
    if expected_commit is not None and commit != expected_commit.casefold():
        raise ReleaseBundleError("release bundle commit does not match the expected commit")
    _verify_wheel(wheel_content, member=wheel, version=version, commit=commit)
    return VerifiedReleaseBundle(
        version=version,
        commit=commit,
        wheel=wheel,
        requirements=requirements,
        bundle_size=len(bundle_content),
        bundle_sha256=bundle_digest,
    )
