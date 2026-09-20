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
import re
import subprocess
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
        self._persist_next_t = 0.0
        self.republished = 0                    # publish_link() writes
        self.rfcomm_released = False
        self._direct_logged = False
        self._misses = 0
        self.scan_note = ''
        self.last_scan_t = None
        self._last_note = None

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
        self.publish_link()
        return self.slave_path

    def publish_link(self):
        """Point LINK at the slave — and keep it pointed there. Called
        once from ensure_pty and again every second while the radio is
        up, because the link is a file in /run/playcall-encoder and
        that directory is not ours alone: sibling units declare it as
        their RuntimeDirectory, and a unit whose file lacks
        RuntimeDirectoryPreserve wipes the directory every time it
        stops (19 Sep 2026: the bridge said 'connected, 101 700 bytes
        received → radar-ble' while radar.py's scan never listed the
        link at all — it had been published once and deleted since).
        Republishing is a lstat per second; a missing link is the whole
        BLE lead gone, so it is cheap at the price. Returns True when
        the link resolves to our slave."""
        if self.slave_path is None:
            return False
        try:
            if os.path.islink(LINK) and os.readlink(LINK) == self.slave_path \
                    and os.path.exists(LINK):
                return True
        except OSError:
            pass
        try:
            os.makedirs(RUN_DIR, exist_ok=True)
            tmp = LINK + '.tmp'
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(self.slave_path, tmp)
            os.replace(tmp, LINK)              # atomic: never a dangling link
            self.republished += 1
            log.info(f'BLE serial lead is {LINK} -> {self.slave_path}'
                     + (' (republished — something removed it)'
                        if self.republished > 1 else ''))
            return True
        except OSError as e:
            log.warning(f'could not publish {LINK} ({e}) — radar.py will '
                        f'not see the BLE lead; slave is {self.slave_path}')
            return False

    def link_ok(self):
        """Does LINK currently resolve to our slave? What the settings
        page shows next to the byte count — 'connected' means the radio;
        this means radar.py can find the tty."""
        try:
            return bool(self.slave_path) and os.path.islink(LINK) \
                and os.readlink(LINK) == self.slave_path \
                and os.path.exists(LINK)
        except OSError:
            return False

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
        on every boot, and radar.py leaves /dev/rfcomm* out of its scan.

        Not once per run: the settings page's select is rendered from
        config and saved back whole, so a page opened before the bridge
        learned and saved after it put 'auto' straight back (19 Sep
        2026: learned 19:26, saved over 19:29, and the box spent the
        evening paging the adapter over classic Bluetooth again). The
        connected loop calls this whenever config says anything but
        'ble'; a write that fails is retried no sooner than 30 s."""
        now = time.monotonic()
        if now < self._persist_next_t:
            return
        self._persist_next_t = now + 30
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
                self.learned_kind = True
                return
            was = rad.get('bluetooth_kind') or 'auto'
            rad['bluetooth_kind'] = 'ble'
            cfg['radar'] = rad
            save(cfg)
            if self.learned_kind:
                log.warning("radar.bluetooth_kind had gone back to "
                            f"'{was}' — a save "
                            "on the settings page? — rewrote 'ble': the "
                            "adapter is connected over BLE right now")
            else:
                log.warning('learned that the gun adapter is BLE and wrote '
                            'radar.bluetooth_kind=ble — the rfcomm binder '
                            'stands down from now on')
            self.learned_kind = True
        except Exception as e:
            log.warning(f'could not persist bluetooth_kind ({e})')

    def _note(self, text):
        """One line in the journal per CHANGE of what the bridge sees —
        never per pass, never silence. The night of 19 Sep 2026 the
        bridge had four ways to wait without a word (scan failed, scan
        hung, MAC not seen, bluetoothctl timed out) and took one of
        them for an hour while the log showed nothing but the radar
        loop's USB warnings. The same text is also what the settings
        page shows under the byte count."""
        self.scan_note = text
        self.last_scan_t = time.monotonic()
        if text != self._last_note:
            self._last_note = text
            log.info(f'BLE serial: {text}')

    async def _scan(self, bleak):
        """One LE scan, bounded: bleak's discover() has been seen to
        neither return nor raise, and a bridge parked in it for ever
        looks exactly like one that found nothing. Returns the devices,
        or None after saying why."""
        try:
            try:
                return await asyncio.wait_for(
                    bleak.BleakScanner.discover(timeout=SCAN_S,
                                                return_adv=True),
                    SCAN_S + 15)
            except TypeError:
                # older bleak: no return_adv — fall back to devices only
                found = await asyncio.wait_for(
                    bleak.BleakScanner.discover(timeout=SCAN_S), SCAN_S + 15)
                return {d.address: (d, None) for d in found}
        except asyncio.TimeoutError:
            self.scan_error = (f'the Bluetooth scan hung for {SCAN_S + 15:.0f}s '
                               '(bluetoothd stuck? — sudo systemctl restart '
                               'bluetooth)')
        except Exception as e:
            self.scan_error = f'{e.__class__.__name__}: {e}' if str(e) \
                else e.__class__.__name__
        self._note(f'scan failed: {self.scan_error}')
        return None

    @staticmethod
    def _bluez_info(mac):
        """What BlueZ knows about this address, from `bluetoothctl info`:
        whether it knows it at all, holds it connected right now, and
        has seen it as an LE device (advertising flags, or a serial
        service UUID — a classic adapter paired for rfcomm shows
        neither). Best effort; all False on any doubt."""
        out = ''
        try:
            r = subprocess.run(['bluetoothctl', 'info', mac],
                               capture_output=True, text=True, timeout=5)
            out = r.stdout or ''
        except Exception:
            pass
        low = out.lower()
        known = 'device ' in low and 'not available' not in low
        le = known and ('advertisingflags' in low
                        or looks_like_uart(re.findall(
                            r'uuid:\s*.*?\(([0-9a-f-]{36})\)', low)))
        return {'known': known, 'connected': 'connected: yes' in low,
                'le': le}

    def clear_stale_link(self):
        """A link left by a previous run points at a pty that died with
        it — or worse, at a pty number the kernel has since handed to
        someone else. 19 Sep 2026, 20:10:06: 'radar listening on
        /run/playcall-encoder/radar-ble' — which was /dev/pts/1, which
        had just become the operator's SSH terminal. Before this bridge
        has a tty of its own, any link there is nobody's: remove it."""
        if self.slave_path is not None:
            return
        try:
            if os.path.islink(LINK):
                was = os.readlink(LINK)
                os.unlink(LINK)
                log.info(f'removed a stale BLE serial lead {LINK} -> {was} '
                         'left by a previous run')
        except OSError:
            pass

    def unpublish_link(self):
        """Take LINK down when it is ours and the MAC is gone from
        config — otherwise radar.py keeps a phantom adapter open."""
        try:
            if self.slave_path and os.path.islink(LINK) \
                    and os.readlink(LINK) == self.slave_path:
                os.unlink(LINK)
                log.info(f'BLE serial lead {LINK} taken down — no adapter '
                         'configured')
        except OSError:
            pass

    def _release_rfcomm(self, mac):
        """A binding the rfcomm binder made for this MAC before anyone
        knew it was BLE is a trap: /dev/rfcomm0 exists, so radar.py
        opens it, the open blocks while the kernel tries an RFCOMM
        connect that a BLE-only adapter can never answer, the read then
        fails EIO, and the whole radar loop spends its life reopening a
        dead node (19 Sep 2026, every 8 s, all evening). The binder
        stands down on the NEXT boot once bluetooth_kind=ble is written;
        this clears the binding it already made on THIS one. Only a
        binding that names our MAC is touched. Best effort, once."""
        if self.rfcomm_released:
            return
        self.rfcomm_released = True
        dev = '/dev/rfcomm0'
        if not os.path.exists(dev):
            return
        try:
            show = subprocess.run(['rfcomm', 'show', dev], capture_output=True,
                                  text=True, timeout=5)
            if mac.upper() not in (show.stdout or '').upper():
                return
            r = subprocess.run(['rfcomm', 'release', dev], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0:
                log.warning(f'released the stale rfcomm binding on {dev} — '
                            f'{mac} is BLE, radar.py reads it from {LINK}')
            else:
                log.warning(f'could not release {dev} ({(r.stderr or "").strip()})')
        except Exception as e:
            log.debug(f'rfcomm release skipped: {e}')

    # ── the BLE loop ─────────────────────────────────────────────────────────
    async def _run(self, bleak):
        self.clear_stale_link()
        while self.running:
            cfg = self.cfg_load()
            mac, kind = self._want(cfg)
            if not mac:
                self.connected = False
                self.unpublish_link()
                await asyncio.sleep(10)
                continue
            self.mac = mac
            if kind == 'ble':
                # Known BLE (learned on an earlier boot, or set): publish
                # the tty NOW, before the radio is even found. A restart
                # leaves the old process's link pointing at a pty that
                # died with it, and radar.py's scan skips a dangling
                # link without a word — the lead came back only when the
                # radio reconnected, which the stale rfcomm binding was
                # blocking (19 Sep 2026, 19:26 → 20:10). With the tty
                # up from the start the loop holds it open and silent,
                # like a gun between innings, and bytes land the moment
                # the radio connects.
                self.ensure_pty()
                self.publish_link()
            devs = await self._scan(bleak)
            if devs is None:                    # failed or hung: said below
                self.connected = False
                await asyncio.sleep(30)
                continue
            self.scan_error = ''
            hit = None
            seen = 0
            for addr, pair in (devs.items() if isinstance(devs, dict)
                               else ((d.address, (d, None)) for d in devs)):
                seen += 1
                if (addr or '').upper() == mac:
                    hit = pair
                    break
            if hit is None:
                # A BLE adapter that is not advertising is usually one
                # BlueZ still holds connected from a previous run of this
                # process — the LED solid blue, nobody reading it, and a
                # scan that can never see it (19 Sep 2026: the bridge
                # waited on that scan for an hour). BlueZ can connect to
                # an address it knows without an advertisement, so a
                # known-BLE adapter, or one BlueZ says is connected, is
                # connected by address at once — and so is one BlueZ has
                # seen as an LE device, since a scan can miss a slow
                # advertiser. Only an address BlueZ has never seen as LE
                # is left alone: that is a classic adapter, the binder's.
                self._misses += 1
                bz = self._bluez_info(mac)
                self._note(f'scan saw {seen} device(s); {mac} not among '
                           'them — BlueZ '
                           + ('holds it connected' if bz['connected'] else
                              'knows it as an LE device' if bz['le'] else
                              'knows it (not as LE)' if bz['known'] else
                              'has never seen it'))
                if kind == 'ble' or self.learned_kind or bz['connected'] \
                        or bz['le']:
                    log.info(f'{mac} not advertising — connecting by '
                             f'address (pass {self._misses})')
                    hit = (mac, None)
                else:
                    self.connected = False
                    await asyncio.sleep(RESCAN_IDLE_S)
                    continue
            else:
                self._misses = 0
                self._note(f'scan saw {seen} device(s); {mac} among them')
            dev, adv = hit
            uuids = getattr(adv, 'service_uuids', None) if adv else None
            if kind == 'auto' and uuids is not None \
                    and not looks_like_uart(uuids):
                log.info(f'{mac} is BLE but advertises no serial service '
                         f'({sorted(uuids)}) — subscribing to whatever '
                         'notifies')
            self.device_name = (getattr(dev, 'name', None) or None
                                if not isinstance(dev, str) else self.device_name)
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
                    self._release_rfcomm(mac)
                    while self.running and client.is_connected:
                        m, k = self._want(self.cfg_load())
                        if m != mac:
                            break               # setting changed
                        if k != 'ble':
                            self._persist_kind()  # the form put 'auto' back
                        self.publish_link()     # see publish_link()
                        await asyncio.sleep(1.0)
            except Exception as e:
                self.connect_error = str(e) or e.__class__.__name__
                log.warning(f'BLE serial lead dropped ({self.connect_error}) '
                            '— reconnecting')
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
            'link_ok': self.link_ok(),
            'scan_note': self.scan_note,
            'scan_age_s': (round(time.monotonic() - self.last_scan_t, 1)
                           if self.last_scan_t is not None else None),
            'chars': list(self.chars),
            'notifies': self.notifies,
            'bytes': self.bytes_in,
            'heard_s': (round(time.monotonic() - self.last_rx_t, 1)
                        if self.last_rx_t is not None else None),
            'bleak': self.have_bleak,
            'scan_error': self.scan_error,
            'connect_error': self.connect_error,
        }
