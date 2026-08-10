# The prebuilt SD image

Everything here already works from the one-line installer. The image
exists for the coach who does not want a terminal at all.

## What a coach does

1. In Raspberry Pi Imager: **gear → Custom repository →
   `https://basebook.org/imager.json`**. PlayCall Encoder then appears in
   Imager's own OS list beside Raspberry Pi OS, and Imager handles the
   download, the checksum, and the too-small-card check. (A direct
   `playcall-encoder.img.xz` download works as well.) In Imager's **OS
   customisation** the coach sets the things Imager is good at:
   **username and password**, **Wi-Fi**, and SSH if they want it —
   nothing in the image overrides those.
2. On basebook.org: **Score Bug Studio → Encoders → ➕ Add an encoder**.
   That mints one activation code.
3. Give the box the code, either way round:
   - **With a card reader.** The card is still in the computer after
     flashing. Open the small `bootfs` volume, rename
     `playcall-code.txt.example` to `playcall-code.txt`, put the code
     in it, save, eject.
   - **Without one.** Boot the Pi. With no code it raises a hotspot
     called `PlayCall-Encoder-XXXX`; join it from a phone and the setup
     page asks for the code (step 1) along with Wi-Fi.
4. Power it on. It pairs itself and appears on the Encoders card.

No SSH, no `sudo`, no PIN written on a scrap of paper.

## What makes that work

| Piece | Where |
| --- | --- |
| Redemption logic (normalise, redeem, retry, persist) | `encoder/activation.py` |
| CLI wrapper — what the one-liner and the boot unit call | `scripts/activate.py` |
| First-boot unit: reads the boot partition, retries while offline | `systemd/playcall-encoder-activate.service` |
| Portal field + deferred redemption after the box joins Wi-Fi | `encoder/provisioning.py` (`redeem_pending`) |
| Stored-but-unspent code | `config.json` → `pending_code` (redacted from log bundles) |
| Image build | `scripts/build_image.sh` |
| Imager custom-repo manifest + the coach-facing page | `cloud/scorekeeper/encoder_image.py` (Play-call) |

Three routes, one implementation. A code is normalised the same way
(case, spaces, missing dash all forgiven), spent once, and dropped the
moment the site refuses it — otherwise a bad code re-asks the same
question on every boot forever.

The code file is **deleted from the boot partition** as soon as it is
spent. It is a bearer credential for the team on a FAT partition that any
computer can read; leaving it there would be the one insecure step in the
flow.

## Building the image

On a Linux host, as root:

```bash
sudo apt install qemu-user-static binfmt-support xz-utils parted
sudo bash scripts/build_image.sh 2025-xx-xx-raspios-bookworm-arm64-lite.img.xz
xz -T0 -9 playcall-encoder.img
```

It grows the stock image, runs the ordinary `install.sh` inside it under
qemu, then strips everything that must not be shared between boxes:
`machine-id`, SSH host keys, logs, and `config.json` (which holds the
recovery PIN and the local ingest key — both per-box).

It deliberately does **not** bake in a user account, a password, or a
Wi-Fi network. One shared image with one shared password is how a fleet
ends up compromised; Imager already asks for those per card.

## Before publishing an image

- [ ] Flash it and check the box comes up with no code (hotspot appears).
- [ ] Flash it, drop in `playcall-code.txt`, confirm it pairs itself and
      the file is gone afterwards.
- [ ] Confirm a second box built from the same image gets a **different**
      recovery PIN and ingest key (proves `config.json` was stripped).
- [ ] Confirm Imager's username/Wi-Fi customisation still applies.
- [ ] Re-run `install.sh` on a box built from the image and confirm it
      upgrades without trying to re-spend a code.
- [ ] Set `ENCODER_IMAGE_URL` (plus `_SHA256`, `_BYTES`,
      `_EXTRACT_BYTES`, `_EXTRACT_SHA256`, `_VERSION`, `_DATE`) on the
      site. Until that is set, `/imager.json` returns an empty list and
      `/score/encoder/image` says the image is not out yet — which is
      what makes publishing a release step rather than a code change.
- [ ] Paste the custom-repo URL into a real Raspberry Pi Imager and
      confirm the entry appears, downloads, and verifies.
