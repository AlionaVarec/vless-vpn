"""Parsing vless:// links into a structure the sing-box config builder understands."""

import base64
import ipaddress
import re
import urllib.parse as up
import uuid as uuidlib
from dataclasses import dataclass, field

from .i18n import _

# Transports sing-box supports. xhttp, kcp and quic exist only in Xray.
NETWORKS = {"tcp", "ws", "grpc", "http", "h2", "httpupgrade"}
SECURITIES = {"none", "tls", "reality"}


class LinkError(ValueError):
    pass


@dataclass
class VlessLink:
    name: str
    uuid: str
    host: str
    port: int
    network: str = "tcp"
    security: str = "none"
    flow: str = ""
    sni: str = ""
    fingerprint: str = ""
    public_key: str = ""
    short_id: str = ""
    alpn: list[str] = field(default_factory=list)
    insecure: bool = False
    path: str = ""
    host_header: str = ""
    service_name: str = ""

    @property
    def host_is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True


def _is_x25519_key(value: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))) == 32
    except ValueError:
        return False


def parse(link: str) -> VlessLink:
    link = link.strip()
    if not link.lower().startswith("vless://"):
        raise LinkError(_("the link must start with vless://"))
    try:
        u = up.urlsplit(link)
        host = u.hostname
    except ValueError:
        raise LinkError(_("invalid server address")) from None
    try:
        port = u.port
    except ValueError:
        raise LinkError(_("invalid port")) from None
    if not u.username or not host or not port:
        raise LinkError(_("the link has no UUID, address or port"))
    uid = up.unquote(u.username)
    try:
        uuidlib.UUID(uid)
    except ValueError:
        raise LinkError(_("the UUID in the link is invalid")) from None

    q = dict(up.parse_qsl(u.query, keep_blank_values=True))
    network = (q.get("type") or "tcp").lower()
    if network == "raw":  # the new Xray name for tcp
        network = "tcp"
    if network not in NETWORKS:
        raise LinkError(_("transport “{name}” is not supported by sing-box").format(name=network))
    security = (q.get("security") or "none").lower()
    if security not in SECURITIES:
        raise LinkError(_("security “{name}” is not supported").format(name=security))
    encryption = q.get("encryption") or "none"
    if encryption != "none":
        raise LinkError(_("VLESS encryption is not supported by sing-box"))
    if network == "tcp" and q.get("headerType", "none") not in ("", "none"):
        raise LinkError(_("TCP HTTP header obfuscation (headerType) is not supported by sing-box"))

    flow = q.get("flow", "")
    if flow.startswith("xtls-rprx-vision"):
        flow = "xtls-rprx-vision"  # sing-box has no separate -udp443 variant
    elif flow:
        raise LinkError(_("flow “{name}” is not supported").format(name=flow))

    if security == "reality":
        if not _is_x25519_key(q.get("pbk", "")):
            raise LinkError(_("Reality needs a valid public key (pbk)"))
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", q.get("sid", "")):
            raise LinkError(_("sid (short ID) must be a hex string of up to 16 characters"))

    return VlessLink(
        name=up.unquote(u.fragment).strip() or f"{u.hostname}:{port}",
        uuid=uid,
        host=host,
        port=port,
        network=network,
        security=security,
        flow=flow,
        sni=q.get("sni") or q.get("peer") or "",
        fingerprint=q.get("fp", ""),
        public_key=q.get("pbk", ""),
        short_id=q.get("sid", ""),
        alpn=[a for a in q.get("alpn", "").split(",") if a],
        insecure=q.get("allowInsecure", q.get("insecure", "")).lower() in ("1", "true"),
        path=q.get("path", ""),
        host_header=q.get("host", ""),
        service_name=q.get("serviceName", ""),
    )


def find_links(text: str) -> list[str]:
    """Every vless:// link in free text (one per line or separated by spaces)."""
    return [w for w in text.split() if w.lower().startswith("vless://")]
