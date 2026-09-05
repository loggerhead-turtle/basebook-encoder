# Radar gun & LED board

The gun talks RS-232. The box reads it, forwards speeds to the LED
board, and pushes velo/spin to the cloud. Velo is **decoration** —
nothing in this pipeline can write to the scorebook.

Two ways to get the gun's serial data into the box:

* **Cabled** — USB→RS-232 adapter, appears as `/dev/serial/by-id/…`
* **Bluetooth** — a serial→Bluetooth adapter, bound to `/dev/rfcomm0`

The radar service does not care which. Both are just ttys.

---

## Gun settings

The box's parser is the source of truth for what it can read. These are
the settings that matter; the exact menu labels differ between Stalker
firmware revisions, so find them in your gun's manual rather than
trusting a menu path from here.

| Setting | Value | Why |
|---|---|---|
| **Baud** | **19200** | The box's default (`BAUD`). Any rate works if you also set `radar.baud` to match — but if you change one and not the other you get silence or character salad, never a useful error. |
| **Frame** | **8N1** | 8 data bits, no parity, 1 stop bit. |
| **Output mode** | **Continuous / streaming** | The box reads a stream of frames, not a polled reading. A gun set to send only on request never says anything. |
| **Format** | Multi-value (`RD 34x … 5x … 9A…`) **or** plain speed-per-line | Both are handled. Multi-value is better: it carries live speed, peak, **and spin**. Plain format gives speed only. |
| **Units** | Either | The tag suffix letter changes with units/format (`34C` on the bench, `34A` in the field). Any letter is accepted. |
| **LO threshold** | **Below your slowest pitcher** | Set the gun permissive and let software filter. The box's plausible-pitch band is 30–110 mph; a gun that filters at 60 throws away the data the box needs to tell a pitch from a throw. |

### Verify before a game

```bash
sudo journalctl -u playcall-encoder -f | grep -i radar
```

Fire the gun at something moving. You want parsed frames climbing. The
Field check on the site shows the same thing as a percentage: **a low
parse rate means the gun is speaking a format or rate the box does not
expect** — check baud first, format second.

---

## Which adapter is which

With two adapters plugged in (gun + LED board), the box works out which
is which: the first port that parses gun frames claims the title, and a
wrong claim self-corrects — a display board chatters status back up its
own cable, and one lucky parse could crown it, after which the real gun
is never read and the board stays dark.

To settle it permanently, pin both:

```json
"radar": {
  "port": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXX-if00-port0",
  "display_port": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_YYYYYYY-if00-port0",
  "baud": 19200,
  "display_baud": 9600
}
```

**You should never need to write these by hand: the box learns its
cables.** The gun's cable ends in a male serial plug and the board's in
a female, so each adapter is physically married to its device for life
— and the by-id name encodes the adapter's own serial number, so it
does not care which USB socket it lands in. The first time a port
proves what it is (multi-tag RD frames that only a Stalker emits, or
sustained parses on a lone adapter), the box writes the roles to config
itself and logs `learned the cables`. Replace a cable and it re-learns
on the first trigger pull. Plug into different USB ports every game and
nothing changes at all.

**Same-model cables: check whether your adapters have real serials.**
With both cables plugged in:

```bash
ls -l /dev/serial/by-id/; ls /dev/ttyUSB*
```

Two by-id entries with **different** serial strings = trustworthy names;
the box remembers its cables and everything above applies. **One by-id
entry while two ttyUSB devices exist, or two entries whose serial
strings match** = clone chips sharing a serial. Those names point at
whichever cable enumerated last — the gun on one boot, the board on the
next — so the box refuses to remember them (it logs `same-model clone
cables`) and instead identifies the gun by listening on every boot:
both ports are watched in silence, the first one producing radar frames
is the gun, the other is therefore the board, and only then is anything
written to it. That works regardless of names; the only cost is a few
seconds of listening after each boot. Cables with genuine unique
serials are worth the money for a product build.

