"""The app side of the control socket: asks the privileged service to connect or disconnect."""

import socket

from . import control
from .i18n import _


class Unavailable(OSError):
    """The service is not installed or not running."""


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def call(cmd: str, timeout: float = 5, **args) -> dict:
    path = control.socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise Unavailable(str(e)) from None
        except PermissionError:
            raise ServiceError("forbidden", _("No permission to use the VPN service")) from None
        try:
            try:
                control.send(s, {"cmd": cmd, **args})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the service may refuse before reading; its answer is still waiting
            reply = control.recv(s)
        except ValueError:
            raise ServiceError("protocol", _("The VPN service sent a broken reply")) from None
        except OSError:
            raise ServiceError("timeout", _("The VPN service did not answer")) from None
    if not reply.get("ok"):
        # the service speaks English; known messages are translated here
        raise ServiceError(reply.get("code", "error"), _(reply.get("message") or "error"))
    return reply


def hello() -> dict:
    """API version, service version and setup problems (missing snap permissions)."""
    return call("hello")


def status() -> dict:
    """{"state": active/activating/deactivating/inactive/failed, "running": {...}, "error": ""}"""
    return call("status")


def connect(request: dict) -> dict:
    """Starts or switches the tunnel and waits until traffic goes through it."""
    return call("connect", timeout=40, request=request)


def disconnect() -> dict:
    return call("disconnect", timeout=20)
