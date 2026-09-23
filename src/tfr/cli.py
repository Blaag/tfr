from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from tfr.config import (
    ConfigurationError,
    default_config_path,
    load_configuration,
    load_ui_configuration,
)
from tfr.gateway_transport import DEFAULT_GATEWAY_PORT
from tfr.updates import current_build


def build_parser() -> argparse.ArgumentParser:
    build = current_build()
    build_version = (
        f"{build.version} ({build.commit[:12]})" if build.commit is not None else build.version
    )
    parser = argparse.ArgumentParser(
        prog="tfr",
        description="Connect to and supervise human and LLM world sessions.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("ui", "gateway", "pair", "devices", "revoke-device"),
        help="run a client, the Gateway, or local web-device administration",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="main JSONC configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit",
    )
    parser.add_argument(
        "--rollback-plugin",
        metavar="REPO[:PATH]",
        help="activate the previous verified stable release for a configured plugin source",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        help="open a JSONL event transcript without connecting",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        help="gateway Unix socket path",
    )
    parser.add_argument(
        "--device-name",
        help="pair mode: label for the mobile device",
    )
    parser.add_argument(
        "--device-id",
        help="revoke-device mode: paired device UUID",
    )
    parser.add_argument(
        "--listen-host",
        help="gateway mode: also listen for authenticated TLS clients on this host",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_GATEWAY_PORT,
        help="gateway mode: network listener port (default: %(default)s)",
    )
    parser.add_argument(
        "--gateway-host",
        help="UI mode: connect to an authenticated TLS gateway at this host",
    )
    parser.add_argument(
        "--gateway-port",
        type=int,
        default=DEFAULT_GATEWAY_PORT,
        help="UI mode: remote gateway port (default: %(default)s)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="private authentication token file for a network gateway",
    )
    parser.add_argument("--tls-cert", type=Path, help="gateway mode: TLS certificate chain")
    parser.add_argument("--tls-key", type=Path, help="gateway mode: private TLS key")
    parser.add_argument("--tls-ca", type=Path, help="UI mode: custom CA for gateway TLS")
    parser.add_argument(
        "--tls-server-name",
        help="UI mode: TLS certificate hostname (defaults to --gateway-host)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {build_version}")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    network_arguments = (
        any(
            value is not None
            for value in (
                args.listen_host,
                args.gateway_host,
                args.token_file,
                args.tls_cert,
                args.tls_key,
                args.tls_ca,
                args.tls_server_name,
            )
        )
        or args.listen_port != DEFAULT_GATEWAY_PORT
        or args.gateway_port != DEFAULT_GATEWAY_PORT
    )

    if args.rollback_plugin is not None:
        if (
            args.mode is not None
            or args.check_config
            or args.replay is not None
            or args.socket is not None
            or network_arguments
            or args.device_name is not None
            or args.device_id is not None
        ):
            print(
                "tfr: --rollback-plugin cannot be combined with runtime or transport options",
                file=sys.stderr,
            )
            return 2
        try:
            bundle = load_configuration(args.config)
            from tfr.plugin_sources import plugin_source_id

            exact_matches = [
                source
                for source in bundle.main.plugins.sources
                if plugin_source_id(source.repo, source.path) == args.rollback_plugin
            ]
            matches = exact_matches or [
                source
                for source in bundle.main.plugins.sources
                if source.repo == args.rollback_plugin
            ]
            if len(matches) != 1:
                raise ValueError(
                    "--rollback-plugin must name exactly one configured plugin source"
                )
            source = matches[0]
            if source.policy not in {"stable-auto", "stable-notify"}:
                raise ValueError("plugin rollback requires a stable plugin source policy")
            from tfr.plugin_releases import (
                PluginReleaseLayout,
                current_plugin_release,
                rollback_plugin_release,
            )
            from tfr.plugin_sources import normalize_repo_url, stable_source_slug

            repo_url = normalize_repo_url(source.repo)
            layout = PluginReleaseLayout(
                (
                    bundle.main.plugins.state_directory.expanduser()
                    / "managed"
                    / stable_source_slug(source)
                ).resolve()
            )
            manifest_url = str(source.manifest_url)
            rollback_plugin_release(
                layout,
                repo_url=repo_url,
                manifest_url=manifest_url,
                source_path=source.path,
            )
            current = current_plugin_release(
                layout,
                repo_url=repo_url,
                manifest_url=manifest_url,
                source_path=source.path,
            )
            if current is None:  # pragma: no cover - rollback guarantees a current release
                raise ValueError("plugin rollback did not activate a release")
            print(
                f"Rolled back {source.repo} to {current[1].version} at {current[1].commit}."
            )
            if source.policy == "stable-auto":
                print(
                    "Set this source to stable-notify before the next launch to hold the rollback.",
                    file=sys.stderr,
                )
            return 0
        except (ConfigurationError, OSError, RuntimeError, ValueError) as exc:
            print(f"tfr: plugin rollback error: {exc}", file=sys.stderr)
            return 2

    if args.replay is not None:
        if (
            args.check_config
            or args.mode is not None
            or args.socket is not None
            or network_arguments
            or args.device_name is not None
            or args.device_id is not None
        ):
            print(
                "tfr: --replay cannot be combined with gateway mode, --socket, or --check-config",
                file=sys.stderr,
            )
            return 2
        from tfr.replay import ReplayError, run_replay

        try:
            return asyncio.run(run_replay(args.replay))
        except ReplayError as exc:
            print(f"tfr: replay error: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130

    if args.mode in {"pair", "devices", "revoke-device"}:
        expected_device_name = args.mode == "pair"
        expected_device_id = args.mode == "revoke-device"
        if (
            args.check_config
            or network_arguments
            or (args.device_name is not None) != expected_device_name
            or (args.device_id is not None) != expected_device_id
        ):
            print(
                "tfr: web device administration accepts only --socket plus its required "
                "device option",
                file=sys.stderr,
            )
            return 2
        try:
            from tfr.gateway_admin import (
                create_device_pairing_url,
                list_web_devices,
                revoke_web_device,
            )

            if args.mode == "pair":
                print(asyncio.run(create_device_pairing_url(args.socket, args.device_name)))
            elif args.mode == "devices":
                devices = asyncio.run(list_web_devices(args.socket))
                if not devices:
                    print("No paired web devices.")
                for device in devices:
                    worlds = ",".join(str(world) for world in device["allowed_worlds"])
                    print(
                        f"{device['device_id']}  {device['label']}  "
                        f"{device['tailscale_login']}  {device['scope']}  {worlds}  "
                        f"expires {device['expires_at']}"
                    )
            else:
                try:
                    device_id = UUID(args.device_id)
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("--device-id must be a UUID") from None
                asyncio.run(revoke_web_device(args.socket, device_id))
                print(f"Revoked web device {device_id}.")
            return 0
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            print(f"tfr: pairing error: {exc}", file=sys.stderr)
            return 2

    if args.device_name is not None or args.device_id is not None:
        print("tfr: web device options require a device administration mode", file=sys.stderr)
        return 2

    if args.check_config and (args.socket is not None or network_arguments):
        print("tfr: transport options cannot be combined with --check-config", file=sys.stderr)
        return 2

    if not args.check_config:
        if args.mode == "ui":
            if (
                args.listen_host is not None
                or args.listen_port != DEFAULT_GATEWAY_PORT
                or args.tls_cert is not None
                or args.tls_key is not None
            ):
                print(
                    "tfr: --listen-host, --tls-cert, and --tls-key require gateway mode",
                    file=sys.stderr,
                )
                return 2
            if args.gateway_host is not None and args.socket is not None:
                print(
                    "tfr: UI mode accepts either --socket or --gateway-host, not both",
                    file=sys.stderr,
                )
                return 2
            if args.gateway_host is None and (
                args.gateway_port != DEFAULT_GATEWAY_PORT
                or any(
                    value is not None
                    for value in (args.token_file, args.tls_ca, args.tls_server_name)
                )
            ):
                print(
                    "tfr: --token-file and UI TLS options require --gateway-host", file=sys.stderr
                )
                return 2
            if args.gateway_host is not None and args.token_file is None:
                print("tfr: --gateway-host requires --token-file", file=sys.stderr)
                return 2
        elif args.mode == "gateway":
            if (
                args.gateway_host is not None
                or args.gateway_port != DEFAULT_GATEWAY_PORT
                or args.tls_ca is not None
                or args.tls_server_name is not None
            ):
                print("tfr: --gateway-host and UI TLS options require ui mode", file=sys.stderr)
                return 2
            if args.listen_host is None and (
                args.listen_port != DEFAULT_GATEWAY_PORT
                or any(
                    value is not None for value in (args.token_file, args.tls_cert, args.tls_key)
                )
            ):
                print("tfr: network gateway options require --listen-host", file=sys.stderr)
                return 2
            if args.listen_host is not None and any(
                value is None for value in (args.token_file, args.tls_cert, args.tls_key)
            ):
                print(
                    "tfr: --listen-host requires --token-file, --tls-cert, and --tls-key",
                    file=sys.stderr,
                )
                return 2
        elif network_arguments:
            print("tfr: network gateway options require gateway or ui mode", file=sys.stderr)
            return 2

    if args.mode == "ui" and not args.check_config:
        try:
            configuration = load_ui_configuration(args.config)
            from tfr.gateway_client import run_gateway_ui

            if args.gateway_host is None:
                return asyncio.run(run_gateway_ui(configuration, args.socket))
            return asyncio.run(
                run_gateway_ui(
                    configuration,
                    gateway_host=args.gateway_host,
                    gateway_port=args.gateway_port,
                    token_file=args.token_file,
                    tls_ca=args.tls_ca,
                    tls_server_name=args.tls_server_name,
                )
            )
        except ConfigurationError as exc:
            print(f"tfr: configuration error: {exc}", file=sys.stderr)
            return 2
        except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
            print(f"tfr: gateway error: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130

    try:
        bundle = load_configuration(args.config)
    except ConfigurationError as exc:
        print(f"tfr: configuration error: {exc}", file=sys.stderr)
        return 2

    for warning in bundle.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.check_config:
        print(
            "Configuration valid: "
            f"{len(bundle.worlds.worlds)} world(s), {len(bundle.agents.agents)} agent(s)."
        )
        return 0
    if args.socket is not None and args.mode not in {"gateway", "ui"}:
        print("tfr: --socket requires gateway or ui mode", file=sys.stderr)
        return 2
    if not bundle.worlds.worlds:
        print("tfr: configuration error: no worlds are configured", file=sys.stderr)
        return 2

    try:
        if args.mode == "gateway":
            from tfr.gateway import run_gateway

            if args.listen_host is None:
                return asyncio.run(run_gateway(bundle, args.socket))
            return asyncio.run(
                run_gateway(
                    bundle,
                    args.socket,
                    listen_host=args.listen_host,
                    listen_port=args.listen_port,
                    token_file=args.token_file,
                    tls_certificate=args.tls_cert,
                    tls_private_key=args.tls_key,
                )
            )
        from tfr.tui import run_client

        return asyncio.run(run_client(bundle))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"tfr: gateway error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def main() -> None:
    raise SystemExit(run())