**A pin is a preference, not a requirement.** A by-id path carries the
adapter's serial number, so an adapter that is unplugged, swapped or
dead takes the pin with it. When a pinned port is missing the box logs
it once and scans for any adapter instead. (It used to retry the dead
path every five seconds for ever, ignoring the adapter that *was*
plugged in — that cost a game's velo.)

Gun and board rates are **independent**: `radar.baud` for the gun,
`radar.display_baud` for the board. A Stalker on LO may talk 9600 while
its board wants something else entirely.

---

## Bluetooth

One-time pairing, then it is automatic for ever.

**1. Find the adapter's MAC.** With it powered and in range:

```bash
bluetoothctl --agent
```

then inside that prompt:

```
scan on
```

Wait for your adapter to appear, note its MAC, then:

```
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
quit
```

Most of these modules use PIN **1234** (some **0000**). Pairing is
deliberately manual — a script that silently pairs whatever is in range
is a bad idea at a field full of other people's phones.

**2. Tell the box.** Add the MAC to the config:

```json
"radar": { "bluetooth_mac": "AA:BB:CC:DD:EE:FF" }
```

**3. Bind it.**

```bash
sudo systemctl restart playcall-encoder-radarbt
sudo systemctl status playcall-encoder-radarbt
```

You want `bound /dev/rfcomm0 -> AA:BB:CC:DD:EE:FF`. The binder runs at
every boot, before the radar service scans, and exits quietly on boxes
with no MAC configured.

**4. Point the gun at it** (optional). `/dev/rfcomm0` is discovered
automatically, but you can pin it like any other port:

```json
"radar": { "port": "/dev/rfcomm0" }
```

### The BT578 V3 specifically

The adapter this was built against: an RS-232↔Bluetooth brick with both
male and female serial heads and Type-C for power.

* **The Type-C port is power only.** It charges/feeds the module; it is
  not a data path. The serial data rides Bluetooth.
* **Which head you use mirrors the cable rule**: the gun end wants the
  head that mates the gun's connector. The module doesn't care — it
  forwards whatever arrives.
* **Its own serial rate is a setting, and it ships at 9600.** This is
  the trap: the module has an internal UART rate (changed over AT
  commands from a paired terminal — see its leaflet), independent of
  Bluetooth. A module at its factory 9600 in front of a gun talking
  19200 delivers perfectly-paired, perfectly-connected **garbage** —
  which on the test page reads as "lines seen, nothing parses". Either
  set the module to 19200, or set the gun AND `radar.baud` to 9600.
  `radar.baud` describes the rate on the tty, which for Bluetooth is
  whatever the module was told to speak.
* **Pairing PIN** is typically `1234` (sometimes `0000`) — the leaflet
  wins.
* Battery bricks sleep: if readings stop between innings and resume on
  the next trigger pull, the module's power saving is dozing — keep it
  on Type-C power at the gun end.

### Bluetooth notes worth knowing

* **Set the adapter's baud to match the gun.** These modules have their
  own serial rate, configured over AT commands, and it is independent of
  the Bluetooth link. Adapter at 9600 with a gun at 19200 produces
  garbage that looks exactly like a broken gun.
* **The tty survives the adapter losing power.** Reads block until it
  comes back; the radar service already tolerates that. Trusting the
  device is what lets the link re-establish itself between innings.
* **Range and interference.** A ball field is a crowded 2.4 GHz
  environment. Bluetooth SPP is robust but not magic — keep the adapter
  in line of sight of the box where you can.
* **Cabled is still more reliable.** Bluetooth removes a cable run at
  the cost of one more thing that can fail. If the gun is near the box
  anyway, use the cable.

---

## Pocket Radar Smart Coach (BLE)

The Smart Coach (SR1100) has **no wired data output** — its micro-USB
port is power and firmware updates only. Readings leave the gun
exclusively as Bluetooth LE notifications to the Pocket Radar phone
app, so the box runs a second, independent capture service
(`encoder/smart_coach.py`) that plays the app's role. It feeds the same
cloud endpoint as the Stalker reader; the pad tile, score bug and
play-by-play stamps don't know or care which gun spoke.

Setup is one step: turn the gun on near the box with the phone app
**closed**. Pocket Radar has never published the protocol, so the box
learns it — it connects to anything advertising as a Pocket Radar,
subscribes to every notifying characteristic, and decodes defensively;
after three consistent readings it writes the gun's MAC,
characteristic and wire format to config
(`radar.smart_coach_mac/_char/_decode`) and reconnects straight to it
on every boot. The settings page can pin the MAC up front and switch
the capture off (`radar.smart_coach: off`).

Know what you're trading against the Stalker:

* **One number per pitch.** No deceleration curve (no plate speed), no
  spin, no track shape — so no throw detection either: every in-band
  reading files as a one-frame pitch, out-of-band as a ghost.
* **One client at a time.** While the box holds the connection the
  phone app cannot connect, and a gun already connected to a phone is
  invisible to the box's scan. Close the app at the field.
* **Unofficial protocol.** A firmware update could change the wire
  format. The learned decode is pinned, so a change shows up loudly as
  "undecoded payloads" in the journal rather than as silently wrong
  speeds — press **Forget learned cables** on the settings page and let
  the box re-learn.
* **Needs `python3-bleak`** (the installer adds it; without it the
  service logs one line and stays out of the way).

Diagnose with `journalctl -u playcall-encoder | grep -i 'smart coach'`
— the first raw payloads are logged hex-escaped, which settles the wire
format in one glance, exactly like the serial reader's rx samples.

---

## When there is no velo

In order, cheapest first:

1. **Is an adapter present?** `ls -l /dev/serial/by-id/ /dev/rfcomm*`
2. **Is the service reading it?** `journalctl -u playcall-encoder | grep -i radar | tail -20`
   — "no USB-serial adapter present" means nothing is plugged in;
   "pinned radar port … is not there" means the pin outlived its adapter.
3. **Is the gun speaking?** Parsed frames climbing = yes. Lines seen but
   nothing parsed = wrong baud or wrong format.
4. **Is the board on its own rate?** A dark board with good velo on the
   site is `display_baud`, not the gun.
5. **Are the adapters swapped?** The log says so explicitly when a port
   claims the gun and then fails to parse.
