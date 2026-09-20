"""The BLE serial bridge: an HM-10-family adapter on the gun, read over
Bluetooth LE and presented to the radar service as a tty.

Built against a real one — an IRXON brick advertising as VELOBEAM_003
with service 0xFFE0 and AdvertisingFlags 06 (BLE only, no classic), on
a Stalker Pro IIs. Nothing about it can become /dev/rfcomm0, and
nothing about it pairs; it hands its bytes to whoever subscribes.
"""
import asyncio
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from encoder import ble_serial, radar  # noqa: E402

FRAME = '\x88RD   34A 586         5A 589         9A15650        \r'


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """The bridge publishes its tty under a fixed path; point that at
    the sandbox for both the bridge and the radar module's view of it."""
    d = str(tmp_path / 'run')
    monkeypatch.setattr(ble_serial, 'RUN_DIR', d)
    monkeypatch.setattr(ble_serial, 'LINK', os.path.join(d, 'radar-ble'))
    monkeypatch.setattr(radar, 'BLE_LINK', os.path.join(d, 'radar-ble'))
    return d


def _read_slave(path, n, tries=50):
    """Read up to n bytes off the slave side, non-blocking, patiently."""
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        out = b''
        for _ in range(tries):
            try:
                out += os.read(fd, n)
            except BlockingIOError:
                pass
            if len(out) >= n:
                break
        return out
    finally:
        os.close(fd)


# ── recognising the adapter ──────────────────────────────────────────────────

def test_the_hm10_family_service_is_recognised_as_a_serial_bridge():
    assert ble_serial.looks_like_uart(['0000ffe0-0000-1000-8000-00805f9b34fb'])
    assert ble_serial.looks_like_uart(['0000FFE0-0000-1000-8000-00805F9B34FB'])
    assert ble_serial.looks_like_uart(['6e400001-b5a3-f393-e0a9-e50e24dcca9e'])
    # the Tencent UUID these bricks also carry is not the serial service
    assert not ble_serial.looks_like_uart(['0000fee7-0000-1000-8000-00805f9b34fb'])
    assert not ble_serial.looks_like_uart([]) and not ble_serial.looks_like_uart(None)


# ── the tty ──────────────────────────────────────────────────────────────────

def test_the_bridge_publishes_a_tty_the_radar_scan_picks_up(run_dir):
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    slave = b.ensure_pty()
    assert os.path.exists(slave)
    link = os.path.join(run_dir, 'radar-ble')
    assert os.path.islink(link) and os.path.realpath(link) == slave
    # radar.py's port scan sees it beside rfcomm, and knows it is a
    # Bluetooth lead — the same peer-of-the-pin rule as rfcomm applies
    assert link in radar.find_ports({})
    assert radar.RadarService._is_bluetooth(link)
    assert radar.RadarService._stable_path(link)
    # one pty for the life of the process
    assert b.ensure_pty() == slave


def test_bytes_off_the_air_come_out_of_the_tty_in_order(run_dir):
    """A fifty-character Stalker frame arrives as three notifications
    of twenty-odd bytes. The tty carries them through untouched, and
    the line buffer on the other side puts the frame back together —
    exactly what happens to a cable that delivers a frame a byte at a
    time."""
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    slave = b.ensure_pty()
    raw = FRAME.encode('latin-1')
    pieces = [raw[i:i + 20] for i in range(0, len(raw), 20)]
    assert len(pieces) == 3
    for piece in pieces:
        b.feed(piece)
    got = _read_slave(slave, len(raw))
    assert got == raw
    assert b.notifies == 3 and b.bytes_in == len(raw)
    assert b.health()['heard_s'] is not None
    # …and it parses as the gun frame it is, spin included
    from tests.test_radar import _FakeLink
    svc = radar.RadarService(_FakeLink())
    svc.handle_line(got.decode('ascii', 'replace').rstrip('\r'))
    assert svc.frames_parsed == 1


def test_feeding_nothing_or_before_the_pty_exists_is_harmless(run_dir):
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    assert b.feed(b'') == 0 and b.feed(None) == 0
    assert b.feed(b'x') == 0            # no pty yet: dropped, no error
    assert b.notifies == 0


