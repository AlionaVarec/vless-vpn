"""Network checks: TCP ping of a server and the public exit address."""

import socket
import time
import urllib.request

TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"
_resolved: dict[str, str] = {}


def resolve(host: str) -> str:
    """Server IP, IPv4 first (DNS inside the tunnel is IPv4 only too).

    The last good answer is remembered: with the VPN on, DNS goes through the tunnel, and if
    that server dies, without the cache there would be no way to switch to another one.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        if host in _resolved:
            return _resolved[host]
        raise
    v4 = [i[4][0] for i in infos if i[0] == socket.AF_INET]
    _resolved[host] = (v4 or [infos[0][4][0]])[0]
    return _resolved[host]


def tcp_ping(ip: str, port: int, timeout: float = 3.0, tries: int = 2) -> float | None:
    """TCP handshake time in ms (best of the tries), None if the server did not answer."""
    best = None
    for _ in range(tries):
        start = time.perf_counter()
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                pass
        except OSError:
            continue
        ms = (time.perf_counter() - start) * 1000
        best = ms if best is None else min(best, ms)
    return best


def exit_info(timeout: float = 8.0) -> dict:
    """The public IP and country the internet sees us under."""
    with urllib.request.urlopen(TRACE_URL, timeout=timeout) as r:
        text = r.read(4096).decode()
    kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {"ip": kv.get("ip", "?"), "loc": kv.get("loc", "?")}
