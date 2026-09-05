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
    MAC, characteristic and decode back to config
    (radar.smart_coach_mac/_char/_decode). Reboots reconnect straight
    to the proven gun.

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
# says only that.
NAME_RE = re.compile(r'pocket\s*radar|smart\s*coach|sr-?1100', re.I)

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


def decode_reading(data, want=None):
    """One notification payload → (mph, decode_name) or None.

    Tries the encodings a speed most plausibly travels as, in a FIXED
    order, and accepts the first that lands in the plausible band —
    determinism is what lets the learner demand the same decode three
    times before trusting it. `want` pins one decoder (the learned
    config) and tries nothing else, so a firmware update that changes
    the format goes loudly unparsed instead of silently misread.

    Names: ascii, ascii_x10, u8, u16le, u16le_x10, u16le_cmps
    (hundredths of m/s — the SI-flavored encoding BLE sensors love),
    u16be, u16be_x10, f32le.
    """
    if not data:
        return None
    tries = []

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
    for name, v in tries:
        if want is not None and name != want:
            continue
        if _band(v):
            return round(v, 1), name
    return None


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
        self.decode = None                  # proven decode name
        self.notifies = 0
        self.readings = 0
        self.unparsed = 0
        self.last_gun_t = None
        self.learned = False                # persisted identity this run
        self.have_bleak = None              # None until the loop checks
        # (uuid, decode) → consecutive in-band readings, for the learner
        self._streak = {}

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
    def handle_notify(self, char_uuid, data, t=None):
        """One GATT notification through the whole pipeline. Returns the
        event dict when the payload decoded, else None."""
        t = time.monotonic() if t is None else t
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
            'char': self.char,
            'decode': self.decode,
            'notifies': self.notifies,
            'readings': self.readings,
            'unparsed': self.unparsed,
            'heard_s': (round(time.monotonic() - self.last_gun_t, 1)
                        if self.last_gun_t is not None else None),
            'learned': bool(self.learned),
            'bleak': self.have_bleak,
        }

    # ── BLE loop ─────────────────────────────────────────────────────────────
    def _want(self, cfg):
        rad = (cfg or {}).get('radar') or {}
        if (rad.get('smart_coach') or 'auto') == 'off':
            return None
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
            try:
                devs = await bleak.BleakScanner.discover(timeout=SCAN_S)
            except Exception as e:
                # no adapter / bluetoothd down — idle quietly, keep trying
                self.connected = False
                if not getattr(self, '_scan_warned', False):
                    self._scan_warned = True
                    log.info(f'BLE scan unavailable ({e}) — Smart Coach '
                             'idle, watching for Bluetooth to come up')
                await asyncio.sleep(30)
                continue
            self._scan_warned = False
            gun = next((d for d in devs if self._match(d, rad)), None)
            if gun is None:
                self.connected = False
                await asyncio.sleep(RESCAN_IDLE_S)
                continue
            self.device = (gun.address or '').upper()
            self.device_name = gun.name or None
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

                            def _cb(sender, data, _u=uuid):
                                try:
                                    self.handle_notify(_u, data)
                                except Exception:
                                    log.debug('smart coach notify failed',
                                              exc_info=True)
                            try:
                                await client.start_notify(ch, _cb)
                                subs.append(uuid)
                            except Exception as e:
                                log.debug(f'start_notify {uuid} failed: {e}')
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
            log.info('bleak not installed — Smart Coach capture disabled '
                     '(apt install python3-bleak, or pip3 install bleak)')
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