# ── the BLE loop, against a fake bleak ───────────────────────────────────────

class _Adv:
    def __init__(self, uuids):
        self.service_uuids = uuids


class _Dev:
    def __init__(self, address, name):
        self.address, self.name = address, name


class _Char:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, props


class _Svc:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


def _fake_bleak(devs, notify_payloads, fail_connect=None):
    """A bleak that finds `devs` ({mac: (dev, adv)}), whose client
    offers one FFE1 notify characteristic, and which fires
    `notify_payloads` at the callback once subscribed."""
    state = {'subscribed': [], 'connects': 0}

    class Scanner:
        @staticmethod
        async def discover(timeout=0, return_adv=False):
            return devs

    class Client:
        def __init__(self, dev):
            self.dev = dev
            self.is_connected = False
            self.services = [_Svc('0000ffe0-0000-1000-8000-00805f9b34fb', [
                _Char('0000ffe1-0000-1000-8000-00805f9b34fb',
                      ['read', 'write-without-response', 'notify'])])]

        async def __aenter__(self):
            state['connects'] += 1
            if fail_connect:
                raise fail_connect
            self.is_connected = True
            return self

        async def __aexit__(self, *a):
            self.is_connected = False

        async def start_notify(self, ch, cb):
            state['subscribed'].append(str(ch.uuid))
            for p in notify_payloads:
                cb(None, p)
            # then the adapter goes quiet and drops, as between innings
            self.is_connected = False

    class Bleak:
        BleakScanner = Scanner
        BleakClient = Client
    return Bleak, state


def _drive(bridge, bleak, ticks=6):
    """Run the loop for a bounded number of sleeps."""
    sleeps = {'n': 0}

    async def fake_sleep(s):
        sleeps['n'] += 1
        if sleeps['n'] >= ticks:
            bridge.running = False
    real = asyncio.sleep
    asyncio.sleep = fake_sleep
    try:
        asyncio.run(bridge._run(bleak))
    finally:
        asyncio.sleep = real


def test_a_found_adapter_is_subscribed_fed_through_and_learned(run_dir):
    """The whole path: scan finds the MAC with the serial service,
    connect, subscribe to what notifies, bytes reach the tty, and the
    kind is written to config so the rfcomm binder stands down."""
    cfg = {'radar': {'bluetooth_mac': '88:0a:98:19:08:16'}}
    saved = {}
    dev = _Dev('88:0A:98:19:08:16', 'VELOBEAM_003')
    bleak, state = _fake_bleak(
        {'88:0A:98:19:08:16': (dev, _Adv(
            ['0000fee7-0000-1000-8000-00805f9b34fb',
             '0000ffe0-0000-1000-8000-00805f9b34fb']))},
        [FRAME[:20].encode('latin-1'), FRAME[20:40].encode('latin-1'),
         FRAME[40:].encode('latin-1')])
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg,
                                   cfg_save=lambda c: saved.update(c))
    _drive(b, bleak)
    assert state['connects'] >= 1
    # the fake adapter drops after each delivery and the bridge comes
    # back every time, so the same characteristic is subscribed once
    # per connection — reconnecting IS the behaviour
    assert set(state['subscribed']) == {'0000ffe1-0000-1000-8000-00805f9b34fb'}
    assert len(state['subscribed']) == state['connects']
    assert b.device_name == 'VELOBEAM_003'
    assert b.bytes_in >= len(FRAME)
    assert _read_slave(b.slave_path, len(FRAME)) == FRAME.encode('latin-1')
    # learned: the binder reads this and stops trying rfcomm
    assert saved['radar']['bluetooth_kind'] == 'ble'
    assert b.health()['chars'] == ['0000ffe1-0000-1000-8000-00805f9b34fb']


