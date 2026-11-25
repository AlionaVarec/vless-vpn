import re
import subprocess
import sys
from pathlib import Path

import pytest

from vless_vpn import i18n

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from msgfmt import parse_po  # noqa: E402
from update_po import extract  # noqa: E402


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "en"),
        ({"LANG": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "de_DE.UTF-8"}, "en"),
        ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "ru_RU.UTF-8", "LC_ALL": "C"}, "en"),
        ({"LANGUAGE": "uk:ru:en", "LANG": "uk_UA.UTF-8"}, "ru"),
        ({"LANGUAGE": "", "LANG": "ru_RU.UTF-8"}, "ru"),
    ],
)
def test_system_language(env, expected):
    assert i18n.system_language(env) == expected


def test_setup_switches_back_and_forth(locale_dir):
    assert i18n.setup("ru", locale_dir) == "ru"
    assert i18n._("Connected") == "Подключено"
    assert i18n.setup("en", locale_dir) == "en"
    assert i18n._("Connected") == "Connected"


@pytest.mark.parametrize(
    "n, word",
    [
        (1, "Добавлен 1 конфиг"),
        (3, "Добавлено 3 конфига"),
        (11, "Добавлено 11 конфигов"),
        (22, "Добавлено 22 конфига"),
    ],
)
def test_russian_plurals(russian, n, word):
    assert i18n.ngettext("Added {n} config", "Added {n} configs", n).format(n=n) == word


def test_every_string_is_translated_to_russian():
    ru = parse_po((ROOT / "po" / "ru.po").read_text(), keep_empty=True)
    for msgid, plural in extract():
        key = msgid + ("\0" + plural if plural else "")
        assert ru.get(key, "").replace("\0", ""), f"no Russian for {msgid!r}"
        for form in ru[key].split("\0"):
            # the same {placeholders}, or .format() breaks in Russian
            assert set(re.findall(r"{\w+}", form)) == set(re.findall(r"{\w+}", msgid)), msgid


def test_pot_is_up_to_date(tmp_path):
    before = (ROOT / "po" / "vless-vpn.pot").read_text()
    subprocess.run([sys.executable, str(ROOT / "tools" / "update_po.py")], check=True, capture_output=True)
    assert (ROOT / "po" / "vless-vpn.pot").read_text() == before, "run tools/update_po.py"


def test_mo_roundtrip_matches_gnu_format(locale_dir):
    """Python's own gettext reads what tools/msgfmt.py writes."""
    import gettext

    t = gettext.translation(i18n.DOMAIN, locale_dir, languages=["ru"])
    assert t.gettext("Disconnected") == "Отключено"
    assert t.ngettext("Added {n} config", "Added {n} configs", 5) == "Добавлено {n} конфигов"
