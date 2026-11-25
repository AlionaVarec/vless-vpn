"""The privileged service with a fake sing-box: no root and no real tunnel needed."""

import grp
import json
import os
import pwd
import stat
import sys
import threading
import time

import pytest

from vless_vpn import client, daemon, singbox

from .test_links import REALITY, UID

FAKE = """#!{python}
import os, signal, sys, time
from pathlib import Path

mode = os.environ.get("FAKE_SB", "run")
counter = Path(os.environ["FAKE_SB_COUNTER"])
runs = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(runs))
ready = counter.with_suffix(".ready")
assert sys.argv[1:3] == ["run", "-D"] and Path(sys.argv[sys.argv.index("-c") + 1]).exists()


def leave(code):
    ready.unlink(missing_ok=True)  # like sing-box removing its routes
    sys.exit(code)


signal.signal(signal.SIGTERM, lambda *a: leave(0))
print("WARN[0000] starting", flush=True)
if mode == "fatal":
    print("FATAL[0000] start service: open tun: operation not permitted", flush=True)
    leave(1)
ready.touch()
if mode == "crash" or (mode == "crash-once" and runs == 1):
    time.sleep(0.3)
    print("ERROR[0001] connection: broken pipe", flush=True)
    leave(2)
while True:
    time.sleep(1)
"""


def request(pid="p1"):
    return singbox.make_request(pid, REALITY, "203.0.113.10", ["203.0.113.5"])


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fake_singbox(tmp_path, monkeypatch):
    path = tmp_path / "sing-box"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_SB_COUNTER", str(tmp_path / "runs"))
    monkeypatch.setattr(daemon, "RESTART_DELAY", 0.1)
    return path


def runs(tmp_path) -> int:
    f = tmp_path / "runs"
    return int(f.read_text()) if f.exists() else 0


def tunnel_up(tmp_path):
    """The fake core marks the tunnel ready while it runs, as routes exist while sing-box runs."""
    return lambda: (tmp_path / "runs.ready").exists()


@pytest.fixture
def controller(tmp_path, fake_singbox):
    ctl = daemon.Controller(str(fake_singbox), tmp_path / "work", ready=tunnel_up(tmp_path))
    yield ctl
    ctl.disconnect()


def test_connect_runs_singbox_and_disconnect_cleans_up(controller, tmp_path):
    status = controller.connect(request())
    assert status["state"] == "active" and status["error"] == ""
    running = {"profile_id": "p1", "server_ip": "203.0.113.10", "bypass_ips": ["203.0.113.5"]}
    assert status["running"] == running
    cfg = tmp_path / "work" / "sing-box.json"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert json.loads(cfg.read_text())["outbounds"][0]["uuid"] == UID
    proc = controller.proc

    status = controller.disconnect()
    assert status == {"state": "inactive", "running": {}, "error": ""}
    assert proc.poll() is not None and not cfg.exists()


def test_switch_replaces_the_process(controller, tmp_path):
    controller.connect(request("p1"))
    first = controller.proc
    status = controller.connect(request("p2"))
    assert status["running"]["profile_id"] == "p2" and first.poll() is not None
    assert runs(tmp_path) == 2 and controller.state == "active"


def test_bad_request_keeps_the_running_tunnel(controller):
    controller.connect(request())
    proc = controller.proc
    with pytest.raises(singbox.RequestError):
        controller.connect({"link": REALITY, "server_ip": "not-an-ip"})
    assert controller.proc is proc and controller.state == "active"


def test_core_error_on_start_is_reported(tmp_path, fake_singbox, monkeypatch):
    monkeypatch.setenv("FAKE_SB", "fatal")
    ctl = daemon.Controller(str(fake_singbox), tmp_path / "work", ready=lambda: False)
    status = ctl.connect(request())
    assert status["state"] == "failed" and status["running"] == {}
    assert "operation not permitted" in status["error"] and runs(tmp_path) == 1


def test_tunnel_that_never_gets_ready_fails_in_time(tmp_path, fake_singbox, monkeypatch):
    monkeypatch.setattr(daemon, "READY_TIMEOUT", 0.3)
    ctl = daemon.Controller(str(fake_singbox), tmp_path / "work", ready=lambda: False)
    status = ctl.connect(request())
    assert status["state"] == "failed" and "10 seconds" in status["error"]
    time.sleep(0.3)  # the killed process must not count as a crash and restart
    assert ctl.state == "failed" and runs(tmp_path) == 1


