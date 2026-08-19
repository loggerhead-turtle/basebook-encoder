#!/usr/bin/env bash
# Keep the radar gun's Bluetooth serial adapter bound to /dev/rfcomm0.
#
# A serial→Bluetooth adapter (HC-05/HC-06 and the RS-232 bricks that
# behave like them) speaks SPP. Linux turns SPP into an ordinary tty via
# an rfcomm binding, after which encoder/radar.py opens /dev/rfcomm0
# exactly as it opens a USB adapter — the gun does not know or care
# which cable it is on.
#
# The MAC comes from radar.bluetooth_mac in the encoder config, so the
# same settings page that holds every other radar setting holds this one
# too. No MAC configured = this exits quietly and the box runs cabled.
#
# PAIRING IS A ONE-TIME MANUAL STEP and deliberately not automated here:
# it needs the adapter's PIN (1234 on most of these modules, sometimes
# 0000), and a script that silently pairs whatever is in range is a bad
# idea on a field full of other people's phones. See docs/RADAR.md.
#
# Binding is idempotent and survives the adapter being switched off —
# the tty stays, reads simply block until it comes back, which is what
# the radar service already tolerates.
set -uo pipefail

CONFIG=/etc/playcall-encoder/config.json
DEV=/dev/rfcomm0
CHANNEL="${RFCOMM_CHANNEL:-1}"

mac() {
  python3 - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
print(((cfg.get('radar') or {}).get('bluetooth_mac') or '').strip())
PY
}

MAC="$(mac)"
if [[ -z "$MAC" ]]; then
  echo "radar-bt: no radar.bluetooth_mac configured — nothing to bind"
  exit 0
fi

if ! command -v rfcomm >/dev/null; then
  echo "radar-bt: rfcomm missing — install bluez" >&2
  exit 1
fi

systemctl is-active --quiet bluetooth || systemctl start bluetooth || true
# The controller can take a moment after boot; without this the first
# bind after a power cut fails and the gun is silent until someone
# notices, which is the whole class of failure this box exists to avoid.
for _ in $(seq 1 10); do
  bluetoothctl show >/dev/null 2>&1 && break
  sleep 1
done
bluetoothctl power on >/dev/null 2>&1 || true

# Trust it so the link re-establishes on its own after the adapter is
# power-cycled between innings.
bluetoothctl trust "$MAC" >/dev/null 2>&1 || true

if [[ -e "$DEV" ]]; then
  echo "radar-bt: $DEV already bound"
  exit 0
fi

if rfcomm bind "$DEV" "$MAC" "$CHANNEL" 2>/dev/null; then
  echo "radar-bt: bound $DEV -> $MAC channel $CHANNEL"
  exit 0
fi

echo "radar-bt: could not bind $DEV -> $MAC." >&2
echo "  Pair it once by hand, then re-run:" >&2
echo "    bluetoothctl --agent" >&2
echo "    scan on ; pair $MAC ; trust $MAC ; quit" >&2
exit 1
