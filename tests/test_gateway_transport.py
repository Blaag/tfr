from __future__ import annotations

import os
from pathlib import Path

import pytest

from tfr.gateway_transport import load_gateway_token, validate_tcp_endpoint


def test_gateway_token_file_must_be_private_and_long_enough(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway.token"
    token_file.write_text("a" * 64 + "\n", encoding="utf-8")
    token_file.chmod(0o600)

    assert load_gateway_token(token_file) == "a" * 64

    if os.name == "posix":
        token_file.chmod(0o644)
        with pytest.raises(ValueError, match="group or other"):
            load_gateway_token(token_file)

    token_file.chmod(0o600)
    token_file.write_text("short", encoding="utf-8")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        load_gateway_token(token_file)


def test_gateway_token_file_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target.token"
    target.write_text("b" * 64, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "gateway.token"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="cannot open"):
        load_gateway_token(link)


def test_gateway_token_rejects_non_ascii_text(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway.token"
    token_file.write_text("é" * 32, encoding="utf-8")
    token_file.chmod(0o600)

    with pytest.raises(ValueError, match="ASCII"):
        load_gateway_token(token_file)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "  "])
def test_gateway_tcp_endpoint_rejects_wildcard_or_empty_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        validate_tcp_endpoint(host, 7347)
