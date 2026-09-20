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

then inside that prompt, scan for **classic** Bluetooth only — the
adapter is classic (SPP), and a plain `scan on` buries it under every
Bluetooth LE gadget in the house, hundreds of lines of signal-strength
updates with the one you want somewhere in the middle:

```
menu scan
transport bredr
back
scan on
```

Give it twenty seconds, then `scan off` and `devices`. The adapter is
one of a handful of lines, named or not; `info <mac>` shows a `Class:`
line for a classic device and a name once it has resolved.

**A Stalker Pro IIs advertises its own Bluetooth** as `Stalker Pro IIs
NNNN` — that is the gun's built-in radio, not your adapter, and it is
BLE. Ignore it here; it is not the thing being paired.

If the adapter is not in the list, it is not in pairing mode: power it
off and on (unplug it from the gun for five seconds), and if it still
does not appear, try the other position of its slide switch — on some
bricks that switch is a master/slave role selector, and in master mode
the adapter hunts for something to connect to rather than waiting to be
paired. Note its MAC, then:

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

**4. That is all. Leave `radar.port` alone.** A box that runs the gun
on its USB cable some games and over Bluetooth on others keeps the USB
pin exactly as it is: `/dev/rfcomm0` is a **peer** of the pin, not a
correction of it. The box opens the pinned cable, the pinned board and
the Bluetooth lead all at once and follows the gun to whichever is
talking — quietly, rewriting nothing, flagging nothing. Cable one
night, Bluetooth the next, no config change in between. (It used to
rewrite `radar.port` to `/dev/rfcomm0` the first time the gun spoke
over Bluetooth and nag that the pin was wrong, then write it back the
next cable game.) The LED board stays on its own pin throughout.

A plain-format gun (bare numbers, no RD tags) is found over Bluetooth
too, after thirty lines; on a spare USB port bare numbers are still
ignored, because there they could be the board echoing what the box
wrote. Nothing is ever written to rfcomm, so on Bluetooth that echo
cannot happen.

If you never use the cable, you can pin the Bluetooth lead like any
other port, but there is no need:

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

### The IRXON RS-232 adapter — and any BLE serial brick

The second brick this has been used with: an IRXON RS-232↔Bluetooth
adapter with a male DB9 that plugs straight into the gun's jack, two
status LEDs and a small two-position slide switch by the connector.
The one in the field advertises as `VELOBEAM_003`.

* **It is Bluetooth LE, not classic.** `bluetoothctl info` shows
  `AdvertisingFlags: 06` (LE only) and service `0000ffe0`, the HM-10
  family's transparent-UART service. So there is **no SPP to bind**:
  `rfcomm` can never turn it into `/dev/rfcomm0`, and there is **no
  pairing** — `pair` does nothing, and nothing needs it. It hands its
  bytes to whoever subscribes.
* **The box reads it with the BLE serial bridge** (`encoder/
  ble_serial.py`), which subscribes over BLE and presents the bytes as
  a tty at `/run/playcall-encoder/radar-ble`. The radar service opens
  that beside the USB leads and treats it exactly like a cable —
  claim, parse, spin, LED board, all unchanged. Set
  `radar.bluetooth_mac` and nothing else: `radar.bluetooth_kind` is
  `auto`, the bridge finds the MAC in a BLE scan, and on its first
  connection writes `bluetooth_kind: ble` so the rfcomm binder stands
  down on every boot after. A classic adapter never appears in a BLE
  scan and is left to the binder as before.
* **Bytes climbing, radar still "no serial adapter open"?** Two things
  bit on the first field night. A binding the rfcomm binder made for
  the MAC before anyone knew it was BLE leaves `/dev/rfcomm0` behind,
  and opening that blocks on a connect the adapter cannot answer, then
  reads EIO — the radar loop reopened it every 8 s all evening. The
  bridge now releases a binding that names its MAC, and the scan leaves
  `/dev/rfcomm*` out once `bluetooth_kind` is `ble`. And the link
  itself lives in `/run/playcall-encoder`, a directory sibling units
  own as their RuntimeDirectory: a unit file without
  `RuntimeDirectoryPreserve=yes` wipes it when that unit stops. The
  bridge republishes the link every second while the radio is up, and
  the settings page says "link is missing" rather than showing a path
  that is not there. If it does say that, the journal names the culprit:
  `journalctl -u playcall-encoder | grep republished`.
