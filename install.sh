#!/usr/bin/env bash
# Installs VLESS VPN without Snap: the sing-box core, the app, the root service and the menu entry.
# Run from the project folder as your own user: ./install.sh (sudo asks for the password).
# Running it again is safe and is also how updates are installed.
set -euo pipefail

APP_ID=io.github.AlionaVarec.VlessVpn
PREFIX=/usr/local
LIB=$PREFIX/lib/vless-vpn
SINGBOX=$LIB/sing-box  # our own copy: never touches another sing-box on the system
UNIT=/etc/systemd/system/vless-vpn.service

[[ $EUID -eq 0 ]] || exec sudo -- bash "$(readlink -f "$0")" "$@"
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
# shellcheck source=tools/sing-box.env
. "$HERE/tools/sing-box.env"

fail() { echo "error: $*" >&2; exit 1; }

case "$(uname -m)" in
    x86_64) ARCH=amd64 ;;
    aarch64 | arm64) ARCH=arm64 ;;
    *) fail "unsupported CPU $(uname -m); only x86_64 and aarch64 are supported" ;;
esac

command -v systemctl > /dev/null || fail "systemd is required"
if ! /usr/bin/python3 - << 'PY' 2> /dev/null; then
import sys
assert sys.version_info >= (3, 10)
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
PY
    cat >&2 << 'EOF'
error: Python 3.10+, GTK 4 and libadwaita 1.5+ for Python are required. Install them:
  Ubuntu, Debian:  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
  Fedora:          sudo dnf install python3-gobject gtk4 libadwaita
  Arch:            sudo pacman -S python-gobject gtk4 libadwaita
EOF
    exit 1
fi

echo "==> sing-box ${SB_VERSION}"
if [[ "$("$SINGBOX" version 2> /dev/null | sed -n 1p)" != "sing-box version ${SB_VERSION}" ]]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    url="https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/sing-box-${SB_VERSION}-linux-${ARCH}.tar.gz"
    curl -fL --proto '=https' -o "$tmp/sb.tar.gz" "$url"
    sum_var="SB_SHA256_${ARCH}"
    echo "${!sum_var}  $tmp/sb.tar.gz" | sha256sum -c --quiet
    tar -xzf "$tmp/sb.tar.gz" -C "$tmp"
    install -D -m 755 "$tmp/sing-box-${SB_VERSION}-linux-${ARCH}/sing-box" "$SINGBOX"
fi
"$SINGBOX" version | sed -n 1p

echo "==> app in ${LIB}"
rm -rf "$LIB/vless_vpn" "$LIB/vless-vpn" "$LIB/vless-vpnd"
install -d -m 755 "$LIB/vless_vpn"
install -m 644 "$HERE"/vless_vpn/*.py "$LIB/vless_vpn/"
install -m 755 "$HERE/vless-vpn" "$HERE/vless-vpnd" "$LIB/"
/usr/bin/python3 -I "$HERE/tools/msgfmt.py" --out "$LIB/vless_vpn/locale" > /dev/null
chmod -R a+rX "$LIB"
ln -sf "$LIB/vless-vpn" "$PREFIX/bin/vless-vpn"

echo "==> menu entry"
install -D -m 644 "$HERE/data/$APP_ID.desktop" "$PREFIX/share/applications/$APP_ID.desktop"
install -D -m 644 "$HERE/data/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
install -D -m 644 "$HERE/data/$APP_ID.metainfo.xml" "$PREFIX/share/metainfo/$APP_ID.metainfo.xml"
if command -v update-desktop-database > /dev/null; then
    update-desktop-database -q "$PREFIX/share/applications" || true
fi
if command -v gtk-update-icon-cache > /dev/null; then
    gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" || true
fi

echo "==> service vless-vpn.service"
sed -e "s|@LIB@|${LIB}|" -e "s|@SINGBOX@|${SINGBOX}|" "$HERE/data/vless-vpn.service.in" > "$UNIT"
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable vless-vpn.service > /dev/null 2>&1
# a running service keeps the old code; the restart drops an active VPN, the button brings it back
systemctl restart vless-vpn.service

echo
echo "Done. Open \"VLESS VPN\" from the app menu or run: vless-vpn"
