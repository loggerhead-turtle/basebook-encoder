"""[NEW] does not mean "answered this scan", and that was the whole bug.

FIELD REPORT, after a day of it: "my headset says 'ready to pair' and
then nothing... this still isn't working... I tried both devices." The
page read `0 answered this scan · 1 remembered from before` with the bud
flashing two feet away, and ⚡ Pair the flashing bud came back with "no
new earbud appeared — is it flashing in pairing mode?"

BlueZ prints `[NEW] Device …` when it CREATES the D-Bus object for a
device — the first time it ever meets one. A device it already has an
object for answers the very same scan with

    [CHG] Device 60:AB:D2:11:22:33 RSSI: -47

and both the scan list and the auto-pair loop threw every one of those
lines away. Objects persist FOREVER for anything paired, and until
bluetoothd purges it for anything else.

So: a bud you paired yesterday can never be [NEW] again. Neither can a
fridge you scanned ten minutes ago. It reads as a radio gone deaf, and it
is why this worked the day the box was set up and never afterwards — the
one thing that reliably produced [NEW] lines was a reboot.

RE-PAIRING THE BUD YOU ALREADY OWN is the most common thing anyone does
on this page, and it was the exact case that could not work: `[NEW]` was
impossible for it, and `if mac in known: continue` skipped it a second
time even if a [NEW] had somehow arrived. Two independent blocks on the
one path that mattered.

RSSI is the right evidence. It only arrives on a real advertisement or
inquiry response, so it means "heard, just now" in a way no other
property change does.

Run: python -m pytest tests/test_scan_liveness.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from comms import comms_ear                             # noqa: E402

BUD = '60:AB:D2:11:22:33'
FRIDGE = '41:2C:9A:44:55:66'

# What a scan REALLY looks like once the devices are known to bluetoothd:
# no [NEW] at all, just property churn carrying RSSI.
KNOWN_SCAN = f"""Discovery started
[CHG] Controller 2C:CF:67:2C:CC:8F Discovering: yes
[CHG] Device {BUD} RSSI: -47
[CHG] Device {FRIDGE} RSSI: -71
[CHG] Device {BUD} TxPower: 4
"""

FIRST_EVER_SCAN = f"""Discovery started
[NEW] Device {BUD} JLab GO Sport+
[NEW] Device {FRIDGE} Fridge
"""


# ── what counts as an answer ─────────────────────────────────────────────

def test_a_brand_new_device_counts():
    assert comms_ear._live_macs(FIRST_EVER_SCAN) == {BUD, FRIDGE}


def test_a_device_bluez_already_knows_counts_too():
    """The whole bug in one assertion. This returned an empty set."""
    assert comms_ear._live_macs(KNOWN_SCAN) == {BUD, FRIDGE}


def test_property_churn_without_rssi_is_not_evidence_of_presence():
    """A device going Connected:no or Paired:yes says nothing about
    whether it is in the room."""
    assert comms_ear._live_macs(
        f'[CHG] Device {BUD} Connected: no\n'
        f'[CHG] Device {BUD} Paired: yes\n') == set()


def test_the_controller_is_not_a_device():
    assert comms_ear._live_macs(
        '[CHG] Controller 2C:CF:67:2C:CC:8F Discovering: yes\n') == set()


def test_nothing_at_all_is_still_nothing():
    assert comms_ear._live_macs('') == set()
    assert comms_ear._live_macs('Discovery started\n') == set()


# ── the list ─────────────────────────────────────────────────────────────

def test_the_scan_list_stops_calling_a_live_device_a_memory(monkeypatch):
    """0 answered · 1 remembered, with the bud shouting from two feet."""
    def fake_bt(*a, **k):
        if a[:1] == ('devices',):
            return (f'Device {BUD} JLab GO Sport+\n'
                    f'Device {FRIDGE} Fridge\n')
        if 'scan' in a:
            return KNOWN_SCAN
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    devs = {d['mac']: d for d in comms_ear.bt_scan()}
    assert devs[BUD]['live'] is True
    assert devs[FRIDGE]['live'] is True


# ── the pairing path, which is what actually blocked him ─────────────────

class _Scanner:
    def __init__(self, lines):
        import io
        self.stdout = io.StringIO(''.join(lines))

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return None


def _autopair(monkeypatch, scan_lines, paired=(), connected=(), devices=''):
    seq = []

    def fake_bt(*a, **k):
        seq.append(a)
        if a[:1] == ('devices',):
            return devices
        if a[:1] == ('show',):
            return 'Controller 2C:CF:67:2C:CC:8F (public)\n'
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    monkeypatch.setattr(comms_ear, '_paired_macs', lambda: list(paired))
    monkeypatch.setattr(comms_ear, 'bt_status', lambda: {
        'ok': True, 'connected': [{'mac': m, 'name': 'x'}
                                  for m in connected]})
    monkeypatch.setattr(comms_ear, '_pair_seq', lambda mac: 'PAIRED ' + mac)
    monkeypatch.setattr(comms_ear.subprocess, 'Popen',
                        lambda *a, **k: _Scanner(scan_lines))
    # The give-up timer is 22 REAL seconds, and four of these tests are
    # meant to reach it. Run the clock off the sleeps instead so the suite
    # stays something people actually run.
    clock = [0.0]
    monkeypatch.setattr(comms_ear.time, 'time', lambda: clock[0])
    monkeypatch.setattr(comms_ear.time, 'sleep',
                        lambda n: clock.__setitem__(0, clock[0] + n))
    return comms_ear.bt_autopair(), seq


KNOWN_LINES = [f'[CHG] Device {BUD} RSSI: -47\n']


def test_autopair_finds_a_bud_bluez_already_knows(monkeypatch):
    """The reported failure. This returned "no new earbud appeared" to a
    coach holding a flashing earbud."""
    out, _seq = _autopair(monkeypatch, KNOWN_LINES,
                          devices=f'Device {BUD} JLab GO Sport+\n')
    assert 'PAIRED ' + BUD in out
    assert 'JLab GO Sport+' in out


def test_autopair_drops_a_stale_bond_before_re_pairing(monkeypatch):
    """Bonded but not connected is the stale bond that brought him here.
    Pairing on top of it is what the bud keeps refusing."""
    out, seq = _autopair(monkeypatch, KNOWN_LINES, paired=[BUD],
                         devices=f'Device {BUD} JLab GO Sport+\n')
    assert ('remove', BUD) in seq, 'the dead bond has to go first'
    assert 'PAIRED ' + BUD in out


def test_autopair_leaves_a_working_earpiece_alone(monkeypatch):
    """Connected means it is somebody's ear right now. Never grab it."""
    out, seq = _autopair(monkeypatch, KNOWN_LINES, paired=[BUD],
                         connected=[BUD],
                         devices=f'Device {BUD} JLab GO Sport+\n')
    assert 'nothing answered' in out
    assert ('remove', BUD) not in seq


def test_autopair_still_avoids_the_ble_twin(monkeypatch):
    """It pairs fine and carries no audio — success you cannot hear."""
    out, _seq = _autopair(
        monkeypatch, [f'[CHG] Device {FRIDGE} RSSI: -60\n'],
        devices=f'Device {FRIDGE} JLab GO Sport+-BLE\n')
    assert 'nothing answered' in out


def test_autopair_does_not_take_a_property_name_for_a_device_name(
        monkeypatch):
    """A [CHG] line's tail is 'RSSI: -47', not a name. Reading that as
    the bud's name would pair the right MAC under a nonsense label — and
    would sail straight past the -BLE check."""
    out, _seq = _autopair(monkeypatch, KNOWN_LINES,
                          devices=f'Device {BUD} JLab GO Sport+\n')
    assert 'RSSI' not in out


def test_autopair_waits_when_it_has_no_name_at_all(monkeypatch):
    """An unnamed device could be anything in the car park. The old code
    waited for a name and that part was right."""
    out, _seq = _autopair(monkeypatch, KNOWN_LINES, devices='')
    assert 'nothing answered' in out
