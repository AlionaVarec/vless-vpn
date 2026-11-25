"""The VLESS VPN window (libadwaita 1.5 or newer)."""

import os
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID as MAIN_APP_ID  # noqa: E402
from . import APP_NAME, ISSUES, WEBSITE, __version__, client, control, i18n, links, net, singbox  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .store import Settings, Store  # noqa: E402

# A test run under Broadway gets its own ID: the app is single-window, and an invisible test
# copy would otherwise catch the launch from the app menu.
APP_ID = os.environ.get("VLESS_VPN_APP_ID") or (
    MAIN_APP_ID + "Test" if os.environ.get("GDK_BACKEND") == "broadway" else MAIN_APP_ID
)
IN_SNAP = bool(os.environ.get("SNAP"))
SNAP_NAME = os.environ.get("SNAP_INSTANCE_NAME") or "vless-vpn"
DEVELOPER = "Aliona Varec"
POLL_MS = 1500
HELLO_EVERY = 20  # polls between checks of the service version and snap permissions
PING_EVERY_S = 30
SOURCE_ICONS = Path(__file__).resolve().parent.parent / "data" / "icons"
SYSTEM_LANGUAGE_ENV = os.environ.get("LANGUAGE")  # before we change it for a chosen language

CSS = """
.vpn-power { min-width: 136px; min-height: 136px; padding: 0; }
.vpn-power.on { background-color: @success_bg_color; color: @success_fg_color; }
.ping { min-width: 64px; }
"""


def esc(text: str) -> str:
    return GLib.markup_escape_text(text)


def bg(fn, *args, done=None):
    """fn(*args) in a thread, done(result, error) back in the main loop."""

    def run():
        try:
            res, err = fn(*args), None
        except Exception as e:  # noqa: BLE001 - every error is shown in the window
            res, err = None, e
        if done:
            GLib.idle_add(lambda: done(res, err) and False)

    threading.Thread(target=run, daemon=True).start()


def spinner() -> Gtk.Widget:
    if hasattr(Adw, "Spinner"):  # libadwaita 1.6
        return Adw.Spinner(width_request=48, height_request=48)
    return Gtk.Spinner(spinning=True, width_request=48, height_request=48)


def button_row(title: str, icon: str) -> Gtk.ListBoxRow:
    if hasattr(Adw, "ButtonRow"):  # libadwaita 1.6
        return Adw.ButtonRow(title=title, start_icon_name=icon)
    row = Adw.ActionRow(title=title, activatable=True)
    row.add_prefix(Gtk.Image(icon_name=icon))
    return row


def snap_connect_command(plugs) -> str:
    return " && ".join(f"sudo snap connect {SNAP_NAME}:{p}" for p in plugs)


