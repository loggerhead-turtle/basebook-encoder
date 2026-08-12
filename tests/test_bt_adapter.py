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
    # `show` reports the controller bluetoothctl is really aimed at; the
    # fixture lets a test move it independently of the pin, because on
    # this box they have been seen to disagree.
    aimed = {'at': DONGLE}

    def fake_bt(*a, **k):
        if a[:1] == ('list',):
            return BT_LIST
        if a[:1] == ('show',):
            return f'Controller {aimed["at"]} (public)\n\tPowered: yes\n'
        return ''
    monkeypatch.setattr(comms_ear, '_bt', fake_bt)
    # the real /sys/class/bluetooth also carries child nodes
    monkeypatch.setattr(os, 'listdir',
                        lambda p: ['hci0', 'hci0:2', 'hci1',
                                   'hci1:12'])
    monkeypatch.setattr(os.path, 'realpath', lambda p: SYSFS[p.split('/')[-1]])
    # his kernel: sysfs has no address, BlueZ over D-Bus does — and it maps
    # hci0 to the BUILT-IN radio even though bluetoothctl lists the dongle
    # first, which is exactly the trap
    monkeypatch.setattr(comms_ear, '_adapter_address',
                        lambda hci: {'hci0': BUILTIN, 'hci1': DONGLE}[hci])
    state = {'blocked': {'hci0': False, 'hci1': False}, 'calls': [],
             'aimed': aimed}
    monkeypatch.setattr(comms_ear, '_rfkill_rows', lambda: [
        {'id': '0', 'dev': 'hci0', 'blocked': state['blocked']['hci0']},
        {'id': '1', 'dev': 'hci1', 'blocked': state['blocked']['hci1']}])

    comms_ear.ADAPTER_ERR.update(no_permission=False,
                                 did_not_take=False)

    def fake_rfkill(action, ident):
        if not state.get('allowed', True):
            return False              # "Operation not permitted"
        dev = {'0': 'hci0', '1': 'hci1'}[str(ident)]
        state['blocked'][dev] = (action == 'block')
        state['calls'].append((action, dev))
        return True
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
    assert 'USB dongle' in html and 'built-in radio' in html


def test_the_card_never_claims_an_antenna_it_cannot_see(box):
    """Whether a dongle has an external antenna is not visible from
    here. The UB500 in his box is the nano model with an internal one,
    and the card was congratulating him on range he had not installed."""
    assert 'external antenna' not in comms_ear._adapter_card()


def test_a_pinned_box_is_warned_that_its_pairings_did_not_come_along(box):
    """BlueZ keys pairings by adapter, so switching hands you an empty
    list and a bud that will not appear in a scan until it is put back
    into pairing mode. Discovering that at a field is an afternoon."""
    comms_ear.set_adapter_pref(DONGLE)
    html = comms_ear._adapter_card()
    assert 'Pairings belong to the adapter' in html
    assert 'pairing mode' in html


def test_an_unpinned_box_is_told_why_that_is_a_problem(box):
    html = comms_ear._adapter_card()
    assert 'Not pinned' in html
    assert 'changes between reboots' in html


def test_a_pinned_box_shows_which_one_is_live(box):
    comms_ear.set_adapter_pref(DONGLE)
    html = comms_ear._adapter_card()
    assert '← in use' in html
    assert 'NOT the one in use' not in html
    assert 'unpin' in html, 'and a way back out'


def test_in_use_is_read_from_bluez_not_assumed_from_the_pin(box):
    """Powering the others down is meant to leave BlueZ no choice, and it
    is not quite a guarantee — this box printed a controller as [default]
    while that same controller read PowerState: off-blocked. If the card
    says "in use" about a radio nothing is using, every other reading on
    the page is a lie."""
    comms_ear.set_adapter_pref(DONGLE)
    box['aimed']['at'] = BUILTIN            # BlueZ ignored the pin
    html = comms_ear._adapter_card()
    assert 'NOT the one in use' in html
    assert 'BlueZ is still using a different radio' in html
    assert BUILTIN in html


def test_unpinning_powers_the_other_radio_back_up(box):
    """The escape hatch has to actually escape. Returning early left the
    radios exactly as the last pin had powered them, so "let BlueZ
    choose" handed BlueZ one choice — and the coach tapping it is a coach
    trying to get back to the setup that worked yesterday."""
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    assert box['blocked'] == {'hci0': True, 'hci1': False}
    comms_ear.set_adapter_pref('')
    comms_ear.enforce_adapter()
    assert box['blocked'] == {'hci0': False, 'hci1': False}


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


