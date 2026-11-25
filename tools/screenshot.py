#!/usr/bin/python3
"""Screenshots of the real window with demo servers, for the README and the stores.

The VPN service, ping and the exit check are replaced with stand-ins, so nothing connects
anywhere. Needs a desktop session (Wayland or X11).

Usage: tools/screenshot.py --lang en|ru --scene connected|add --out file.png
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UID = "11111111-2222-4333-8444-555555555555"
PBK = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SERVERS = [  # documentation addresses (RFC 5737), no real servers
    ("Amsterdam", "203.0.113.10", 38.0),
    ("Frankfurt", "203.0.113.20", 46.0),
    ("Helsinki", "198.51.100.30", 61.0),
]


def demo_link(name: str, ip: str) -> str:
    return (
        f"vless://{UID}@{ip}:443?type=tcp&security=reality&flow=xtls-rprx-vision"
        f"&pbk={PBK}&fp=chrome&sni=www.example.com&sid=7b#{name}"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lang", default="en", choices=["en", "ru"])
    p.add_argument("--scene", default="connected", choices=["connected", "add"])
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vless-vpn-shot-")
    os.environ["VLESS_VPN_APP_ID"] = "io.github.AlionaVarec.VlessVpnScreenshot"
    os.environ["LANGUAGE"] = args.lang

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Graphene, Gtk

    from vless_vpn import app, client, net, store

    s = store.Store()
    s.add_from_text("\n".join(demo_link(n, ip) for n, ip, _ in SERVERS))
    st = store.Settings()
    st.language = args.lang
    st.save()

    first = s.profiles[0].id
    ips = [ip for _, ip, _ in SERVERS]
    client.hello = lambda: {"api": 1, "version": "demo", "problems": []}
    client.status = lambda: {
        "state": "active",
        "running": {"profile_id": first, "server_ip": ips[0], "bypass_ips": ips},
        "error": "",
    }
    net.resolve = lambda host: host
    net.tcp_ping = lambda ip, port, **kw: next(ms for _, i, ms in SERVERS if i == ip)
    net.exit_info = lambda timeout=0: {"ip": "203.0.113.10", "loc": "NL"}

    def shoot(win):
        w, h = win.get_width(), win.get_height()
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
        texture = win.get_native().get_renderer().render_texture(
            snap.to_node(), Graphene.Rect().init(0, 0, w, h)
        )
        texture.save_to_png(str(args.out))
        print(args.out)
        win.get_application().quit()

    def staged(win):
        if args.scene == "add":
            dialog = app.AddDialog(lambda text: None)
            dialog.text.get_buffer().set_text(demo_link("Warsaw", "198.51.100.40"))
            dialog.present(win)
        GLib.timeout_add(1500, lambda: shoot(win) and False)

    class ShotApp(app.App):
        def do_activate(self):
            app.App.do_activate(self)
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            win = self.get_active_window()
            win.set_default_size(440, 640)
            GLib.timeout_add(1500, lambda: staged(win) and False)

    app.apply_language(args.lang)
    return ShotApp(st).run(["vless-vpn"])


if __name__ == "__main__":
    sys.exit(main())
