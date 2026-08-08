#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  PlayCall radar box — installer.
#  Pi Zero 2 W class hardware, Raspberry Pi OS Bookworm (Lite is ideal).
#
#  From a checkout:
#    sudo bash scripts/install_radar_box.sh
#
#  Installs ONLY the radar path: serial capture + LAN feed + cloud
#  shadow (encoder/radar_standalone.py). No MediaMTX, no YouTube push,
#  no clipper, no hotspot — a Zero 2 has no business running any of
#  that, and the full install.sh stays the path for encoder boxes
#  (which already serve the identical radar LAN feed in-process).
#
#  Idempotent — safe to re-run for upgrades; it never touches an
#  existing /etc/playcall-encoder/config.json. Networking is the OS's
#  job on this box (raspi-config / Imager presets); pairing to the
#  cloud is optional and done by dropping cloud.base_url + cloud.api_key
#  into the config — unpaired, the box is LAN-only and fully useful.
# ════════════════════════════════════════════════════════════

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash scripts/install_radar_box.sh" >&2
  exit 1
fi

INSTALL_DIR=/opt/playcall-encoder
CONFIG_DIR=/etc/playcall-encoder

echo "── PlayCall radar box installer ──"

# ── Packages ──────────────────────────────────────────────────────────────────
# python3-serial reads the gun; avahi advertises the feed. That's it.
apt-get update -qq
apt-get install -y -qq python3 python3-serial avahi-daemon >/dev/null

# ── Service user + directories (same identities as the encoder box, so a
#    box promoted to a full encoder later needs no migration) ─────────────────
id playcall &>/dev/null || \
  useradd --system --home /var/lib/playcall-encoder --shell /usr/sbin/nologin playcall
mkdir -p "$CONFIG_DIR" "$INSTALL_DIR"
chgrp playcall "$CONFIG_DIR"

# ── Package files → /opt/playcall-encoder ─────────────────────────────────────
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
if [[ ! -d "$HERE/encoder" || ! -f "$HERE/VERSION" ]]; then
  echo "ERROR: run from a repo checkout (scripts/install_radar_box.sh)" >&2
  exit 1
fi
cp -r "$HERE/encoder" "$INSTALL_DIR/"
cp "$HERE/VERSION" "$INSTALL_DIR/"
cp "$HERE/LICENSE" "$INSTALL_DIR/" 2>/dev/null || true
find "$INSTALL_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ── mDNS advertisement (_basebook-radar._tcp :8791) ──────────────────────────
mkdir -p /etc/avahi/services
install -m 644 "$HERE/preconfig/avahi-basebook-radar.service" \
  /etc/avahi/services/basebook-radar.service
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

# ── systemd unit ──────────────────────────────────────────────────────────────
install -m 644 "$HERE/systemd/playcall-encoder-radar.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable playcall-encoder-radar >/dev/null
systemctl restart playcall-encoder-radar

echo
echo "── Done ─────────────────────────────────────────────────"
echo "Radar box is up: cable the gun (USB→RS-232, 19200 8N1) and the app"
echo "will discover this box as _basebook-radar._tcp on the LAN."
echo "Optional cloud shadow: put cloud.base_url + cloud.api_key in"
echo "$CONFIG_DIR/config.json (or ship a preconfig — see preconfig/)."
echo "Logs: journalctl -u playcall-encoder-radar -f"
