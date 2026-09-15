from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import quote

from tfr.gateway_protocol import PROTOCOL_VERSION


def build_manifest(
    *,
    repository: str,
    tag: str,
    commit: str,
    artifact: Path,
) -> dict[str, object]:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if (
        not isinstance(version, str)
        or re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None
    ):
        raise ValueError("pyproject.toml must contain a stable semantic version")
    if tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match project version {version!r}")
    commit = commit.casefold()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("commit must be a full hexadecimal Git commit")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPOSITORY")
    if not artifact.is_file():
        raise ValueError(f"artifact does not exist: {artifact}")

    artifact_content = artifact.read_bytes()
    release_base = f"https://github.com/{repository}/releases"
    return {
        "schema_version": 1,
        "project": "tfr",
        "channel": "stable",
        "version": version,
        "tag": tag,
        "commit": commit,
        "protocol": {"minimum": PROTOCOL_VERSION, "maximum": PROTOCOL_VERSION},
        "release_url": f"{release_base}/tag/{quote(tag, safe='')}",
        "artifact": {
            "url": (
                f"{release_base}/download/{quote(tag, safe='')}/{quote(artifact.name, safe='')}"
            ),
            "size": len(artifact_content),
            "sha256": hashlib.sha256(artifact_content).hexdigest(),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the stable TFR update manifest.")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("update-manifest.json"))
    args = parser.parse_args()
    try:
        manifest = build_manifest(
            repository=args.repository,
            tag=args.tag,
            commit=args.commit,
            artifact=args.artifact,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
