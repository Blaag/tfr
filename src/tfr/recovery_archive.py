from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import zipfile
from pathlib import Path

FORMAT = "tfr-complete-gateway-recovery"
SCHEMA_VERSION = 2
CONFIGURATION_NAME = "configuration.json"
MANIFEST_NAME = "manifest.json"
MAX_CONFIGURATION_BYTES = 4 * 1024 * 1024
MAX_LOG_FILES = 100_000
MAX_LOG_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_LOG_BYTES = 16 * 1024 * 1024 * 1024
_CHUNK = 1024 * 1024


def is_complete_recovery_archive(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def write_complete_recovery_archive(
    path: Path, configuration: dict[str, object], log_directory: Path
) -> dict[str, int]:
    path = path.expanduser()
    log_directory = log_directory.expanduser()
    encoded = json.dumps(
        configuration, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_CONFIGURATION_BYTES:
        raise ValueError("configuration export exceeds 4 MiB")
    paths = _log_paths(log_directory)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    total = 0
    entries: list[dict[str, object]] = []
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr(CONFIGURATION_NAME, encoded)
            for source in paths:
                source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                details = os.fstat(source_descriptor)
                size = details.st_size
                if not stat.S_ISREG(details.st_mode):
                    os.close(source_descriptor)
                    raise ValueError(f"recovery log must be a regular file: {source.name}")
                if size > MAX_LOG_FILE_BYTES or total + size > MAX_TOTAL_LOG_BYTES:
                    os.close(source_descriptor)
                    raise ValueError("recovery logs exceed the bounded archive size")
                digest = hashlib.sha256()
                remaining = size
                last = b""
                with (
                    os.fdopen(source_descriptor, "rb") as input_stream,
                    archive.open(f"logs/{source.name}", "w") as output_stream,
                ):
                    while remaining:
                        chunk = input_stream.read(min(_CHUNK, remaining))
                        if not chunk:
                            raise ValueError(f"recovery log changed during export: {source.name}")
                        output_stream.write(chunk)
                        digest.update(chunk)
                        last = chunk[-1:]
                        remaining -= len(chunk)
                if size and last != b"\n":
                    raise ValueError(f"recovery log has an incomplete final record: {source.name}")
                total += size
                entries.append({"name": source.name, "size": size, "sha256": digest.hexdigest()})
            manifest = {
                "format": FORMAT,
                "schema_version": SCHEMA_VERSION,
                "log_directory": _portable_path(log_directory),
                "logs": entries,
            }
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return {"files": len(entries), "bytes": total}


def read_complete_recovery_configuration(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        _validated_manifest(archive)
        data = _read_bounded(archive, CONFIGURATION_NAME, MAX_CONFIGURATION_BYTES)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("complete recovery configuration is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("complete recovery configuration must be an object")
    return value


def restore_complete_recovery_logs(path: Path) -> dict[str, int]:
    if not is_complete_recovery_archive(path):
        return {"files": 0, "bytes": 0}
    with zipfile.ZipFile(path) as archive:
        manifest = _validated_manifest(archive)
        target = Path(str(manifest["log_directory"])).expanduser()
        resolved_home = Path.home().resolve()
        resolved_target = target.resolve(strict=False)
        try:
            resolved_target.relative_to(resolved_home)
        except ValueError as exc:
            raise ValueError("recovery logging directory escapes the owner's home") from exc
        if target.is_symlink():
            raise ValueError("recovery logging directory cannot be a symbolic link")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = target.stat(follow_symlinks=False)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("recovery logging destination is not a directory")
        if os.name == "posix":
            os.chmod(target, 0o700)
        restored = 0
        total = 0
        for entry in manifest["logs"]:
            name = str(entry["name"])
            destination = target / name
            expected = str(entry["sha256"])
            if destination.exists() or destination.is_symlink():
                if not destination.is_file() or destination.is_symlink():
                    raise ValueError(f"recovery log destination is unsafe: {name}")
                if _file_digest(destination) != expected:
                    raise ValueError(f"recovery log conflicts with an existing file: {name}")
                continue
            temporary = target / f".{name}.{secrets.token_hex(8)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(descriptor, "wb") as output, archive.open(f"logs/{name}") as source:
                    descriptor = -1
                    while chunk := source.read(_CHUNK):
                        size += len(chunk)
                        if size > int(entry["size"]):
                            raise ValueError(f"recovery log exceeds its declared size: {name}")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != entry["size"] or digest.hexdigest() != expected:
                    raise ValueError(f"recovery log failed integrity validation: {name}")
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
            restored += 1
            total += size
    return {"files": restored, "bytes": total}


def _log_paths(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Gateway logging directory is unavailable: {directory}")
    paths = tuple(sorted(directory.glob("tfr-*.jsonl"), key=lambda item: item.name))
    if len(paths) > MAX_LOG_FILES:
        raise ValueError("recovery log file count exceeds the archive limit")
    if len({path.name for path in paths}) != len(paths):  # pragma: no cover
        raise ValueError("recovery log names are not unique")
    return paths


def _validated_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if (
        len(names) != len(set(names))
        or CONFIGURATION_NAME not in names
        or MANIFEST_NAME not in names
    ):
        raise ValueError("complete recovery archive has invalid members")
    manifest_data = _read_bounded(archive, MANIFEST_NAME, MAX_CONFIGURATION_BYTES)
    try:
        manifest = json.loads(manifest_data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("complete recovery manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "schema_version", "log_directory", "logs"}
        or manifest.get("format") != FORMAT
        or manifest.get("schema_version") != SCHEMA_VERSION
        or not isinstance(manifest.get("log_directory"), str)
        or not str(manifest["log_directory"])
        or not isinstance(manifest.get("logs"), list)
        or len(manifest["logs"]) > MAX_LOG_FILES
    ):
        raise ValueError("complete recovery manifest is unsupported")
    configured_directory = str(manifest["log_directory"])
    if not configured_directory.startswith("~/"):
        raise ValueError("complete recovery logging directory is unsafe")
    expected_names = {CONFIGURATION_NAME, MANIFEST_NAME}
    total = 0
    for entry in manifest["logs"]:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise ValueError("complete recovery log manifest is invalid")
        name, size, digest = entry["name"], entry["size"], entry["sha256"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.startswith("tfr-")
            or not name.endswith(".jsonl")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_LOG_FILE_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("complete recovery log manifest is invalid")
        total += size
        if total > MAX_TOTAL_LOG_BYTES:
            raise ValueError("complete recovery logs exceed the archive limit")
        expected_names.add(f"logs/{name}")
    if set(names) != expected_names:
        raise ValueError("complete recovery archive has undeclared members")
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError("complete recovery archive contains a symbolic link")
    return manifest


def _read_bounded(archive: zipfile.ZipFile, name: str, maximum: int) -> bytes:
    with archive.open(name) as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f"complete recovery member exceeds its size limit: {name}")
    return data


def _portable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.home().resolve())
    except ValueError as exc:
        raise ValueError("Gateway logging directory must be inside the owner's home") from exc
    return str(Path("~") / relative)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
