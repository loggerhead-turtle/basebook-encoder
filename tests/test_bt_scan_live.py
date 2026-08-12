"""What "found nearby" actually means.

FIELD REPORT, after pinning the USB dongle: "there used to be like 10
devices found... and, I'm missing all of the other Bluetooth devices
around me that are no longer showing up on the scan."

Nothing went deaf. The list was never "what is around you" — bt_scan
merges the live scan's [NEW] lines with `bluetoothctl devices`, and that
second command is the ADAPTER'S CACHE: every device that radio has ever
met, kept under /var/lib/bluetooth/<adapter>/cache. A box that has sat in
a house for months lists the neighbours' phones and televisions whether
or not they are switched on today.

Pin a different adapter and that cache is empty, because the new radio
has met nobody. The list drops from ten devices to two and reads as a
broken dongle. Those two are simply the only things actually in range AND
announcing themselves.

Which is the second half of it: only devices in PAIRING MODE answer an
inquiry. His JLab showed its '-BLE' twin (BLE advertises continuously)
and not its classic half (silent unless flashing), and that difference is
the whole reason the bud he wants looked missing.

So the page stops running the two together. Answering-right-now is the
only evidence that a bud can be paired; remembered is a fact about the
radio's memory, and it is labelled as one.

Run: python -m pytest tests/test_bt_scan_live.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from comms import comms_ear                             # noqa: E402

# a real 12 s scan: one bud actually flashing, plus BLE chatter
SCAN_OUT = """Discovery started
[NEW] Device 60:AB:D2:11:22:33 JLab GO Sport+
[NEW] Device 41:2C:9A:44:55:66 Fridge
[CHG] Device 60:AB:D2:11:22:33 RSSI: -47
"""

# ...and the adapter's memory, which on the OLD radio was months deep
CACHED = """Device 60:AB:D2:11:22:33 JLab GO Sport+
Device 41:2C:9A:44:55:66 Fridge
Device AA:AA:AA:00:00:01 Living Room TV
Device AA:AA:AA:00:00:02 Kate's iPhone
Device AA:AA:AA:00:00:03 Soundbar
"""


@pytest.fixture
def scan(monkeypatch):
    def fake_bt(*a, **k):
        if a[:1] == ('devices',):
            return CACHED
        if 'scan' in a:
            return SCAN_OUT
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    return comms_ear.bt_scan()


def test_it_still_lists_everything_it_knows(scan):
    assert len(scan) == 5


def test_it_marks_the_ones_that_actually_answered(scan):
    live = {d['name'] for d in scan if d['live']}
    assert live == {'JLab GO Sport+', 'Fridge'}


def test_it_marks_the_ones_it_is_only_remembering(scan):
    old = {d['name'] for d in scan if not d['live']}
    assert old == {'Living Room TV', "Kate's iPhone", 'Soundbar'}


def test_a_fresh_adapter_lists_only_what_is_here(monkeypatch):
    """The reported symptom, reproduced: pin a new dongle and the cache
    is empty, so ten devices become two. Both of them real."""
    def fake_bt(*a, **k):
        if a[:1] == ('devices',):
            return ''            # a radio that has met nobody
        if 'scan' in a:
            return SCAN_OUT
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    devs = comms_ear.bt_scan()
    assert len(devs) == 2
    assert all(d['live'] for d in devs), 'and every one is really there'


def test_a_name_that_is_just_the_mac_again_is_not_a_name(monkeypatch):
    def fake_bt(*a, **k):
        if 'scan' in a:
            return '[NEW] Device 60:AB:D2:11:22:33 60-AB-D2-11-22-33\n'
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    assert comms_ear.bt_scan()[0]['name'] == ''


# ── how the page says it ─────────────────────────────────────────────────

def _page(monkeypatch, devs):
    monkeypatch.setattr(comms_ear, 'bt_scan', lambda: list(devs))
    monkeypatch.setattr(comms_ear, 'bt_status', lambda: {
        'ok': True, 'connected': [], 'pairable': True,
        'discoverable': True})
    monkeypatch.setattr(comms_ear, '_ghost_rows', lambda st: '')
    monkeypatch.setattr(comms_ear, '_adapter_card', lambda: '')
    monkeypatch.setattr(comms_ear, 'audio_ok', lambda: True)
    monkeypatch.setattr(comms_ear, 'code_version', lambda: 'test')
    return comms_ear._page_body({'scanned': '1'})


LIVE = {'mac': '60:AB:D2:11:22:33', 'name': 'JLab GO Sport+', 'live': True}
OLD = {'mac': 'AA:AA:AA:00:00:01', 'name': 'Living Room TV', 'live': False}


def test_the_page_counts_both_kinds(monkeypatch):
    html = _page(monkeypatch, [LIVE, OLD])
    assert '1 answered this scan' in html
    assert '1 remembered from before' in html


def test_a_remembered_device_is_not_offered_as_if_it_were_here(monkeypatch):
    """Tapping it gets a timeout. Say so on the row instead."""
    html = _page(monkeypatch, [LIVE, OLD])
    i = html.index('Living Room TV')
    assert 'remembered, not answering now' in html[i:i + 260]
    j = html.index('JLab GO Sport+')
    assert 'remembered' not in html[j:j + 200]


def test_the_ones_that_answered_come_first(monkeypatch):
    html = _page(monkeypatch, [OLD, LIVE])
    assert html.index('JLab GO Sport+') < html.index('Living Room TV')


def test_a_short_list_is_explained_rather_than_left_to_look_broken(
        monkeypatch):
    """The two facts a coach needs and cannot be expected to know: only a
    bud in pairing mode answers at all, and a freshly pinned adapter has
    no memory to pad the list with."""
    html = _page(monkeypatch, [LIVE])
    assert 'PAIRING MODE' in html
    assert 'a short list is normal' in html
    assert 'freshly pinned adapter' in html


def test_the_ble_twin_is_still_steered_around(monkeypatch):
    """It pairs fine and carries no audio — the silent failure that looks
    like success, and the only JLab entry his scan returned."""
    ble = {'mac': '60:AB:D2:11:22:34', 'name': 'JLab GO Sport+-BLE',
           'live': True}
    html = _page(monkeypatch, [ble])
    assert 'BLE twin, no audio' in html