def test_a_mac_that_never_appears_in_a_ble_scan_is_left_to_the_binder(run_dir):
    """A classic adapter (a BT578) is invisible to a BLE scan. The bridge
    must not claim it, not open a pty for it, and not write anything —
    that one is rfcomm's."""
    cfg = {'radar': {'bluetooth_mac': 'AA:BB:CC:DD:EE:FF'}}
    saved = {}
    bleak, state = _fake_bleak({}, [])
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg,
                                   cfg_save=lambda c: saved.update(c))
    _drive(b, bleak, ticks=3)
    assert state['connects'] == 0
    assert b.master is None and saved == {}
    assert not os.path.exists(os.path.join(run_dir, 'radar-ble'))


def test_kind_spp_switches_the_bridge_off_entirely(run_dir):
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16',
                     'bluetooth_kind': 'spp'}}
    dev = _Dev('88:0A:98:19:08:16', 'VELOBEAM_003')
    bleak, state = _fake_bleak({'88:0A:98:19:08:16': (dev, _Adv([]))}, [])
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg)
    _drive(b, bleak, ticks=3)
    assert state['connects'] == 0 and b.master is None


def test_a_dropped_link_is_reported_and_retried(run_dir):
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16'}}
    dev = _Dev('88:0A:98:19:08:16', 'VELOBEAM_003')
    bleak, state = _fake_bleak(
        {'88:0A:98:19:08:16': (dev, _Adv(
            ['0000ffe0-0000-1000-8000-00805f9b34fb']))},
        [], fail_connect=OSError('Device disconnected'))
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=lambda c: None)
    _drive(b, bleak, ticks=5)
    assert state['connects'] >= 2                  # it keeps trying
    assert b.connected is False
    assert 'Device disconnected' in b.health()['connect_error']


def test_without_bleak_the_bridge_says_so_and_stands_aside(run_dir, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_bleak(name, *a, **k):
        if name == 'bleak':
            raise ImportError('no bleak')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', no_bleak)
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    b.loop()
    assert b.have_bleak is False and b.health()['bleak'] is False


# ── the rfcomm binder stands down for BLE ────────────────────────────────────

def test_the_binder_stands_down_for_a_ble_adapter(tmp_path):
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'scripts', 'radar_bt_bind.sh')
    (tmp_path / 'config.json').write_text(
        '{"radar": {"bluetooth_mac": "88:0A:98:19:08:16", '
        '"bluetooth_kind": "ble"}}')
    r = subprocess.run(['bash', script], capture_output=True, text=True,
                       env={**os.environ, 'PLAYCALL_ENCODER_DIR': str(tmp_path)})
    assert r.returncode == 0
    assert 'BLE adapter' in r.stdout and 'no rfcomm' in r.stdout
    # and with no MAC at all, the same quiet exit as before
    (tmp_path / 'config.json').write_text('{"radar": {}}')
    r = subprocess.run(['bash', script], capture_output=True, text=True,
                       env={**os.environ, 'PLAYCALL_ENCODER_DIR': str(tmp_path)})
    assert r.returncode == 0 and 'nothing to bind' in r.stdout


# ── the radar service treats the lead as the Bluetooth peer it is ────────────

def test_the_gun_on_the_ble_lead_is_followed_without_touching_the_usb_pin(
        run_dir, monkeypatch):
    """Same three-lead rule as rfcomm: the USB cable pinned, the board
    pinned, and the BLE lead talking — the claim follows the gun and
    the pin stays the cable's."""
    import types
    from tests.test_radar import _FakePort, _FakeLink, FIELD_LIVE, FIELD_SPIN
    link = os.path.join(run_dir, 'radar-ble')
    usb_gun = '/dev/serial/by-id/usb-FTDI_GUN-if00-port0'
    usb_board = '/dev/serial/by-id/usb-FTDI_BOARD-if00-port0'
    burst = (FIELD_LIVE + '\r' + FIELD_LIVE + '\r' + FIELD_LIVE + '\r'
             + FIELD_SPIN + '\r').encode()
    _FakePort.SCRIPT = {usb_gun: [], usb_board: [], link: [burst]}
    _FakePort.OPEN = {}
    monkeypatch.setitem(sys.modules, 'serial',
                        types.SimpleNamespace(Serial=_FakePort))
    monkeypatch.setattr(radar, 'find_ports',
                        lambda c=None: [usb_gun, usb_board, link])
    real_exists = radar.os.path.exists
    monkeypatch.setattr(radar.os.path, 'exists',
                        lambda q: q in (usb_gun, usb_board, link)
                        or real_exists(q))
    cfg = {'radar': {'port': usb_gun, 'display_port': usb_board,
                     'bluetooth_mac': '88:0A:98:19:08:16'}}
    saved = {}
    svc = radar.RadarService(_FakeLink(), cfg_load=lambda: cfg,
                             cfg_save=lambda c: saved.update(c))
    sleeps = {'n': 0}

    def _sleep(s):
        sleeps['n'] += 1
        if sleeps['n'] > 3:
            svc.running = False
    monkeypatch.setattr(radar.time, 'sleep', _sleep)
    svc.loop()
    assert svc.port == link
    assert svc.health()['bluetooth'] is True
    assert svc.pin_overridden is False and saved == {}
    assert cfg['radar']['port'] == usb_gun
    assert _FakePort.OPEN[usb_board].written        # the board still fed


