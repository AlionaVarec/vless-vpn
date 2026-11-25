"""The minimal D-Bus client that points systemd-resolved at the tunnel."""

import os
import socket

import pytest

from vless_vpn import resolved

gi = pytest.importorskip("gi")
from gi.repository import Gio, GLib  # noqa: E402

CALLS = [
    ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello", "", ()),
    (resolved.RESOLVED, resolved.RESOLVED_PATH, resolved.RESOLVED_IFACE, "SetLinkDNS", "ia(iay)",
     (7, [(socket.AF_INET, [198, 18, 0, 2])])),
    (resolved.RESOLVED, resolved.RESOLVED_PATH, resolved.RESOLVED_IFACE, "SetLinkDomains", "ia(sb)",
     (7, [(".", True), ("example.com", False)])),
    (resolved.RESOLVED, resolved.RESOLVED_PATH, resolved.RESOLVED_IFACE, "SetLinkDefaultRoute", "ib",
     (7, True)),
]


def gdbus_blob(serial, dest, path, iface, member, sig, args) -> bytes:
    msg = Gio.DBusMessage.new_method_call(dest, path, iface, member)
    if sig:
        msg.set_body(GLib.Variant(f"({sig})", args))
    msg.set_serial(serial)
    msg.set_byte_order(Gio.DBusMessageByteOrder.LITTLE_ENDIAN)
    return bytes(msg.to_blob(Gio.DBusCapabilityFlags.NONE))


@pytest.mark.parametrize("call", CALLS, ids=[c[3] for c in CALLS])
def test_messages_match_glib(call):
    ours = resolved.method_call(5, *call)
    parsed = Gio.DBusMessage.new_from_blob(ours, Gio.DBusCapabilityFlags.NONE)
    assert parsed.get_member() == call[3] and parsed.get_serial() == 5
    if call[4]:
        assert parsed.get_body().unpack() == GLib.Variant(f"({call[4]})", call[5]).unpack()
    # header fields may come in any order; written again by GLib, the message must be identical
    parsed.set_byte_order(Gio.DBusMessageByteOrder.LITTLE_ENDIAN)
    assert bytes(parsed.to_blob(Gio.DBusCapabilityFlags.NONE)) == gdbus_blob(5, *call)


def test_signature_split():
    assert resolved.split("ia(iay)") == ["i", "a(iay)"]
    assert resolved.split("a(yv)s") == ["a(yv)", "s"]
    with pytest.raises(ValueError):
        resolved.split("i(a")


def test_error_reply_is_parsed():
    request = Gio.DBusMessage.new_method_call("a.b", "/", "a.b", "M")
    request.set_serial(4)
    reply = Gio.DBusMessage.new_method_error_literal(request, "org.freedesktop.DBus.Error.AccessDenied", "no")
    reply.set_serial(9)
    reply.set_byte_order(Gio.DBusMessageByteOrder.LITTLE_ENDIAN)
    blob = bytes(reply.to_blob(Gio.DBusCapabilityFlags.NONE))
    fields_len = int.from_bytes(blob[12:16], "little")
    mtype, reply_serial, error = resolved.parse_header(blob[:16], blob[16 : 16 + fields_len])
    assert mtype == resolved.ERROR and reply_serial == 4
    assert error == "org.freedesktop.DBus.Error.AccessDenied"


live = pytest.mark.skipif(
    not (os.path.exists(resolved.SYSTEM_BUS) and os.path.isdir("/run/systemd/resolve")),
    reason="no system bus or systemd-resolved",
)


@live
def test_live_bus_calls_succeed_and_errors_come_back():
    with resolved.SystemBus() as bus:
        bus.call(resolved.RESOLVED, resolved.RESOLVED_PATH, "org.freedesktop.DBus.Peer", "Ping")
        with pytest.raises(resolved.BusError):  # no such link (or not allowed): nothing changes
            bus.call(resolved.RESOLVED, resolved.RESOLVED_PATH, resolved.RESOLVED_IFACE,
                     "SetLinkDefaultRoute", "ib", (999_999, True))
