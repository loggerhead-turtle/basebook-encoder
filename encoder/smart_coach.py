#!/usr/bin/env python3
"""Pocket Radar Smart Coach (SR1100) capture — Bluetooth LE.

The Smart Coach has no wired data output: its micro-USB port is power
and firmware only, and readings leave the gun exclusively as BLE GATT
notifications to the Pocket Radar phone app. This module is the box's
version of that app: find the gun, subscribe, decode, and feed the SAME
cloud pipeline the Stalker reader feeds (POST /api/encoder/radar), so
the pad velo tile, the score bug and the play-by-play stamps light up
identically whichever gun is at the field.

Pocket Radar has never published the protocol, so nothing here trusts a
guessed UUID. THE BOX LEARNS THE GUN, the same way radar.py learns its
serial cables:

  * scan for an advertisement that NAMES itself a Pocket Radar (or the
    pinned radar.smart_coach_mac);
  * subscribe to EVERY characteristic that can notify;
  * decode each notification defensively — ASCII digits and the common
    integer/float encodings, accepted only inside a plausible speed
    band — and log the first payloads raw, so one
    `journalctl | grep 'smart coach'` settles the true wire format;
  * after a few consistent readings on one characteristic, write the
    MAC, SERVICE, characteristic and decode back to config
    (radar.smart_coach_mac/_service/_char/_decode). Reboots reconnect
    straight to the proven gun.

The service UUID is of no use to this module — bleak enumerates the
whole GATT tree and never has to ask. It is learned for the PHONES: a
web page may only touch services it named before it opened the chooser
(there is no wildcard in Web Bluetooth), so a phone can never discover
an unpublished vendor service for itself. This box can, and the answer
rides the heartbeat to the cloud, which hands it to every phone on the
team (/api/camera/radar/hints). One box meeting the gun once is what
lets a phone behind the plate hold it — see static/radar_ble.js.

What a Smart Coach cannot give: the deceleration curve (no plate
speed), spin, or track shape. Each pitch is ONE peak number, so every
in-band reading files as a one-frame 'pitch' burst; the throw/ghost
split the Stalker gets from track duration does not exist here (out of
band still files as 'ghost'). BLE also allows one client at a time:
while this box holds the connection the phone app cannot, and a gun
already connected to a phone is invisible to the scan.

Runs beside the Stalker service, never instead of it — the two share
nothing but the cloud endpoint, and the cloud merges freshest-wins.

HARD INVARIANT (same as radar.py): display-only data. Nothing in this
pipeline can write to the scorebook.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import threading
import time
from collections import deque

# the Stalker module's tunables are the contract the cloud already
# understands — importing them (never editing them) keeps one truth
from .radar import ALIVE_INTERVAL, BAND, LIVE_MIN_INTERVAL, POST_PATH

log = logging.getLogger('smartcoach')

# What the gun calls itself over the air. Field units advertise with
# "Pocket Radar" in the name; the model number covers a firmware that
# says only that; and a real unit turned up calling itself "SC-236" —
# Smart Coach abbreviated plus a unit number — which matched none of
# the above, so this box would have scanned straight past its own gun
# for ever unless somebody pinned the MAC by hand. The trailing digit
# is what keeps this from adopting every three-letter gadget in the
# next dugout.
NAME_RE = re.compile(r'pocket\s*radar|smart\s*coach|sr-?1100|^sc-?\d',
                     re.I)

# A decoded number is believed only inside this band (mph). Wider than
# the pitch BAND on purpose: the gun itself measures 25–130, and a
# reading between the bands should file as a ghost, not silently
# convince the decoder to try a different encoding until one "fits".
PLAUSIBLE = (15.0, 135.0)

# Consistent readings (same characteristic, same decode) before the gun's
# identity is written to config. Three real pitches is proof; one could
# be a coincidence of bytes.
LEARN_READINGS = 3

SCAN_S = 8.0            # one BLE scan window
RESCAN_IDLE_S = 15      # pause between scans while no gun is found

_ASCII_NUM = re.compile(r'^\s*(\d{1,4}(?:\.\d+)?)\s*$')

_MPS_TO_MPH = 2.23694


def _band(v):
    return PLAUSIBLE[0] <= v <= PLAUSIBLE[1]


def candidates(data):
    """Every encoding this payload COULD be, in the FIXED order the
    decoder tries them: [(name, value), ...].

    One ladder, three readers — decode_reading() below, the probe script
    (scripts/radar_ble_probe.py) that prints the whole table when a gun
    speaks a format nobody has seen, and its browser twin in
    static/radar_ble.js. The order is not cosmetic: it is what makes
    "the same decode three times" mean anything.

    Names: ascii, ascii_x10, u8, u16le, u16le_x10, u16le_cmps
    (hundredths of m/s — the SI-flavored encoding BLE sensors love),
    u16be, u16be_x10, f32le.
    """
    tries = []
    if not data:
        return tries

    def offer(name, v):
        if v is not None:
            tries.append((name, v))

    try:
        text = bytes(data).decode('ascii')
        m = _ASCII_NUM.match(text.replace('\x00', ' '))
        if m:
            v = float(m.group(1))
            offer('ascii', v)
            if '.' not in m.group(1):
                offer('ascii_x10', v / 10.0)
    except (UnicodeDecodeError, ValueError):
        pass
    n = len(data)
    if n == 1:
        offer('u8', float(data[0]))
    if n >= 2:
        le = struct.unpack_from('<H', data)[0]
        be = struct.unpack_from('>H', data)[0]
        offer('u16le', float(le))
        offer('u16le_x10', le / 10.0)
        offer('u16le_cmps', le / 100.0 * _MPS_TO_MPH)
        offer('u16be', float(be))
        offer('u16be_x10', be / 10.0)
    if n >= 4:
        try:
            offer('f32le', float(struct.unpack_from('<f', data)[0]))
        except struct.error:
            pass
    return tries


def decode_reading(data, want=None):
    """One notification payload → (mph, decode_name) or None.

    The first encoding on the ladder that lands in the plausible band
    wins. `want` pins one decoder (the learned config) and tries nothing
    else, so a firmware update that changes the format goes loudly
    unparsed instead of silently misread.
    """
    for name, v in candidates(data):
        if want is not None and name != want:
            continue
        if _band(v):
            return round(v, 1), name
    return None


def status_line(h):
    """The Smart Coach's state as one sentence for the settings page.

    "Not found" used to cover four situations that want opposite things
    done about them — no Bluetooth on this box, nothing in range,
    plenty in range but none of it the pinned address, and has not
    looked yet — and a field box sat on the third of those with a
    correct-looking pin while its owner checked a gun that was fine.

    The cloud says the same four on its own screens
    (cloud/routes/scorekeeper.py: ble_gun_state); the wording is
    deliberately close and the CASES are the contract.
    """
    h = h or {}
    if h.get('bleak') is False:
        return ('⚫ Smart Coach: Bluetooth support is not installed on this '
                'box — sudo apt install -y python3-bleak (or sudo pip3 '
                'install bleak --break-system-packages), then restart the '
                'encoder')
    if h.get('stood_down'):
        return '⚫ Smart Coach: standing down — ' + str(h['stood_down'])
    if h.get('connected'):
        who = h.get('name') or h.get('device') or '?'
        heard = h.get('heard_s')
        return ('🟢 Smart Coach connected: ' + str(who)
                + (f' — last reading {int(heard)}s ago'
                   if heard is not None else
                   ' — no reading yet (pull the trigger once)')
                + (' · gun learned ✓' if h.get('learned') else ''))
    if h.get('connect_error') and h.get('found_s') is not None:
        return ('🔴 Smart Coach: found ' + str(h.get('name') or h.get('device')
                                               or 'the gun')
                + ' and could not hold the connection — '
                + str(h['connect_error'])
                + f' (failed {int(h.get("connect_fails") or 0)}×). '
                + 'The gun refuses everything to a client that is not its '
                'own app — see docs/POCKET_RADAR.md. There is nothing to '
                'fix on this box.')
    if h.get('scan_error'):
        return ('🔴 Smart Coach: this box cannot scan for Bluetooth at all '
                f'— {h["scan_error"]}. Check: systemctl status bluetooth, '
                'rfkill list, bluetoothctl show. The gun is not the '
                'problem.')
    if h.get('seen') is None:
        return '⚫ Smart Coach: starting up — no scan has finished yet'
    if not h.get('seen'):
        return ('⚫ Smart Coach: nothing at all is advertising nearby '
                '(0 devices in the last scan) — turn the gun on, and check '
                'this box has a Bluetooth antenna')
    names = ', '.join(n for n in (h.get('seen_names') or []) if n != '?')
    seen = int(h.get('seen') or 0)
    if h.get('pinned'):
        return (f'⚫ Smart Coach: {seen} devices in range and NONE of them '
                'has the pinned address. Either the pin is wrong, or this '
                'gun advertises a rotating (random) address, which a pin '
                'can never match — clear the MAC field and let it match by '
                'name instead.'
                + (f' Seen: {names}' if names else ''))
    return (f'⚫ Smart Coach: {seen} devices in range, none named like a '
            'Pocket Radar. Scan below and pin the gun by hand.'
            + (f' Seen: {names}' if names else ''))


class SmartCoachService:
    """BLE notify → decode → cloud. Runs as a daemon thread; silently
    idles when bleak (or Bluetooth hardware) is missing, and keeps
    scanning so turning the gun on mid-game just works.

    The cloud-facing half deliberately mirrors radar.RadarService: the
    notify callback only records intent and a sender thread owns all
    HTTP (the serial reader learned that lesson the hard way — see the
    push() comment there); with no sender running, a kick sends inline,
    keeping unit tests synchronous."""

    def __init__(self, link, cfg_load=None, cfg_save=None):
        self.link = link
        self.cfg_load = cfg_load or (lambda: {})
        self.cfg_save = cfg_save            # None → encoder.config
        self.stood_down = ''                # why _want() said no (a Stalker BT adapter)
        self.running = True
        self.pending = deque(maxlen=200)
        self._last_live_post = 0.0
        self._last_alive_post = 0.0
        self._last_live = (None, None)
        self._live_out = None
        self._force_alive = False
        self._send_wake = threading.Event()
        self._sender = None
        self._post_fails = 0
        self.connected = False
        self.device = None                  # MAC once found
        self.device_name = None
        self.char = None                    # proven characteristic uuid
        self.service = None                 # …and the service it lives in
        self.decode = None                  # proven decode name
        self.notifies = 0
        self.readings = 0
        self.unparsed = 0
        self.last_gun_t = None
        self.learned = False                # persisted identity this run
        self.have_bleak = None              # None until the loop checks
        self.scan_error = ''                # why the last scan failed
        self.connect_error = ''             # …and why the last connect did
        self.connect_fails = 0
        self.found_at = None                # a scan last matched the gun
        self.scan_at = None                 # when a scan last finished
        self.seen = None                    # devices the last scan saw
        self.seen_names = []                # …and what they call themselves
        # (uuid, decode) → consecutive in-band readings, for the learner
        self._streak = {}
        # characteristic uuid → the service it was found under
        self._svc_of = {}

    # ── cloud (same contract as radar.RadarService) ──────────────────────────
    def _post(self, payload):
        base, _ = self.link._cloud()
        if not base:
            return False
        try:
            self.link.http(f'{base}{POST_PATH}',
                           headers=self.link._headers(), payload=payload)
            self._post_fails = 0
            return True
        except Exception as e:
            self._post_fails += 1
            if self._post_fails == 1 or self._post_fails % 50 == 0:
                log.warning(f'smart coach post failed x{self._post_fails} '
                            f'(retrying): {e}')
            else:
                log.debug(f'smart coach post failed (retrying): {e}')
            return False

    def push(self, live=None, event=None, force_alive=False, now=None):
        now = time.monotonic() if now is None else now
        if event:
            self.pending.append(event)
        if (live is not None and live != self._last_live
                and now - self._last_live_post >= LIVE_MIN_INTERVAL):
            self._live_out = live
        if force_alive:
            self._force_alive = True
        if (self._live_out is not None or self.pending or self._force_alive
                or now - self._last_alive_post >= ALIVE_INTERVAL):
            if self._sender is not None and self._sender.is_alive():
                self._send_wake.set()
            else:
                self._send_now(now)

    def _send_now(self, now):
        live = self._live_out
        want_live = live is not None
        want_alive = self._force_alive \
            or now - self._last_alive_post >= ALIVE_INTERVAL
        evs = list(self.pending)
        if not (want_live or evs or want_alive):
            return False
        payload = {'alive': True,
                   'gun': {'heard_s': (round(now - self.last_gun_t, 1)
                                       if self.last_gun_t is not None
                                       else None),
                           'connected': bool(self.connected),
                           'source': 'smart_coach'}}
        if want_live:
            payload['live'] = {'velo': live[0], 'rpm': live[1]}
        if evs:
            payload['events'] = evs
        if not self._post(payload):
            return False
        if want_live:
            self._last_live_post = now
            self._last_live = live
            if self._live_out == live:
                self._live_out = None
        self._force_alive = False
        self._last_alive_post = now
        for _ in evs:
            try:
                self.pending.popleft()
            except IndexError:
                break
        return True

    def _send_loop(self):
        while self.running:
            self._send_wake.wait(timeout=1.0)
            self._send_wake.clear()
            try:
                self._send_now(time.monotonic())
            except Exception:
                log.debug('smart coach sender pass failed', exc_info=True)

    def ensure_sender(self):
        if self._sender is None or not self._sender.is_alive():
            self._sender = threading.Thread(target=self._send_loop,
                                            daemon=True,
                                            name='smartcoach-sender')
            self._sender.start()

    # ── learning ─────────────────────────────────────────────────────────────
    def _persist(self):
        """Write the proven gun back to config — once per run, best
        effort forever: a config that cannot be written (dev checkout,
        read-only /etc) must never touch capture."""
        if self.learned:
            return False
        if getattr(self, '_learn_attempts', 0) >= 3:
            return False
        self._learn_attempts = getattr(self, '_learn_attempts', 0) + 1
        try:
            if self.cfg_save is not None:
                save = self.cfg_save
                cfg = dict(self.cfg_load() or {})
            else:
                from . import config as _config
                cfg = _config.load()
                save = _config.save
            rad = dict(cfg.get('radar') or {})
            changed = []
            for k, v in (('smart_coach_mac', self.device),
                         ('smart_coach_service', self.service),
                         ('smart_coach_char', self.char),
                         ('smart_coach_decode', self.decode)):
                if v and rad.get(k) != v:
                    rad[k] = v
                    changed.append(f'{k.split("_")[-1]}={v}')
            if not changed:
                self.learned = True
                return False
            cfg['radar'] = rad
            save(cfg)
            self.learned = True
            log.warning('learned the Smart Coach and wrote it to config: '
                        + ', '.join(changed)
                        + ' — reboots reconnect straight to it')
            return True
        except Exception as e:
            log.warning(f'could not persist learned Smart Coach ({e}) — '
                        'identity stays runtime-only this boot')
            return False

    # ── the pipeline (test entrypoint) ───────────────────────────────────────
    def handle_notify(self, char_uuid, data, t=None, service_uuid=None):
        """One GATT notification through the whole pipeline. Returns the
        event dict when the payload decoded, else None.

        `service_uuid` is remembered, never judged: it is the one fact a
        browser cannot find out for itself (see the module docstring)."""
        t = time.monotonic() if t is None else t
        if service_uuid:
            self._svc_of[str(char_uuid)] = str(service_uuid)
        self.notifies += 1
        data = bytes(data or b'')
        if self.notifies <= 3:
            # the first few RAW payloads — one glance settles "is the
            # gun talking, and in which format?" (radar.py's rx sample)
            log.info(f'smart coach rx sample: {str(char_uuid)[-12:]} '
                     f'{data.hex(" ")!r}')
        # a learned characteristic mutes the rest of the gun's chatter
        # (battery notifies, button events) instead of asking the
        # decoder to reject them byte by byte
        if self.char and str(char_uuid) != self.char:
            return None
        got = decode_reading(data, want=self.decode)
        if got is None:
            self.unparsed += 1
            if self.unparsed <= 3:
                log.info(f'smart coach payload did not decode: '
                         f'{str(char_uuid)[-12:]} {data.hex(" ")!r}')
            elif self.unparsed % 200 == 0:
                log.warning(f'smart coach: {self.unparsed} undecoded '
                            f'payloads of {self.notifies} — firmware '
                            'format change? clear the learned gun in '
                            'settings to re-learn')
            return None
        mph, how = got
        self.readings += 1
        self.last_gun_t = t
        if not (self.char and self.decode):
            key = (str(char_uuid), how)
            self._streak[key] = self._streak.get(key, 0) + 1
            self._streak = {k: v for k, v in self._streak.items()
                            if k == key}        # consistency, not volume
            if self._streak[key] >= LEARN_READINGS:
                self.char, self.decode = key
                self.service = self._svc_of.get(self.char) or self.service
                self._persist()
        # one reading = one whole burst. No track shape exists to call a
        # throw, so in-band is a pitch and out-of-band is a ghost — same
        # bands, same row shape the cloud already stores.
        kind = 'pitch' if BAND[0] <= mph <= BAND[1] else 'ghost'
        ev = {'kind': kind, 'peak': mph, 'plate': None, 'rpm': None,
              'frames': 1, 'dur': 0.0}
        if kind != 'ghost':
            log.info(f'smart coach pitch: {mph} mph ({how}) '
                     f'| peak={mph} decode={how}')
        self.push(live=(mph, None), event=ev, now=t)
        return ev

    def health(self):
        """The heartbeat's Smart Coach card — same reasoning as
        radar.health(): the whole outage class must be readable from
        the site, not from SSH."""
        return {
            'connected': bool(self.connected),
            'device': self.device,
            'name': self.device_name,
            'service': self.service,
            'char': self.char,
            'decode': self.decode,
            'notifies': self.notifies,
            'readings': self.readings,
            'unparsed': self.unparsed,
            'heard_s': (round(time.monotonic() - self.last_gun_t, 1)
                        if self.last_gun_t is not None else None),
            'learned': bool(self.learned),
            'bleak': self.have_bleak,
            # the scan's own account of itself — the difference between
            # "no Bluetooth on this box", "nothing in range", "plenty in
            # range, none of it the pinned address" and "has not looked
            # yet", all four of which used to print as "not found"
            'scan_error': self.scan_error or '',
            'stood_down': getattr(self, 'stood_down', '') or '',
            'connect_error': self.connect_error or '',
            'connect_fails': self.connect_fails,
            'found_s': (round(time.monotonic() - self.found_at, 1)
                        if self.found_at is not None else None),
            'seen': self.seen,
            'seen_names': list(self.seen_names or []),
            'pinned': bool((((self.cfg_load() or {}).get('radar') or {})
                            .get('smart_coach_mac') or '').strip()),
            'looked_s': (round(time.monotonic() - self.scan_at, 1)
                         if self.scan_at is not None else None),
        }

    # ── BLE loop ─────────────────────────────────────────────────────────────
    def _want(self, cfg):
        """OFF unless a box explicitly opts in.

        The Smart Coach answers a connection from anything and then hands
        a non-app client nothing: no services to the box, refused writes
        and sixteen zero bytes to a browser, dropped after 1.9 s either
        way. That is a deliberate product boundary, not a bug to find —
        see docs/POCKET_RADAR.md — so the default is not to spend a scan
        every fifteen seconds on a gun that will never answer. The
        setting stays for a firmware that one day might."""
        rad = (cfg or {}).get('radar') or {}
        if (rad.get('smart_coach') or 'off') != 'auto':
            self.stood_down = ''
            return None
        # One gun per team. A Stalker on a Bluetooth serial adapter is
        # read by encoder/ble_serial.py in this same process, and two
        # LE scanners here are one too many: this module's scan held
        # the adapter and the bridge's failed 'Operation already in
        # progress' on every pass, for an hour, while the settings
        # page showed 26 devices seen and none of them the gun that
        # was never going to be there (19 Sep 2026).
        if (rad.get('bluetooth_mac') or '').strip():
            self.stood_down = ('a Stalker Bluetooth adapter is configured '
                               f"({rad.get('bluetooth_mac').strip().upper()}) "
                               '— one gun per team; clear that field to '
                               'look for a Smart Coach instead')
            return None
        self.stood_down = ''
        return rad

    def _match(self, dev, rad):
        """Is this scan result the gun? A pinned MAC is exact; auto mode
        requires the NAME — the box must never latch onto whatever BLE
        widget the neighboring dugout brought."""
        mac = (rad.get('smart_coach_mac') or '').strip().upper()
        addr = (getattr(dev, 'address', '') or '').upper()
        if mac:
            return addr == mac
        return bool(NAME_RE.search(getattr(dev, 'name', '') or ''))

    async def _run(self, bleak):
        while self.running:
            cfg = self.cfg_load()
            rad = self._want(cfg)
            if rad is None:
                self.connected = False
                await asyncio.sleep(10)
                continue
            # adopt a learned identity from config (fresh boot)
            self.char = self.char or rad.get('smart_coach_char') or None
            self.decode = self.decode or rad.get('smart_coach_decode') or None
            self.service = self.service or rad.get('smart_coach_service') \
                or None
            try:
                from .ble_serial import SCAN_LOCK   # one LE scan at a time
                with SCAN_LOCK:
                    devs = await bleak.BleakScanner.discover(timeout=SCAN_S)
            except Exception as e:
                # no adapter / bluetoothd down / rfkill — and until now
                # this read on the settings page as "gun not found",
                # which sent people to check a gun that was fine
                self.connected = False
                self.scan_error = str(e) or e.__class__.__name__
                self.scan_at = time.monotonic()
                if not getattr(self, '_scan_warned', False):
                    self._scan_warned = True
                    log.info(f'BLE scan unavailable ({e}) — Smart Coach '
                             'idle, watching for Bluetooth to come up')
                await asyncio.sleep(30)
                continue
            self._scan_warned = False
            # What the scan actually SAW. A pinned MAC that never matches
            # looks identical to a dead adapter without this, and the two
            # want opposite things done about them.
            self.scan_error = ''
            self.scan_at = time.monotonic()
            self.seen = len(devs or [])
            self.seen_names = sorted({(getattr(d, 'name', '') or '?')
                                      for d in (devs or [])})[:12]
            gun = next((d for d in devs if self._match(d, rad)), None)
            if gun is None:
                self.connected = False
                await asyncio.sleep(RESCAN_IDLE_S)
                continue
            self.device = (gun.address or '').upper()
            self.device_name = gun.name or None
            self.found_at = time.monotonic()
            log.info(f'Smart Coach found: {self.device_name or "?"} '
                     f'[{self.device}] — connecting')
            try:
                async with bleak.BleakClient(gun) as client:
                    self.connected = True
                    subs = []
                    for svc in client.services:
                        for ch in svc.characteristics:
                            props = set(ch.properties or [])
                            if not props & {'notify', 'indicate'}:
                                continue
                            uuid = str(ch.uuid)
                            if self.char and uuid != self.char:
                                continue

                            def _cb(sender, data, _u=uuid,
                                    _s=str(svc.uuid)):
                                try:
                                    self.handle_notify(_u, data,
                                                       service_uuid=_s)
                                except Exception:
                                    log.debug('smart coach notify failed',
                                              exc_info=True)
                            try:
                                await client.start_notify(ch, _cb)
                                subs.append(uuid)
                            except Exception as e:
                                log.debug(f'start_notify {uuid} failed: {e}')
                    self.connect_error = ''
                    if not subs:
                        log.warning('Smart Coach connected but offered no '
                                    'notifying characteristics — is the '
                                    'phone app holding it?')
                    else:
                        log.info(f'subscribed to {len(subs)} '
                                 f'characteristic(s); waiting for readings')
                    self.push(force_alive=True)
                    while self.running and client.is_connected:
                        cfg = self.cfg_load()
                        if self._want(cfg) is None:
                            log.info('Smart Coach capture switched off — '
                                     'disconnecting')
                            break
                        self.push()          # keepalive cadence
                        await asyncio.sleep(1.0)
            except Exception as e:
                # FOUND IT AND COULD NOT HOLD IT. Until this was
                # recorded, the settings page said "not found" about a
                # gun the box had located on every scan for an hour —
                # the same word for the opposite problem.
                self.connect_error = str(e) or e.__class__.__name__
                self.connect_fails += 1
                log.warning(f'Smart Coach connection dropped ({e}) — '
                            'rescanning')
            self.connected = False
            self.push(force_alive=True)      # the drop reaches the site now
            await asyncio.sleep(5)

    def loop(self):
        try:
            import bleak
        except ImportError:
            self.have_bleak = False
            log.info('bleak not installed — Smart Coach capture disabled. '
                     'sudo apt install -y python3-bleak (or sudo pip3 '
                     'install bleak --break-system-packages — Debian 12+ '
                     'refuses a bare pip install), then restart this '
                     'service')
            return
        self.have_bleak = True
        self.ensure_sender()                 # all HTTP off the BLE loop
        try:
            asyncio.run(self._run(bleak))
        except Exception:
            log.exception('Smart Coach loop died')

    def start_thread(self):
        t = threading.Thread(target=self.loop, daemon=True,
                             name='smartcoach')
        t.start()
        return t
