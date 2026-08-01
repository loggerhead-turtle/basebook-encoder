"""Stalker radar capture: the frame parser (against real lines from the
owner's bench captures), the burst engine's pitch/throw/ghost calls, and
the service's push batching — no serial port, no sockets.

Run: python -m pytest ndi-encoder/tests/test_radar.py
"""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from encoder.radar import (BurstEngine, RadarService,  # noqa: E402
                           parse_frame)


# real lines from the 2026-07-26 bench capture (EA mode @ 19200)
IDLE = 'ˆRD   34C             5C     869     6A             '
LIVE_ONLY = 'ˆRD   34C 586         5C 589 869     6A             '
WITH_SPIN = 'ˆRD   34C 537         5C 589 869     9A15650        '


def test_parse_multivalue_frames_match_the_gun_display():
    f = parse_frame(WITH_SPIN)
    assert f == {'live': 53.7, 'peak': 58.9, 'rpm': 1565.0, 'alive': True}
    f = parse_frame(LIVE_ONLY)
    assert f == {'live': 58.6, 'peak': 58.9, 'rpm': None, 'alive': True}
    # idle keepalive: alive, no values — the gun-connected heartbeat
    f = parse_frame(IDLE)
    assert f == {'live': None, 'peak': None, 'rpm': None, 'alive': True}


def test_parse_format_a_fallback():
    assert parse_frame(' 80.1')['live'] == 80.1
    assert parse_frame(' 54.4 ')['live'] == 54.4
    # format A idle line = spaces and a bare decimal point
    assert parse_frame('   . ') == {'live': None, 'peak': None,
                                    'rpm': None, 'alive': True}
    assert parse_frame('garbage &&&') is None
    assert parse_frame('') is None


def test_burst_engine_classifies_a_pitch():
    eng = BurstEngine()
    t = 0.0
    # the bench throw: live decel 58.6→53.7, peak 58.9, spin arrives late
    for line in (LIVE_ONLY, LIVE_ONLY, WITH_SPIN, WITH_SPIN):
        assert eng.feed(parse_frame(line), t=t) is None
        t += 0.1
    # quiet gap closes the burst on the next idle frame
    ev = eng.feed(parse_frame(IDLE), t=t + 2.0)
    assert ev['kind'] == 'pitch'
    assert ev['peak'] == 58.9 and ev['plate'] == 53.7
    assert ev['rpm'] == 1565.0 and ev['frames'] == 4


def test_burst_engine_calls_long_tracks_throws():
    eng = BurstEngine()
    t = 0.0
    for _ in range(30):                      # 3 seconds in the beam
        eng.feed(parse_frame(LIVE_ONLY), t=t)
        t += 0.1
    ev = eng.feed(parse_frame(IDLE), t=t + 2.0)
    assert ev['kind'] == 'throw'             # a CF throw home, not a pitch


def test_burst_engine_calls_ghosts():
    eng = BurstEngine()
    # two frames only — below MIN_FRAMES
    eng.feed({'live': 62.0, 'peak': None, 'rpm': None, 'alive': True}, t=0)
    eng.feed({'live': 62.1, 'peak': None, 'rpm': None, 'alive': True}, t=0.1)
    assert eng.feed(parse_frame(IDLE), t=3.0)['kind'] == 'ghost'
    # out-of-band crawl (the lawnmower)
    for i in range(6):
        eng.feed({'live': 6.0, 'peak': None, 'rpm': None, 'alive': True},
                 t=10 + i * 0.1)
    assert eng.feed(parse_frame(IDLE), t=15.0)['kind'] == 'ghost'


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


def test_service_pushes_live_then_batches_the_event():
    link = _FakeLink()
    svc = RadarService(link)
    t = 100.0
    for line in (LIVE_ONLY, WITH_SPIN, WITH_SPIN):
        svc.handle_line(line, t=t)
        t += 0.4                    # past the live-push throttle window
    # live velo went out (peak channel) with the spin once present
    lives = [p for _u, p in link.posts if p.get('live')]
    assert lives and lives[0]['live']['velo'] == 58.9
    assert any(p['live'].get('rpm') == 1565.0 for p in lives)
    # the burst closes on the idle frame after the gap → event posted
    svc.handle_line(IDLE, t=t + 2.5)
    events = [p for _u, p in link.posts if p.get('events')]
    assert events and events[-1]['events'][0]['kind'] == 'pitch'
    assert events[-1]['events'][0]['peak'] == 58.9
    assert all(u.endswith('/api/encoder/radar') for u, _p in link.posts)


def test_service_buffers_events_across_cloud_outages():
    link = _FakeLink()
    svc = RadarService(link)
    real_http = link.http
    link.http = lambda *a, **k: (_ for _ in ()).throw(OSError('down'))
    t = 0.0
    for line in (LIVE_ONLY, WITH_SPIN, WITH_SPIN):
        svc.handle_line(line, t=t)
        t += 0.1
    svc.handle_line(IDLE, t=t + 2.5)         # burst closes while cloud down
    assert len(svc.pending) == 1
    link.http = real_http                     # cloud back
    svc.handle_line(WITH_SPIN, t=t + 10)
    assert not svc.pending                    # buffered event delivered
    events = [p for _u, p in link.posts if p.get('events')]
    assert events and events[0]['events'][0]['peak'] == 58.9
