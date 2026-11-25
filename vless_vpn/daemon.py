"""The privileged service: runs sing-box for the app.

It runs as root: as a systemd unit on a normal install and as a daemon inside the snap. It
listens on a unix socket and answers one JSON request per connection. Only root and members of
the admin groups may use it (checked with SO_PEERCRED), so another local account cannot route
this computer's traffic through a server of its choice.

The app never sends a sing-box config. It sends a link and resolved addresses, and the service
builds the config itself with singbox.from_request(). The service writes nothing outside its
work directory and logs no links or UUIDs.
"""

import argparse
import grp
import json
import os
import pwd
import re
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from . import __version__, control, links, resolved, singbox
from .i18n import N_

ADMIN_GROUPS = ("sudo", "admin", "wheel", "vless-vpn")
READY_TIMEOUT = 10  # seconds for routes and DNS of the tunnel to appear
STOP_TIMEOUT = 5
RESTART_DELAY = 5  # after sing-box crashed while connected
RESTART_LIMIT = 5  # crashes within RESTART_WINDOW, then give up and report the error
RESTART_WINDOW = 60
SNAP_PLUGS = ("network-control", "firewall-control")
READ_DEADLINE = 5  # seconds for the whole request, not per byte
MAX_CLIENTS = 16  # connections handled at once; more are closed at once

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def log(message: str) -> None:
    print(f"vless-vpnd: {message}", file=sys.stderr, flush=True)


# ---------- who may talk to the service ----------


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def uid_allowed(uid: int, groups=ADMIN_GROUPS) -> bool:
    """Root and members of the admin groups. Plain accounts (a guest, a child) may not."""
    if uid == 0:
        return True
    try:
        user = pwd.getpwuid(uid)
    except KeyError:
        return False
    member_of = set(os.getgrouplist(user.pw_name, user.pw_gid))
    for name in groups:
        try:
            if grp.getgrnam(name).gr_gid in member_of:
                return True
        except KeyError:
            continue
    return False


# ---------- is the tunnel ready ----------


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None


def routes_ready() -> bool:
    r = _run(["ip", "rule", "show"])
    return bool(r) and r.returncode == 0 and f"lookup {singbox.ROUTE_TABLE}" in r.stdout


_dns_warned = False


def _resolvectl_says_ready() -> bool:
    """Whether sing-box itself pointed systemd-resolved at the tunnel (it runs resolvectl).

    True when it cannot be checked, so a broken check never blocks the VPN.
    """
    if not shutil.which("resolvectl"):
        return True
    r = _run(["resolvectl", "dns", singbox.TUN_NAME])
    if r is None:
        return True
    if r.returncode == 0:
        return singbox.TUN_DNS in r.stdout
    return "not known" not in r.stderr and "No such" not in r.stderr


def dns_ready() -> bool:
    """True once systemd-resolved sends every lookup of the system into the tunnel.

    The service sets this itself over D-Bus. Until then a query to the home router would go
    around the tunnel, because local networks are excluded from it.
    """
    global _dns_warned
    if not Path("/run/systemd/resolve").is_dir():
        return True  # no systemd-resolved: DNS packets enter the tunnel and sing-box catches them
    try:
        index = socket.if_nametoindex(singbox.TUN_NAME)
    except OSError:
        return False  # the interface is not there yet
    try:
        resolved.route_all_dns(index, singbox.TUN_DNS)
        return True
    except resolved.BusError as e:
        if "NoSuchLink" in str(e):
            return False  # resolved has not seen the new interface yet
        error = e
    except OSError as e:
        error = e
    if not _dns_warned:
        _dns_warned = True
        log(f"cannot set DNS of the tunnel: {error}")
    return _resolvectl_says_ready()


def tunnel_ready() -> bool:
    return routes_ready() and dns_ready()


def last_error(lines) -> str:
    clean = [_ANSI.sub("", ln).strip() for ln in lines]
    clean = [ln for ln in clean if ln]
    fatal = [ln for ln in clean if "FATAL" in ln]
    errors = [ln for ln in clean if "ERROR" in ln]
    return (fatal or errors or clean or [""])[-1]


# ---------- sing-box process ----------