* **Why it took an evening to find.** The classic-Bluetooth page the
  kernel makes for every open of `/dev/rfcomm0` holds the radio for
  ~5 s, and the loop was opening it every 8 s — so the LE scan ran in
  the gaps and the bridge could not reconnect after a restart. That
  restart is also why the link dangled: a pty dies with its process,
  and the new one only published once it connected. Now a known-BLE
  adapter gets its tty the moment the encoder starts, the loop holds
  it open and quiet like a gun between innings, and bytes land when
  the radio comes up. And the kind, once learned, is re-asserted while
  the radio is connected: the settings form saves its select back
  whole, and a page opened before the learn and saved after it had put
  `auto` straight back.
* **Solid blue LED, nothing read, no bridge lines in the journal.** The
  previous encoder process's BLE connection outlived it at the BlueZ
  daemon, so the adapter stopped advertising and the scan could never
  see it. A known-BLE adapter, or one `bluetoothctl info` says is
  connected, is now connected **by address** without waiting for an
  advertisement. And a link left by a dead process is removed before
  the bridge does anything else, because a pty number is handed out
  again — at 20:10 that night to the operator's SSH login, and the
  radar loop "listened" to a shell. The loop also opens the link only
  when the running bridge says it is its own.
* **The bridge is never silent.** Every way it can wait — scan failed,
  scan hung (bounded now; `sudo systemctl restart bluetooth` if it says
  so), MAC not in the scan, what BlueZ knows about the address — is one
  journal line per change (`journalctl -u playcall-encoder | grep
  bleserial`) and the same words under the byte count on the radar
  card. On a miss it asks `bluetoothctl info`: an address BlueZ holds
  connected or has seen as an LE device is connected directly; one it
  knows only as classic, or not at all, is the binder's. And the radar
  loop's warnings about an unplugged USB pin are said once, then at
  most every ten minutes, instead of three lines every five seconds.
* **"BlueZ holds it connected" + "was not found", every pass.** The
  encoder is stopped with SIGTERM (every update restarts it), the BLE
  loop runs in a daemon thread and never says goodbye, and a connection
  nobody closed is bluetoothd's to keep: LED solid blue, nobody
  reading, and no client can reach it, because bleak resolves an
  address through a scan and a connected device does not advertise.
  The bridge now asks BlueZ to drop such a connection (once a minute
  at most), the adapter advertises again within a couple of seconds,
  and the next scan finds it the normal way. And on its own shutdown
  the encoder hands the adapter back, so the next run does not start
  in that state. By hand, the same thing is
  `bluetoothctl disconnect <MAC>`.
* **The slide switch is a TX/RX crossover.** If the bridge is up
  (bytes received climbing on the settings page) and the gun is silent,
  flip it and pull the trigger again. First thing to try.
* **Its UART rate is a setting** and must equal the gun's. Same trap as
  the BT578: connected, delivering perfect garbage. The leaflet gives
  the default (commonly 9600) and how it is changed; the gun, the
  adapter and `radar.baud` want the same number.
* **Blue LED**: blinking = advertising, solid = a client is subscribed.
  Solid after the encoder boots is the bridge holding it.
* It draws power from the gun's port; dark LEDs with the gun on mean
  that port is not feeding it.

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

## Pocket Radar Smart Coach — not supported over Bluetooth

**The capture service is off by default and should stay off.** The gun
will not give its readings to anything except Pocket Radar's own app.
This was established against a real SR1100, from three directions, and
the evidence is written up in `docs/POCKET_RADAR.md` in the site repo
along with the cloud integration that replaces it.

The short version, so nobody spends another evening on it:

* The box connects and BlueZ reports `failed to discover services,
  device disconnected` — every time, against a gun visible on every
  scan.
* A browser gets further (it can see the one vendor service and
  subscribe) and is handed **sixteen zero bytes** on every read, has
  **every write refused**, and is dropped after **1.9 seconds** on the
  dot.
* `bluetoothctl pair` connects and never completes.

That is a product boundary, not a protocol nobody has guessed yet.
`radar.smart_coach: auto` still turns the capture on for a firmware that
one day behaves differently; nothing else about this box changes.

**The Stalker on a cable is the supported gun**, and it gives spin as
well as velocity, which a Smart Coach never does.

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
