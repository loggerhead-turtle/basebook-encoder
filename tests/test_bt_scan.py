"""'Find my adapter': the scan behind the radar card's button.

Parsing is a pure function of bluetoothctl's text, so the fixtures
here are what the N150 actually printed on 19 Sep 2026; scan() is
driven with a fake subprocess.run; the two routes are exercised
through the Flask test client with the scan stubbed."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from encoder import bt_scan  # noqa: E402

SCAN_OUT = (
    "\x1b[0;92mDiscovery started\x1b[0m\n"
    "\x1b[0;93m[CHG]\x1b[0m Controller 00:1A:7D:DA:71:13 Discovering: yes\n"
    "\x1b[0;92m[NEW]\x1b[0m Device 88:0A:98:19:08:16 VELOBEAM_003\n"
    "\x1b[0;92m[NEW]\x1b[0m Device 5A:50:15:4F:EB:F0 5A-50-15-4F-EB-F0\n"
    "\x1b[0;92m[NEW]\x1b[0m Device 00:11:22:33:44:55 BT578\n"
    "\x1b[0;92m[NEW]\x1b[0m Device F8:2E:0C:A7:16:CB SC-236\n"
    "\x1b[0;93m[CHG]\x1b[0m Device 88:0A:98:19:08:16 RSSI: -61\n"
    "\x1b[0;93m[CHG]\x1b[0m Device 5A:50:15:4F:EB:F0 RSSI: -88\n"
    "\x1b[0;93m[CHG]\x1b[0m Device 7C:8D:40:3B:A1:80 Name: JBL Flip 6\n"
    "\x1b[0;92m[NEW]\x1b[0m Device 7C:8D:40:3B:A1:80 JBL Flip 6\n"
)

INFO = {
    '88:0A:98:19:08:16': (
        "Device 88:0A:98:19:08:16 (public)\n\tName: VELOBEAM_003\n"
        "\tAlias: VELOBEAM_003\n\tPaired: no\n\tConnected: no\n"
        "\tUUID: Unknown                   (0000ffe0-0000-1000-8000-00805f9b34fb)\n"
        "\tRSSI: -61\n\tAdvertisingFlags: 06\n"),
    '00:11:22:33:44:55': (
        "Device 00:11:22:33:44:55 (public)\n\tName: BT578\n\tPaired: yes\n"
        "\tConnected: no\n"
        "\tUUID: Serial Port                (00001101-0000-1000-8000-00805f9b34fb)\n"),
    'F8:2E:0C:A7:16:CB': (
        "Device F8:2E:0C:A7:16:CB (public)\n\tName: SC-236\n\tConnected: no\n"
        "\tUUID: Vendor specific           (0000fee7-0000-1000-8000-00805f9b34fb)\n"
        "\tAdvertisingFlags: 06\n"),
    '7C:8D:40:3B:A1:80': (
        "Device 7C:8D:40:3B:A1:80 (public)\n\tName: JBL Flip 6\n\tConnected: no\n"
        "\tUUID: Audio Sink                (0000110b-0000-1000-8000-00805f9b34fb)\n"),
    '5A:50:15:4F:EB:F0': "Device 5A:50:15:4F:EB:F0 (random)\n\tConnected: no\n",
}


class _R:
    def __init__(self, out='', rc=0):
        self.stdout, self.stderr, self.returncode = out, '', rc


def _fake_run(argv, **kw):
    if argv[:3] == ['bluetoothctl', '--timeout', '8'] or argv[-2:] == ['scan', 'on']:
        return _R(SCAN_OUT)
    if argv[:2] == ['bluetoothctl', 'info']:
        return _R(INFO.get(argv[2], f'Device {argv[2]} not available\n'), 1)
    return _R()


def test_the_scan_text_is_read_through_the_colour_codes():
    seen = bt_scan.parse_scan(SCAN_OUT)
    assert list(seen) == ['88:0A:98:19:08:16', '5A:50:15:4F:EB:F0',
                          '00:11:22:33:44:55', 'F8:2E:0C:A7:16:CB',
                          '7C:8D:40:3B:A1:80']
    assert seen['88:0A:98:19:08:16'] == {'name': 'VELOBEAM_003', 'rssi': -61}
    # an address-as-name is no name; a name arriving on a [CHG] is kept
    assert seen['5A:50:15:4F:EB:F0']['name'] == ''
    assert seen['7C:8D:40:3B:A1:80']['name'] == 'JBL Flip 6'


def test_info_tells_le_from_classic_and_finds_the_serial_service():
    v = bt_scan.parse_info(INFO['88:0A:98:19:08:16'])
    assert v['le'] and '0000ffe0-0000-1000-8000-00805f9b34fb' in v['uuids']
    c = bt_scan.parse_info(INFO['00:11:22:33:44:55'])
    assert not c['le'] and bt_scan.SPP_UUID in c['uuids']
    assert bt_scan.parse_info('Device X not available') == {
        'name': '', 'uuids': [], 'le': False, 'connected': False}


def test_the_list_puts_the_adapters_first_and_says_what_kind_they_are():
    res = bt_scan.scan(run=_fake_run)
    assert res['error'] == ''
    names = [c['name'] for c in res['found']]
    # both serial adapters first (IRXON by service AND name, BT578 by both),
    # then the named non-adapters; the nameless rotating address is gone
    assert names[:2] == ['VELOBEAM_003', 'BT578']
    assert 'JBL Flip 6' in names and 'SC-236' in names
    assert all(c['name'] for c in res['found'])
    irxon = res['found'][0]
    assert irxon == {'mac': '88:0A:98:19:08:16', 'name': 'VELOBEAM_003',
                     'rssi': -61, 'kind': 'ble', 'serial': True,
                     'likely': True, 'score': 5}
    bt578 = res['found'][1]
    assert bt578['kind'] == 'spp' and bt578['serial'] and bt578['likely']
    jbl = next(c for c in res['found'] if c['name'] == 'JBL Flip 6')
    assert jbl['kind'] == 'auto' and not jbl['likely']


def test_no_bluez_and_no_controller_are_said_not_raised():
    def missing(argv, **kw):
        raise FileNotFoundError('bluetoothctl')
    assert 'bluez' in bt_scan.scan(run=missing)['error']

    def down(argv, **kw):
        return _R('No default controller available\n', 1)
    assert 'no Bluetooth adapter' in bt_scan.scan(run=down)['error']


# ── the two routes ───────────────────────────────────────────────────────────

def _client(monkeypatch):
    from tests.test_encoder import _radar_client
    return _radar_client(monkeypatch)


def test_the_button_scans_and_the_page_lists_what_was_heard(monkeypatch):
    web, client = _client(monkeypatch)
    real = bt_scan.scan
    monkeypatch.setattr(bt_scan, 'scan', lambda **kw: real(run=_fake_run))
    html = client.get('/').get_data(as_text=True)
    assert 'Find my adapter' in html and 'action="/radar/use"' not in html
    r = client.post('/radar/find')
    assert r.status_code == 302 and r.headers['Location'].endswith('#found')
    html = client.get('/').get_data(as_text=True)
    assert 'VELOBEAM_003' in html and '88:0A:98:19:08:16' in html
    assert html.count('action="/radar/use"') == 4
    assert html.index('VELOBEAM_003') < html.index('JBL Flip 6')
    assert 'Bluetooth LE' in html and 'classic Bluetooth' in html


def test_a_failed_scan_is_shown_not_hidden(monkeypatch):
    web, client = _client(monkeypatch)
    monkeypatch.setattr(bt_scan, 'scan',
                        lambda **kw: {'found': [], 'error': 'bluetoothctl is not installed (sudo apt install -y bluez)'})
    client.post('/radar/find')
    html = client.get('/').get_data(as_text=True)
    assert 'could not scan' in html and 'bluez' in html


def test_one_tap_writes_the_adapter_and_restarts_the_radar(monkeypatch):
    from encoder import config
    web, client = _client(monkeypatch)
    calls = []
    monkeypatch.setattr(web.system, 'systemctl', lambda *a: calls.append(a))
    restarted = []
    monkeypatch.setattr(web.threading, 'Thread',
                        lambda *a, **k: type('T', (), {'start': lambda self: restarted.append(1)})())
    r = client.post('/radar/use', data={'mac': '88:0a:98:19:08:16',
                                        'kind': 'ble', 'name': 'VELOBEAM_003'})
    assert r.status_code == 302 and 'Using+VELOBEAM_003' in r.headers['Location']
    rd = config.load()['radar']
    assert rd['bluetooth_mac'] == '88:0A:98:19:08:16'
    assert rd['bluetooth_kind'] == 'ble'
    # a new address: the binder is re-run so a stale rfcomm binding goes
    assert ('restart', 'playcall-encoder-radarbt') in calls
    # a classic adapter, same tap
    r = client.post('/radar/use', data={'mac': '00:11:22:33:44:55',
                                        'kind': 'spp', 'name': 'BT578'})
    rd = config.load()['radar']
    assert rd['bluetooth_mac'] == '00:11:22:33:44:55' and rd['bluetooth_kind'] == 'spp'
    # garbage is refused and writes nothing
    r = client.post('/radar/use', data={'mac': 'not-a-mac', 'kind': 'ble'})
    assert 'not+a+Bluetooth+MAC' in r.headers['Location']
    assert config.load()['radar']['bluetooth_mac'] == '00:11:22:33:44:55'
    # an unknown kind falls back to auto — the box finds out
    client.post('/radar/use', data={'mac': '7C:8D:40:3B:A1:80', 'kind': 'weird'})
    assert config.load()['radar']['bluetooth_kind'] == 'auto'
