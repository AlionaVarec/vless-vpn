import json
import os
import shutil
import subprocess

import pytest

from vless_vpn import links, singbox

from .test_links import PBK, REALITY, UID


def test_reality_outbound():
    cfg = singbox.build(links.parse(REALITY), "203.0.113.10")
    out = cfg["outbounds"][0]
    assert out == {
        "type": "vless",
        "tag": "proxy",
        "server": "203.0.113.10",
        "server_port": 443,
        "uuid": UID,
        "flow": "xtls-rprx-vision",
        "tls": {
            "enabled": True,
            "server_name": "www.example.com",
            "utls": {"enabled": True, "fingerprint": "chrome"},
            "reality": {"enabled": True, "public_key": PBK, "short_id": "7b"},
        },
    }
    assert "transport" not in out
    assert cfg["route"]["final"] == "proxy"


def test_tun_routes_whole_system_except_lan_and_servers():
    cfg = singbox.build(links.parse(REALITY), "203.0.113.10", ["203.0.113.10", "203.0.113.5"])
    tun = cfg["inbounds"][0]
    assert tun["type"] == "tun" and tun["auto_route"] and tun["strict_route"]
    exclude = tun["route_exclude_address"]
    assert "192.168.0.0/16" in exclude and "172.16.0.0/12" in exclude  # LAN и Docker
    assert exclude.count("203.0.113.10/32") == 1 and "203.0.113.5/32" in exclude


def test_dns_goes_through_proxy():
    cfg = singbox.build(links.parse(REALITY), "203.0.113.10")
    remote = cfg["dns"]["servers"][0]
    assert remote["detour"] == "proxy" and cfg["dns"]["final"] == remote["tag"]
    assert {"protocol": "dns", "action": "hijack-dns"} in cfg["route"]["rules"]


def test_server_name_falls_back_to_domain_host():
    ln = links.parse(f"vless://{UID}@vpn.example.com:443?security=tls&fp=firefox")
    tls = singbox.build(ln, "203.0.113.5")["outbounds"][0]["tls"]
    assert tls["server_name"] == "vpn.example.com"
    assert tls["utls"] == {"enabled": True, "fingerprint": "firefox"}


def test_ws_early_data_moves_out_of_path():
    ln = links.parse(f"vless://{UID}@h:443?type=ws&security=tls&path=%2Fws%3Fed%3D2048&host=f.example")
    t = singbox.build(ln, "203.0.113.5")["outbounds"][0]["transport"]
    assert t == {
        "type": "ws",
        "path": "/ws",
        "max_early_data": 2048,
        "early_data_header_name": "Sec-WebSocket-Protocol",
        "headers": {"Host": "f.example"},
    }


def test_grpc_transport():
    ln = links.parse(f"vless://{UID}@h:443?type=grpc&security=tls&serviceName=svc")
    t = singbox.build(ln, "203.0.113.5")["outbounds"][0]["transport"]
    assert t == {"type": "grpc", "service_name": "svc"}


def test_request_roundtrip_is_private(tmp_path):
    path = tmp_path / "cfg" / "request.json"
    req = singbox.make_request("p1", REALITY, "203.0.113.10", ["203.0.113.5", "203.0.113.5"])
    singbox.write_private(req, path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text()) == {
        "profile_id": "p1",
        "link": REALITY,
        "server_ip": "203.0.113.10",
        "bypass_ips": ["203.0.113.5"],
    }


def test_from_request_builds_same_config_as_build():
    req = singbox.make_request("p1", REALITY, "203.0.113.10", ["203.0.113.5"])
    assert singbox.from_request(req) == singbox.build(
        links.parse(REALITY), "203.0.113.10", ["203.0.113.5"]
    )


@pytest.mark.parametrize(
    "request_, message",
    [
        ([], "JSON object"),
        ({"server_ip": "203.0.113.10"}, "no link"),
        ({"link": "x" * 5000, "server_ip": "203.0.113.10"}, "no link"),
        ({"link": REALITY, "server_ip": "evil.example"}, "IP"),
        ({"link": REALITY, "server_ip": "203.0.113.10", "bypass_ips": ["1.2.3.4/0"]}, "IP"),
        ({"link": REALITY, "server_ip": "fe80::1%eth0"}, "zone"),
        ({"link": REALITY, "server_ip": "203.0.113.10", "bypass_ips": "1.2.3.4"}, "list"),
        ({"link": REALITY, "server_ip": "203.0.113.10", "bypass_ips": ["1.1.1.1"] * 300}, "list"),
        ({"link": REALITY, "server_ip": "203.0.113.10", "profile_id": ["x"]}, "profile ID"),
    ],
)
def test_from_request_rejects_bad_input(request_, message):
    with pytest.raises(singbox.RequestError, match=message):
        singbox.from_request(request_)


def test_from_request_rejects_bad_link():
    with pytest.raises(links.LinkError):
        singbox.from_request({"link": "vless://bad@x:1", "server_ip": "203.0.113.10"})


def test_lone_surrogate_does_not_break_write(tmp_path):
    # a surrogate in sni: this field goes into the sing-box config
    link = REALITY.replace("sni=www.example.com", "sni=www.example.com\ud800")
    path = tmp_path / "sing-box.json"
    singbox.write_private(singbox.from_request({"link": link, "server_ip": "203.0.113.10"}), path)
    assert json.loads(path.read_text())["outbounds"][0]["tls"]["server_name"].startswith("www.")
    assert not list(tmp_path.glob("*.tmp"))


def test_running_state_has_no_link():
    req = singbox.make_request("p1", REALITY, "203.0.113.10", ["203.0.113.5"])
    state = singbox.running_state(req)
    assert state == {"profile_id": "p1", "server_ip": "203.0.113.10", "bypass_ips": ["203.0.113.5"]}
    assert UID not in json.dumps(state)


SING_BOX = os.environ.get("SING_BOX") or shutil.which("sing-box") or "/usr/local/bin/sing-box"


@pytest.mark.skipif(not os.path.exists(SING_BOX), reason="sing-box is not installed")
@pytest.mark.parametrize(
    "link",
    [
        REALITY,
        f"vless://{UID}@h.example:443?type=ws&security=tls&path=%2Fws%3Fed%3D2048",
        f"vless://{UID}@h.example:443?type=grpc&security=tls&serviceName=svc",
        f"vless://{UID}@h.example:80?type=httpupgrade&path=%2Fup&host=f.example",
    ],
)
def test_sing_box_accepts_config(tmp_path, link):
    path = tmp_path / "sb.json"
    path.write_text(json.dumps(singbox.build(links.parse(link), "203.0.113.5")))
    r = subprocess.run([SING_BOX, "check", "-c", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
