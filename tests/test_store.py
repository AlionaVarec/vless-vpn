from vless_vpn.store import Settings, Store, config_dir

from .test_links import REALITY, UID

OTHER = f"vless://{UID}@203.0.113.5:443?security=tls#Second"


def test_add_select_remove_persist(tmp_path):
    path = tmp_path / "profiles.json"
    s = Store(path)
    added, errors = s.add_from_text(f"{REALITY}\n{OTHER}\nvless://bad@x:1")
    assert [p.name for p in added] == ["Home", "Second"]
    assert len(errors) == 1 and "UUID" in errors[0]
    assert s.current.name == "Home"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    s.select(added[1].id)
    again = Store(path)
    assert again.current.name == "Second"

    again.remove(added[1].id)
    assert [p.name for p in Store(path).profiles] == ["Home"]
    assert Store(path).current.name == "Home"


def test_duplicates_and_empty_text(tmp_path):
    s = Store(tmp_path / "p.json")
    s.add_from_text(REALITY)
    added, errors = s.add_from_text(REALITY)
    assert added == [] and "already in the list" in errors[0]
    added, errors = s.add_from_text("just some text")
    assert added == [] and "no vless:// links" in errors[0]
    assert len(s.profiles) == 1


def test_errors_in_russian(tmp_path, russian):
    s = Store(tmp_path / "p.json")
    s.add_from_text(REALITY)
    assert "«Home» уже есть в списке" in s.add_from_text(REALITY)[1][0]


def test_reload_keeps_profiles_added_by_import_while_window_open(tmp_path):
    path = tmp_path / "p.json"
    window = Store(path)
    window.add_from_text(REALITY)
    Store(path).add_from_text(OTHER)  # vless-vpn --import while the window is open
    window.select(window.profiles[0].id)  # the window saves its own choice
    assert [p.name for p in Store(path).profiles] == ["Home", "Second"]


def test_broken_file_is_moved_aside_not_crashing(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{broken")
    s = Store(path)
    assert s.profiles == [] and s.current is None
    assert (tmp_path / "p.broken").read_text() == "{broken"


def test_config_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path / "vless-vpn"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_dir().parts[-2:] == (".config", "vless-vpn")


def test_settings_language(tmp_path):
    path = tmp_path / "settings.json"
    assert Settings(path).language == "system"
    s = Settings(path)
    s.language = "ru"
    s.save()
    assert Settings(path).language == "ru"
    path.write_text('{"language": "klingon"}')
    assert Settings(path).language == "system"
    path.write_text("[")
    assert Settings(path).language == "system"
