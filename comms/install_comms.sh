#!/usr/bin/env bash
# Turn the (already-activated) display Pi into the comms ear.
#
#   sudo bash pi/install_comms.sh
#
# Installs speech + Bluetooth-audio packages, generates the 4-digit PIN
# for the local admin page, installs playcall-comms.service, and starts
# it. Afterwards, from any phone on the same WiFi:
#
#   http://<this-pi's-hostname>.local:8790   ← pair the bud, then 🔒 LOCK
#
# The service reuses the Pi's existing activation key
# (/etc/playcall.env) — nothing new to pair with the cloud.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
ENV=/etc/playcall.env
# The display app (main.py) keeps its activation in /var/lib/playcall/
# — same variable names, different home. Import it rather than making a
# working display box re-activate.
DISPLAY_ENV=/var/lib/playcall/.playcall.env
ENC_CFG=/etc/playcall-encoder/config.json
if ! grep -q PLAYCALL_API_KEY "$ENV" 2>/dev/null; then
  ENC_KEY=""
  if [ -f "$ENC_CFG" ]; then
    ENC_KEY=$(python3 -c "import json;print((json.load(open('$ENC_CFG'))
.get('cloud') or {}).get('api_key') or '')" 2>/dev/null || true)
    ENC_URL=$(python3 -c "import json;print((json.load(open('$ENC_CFG'))
.get('cloud') or {}).get('base_url') or '')" 2>/dev/null || true)
  fi
  if grep -q PLAYCALL_API_KEY "$DISPLAY_ENV" 2>/dev/null; then
    echo "── importing the display app's activation key ──"
    grep -E '^PLAYCALL_(CLOUD_URL|API_KEY)=' "$DISPLAY_ENV" >> "$ENV"
  elif [ -n "$ENC_KEY" ]; then
    # ONE-PI STORY: this box is an encoder — comms rides its identity.
    echo "── importing the encoder's activation key ──"
    {
      echo "PLAYCALL_CLOUD_URL=${ENC_URL:-https://basebook.org}"
      echo "PLAYCALL_API_KEY=$ENC_KEY"
    } >> "$ENV"
    chmod 600 "$ENV"
  else
    # A display box that has only ever worked locally has no cloud key
    # anywhere — pair it right here with a one-time code.
    echo "── this Pi isn't paired with the cloud yet ──"
    echo "On the site: sign in as a coach → your team page (/auth/team)"
    echo "→ Raspberry Pi devices → generate a code for a DISPLAY Pi."
    read -rp "Activation code (e.g. HAWK-4823): " CODE
    [ -n "$CODE" ] || { echo "no code — aborting"; exit 1; }
    CLOUD="${PLAYCALL_CLOUD_URL:-https://basebook.org}"
    RESP=$(curl -sS -X POST "$CLOUD/api/pi/activate" \
      -H 'Content-Type: application/json' \
      -d "{\"code\":\"$CODE\",\"device_name\":\"$(hostname) (comms)\"}")
    KEY=$(printf '%s' "$RESP" | python3 -c \
      "import sys,json;print(json.load(sys.stdin).get('api_key',''))" \
      2>/dev/null || true)
    CURL2=$(printf '%s' "$RESP" | python3 -c \
      "import sys,json;print(json.load(sys.stdin).get('cloud_url',''))" \
      2>/dev/null || true)
    if [ -z "$KEY" ]; then
      echo "Activation failed — the cloud said:"
      echo "  $RESP"
      exit 1
    fi
    {
      echo "PLAYCALL_CLOUD_URL=${CURL2:-$CLOUD}"
      echo "PLAYCALL_API_KEY=$KEY"
    } >> "$ENV"
    chmod 600 "$ENV"
    echo "✓ Paired with the cloud."
  fi
fi

echo "── packages (speech + Bluetooth audio + live voice) ──"
apt-get update -qq
apt-get install -y -qq espeak-ng mpg123 ffmpeg \
  pipewire pipewire-audio pipewire-pulse wireplumber \
  pulseaudio-utils libspa-0.2-bluetooth >/dev/null
# aiortc = the box answers the coach's LIVE WebRTC voice link (clips
# stay as the fallback if it can't install on this OS release)
apt-get install -y -qq python3-aiortc python3-av >/dev/null 2>&1 \
  || pip3 install --break-system-packages -q aiortc 2>/dev/null \
  || echo "   (aiortc unavailable — live voice falls back to clips)"
# livekit = the ☁ cloud voice channel (SFU): the coach talks from ANY
# network once the site has LiveKit configured. Optional — everything
# else works without it, and the box says so on the coach page if the
# site enables cloud voice before this lands.
# a netinst Debian ships NO pip3 at all — both pip calls above and
# below died with 'command not found' into /dev/null, and the box then
# told the coach page 'livekit is not installed' after every install
# (field report). Make pip exist, and make a livekit failure SAY WHY.
command -v pip3 >/dev/null 2>&1 \
  || apt-get install -y -qq python3-pip >/dev/null 2>&1 || true
