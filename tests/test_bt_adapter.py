"""Which radio the comms box actually uses.

FIELD REPORT: a TP-Link UB500 (RTL8761BU) went into the box for the
external antenna. Firmware loaded, the dongle came up as hci1 — and
nothing improved, because every bluetoothctl call in comms_ear runs
against BlueZ's DEFAULT controller and the default is not stable.

From the box itself, before a reboot:

    Controller AC:A7:F1:29:A9:29 playcall-encoder #2 [default]   <- dongle
    Controller 2C:CF:67:2C:CC:8F playcall-encoder

and after one, with nothing changed in between:

    Controller 2C:CF:67:2C:CC:8F (public)                        <- built-in
            Powered: yes

So the coach gets the external antenna on some boots and not others, and
has no way to tell which — the range is simply worse some days. That is
the worst kind of fault: intermittent, invisible, and it looks like the
field.

bluetoothctl has no per-invocation adapter flag (`select` lasts one
interactive session; every call here is one-shot), so pinning is done the
only way that holds for all of them: POWER DOWN the adapters we did not
choose. One controller up means "default" has nothing to be ambiguous
about, and every existing call lands on the right radio without knowing
this code exists.

And it is re-applied at STARTUP, not just when the button is tapped —
the thing being corrected is a boot-time race, so a preference that waits
for a human is a preference that is wrong every morning.

Run: python -m pytest tests/test_bt_adapter.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from comms import comms_ear                             # noqa: E402

DONGLE = 'AC:A7:F1:29:A9:29'
BUILTIN = '2C:CF:67:2C:CC:8F'

# straight from the box
BT_LIST = (f'Controller {DONGLE} playcall-encoder #2 [default]\n'
           f'Controller {BUILTIN} playcall-encoder\n')

SYSFS = {
    'hci0': '/sys/devices/platform/soc@107c000000/107d50c000.serial/'
            '107d50c000.serial:0/serial0/serial0-0/bluetooth/hci0',
    'hci1': '/sys/devices/platform/axi/1000120000.pcie/1f00200000.usb/'
            'xhci-hcd.0/usb1/1-2/1-2:1.0/bluetooth/hci1',
}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A two-controller box, with the kernel that stopped exposing
    /sys/class/bluetooth/hciN/address (which his does)."""
    monkeypatch.setattr(comms_ear, 'ADAPTER_FILE', str(tmp_path / 'adapter'))
    monkeypatch.setattr(comms_ear, '_bt',
                        lambda *a, **k: BT_LIST if a[:1] == ('list',) else '')
    monkeypatch.setattr(os, 'listdir', lambda p: ['hci0', 'hci1'])
    monkeypatch.setattr(os.path, 'realpath', lambda p: SYSFS[p.split('/')[-1]])
    # his kernel: sysfs has no address, BlueZ over D-Bus does — and it maps
    # hci0 to the BUILT-IN radio even though bluetoothctl lists the dongle
    # first, which is exactly the trap
    monkeypatch.setattr(comms_ear, '_adapter_address',
                        lambda hci: {'hci0': BUILTIN, 'hci1': DONGLE}[hci])
    state = {'blocked': {'hci0': False, 'hci1': False}, 'calls': []}
    monkeypatch.setattr(comms_ear, '_rfkill_rows', lambda: [
        {'id': '0', 'dev': 'hci0', 'blocked': state['blocked']['hci0']},
        {'id': '1', 'dev': 'hci1', 'blocked': state['blocked']['hci1']}])

    def fake_rfkill(action, ident):
        dev = {'0': 'hci0', '1': 'hci1'}[str(ident)]
        state['blocked'][dev] = (action == 'block')
        state['calls'].append((action, dev))
    monkeypatch.setattr(comms_ear, '_rfkill', fake_rfkill)
    return state


# ── seeing what is there ─────────────────────────────────────────────────

def test_it_finds_both_controllers(box):
    assert [a['hci'] for a in comms_ear.adapters()] == ['hci0', 'hci1']


def test_it_tells_the_dongle_from_the_built_in_radio_by_the_bus(box):
    """Not by name, not by MAC — a dongle hangs off USB and the Pi's own
    radio hangs off the SoC serial line, and that is the one difference no
    vendor can get wrong."""
    ads = {a['hci']: a for a in comms_ear.adapters()}
    assert ads['hci1']['usb'] is True
    assert ads['hci0']['usb'] is False