class Controller:
    """Starts, watches and stops one sing-box process."""

    def __init__(self, singbox_bin: str, work_dir: Path, ready=tunnel_ready, clock=time.monotonic):
        self.singbox_bin = singbox_bin
        self.work_dir = Path(work_dir)
        self.config_path = self.work_dir / "sing-box.json"
        self.ready = ready
        self.clock = clock
        self.op = threading.Lock()  # one connect, disconnect or restart at a time
        self.mu = threading.Lock()  # guards the fields below, held only briefly
        self.state = "inactive"
        self.proc: subprocess.Popen | None = None
        self.pump: threading.Thread | None = None
        self.request: dict | None = None
        self.error = ""
        self.output: deque[str] = deque(maxlen=50)
        self.crashes: deque[float] = deque()
        self.gen = 0  # grows on every start and stop; stale watchers and restarts compare it

    def status(self) -> dict:
        with self.mu:
            on = self.state in ("activating", "active") and self.request
            running = singbox.running_state(self.request) if on else {}
            return {"state": self.state, "running": running, "error": self.error}

    def connect(self, request) -> dict:
        config = singbox.from_request(request)  # reject bad input before touching the tunnel
        with self.op:
            self._stop()
            with self.mu:
                self.request = request
                self.error = ""
                self.crashes.clear()
            self._start(config)
        return self.status()

    def disconnect(self) -> dict:
        with self.op:
            self._stop()
            with self.mu:
                self.request = None
                self.error = ""
        return self.status()

    # the methods below run with self.op held

    def _start(self, config: dict) -> None:
        singbox.write_private(config, self.config_path)
        with self.mu:
            self.gen += 1
            gen = self.gen
            self.state = "activating"
            self.output.clear()
        cmd = [self.singbox_bin, "run", "-D", str(self.work_dir), "-c", str(self.config_path)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as e:
            log(f"cannot start sing-box: {e}")
            with self.mu:
                self.state = "failed"
                self.error = N_("Cannot start the sing-box core")
            return
        pump = threading.Thread(target=self._pump, args=(proc, gen), daemon=True)
        with self.mu:
            self.proc, self.pump = proc, pump
        try:
            pump.start()
        except RuntimeError as e:  # no threads left: nobody would watch this sing-box
            log(f"cannot watch sing-box: {e}")
            with self.mu:
                self.gen += 1
                self.proc = None
                self.state = "failed"
                self.error = N_("Cannot start the sing-box core")
            self._kill(proc)
            return

        deadline = self.clock() + READY_TIMEOUT
        while self.clock() < deadline:
            if proc.poll() is not None:
                pump.join(2)  # let the watcher record the exit and its error
                return
            if self.ready():
                with self.mu:
                    if self.gen == gen and self.state == "activating":
                        self.state = "active"
                return
            time.sleep(0.1)
        log("the tunnel did not come up in time")
        with self.mu:
            self.gen += 1  # the watcher must not treat this kill as a crash
            self.proc = None
            self.state = "failed"
            self.error = N_("The tunnel did not come up in 10 seconds")
        self._kill(proc)

    def _stop(self) -> None:
        with self.mu:
            proc, self.proc = self.proc, None
            self.gen += 1
            if proc:
                self.state = "deactivating"
        if proc:
            self._kill(proc)
        self.config_path.unlink(missing_ok=True)  # it holds the UUID
        with self.mu:
            self.state = "inactive"

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()  # sing-box removes its routes and firewall rules on SIGTERM
            try:
                proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # watcher thread

    def _pump(self, proc: subprocess.Popen, gen: int) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # connection errors name the sites visited: keep them in memory only, not in the journal
            if "FATAL" in line:
                print(line, file=sys.stderr, flush=True)
            with self.mu:
                if gen == self.gen:  # lines of a replaced process must not become the new error
                    self.output.append(line)
        code = proc.wait()
        with self.mu:
            if gen != self.gen:
                return  # stopped on purpose or replaced
            self.proc = None
            self.error = last_error(self.output) or f"sing-box exited with code {code}"
            now = self.clock()
            self.crashes.append(now)
            while self.crashes and now - self.crashes[0] > RESTART_WINDOW:
                self.crashes.popleft()
            retry = self.state == "active" and len(self.crashes) <= RESTART_LIMIT
            self.state = "activating" if retry else "failed"
        log(f"sing-box exited with code {code}" + (", restarting" if retry else ""))
        if retry:
            timer = threading.Timer(RESTART_DELAY, self._restart, args=(gen,))
            timer.daemon = True
            timer.start()

    def _restart(self, gen: int) -> None:
        with self.op:
            with self.mu:
                if gen != self.gen or self.state != "activating" or not self.request:
                    return  # the user disconnected or switched meanwhile
                request = self.request
            try:
                self._start(singbox.from_request(request))
            except Exception as e:  # noqa: BLE001 - never leave the state stuck in "activating"
                log(f"restart failed: {e}")
                with self.mu:
                    self.state = "failed"


# ---------- socket server ----------


def snap_problems() -> list[str]:
    """Snap permissions that are not connected yet. Empty outside the snap."""
    if not os.environ.get("SNAP"):
        return []
    missing = []
    for plug in SNAP_PLUGS:
        r = _run(["snapctl", "is-connected", plug])
        if not r or r.returncode != 0:
            missing.append(plug)
    return missing


def read_request(sock: socket.socket) -> bytes:
    """One request line, within an overall deadline and size limit. Raises ValueError otherwise."""
    end = time.monotonic() + READ_DEADLINE
    data = b""
    while b"\n" not in data:
        left = end - time.monotonic()
        if left <= 0:
            raise ValueError("request too slow")
        sock.settimeout(left)
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            raise ValueError("request too slow") from None
        if not chunk:
            break
        data += chunk
        if len(data) > control.MAX_MESSAGE:
            raise ValueError("request too large")
    return data


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        ctl: Controller = self.server.controller
        try:
            uid = peer_uid(self.request)
        except OSError:
            return
        # refuse strangers before reading anything: they cannot tie up the service
        if not uid_allowed(uid):
            self.server.log_refusal(uid)
            return self.reply(False, code="forbidden", message=N_("Only administrators can use the VPN"))
        try:
            msg = json.loads(read_request(self.request))
            if not isinstance(msg, dict):
                raise ValueError
        except (OSError, ValueError, RecursionError):
            return self.reply(False, code="protocol", message=N_("The VPN service got a broken request"))

        cmd = msg.get("cmd")
        try:
            if cmd == "hello":
                return self.reply(True, api=control.API, version=__version__, problems=snap_problems())
            if cmd == "status":
                return self.reply(True, **ctl.status())
            if cmd == "connect":
                log(f"connect from uid {uid}")
                return self.reply(True, **ctl.connect(msg.get("request")))
            if cmd == "disconnect":
                log(f"disconnect from uid {uid}")
                return self.reply(True, **ctl.disconnect())
        except (singbox.RequestError, links.LinkError) as e:
            return self.reply(False, code="bad_request", message=str(e))
        except Exception as e:  # noqa: BLE001 - the app must always get an answer
            log(f"{cmd} failed: {type(e).__name__}: {e}")
            return self.reply(False, code="error", message=str(e) or type(e).__name__)
        return self.reply(False, code="protocol", message=N_("Unknown command"))

    def reply(self, ok: bool, **fields) -> None:
        try:
            self.request.settimeout(READ_DEADLINE)
            self.request.sendall(json.dumps({"ok": ok, **fields}).encode() + b"\n")
        except OSError:
            pass  # the app went away


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, controller: Controller):
        self.controller = controller
        self.slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self.refusals: dict[int, float] = {}
        if not path.parent.exists():
            path.parent.mkdir(parents=True)
            os.chmod(path.parent, 0o755)  # the app must reach the socket
        path.unlink(missing_ok=True)  # left over from a killed run
        super().__init__(str(path), Handler)
        # everyone may connect; Handler lets in only admins (SO_PEERCRED)
        os.chmod(path, 0o666)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # too many at once: drop instead of piling up threads
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def log_refusal(self, uid: int) -> None:
        """At most one line per user a minute, so refusals cannot flood the journal."""
        now = time.monotonic()
        if now - self.refusals.get(uid, -60.0) >= 60:
            self.refusals[uid] = now
            log(f"refused uid {uid}")


def default_paths() -> tuple[str, Path]:
    """sing-box binary and work directory: inside the snap, or the normal install."""
    snap = os.environ.get("SNAP")
    if snap:
        return f"{snap}/bin/sing-box", Path(os.environ["SNAP_DATA"])
    return "/usr/local/lib/vless-vpn/sing-box", Path("/var/lib/vless-vpn")


def main(argv=None) -> int:
    singbox_bin, work_dir = default_paths()
    p = argparse.ArgumentParser(prog="vless-vpnd", description="VLESS VPN privileged service")
    p.add_argument("--singbox", default=singbox_bin)
    p.add_argument("--work-dir", type=Path, default=work_dir)
    p.add_argument("--socket", type=Path, default=control.socket_path())
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    if os.geteuid() != 0:
        log("must run as root")
        return 1
    os.umask(0o077)
    args.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    controller = Controller(args.singbox, args.work_dir)
    controller.config_path.unlink(missing_ok=True)  # left by a killed run; it holds the UUID
    server = Server(args.socket, controller)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"{__version__} listening on {args.socket}")
    try:
        server.serve_forever()
    finally:
        controller.disconnect()
        server.server_close()
        args.socket.unlink(missing_ok=True)
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