pip3 install --break-system-packages -q livekit \
  || echo "   ⚠ livekit install failed (see pip output above) — cloud \
voice stays off on this box"

# ── a HUMAN voice (Piper neural TTS; espeak-ng stays as fallback) ───────
PIPER_DIR=/opt/piper
if [ ! -x "$PIPER_DIR/piper" ]; then
  echo "── natural voice (Piper) ──"
  case "$(uname -m)" in
    aarch64) PT=piper_linux_aarch64.tar.gz ;;
    armv7l)  PT=piper_linux_armv7l.tar.gz ;;
    x86_64)  PT=piper_linux_x86_64.tar.gz ;;
    *)       PT= ;;
  esac
  if [ -n "$PT" ]; then
    mkdir -p "$PIPER_DIR"
    curl -sSL "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/$PT" \
      | tar xz -C "$PIPER_DIR" --strip-components=1 \
      || echo "   (piper download failed — keeping espeak voice)"
  fi
fi
if [ -x "$PIPER_DIR/piper" ] && [ ! -f "$PIPER_DIR/voice.onnx" ]; then
  VBASE="https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
  curl -sSL -o "$PIPER_DIR/voice.onnx" "$VBASE/en_US-lessac-medium.onnx" \
    && curl -sSL -o "$PIPER_DIR/voice.onnx.json" \
         "$VBASE/en_US-lessac-medium.onnx.json" \
    || { rm -f "$PIPER_DIR/voice.onnx"; \
         echo "   (voice download failed — keeping espeak voice)"; }
fi

# ── the audio engine, headless ──────────────────────────────────────────
# Bluetooth AUDIO needs PipeWire running, and PipeWire runs per-user —
# on a headless box nobody ever logs in, so the bud pairs but connect
# fails with br-connection-profile-unavailable. Lingering gives the
# user a session at boot with no login; the comms service then runs AS
# that user so speech routes into the same engine.
# WHOSE session. Current Raspberry Pi OS has no default 'pi' account —
# the Imager makes you invent a username — so the old `${SUDO_USER:-pi}`
# fallback pointed at a user that does not exist. `id -u` then failed
# under `set -e` a dozen lines further down, in a block about PipeWire,
# with nothing on screen about usernames. Resolve it in the open, and
# say so plainly when we cannot.
RUNUSER="${PLAYCALL_USER:-${SUDO_USER:-}}"
if [ -z "$RUNUSER" ] || [ "$RUNUSER" = root ]; then
  # `sudo bash install_comms.sh` sets SUDO_USER; a root shell or a
  # systemd/cloud-init run does not. Fall back to the first real login
  # account on the box (uid >= 1000), which on a Pi is the one the
  # Imager created.
  RUNUSER=$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')
fi
if [ -z "$RUNUSER" ] || ! id -u "$RUNUSER" >/dev/null 2>&1; then
  echo "ERROR: could not work out which user account should own the audio" >&2
  echo "session on this Pi. Bluetooth audio runs inside a login session, so" >&2
  echo "this has to be a real account — the one you created in Raspberry Pi" >&2
  echo "Imager, whatever you named it." >&2
  echo >&2
  echo "Re-run and name it:  sudo PLAYCALL_USER=yourusername bash pi/install_comms.sh" >&2
  exit 1
fi
RUNUID=$(id -u "$RUNUSER")
echo "── audio engine (PipeWire, headless session for $RUNUSER) ──"
# A running session never re-reads its group list, and wireplumber only
# offers BlueZ an A2DP endpoint when the bluetooth SPA plugin is loaded
# — so a box that gains either AFTER its session started ends up with
# PipeWire owning nothing ('auto_null'), buds failing to connect with
# profile-unavailable, and total silence. Note it and demand a reboot.
NEEDS_REBOOT=""
id -nG "$RUNUSER" | tr ' ' '\n' | grep -qx bluetooth || NEEDS_REBOOT="1"
usermod -aG bluetooth "$RUNUSER" 2>/dev/null || true
dpkg -s libspa-0.2-bluetooth >/dev/null 2>&1 || NEEDS_REBOOT="1"
# HEADLESS SEAT FIX — the one that costs an afternoon if missed.
# WirePlumber's bluez monitor 'wants' logind seat-monitoring, and a
# LINGERING session (which is what a headless box has) owns no seat. It
# then loads the monitor but parks it: no A2DP endpoint is registered
# with BlueZ, so `bluetoothctl show` lists only AVRCP UUIDs, every bud
# connect dies with profile-unavailable — and NOTHING is logged. Turn
# seat-monitoring off so the monitor runs unconditionally.
WPDIR="$(getent passwd "$RUNUSER" | cut -d: -f6)/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$WPDIR"
cat > "$WPDIR/50-bluez-headless.conf" <<'WPCONF'
# Headless box: no logind seat, so never gate Bluetooth audio on one.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
WPCONF
chown -R "$RUNUSER" "$(getent passwd "$RUNUSER" | cut -d: -f6)/.config/wireplumber"

