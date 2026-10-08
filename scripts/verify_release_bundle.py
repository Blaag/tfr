from __future__ import annotations

import argparse
import json
from pathlib import Path

from tfr.release_bundle import ReleaseBundleError, verify_release_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a TFR release bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--size", type=int)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        bundle = verify_release_bundle(
            args.bundle,
            expected_version=args.version,
            expected_commit=args.commit,
            expected_size=args.size,
            expected_sha256=args.sha256,
        )
    except ReleaseBundleError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "version": bundle.version,
                "commit": bundle.commit,
                "wheel": bundle.wheel.name,
                "requirements": bundle.requirements.name,
                "size": bundle.bundle_size,
                "sha256": bundle.bundle_sha256,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
