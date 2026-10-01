"""Newline-delimited JSON protocol shared by the service and worker runtime."""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1


def encode_message(message: dict[str, Any]) -> bytes:
    payload = {"version": PROTOCOL_VERSION, **message}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("Protocol messages must be JSON objects")
    version = message.get("version", PROTOCOL_VERSION)
    if version != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported Sanctuary protocol version: {version}")
    return message
