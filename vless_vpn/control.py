"""The local control socket between the app and the privileged service.

One connection carries one request: a line of JSON from the app, a line of JSON back.
"""

import json
import os
import socket
from pathlib import Path

API = 1
MAX_MESSAGE = 128 * 1024


def socket_path() -> Path:
    if os.environ.get("VLESS_VPN_SOCKET"):
        return Path(os.environ["VLESS_VPN_SOCKET"])
    if os.environ.get("SNAP_COMMON"):  # the app and the daemon of the snap share it
        return Path(os.environ["SNAP_COMMON"]) / "control.sock"
    return Path("/run/vless-vpn/control.sock")


def send(sock: socket.socket, message: dict) -> None:
    sock.sendall(json.dumps(message).encode() + b"\n")


def recv(sock: socket.socket) -> dict:
    """Reads one JSON line. Raises ValueError on garbage or an oversized message."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_MESSAGE:
            raise ValueError("message too large")
    message = json.loads(data)
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message
