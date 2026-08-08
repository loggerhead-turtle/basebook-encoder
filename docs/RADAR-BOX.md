# The radar box — velo on the LAN, with a cloud shadow

A tiny standalone appliance that does one thing: capture the Stalker
Pro II s and serve velo to the app **without needing the internet**.
It runs the exact same capture as a full encoder box
(`encoder/radar.py` — same parser, same pitch/throw/ghost calls, all
bench-verified against the gun), minus everything a Zero 2 can't and
shouldn't run: no MediaMTX, no YouTube push, no clipper.

```
Stalker Pro II s ──RS-232──► radar box (Pi Zero 2 W)
                               ├─ LAN feed: _basebook-radar._tcp :8791
                               │    JSON lines: hello / live / burst / alive
                               └─ cloud shadow: POST /api/encoder/radar
                                    (when paired — optional)
```

## Hardware

* Pi Zero 2 W (or anything better — the capture loafs on a Zero 2).
* USB→RS-232 adapter to the gun's serial port, **19200 8N1**. An
  FTDI-chipset adapter gets a stable `/dev/serial/by-id/` path; the
  service scans for whatever is plugged in either way.
* Power. That's the whole box.

## Install

Flash Raspberry Pi OS Bookworm Lite, get the Pi on the field network
your way (Raspberry Pi Imager presets are the easy path), then from a
repo checkout:

```bash
sudo bash scripts/install_radar_box.sh
```

That installs the `playcall-encoder-radar` service
(`python3 -m encoder.radar_standalone`), the Avahi advertisement, and
nothing else. Re-run it any time to upgrade — it never touches an
existing config. Logs: `journalctl -u playcall-encoder-radar -f`.

**Cloud shadow (optional).** Paired, the box also POSTs readings to
`/api/encoder/radar` exactly as a full encoder does, so the cloud can
stamp velo onto plays and clips server-side. Drop `cloud.base_url` +
`cloud.api_key` into `/etc/playcall-encoder/config.json` (or ship a
boot-partition preconfig, see `preconfig/README.md`). Unpaired, the box
is LAN-only and fully useful.

## How the app finds it

The box advertises **`_basebook-radar._tcp`** over mDNS and serves
**newline-delimited JSON over plain TCP** on the advertised port
(default 8791, `RADAR_LAN_PORT` overrides — edit the Avahi file to
match). The app browses, connects a plain socket, and reads one JSON
object per line:

* `hello` — first line on connect: service version, gun-connected state
* `live` — velo (mph) + rpm (null until the gun computes it)
* `burst` — one per tracked object, already classified pitch/throw/ghost
* `alive` — idle heartbeat, so "gun asleep" (a Stalker dozes between
  pitches) never reads as "box gone"

The schema is `protocol/radar-lan.schema.json` in the basebook-stream
repo. A full encoder box serves the identical feed in-process, so the
app treats both the same; multiple finds (backstop vs bullpen gun) are
an operator pick, not a bug.

Slow readers never hurt the feed: each client gets a small drop-oldest
queue, so a stalled phone sees gaps, never stale numbers, and a wedged
connection is dropped.

## The invariant

**Nothing in the radar path writes to the scorebook.** Not the box,
not the LAN feed, not the cloud shadow. Velo is decoration — it rides
the velo pill, the overlay, and clip metadata, and the scorekeeper's
book never hears about it. Idle frames are dropped at read time, events
are tiny, nothing raw is retained.