def test_the_tty_is_raw_from_birth(run_dir):
    """Cooked mode turned \\r into \\n, echoed bytes at the master, held
    them until a newline, and would read a 0x03 as Ctrl-C. A byte is
    either what the gun sent or it is worthless."""
    import termios
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    slave = b.ensure_pty()
    fd = os.open(slave, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        iflag, oflag, cflag, lflag, *_ = termios.tcgetattr(fd)
    finally:
        os.close(fd)
    assert not (iflag & termios.ICRNL)        # \\r stays \\r
    assert not (lflag & termios.ECHO)         # nothing bounced back
    assert not (lflag & termios.ICANON)       # bytes flow as they come
    assert not (lflag & termios.ISIG)         # 0x03 is data, not a signal
    # and the master cannot park the radio loop
    assert not os.get_blocking(b.master)
    # a control byte and a bare carriage return come through intact
    b.feed(b'\x03\r\x88')
    assert _read_slave(slave, 3) == b'\x03\r\x88'


# ── the link must survive whatever else lives in /run/playcall-encoder ────────
# 19 Sep 2026: the bridge reported connected with 101 700 bytes received
# and radar.py's scan listed the stale pin and /dev/rfcomm0 — never the
# link. It had been published once at connect and removed since (sibling
# units own that directory as their RuntimeDirectory). The bridge now
# republishes every second and the page says when the link is gone.

def test_a_removed_link_is_republished(run_dir):
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    slave = b.ensure_pty()
    link = os.path.join(run_dir, 'radar-ble')
    assert b.link_ok() and b.health()['link_ok']
    os.unlink(link)                       # what a RuntimeDirectory wipe does
    assert not b.link_ok() and not b.health()['link_ok']
    assert not os.path.exists(radar.BLE_LINK)
    assert b.publish_link() is True
    assert os.readlink(link) == slave and b.link_ok()
    assert radar.find_ports({}) == [radar.BLE_LINK]
    assert b.republished == 2
    # …and the whole directory going is the same story
    os.unlink(link)
    os.rmdir(run_dir)
    assert b.publish_link() is True and b.link_ok()


def test_the_link_is_republished_while_the_radio_is_up(run_dir):
    """The connected wait loop republishes every tick: a link removed
    while the adapter is up is back before the next radar rescan."""
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16'}}
    dev = _Dev('88:0A:98:19:08:16', 'VELOBEAM_003')
    bleak, state = _fake_bleak({'88:0A:98:19:08:16': (dev, _Adv(
        ['0000ffe0-0000-1000-8000-00805f9b34fb']))}, [b'x\r'])

    async def stay_up(self, ch, cb):        # the fake drops at once; don't
        state['subscribed'].append(str(ch.uuid))
        cb(None, b'x\r')
    bleak.BleakClient.start_notify = stay_up
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=lambda c: None)
    link = os.path.join(run_dir, 'radar-ble')
    real_publish = b.publish_link
    hits = {'n': 0}

    def wipe_then_publish():
        hits['n'] += 1
        if hits['n'] == 2:                   # 1 is ensure_pty's own publish
            os.unlink(link)                  # from under the connected loop
            assert not os.path.lexists(link)
        return real_publish()
    b.publish_link = wipe_then_publish
    _drive(b, bleak, ticks=4)
    assert state['connects'] == 1 and hits['n'] >= 3
    assert os.path.islink(link) and os.readlink(link) == b.slave_path
    assert b.republished == 2


