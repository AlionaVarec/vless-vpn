#!/usr/bin/env python3
"""Extracts translatable strings from vless_vpn/*.py into po/vless-vpn.pot and adds new
ones to every po/*.po (untranslated). Strings no longer used are reported, not deleted.

Marked strings: _("..."), N_("...") and ngettext("...", "...", n).
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from msgfmt import parse_po  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def extract() -> dict[tuple[str, str | None], list[str]]:
    """(msgid, msgid_plural) -> places like vless_vpn/app.py:42."""
    found: dict[tuple[str, str | None], list[str]] = {}
    for path in sorted((ROOT / "vless_vpn").glob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            args = node.args
            strings = [a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if node.func.id in ("_", "N_") and len(args) == 1 and strings:
                key = (strings[0], None)
            elif node.func.id == "ngettext" and len(strings) >= 2:
                key = (strings[0], strings[1])
            else:
                continue
            found.setdefault(key, []).append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def pot_text(found) -> str:
    out = [
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        '"Content-Transfer-Encoding: 8bit\\n"',
        "",
    ]
    for (msgid, plural), places in found.items():
        out.append("#: " + " ".join(places))
        if "{" in msgid:
            out.append("#, python-brace-format")
        out.append(f"msgid {quote(msgid)}")
        if plural:
            out.append(f"msgid_plural {quote(plural)}")
            out += ['msgstr[0] ""', 'msgstr[1] ""']
        else:
            out.append('msgstr ""')
        out.append("")
    return "\n".join(out)


def main() -> int:
    found = extract()
    (ROOT / "po" / "vless-vpn.pot").write_text(pot_text(found), encoding="utf-8")
    wanted = {m + ("\0" + p if p else "") for m, p in found}
    for po in sorted((ROOT / "po").glob("*.po")):
        text = po.read_text(encoding="utf-8")
        have = parse_po(text, keep_empty=True)
        missing = [(m, p) for m, p in found if m + ("\0" + p if p else "") not in have]
        if missing:
            add = [""]
            for m, p in missing:
                add.append(f"msgid {quote(m)}")
                if p:
                    add += [f"msgid_plural {quote(p)}", 'msgstr[0] ""', 'msgstr[1] ""', 'msgstr[2] ""']
                else:
                    add.append('msgstr ""')
                add.append("")
            po.write_text(text.rstrip("\n") + "\n" + "\n".join(add), encoding="utf-8")
        unused = [k for k in have if k and k not in wanted]
        print(f"{po.name}: {len(missing)} new, {len(unused)} unused")
        for k in unused:
            print(f"  unused: {k!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
