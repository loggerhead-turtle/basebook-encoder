"""The reconnect chaser was eating the scan.

FIELD REPORT: "my headset says 'ready to pair' and then nothing. it
worked earlier yesterday, and then nothing today after the newly
installed adapter. it is not the Bluetooth headset. I'm convinced of it."

He was right, and it was not the adapter either.

`bt_pair` takes PAIRING['busy'] and BT_LOCK, with a docstring saying
exactly why: a collision with the reconnect chaser is
org.bluez.Error.InProgress. `bt_scan` took neither. So a 12-second
discovery ran straight through the chaser's `connect` attempts, and BlueZ
will not run an inquiry and a connection on one controller at the same
time. Discovery loses. Silently — the error lands in output nobody parses
and the scan returns the adapter cache plus whatever BLE advertising
leaked past, which is precisely what he saw: a fridge, a '-BLE' twin, and
never the classic half of the bud he was holding.

IT ONLY BITES ONCE A BUD IS PAIRED AND ABSENT — which is the exact state
of a headset sitting in pairing mode waiting to be re-paired. That is why
it "worked yesterday": yesterday the bud was connected, so the chaser had
nothing to chase. The moment it dropped, the chaser began waking every 7 s
and holding the controller for up to 8, forever, and the one operation
that could have fixed it was the one being blocked.

Which also means an earpiece left at home costs half the radio all day,
so misses now back off per bud and any success resets to instant.

Run: python -m pytest tests/test_scan_vs_chaser.py
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from comms import comms_ear                             # noqa: E402

BUD = '60:AB:D2:11:22:33'


@pytest.fixture(autouse=True)
def clean():
    comms_ear._CHASE.clear()
    comms_ear.PAIRING['busy'] = False
    yield
    comms_ear._CHASE.clear()
    comms_ear.PAIRING['busy'] = False


# ── the collision ────────────────────────────────────────────────────────

def test_a_scan_holds_the_bluetooth_lock(monkeypatch):
    """The chaser acquires BT_LOCK non-blocking and skips when it can't
    get it. If the scan doesn't hold it, they run at the same time."""
    held = {}

    def fake_bt(*a, **k):
        held['locked'] = comms_ear.BT_LOCK.locked()
        held['busy'] = comms_ear.PAIRING['busy']
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    comms_ear.bt_scan()
    assert held['locked'] is True, 'the chaser could have cut in'
    assert held['busy'] is True, 'and it checks the flag before the lock'


def test_the_lock_is_released_even_when_the_scan_blows_up(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('bluetoothctl died')
    monkeypatch.setattr(comms_ear, '_bt', boom)
    with pytest.raises(RuntimeError):
        comms_ear.bt_scan()
    assert not comms_ear.BT_LOCK.locked()
    assert comms_ear.PAIRING['busy'] is False, \
        'a stuck flag would stop the chaser forever'


def test_the_chaser_stands_down_while_a_scan_runs(monkeypatch):
    """End to end on the real threading primitives: start a scan, and
    prove a concurrent chaser tick issues no connect."""
    connects = []
    gate = threading.Event()

    def fake_bt(*a, **k):
        if a[:1] == ('connect',):
            connects.append(a[1])
            return 'Connection successful'
        if 'scan' in a:
            gate.set()                     # scan is in flight
            comms_ear.time.sleep(0.05)
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    monkeypatch.setattr(comms_ear, 'bt_status',
                        lambda: {'ok': True, 'connected': []})
    monkeypatch.setattr(comms_ear, 'ear_labels', lambda: {BUD: 'catcher'})

    t = threading.Thread(target=comms_ear.bt_scan, daemon=True)
    t.start()
    gate.wait(2)
    # exactly what one tick of reconnect_loop does, minus the sleep
    if not comms_ear.PAIRING['busy'] \
            and comms_ear.BT_LOCK.acquire(blocking=False):
        try:
            comms_ear._bt('connect', BUD, timeout=8)
        finally:
            comms_ear.BT_LOCK.release()
    t.join(3)
    assert connects == [], 'the chaser cut into the scan'


# ── the backoff ──────────────────────────────────────────────────────────

def test_a_bud_is_chased_immediately_the_first_time():
    assert comms_ear._chase_due(BUD) is True


def test_repeated_misses_back_off():
    comms_ear._chase_mark(BUD, False, now=0)
    assert comms_ear._chase_due(BUD, now=1) is False
    assert comms_ear._chase_due(BUD, now=8) is True
    comms_ear._chase_mark(BUD, False, now=8)
    assert comms_ear._chase_due(BUD, now=16) is False
    assert comms_ear._chase_due(BUD, now=23) is True


def test_the_backoff_is_capped_so_a_bud_coming_back_is_still_caught():
    now = 0.0
    for _ in range(20):
        comms_ear._chase_mark(BUD, False, now=now)
        now += 1000
    comms_ear._chase_mark(BUD, False, now=0)
    assert comms_ear._chase_due(BUD, now=comms_ear._CHASE_MAX + 1) is True
    assert comms_ear._CHASE_MAX <= 120


def test_success_resets_it_to_instant():
    comms_ear._chase_mark(BUD, False, now=0)
    comms_ear._chase_mark(BUD, False, now=8)
    comms_ear._chase_mark(BUD, True, now=9)
    assert comms_ear._chase_due(BUD, now=9) is True
    assert BUD not in comms_ear._CHASE


def test_the_case_is_not_what_decides_whether_a_bud_is_chased():
    """bluetoothctl prints MACs upper-case, ear_labels stores whatever the
    scan gave us. A mismatch here means a bud is chased twice as often as
    it should be, or backed off under one spelling and hammered under the
    other."""
    comms_ear._chase_mark(BUD.lower(), False, now=0)
    assert comms_ear._chase_due(BUD.upper(), now=1) is False
