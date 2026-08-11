#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════
#  Bake a ready-to-flash PlayCall Encoder SD image.
#
#    sudo bash scripts/build_image.sh raspios-lite-arm64.img.xz [out.img]
#
#  Takes a stock Raspberry Pi OS Lite (64-bit) image, runs the normal
#  installer inside it, and hands back an image where the only thing left
#  for a coach to do is flash it and give it an activation code. See
#  docs/IMAGE.md for what the coach's side looks like.
#
#  Build host: Linux, root, with qemu-user-static registered (the image is
#  arm64 and your build machine probably is not). On Debian/Ubuntu:
#      sudo apt install qemu-user-static binfmt-support xz-utils
#
#  What this does NOT do: create a user account, set a password, or join a
#  Wi-Fi network. Raspberry Pi Imager still does all three when the coach
#  writes the card, and that is deliberate — baking credentials into a
#  shared image is how a fleet ends up with one password.
# ════════════════════════════════════════════════════════════

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
SRC_IMG="${1:-}"
OUT_IMG="${2:-playcall-encoder.img}"
GROW_MB="${GROW_MB:-2048}"     # headroom for packages + MediaMTX + ffmpeg

[[ -f "$SRC_IMG" ]] || {
  echo "Usage: sudo bash scripts/build_image.sh <raspios-lite-arm64.img[.xz]> [out.img]" >&2
  exit 1
}
command -v qemu-aarch64-static >/dev/null || {
  echo "qemu-aarch64-static not found — apt install qemu-user-static binfmt-support" >&2
  exit 1
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
MNT="$WORK/root"
LOOP=""

cleanup() {
  set +e
  mountpoint -q "$MNT/boot/firmware" && umount "$MNT/boot/firmware"
  for d in dev/pts dev proc sys; do mountpoint -q "$MNT/$d" && umount "$MNT/$d"; done
  mountpoint -q "$MNT" && umount "$MNT"
  [[ -n "$LOOP" ]] && losetup -d "$LOOP"
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── unpack + grow ────────────────────────────────────────────────────────────
echo "── preparing $OUT_IMG ──"
if [[ "$SRC_IMG" == *.xz ]]; then
  xz -dc "$SRC_IMG" > "$OUT_IMG"
else
  cp --sparse=always "$SRC_IMG" "$OUT_IMG"
fi
# A stock Lite image has a few hundred MB free; ffmpeg, MediaMTX and the
# Python stack do not fit in that. Grow the file, then the last partition,
# then the filesystem inside it.
truncate -s "+${GROW_MB}M" "$OUT_IMG"
LOOP="$(losetup --show -fP "$OUT_IMG")"
PART_NUM="$(lsblk -nro NAME "$LOOP" | tail -n1 | sed 's/.*p//')"
parted -s "$LOOP" resizepart "$PART_NUM" 100%
losetup -d "$LOOP"; LOOP="$(losetup --show -fP "$OUT_IMG")"
e2fsck -pf "${LOOP}p${PART_NUM}" >/dev/null || true
resize2fs "${LOOP}p${PART_NUM}" >/dev/null

# ── mount ────────────────────────────────────────────────────────────────────
mkdir -p "$MNT"
mount "${LOOP}p${PART_NUM}" "$MNT"
mkdir -p "$MNT/boot/firmware"
mount "${LOOP}p1" "$MNT/boot/firmware"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
cp /usr/bin/qemu-aarch64-static "$MNT/usr/bin/" 2>/dev/null || true
cp /etc/resolv.conf "$MNT/etc/resolv.conf"

# ── run the ordinary installer inside the image ──────────────────────────────
# The same install.sh a coach would run by hand. No activation code: the
# image is generic and one code pairs one box, so the code arrives later,
# per box, via the boot partition or the setup portal.
echo "── installing PlayCall Encoder into the image ──"
mkdir -p "$MNT/tmp/playcall-src"
tar -C "$HERE" --exclude='__pycache__' --exclude='.git' -cf - . \
  | tar -C "$MNT/tmp/playcall-src" -xf -
chroot "$MNT" /bin/bash -c 'cd /tmp/playcall-src && bash install.sh'
rm -rf "$MNT/tmp/playcall-src"

# ── de-identify: an image is copied, so nothing unique may survive ───────────
# A machine-id or SSH host key baked into a shared image means every box
# built from it claims the same identity on the network.
echo "── de-identifying ──"
: > "$MNT/etc/machine-id"
rm -f "$MNT/var/lib/dbus/machine-id" "$MNT"/etc/ssh/ssh_host_*
rm -f "$MNT/etc/playcall-encoder/config.json"   # PIN + ingest key are per-box
rm -rf "$MNT/var/log/"* "$MNT/tmp/"* "$MNT/root/.bash_history"
rm -f "$MNT/usr/bin/qemu-aarch64-static"
chroot "$MNT" /bin/bash -c 'apt-get clean' 2>/dev/null || true

# ── the note the coach actually sees ─────────────────────────────────────────
# The boot partition is the one part of the card Windows and macOS mount
# without complaint, so it is the only place a plain-language file will be
# read. Ship the template next to it.
cat > "$MNT/boot/firmware/playcall-code.txt.example" <<'NOTE'
Rename this file to  playcall-code.txt  and replace the line below with
the activation code from basebook.org (Score Bug Studio -> Encoders ->
"+ Add an encoder").

HAWK-4823

That is the whole setup. On its first boot with internet, this box joins
your team by itself and the code file is deleted.

No code handy? Boot the Pi anyway. It raises a Wi-Fi hotspot named
PlayCall-Encoder-XXXX; join it from your phone and the setup page asks
for the code there instead.
NOTE

sync
echo
echo "── Done ─────────────────────────────────────────────────"
echo "Image: $OUT_IMG"
echo "Compress it for distribution:  xz -T0 -9 $OUT_IMG"
