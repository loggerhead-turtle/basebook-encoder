"""
"Find my adapter" — the scan behind the button on the radar card.

A coach who buys a Bluetooth serial adapter for the gun should not
have to learn what a MAC address is. The box scans, lists what it
saw by name with the likely adapters on top, and one tap writes the
address and the kind into config. The bridge (BLE) or the rfcomm
binder (classic) takes it from there.

Why bluetoothctl and not bleak:

* an LE scan sees only LE devices, and a classic adapter (BT578) is
  a real thing people own — `bluetoothctl scan on` reports both;
* BlueZ allows ONE discovery per D-Bus client, and every bleak
  scanner in the encoder is the same client. The BLE serial bridge
  is scanning in its own thread whenever it is not connected; a
  second bleak scan from the web thread would fail 'Operation
  already in progress' (19 Sep 2026). bluetoothctl is its own D-Bus
  client, and BlueZ merges discovery sessions from different clients
  without complaint;
* it needs nothing installed beyond bluez itself.

Everything that parses is a pure function of text, so it is tested
against captured output; only scan() shells out.
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger('btscan')

SCAN_S = 8
MAX_INFO = 40           # bluetoothctl info calls per scan, ~50 ms each

# The names serial adapters ship with. A match is a strong hint; the
# service UUIDs below are a stronger one; both together is certainty.
ADAPTER_NAME_RE = re.compile(
    r'VELOBEAM|IRXON|HM-?1[0-9]|HMSoft|BT-?578|BT-?0[45]|JDY|DSD|HC-?0[5-8]'
    r'|MLT-BT|SPP|UART|SERIAL|RS-?232',
    re.I)

# Serial-over-Bluetooth services, LE and classic.
SERIAL_UUIDS = {
    '0000ffe0-0000-1000-8000-00805f9b34fb',   # HM-10 family (IRXON)
    '0000ffe1-0000-1000-8000-00805f9b34fb',
    '0000fff0-0000-1000-8000-00805f9b34fb',   # some JDY/BT05 clones
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e',   # Nordic UART
    '00001101-0000-1000-8000-00805f9b34fb',   # classic SPP (BT578)
}
SPP_UUID = '00001101-0000-1000-8000-00805f9b34fb'

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_MAC = r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})'
_NEW = re.compile(r'\[(?:NEW|CHG)\]\s+Device\s+' + _MAC + r'\s*(.*)$')
_RSSI = re.compile(r'RSSI:\s*(-?\d+)')
_NAME_CHG = re.compile(r'^(?:Name|Alias):\s*(.*)$')
_UUID = re.compile(r'\(([0-9a-fA-F-]{36})\)')


def parse_scan(text):
    """`bluetoothctl --timeout N scan on` output → {MAC: {name, rssi}},
    in the order first seen. Handles the colour codes bluetoothctl
    prints even when not on a tty, [NEW] with a name, and the [CHG]
    lines that carry RSSI or a late-arriving name."""
    seen = {}
    for raw in (text or '').splitlines():
        line = _ANSI.sub('', raw).strip()
        m = _NEW.search(line)
        if not m:
            continue
        mac, rest = m.group(1).upper(), m.group(2).strip()
        d = seen.setdefault(mac, {'name': '', 'rssi': None})
        r = _RSSI.search(rest)
        if r:
            d['rssi'] = int(r.group(1))
            continue
        n = _NAME_CHG.match(rest)
        if n:
            d['name'] = n.group(1).strip()
            continue
        if line.startswith('[NEW]') and rest and rest.replace(':', '').replace('-', '').upper() != mac.replace(':', ''):
            d['name'] = rest
    return seen


def parse_info(text):
    """`bluetoothctl info MAC` → {name, uuids, le, connected}. LE is
    read off AdvertisingFlags (only an LE advertisement has them) or
    an LE serial service; a classic adapter paired for rfcomm shows
    neither."""
    name, uuids, le, connected = '', [], False, False
    for raw in (text or '').splitlines():
        line = _ANSI.sub('', raw).strip()
        low = line.lower()
        if low.startswith('name:'):
            name = line.split(':', 1)[1].strip()
        elif low.startswith('alias:') and not name:
            name = line.split(':', 1)[1].strip()
        elif low.startswith('uuid:'):
            u = _UUID.search(line)
            if u:
                uuids.append(u.group(1).lower())
        elif low.startswith('advertisingflags'):
            le = True
        elif low.startswith('connected:'):
            connected = 'yes' in low
    if any(u in SERIAL_UUIDS and u != SPP_UUID for u in uuids):
        le = True
    return {'name': name, 'uuids': uuids, 'le': le, 'connected': connected}


def classify(mac, seen, info):
    """One candidate for the list. kind is what the config wants:
    'ble' (the bridge), 'spp' (the rfcomm binder) or 'auto' when the
    scan could not tell. score orders the list — serial service and
    adapter-ish name on top, nameless nothing at the bottom."""
    name = (info.get('name') or seen.get('name') or '').strip()
    uuids = set(info.get('uuids') or [])
    serial = bool(uuids & SERIAL_UUIDS)
    named = bool(ADAPTER_NAME_RE.search(name))
    if info.get('le'):
        kind = 'ble'
    elif SPP_UUID in uuids:
        kind = 'spp'
    else:
        kind = 'auto'
    score = (2 if serial else 0) + (2 if named else 0) + (1 if name else 0)
    return {'mac': mac, 'name': name, 'rssi': seen.get('rssi'),
            'kind': kind, 'serial': serial, 'likely': serial or named,
            'score': score}


def scan(seconds=SCAN_S, run=subprocess.run):
    """The whole thing: scan, ask about each device seen, rank. Returns
    {'found': [candidates…], 'error': ''} — error set (and found
    empty) when bluetoothctl is missing or Bluetooth is down. Nameless
    devices that advertise no serial service are left out: a serial
    adapter always has a name, and a phone's rotating address is
    nobody's gun."""
    try:
        r = run(['bluetoothctl', '--timeout', str(int(seconds)), 'scan', 'on'],
                capture_output=True, text=True, timeout=seconds + 15)
    except FileNotFoundError:
        return {'found': [], 'error': 'bluetoothctl is not installed '
                                      '(sudo apt install -y bluez)'}
    except subprocess.TimeoutExpired:
        return {'found': [], 'error': 'the Bluetooth scan hung — '
                                      'sudo systemctl restart bluetooth'}
    except Exception as e:
        return {'found': [], 'error': f'scan failed: {e}'}
    out = (r.stdout or '') + '\n' + (r.stderr or '')
    low = out.lower()
    if 'no default controller' in low or 'not available' in low and not _NEW.search(out):
        return {'found': [], 'error': 'this box has no Bluetooth adapter '
                                      'up — bluetoothctl show / rfkill list'}
    seen = parse_scan(out)
    found = []
    for mac, d in list(seen.items())[:MAX_INFO]:
        info = {}
        try:
            ri = run(['bluetoothctl', 'info', mac], capture_output=True,
                     text=True, timeout=5)
            info = parse_info(ri.stdout or '')
        except Exception:
            pass
        c = classify(mac, d, info)
        if not c['name'] and not c['serial']:
            continue
        found.append(c)
    found.sort(key=lambda c: (-c['score'], -(c['rssi'] if c['rssi'] is not None else -999),
                              c['name'].lower()))
    log.info(f'adapter scan: {len(seen)} device(s) seen, {len(found)} listed'
             + (f", top: {found[0]['name']} [{found[0]['mac']}] {found[0]['kind']}"
                if found else ''))
    return {'found': found, 'error': ''}
