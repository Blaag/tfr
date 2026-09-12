from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from tfr.config import (
    ConfigurationError,
    default_config_path,
    load_configuration,
    load_ui_configuration,
)
from tfr.gateway_transport import DEFAULT_GATEWAY_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tfr",
        description="Connect to and supervise human and LLM world sessions.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("ui", "gateway"),
        help="attach a terminal UI to a gateway or run the persistent gateway",
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('tfr')}")
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

    if args.replay is not None:
        if (
            args.check_config
            or args.mode is not None
            or args.socket is not None
            or network_arguments
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
