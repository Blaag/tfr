from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tfr.gateway_client import GatewayClient
from tfr.gateway_profiles import GatewayProfileStore
from tfr.gateway_transport import create_gateway_client_tls_context
from tfr.settings_ui import parse_setting_value


def build_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tfr config",
        description="Inspect and safely change Gateway-managed configuration.",
    )
    parser.add_argument("--socket", type=Path, help="local Gateway Unix socket")
    parser.add_argument("--gateway-profile", help="authenticated remote Gateway profile")
    parser.add_argument("--json", action="store_true", help="emit stable JSON output")
    commands = parser.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="list available settings")
    list_parser.add_argument("query", nargs="?", help="filter by key or metadata")
    get_parser = commands.add_parser("get", help="show one effective setting")
    get_parser.add_argument("key")
    get_parser.add_argument("--ui", help="show the value effective for a named UI")

    set_parser = commands.add_parser("set", help="preview or atomically set values")
    set_parser.add_argument(
        "assignments",
        nargs="+",
        help="KEY=VALUE entries, or one KEY VALUE pair",
    )
    set_parser.add_argument("--apply", action="store_true", help="persist the batch")
    _target_arguments(set_parser)

    reset_parser = commands.add_parser("reset", help="preview or reset a setting")
    reset_parser.add_argument("key")
    reset_parser.add_argument("--apply", action="store_true", help="persist the reset")
    _target_arguments(reset_parser)

    commands.add_parser("status", help="show configuration convergence status")
    changes = commands.add_parser("changes", help="show recent changes")
    changes.add_argument("--limit", type=int, default=50)
    undo = commands.add_parser("undo", help="preview or undo the latest batch")
    undo.add_argument("--apply", action="store_true", help="persist the undo")
    return parser


def _target_arguments(parser: argparse.ArgumentParser) -> None:
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--ui", help="apply to one named UI")
    targets.add_argument("--ui-type", help="apply to a UI type, normally terminal")
    targets.add_argument("--world", help="apply to one world")
    targets.add_argument("--shared", action="store_true", help="apply as a shared value")