# ── when the box is not allowed to touch the radios ──────────────────────

def test_a_refused_rfkill_is_noticed_not_swallowed(box):
    """READING rfkill works as any user; WRITING needs root. That is what
    made this so quiet — the card could print the block state perfectly
    while every attempt to change it failed into output nothing read."""
    box['allowed'] = False
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    assert comms_ear.ADAPTER_ERR['no_permission'] is True
    assert box['blocked'] == {'hci0': False, 'hci1': False}


def test_the_card_names_the_permission_problem_not_a_wild_goose_chase(box):
    """"Unpin and try the other adapter" is useless advice when the pin
    was never applied at all — he would swap adapters forever."""
    box['allowed'] = False
    box['aimed']['at'] = BUILTIN
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    html = comms_ear._adapter_card()
    assert 'not allowed to power a radio down' in html
    assert 'install_comms.sh' in html


def test_a_working_install_says_nothing_about_permissions(box):
    box['aimed']['at'] = BUILTIN
    comms_ear.set_adapter_pref(DONGLE)
    comms_ear.enforce_adapter()
    html = comms_ear._adapter_card()
    assert 'not allowed' not in html


def test_a_pin_that_does_not_take_puts_every_radio_back(box):
    """The vicious failure: the box comes up on a radio carrying none of
    its pairings, with the earpiece it needs bonded to the one we just
    switched off. Verify, then keep — or put it all back."""
    box['aimed']['at'] = BUILTIN            # BlueZ ignores the pin
    comms_ear.set_adapter_pref(DONGLE)
    assert comms_ear.enforce_adapter() is None
    assert box['blocked'] == {'hci0': False, 'hci1': False}
    assert comms_ear.ADAPTER_ERR['did_not_take'] is True
    assert 'left switched on' in comms_ear._adapter_card()


def test_a_pin_that_takes_is_kept(box):
    box['aimed']['at'] = DONGLE             # BlueZ followed the pin
    comms_ear.set_adapter_pref(DONGLE)
    got = comms_ear.enforce_adapter()
    assert got and got['mac'] == DONGLE
    assert box['blocked'] == {'hci0': True, 'hci1': False}
    assert comms_ear.ADAPTER_ERR['did_not_take'] is False


def test_rfkill_escalates_when_it_has_to(monkeypatch):
    tried = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = self.stderr = ''

    def fake_run(cmd, **k):
        tried.append(cmd)
        return _R(0 if cmd[:1] == ['sudo'] else 1)
    monkeypatch.setattr(comms_ear.subprocess, 'run', fake_run)
    assert comms_ear._rfkill('block', 0) is True
    assert tried[0][0] == 'rfkill', 'plain first — no sudo when not needed'
    assert tried[1][:3] == ['sudo', '-n', 'rfkill']


def test_rfkill_reports_failure_when_even_sudo_is_refused(monkeypatch):
    class _R:
        returncode = 1
        stdout = stderr = ''
    monkeypatch.setattr(comms_ear.subprocess, 'run', lambda *a, **k: _R())
    assert comms_ear._rfkill('block', 0) is False


# ── the grant itself ─────────────────────────────────────────────────────

def _installer():
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'comms', 'install_comms.sh')).read()


def test_the_installer_grants_rfkill():
    src = _installer()
    assert '/usr/sbin/rfkill block' in src
    assert '/usr/sbin/rfkill unblock' in src


def test_the_grant_cannot_switch_off_the_box_s_own_network():
    """A bare wildcard would also allow `rfkill block all`, which turns
    the Wi-Fi off and takes the box off the network for good — from a
    page reachable on the LAN behind a four-digit PIN."""
    src = _installer()
    assert 'rfkill block *' not in src
    assert 'rfkill unblock *' not in src
    assert 'rfkill block [0-9]*' in src


def test_child_nodes_are_not_mistaken_for_radios(box):
    """/sys/class/bluetooth carries entries like 'hci0:2' — an rfcomm/LE
    sub-device, not a controller. One slipped through as a second adapter
    with no MAC, and the card drew it in red as picked-but-not-in-use: an
    invented fault, on the page a coach reads when something is wrong."""
    got = [a['hci'] for a in comms_ear.adapters()]
    assert got == ['hci0', 'hci1']
    assert all(a['mac'] for a in comms_ear.adapters()), 'no MAC-less ghosts'
