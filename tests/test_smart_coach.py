"""Pocket Radar Smart Coach capture — decode, pipeline, learning.

The gun's BLE protocol is unpublished, so the module's whole design is
"learn, don't guess": these tests pin the defensive decoder, the
one-reading-one-burst pipeline, the identity learning, and — most
importantly — that the Stalker serial module is untouched by all of it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import struct

from encoder import smart_coach
from encoder.smart_coach import SmartCoachService, decode_reading


class _FakeLink:
    def __init__(self):
        self.posts = []

    def _cloud(self):
        return 'https://cloud.example', 'k'

    def _headers(self):
        return {'X-Api-Key': 'k'}

    def http(self, url, headers=None, payload=None):
        self.posts.append((url, payload))
        return {'ok': True}


# ── the defensive decoder ────────────────────────────────────────────────────

def test_decodes_ascii_speeds():
    assert decode_reading(b'87') == (87.0, 'ascii')
    assert decode_reading(b'87.4') == (87.4, 'ascii')
    # an integer out of band divides by ten before giving up
    assert decode_reading(b'874') == (87.4, 'ascii_x10')
    # NUL padding (fixed-width firmware buffers) doesn't kill the parse
    assert decode_reading(b'92\x00\x00') == (92.0, 'ascii')


def test_decodes_binary_speeds():
    assert decode_reading(struct.pack('<H', 87)) == (87.0, 'u16le')
    assert decode_reading(struct.pack('<H', 874)) == (87.4, 'u16le_x10')
    # hundredths of m/s — 87.4 mph ≈ 39.07 m/s ≈ 3907
    v, how = decode_reading(struct.pack('<H', 3907))
    assert how == 'u16le_cmps' and abs(v - 87.4) < 0.2
    assert decode_reading(struct.pack('<f', 88.5)) == (88.5, 'f32le')
    assert decode_reading(bytes([76])) == (76.0, 'u8')


def test_rejects_what_is_not_a_speed():
    assert decode_reading(b'') is None
    assert decode_reading(None) is None
    assert decode_reading(b'\x00\x00') is None          # zero, every way
    assert decode_reading(b'BATT 47%') is None
    # no encoding makes 140+ mph plausible on this gun (b'200' DOES
    # decode — as 20.0 via the x10 rule, which is a real slow reading)
    assert decode_reading(b'1400') is None


def test_a_pinned_decode_tries_nothing_else():
    """The learned format is a contract: a firmware that changes it must
    go loudly unparsed, never silently misread by a luckier decoder."""
    data = struct.pack('<H', 874)
    assert decode_reading(data, want='u16le_x10') == (87.4, 'u16le_x10')
    assert decode_reading(data, want='ascii') is None
    assert decode_reading(b'87', want='u16le_x10') is None


# ── the pipeline ─────────────────────────────────────────────────────────────

CHAR = '0000fff1-0000-1000-8000-00805f9b34fb'


def test_a_reading_posts_live_velo_and_a_one_frame_pitch():
    link = _FakeLink()
    svc = SmartCoachService(link)
    ev = svc.handle_notify(CHAR, b'87.4', t=100.0)
    assert ev == {'kind': 'pitch', 'peak': 87.4, 'plate': None,
                  'rpm': None, 'frames': 1, 'dur': 0.0}
    lives = [p for _u, p in link.posts if p.get('live')]
    assert lives and lives[0]['live'] == {'velo': 87.4, 'rpm': None}
    events = [p for _u, p in link.posts if p.get('events')]
    assert events and events[-1]['events'][0]['kind'] == 'pitch'
    assert all(u.endswith('/api/encoder/radar') for u, _p in link.posts)


def test_out_of_band_readings_are_ghosts_not_pitches():
    # the lawnmower, the golf cart: in the plausible band, not the pitch
    # band — kept for analysis, never decorating the play-by-play
    svc = SmartCoachService(_FakeLink())
    assert svc.handle_notify(CHAR, b'17', t=1.0)['kind'] == 'ghost'
    assert svc.handle_notify(CHAR, b'128', t=2.0)['kind'] == 'ghost'


def test_keepalive_is_honest_about_the_gun():
    """'alive' alone must never light the pad's radar ✓ — that's the
    exact Stalker outage class (open adapter, silent gun) reproduced
    over BLE as 'connected to something, no readings yet'."""
    link = _FakeLink()
    svc = SmartCoachService(link)
    svc.connected = True
    svc.push(force_alive=True, now=50.0)
    gun = link.posts[-1][1]['gun']
    assert gun['heard_s'] is None and gun['connected'] is True
    assert gun['source'] == 'smart_coach'
    svc.handle_notify(CHAR, b'88', t=60.0)
    svc._force_alive = True
    svc.push(now=65.0)
    assert link.posts[-1][1]['gun']['heard_s'] == 5.0


def test_events_buffer_across_cloud_outages():
    link = _FakeLink()
    svc = SmartCoachService(link)
    real_http = link.http
    link.http = lambda *a, **k: (_ for _ in ()).throw(OSError('down'))
    svc.handle_notify(CHAR, b'86', t=0.0)
    assert len(svc.pending) == 1
    link.http = real_http
    svc.handle_notify(CHAR, b'87', t=20.0)
    assert not svc.pending
    events = [e for _u, p in link.posts for e in p.get('events') or []]
    assert [e['peak'] for e in events] == [86.0, 87.0]


# ── learning the gun ─────────────────────────────────────────────────────────

def test_three_consistent_readings_learn_and_persist_the_gun():
    saved = {}
    svc = SmartCoachService(_FakeLink(), cfg_load=lambda: {},
                            cfg_save=lambda c: saved.update(c))
    svc.device = 'AA:BB:CC:DD:EE:FF'
    for i, t in enumerate((1.0, 12.0, 23.0)):
        svc.handle_notify(CHAR, b'8%d' % (5 + i), t=t)
    assert svc.learned
    assert (svc.char, svc.decode) == (CHAR, 'ascii')
    rad = saved['radar']
    assert rad['smart_coach_mac'] == 'AA:BB:CC:DD:EE:FF'
    assert rad['smart_coach_char'] == CHAR
    assert rad['smart_coach_decode'] == 'ascii'


def test_an_inconsistent_decode_resets_the_streak():
    """One lucky binary payload between real ASCII readings must not
    poison the learned identity — consistency, not volume."""
    svc = SmartCoachService(_FakeLink(), cfg_load=lambda: {},
                            cfg_save=lambda c: None)
    svc.handle_notify(CHAR, b'85', t=1.0)
    svc.handle_notify(CHAR, struct.pack('<H', 874), t=2.0)   # u16le_x10
    svc.handle_notify(CHAR, b'86', t=3.0)
    assert not svc.learned and svc.char is None
    svc.handle_notify(CHAR, b'87', t=4.0)
    svc.handle_notify(CHAR, b'88', t=5.0)
    assert svc.learned and svc.decode == 'ascii'


def test_a_learned_char_mutes_the_guns_other_chatter():
    svc = SmartCoachService(_FakeLink())
    svc.char, svc.decode = CHAR, 'ascii'
    # battery/button notifications on other characteristics: ignored
    # entirely, not counted as decode failures
    assert svc.handle_notify('other-uuid', b'47', t=1.0) is None
    assert svc.unparsed == 0
    assert svc.handle_notify(CHAR, b'91', t=2.0)['kind'] == 'pitch'


def test_a_config_that_cannot_save_never_blocks_capture():
    def boom(_c):
        raise OSError('read-only /etc')
    svc = SmartCoachService(_FakeLink(), cfg_load=lambda: {}, cfg_save=boom)
    for t in (1.0, 2.0, 3.0, 4.0):
        assert svc.handle_notify(CHAR, b'88', t=t)
    assert not svc.learned and svc.readings == 4


# ── coexistence guarantees ───────────────────────────────────────────────────

def test_module_imports_without_bleak():
    """bleak is optional exactly like pyserial: the import lives inside
    loop(), so a box without it still boots everything else."""
    src = open(smart_coach.__file__.rstrip('c')).read()
    head = src.split('class SmartCoachService')[0]
    assert 'import bleak' not in head
    assert 'import bleak' in src        # ...but only inside loop()


def test_auto_mode_only_connects_to_something_named_pocket_radar():
    class D:
        def __init__(self, name, address):
            self.name, self.address = name, address
    svc = SmartCoachService(_FakeLink())
    rad = {}
    assert svc._match(D('Pocket Radar', 'AA:00:00:00:00:01'), rad)
    assert svc._match(D('SR1100', 'AA:00:00:00:00:02'), rad)
    assert not svc._match(D('JBL Flip 6', 'AA:00:00:00:00:03'), rad)
    assert not svc._match(D(None, 'AA:00:00:00:00:04'), rad)
    # a pinned MAC is exact and ignores the name entirely
    rad = {'smart_coach_mac': 'aa:00:00:00:00:03'}
    assert svc._match(D('JBL Flip 6', 'AA:00:00:00:00:03'), rad)
    assert not svc._match(D('Pocket Radar', 'AA:00:00:00:00:01'), rad)


def test_the_stalker_module_is_untouched():
    """The whole point of a separate service: the serial pipeline's
    tunables are IMPORTED by the BLE module, never redefined, and
    radar.py knows nothing about smart_coach."""
    import encoder.radar as radar
    assert smart_coach.BAND is radar.BAND
    assert smart_coach.POST_PATH is radar.POST_PATH
    src = open(radar.__file__.rstrip('c')).read()
    assert 'smart_coach' not in src and 'bleak' not in src


def test_heartbeat_and_health_carry_the_ble_gun():
    svc = SmartCoachService(_FakeLink())
    h = svc.health()
    assert h['connected'] is False and h['heard_s'] is None
    svc.handle_notify(CHAR, b'89', t=1.0)
    assert svc.health()['readings'] == 1
    # the heartbeat ships it under its own key, beside — never inside —
    # the Stalker's
    import encoder.cloud_link as cl
    src = open(cl.__file__.rstrip('c')).read()
    assert "'ble_radar'" in src and 'ble_radar_health' in src
