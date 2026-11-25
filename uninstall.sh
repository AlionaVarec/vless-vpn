#!/usr/bin/env bash
# Removes what install.sh installed. Your configs in ~/.config/vless-vpn stay.
set -euo pipefail

APP_ID=io.github.AlionaVarec.VlessVpn
PREFIX=/usr/local

[[ $EUID -eq 0 ]] || exec sudo -- bash "$(readlink -f "$0")" "$@"

systemctl disable --now vless-vpn.service 2> /dev/null || true
rm -f /etc/systemd/system/vless-vpn.service
systemctl daemon-reload
rm -rf "$PREFIX/lib/vless-vpn" /var/lib/vless-vpn
rm -f "$PREFIX/bin/vless-vpn"
rm -f "$PREFIX/share/applications/$APP_ID.desktop" \
    "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
    "$PREFIX/share/metainfo/$APP_ID.metainfo.xml"
echo "Removed. Your configs stay in ~/.config/vless-vpn"