class AddDialog(Adw.Dialog):
    """Paste vless:// links as text, from the clipboard or from a file."""

    def __init__(self, on_submit):
        super().__init__(title=_("New Config"), content_width=480)
        self.on_submit = on_submit

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_a: self.close())
        self.add_btn = Gtk.Button(label=_("Add"), css_classes=["suggested-action"], sensitive=False)
        self.add_btn.connect("clicked", self._submit)
        header.pack_start(cancel)
        header.pack_end(self.add_btn)

        hint = Gtk.Label(
            label=_("Paste a vless:// link. You can add several, one per line."),
            wrap=True,
            xalign=0,
        )
        self.text = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.CHAR,
            monospace=True,
            top_margin=10,
            bottom_margin=10,
            left_margin=10,
            right_margin=10,
        )
        # long links wrap by characters; without this tag GTK adds hyphens that are not in the link
        buf = self.text.get_buffer()
        self.no_hyphens = buf.create_tag(None, insert_hyphens=False)
        buf.connect("changed", self._changed)
        scroll = Gtk.ScrolledWindow(
            child=self.text, min_content_height=150, css_classes=["card"], overflow=Gtk.Overflow.HIDDEN
        )
        paste = Gtk.Button(label=_("Paste from Clipboard"), hexpand=True)
        paste.connect("clicked", self._paste)
        from_file = Gtk.Button(label=_("Open File…"), hexpand=True)
        from_file.connect("clicked", self._open_file)
        buttons = Gtk.Box(spacing=12, homogeneous=True)
        buttons.append(paste)
        buttons.append(from_file)
        self.error = Gtk.Label(css_classes=["error"], wrap=True, xalign=0, visible=False)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        for w in (hint, scroll, buttons, self.error):
            box.append(w)
        view = Adw.ToolbarView(content=box)
        view.add_top_bar(header)
        self.set_child(view)
        self.set_focus(self.text)

    def _value(self) -> str:
        buf = self.text.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _changed(self, buf):
        buf.apply_tag(self.no_hyphens, buf.get_start_iter(), buf.get_end_iter())
        self.add_btn.set_sensitive(bool(self._value().strip()))
        self.error.set_visible(False)

    def _paste(self, *_a):
        clipboard = self.get_clipboard()

        def done(cb, res):
            try:
                text = cb.read_text_finish(res) or ""
            except GLib.Error:
                text = ""
            if text.strip():
                self.text.get_buffer().set_text(text.strip())
            else:
                self._show_error(_("The clipboard has no text"))

        clipboard.read_text_async(None, done)

    def _open_file(self, *_a):
        dialog = Gtk.FileDialog(title=_("File with vless:// Links"))

        def done(d, res):
            try:
                f = d.open_finish(res)
                _ok, data, _etag = f.load_contents(None)
            except GLib.Error:
                return  # the file chooser was closed
            self.text.get_buffer().set_text(data.decode(errors="replace").strip())

        dialog.open(self.get_root(), None, done)

    def _show_error(self, msg: str):
        self.error.set_label(msg)
        self.error.set_visible(True)

    def _submit(self, *_a):
        errors = self.on_submit(self._value())
        if errors is None:
            self.close()
        else:
            self._show_error("\n".join(errors))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=440, default_height=700)
        self.store = Store()
        self.state = "unknown"  # what the service reports: active, inactive, failed, ...
        self.problem = None  # None, or why the VPN cannot work: missing, outdated, plugs, forbidden
        self.plugs: list[str] = []  # snap permissions that are not connected
        self.polls = 0
        self.busy = None  # text of the running operation: connect, switch, disconnect
        self.seq = 0  # number of the last finished operation: a poll started before it is stale
        self.polling = False
        self.checking = False
        self.exit_gen = 0  # number of the exit check: results of older ones are dropped
        self.pings: dict[str, float | None] = {}  # profile id -> ms, None = no answer
        self.via_tunnel: set[str] = set()  # this ping would go through the tunnel, so it lies
        self.exit: dict | None | bool = None  # exit to the internet: dict, False (none), None (unknown)
        self.error = ""
        self.running_id = None  # the config the service actually runs
        self.bypass: set[str] = set()  # the IPs it sends around the tunnel
        self.pending_switch = False  # a choice made in this window is not applied yet
        self.rows: dict[str, tuple] = {}
        self.alive = True  # False once replaced (language switch): late callbacks do nothing
        # invisible leader of the radio group: even a single config is drawn as a radio button;
        # keep the reference, or Python frees it and GTK drops it from the group
        self.group_leader = Gtk.CheckButton()

        self.toasts = Adw.ToastOverlay()
        self.banner = Adw.Banner(use_markup=True)
        self.banner.connect("button-clicked", self.on_banner_button)

        self.power_icon = Gtk.Image(icon_name="network-vpn-symbolic", pixel_size=64)
        self.power_stack = Gtk.Stack()
        self.power_stack.add_named(self.power_icon, "icon")
        self.power_stack.add_named(spinner(), "spin")
        self.power = Gtk.Button(
            child=self.power_stack, halign=Gtk.Align.CENTER, css_classes=["circular", "vpn-power"]
        )
        self.power.connect("clicked", self.on_power)

        self.title = Gtk.Label(css_classes=["title-1"], margin_top=18)
        self.subtitle = Gtk.Label(css_classes=["heading"], wrap=True, justify=Gtk.Justification.CENTER)
        self.detail = Gtk.Label(
            css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER, selectable=True
        )

        refresh = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text=_("Refresh Ping"), css_classes=["flat"]
        )
        refresh.connect("clicked", lambda *_a: self.refresh_pings())
        group = Adw.PreferencesGroup(title=_("Configs"), margin_top=30, header_suffix=refresh)
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.list.connect("row-activated", self.on_row_activated)
        group.add(self.list)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            margin_top=30,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        for w in (self.power, self.title, self.subtitle, self.detail, group):
            box.append(w)
        scroll = Gtk.ScrolledWindow(
            child=Adw.Clamp(child=box, maximum_size=520), hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        header = Adw.HeaderBar()
        header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic",
                menu_model=app.menu(),
                primary=True,
                tooltip_text=_("Main Menu"),
            )
        )
        view = Adw.ToolbarView(content=scroll)
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.build_rows()
        self.update_status()
        self.poll()
        self.refresh_pings()
        self.timers = [
            GLib.timeout_add(POLL_MS, self.poll),
            GLib.timeout_add_seconds(PING_EVERY_S, self.refresh_pings),
        ]
        self.connect("destroy", self.on_destroy)

    def on_destroy(self, *_a):
        self.alive = False
        for source in self.timers:
            GLib.source_remove(source)
        self.timers = []

    # ---------- config list ----------

    def build_rows(self):
        self.list.remove_all()
        self.rows = {}
        for p in self.store.profiles:
            try:
                ln = p.parsed()
                sub = f"{ln.host}:{ln.port} · {ln.network} · {ln.security}"
            except links.LinkError as e:
                sub = _("link error: {error}").format(error=e)
            row = Adw.ActionRow(title=esc(p.name), subtitle=esc(sub))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self.group_leader)
            check.set_active(p.id == self.store.selected)
            check.connect("toggled", self.on_select, p.id)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            ping = Gtk.Label(css_classes=["ping", "numeric"], xalign=1)
            row.add_suffix(ping)
            delete = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat"],
                tooltip_text=_("Delete"),
            )
            delete.connect("clicked", self.on_delete, p.id)
            row.add_suffix(delete)
            self.list.append(row)
            self.rows[p.id] = (row, check, ping, delete)
            self.show_ping(p.id)

        self.add_row = button_row(_("Add Config"), "list-add-symbolic")
        self.list.append(self.add_row)
        self.update_rows_sensitivity()

    def on_row_activated(self, _list, row):
        if row is self.add_row:
            AddDialog(self.add_links).present(self)

    def add_links(self, text: str) -> list[str] | None:
        added, errors = self.store.add_from_text(text)
        if not added:
            return errors
        self.build_rows()
        self.update_status()
        self.refresh_pings()
        msg = ngettext("Added {n} config", "Added {n} configs", len(added)).format(n=len(added))
        if errors:
            msg += ", " + _("skipped: {n}").format(n=len(errors))
        self.toast(msg)
        return None

    def on_select(self, check, pid):
        if not check.get_active() or pid == self.store.selected:
            return
        self.store.select(pid)
        if self.busy or self.state in ("active", "activating"):
            # another operation may be running: reconcile() applies the choice after it
            self.pending_switch = True
            self.reconcile()
        self.update_status()

    def on_delete(self, _btn, pid):
        p = self.store.get(pid)
        dialog = Adw.AlertDialog(
            heading=_("Delete “{name}”?").format(name=p.name),
            body=_("You can add the link again later."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_d, response):
            if response == "delete":
                self.store.remove(pid)
                self.pings.pop(pid, None)
                self.build_rows()
                self.update_status()

        dialog.connect("response", done)
        dialog.present(self)

    def update_rows_sensitivity(self):
        # while the VPN is on, the running and the selected configs cannot be deleted
        on = self.state in ("active", "activating") or bool(self.busy)
        locked = {self.running_id, self.store.selected} if on else set()
        for pid, (_row, _check, _ping, delete) in self.rows.items():
            delete.set_sensitive(pid not in locked)

    # ---------- ping ----------

    def refresh_pings(self):
        if self.busy:
            return True  # a ping during reconnection lies; measure again after the operation
        active = self.state == "active"
        seq = self.seq
        if active:
            self.check_exit(fresh=False)
        for p in self.store.profiles:
            try:
                ln = p.parsed()
            except links.LinkError:
                continue

            def measure(host=ln.host, port=ln.port):
                ip = net.resolve(host)
                return ip, net.tcp_ping(ip, port)

            def done(res, err, pid=p.id):
                if not self.alive or self.busy or seq != self.seq:
                    return  # the service restarted while measuring: a SYN may have been lost
                ip, ms = res if res else (None, None)
                # with the VPN on, a server outside the bypass list is pinged through the tunnel
                if active and ip and ip not in self.bypass:
                    self.via_tunnel.add(pid)
                else:
                    self.via_tunnel.discard(pid)
                self.pings[pid] = ms
                self.show_ping(pid)
                if pid == self.store.selected:
                    self.update_status()

            bg(measure, done=done)
        return True

    def ping_text(self, pid) -> tuple[str, str]:
        if pid in self.via_tunnel:
            return _("n/a"), "dim-label"
        if pid not in self.pings:
            return "…", "dim-label"
        ms = self.pings[pid]
        if ms is None:
            return _("no answer"), "error"
        level = "success" if ms < 120 else "warning" if ms < 300 else "error"
        return _("{ms} ms").format(ms=f"{ms:.0f}"), level

    def show_ping(self, pid):
        if pid not in self.rows:
            return
        label = self.rows[pid][2]
        text, cls = self.ping_text(pid)
        label.set_label(text)
        label.set_css_classes(["ping", "numeric", cls])
        label.set_tooltip_text(
            _("Ping will appear after the VPN reconnects")
            if pid in self.via_tunnel
            else _("TCP ping of the server")
        )

    # ---------- service ----------

    def fetch(self, hello: bool):
        """Runs in a thread: the service version check (sometimes) and its state."""
        info = client.hello() if hello else None
        return info, client.status()

    def poll(self):
        if not self.polling and not self.busy:
            self.polling = True
            seq = self.seq
            hello = self.problem is not None or self.polls % HELLO_EVERY == 0
            self.polls += 1
            bg(self.fetch, hello, done=lambda res, err: self.on_poll(res, err, seq))
        return True

    def on_poll(self, res, err, seq):
        self.polling = False
        if not self.alive or self.busy or seq != self.seq:
            return  # the button was pressed while polling: the answer is stale
        if err:
            self.problem = self.problem_of(err)
            self.set_state("unknown")
            return
        info, status = res
        if info is not None:
            self.plugs = list(info.get("problems") or [])
            if info.get("api") != control.API:
                self.problem = "outdated"
            elif self.plugs:
                self.problem = "plugs"
            else:
                self.problem = None
        self.apply_status(status)
        self.reconcile()

    def problem_of(self, err) -> str | None:
        if isinstance(err, client.Unavailable):
            return "missing"
        if isinstance(err, client.ServiceError) and err.code == "forbidden":
            return "forbidden"
        return self.problem

    def apply_status(self, status: dict, recheck=False):
        running = status.get("running") or {}
        self.running_id = running.get("profile_id")
        self.bypass = set(running.get("bypass_ips") or []) | {running.get("server_ip")}
        state = status.get("state", "unknown")
        if state == "failed" and status.get("error"):
            self.error = _(status["error"])  # messages of the service are translated by msgid
        self.set_state(state, recheck)

    def set_state(self, state, recheck=False):
        """recheck: after our own operation; a switch keeps "active", but the server changed."""
        prev, self.state = self.state, state
        if state == "active" and (prev != "active" or recheck):
            self.error = ""
            self.check_exit(fresh=True)
        if state != "active":
            self.exit = None
            self.exit_gen += 1  # an unfinished check is not needed anymore
        if prev != state or recheck:
            self.refresh_pings()
        self.update_status()

    def check_exit(self, fresh=True):
        """The public IP seen through the system, so through the tunnel. fresh: right after
        connecting, several tries; a periodic check (fresh=False) does not interrupt a running one."""
        if self.checking and not fresh:
            return
        self.exit_gen += 1
        gen = self.exit_gen
        self.checking = True
        if fresh:
            self.exit = None
        delays = (0, 1.5, 3) if fresh else (0, 2)

        def probe():
            for i, delay in enumerate(delays):
                time.sleep(delay)
                try:
                    return net.exit_info(timeout=6)
                except OSError:
                    if i == len(delays) - 1:
                        raise

        def done(info, err):
            if not self.alive or gen != self.exit_gen:
                return
            self.checking = False
            if self.state == "active":
                self.exit = info if info else False
                self.update_status()

        bg(probe, done=done)

    def on_power(self, *_a):
        if self.busy:
            return
        if self.state in ("active", "activating"):
            self.disconnect_vpn()
        else:
            self.connect_vpn()

    def run_action(self, label, work):
        """work() runs in a thread and returns the fresh state of the service."""
        self.busy = label
        self.error = ""
        self.update_status()

        def done(status, err):
            if not self.alive:
                return  # the new window learns the result from the service
            self.busy = None
            self.seq += 1
            if err:
                self.pending_switch = False  # no endless retries: pick the config again
                self.error = str(err)
                self.toast(_("Failed: {error}").format(error=err))
                self.update_status()
                self.poll()
                return
            self.apply_status(status, recheck=True)
            self.reconcile()

        bg(work, done=done)

    def reconcile(self):
        """A choice made in this window during another operation is applied to the service."""
        if not self.pending_switch or self.busy:
            return
        if self.state not in ("active", "activating") or self.running_id == self.store.selected:
            self.pending_switch = False
            return
        self.connect_vpn()

    def connect_vpn(self):
        p = self.store.current
        if not p:
            self.toast(_("Add a config first"))
            return
        try:
            link = p.parsed()
        except links.LinkError as e:
            self.toast(_("Link error: {error}").format(error=e))
            return
        hosts = []
        for q in self.store.profiles:
            try:
                hosts.append(q.parsed().host)
            except links.LinkError:
                pass

        def work():
            try:
                server_ip = net.resolve(link.host)
            except OSError:
                raise RuntimeError(_("Cannot find the address of {host}").format(host=link.host)) from None
            bypass = set()
            for h in hosts:
                try:
                    bypass.add(net.resolve(h))
                except OSError:
                    pass
            return client.connect(singbox.make_request(p.id, p.link, server_ip, bypass))

        on = self.state in ("active", "activating")
        self.run_action(_("Switching…") if on else _("Connecting…"), work)

    def disconnect_vpn(self):
        self.run_action(_("Disconnecting…"), client.disconnect)

    # ---------- view ----------

    def banner_text(self) -> tuple[str, str]:
        """Banner title and button label for the current problem."""
        if self.problem == "plugs":
            return (
                _("The VPN needs network permissions. Run in a terminal: {command}").format(
                    command=snap_connect_command(self.plugs)
                ),
                _("Copy Command"),
            )
        if self.problem == "missing":
            if IN_SNAP:
                return _("The VPN service is not running. Run in a terminal: {command}").format(
                    command=f"sudo snap restart {SNAP_NAME}"
                ), ""
            return _("The VPN service is not installed. Run install.sh from the project folder."), ""
        if self.problem == "outdated":
            return _("The VPN service is from another version. Run install.sh again."), ""
        if self.problem == "forbidden":
            return _("Only administrators can use the VPN"), ""
        return "", ""

    def on_banner_button(self, *_a):
        if self.problem == "plugs":
            self.get_clipboard().set(snap_connect_command(self.plugs))
            self.toast(_("Command copied"))

    def update_status(self):
        title, button = self.banner_text()
        self.banner.set_title(esc(title))
        self.banner.set_button_label(button or None)
        self.banner.set_revealed(bool(title))
        ready = self.problem is None
        p = self.store.current
        st = self.state
        spinning = bool(self.busy) or st in ("activating", "deactivating")
        self.power_stack.set_visible_child_name("spin" if spinning else "icon")
        # the state is unknown before the first answer of the service: no guessing
        self.power.set_sensitive(ready and p is not None and not self.busy and st != "unknown")
        on = st in ("active", "activating")
        if on:
            self.power.add_css_class("on")
        else:
            self.power.remove_css_class("on")
        self.power.set_tooltip_text(_("Disconnect") if on else _("Connect"))

        if self.busy:
            title = self.busy
        elif st == "active":
            title = _("Connected")
        elif st == "activating":
            title = _("Connecting…")
        elif st == "deactivating":
            title = _("Disconnecting…")
        elif st == "failed":
            title = _("Connection Error")
        elif st == "unknown" and self.problem != "missing":
            title = _("Status Unknown")  # the service is slow or refused us: the VPN may still be on
        else:
            title = _("Disconnected")
        self.title.set_label(title)

        if on and self.store.get(self.running_id):
            p = self.store.get(self.running_id)  # show what really runs
        if p:
            ping, _cls = self.ping_text(p.id)
            self.subtitle.set_label(_("{name} · ping {ping}").format(name=p.name, ping=ping))
        else:
            self.subtitle.set_label(_("No configs yet. Add a vless:// link."))

        if self.error:
            detail = self.error
        elif st == "active" and not self.busy:
            if self.exit is None:
                detail = _("Checking internet access…")
            elif self.exit is False:
                detail = _("The VPN is on, but there is no internet through it")
            else:
                detail = _("Your IP: {ip} · {country}").format(ip=self.exit["ip"], country=self.exit["loc"])
        else:
            detail = ""
        self.detail.set_label(detail)
        self.detail.set_visible(bool(detail))
        self.update_rows_sensitivity()

    def toast(self, msg: str):
        self.toasts.add_toast(Adw.Toast(title=esc(msg), timeout=4))


class App(Adw.Application):
    def __init__(self, settings: Settings):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = settings
        GLib.set_application_name(APP_NAME)

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if SOURCE_ICONS.is_dir():  # running from the source tree
            Gtk.IconTheme.get_for_display(display).add_search_path(str(SOURCE_ICONS))
        Gtk.Window.set_default_icon_name(MAIN_APP_ID)

        language = Gio.SimpleAction.new_stateful(
            "language", GLib.VariantType.new("s"), GLib.Variant("s", self.settings.language)
        )
        language.connect("change-state", self.on_language)
        self.add_action(language)
        for name, handler in (("about", self.on_about), ("quit", lambda *_a: self.quit())):
            action = Gio.SimpleAction(name=name)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])
        self.set_accels_for_action("window.close", ["<Ctrl>w"])

    def menu(self) -> Gio.Menu:
        languages = Gio.Menu()
        languages.append(_("System"), "app.language::system")
        languages.append("English", "app.language::en")
        languages.append("Русский", "app.language::ru")
        menu = Gio.Menu()
        menu.append_submenu(_("Language"), languages)
        section = Gio.Menu()
        section.append(_("About VLESS VPN"), "app.about")
        menu.append_section(None, section)
        return menu

    def on_language(self, action, value):
        code = value.get_string()
        if code == self.settings.language:
            return
        action.set_state(value)
        self.settings.language = code
        self.settings.save()
        apply_language(code)
        # rebuild the window: every label is created in the new language
        old = self.get_active_window()
        Window(self).present()
        if old:
            old.destroy()

    def on_about(self, *_a):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=MAIN_APP_ID,
            version=__version__,
            developer_name=DEVELOPER,
            comments=_("A simple VPN client for VLESS servers. One button turns it on for the whole system."),
            website=WEBSITE,
            issue_url=ISSUES,
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 " + DEVELOPER,
        )
        about.add_link(_("sing-box Core"), "https://sing-box.sagernet.org/")
        about.present(self.get_active_window())

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


def apply_language(code: str) -> str:
    """Our strings come from i18n; LANGUAGE also switches the strings of GTK itself."""
    if code in i18n.LANGUAGES:
        os.environ["LANGUAGE"] = code
    elif SYSTEM_LANGUAGE_ENV is None:
        os.environ.pop("LANGUAGE", None)
    else:
        os.environ["LANGUAGE"] = SYSTEM_LANGUAGE_ENV
    return i18n.setup(code)


def import_file(path: str) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(1024 * 1024)
    except OSError as e:
        print(_("cannot read {path}: {error}").format(path=path, error=e.strerror or e), file=sys.stderr)
        return 1
    added, errors = Store().add_from_text(text)
    for p in added:
        print(_("added: {name}").format(name=p.name))
    for e in errors:
        print(_("skipped: {error}").format(error=e), file=sys.stderr)
    return 0 if added else 1


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    settings = Settings()
    apply_language(settings.language)
    if len(argv) == 2 and argv[1] == "--version":
        print(f"{APP_NAME} {__version__}")
        return 0
    if len(argv) == 3 and argv[1] == "--import":
        return import_file(argv[2])
    return App(settings).run(argv)

