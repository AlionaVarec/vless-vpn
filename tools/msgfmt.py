#!/usr/bin/env python3
"""Compiles po/*.po into vless_vpn/locale/<lang>/LC_MESSAGES/vless-vpn.mo.

A small replacement for GNU msgfmt, so installing needs no gettext tools.
Usage: tools/msgfmt.py [--out DIR] [po files...]
"""

import argparse
import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "vless-vpn"


def parse_po(text: str, keep_empty: bool = False) -> dict[str, str]:
    """msgid -> msgstr. Plural forms are joined with NUL as in .mo files; fuzzy entries are skipped,
    untranslated ones too unless keep_empty."""
    entries: dict[str, str] = {}
    entry: dict[str, list[str]] = {}
    key = None
    fuzzy = False

    def flush():
        nonlocal entry, fuzzy
        if "msgid" in entry and not fuzzy:
            msgid = "".join(entry["msgid"])
            if "msgid_plural" in entry:
                msgid += "\0" + "".join(entry["msgid_plural"])
                forms = sorted(k for k in entry if k.startswith("msgstr["))
                msgstr = "\0".join("".join(entry[k]) for k in forms)
            else:
                msgstr = "".join(entry.get("msgstr", []))
            if "msgctxt" in entry:
                msgid = "".join(entry["msgctxt"]) + "\x04" + msgid
            if keep_empty or msgstr.replace("\0", ""):
                entries[msgid] = msgstr
        entry, fuzzy = {}, False

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                if "msgstr" in entry or "msgstr[0]" in entry:
                    flush()
                fuzzy = True
            continue
        if line.startswith('"'):
            if key is None:
                raise ValueError(f"line {n}: string without a keyword")
            entry[key].append(ast.literal_eval(line))
            continue
        keyword, _, value = line.partition(" ")
        if keyword in ("msgctxt", "msgid") and ("msgstr" in entry or "msgstr[0]" in entry):
            flush()
        known = keyword in ("msgctxt", "msgid", "msgid_plural", "msgstr") or keyword.startswith("msgstr[")
        if not known:
            raise ValueError(f"line {n}: unknown keyword {keyword}")
        key = keyword
        entry[key] = [ast.literal_eval(value)]
    flush()
    return entries


def mo_bytes(entries: dict[str, str]) -> bytes:
    keys = sorted(entries, key=lambda k: k.encode())
    ids = [k.encode() for k in keys]
    strs = [entries[k].encode() for k in keys]
    n = len(keys)
    header = 7 * 4
    orig_table = header
    trans_table = orig_table + n * 8
    data_start = trans_table + n * 8
    data = b""
    orig, trans = [], []
    for s in ids:
        orig.append((len(s), data_start + len(data)))
        data += s + b"\0"
    for s in strs:
        trans.append((len(s), data_start + len(data)))
        data += s + b"\0"
    out = struct.pack("<7I", 0x950412DE, 0, n, orig_table, trans_table, 0, data_start)
    for length, offset in orig + trans:
        out += struct.pack("<2I", length, offset)
    return out + data


def compile_po(po: Path, out_dir: Path) -> Path:
    target = out_dir / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(mo_bytes(parse_po(po.read_text(encoding="utf-8"))))
    return target


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=ROOT / "vless_vpn" / "locale")
    p.add_argument("po", nargs="*", type=Path)
    args = p.parse_args(argv)
    for po in args.po or sorted((ROOT / "po").glob("*.po")):
        print(compile_po(po, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
