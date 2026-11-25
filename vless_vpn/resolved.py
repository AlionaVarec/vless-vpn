"""Tells systemd-resolved to send DNS of the whole system into the tunnel.

sing-box does this itself by running `resolvectl`, but inside the snap that fails: newer
systemd tools bind their D-Bus socket to an abstract address the snap sandbox forbids. The
plain socket and the SetLink* calls are allowed, so the service makes the calls itself here,
with a minimal D-Bus client in pure Python (no extra libraries in the root service).
"""

import os
import socket
import struct

SYSTEM_BUS = "/run/dbus/system_bus_socket"
RESOLVED = "org.freedesktop.resolve1"
RESOLVED_PATH = "/org/freedesktop/resolve1"
RESOLVED_IFACE = "org.freedesktop.resolve1.Manager"

METHOD_CALL, METHOD_RETURN, ERROR = 1, 2, 3
_ALIGN = {"y": 1, "b": 4, "i": 4, "u": 4, "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "v": 1}


class BusError(Exception):
    pass


# ---------- marshalling (little endian) ----------


def split(signature: str) -> list[str]:
    """ "ia(iay)" -> ["i", "a(iay)"]: complete types one by one."""
    types, i = [], 0
    while i < len(signature):
        j = _type_end(signature, i)
        types.append(signature[i:j])
        i = j
    return types


def _type_end(sig: str, i: int) -> int:
    if sig[i] == "a":
        return _type_end(sig, i + 1)
    if sig[i] == "(":
        depth = 0
        for j in range(i, len(sig)):
            depth += (sig[j] == "(") - (sig[j] == ")")
            if depth == 0:
                return j + 1
        raise ValueError(f"unbalanced signature {sig!r}")
    if sig[i] not in _ALIGN:
        raise ValueError(f"unsupported type {sig[i]!r}")
    return i + 1


class _Out:
    def __init__(self):
        self.buf = bytearray()

    def pad(self, n: int) -> None:
        self.buf += b"\0" * (-len(self.buf) % n)

    def put(self, fmt: str, value, align: int) -> None:
        self.pad(align)
        self.buf += struct.pack("<" + fmt, value)


def _put(out: _Out, t: str, v) -> None:
    c = t[0]
    if c == "y":
        out.put("B", v, 1)
    elif c == "b":
        out.put("I", 1 if v else 0, 4)
    elif c == "i":
        out.put("i", v, 4)
    elif c == "u":
        out.put("I", v, 4)
    elif c in "so":
        data = v.encode()
        out.put("I", len(data), 4)
        out.buf += data + b"\0"
    elif c == "g":
        data = v.encode()
        out.put("B", len(data), 1)
        out.buf += data + b"\0"
    elif c == "a":
        elem = t[1:]
        out.pad(4)
        at = len(out.buf)
        out.buf += b"\0\0\0\0"
        out.pad(_ALIGN[elem[0]])  # the padding before the first element is not counted
        start = len(out.buf)
        if elem == "y":
            out.buf += bytes(v)
        else:
            for item in v:
                _put(out, elem, item)
        struct.pack_into("<I", out.buf, at, len(out.buf) - start)
    elif c == "(":
        out.pad(8)
        for sub, item in zip(split(t[1:-1]), v, strict=True):
            _put(out, sub, item)
    elif c == "v":
        sig, value = v
        _put(out, "g", sig)
        _put(out, sig, value)
    else:
        raise ValueError(f"unsupported type {t!r}")


def method_call(serial: int, dest: str, path: str, iface: str, member: str, sig: str = "", args=()) -> bytes:
    body = _Out()
    for t, a in zip(split(sig), args, strict=True):
        _put(body, t, a)
    fields = [(1, ("o", path)), (2, ("s", iface)), (3, ("s", member)), (6, ("s", dest))]
    if sig:
        fields.append((8, ("g", sig)))
    out = _Out()
    out.buf += b"l" + bytes([METHOD_CALL, 0, 1])
    out.put("I", len(body.buf), 4)
    out.put("I", serial, 4)
    _put(out, "a(yv)", fields)
    out.pad(8)  # the body starts 8-aligned, so it could be marshalled from offset 0
    return bytes(out.buf + body.buf)


# ---------- reading replies ----------


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise BusError("the bus closed the connection")
        data += chunk
    return data


def parse_header(head: bytes, fields: bytes) -> tuple[int, int | None, str]:
    """(message type, reply serial, error name) from the fixed header and header fields."""
    e = "<" if head[:1] == b"l" else ">"
    mtype = head[1]
    reply_serial, error_name = None, ""
    pos = 0
    while pos < len(fields):
        pos += -(16 + pos) % 8  # every field is a struct, aligned to 8 from the message start
        if pos >= len(fields):
            break
        code = fields[pos]
        sig_len = fields[pos + 1]
        sig = fields[pos + 2 : pos + 2 + sig_len].decode()
        pos += 3 + sig_len
        if sig in ("s", "o"):
            pos += -(16 + pos) % 4
            (n,) = struct.unpack_from(e + "I", fields, pos)
            value = fields[pos + 4 : pos + 4 + n].decode(errors="replace")
            pos += 5 + n
        elif sig == "u":
            pos += -(16 + pos) % 4
            (value,) = struct.unpack_from(e + "I", fields, pos)
            pos += 4
        elif sig == "g":
            n = fields[pos]
            value = fields[pos + 1 : pos + 1 + n].decode()
            pos += 2 + n
        else:
            raise BusError(f"unexpected header field type {sig!r}")
        if code == 5:
            reply_serial = value
        elif code == 4:
            error_name = value
    return mtype, reply_serial, error_name


class SystemBus:
    def __init__(self, path: str = SYSTEM_BUS, timeout: float = 3):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.serial = 0
        try:
            self.sock.connect(path)
            self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.geteuid()).encode().hex().encode() + b"\r\n")
            if not self._line().startswith(b"OK "):
                raise BusError("the bus refused authentication")
            self.sock.sendall(b"BEGIN\r\n")
            self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello")
        except BaseException:
            self.sock.close()
            raise

    def _line(self) -> bytes:
        data = b""
        while not data.endswith(b"\r\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise BusError("the bus closed the connection")
            data += chunk
        return data

    def call(self, dest: str, path: str, iface: str, member: str, sig: str = "", args=()) -> None:
        """Calls a method and waits for its answer; raises BusError if it is an error."""
        self.serial += 1
        self.sock.sendall(method_call(self.serial, dest, path, iface, member, sig, args))
        while True:
            head = _recv_exact(self.sock, 16)
            e = "<" if head[:1] == b"l" else ">"
            body_len, _serial, fields_len = struct.unpack(e + "III", head[4:16])
            rest = _recv_exact(self.sock, fields_len + (-(16 + fields_len) % 8) + body_len)
            mtype, reply_serial, error_name = parse_header(head, rest[:fields_len])
            if reply_serial != self.serial:
                continue  # a signal such as NameAcquired
            if mtype == ERROR:
                raise BusError(error_name or "D-Bus error")
            return

    def close(self) -> None:
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def route_all_dns(ifindex: int, dns_ip: str, path: str = SYSTEM_BUS) -> None:
    """DNS server dns_ip on the link, the "~." routing domain and the default route: every
    lookup of the system goes to that link."""
    packed = list(socket.inet_aton(dns_ip))
    with SystemBus(path) as bus:

        def manager(member, sig, args):
            bus.call(RESOLVED, RESOLVED_PATH, RESOLVED_IFACE, member, sig, args)

        manager("SetLinkDNS", "ia(iay)", (ifindex, [(socket.AF_INET, packed)]))
        manager("SetLinkDomains", "ia(sb)", (ifindex, [(".", True)]))
        manager("SetLinkDefaultRoute", "ib", (ifindex, True))
