#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  PlayCall Encoder — installer.
#  Raspberry Pi 4/5, Raspberry Pi OS Bookworm (64-bit), NetworkManager.
#
#  One-liner (self-install):
#    curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/basebook-encoder/main/install.sh | sudo bash
#
#  Or from a checkout:
#    sudo bash install.sh
#
#  Idempotent — safe to re-run for upgrades; it never touches an existing
#  /etc/playcall-encoder/config.json.
# ════════════════════════════════════════════════════════════

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh" >&2
  exit 1
fi

REPO_URL="${PLAYCALL_ENCODER_REPO:-https://github.com/loggerhead-turtle/basebook-encoder}"
INSTALL_DIR=/opt/playcall-encoder
CONFIG_DIR=/etc/playcall-encoder
MEDIAMTX_VERSION="${MEDIAMTX_VERSION:-latest}"

echo "── PlayCall Encoder installer ──"

# ── Not-fresh Pi detection (BEFORE we touch anything) ─────────────────────────
# A box that is already online — Ethernet, USB tether, or a Speedify cellular
# bond — gets its network ADOPTED as-is: no setup hotspot, no wpa/nmcli
# writes, and no disabling of services (dnsmasq etc.) it may depend on.
ADOPTED=0
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  if systemctl is-active --quiet speedify 2>/dev/null \
     || ip route show default 2>/dev/null | grep -q .; then
    ADOPTED=1
    echo "Detected working connectivity — adopting this Pi's existing network."
  fi
fi

# ── Packages ──────────────────────────────────────────────────────────────────
# NetworkManager is assumed present (Bookworm default); hostapd/dnsmasq are
# only used for the first-boot setup hotspot.
apt-get update -qq
apt-get install -y -qq ffmpeg curl git avahi-daemon hostapd dnsmasq \
  python3 python3-pil python3-flask python3-numpy python3-serial >/dev/null
# hostapd/dnsmasq must NOT run as daemons — provisioning spawns them ad hoc.
# On an ADOPTED box we leave existing services strictly alone: this Pi's
# network stack (possibly Speedify-bonded) is not ours to manage.
if [[ "$ADOPTED" != 1 ]]; then
  systemctl disable --now hostapd dnsmasq >/dev/null 2>&1 || true
fi

# ── Service user + directories ────────────────────────────────────────────────
id playcall &>/dev/null || \
  useradd --system --home /var/lib/playcall-encoder --shell /usr/sbin/nologin playcall
mkdir -p "$CONFIG_DIR" "$INSTALL_DIR" /var/lib/playcall-encoder/segments \
         /var/lib/playcall-encoder/clips
chown -R playcall:playcall /var/lib/playcall-encoder
chgrp playcall "$CONFIG_DIR"

# ── Package files → /opt/playcall-encoder ─────────────────────────────────────
# Prefer the files next to this script (checkout install); fall back to a
# fresh git clone (curl | bash install).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
if [[ -d "$HERE/encoder" && -f "$HERE/VERSION" ]]; then
  SRC="$HERE"
else
  echo "Fetching $REPO_URL…"
  TMP_SRC="$(mktemp -d)"
  git clone --depth 1 "$REPO_URL" "$TMP_SRC/ndi-encoder" >/dev/null
  SRC="$TMP_SRC/ndi-encoder"
fi
cp -r "$SRC/encoder" "$INSTALL_DIR/"
cp "$SRC/VERSION" "$SRC/mediamtx.yml" "$INSTALL_DIR/"
cp "$SRC/LICENSE" "$INSTALL_DIR/" 2>/dev/null || true
mkdir -p "$INSTALL_DIR/scripts"
install -m 755 "$SRC/scripts/"*.sh "$SRC/scripts/"*.py "$INSTALL_DIR/scripts/" 2>/dev/null || \
  install -m 755 "$SRC/scripts/youtube_push.sh" "$INSTALL_DIR/scripts/"