def run_config_cli(argv: Sequence[str]) -> int:
    parser = build_config_parser()
    args = parser.parse_args(_normalize_global_options(argv))
    try:
        result = asyncio.run(_run(args))
    except (ConnectionError, OSError, RuntimeError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"tfr config: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    else:
        print(_human_output(result))
    return 0


def _normalize_global_options(argv: Sequence[str]) -> list[str]:
    """Allow connection/output options before or after the config subcommand."""

    values = list(argv)
    global_values: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--json":
            global_values.append(value)
            index += 1
            continue
        if value in {"--socket", "--gateway-profile"}:
            if index + 1 >= len(values):
                remaining.append(value)
                index += 1
                continue
            global_values.extend((value, values[index + 1]))
            index += 2
            continue
        remaining.append(value)
        index += 1
    return [*global_values, *remaining]


async def _connect(args: argparse.Namespace) -> GatewayClient:
    if args.gateway_profile:
        store = GatewayProfileStore()
        profile = store.get(args.gateway_profile)
        token = store.credentials.load(profile.credential_reference)
        return await GatewayClient.connect_tcp(
            profile.host,
            profile.port,
            auth_token=token,
            tls_context=create_gateway_client_tls_context(profile.tls_ca),
            server_hostname=profile.tls_server_name,
        )
    from tfr.gateway import default_gateway_socket

    return await GatewayClient.connect(args.socket or default_gateway_socket())


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    client = await _connect(args)
    client.start()
    try:
        if args.action == "list":
            return {"action": "list", "settings": await client.config_list(args.query)}
        if args.action == "get":
            return {
                "action": "get",
                "setting": await client.config_show(args.key, ui=args.ui),
            }
        if args.action == "status":
            return {"action": "status", "status": await client.config_status()}
        if args.action == "changes":
            if not 1 <= args.limit <= 1_000:
                raise ValueError("--limit must be between 1 and 1000")
            return {"action": "changes", **await client.config_changes(args.limit)}
        status = await client.config_status()
        revision = int(status["revision"])
        if args.action == "undo":
            result = await client.config_undo(
                expected_revision=revision, confirm=args.apply
            )
            return {"action": "undo", "applied": args.apply, **result}
        settings = {str(item["key"]): item for item in await client.config_list(None)}
        if args.action == "reset":
            setting = _known_setting(settings, args.key)
            scope, target = _destination(setting, args)
            changes = [
                {
                    "operation": "reset",
                    "key": args.key,
                    "scope": scope,
                    "target": target,
                }
            ]
        else:
            changes = []
            for key, raw in _assignments(args.assignments):
                setting = _known_setting(settings, key)
                value = parse_setting_value(raw, str(setting.get("type", "json")))
                scope, target = _destination(setting, args)
                changes.append(
                    {
                        "operation": "set",
                        "key": key,
                        "value": value,
                        "scope": scope,
                        "target": target,
                    }
                )
        result = await client.config_batch(
            changes, expected_revision=revision, confirm=args.apply
        )
        return {
            "action": args.action,
            "applied": args.apply,
            "base_revision": revision,
            **result,
            "changes": changes,
        }
    finally:
        await client.stop()


def _known_setting(
    settings: dict[str, dict[str, object]], key: str
) -> dict[str, object]:
    try:
        return settings[key]
    except KeyError as exc:
        raise ValueError(f"unknown setting: {key}; use `tfr config list` to inspect keys") from exc


def _assignments(values: list[str]) -> list[tuple[str, str]]:
    if len(values) == 2 and "=" not in values[0]:
        return [(values[0], values[1])]
    output = []
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key or not raw:
            raise ValueError("multiple settings must use KEY=VALUE assignments")
        output.append((key, raw))
    if len({key for key, _raw in output}) != len(output):
        raise ValueError("a setting can appear only once in an atomic batch")
    return output


def _destination(
    setting: dict[str, object], args: argparse.Namespace
) -> tuple[str, str | None]:
    key = str(setting["key"])
    if args.ui:
        return "ui", args.ui
    if args.ui_type:
        return "ui-type", args.ui_type
    if args.world:
        return "world", args.world
    if args.shared:
        return "shared", None
    scope = str(setting["scope"])
    if scope == "ui-type":
        return "ui-type", "terminal"
    if scope == "ui":
        raise ValueError(f"{key} requires --ui NAME")
    if scope == "world":
        parts = key.split(".", 2)
        if len(parts) < 3 or parts[1] == "*":
            raise ValueError(f"{key} requires --world NAME")
        return "world", parts[1]
    if scope == "plugin":
        parts = key.split(".", 3)
        return "plugin", parts[2] if len(parts) > 3 else None
    return scope, None


def _human_output(result: dict[str, Any]) -> str:
    action = result["action"]
    if action == "list":
        settings = result["settings"]
        return "\n".join(
            f"{item.get('key')}  [{item.get('type')}; {item.get('scope')}]"
            for item in settings
        ) or "No matching settings."
    if action == "get":
        item = result["setting"]
        return "\n".join(
            (
                f"{item.get('key')} = {json.dumps(item.get('value'), ensure_ascii=False)}",
                f"Source: {item.get('source')}",
                f"Required action: {item.get('restart')}",
            )
        )
    if action in {"set", "reset"}:
        state = "Applied" if result.get("applied") else "Preview only; add --apply to save"
        lines = [state, f"Required action: {result.get('restart', 'live')}"]
        for change in result.get("changes", []):
            value = (
                json.dumps(change.get("value"), ensure_ascii=False)
                if change.get("operation") == "set"
                else "inherited/default"
            )
            target = f" · {change.get('target')}" if change.get("target") else ""
            lines.append(f"  {change.get('key')} = {value} [{change.get('scope')}{target}]")
        if result.get("applied"):
            lines.append(f"Revision: {result.get('revision')}")
        return "\n".join(lines)
    return json.dumps({key: value for key, value in result.items() if key != "action"}, indent=2)
