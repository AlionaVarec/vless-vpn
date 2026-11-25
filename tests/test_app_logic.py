"""Window logic without a real window: Window methods on plain objects."""

import os
import subprocess
from types import SimpleNamespace

from vless_vpn import APP_ID, app, client
from vless_vpn.app import Window


def fake(**kw):
    w = SimpleNamespace(
        pending_switch=False, busy=None, state="active", running_id="A",
        store=SimpleNamespace(selected="A"), connects=0,
    )
    w.__dict__.update(kw)
    w.connect_vpn = lambda: setattr(w, "connects", w.connects + 1)
    return w


def test_switch_made_during_operation_is_applied_after_it():
    w = fake(pending_switch=True, busy="Switching…", store=SimpleNamespace(selected="B"))
    Window.reconcile(w)
    assert w.connects == 0  # wait while an operation runs
    w.busy = None
    Window.reconcile(w)
    assert w.connects == 1


def test_no_switch_without_choice_in_this_window():
    # the service runs A, the file says B (another window): do not switch on our own
    w = fake(store=SimpleNamespace(selected="B"))
    Window.reconcile(w)
    assert w.connects == 0


def test_switch_done_clears_flag_and_vpn_off_cancels_it():
    w = fake(pending_switch=True, running_id="B", store=SimpleNamespace(selected="B"))
    Window.reconcile(w)
    assert w.connects == 0 and not w.pending_switch
    w = fake(pending_switch=True, state="inactive", store=SimpleNamespace(selected="B"))
    Window.reconcile(w)
    assert w.connects == 0 and not w.pending_switch


def poll_target(**kw):
    w = SimpleNamespace(
        polling=True, busy=None, seq=0, problem=None, plugs=[], states=[], error="", alive=True,
        running_id=None, bypass=set(),
    )
    w.__dict__.update(kw)
    w.problem_of = lambda err: Window.problem_of(w, err)
    w.set_state = lambda state, recheck=False: w.states.append(state)
    w.apply_status = lambda status, recheck=False: Window.apply_status(w, status, recheck)
    w.reconcile = lambda: None
    return w


def test_poll_finds_missing_or_forbidden_service():
    w = poll_target()
    Window.on_poll(w, None, client.Unavailable("no socket"), 0)
    assert w.problem == "missing" and w.states == ["unknown"]
    Window.on_poll(w, None, client.ServiceError("forbidden", "no"), 0)
    assert w.problem == "forbidden"


def test_poll_finds_snap_permissions_and_old_service():
    w = poll_target()
    status = {"state": "inactive", "running": {}, "error": ""}
    Window.on_poll(w, ({"api": 1, "problems": ["network-control"]}, status), None, 0)
    assert w.problem == "plugs" and w.plugs == ["network-control"]
    Window.on_poll(w, ({"api": 99, "problems": []}, status), None, 0)
    assert w.problem == "outdated"
    Window.on_poll(w, ({"api": 1, "problems": []}, status), None, 0)
    assert w.problem is None and w.states[-1] == "inactive"


def test_failed_state_shows_translated_service_error(russian):
    w = poll_target()
    Window.apply_status(w, {"state": "failed", "error": "The tunnel did not come up in 10 seconds"})
    assert w.error == "Туннель не поднялся за 10 секунд" and w.states == ["failed"]
    Window.apply_status(w, {"state": "failed", "error": "FATAL[0000] open tun: denied"})
    assert w.error == "FATAL[0000] open tun: denied"  # core messages stay as they are


def test_apply_status_takes_what_service_runs():
    w = poll_target()
    running = {"profile_id": "A", "server_ip": "203.0.113.10", "bypass_ips": ["203.0.113.5"]}
    Window.apply_status(w, {"state": "active", "running": running})
    assert w.running_id == "A" and w.bypass == {"203.0.113.10", "203.0.113.5"}


def test_banner_for_snap_permissions_has_the_exact_command(monkeypatch):
    monkeypatch.setattr(app, "SNAP_NAME", "vless-vpn")
    w = SimpleNamespace(problem="plugs", plugs=["network-control", "firewall-control"])
    title, button = Window.banner_text(w)
    cmd = "sudo snap connect vless-vpn:network-control && sudo snap connect vless-vpn:firewall-control"
    assert cmd in title and button
    assert Window.banner_text(SimpleNamespace(problem=None, plugs=[])) == ("", "")


def test_ping_measured_during_restart_is_dropped(monkeypatch):
    pending = []
    monkeypatch.setattr(app, "bg", lambda fn, *a, done=None: pending.append(done))
    link = "vless://11111111-2222-4333-8444-555555555555@203.0.113.10:443#A"
    shown = []
    w = SimpleNamespace(
        busy=None, state="inactive", seq=0, pings={}, via_tunnel=set(), bypass=set(), alive=True,
        store=SimpleNamespace(
            profiles=[SimpleNamespace(id="A", parsed=lambda: app.links.parse(link))], selected="A"
        ),
        show_ping=shown.append, update_status=lambda: None, check_exit=lambda fresh: None,
    )
    Window.refresh_pings(w)
    w.seq = 1  # a restart happened while measuring
    pending[0](("203.0.113.10", 1093.0), None)
    assert w.pings == {} and shown == []

    Window.refresh_pings(w)  # a measurement after the operation is taken
    pending[1](("203.0.113.10", 52.0), None)
    assert w.pings == {"A": 52.0}

    w.busy = "Switching…"
    assert Window.refresh_pings(w) is True and len(pending) == 2  # no measuring during an operation


def test_broadway_run_never_takes_the_real_app_id():
    code = "from vless_vpn import app; print(app.APP_ID)"
    env = {**os.environ, "GDK_BACKEND": "broadway"}
    env.pop("VLESS_VPN_APP_ID", None)
    out = subprocess.run(
        ["/usr/bin/python3", "-c", code], env=env, capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    ).stdout.strip()
    assert out == APP_ID + "Test"


def test_cli_version_and_import(tmp_path, monkeypatch, capsys):
    from .test_links import REALITY

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert app.main(["vless-vpn", "--version"]) == 0
    assert "VLESS VPN 1.0.0" in capsys.readouterr().out
    links_file = tmp_path / "links.txt"
    links_file.write_text(REALITY + "\nvless://bad@x:1\n")
    assert app.main(["vless-vpn", "--import", str(links_file)]) == 0
    out = capsys.readouterr()
    assert "added: Home" in out.out and "skipped:" in out.err


def test_system_language_comes_back_after_a_chosen_one(monkeypatch):
    monkeypatch.setattr(app, "SYSTEM_LANGUAGE_ENV", None)
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert app.apply_language("ru") == "ru"
    assert app.apply_language("system") == "en" and "LANGUAGE" not in os.environ
    monkeypatch.setattr(app, "SYSTEM_LANGUAGE_ENV", "ru:en")
    assert app.apply_language("system") == "ru"
    app.apply_language("en")


def test_replaced_window_ignores_late_answers():
    w = poll_target(alive=False, problem="missing")
    Window.on_poll(w, ({"api": 1, "problems": []}, {"state": "active"}), None, 0)
    assert w.problem == "missing" and w.states == []


def test_import_of_unreadable_file_explains_instead_of_crashing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert app.main(["vless-vpn", "--import", str(tmp_path / "missing.txt")]) == 1
    assert "cannot read" in capsys.readouterr().err