find "$INSTALL_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# ── MediaMTX (static binary from GitHub releases; arm64 on Pi 4/5) ────────────
if ! command -v mediamtx >/dev/null; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64) MTX_ARCH=arm64 ;;
    armv7l)  MTX_ARCH=armv7 ;;
    x86_64)  MTX_ARCH=amd64 ;;
    *) echo "ERROR: unsupported architecture $ARCH" >&2; exit 1 ;;
  esac
  if [[ "$MEDIAMTX_VERSION" == "latest" ]]; then
    # Capture the response first: grep -m1 SIGPIPE-ing curl mid-write would
    # trip pipefail (see the original Play-call installer for the war story).
    MTX_API_RESPONSE="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest)"
    MEDIAMTX_VERSION="$(grep -oPm1 '"tag_name":\s*"\K[^"]+' <<< "$MTX_API_RESPONSE")"
  fi
  echo "Installing MediaMTX ${MEDIAMTX_VERSION} (${MTX_ARCH})…"
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/mtx.tar.gz" \
    "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
  tar -xzf "$TMP/mtx.tar.gz" -C "$TMP" mediamtx
  install -m 755 "$TMP/mediamtx" /usr/local/bin/mediamtx
  rm -rf "$TMP"
fi
echo "MediaMTX: $(mediamtx --version 2>/dev/null || echo installed)"

# ── Adopted box: complete setup headlessly (key + PIN, network unmanaged) ────
if [[ "$ADOPTED" == 1 ]]; then
  python3 - <<'PY'
import sys
sys.path.insert(0, '/opt/playcall-encoder')
from encoder import provisioning
provisioning.headless_setup()
PY
fi

# ── mediamtx config (bake in the ingest key if one exists yet) ───────────────
python3 - <<'PY'
import sys
sys.path.insert(0, '/opt/playcall-encoder')
from encoder import config
config.write_mediamtx_config(config.load())
PY

# ── Optional NDI bindings (scorebug falls back to MJPEG/PNG without them) ────
pip3 install -q ndi-python --break-system-packages 2>/dev/null \
  || echo "⚠ ndi-python not installed — scorebug serves MJPEG/PNG on :8765 only"

# ── Hostname: playcall-encoder.local via avahi (only if still the default) ───
CURRENT_HOST="$(hostname)"
if [[ "$CURRENT_HOST" == "raspberrypi" || -z "$CURRENT_HOST" ]]; then
  hostnamectl set-hostname playcall-encoder
  sed -i 's/raspberrypi/playcall-encoder/g' /etc/hosts || true
  echo "Hostname set to playcall-encoder (.local via avahi)"
fi
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

# ── systemd units ─────────────────────────────────────────────────────────────
install -m 644 "$SRC/systemd/playcall-encoder.service"          /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-mediamtx.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-youtube.service"  /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-clipper.service"  /etc/systemd/system/
systemctl daemon-reload
systemctl enable playcall-encoder playcall-encoder-mediamtx \
                 playcall-encoder-youtube playcall-encoder-clipper >/dev/null
systemctl restart playcall-encoder-mediamtx playcall-encoder-youtube \
                  playcall-encoder-clipper playcall-encoder

echo
echo "── Done ─────────────────────────────────────────────────"
if [[ "$ADOPTED" == 1 ]]; then
  echo "This Pi was already online (Ethernet / Speedify / tether), so its"
  echo "network was ADOPTED as-is — no hotspot, and PlayCall will never"
  echo "modify this box's network configuration."
  python3 - <<'PY'
import sys
sys.path.insert(0, '/opt/playcall-encoder')
from encoder import config, provisioning
cfg = config.load()
print()
print('Camera app RTMP URL(s):')
for u in provisioning.rtmp_urls(cfg):
    print(f'  {u}')
print(f"Settings page:  http://localhost:8080 (or <this-pi>.local:8080)")
print(f"Settings PIN:   {cfg['device']['pin']}   ← write this down")
PY
elif [[ -f "$CONFIG_DIR/config.json" ]]; then
  echo "Existing config kept. Settings page: http://playcall-encoder.local:8080"
else
  echo "First-time setup: the encoder is now broadcasting a Wi-Fi hotspot"
  echo "named PlayCall-Encoder-XXXX. Join it from your phone and follow the"
  echo "setup page (it opens automatically, or browse to http://192.168.4.1)."
fi
