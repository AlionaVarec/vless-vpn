"""Translations.

Source strings are English. Russian lives in po/ru.po and is compiled to
vless_vpn/locale/ru/LC_MESSAGES/vless-vpn.mo by tools/msgfmt.py at install time.
The language follows the system unless the user picks one in the app menu.
"""

import gettext
import os
from pathlib import Path

DOMAIN = "vless-vpn"
LOCALE_DIR = Path(__file__).with_name("locale")
LANGUAGES = ("en", "ru")  # "en" is the source language and needs no catalog

_catalog: gettext.NullTranslations = gettext.NullTranslations()
_current = "en"


def _code(value: str) -> str:
    """ru_RU.UTF-8 -> ru, en@quot -> en."""
    return value.split(".")[0].split("@")[0].split("_")[0].lower()


def system_language(env=None) -> str:
    """The first supported language from the usual gettext variables, English otherwise."""
    env = os.environ if env is None else env
    for part in (env.get("LANGUAGE") or "").split(":"):
        if _code(part) in LANGUAGES:
            return _code(part)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if env.get(var):
            code = _code(env[var])
            return code if code in LANGUAGES else "en"
    return "en"


def setup(language: str | None = None, locale_dir: Path | None = None) -> str:
    """Switch the language: "en", "ru", or None / "system". Returns the language in use."""
    global _catalog, _current
    code = language if language in LANGUAGES else system_language()
    if code == "en":
        _catalog = gettext.NullTranslations()
    else:
        _catalog = gettext.translation(
            DOMAIN, locale_dir or LOCALE_DIR, languages=[code], fallback=True
        )
    _current = code
    return code


def current() -> str:
    return _current


def _(message: str) -> str:
    return _catalog.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _catalog.ngettext(singular, plural, n)


def N_(message: str) -> str:
    """Marks a string for extraction without translating it yet."""
    return message