def test_a_stale_rfcomm_binding_for_our_mac_is_released(monkeypatch, tmp_path):
    calls = []
    dev = tmp_path / 'rfcomm0'
    dev.write_text('')
    monkeypatch.setattr(ble_serial.os.path, 'exists',
                        lambda p: p == '/dev/rfcomm0' or os.path.lexists(p))

    class R:
        def __init__(self, out='', rc=0):
            self.stdout, self.stderr, self.returncode = out, '', rc

    def run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ['rfcomm', 'show']:
            return R('rfcomm0: 88:0A:98:19:08:16 channel 1 clean\n')
        return R()
    monkeypatch.setattr(ble_serial.subprocess, 'run', run)
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    b._release_rfcomm('88:0a:98:19:08:16')
    assert ['rfcomm', 'release', '/dev/rfcomm0'] in calls
    # once per run — not every reconnect
    b._release_rfcomm('88:0a:98:19:08:16')
    assert sum(1 for c in calls if c[1] == 'release') == 1


def test_an_rfcomm_binding_for_another_device_is_left_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(ble_serial.os.path, 'exists',
                        lambda p: p == '/dev/rfcomm0' or os.path.lexists(p))

    class R:
        stdout = 'rfcomm0: 00:11:22:33:44:55 channel 1 clean\n'
        stderr = ''
        returncode = 0
    monkeypatch.setattr(ble_serial.subprocess, 'run',
                        lambda argv, **kw: calls.append(argv) or R())
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    b._release_rfcomm('88:0A:98:19:08:16')
    assert calls == [['rfcomm', 'show', '/dev/rfcomm0']]


def test_no_rfcomm_node_means_nothing_to_release(monkeypatch):
    monkeypatch.setattr(ble_serial.subprocess, 'run',
                        lambda *a, **k: pytest.fail('rfcomm must not run'))
    monkeypatch.setattr(ble_serial.os.path, 'exists', lambda p: False)
    ble_serial.BleSerialBridge(cfg_load=lambda: {})._release_rfcomm('88:0A:98:19:08:16')


def test_the_scan_leaves_rfcomm_out_once_the_adapter_is_known_ble(run_dir, monkeypatch):
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {})
    b.ensure_pty()
    monkeypatch.setattr(radar.glob, 'glob',
                        lambda pat: ['/dev/rfcomm0'] if 'rfcomm' in pat else [])
    # auto / spp: the node is a candidate, as before
    assert radar.find_ports({}) == ['/dev/rfcomm0', radar.BLE_LINK]
    assert radar.find_ports({'radar': {'bluetooth_kind': 'spp'}}) \
        == ['/dev/rfcomm0', radar.BLE_LINK]
    # ble: the node is the binder's leftover — only the lead is listed
    assert radar.find_ports({'radar': {'bluetooth_kind': 'ble'}}) \
        == [radar.BLE_LINK]


# ── the learned kind must not be lost to the settings form ───────────────────
# 19 Sep 2026: learned 'ble' at 19:26, the settings page (rendered before
# that) was saved at 19:29 with its select still on Auto, and the box
# spent the evening paging the adapter over classic Bluetooth again.

def _stay_up_bleak(devs, payload=b'x\r'):
    bleak, state = _fake_bleak(devs, [payload])

    async def stay_up(self, ch, cb):
        state['subscribed'].append(str(ch.uuid))
        cb(None, payload)
    bleak.BleakClient.start_notify = stay_up
    return bleak, state