# let the box reboot itself from its own admin page — no SSH at a field,
# and let it power a Bluetooth controller down. READING rfkill works as
# any user, which is what made this so quiet: the admin page could show
# the block state perfectly while every attempt to CHANGE it failed with
# "Operation not permitted" into output nobody looked at. The adapter
# picker said "picked" and nothing moved.
{ printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl reboot\n' "$RUNUSER"
  # numeric ids ONLY — this page is reachable on the LAN behind a PIN,
  # and a bare wildcard would also grant `rfkill block all`, which turns
  # the box's own Wi-Fi off and takes it off the network for good.
  printf '%s ALL=(root) NOPASSWD: /usr/sbin/rfkill block [0-9]*, '\
'/usr/sbin/rfkill unblock [0-9]*\n' "$RUNUSER"
} > /etc/sudoers.d/playcall-comms
chmod 440 /etc/sudoers.d/playcall-comms
visudo -c -f /etc/sudoers.d/playcall-comms >/dev/null 2>&1 \
  || rm -f /etc/sudoers.d/playcall-comms
loginctl enable-linger "$RUNUSER"
# a fresh linger takes a moment to bring the user manager (and its bus)
# up — starting pipewire before the bus exists fails SILENTLY, and the
# symptom downstream is maddening: buds pair, connect, then drop, and
# the test speaker plays nothing
for _i in $(seq 1 15); do
  [ -S "/run/user/$RUNUID/bus" ] && break
  sleep 1
done
if sudo -u "$RUNUSER" XDG_RUNTIME_DIR="/run/user/$RUNUID" \
  systemctl --user enable --now pipewire pipewire-pulse wireplumber \
  2>/dev/null; then
  echo "   ✓ audio engine running"
else
  echo "   ⚠ the user audio engine did not start — buds will pair but"
  echo "     drop, and nothing will play. Fix: log in as $RUNUSER and run"
  echo "       systemctl --user enable --now pipewire pipewire-pulse wireplumber"
fi

if ! grep -q PLAYCALL_COMMS_PIN "$ENV"; then
  PIN=$(( RANDOM % 9000 + 1000 ))
  echo "PLAYCALL_COMMS_PIN=$PIN" >> "$ENV"
else
  PIN=$(grep PLAYCALL_COMMS_PIN "$ENV" | cut -d= -f2)
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
# Runs AS the audio user (not root): that puts espeak/ffplay inside the
# same PipeWire session the earbud connects to. systemd reads the env
# file as root before dropping privileges, so its 600 perms are fine.
cat > /etc/systemd/system/playcall-comms.service <<UNIT
[Unit]
Description=PlayCall comms ear (spoken pitch calls to the earpiece)
After=network-online.target bluetooth.target user@$RUNUID.service
Wants=network-online.target user@$RUNUID.service

[Service]
User=$RUNUSER
EnvironmentFile=$ENV
Environment=XDG_RUNTIME_DIR=/run/user/$RUNUID
ExecStart=/usr/bin/python3 $HERE/comms_ear.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now playcall-comms.service
systemctl restart playcall-comms.service

HOST=$(hostname)
echo
echo "══════════════════════════════════════════════════════════"
echo "  🎧 Comms ear is running."
echo
echo "  Admin page:  http://$HOST.local:8790"
echo "  PIN:         $PIN        (also in $ENV)"
echo
echo "  From a phone on the same WiFi: open the page, enter the"
echo "  PIN, put the earbud in pairing mode, tap Scan → Pair,"
echo "  then tap 🔒 LOCK Bluetooth. Locking is what stops anyone"
echo "  else at the field from pairing their own headset."
echo
echo "  Even easier: team staff get a '⚙ Open settings — no PIN'"
echo "  button on the team comms page on the site — it signs into"
echo "  this page in one tap."
if [ -n "$NEEDS_REBOOT" ]; then
  echo
  echo "  ⚠ REBOOT THIS BOX NOW:  sudo reboot"
  echo "    This install added the bluetooth group and/or the Bluetooth"
  echo "    audio plugin. A session already running never re-reads"
  echo "    those, and the symptom is brutal — buds refuse to connect"
  echo "    (profile-unavailable) and nothing plays. One reboot and it"
  echo "    all works."
fi
echo "══════════════════════════════════════════════════════════"
