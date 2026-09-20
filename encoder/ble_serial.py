#!/usr/bin/env python3
"""A BLE serial bridge, presented to the radar service as a tty.

Some RS-232↔Bluetooth bricks are not classic Bluetooth at all. They
advertise a BLE "transparent UART" service — 0xFFE0 with its 0xFFE1
characteristic on the HM-10 family, Nordic's UART service on others —
and hand the serial bytes to whoever subscribes. There is no SPP, so
`rfcomm bind` can never turn them into /dev/rfcomm0, and there is no
pairing either; `bluetoothctl pair` simply does nothing.

This module connects to one, subscribes to everything that notifies,
and writes the bytes into the master side of a PSEUDO-TERMINAL. The
slave side is an ordinary tty, published at a stable path
(`LINK`, under /run) that radar.py's port scan picks up beside
/dev/rfcomm* — so the gun on the far end is claimed, parsed, forwarded
to the LED board and reported exactly like a gun on a cable, and
nothing in radar.py needed to learn what BLE is. A cable some games,
Bluetooth others, the same three-lead rule throughout.

It is decided by the same setting as the classic path: radar.bluetooth_mac.
Which KIND of adapter that MAC is — classic (rfcomm) or BLE (this) — is
radar.bluetooth_kind: 'spp', 'ble', or 'auto', which is the default and
means "the box finds out": this bridge looks for the MAC in a BLE scan
and, when it connects and subscribes, writes 'ble' to config so the
rfcomm binder stops trying on every boot. A MAC that never appears in a
BLE scan is left to the binder.

Notifications arrive in pieces of twenty-odd bytes, so a fifty-character
Stalker frame lands in three of them; the tty and radar.py's line buffer
put it back together, which is what they do for a cable that delivers a
frame a byte at a time.

Display-only, like everything on this path: nothing here can write to
the scorebook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import tty

log = logging.getLogger('bleserial')

RUN_DIR = os.environ.get('PLAYCALL_ENCODER_RUN', '/run/playcall-encoder')
LINK = os.path.join(RUN_DIR, 'radar-ble')      # what radar.py opens

# Services these bricks use for their serial stream. Subscribing to
# every notifying characteristic on the device would also work — a
# bridge has nothing else to say — but naming them keeps the log honest
# about what was found.
UART_SERVICES = (
    '0000ffe0-0000-1000-8000-00805f9b34fb',    # HM-10 / CC254x family
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e',    # Nordic UART
    '0000fff0-0000-1000-8000-00805f9b34fb',    # common vendor variant
)

SCAN_S = 8.0
RESCAN_IDLE_S = 15
RECONNECT_S = 3


def looks_like_uart(uuids):
    """Does an advertisement carry one of the serial services?"""
    got = {str(u).lower() for u in (uuids or [])}
    return any(u in got for u in UART_SERVICES)


class BleSerialBridge:
    """BLE notify → pty master → (slave tty) → radar.py.

    Runs as a daemon thread and idles harmlessly when there is no MAC,
    the kind is 'spp', bleak is missing, or Bluetooth is down."""

    def __init__(self, cfg_load=None, cfg_save=None):
        self.cfg_load = cfg_load or (lambda: {})
        self.cfg_save = cfg_save                # None → encoder.config
        self.running = True
        self.master = None                      # pty master fd
        self.slave_path = None
        self.connected = False
        self.device_name = None
        self.mac = None
        self.chars = []                         # subscribed uuids
        self.bytes_in = 0
        self.notifies = 0
        self.last_rx_t = None
        self.have_bleak = None
        self.scan_error = ''
        self.connect_error = ''
        self.learned_kind = False

    # ── the tty ──────────────────────────────────────────────────────────────
    def ensure_pty(self):
        """One pty for the life of the process. The master stays open
        whether or not the radio is connected — a slave whose master is
        gone reads EIO, which radar.py would report as a dead adapter,
        and an adapter that is merely between innings is not dead."""
        if self.master is not None:
            return self.slave_path
        master, slave = os.openpty()
        # RAW, or the line discipline "helps": it turns the frame's \r
        # into \n, echoes every byte back at the master, holds bytes
        # until a newline, and reads a stray 0x03 in a Stalker frame as
        # Ctrl-C. pyserial makes the slave raw again when it opens it,
        # but the terminal must be raw from birth — a byte is either
        # what the gun sent or it is worthless.
        tty.setraw(slave)
        # …and the master must never block the radio loop: a tty nobody
        # is reading fills a small kernel buffer, after which a blocking
        # write would park the BLE thread for ever.
        os.set_blocking(master, False)
        self.master = master
        self.slave_path = os.ttyname(slave)
        os.close(slave)     # radar.py opens the slave by path itself
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            tmp = LINK + '.tmp'
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(self.slave_path, tmp)
            os.replace(tmp, LINK)              # atomic: never a dangling link
            log.info(f'BLE serial lead is {LINK} -> {self.slave_path}')
        except OSError as e:
            log.warning(f'could not publish {LINK} ({e}) — radar.py will '
                        f'not see the BLE lead; slave is {self.slave_path}')
        return self.slave_path

    def feed(self, data):
        """Bytes off the air → the tty. Never blocks the BLE loop: a tty
        nobody is reading fills a small kernel buffer and then drops,
        which is the right behaviour when the radar service is down."""
        data = bytes(data or b'')
        if not data or self.master is None:
            return 0
        self.notifies += 1
        self.bytes_in += len(data)
        self.last_rx_t = time.monotonic()
        try:
            return os.write(self.master, data)
        except BlockingIOError:
            return 0
        except OSError as e:
            log.debug(f'pty write failed: {e}')
            return 0

    # ── config ───────────────────────────────────────────────────────────────
    def _want(self, cfg):
        rad = (cfg or {}).get('radar') or {}
        mac = (rad.get('bluetooth_mac') or '').strip().upper()
        kind = (rad.get('bluetooth_kind') or 'auto').lower()
        if not mac or kind == 'spp':
            return None, kind
        return mac, kind

    def _persist_kind(self):
        """Write bluetooth_kind='ble' once this bridge has proven it —
        the rfcomm binder reads it and stands down instead of failing
        on every boot. Best effort, once."""
        if self.learned_kind:
            return
        self.learned_kind = True
        try:
            if self.cfg_save is not None:
                save = self.cfg_save
                cfg = dict(self.cfg_load() or {})
            else:
                from . import config as _config
                cfg = _config.load()
                save = _config.save
            rad = dict(cfg.get('radar') or {})
            if rad.get('bluetooth_kind') == 'ble':
                return
            rad['bluetooth_kind'] = 'ble'
            cfg['radar'] = rad
            save(cfg)
            log.warning('learned that the gun adapter is BLE and wrote '
                        'radar.bluetooth_kind=ble — the rfcomm binder '
                        'stands down from now on')
        except Exception as e:
            log.warning(f'could not persist bluetooth_kind ({e})')

    # ── the BLE loop ─────────────────────────────────────────────────────────
    async def _run(self, bleak):
        while self.running:
            cfg = self.cfg_load()
            mac, kind = self._want(cfg)
            if not mac:
                self.connected = False
                await asyncio.sleep(10)
                continue
            self.mac = mac
            try:
                devs = await bleak.BleakScanner.discover(
                    timeout=SCAN_S, return_adv=True)
            except TypeError:
                # older bleak: no return_adv — fall back to devices only
                try:
                    found = await bleak.BleakScanner.discover(timeout=SCAN_S)
                    devs = {d.address: (d, None) for d in found}
                except Exception as e:
                    self.scan_error = str(e)
                    self.connected = False
                    await asyncio.sleep(30)
                    continue
            except Exception as e:
                self.scan_error = str(e)
                self.connected = False
                await asyncio.sleep(30)
                continue
            self.scan_error = ''
            hit = None
            for addr, pair in (devs.items() if isinstance(devs, dict)
                               else ((d.address, (d, None)) for d in devs)):
                if (addr or '').upper() == mac:
                    hit = pair
                    break
            if hit is None:
                # not a BLE device (or off): the rfcomm binder's problem
                self.connected = False
                await asyncio.sleep(RESCAN_IDLE_S)
                continue
            dev, adv = hit
            uuids = getattr(adv, 'service_uuids', None) if adv else None
            if kind == 'auto' and uuids is not None \
                    and not looks_like_uart(uuids):
                log.info(f'{mac} is BLE but advertises no serial service '
                         f'({sorted(uuids)}) — subscribing to whatever '
                         'notifies')
            self.device_name = getattr(dev, 'name', None) or None
            self.ensure_pty()
            log.info(f'BLE serial adapter found: {self.device_name or "?"} '
                     f'[{mac}] — connecting')
            try:
                async with bleak.BleakClient(dev) as client:
                    subs = []
                    for svc in client.services:
                        for ch in svc.characteristics:
                            props = set(ch.properties or [])
                            if not props & {'notify', 'indicate'}:
                                continue

                            def _cb(_sender, data):
                                self.feed(data)
                            try:
                                await client.start_notify(ch, _cb)
                                subs.append(str(ch.uuid))
                            except Exception as e:
                                log.debug(f'start_notify {ch.uuid}: {e}')
                    self.chars = subs
                    if not subs:
                        raise RuntimeError('the adapter offers nothing that '
                                           'notifies')
                    self.connected = True
                    self.connect_error = ''
                    log.info(f'BLE serial lead up: {len(subs)} '
                             f'characteristic(s) → {LINK}')
                    self._persist_kind()
                    while self.running and client.is_connected:
                        if self._want(self.cfg_load())[0] != mac:
                            break               # setting changed
                        await asyncio.sleep(1.0)
            except Exception as e:
                self.connect_error = str(e) or e.__class__.__name__
                log.warning(f'BLE serial lead dropped ({e}) — '
                            'reconnecting')
            self.connected = False
            await asyncio.sleep(RECONNECT_S)

    def loop(self):
        try:
            import bleak
        except ImportError:
            self.have_bleak = False
            log.info('bleak not installed — a BLE serial adapter cannot be '
                     'read (sudo apt install -y python3-bleak)')
            return
        self.have_bleak = True
        try:
            asyncio.run(self._run(bleak))
        except Exception:
            log.exception('BLE serial bridge died')

    def start_thread(self):
        t = threading.Thread(target=self.loop, daemon=True, name='bleserial')
        t.start()
        return t

    def health(self):
        return {
            'mac': self.mac,
            'name': self.device_name,
            'connected': bool(self.connected),
            'link': LINK if self.master is not None else None,
            'chars': list(self.chars),
            'notifies': self.notifies,
            'bytes': self.bytes_in,
            'heard_s': (round(time.monotonic() - self.last_rx_t, 1)
                        if self.last_rx_t is not None else None),
            'bleak': self.have_bleak,
            'scan_error': self.scan_error,
            'connect_error': self.connect_error,
        }
