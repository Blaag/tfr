from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from tfr.gateway import default_gateway_socket
from tfr.gateway_protocol import (
    MAX_MESSAGE_BYTES,
    MAX_SNAPSHOT_EVENTS,
    GatewayProtocolError,
    read_message,
    write_message,
)


async def _admin_request(
    path: Path | str | None,
    message_type: str,
    **values: Any,
) -> dict[str, Any]:
    socket_path = Path(path).expanduser() if path is not None else default_gateway_socket()
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(socket_path),
            limit=MAX_MESSAGE_BYTES,
        )
    except (ConnectionError, OSError) as exc:
        raise ConnectionError(f"cannot connect to Gateway at {socket_path}") from exc
    try:
        await write_message(
            writer,
            {
                "type": "hello",
                "client_id": str(uuid4()),
                "gateway_id": None,
                "after_cursor": None,
                "admin": True,
            },
        )
        hello = await asyncio.wait_for(read_message(reader), timeout=10)
        if hello is None or hello.get("type") != "hello":
            raise GatewayProtocolError("Gateway did not accept the pairing connection")
        snapshot_count = hello.get("snapshot_count")
        if (
            not isinstance(snapshot_count, int)
            or isinstance(snapshot_count, bool)
            or not 0 <= snapshot_count <= MAX_SNAPSHOT_EVENTS
        ):
            raise GatewayProtocolError("Gateway returned an invalid snapshot count")
        for _ in range(snapshot_count):
            if await read_message(reader) is None:
                raise GatewayProtocolError("Gateway disconnected during pairing")

        request_id = uuid4()
        await write_message(
            writer,
            {
                "type": message_type,
                "request_id": str(request_id),
                **values,
            },
        )
        while True:
            message = await asyncio.wait_for(read_message(reader), timeout=10)
            if message is None:
                raise GatewayProtocolError("Gateway disconnected during pairing")
            if message.get("type") == "error":
                raise GatewayProtocolError(str(message.get("message", "Gateway pairing failed")))
            if message.get("type") != "ack" or message.get("request_id") != str(request_id):
                continue
            if message.get("ok") is not True:
                raise ValueError(str(message.get("error", "Gateway pairing failed")))
            result = message.get("result")
            if not isinstance(result, dict):
                raise GatewayProtocolError("Gateway returned an invalid administration result")
            return result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def create_device_pairing_url(
    path: Path | str | None,
    label: str,
) -> str:
    result = await _admin_request(path, "pair_device", label=label)
    pairing_url = result.get("pairing_url")
    if not isinstance(pairing_url, str):
        raise GatewayProtocolError("Gateway returned an invalid pairing URL")
    return pairing_url


async def list_web_devices(path: Path | str | None) -> list[dict[str, Any]]:
    result = await _admin_request(path, "list_devices")
    devices = result.get("devices")
    if not isinstance(devices, list) or not all(isinstance(item, dict) for item in devices):
        raise GatewayProtocolError("Gateway returned an invalid device list")
    return devices


async def revoke_web_device(path: Path | str | None, device_id: UUID) -> None:
    await _admin_request(path, "revoke_device", device_id=str(device_id))
