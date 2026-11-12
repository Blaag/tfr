from tfr.release_leases import acquire_process_release_lease

acquire_process_release_lease(__file__)


def main() -> None:
    import logging

    from tfr.cli import main as cli_main
    from tfr.installations import InstallationError, prune_managed_releases
    from tfr.release_leases import ReleaseLeaseError

    try:
        prune_managed_releases()
    except (InstallationError, OSError, ReleaseLeaseError) as exc:
        logging.getLogger(__name__).warning("TFR release cleanup was skipped: %s", exc)

    cli_main()


__all__ = ["main"]
