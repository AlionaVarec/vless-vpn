"""The list of configs (profiles.json) and app settings (settings.json), both mode 600.

Files live in $XDG_CONFIG_HOME/vless-vpn, which is ~/.config/vless-vpn on a normal install
and ~/snap/vless-vpn/current/.config/vless-vpn inside the snap.
"""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import links
from .i18n import _


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vless-vpn"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@dataclass
class Profile:
    id: str
    name: str
    link: str

    def parsed(self) -> links.VlessLink:
        return links.parse(self.link)


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "profiles.json"
        self.profiles: list[Profile] = []
        self.selected: str | None = None
        self.load()

    def load(self) -> None:
        """Re-reads the file: `vless-vpn --import` may have changed it while the window is open."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except ValueError:
            # a broken file must not crash the window or get lost: move it aside
            os.replace(self.path, self.path.with_suffix(".broken"))
            self.profiles, self.selected = [], None
            return
        self.profiles = [Profile(**p) for p in data.get("profiles", [])]
        self.selected = data.get("selected")
        if not self.get(self.selected) and self.profiles:
            self.selected = self.profiles[0].id

    def save(self) -> None:
        data = {"selected": self.selected, "profiles": [asdict(p) for p in self.profiles]}
        _write_json(self.path, data)

    def get(self, pid: str | None) -> Profile | None:
        return next((p for p in self.profiles if p.id == pid), None)

    @property
    def current(self) -> Profile | None:
        return self.get(self.selected)

    def add_from_text(self, text: str) -> tuple[list[Profile], list[str]]:
        """Adds every vless:// link found in the text. Returns (added, errors)."""
        self.load()
        added, errors = [], []
        known = {p.link for p in self.profiles}
        found = links.find_links(text)
        if not found:
            errors.append(_("no vless:// links in the text"))
        for raw in found:
            try:
                parsed = links.parse(raw)
            except links.LinkError as e:
                errors.append(f"{raw[:40]}…: {e}")
                continue
            if raw in known:
                errors.append(_("“{name}” is already in the list").format(name=parsed.name))
                continue
            p = Profile(id=uuid.uuid4().hex[:12], name=parsed.name, link=raw)
            self.profiles.append(p)
            known.add(raw)
            added.append(p)
        if added:
            if not self.current:
                self.selected = added[0].id
            self.save()
        return added, errors

    def remove(self, pid: str) -> None:
        self.load()
        self.profiles = [p for p in self.profiles if p.id != pid]
        if self.selected == pid:
            self.selected = self.profiles[0].id if self.profiles else None
        self.save()

    def select(self, pid: str) -> None:
        self.load()
        self.selected = pid
        self.save()


class Settings:
    """App settings. For now only the interface language: "system", "en" or "ru"."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "settings.json"
        self.language = "system"
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("language") in ("system", "en", "ru"):
            self.language = data["language"]

    def save(self) -> None:
        _write_json(self.path, {"language": self.language})
