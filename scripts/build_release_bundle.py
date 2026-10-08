from __future__ import annotations

import argparse
import json
from pathlib import Path

from tfr.release_bundle import ReleaseBundleError, build_release_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a verified TFR release bundle.")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        bundle = build_release_bundle(
            wheel_path=args.wheel,
            requirements_path=args.requirements,
            version=args.version,
            commit=args.commit,
            output=args.output,
        )
    except ReleaseBundleError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "path": str(args.output),
                "size": bundle.bundle_size,
                "sha256": bundle.bundle_sha256,
                "version": bundle.version,
                "commit": bundle.commit,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