def test_crash_while_connected_restarts(controller, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SB", "crash-once")
    controller.connect(request())
    assert wait_for(lambda: runs(tmp_path) == 2 and controller.state == "active")
    assert controller.status()["running"]["profile_id"] == "p1"


def test_crash_loop_gives_up(controller, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SB", "crash")
    monkeypatch.setattr(daemon, "RESTART_LIMIT", 2)
    controller.connect(request())
    assert wait_for(lambda: controller.state == "failed")
    assert runs(tmp_path) == 3 and "broken pipe" in controller.status()["error"]
    time.sleep(0.4)
    assert runs(tmp_path) == 3


def test_disconnect_cancels_a_pending_restart(controller, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SB", "crash")
    monkeypatch.setattr(daemon, "RESTART_DELAY", 0.5)
    controller.connect(request())
    assert wait_for(lambda: controller.state == "activating" and controller.proc is None)
    controller.disconnect()
    time.sleep(0.8)
    assert controller.state == "inactive" and runs(tmp_path) == 1


def test_last_error_prefers_fatal_lines():
    lines = ["\x1b[31mFATAL[0000] start service: boom\x1b[0m", "ERROR later: dial tcp", "INFO bye", ""]
    assert daemon.last_error(lines) == "FATAL[0000] start service: boom"
    assert daemon.last_error(["ERROR a", "INFO b"]) == "ERROR a"
    assert daemon.last_error(["just text"]) == "just text"
    assert daemon.last_error([]) == ""


def test_only_root_and_admins_are_allowed():
    me = pwd.getpwuid(os.getuid())
    my_group = grp.getgrgid(me.pw_gid).gr_name
    assert daemon.uid_allowed(0, groups=())
    assert daemon.uid_allowed(os.getuid(), groups=(my_group,))
    assert not daemon.uid_allowed(os.getuid(), groups=("no-such-group-vless",))
    assert not daemon.uid_allowed(3_999_999_999 % 2**31)  # no such user


# ---------- over the real socket ----------


@pytest.fixture
def server(tmp_path, controller, monkeypatch):
    path = tmp_path / "run" / "control.sock"
    srv = daemon.Server(path, controller)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VLESS_VPN_SOCKET", str(path))
    monkeypatch.setattr(daemon, "uid_allowed", lambda uid: uid == os.getuid())
    yield path
    srv.shutdown()
    srv.server_close()


def test_socket_roundtrip(server):
    assert stat.S_IMODE(server.stat().st_mode) == 0o666
    assert stat.S_IMODE(server.parent.stat().st_mode) == 0o755
    hello = client.hello()
    assert hello["api"] == 1 and hello["problems"] == []
    assert client.status()["state"] == "inactive"
    assert client.connect(request())["state"] == "active"
    assert client.status()["running"]["profile_id"] == "p1"
    assert client.disconnect()["state"] == "inactive"


def test_socket_reports_bad_requests(server):
    with pytest.raises(client.ServiceError) as e:
        client.connect({"link": "vless://bad@x:1", "server_ip": "203.0.113.10"})
    assert e.value.code == "bad_request" and "UUID" in str(e.value)
    with pytest.raises(client.ServiceError) as e:
        client.call("format-disk")
    assert e.value.code == "protocol"


def test_socket_refuses_other_users(server, monkeypatch):
    monkeypatch.setattr(daemon, "uid_allowed", lambda uid: False)
    with pytest.raises(client.ServiceError) as e:
        client.connect(request())
    assert e.value.code == "forbidden" and "administrators" in str(e.value)


def test_refusal_is_translated(server, monkeypatch, russian):
    monkeypatch.setattr(daemon, "uid_allowed", lambda uid: False)
    with pytest.raises(client.ServiceError, match="только администраторы"):
        client.status()


def test_missing_service_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("VLESS_VPN_SOCKET", str(tmp_path / "nothing.sock"))
    with pytest.raises(client.Unavailable):
        client.status()


def test_service_refuses_to_run_without_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    assert daemon.main(["--socket", str(tmp_path / "s.sock"), "--work-dir", str(tmp_path / "w")]) == 1


def test_snap_paths(monkeypatch):
    monkeypatch.setenv("SNAP", "/snap/vless-vpn/x1")
    monkeypatch.setenv("SNAP_DATA", "/var/snap/vless-vpn/x1")
    monkeypatch.setenv("SNAP_COMMON", "/var/snap/vless-vpn/common")
    monkeypatch.delenv("VLESS_VPN_SOCKET", raising=False)
    binary, work = daemon.default_paths()
    assert (binary, str(work)) == ("/snap/vless-vpn/x1/bin/sing-box", "/var/snap/vless-vpn/x1")
    assert str(client.control.socket_path()) == "/var/snap/vless-vpn/common/control.sock"


# ---------- review findings ----------


def test_slow_client_is_cut_off_by_an_overall_deadline(server, monkeypatch):
    import socket as sk

    monkeypatch.setattr(daemon, "READ_DEADLINE", 0.6)
    s = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
    s.connect(str(server))
    s.settimeout(3)
    start = time.monotonic()
    for _ in range(8):  # a byte every 0.25 s: a per-byte timeout would never fire
        try:
            s.sendall(b"{")
        except BrokenPipeError:
            break  # the service gave up on us, as it should
        time.sleep(0.25)
    reply = s.recv(4096)
    assert b"broken request" in reply and time.monotonic() - start < 2
    s.close()


def test_too_many_clients_are_dropped_not_queued(tmp_path, controller, monkeypatch):
    import socket as sk

    monkeypatch.setattr(daemon, "MAX_CLIENTS", 2)
    monkeypatch.setattr(daemon, "READ_DEADLINE", 3)
    monkeypatch.setattr(daemon, "uid_allowed", lambda uid: True)
    path = tmp_path / "s.sock"
    srv = daemon.Server(path, controller)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        held = []
        for _ in range(2):
            c = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
            c.connect(str(path))
            held.append(c)
        time.sleep(0.2)
        extra = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
        extra.connect(str(path))
        extra.settimeout(2)
        assert extra.recv(10) == b""  # closed at once
        for c in held:
            c.close()
        time.sleep(0.2)
        monkeypatch.setenv("VLESS_VPN_SOCKET", str(path))
        assert client.status()["state"] == "inactive"  # slots come back
    finally:
        srv.shutdown()
        srv.server_close()


def test_refusal_needs_no_request(server, monkeypatch):
    import socket as sk

    monkeypatch.setattr(daemon, "uid_allowed", lambda uid: False)
    s = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
    s.connect(str(server))
    s.settimeout(2)
    assert b"forbidden" in s.recv(4096)  # answered without sending a byte
    s.close()


@pytest.mark.parametrize("path", ["%2F%3Fed%3D%C2%B2", "%2F%2F%5Bx"])
def test_odd_ws_paths_still_build_a_config(server, path):
    link = f"vless://{UID}@203.0.113.10:443?type=ws&security=tls&path={path}"
    assert client.connect(singbox.make_request("p1", link, "203.0.113.10", []))["state"] == "active"


def test_any_failure_still_gets_an_answer(server, controller, monkeypatch):
    def boom(_request):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(controller, "connect", boom)
    with pytest.raises(client.ServiceError) as e:
        client.connect(request())
    assert e.value.code == "error" and "disk on fire" in str(e.value)
    assert client.call("status", payload=[[[[]]]])["state"] == "inactive"  # extra fields are ignored


def test_deeply_nested_json_is_a_protocol_error(server):
    import socket as sk

    s = sk.socket(sk.AF_UNIX, sk.SOCK_STREAM)
    s.connect(str(server))
    s.settimeout(3)
    s.sendall(b"[" * 100_000 + b"\n")
    assert b"broken request" in s.recv(4096)
    s.close()


def test_lines_of_a_replaced_process_are_dropped(controller, capsys):
    class Proc:
        stdout = iter(["ERROR old: connection to example.org:443 failed\n", "FATAL[0000] old fatal\n"])

        def wait(self):
            return 1

    controller.gen = 5
    controller._pump(Proc(), gen=4)
    assert list(controller.output) == [] and controller.state == "inactive"
    err = capsys.readouterr().err
    assert "example.org" not in err  # connection lines never reach the journal
    assert "old fatal" in err


def test_dns_is_pointed_at_the_tunnel(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(daemon.socket, "if_nametoindex", lambda name: 42)
    monkeypatch.setattr(daemon.resolved, "route_all_dns", lambda index, ip: calls.append((index, ip)))
    assert daemon.dns_ready() and calls == [(42, "198.18.0.2")]


def test_dns_waits_for_the_link_and_falls_back(monkeypatch):
    monkeypatch.setattr(daemon.Path, "is_dir", lambda self: True)

    def gone(name):
        raise OSError("no such device")

    monkeypatch.setattr(daemon.socket, "if_nametoindex", gone)
    assert not daemon.dns_ready()  # no interface yet

    monkeypatch.setattr(daemon.socket, "if_nametoindex", lambda name: 42)

    def unknown(index, ip):
        raise daemon.resolved.BusError("org.freedesktop.resolve1.NoSuchLink")

    monkeypatch.setattr(daemon.resolved, "route_all_dns", unknown)
    assert not daemon.dns_ready()  # resolved has not seen it yet

    def denied(index, ip):
        raise daemon.resolved.BusError("org.freedesktop.DBus.Error.AccessDenied")

    monkeypatch.setattr(daemon.resolved, "route_all_dns", denied)
    monkeypatch.setattr(daemon, "_resolvectl_says_ready", lambda: "fallback")
    assert daemon.dns_ready() == "fallback"
