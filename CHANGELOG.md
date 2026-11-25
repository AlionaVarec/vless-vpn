# Changelog

## 1.0.0 (2026-11-25)

First public release.

- One-button VPN for the whole system through the sing-box core, with Reality, TLS, WebSocket, gRPC, HTTP/2 and HTTPUpgrade.
- Several servers with ping, switching without turning the VPN off.
- English and Russian interface. The language follows the system and can be changed in the menu.
- The root service is now a small daemon that talks to the window over a local socket. Only administrators can control it.
- Packages: snap for amd64 and arm64, and an install script for any Linux with systemd.
