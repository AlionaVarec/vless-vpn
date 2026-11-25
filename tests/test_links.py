import pytest

from vless_vpn import links

UID = "11111111-2222-4333-8444-555555555555"
PBK = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"  # bytes 0..31 in base64url: a key of the right format
REALITY = (
    f"vless://{UID}@203.0.113.10:443?type=tcp&security=reality&encryption=none"
    f"&flow=xtls-rprx-vision&pbk={PBK}&fp=chrome&sni=www.example.com&sid=7b&spx=%2F#Home"
)


def test_reality_link():
    ln = links.parse(REALITY)
    assert (ln.name, ln.uuid, ln.host, ln.port) == ("Home", UID, "203.0.113.10", 443)
    assert (ln.network, ln.security, ln.flow) == ("tcp", "reality", "xtls-rprx-vision")
    assert (ln.sni, ln.fingerprint, ln.public_key, ln.short_id) == ("www.example.com", "chrome", PBK, "7b")
    assert ln.host_is_ip


def test_name_is_unquoted_and_defaults_to_host():
    ln = links.parse(f"vless://{UID}@example.com:8443?security=tls#%D0%94%D0%BE%D0%BC")
    assert ln.name == "Дом"  # names may be in any language
    assert not ln.host_is_ip
    assert links.parse(f"vless://{UID}@example.com:8443").name == "example.com:8443"


def test_ws_tls_link():
    ln = links.parse(
        f"vless://{UID}@cdn.example.com:443?type=ws&security=tls&path=%2Fws%3Fed%3D2048"
        "&host=front.example.com&alpn=h2,http/1.1&allowInsecure=1"
    )
    assert (ln.network, ln.path, ln.host_header) == ("ws", "/ws?ed=2048", "front.example.com")
    assert ln.alpn == ["h2", "http/1.1"] and ln.insecure


def test_ipv6_host():
    ln = links.parse(f"vless://{UID}@[2001:db8::1]:443?security=none")
    assert ln.host == "2001:db8::1" and ln.host_is_ip


def test_raw_is_tcp():
    assert links.parse(REALITY.replace("type=tcp", "type=raw")).network == "tcp"


def test_udp443_flow_maps_to_vision():
    ln = links.parse(REALITY.replace("xtls-rprx-vision", "xtls-rprx-vision-udp443"))
    assert ln.flow == "xtls-rprx-vision"


@pytest.mark.parametrize(
    "link, message",
    [
        ("vmess://abc", "vless://"),
        (f"vless://{UID}@host", "port"),
        ("vless://not-a-uuid@host:443", "UUID"),
        (f"vless://{UID}@host:99999", "invalid port"),
        (f"vless://{UID}@[::1:443", "address"),
        (f"vless://{UID}@[zz]:443", "address"),
        (f"vless://{UID}@host:443?type=xhttp", "xhttp"),
        (f"vless://{UID}@host:443?security=reality", "pbk"),
        (f"vless://{UID}@host:443?security=reality&pbk=short", "pbk"),
        (REALITY.replace("sid=7b", "sid=xyz"), "sid"),
        (REALITY.replace("sid=7b", "sid=0123456789abcdef00"), "sid"),
        (f"vless://{UID}@host:443?encryption=mlkem768x25519plus.native.0rtt.abc", "encryption"),
        (f"vless://{UID}@host:443?type=tcp&headerType=http", "headerType"),
        (f"vless://{UID}@host:443?flow=xtls-rprx-direct", "flow"),
    ],
)
def test_bad_links(link, message):
    with pytest.raises(links.LinkError, match=message):
        links.parse(link)


def test_find_links_in_text():
    text = f"my server:\n{REALITY}\n\n  vless://x@y:1  junk https://example.com"
    assert links.find_links(text) == [REALITY, "vless://x@y:1"]


def test_errors_follow_the_language(russian):
    with pytest.raises(links.LinkError, match="должна начинаться"):
        links.parse("vmess://abc")
    with pytest.raises(links.LinkError, match="транспорт «xhttp»"):
        links.parse(f"vless://{UID}@host:443?type=xhttp")
