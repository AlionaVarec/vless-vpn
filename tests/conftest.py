import subprocess
import sys
from pathlib import Path

import pytest

from vless_vpn import i18n

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def locale_dir(tmp_path_factory):
    """Russian catalog compiled from po/ru.po by our own tools/msgfmt.py."""
    out = tmp_path_factory.mktemp("locale")
    subprocess.run([sys.executable, str(ROOT / "tools" / "msgfmt.py"), "--out", str(out)], check=True)
    return out


@pytest.fixture
def russian(locale_dir):
    i18n.setup("ru", locale_dir)
    yield
    i18n.setup("en")


@pytest.fixture(autouse=True)
def english_by_default():
    i18n.setup("en")
    yield
    i18n.setup("en")