def test_a_kind_saved_back_to_auto_is_rewritten_while_connected(run_dir, monkeypatch):
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16'}}
    writes = []

    def save(c):
        writes.append(c['radar'].get('bluetooth_kind'))
        cfg.update(c)
    dev = _Dev('88:0A:98:19:08:16', 'VELOBEAM_003')
    bleak, state = _stay_up_bleak({'88:0A:98:19:08:16': (dev, _Adv(
        ['0000ffe0-0000-1000-8000-00805f9b34fb']))})
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=save)
    # no backoff in the test: every call may write
    monkeypatch.setattr(ble_serial.time, 'monotonic',
                        lambda c=[0.0]: c.__setitem__(0, c[0] + 60) or c[0])
    ticks = {'n': 0}
    real_publish = b.publish_link

    def form_saves_auto_then_publish():
        ticks['n'] += 1
        if ticks['n'] == 3:                 # the page's save lands
            cfg['radar']['bluetooth_kind'] = 'auto'
        return real_publish()
    b.publish_link = form_saves_auto_then_publish
    _drive(b, bleak, ticks=6)
    assert state['connects'] == 1
    assert writes == ['ble', 'ble']         # learned, then rewritten
    assert cfg['radar']['bluetooth_kind'] == 'ble'


def test_a_failed_write_is_not_retried_at_frame_rate(monkeypatch):
    calls = []

    def save(c):
        calls.append(1)
        raise OSError('read-only /etc')
    b = ble_serial.BleSerialBridge(cfg_load=lambda: {'radar': {}}, cfg_save=save)
    t = {'now': 100.0}
    monkeypatch.setattr(ble_serial.time, 'monotonic', lambda: t['now'])
    b._persist_kind()
    b._persist_kind()
    t['now'] += 10
    b._persist_kind()
    assert len(calls) == 1
    t['now'] += 25
    b._persist_kind()
    assert len(calls) == 2


# ── a known-BLE adapter gets its tty before the radio is found ───────────────
# A restart leaves the previous process's link pointing at a pty that died
# with it; radar.py skips a dangling link silently. With the kind known,
# the tty is published at once and the loop holds it open and quiet.

def test_a_known_ble_adapter_publishes_its_tty_before_any_connection(run_dir):
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16',
                     'bluetooth_kind': 'ble'}}
    link = os.path.join(run_dir, 'radar-ble')
    os.makedirs(run_dir, exist_ok=True)
    os.symlink('/dev/pts/999', link)             # the dead process's link
    assert not os.path.exists(link) and radar.find_ports({}) == []
    bleak, state = _fake_bleak({}, [])           # radio not found (yet)
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=lambda c: None)
    _drive(b, bleak, ticks=3)
    assert state['connects'] == 0
    assert b.master is not None and os.readlink(link) == b.slave_path
    assert os.path.exists(link) and b.link_ok()
    assert radar.find_ports({}) == [radar.BLE_LINK]


def test_an_unlearned_adapter_still_gets_no_tty_until_it_is_found(run_dir):
    """kind=auto and never seen in a BLE scan: a classic adapter — the
    binder's, no phantom tty (unchanged)."""
    cfg = {'radar': {'bluetooth_mac': 'AA:BB:CC:DD:EE:FF'}}
    bleak, state = _fake_bleak({}, [])
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=lambda c: None)
    _drive(b, bleak, ticks=3)
    assert b.master is None
    assert not os.path.lexists(os.path.join(run_dir, 'radar-ble'))


def test_clearing_the_mac_takes_the_tty_down(run_dir):
    cfg = {'radar': {'bluetooth_mac': '88:0A:98:19:08:16',
                     'bluetooth_kind': 'ble'}}
    link = os.path.join(run_dir, 'radar-ble')
    bleak, state = _fake_bleak({}, [])
    b = ble_serial.BleSerialBridge(cfg_load=lambda: cfg, cfg_save=lambda c: None)
    _drive(b, bleak, ticks=2)
    assert os.path.lexists(link)
    cfg['radar']['bluetooth_mac'] = ''           # blank for cabled
    b.running = True
    _drive(b, bleak, ticks=2)
    assert not os.path.lexists(link)
    # …and someone else's link is never ours to remove
    os.symlink('/dev/pts/998', link)
    b.unpublish_link()
    assert os.path.lexists(link)
