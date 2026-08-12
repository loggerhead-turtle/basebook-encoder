"""The box telling the cloud about itself must never take it off the cloud.

FIELD REPORT, from the comms admin page on a live box:

    cloud: unreachable ('latin-1' codec can't encode character '\\u2026'
    in position 8: ordinal not in range(256))

Position 8 of `starting…` is the ellipsis, and `starting…` is this box's
own opening description of its voice link. HTTP header values are
latin-1 (http.client encodes them that way), and one character outside
that set does not fail the header — it raises before the request is sent,
so the whole poll fails. The box reports "unreachable" and collects no
calls for as long as that text is on screen.

THREE OF THE BOX'S OWN STRINGS WERE UNSENDABLE, and the worst of them is
the one that matters most:

    'starting…'                  every boot, until the link settles
    'answered — connecting…'     while a coach connects
    '🎙 LIVE — coach linked'      THE ENTIRE TIME A COACH IS TALKING

That last one means the box dropped off the cloud for exactly as long as
the coach was calling pitches to his catcher.

AND THE EAR NAMES ARE NOT OURS. `X-Pi-Ears` carries whatever name a
phone gave the earpiece, and iOS writes "Erik's AirPods" with a curly
apostrophe (U+2019, not latin-1). Pairing a bud could take the box off
the cloud until somebody renamed it — which nobody would think to do,
because the error names an ellipsis.

Two rules come out of it and both are tested here: every dynamic header
value goes through hdr(), and if one ever gets past hdr() anyway, the
poll drops the telemetry and keeps the calls. Telemetry is not allowed to
break the control path.

Run: python -m pytest tests/test_comms_headers.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from comms import comms_ear                             # noqa: E402

hdr = comms_ear.hdr


def _sendable(s):
    """The exact thing http.client does to a header value."""
    s.encode('latin-1')
    return True


# ── the strings the box says about itself ────────────────────────────────

REAL_VOICE_STATES = [
    'starting…',
    'waiting for a coach',
    'answered — connecting…',
    '🎙 LIVE — coach linked',
    'link dropped — back to polling',
    'clip mode (aiortc not installed — re-run install_comms.sh)',
    'link error: [Errno 111] Connection refused',
]


@pytest.mark.parametrize('state', REAL_VOICE_STATES)
def test_every_voice_state_can_actually_be_sent(state):
    assert _sendable(hdr(state))


def test_the_reported_failure_is_gone():
    """'starting…' — the exact value, and the exact position, from the
    screenshot."""
    assert hdr('starting…') == 'starting...'
    assert _sendable(hdr('starting…'))


def test_a_live_coach_link_stays_on_the_cloud():
    """The one that cost a game: the box went dark on the cloud for
    precisely as long as somebody was using it."""
    out = hdr('🎙 LIVE — coach linked')
    assert _sendable(out)
    assert 'LIVE' in out and 'coach linked' in out


# ── the strings that come from somebody else's phone ─────────────────────

@pytest.mark.parametrize('name', [
    "Erik's AirPods",          # iOS, curly apostrophe
    'JLab GO Air',
    'Erik — bench',
    'Bud №2',
    'ヘッドセット',                # nothing latin-1 survives; must not raise
])
def test_any_earpiece_name_can_be_sent(name):
    assert _sendable(hdr(name))


def test_an_apple_apostrophe_survives_as_an_apostrophe():
    """Folded, not dropped — the coach has to recognise his own bud in
    the list on the site."""
    assert hdr("Erik's AirPods") == "Erik's AirPods"


def test_a_name_that_is_entirely_unencodable_is_empty_not_an_error():
    assert hdr('ヘッドセット') == ''


def test_a_device_name_cannot_inject_a_header():
    """A Bluetooth name is somebody else's text arriving in an HTTP
    header. CR/LF in there is header injection, not a typo."""
    out = hdr('bud\r\nX-Api-Key: stolen')
    assert '\r' not in out and '\n' not in out
    assert _sendable(out)


# ── the helper's own edges ───────────────────────────────────────────────

def test_it_never_raises_on_anything():
    for v in (None, '', 0, 12345, [], {'a': 1}, b'\xff'):
        assert _sendable(hdr(v))


def test_it_respects_the_limit():
    assert len(hdr('x' * 500, 40)) == 40


def test_it_keeps_ordinary_text_untouched():
    assert hdr('playcall-encoder') == 'playcall-encoder'
    assert hdr('192.168.1.130') == '192.168.1.130'


# ── the poll itself ──────────────────────────────────────────────────────

def test_the_poll_sends_the_sanitised_values(monkeypatch):
    seen = {}

    class _Resp:
        def read(self):
            return b'{}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.update(req.headers)
        import io
        return _FakeCtx(io.BytesIO(b'{}'))

    class _FakeCtx:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self.fh

        def __exit__(self, *a):
            return False

    monkeypatch.setitem(comms_ear.RTC_STATE, 's', '🎙 LIVE — coach linked')
    monkeypatch.setattr(comms_ear, 'bt_status', lambda: {'connected': [
        {'mac': 'AA:BB', 'name': "Erik's AirPods"}]})
    monkeypatch.setattr(comms_ear, 'ear_labels', lambda: {})
    monkeypatch.setattr(comms_ear, 'box_name', lambda: 'playcall-encoder')
    monkeypatch.setattr(comms_ear.urllib.request, 'urlopen', fake_urlopen)
    comms_ear.fetch()
    # urllib title-cases header names
    sent = {k.lower(): v for k, v in seen.items()}
    for k, v in sent.items():
        assert _sendable(v), k
    assert sent['x-pi-voice'] == 'LIVE - coach linked'
    assert sent['x-pi-ears'] == "Erik's AirPods"


def test_a_value_that_still_will_not_encode_costs_the_telemetry_not_the_calls(
        monkeypatch):
    """hdr() should make this impossible. If something ever gets through,
    the box gives up the part where it describes itself — never the part
    where it collects the coach's calls."""
    tries = []

    def fake_urlopen(req, timeout=None):
        import io
        tries.append(dict(req.headers))
        if len(tries) == 1:
            raise UnicodeEncodeError('latin-1', 'x', 0, 1, 'nope')
        return _Ctx(io.BytesIO(b'{"game": "g1"}'))

    class _Ctx:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self.fh

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(comms_ear, 'bt_status', lambda: {'connected': []})
    monkeypatch.setattr(comms_ear, 'ear_labels', lambda: {})
    monkeypatch.setattr(comms_ear, 'box_name', lambda: 'box')
    monkeypatch.setattr(comms_ear.urllib.request, 'urlopen', fake_urlopen)
    d = comms_ear.fetch()
    assert d == {'game': 'g1'}, 'the calls still arrived'
    assert len(tries) == 2
    second = {k.lower() for k in tries[1]}
    assert 'x-pi-voice' not in second, 'the retry dropped the telemetry'
    assert 'x-api-key' in second, 'but kept the credentials'
