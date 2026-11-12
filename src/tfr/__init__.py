from tfr.release_leases import acquire_process_release_lease

acquire_process_release_lease(__file__)


def main() -> None:
    import logging
    import sys

    from tfr.cli import main as cli_main
    from tfr.installations import InstallationError, prune_managed_releases
    from tfr.release_leases import ReleaseLeaseError

    release_status_only = len(sys.argv) >= 3 and sys.argv[1] == "releases" and sys.argv[2] in {
        "status",
        "list",
    }
    if not release_status_only:
        try:
            prune_managed_releases()
        except (InstallationError, OSError, ReleaseLeaseError) as exc:
            logging.getLogger(__name__).warning("TFR release cleanup was skipped: %s", exc)

    cli_main()


__all__ = ["main"]
