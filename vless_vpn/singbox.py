"""sing-box config: a TUN device for the whole system plus a VLESS outbound. Format: sing-box 1.14.

The app never hands a config to the privileged service. It sends a request (the link and the
resolved server IPs) and the service builds the config itself with from_request(), so a local
user cannot make root run an arbitrary sing-box config.
"""

import ipaddress
import json
import os
import urllib.parse as up
from pathlib import Path

from . import links
from .i18n import N_
from .links import VlessLink

MAX_REQUEST_BYTES = 64 * 1024
MAX_BYPASS = 256

TUN_NAME = "vless0"
# 198.18.0.0/15 is reserved for benchmarks, so neither Docker nor home routers use it.
TUN_ADDRESS = ["198.18.0.1/30", "fdfe:dcba:9876::1/126"]
# sing-box announces the next address after the TUN address as the link DNS server.
TUN_DNS = "198.18.0.2"
ROUTE_TABLE = 2022
# Local networks and Docker stay outside the tunnel.
PRIVATE_NETS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
]
REMOTE_DNS = "1.1.1.1"


def _transport(link: VlessLink) -> dict | None:
    if link.network == "ws":
        path = link.path or "/"
        t = {"type": "ws", "path": path}
        # ?ed=2048 in the path is Xray early data
        try:
            pu = up.urlsplit(path)
        except ValueError:  # "//[x" and the like: keep the path as it is
            pu = up.urlsplit("")
        q = up.parse_qs(pu.query)
        ed = q.pop("ed", [""])[0]
        if ed.isascii() and ed.isdigit() and len(ed) <= 6:
            t["path"] = up.urlunsplit(("", "", pu.path or "/", up.urlencode(q, doseq=True), ""))
            t["max_early_data"] = int(ed)
            t["early_data_header_name"] = "Sec-WebSocket-Protocol"
        if link.host_header:
            t["headers"] = {"Host": link.host_header}
        return t
    if link.network == "grpc":
        return {"type": "grpc", "service_name": link.service_name}
    if link.network in ("http", "h2"):
        t = {"type": "http", "path": link.path or "/"}
        if link.host_header:
            t["host"] = link.host_header.split(",")
        return t
    if link.network == "httpupgrade":
        t = {"type": "httpupgrade", "path": link.path or "/"}
        if link.host_header:
            t["host"] = link.host_header
        return t
    return None


def _tls(link: VlessLink) -> dict | None:
    if link.security == "none":
        return None
    server_name = link.sni or link.host_header or ("" if link.host_is_ip else link.host)
    tls = {"enabled": True}
    if server_name:
        tls["server_name"] = server_name
    if link.security == "reality":
        # the sing-box Reality client works only through uTLS
        tls["utls"] = {"enabled": True, "fingerprint": link.fingerprint or "chrome"}
        tls["reality"] = {"enabled": True, "public_key": link.public_key, "short_id": link.short_id}
        return tls
    if link.insecure:
        tls["insecure"] = True
    if link.alpn:
        tls["alpn"] = link.alpn
    if link.fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": link.fingerprint}
    return tls


def outbound(link: VlessLink, server_ip: str) -> dict:
    """The VLESS outbound. server_ip is resolved in advance, so sing-box needs no DNS for it."""
    out = {
        "type": "vless",
        "tag": "proxy",
        "server": server_ip,
        "server_port": link.port,
        "uuid": link.uuid,
    }
    if link.flow:
        out["flow"] = link.flow
    tls = _tls(link)
    if tls:
        out["tls"] = tls
    transport = _transport(link)
    if transport:
        out["transport"] = transport
    return out


def _host_prefix(ip: str) -> str:
    return f"{ip}/128" if ":" in ip else f"{ip}/32"


def build(link: VlessLink, server_ip: str, bypass_ips: list[str] = ()) -> dict:
    """The full config for the service.

    bypass_ips are the addresses of every server in the list. They skip the tunnel, so the
    ping shown for them stays real while the VPN is on.
    """
    exclude = PRIVATE_NETS + sorted({_host_prefix(ip) for ip in [server_ip, *bypass_ips]})
    return {
        "log": {"level": "warn", "timestamp": False},
        "dns": {
            "servers": [
                {"type": "https", "tag": "remote", "server": REMOTE_DNS, "detour": "proxy"},
            ],
            "final": "remote",
            "strategy": "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": TUN_NAME,
                "address": TUN_ADDRESS,
                "auto_route": True,
                "strict_route": True,
                "auto_redirect": True,
                "iproute2_table_index": ROUTE_TABLE,
                "route_exclude_address": exclude,
            }
        ],
        "outbounds": [
            outbound(link, server_ip),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
    }


class RequestError(ValueError):
    pass


def make_request(profile_id: str, link: str, server_ip: str, bypass_ips) -> dict:
    return {
        "profile_id": profile_id,
        "link": link,
        "server_ip": server_ip,
        "bypass_ips": sorted(set(bypass_ips)),
    }


def _ip(value) -> str:
    try:
        ip = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        raise RequestError(N_("the request has an invalid IP address")) from None
    if getattr(ip, "scope_id", None):  # fe80::1%eth0 makes sing-box crash
        raise RequestError(N_("the request has an IPv6 address with a zone"))
    return str(ip)


def running_state(request: dict) -> dict:
    """The non-secret part of a request: what the service runs, without the link and its UUID."""
    return {k: request.get(k) for k in ("profile_id", "server_ip", "bypass_ips")}


def from_request(request) -> dict:
    """Config from an app request. Everything from outside is checked: type, size, format."""
    if not isinstance(request, dict):
        raise RequestError(N_("the request must be a JSON object"))
    link = request.get("link")
    if not isinstance(link, str) or len(link) > 4096:
        raise RequestError(N_("the request has no link"))
    profile_id = request.get("profile_id", "")
    if not isinstance(profile_id, str) or len(profile_id) > 64:
        raise RequestError(N_("the request has an invalid profile ID"))
    bypass = request.get("bypass_ips", [])
    if not isinstance(bypass, list) or len(bypass) > MAX_BYPASS:
        raise RequestError(N_("bypass_ips must be a list of up to 256 addresses"))
    parsed = links.parse(link)
    server_ip = _ip(request.get("server_ip"))
    bypass_ips = [_ip(b) for b in bypass]
    try:
        return build(parsed, server_ip, bypass_ips)
    except ValueError:
        raise RequestError(N_("the link has parameters the config cannot use")) from None


def write_private(data: dict, path: Path, mode: int = 0o600) -> None:
    """Atomic write, mode 600 by default: the config holds the UUID and keys.

    ASCII escaping on purpose: a lone surrogate from a request would otherwise break the write.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