def test_it_recovers_the_macs_the_kernel_stopped_publishing(box):
    """His kernel returns 'No such file or directory' for
    /sys/class/bluetooth/hciN/address. bluetoothctl still knows them."""
    ads = {a['hci']: a for a in comms_ear.adapters()}
    assert ads['hci0']['mac'] == BUILTIN, 'hci0 is the Pi, not the dongle'
    assert ads['hci1']['mac'] == DONGLE


# ── pinning one ──────────────────────────────────────────────────────────

def test_choosing_one_powers_the_other_down(box):
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    assert box['blocked'] == {'hci0': True, 'hci1': False}


def test_the_chosen_one_is_unblocked_even_if_it_was_blocked(box):
    """His dongle arrived soft-blocked — rfkill state persists per device,
    so a box that was ever blocked comes back blocked."""
    box['blocked'] = {'hci0': False, 'hci1': True}
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    assert box['blocked']['hci1'] is False


def test_the_choice_survives_a_restart(box):
    comms_ear.set_adapter_pref(DONGLE)
    assert comms_ear.adapter_pref() == DONGLE
    box['blocked'] = {'hci0': False, 'hci1': True}   # as if rebooted badly
    comms_ear.enforce_adapter()                      # what main() calls
    assert box['blocked'] == {'hci0': True, 'hci1': False}


def test_unpinning_leaves_bluez_to_it(box):
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.set_adapter_pref('')
    assert comms_ear.adapter_pref() == ''
    assert comms_ear.enforce_adapter() is None


def test_a_pinned_adapter_that_is_unplugged_changes_nothing(box, monkeypatch):
    """Somebody pulls the dongle at a field. Blocking the built-in radio
    because the pinned one is missing would take comms out entirely —
    leave BlueZ alone and let the box work."""
    comms_ear.set_adapter_pref('DE:AD:BE:EF:00:00')
    assert comms_ear.enforce_adapter() is None
    assert box['calls'] == []
    assert box['blocked'] == {'hci0': False, 'hci1': False}


def test_it_reports_which_adapter_it_settled_on(box):
    comms_ear.set_adapter_pref(DONGLE)
    got = comms_ear.enforce_adapter()
    assert got['mac'] == DONGLE
    assert got['hci'] == 'hci1' and got['usb'] is True


# ── the card ─────────────────────────────────────────────────────────────

def test_the_card_is_not_drawn_when_there_is_nothing_to_choose(monkeypatch):
    monkeypatch.setattr(comms_ear, 'adapters', lambda: [{'hci': 'hci0',
                                                         'mac': BUILTIN,
                                                         'usb': False,
                                                         'blocked': False,
                                                         'name': ''}])
    assert comms_ear._adapter_card() == ''


def test_the_card_offers_both_and_says_which_is_the_dongle(box):
    html = comms_ear._adapter_card()
    assert DONGLE in html and BUILTIN in html
    assert 'external antenna' in html and 'built-in radio' in html


def test_an_unpinned_box_is_told_why_that_is_a_problem(box):
    html = comms_ear._adapter_card()
    assert 'Not pinned' in html
    assert 'changes between reboots' in html


def test_a_pinned_box_shows_which_one_is_live(box):
    comms_ear.set_adapter_pref(DONGLE)
    html = comms_ear._adapter_card()
    assert 'in use' in html
    assert 'unpin' in html, 'and a way back out'


def test_the_button_posts_the_mac(box):
    html = comms_ear._adapter_card()
    assert 'action="/adapter"' in html
    assert f'name="mac" value="{DONGLE}"' in html


# ── the wiring that makes it stick ───────────────────────────────────────

def test_startup_enforces_the_pin():
    """The whole point. Read the source rather than booting a box: main()
    has to call enforce_adapter() BEFORE the threads that touch a radio."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'comms', 'comms_ear.py')).read()
    body = src[src.index('def main():'):]
    assert 'enforce_adapter()' in body
    assert body.index('enforce_adapter()') < body.index('poll_loop')


def test_a_broken_picker_never_costs_the_box():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'comms', 'comms_ear.py')).read()
    body = src[src.index('def main():'):src.index('def main():') + 700]
    i = body.index('enforce_adapter()')
    assert 'try:' in body[:i] and 'except Exception' in body[i:]
