#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  PlayCall Encoder — installer.
#  Raspberry Pi 4/5, Raspberry Pi OS Bookworm (64-bit), NetworkManager.
#
#  One-liner (self-install), with the activation code from the website:
#    curl -fsSL https://basebook.org/i | sudo bash -s -- HAWK-4823
#
#  basebook.org/i serves THIS FILE. It is short enough to type by hand off
#  a phone onto a Pi keyboard, and reachable on the school and park
#  networks that block raw.githubusercontent.com. The GitHub raw URL still
#  works if you prefer it:
#    curl -fsSL https://raw.githubusercontent.com/loggerhead-turtle/basebook-encoder/main/install.sh | sudo bash -s -- HAWK-4823
#
#  Without a code it still installs — the box just isn't paired to a team
#  until you run it again with one (or drop the code on the boot partition).
#
#  Or from a checkout:
#    sudo bash install.sh [HAWK-4823]
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
CLOUD_URL="${PLAYCALL_CLOUD:-https://basebook.org}"

# ── Where the activation code comes from ──────────────────────────────────────
# Three sources, in priority order, because the person doing this may have
# a terminal, may only have a card reader, or may have neither:
#   1. an argument      — the copy-paste one-liner off the website
#   2. the environment  — PLAYCALL_CODE=…, for scripted/imaged fleets
#   3. a file on the SD card's BOOT partition — the prebuilt-image path:
#      flash, open the card on any computer, save the code into a text
#      file, eject. No SSH, no terminal, nothing typed on the Pi.
# The code is normalised here (case, spaces, missing dash) so a coach
# reading it off a phone can't get it "wrong".
CODE_ARG="${1:-${PLAYCALL_CODE:-}}"
CODE_FILE=""
for f in /boot/firmware/playcall-code.txt /boot/playcall-code.txt; do
  [[ -z "$CODE_ARG" && -f "$f" ]] && { CODE_ARG="$(cat "$f")"; CODE_FILE="$f"; break; }
done
ACT_CODE="$(tr -cd '[:alnum:]' <<< "${CODE_ARG:-}" | tr '[:lower:]' '[:upper:]')"
if [[ ${#ACT_CODE} -eq 8 ]]; then
  ACT_CODE="${ACT_CODE:0:4}-${ACT_CODE:4:4}"
elif [[ -n "$ACT_CODE" ]]; then
  echo "⚠ '$CODE_ARG' is not an activation code (expected 4 letters + 4 digits," >&2
  echo "  e.g. HAWK-4823). Installing anyway — pair the box afterwards." >&2
  ACT_CODE=""
fi

echo "── PlayCall Encoder installer ──"
[[ -n "$ACT_CODE" ]] && echo "Activation code: $ACT_CODE${CODE_FILE:+  (from $CODE_FILE)}"

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

# ── Activation: trade the code for this team's cloud key ─────────────────────
# Last real step before the services come up, so the box's very first
# heartbeat is already authenticated and the website flips to "Online"
# while the person who ran this is still looking at the screen.
#
# A failure here is NOT fatal: the encoder is installed and works locally:
# RTMP ingest, scorebug, clips. Only the cloud link is missing, and it can
# be re-run. Killing the install over it would waste the whole download.
ACTIVATED=0
if [[ -n "$ACT_CODE" ]]; then
  echo "── Pairing with $CLOUD_URL ──"
  set +e
  PLAYCALL_CLOUD="$CLOUD_URL" python3 "$INSTALL_DIR/scripts/activate.py" "$ACT_CODE"
  RC=$?
  set -e
  [[ $RC -eq 0 ]] && ACTIVATED=1
fi

# ── systemd units ─────────────────────────────────────────────────────────────
install -m 644 "$SRC/systemd/playcall-encoder.service"          /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-mediamtx.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-youtube.service"  /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-clipper.service"  /etc/systemd/system/
install -m 644 "$SRC/systemd/playcall-encoder-activate.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable playcall-encoder playcall-encoder-mediamtx \
                 playcall-encoder-youtube playcall-encoder-clipper \
                 playcall-encoder-activate >/dev/null
systemctl restart playcall-encoder-mediamtx playcall-encoder-youtube \
                  playcall-encoder-clipper playcall-encoder

echo
echo "── Done ─────────────────────────────────────────────────"

# ── What to do next ──────────────────────────────────────────────────────────
# Exactly one instruction, chosen by what actually happened. The old banner
# ended by telling everyone to write down a six-digit PIN; almost nobody
# needs it any more (the website opens this box's settings signed in), and
# printing it as homework made a finished install feel unfinished.
if [[ "$ACTIVATED" == 1 ]]; then
  echo "This box is paired. Nothing else to do here."
  echo
  echo "Go back to basebook.org → Score Bug Studio. The box appears on the"
  echo "Encoders card within a few seconds; ⚙ Settings there opens it"
  echo "already signed in — no PIN to remember."
elif [[ -n "$ACT_CODE" ]]; then
  echo "⚠ The encoder is installed, but pairing did not go through (see the"
  echo "  message above). Everything local works; only the cloud link is"
  echo "  missing. Get a fresh code from the website and re-run:"
  echo
  echo "    curl -fsSL $CLOUD_URL/i | sudo bash -s -- YOUR-CODE"
elif ALREADY="$(python3 - <<'PY' 2>/dev/null
import sys
sys.path.insert(0, '/opt/playcall-encoder')
from encoder import config
cur = config.load().get('cloud') or {}
print(cur.get('base_url', '') if cur.get('api_key') else '')
PY
)" && [[ -n "$ALREADY" ]]; then
  # UPGRADING A WORKING BOX. No code was passed because none is needed —
  # this box has been paired for months. Saying "not paired to a team
  # yet" here sent an owner off to mint a code for a box that was fine,
  # right after a game where its data had genuinely gone missing. Read
  # the config and say what is true.
  echo "Upgraded. This box is still paired to $ALREADY — nothing else to do."
  echo
  echo "Its pairing, YouTube key and settings were left untouched."
else
  echo "The encoder is installed but not paired to a team yet."
  echo
  echo "On basebook.org → Score Bug Studio → Encoders, click 'Add an encoder'."
  echo "It gives you a one-line command with your code already in it. Paste"
  echo "that here and this box joins your team:"
  echo
  echo "    curl -fsSL $CLOUD_URL/i | sudo bash -s -- YOUR-CODE"
fi

if [[ "$ADOPTED" == 1 ]]; then
  echo
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
print('Settings page:  http://localhost:8080 (or <this-pi>.local:8080)')
print('Recovery PIN:   %s   (only needed if you open the settings page '
      'directly instead of from the website)' % cfg['device']['pin'])
PY
elif [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  echo
  echo "This Pi has no network yet, so it is broadcasting a Wi-Fi hotspot"
  echo "named PlayCall-Encoder-XXXX. Join it from your phone and follow the"
  echo "setup page (it opens automatically, or browse to http://192.168.4.1)."
fi
