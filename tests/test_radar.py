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


# the field gun (2026-08-07 support bundle): tag suffix A, no constant
# field, frames end with a bare \r so serial reads glue them together
FIELD_IDLE = '\x88RD   34A             5A             6A             '
FIELD_LIVE = '\x88RD   34A 586         5A 589         6A             '
FIELD_SPIN = '\x88RD   34A 537         5A 589         9A15650        '
FIELD_CHUNK = FIELD_IDLE + '\r' + FIELD_IDLE[:30]


def test_parse_a_suffix_frames_from_the_field_gun():
    """The gun that read as "no radar" at the field: tags 34A/5A, no
    constant field. The 34C/5C-only pattern rejected every frame."""
    f = parse_frame(FIELD_IDLE)
    assert f == {'live': None, 'peak': None, 'rpm': None, 'alive': True}
    f = parse_frame(FIELD_LIVE)
    assert f == {'live': 58.6, 'peak': 58.9, 'rpm': None, 'alive': True}
    f = parse_frame(FIELD_SPIN)
    assert f == {'live': 53.7, 'peak': 58.9, 'rpm': 1565.0, 'alive': True}
    # the C-mode idle's LONE number stays a constant, never a phantom
    # 86.9 peak — disambiguated by the empty 34 field
    assert parse_frame(IDLE)['peak'] is None


def test_handle_line_splits_cr_joined_frames():
    svc = RadarService(link=_FakeLink())
    svc.handle_line(FIELD_LIVE + '\r' + FIELD_SPIN + '\r', t=1.0)
    assert svc.frames_parsed == 2
    assert svc.lines_seen == 2
    # a torn tail segment counts as a line but not a frame
    svc.handle_line(FIELD_CHUNK, t=1.1)
    assert svc.frames_parsed == 3
    assert svc.unparsed == 1


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


class _FakePort:
    SCRIPT = {}
    OPEN = {}

    def __init__(self, port, baud, timeout=0, write_timeout=None):
        self.port = port
        self.chunks = list(_FakePort.SCRIPT.get(port, []))
        self.written = []
        _FakePort.OPEN[port] = self

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n):
        return self.chunks.pop(0) if self.chunks else b''

    def write(self, b):
        self.written.append(b)

    def close(self):
        pass


def test_loop_listens_to_every_adapter_at_once(monkeypatch):
    """The gun SLEEPS between pitches, so the old rotate-after-60s
    design parked the service on the LED display board half of every
    game — a pitch landed while we listened to a screen ("a velo popped
    up, then nothing", field report). Every adapter is read at once
    now: the port that parses claims the gun, and its raw bytes feed
    the display board."""
    import sys
    import types
    from encoder import radar as radar_mod
    gun_p = '/dev/serial/by-id/usb-FTDI_GUN'
    disp_p = '/dev/serial/by-id/usb-FTDI_DISPLAY'
    _FakePort.SCRIPT = {gun_p: [FIELD_LIVE.encode() + b'\r'], disp_p: []}
    _FakePort.OPEN = {}
    monkeypatch.setitem(sys.modules, 'serial',
                        types.SimpleNamespace(Serial=_FakePort))
    monkeypatch.setattr(radar_mod, 'find_ports',
                        lambda cfg=None: [gun_p, disp_p])
    svc = RadarService(_FakeLink())
    orig_handle = svc.handle_line

    def _handle(line, t=None):
        ev = orig_handle(line, t=t)
        svc.running = False                 # one frame is the test
        return ev
    svc.handle_line = _handle
    sleeps = {'n': 0}

    def _sleep(s):                          # safety: never hang the suite
        sleeps['n'] += 1
        if sleeps['n'] > 100:
            svc.running = False
    monkeypatch.setattr(radar_mod.time, 'sleep', _sleep)
    svc.loop()
    assert svc.frames_parsed == 1           # the sleeping gun was HEARD
    assert svc.port == gun_p                # and identified as the gun
    # the board is DRIVEN in its own display protocol — a bare
    # right-aligned speed — never the gun's raw multi-tag stream
    # (verbatim EA frames rendered as garbage fragments, field report)
    disp = _FakePort.OPEN.get(disp_p)
    assert disp and disp.written == [b' 58.6\r']


def test_display_line_speaks_the_board_protocol():
    """Value frames become ' 57.8\\r'; keepalives are not display
    traffic (the board holds its last number); live outranks peak."""
    from encoder.radar import RadarService
    assert RadarService.display_line(
        {'live': 57.8, 'peak': 58.9}) == b' 57.8\r'
    assert RadarService.display_line(
        {'live': None, 'peak': 58.9}) == b' 58.9\r'
    assert RadarService.display_line(
        {'live': None, 'peak': None, 'rpm': None, 'alive': True}) is None
    assert RadarService.display_line(None) is None
    assert RadarService.display_line({'live': 104.2})[0:6] == b'104.2\r'


# ── observability: the journal must be able to answer "why no velo" ──────────

class _DeadLink:
    def _cloud(self):
        return '', ''


def test_counters_track_lines_frames_and_unparsed():
    from encoder.radar import RadarService
    svc = RadarService(_DeadLink())
    svc.handle_line('RD   34C 537         5C 589 869     9A15650', t=1.0)
    svc.handle_line(' 80.1 ', t=1.1)
    svc.handle_line('!!not radar!!', t=1.2)
    assert svc.lines_seen == 3
    assert svc.frames_parsed == 2
    assert svc.unparsed == 1


def test_closed_pitch_events_land_in_the_log(caplog):
    import logging
    from encoder.radar import RadarService
    svc = RadarService(_DeadLink())
    with caplog.at_level(logging.INFO, logger='radar'):
        for i in range(6):                       # a tracked pitch...
            svc.handle_line('RD   34C 537         5C 589 869     9A15650',
                            t=1.0 + i * 0.1)
        svc.handle_line(' 62.0 ', t=4.0)         # ...closed by a later burst
    assert any('radar pitch: 58.9 mph' in r.message for r in caplog.records)
    assert any('radar rx sample' in r.message for r in caplog.records)


def test_find_ports_lists_all_candidates_and_config_prefers_one(monkeypatch,
                                                                tmp_path):
    """REGRESSION, twice. Two adapters were present and the service
    silently picked the alphabetically-first (wrong) one for a whole
    game — so the finder returns every candidate. Then a PIN did the
    same thing one layer up: radar.port used to return exactly one
    port, so a pin gone stale (fresh cables, swapped plugs) made the
    box deaf to the port the gun was actually on while the wrong port
    kept the keepalive — "radar ✓", zero velo. A pin now puts its port
    FIRST and keeps the rest visible."""
    from encoder import radar as radar_mod
    fake = {'/dev/serial/by-id/usb-FTDI_A9': None,
            '/dev/serial/by-id/usb-FTDI_BG': None}
    monkeypatch.setattr(radar_mod.glob, 'glob',
                        lambda pat: (sorted(fake) if 'by-id' in pat else []))
    assert radar_mod.find_ports({}) == sorted(fake)
    assert radar_mod.find_port({}) == '/dev/serial/by-id/usb-FTDI_A9'
    pinned = {'radar': {'port': '/dev/serial/by-id/usb-FTDI_BG'}}
    assert radar_mod.find_ports(pinned) == [
        '/dev/serial/by-id/usb-FTDI_BG', '/dev/serial/by-id/usb-FTDI_A9']
    assert radar_mod.find_port(pinned) == '/dev/serial/by-id/usb-FTDI_BG'
    # a stale pin (adapter replaced, by-id path gone) still lists the
    # real adapters after it instead of hiding them
    stale = {'radar': {'port': '/dev/serial/by-id/usb-FTDI_OLD'}}
    assert radar_mod.find_ports(stale) == [
        '/dev/serial/by-id/usb-FTDI_OLD',
        '/dev/serial/by-id/usb-FTDI_A9', '/dev/serial/by-id/usb-FTDI_BG']


# ── LED display board passthrough ────────────────────────────────────────────

class _FakeSerial:
    instances = []

    def __init__(self, port, baud, timeout=0, write_timeout=0.2):
        self.port, self.baud = port, baud
        self.writes = []
        self.closed = False
        self.explode = False
        _FakeSerial.instances.append(self)

    def write(self, raw):
        if self.explode:
            raise IOError('display unplugged')
        self.writes.append(raw)

    def close(self):
        self.closed = True


def test_gun_lines_forward_verbatim_to_the_display_board():
    """The second adapter on the box is the Stalker LED board for the
    fans — the gun's raw frames pass through byte-for-byte, as if the
    board were cabled straight to the gun."""
    from encoder.radar import RadarService
    _FakeSerial.instances = []
    svc = RadarService(_DeadLink())
    svc._serial_cls = _FakeSerial
    svc.port = '/dev/gun'
    raw = b'RD   34C 537         5C 589 869     9A15650\r\n'
    svc.forward_display(raw, '/dev/display')
    svc.forward_display(raw, '/dev/display')
    assert len(_FakeSerial.instances) == 1          # opened once, reused
    assert _FakeSerial.instances[0].port == '/dev/display'
    assert _FakeSerial.instances[0].writes == [raw, raw]


def test_display_board_drops_frames_when_behind_never_queues():
    """A backlog replayed through the board strobed it with stale
    numbers, and blocked writes chopped frames mid-byte — the board's
    parser desynced into garbage segments ("8 / JN / H" flicker, field
    report). A frame the board's UART has no room for is dropped WHOLE;
    the board only ever needs the latest reading."""
    from encoder.radar import RadarService
    _FakeSerial.instances = []
    svc = RadarService(_DeadLink())
    svc._serial_cls = _FakeSerial
    svc.forward_display(b'a\r', '/dev/display')
    disp = _FakeSerial.instances[0]
    disp.out_waiting = 9999                 # the board is far behind
    svc.forward_display(b'b\r', '/dev/display')
    assert disp.writes == [b'a\r']          # dropped whole — no queue
    disp.out_waiting = 0
    svc.forward_display(b'c\r', '/dev/display')
    assert disp.writes == [b'a\r', b'c\r']  # caught up — flowing again


def test_loop_discards_a_stale_backlog(monkeypatch):
    """2,000 frames draining in one read is HISTORY from a stall, not a
    pitch — the field log shows exactly that flood fabricating a
    compressed phantom burst. Only the newest few frames of a giant
    drain are parsed."""
    import sys
    import types
    from encoder import radar as radar_mod
    gun_p = '/dev/serial/by-id/usb-FTDI_GUN'
    _FakePort.SCRIPT = {gun_p: [(FIELD_LIVE + '\r').encode() * 100]}
    _FakePort.OPEN = {}
    monkeypatch.setitem(sys.modules, 'serial',
                        types.SimpleNamespace(Serial=_FakePort))
    monkeypatch.setattr(radar_mod, 'find_ports', lambda cfg=None: [gun_p])
    svc = RadarService(_FakeLink())
    sleeps = {'n': 0}

    def _sleep(s):
        sleeps['n'] += 1
        if sleeps['n'] > 3:
            svc.running = False
    monkeypatch.setattr(radar_mod.time, 'sleep', _sleep)
    svc.loop()
    assert svc.frames_parsed == 5           # the newest 5, not all 100


def test_a_wrong_pin_releases_to_the_port_streaming_gun_frames(monkeypatch):
    """THE ✓-but-no-velo day: radar.port and radar.display_port both
    pinned, then the two identical cables landed on swapped adapters.
    The pinned "gun" port (really the board) opened fine, keepalives
    kept the pad's "radar ✓" lit, and the gun streamed into a port the
    old code never opened at all. Now the pin only sorts first: RD
    frames on the other adapter take the claim, capture flows, and the
    board is driven on the remaining port — with the override flagged
    in health so the config gets fixed instead of rediscovered."""
    import sys
    import types
    from encoder import radar as radar_mod
    board_p = '/dev/serial/by-id/usb-FTDI_BOARD'   # pinned as the "gun"
    gun_p = '/dev/serial/by-id/usb-FTDI_GUN'       # pinned as the "board"
    frames = (FIELD_LIVE + '\r' + FIELD_LIVE + '\r' + FIELD_LIVE + '\r'
              + FIELD_SPIN + '\r').encode()
    _FakePort.SCRIPT = {board_p: [], gun_p: [frames]}
    _FakePort.OPEN = {}
    monkeypatch.setitem(sys.modules, 'serial',
                        types.SimpleNamespace(Serial=_FakePort))
    monkeypatch.setattr(radar_mod, 'find_ports',
                        lambda cfg=None: [board_p, gun_p])
    cfg = {'radar': {'port': board_p, 'display_port': gun_p}}
    svc = RadarService(_FakeLink(), cfg_load=lambda: cfg)
    sleeps = {'n': 0}

    def _sleep(s):
        sleeps['n'] += 1
        if sleeps['n'] > 3:
            svc.running = False
    monkeypatch.setattr(radar_mod.time, 'sleep', _sleep)
    svc.loop()
    assert svc.port == gun_p                # the claim moved to the gun
    assert svc.pin_overridden is True       # and said so in health
    assert svc.health()['pin_overridden'] is True
    # the first two frames were spent as takeover evidence; from the
    # third on, capture flows
    assert svc.frames_parsed == 2
    # the display pin names the gun's own port — the board is driven on
    # the OTHER adapter (where it actually is), never the gun itself
    disp = _FakePort.OPEN.get(board_p)
    assert disp and disp.written == [b' 58.6\r', b' 53.7\r']


def test_board_echo_of_our_own_speeds_never_steals_the_claim(monkeypatch):
    """The speeds we write to the board are format-A lines — exactly
    what the parser accepts. A board (or a looped-back cable) echoing
    them must not count as gun evidence, or the claim would chase its
    own output. Only multi-tag RD frames move the claim."""
    import sys
    import types
    from encoder import radar as radar_mod
    gun_p = '/dev/serial/by-id/usb-FTDI_GUN'
    board_p = '/dev/serial/by-id/usb-FTDI_BOARD'
    echoes = b' 58.6\r' * 5
    _FakePort.SCRIPT = {gun_p: [(FIELD_LIVE + '\r' + FIELD_SPIN
                                 + '\r').encode()],
                        board_p: [echoes]}
    _FakePort.OPEN = {}
    monkeypatch.setitem(sys.modules, 'serial',
                        types.SimpleNamespace(Serial=_FakePort))
    monkeypatch.setattr(radar_mod, 'find_ports',
                        lambda cfg=None: [gun_p, board_p])
    cfg = {'radar': {'port': gun_p, 'display_port': board_p}}
    svc = RadarService(_FakeLink(), cfg_load=lambda: cfg)
    sleeps = {'n': 0}

    def _sleep(s):
        sleeps['n'] += 1
        if sleeps['n'] > 3:
            svc.running = False
    monkeypatch.setattr(radar_mod.time, 'sleep', _sleep)
    svc.loop()
    assert svc.port == gun_p                # the echo changed nothing
    assert svc.pin_overridden is False
    assert svc.frames_parsed == 2           # only the gun's frames


def test_keepalive_reports_whether_the_gun_was_heard():
    """'alive' proves the service has an open adapter — with the LED
    board cabled in, that is always true. The keepalive now carries how
    long since a line PARSED, so the cloud can tell a talking gun from
    a lit heartbeat on the wrong cable."""
    link = _FakeLink()
    svc = RadarService(link)
    svc.push(force_alive=True, now=50.0)
    assert link.posts[-1][1]['gun']['heard_s'] is None   # never heard
    svc.handle_line(FIELD_LIVE, t=100.0)
    svc.push(force_alive=True, now=104.5)
    assert link.posts[-1][1]['gun']['heard_s'] == 4.5


def test_health_reports_gun_heard_and_pin_override():
    svc = RadarService(_DeadLink())
    h = svc.health()
    assert h['gun_heard_s'] is None
    assert h['pin_overridden'] is False


def test_display_board_vanishing_never_touches_capture():
    from encoder.radar import RadarService
    _FakeSerial.instances = []
    svc = RadarService(_DeadLink())
    svc._serial_cls = _FakeSerial
    svc.port = '/dev/gun'
    svc.forward_display(b'x', '/dev/display')
    _FakeSerial.instances[0].explode = True
    svc.forward_display(b'y', '/dev/display')       # raises inside → caught
    assert svc._disp is None                        # handle dropped
    svc.forward_display(b'z', '/dev/display')       # and re-opened after
    assert len(_FakeSerial.instances) == 2
    # no target / no serial class → clean no-ops
    svc.forward_display(b'q', None)
    svc._serial_cls = None
    svc.forward_display(b'q', '/dev/display')


def test_peak_and_constant_glued_together_still_parse():
    """FIELD REPORT: the gun's constant field reached four digits and
    ran into the peak with no separator — 'RD 34C 824  5G 8571024  6A'
    is live 82.4 + peak 85.7 + const 1024. The whole line used to fail,
    so a live gun logged 2000 unparsed lines of 2485, the pad saw only
    the rare narrow frame, and the LED board — which is fed only from
    PARSED frames — stayed dark all game."""
    f = parse_frame('RD   34C 824         5G 8571024     6A             ')
    assert f == {'live': 82.4, 'peak': 85.7, 'rpm': None, 'alive': True}
    # the well-spaced bench format is untouched
    assert parse_frame('RD   34C 537         5C 589 869     9A15650') == {
        'live': 53.7, 'peak': 58.9, 'rpm': 1565.0, 'alive': True}


# ── constant-on guns: the failure that ate a game's velocities ─────────────
#
# FIELD REPORT. 66 pitches charted, 22,500 frames parsed at 100%, the
# velocity tile correct all night — and SIX burst events reached the
# cloud, every one of them classified 'throw'. The play-by-play had no
# speeds on it at all.
#
# A Stalker in constant-on mode streams a frame many times a second
# forever. Between pitches it reports live 0.0 while still LATCHING the
# previous peak and rpm, so an idle frame arrives carrying three
# non-None numbers. The old boundary test asked only "is any field
# present", which is true of every frame the gun will ever send — so no
# burst ever closed, and each one ran until the gun genuinely went
# quiet: minutes long, past PITCH_MAX_DUR, filed as a throw.
#
# The live tile kept working throughout because it is pushed from a
# different field, which is exactly why nobody noticed until the game
# was over and the readings were gone.

def _stalker_idle(peak=0.0, rpm=None):
    """What the gun sends when nothing is moving: zero live, latched
    peak and spin from the last pitch."""
    return {'live': 0.0, 'peak': peak, 'rpm': rpm, 'alive': True}


def _stalker_ball(mph, rpm=2100.0):
    return {'live': mph, 'peak': mph, 'rpm': rpm, 'alive': True}


def _stream(engine, t0, pitches, idle_s=6.0, hz=20.0):
    """Feed a constant-on stream: idle frames, a short ball, idle again."""
    out, t = [], t0
    step = 1.0 / hz
    latched_peak, latched_rpm = 0.0, None
    for mph, rpm in pitches:
        for _ in range(int(idle_s * hz)):
            ev = engine.feed(_stalker_idle(latched_peak, latched_rpm), t)
            if ev:
                out.append(ev)
            t += step
        for _ in range(10):                     # ~0.5 s in the beam
            ev = engine.feed(_stalker_ball(mph, rpm), t)
            if ev:
                out.append(ev)
            t += step
        latched_peak, latched_rpm = mph, rpm
    for _ in range(int(idle_s * hz)):
        ev = engine.feed(_stalker_idle(latched_peak, latched_rpm), t)
        if ev:
            out.append(ev)
        t += step
    ev = engine.flush(t + 5)
    if ev:
        out.append(ev)
    return out


def test_a_constant_on_gun_yields_one_burst_per_pitch():
    from encoder.radar import BurstEngine
    thrown = [(78.5, 2100.0), (71.2, 2450.0), (80.1, 1980.0),
              (69.9, 2510.0), (77.4, 2050.0)]
    evs = _stream(BurstEngine(), 1000.0, thrown)
    assert len(evs) == len(thrown), \
        f'expected one burst per pitch, got {len(evs)}'
    assert [e['kind'] for e in evs] == ['pitch'] * len(thrown)
    assert [e['peak'] for e in evs] == [mph for mph, _ in thrown]
    assert [e['rpm'] for e in evs] == [rpm for _, rpm in thrown]


def test_idle_frames_never_hold_a_burst_open():
    """The specific regression: latched peak/rpm on a zero-live frame is
    the gun saying nothing is happening, not a reading."""
    from encoder.radar import BurstEngine
    eng = BurstEngine()
    t = 500.0
    for _ in range(400):                        # 20 s of pure idle
        assert eng.feed(_stalker_idle(78.5, 2100.0), t) is None
        t += 0.05
    assert eng.flush(t + 5) is None, 'idle alone produced a burst'


def test_a_long_burst_is_still_a_throw():
    """The duration rule has to keep working — a ball tracked across the
    infield is not a pitch."""
    from encoder.radar import BurstEngine
    eng = BurstEngine()
    t, out = 0.0, []
    for _ in range(80):                         # 4 s in the beam
        ev = eng.feed(_stalker_ball(64.0), t)
        if ev:
            out.append(ev)
        t += 0.05
    out.append(eng.flush(t + 5))
    assert [e['kind'] for e in out if e] == ['throw']


def test_the_plate_reading_is_the_last_live_speed_not_a_zero():
    """`plate` used to be able to come back 0.0, because idle frames were
    inside the burst and the last one won."""
    from encoder.radar import BurstEngine
    evs = _stream(BurstEngine(), 10.0, [(75.0, 2000.0)])
    assert len(evs) == 1
    assert evs[0]['plate'] == 75.0


# ── a gun on the far end of Bluetooth ────────────────────────────────────────
# An rfcomm binding is an ordinary tty — it just lives nowhere near
# /dev/serial/by-id, so the scan above could never see one.

def test_a_bluetooth_gun_is_found_like_any_other(monkeypatch):
    from encoder import radar
    monkeypatch.setattr(radar.glob, 'glob',
                        lambda pat: ['/dev/rfcomm0'] if 'rfcomm' in pat else [])
    assert radar.find_ports({}) == ['/dev/rfcomm0']


def test_a_cabled_gun_and_a_bluetooth_one_can_both_be_present(monkeypatch):
    from encoder import radar

    def fake(pat):
        if 'by-id' in pat:
            return ['/dev/serial/by-id/usb-FTDI-if00-port0']
        return ['/dev/rfcomm0'] if 'rfcomm' in pat else []
    monkeypatch.setattr(radar.glob, 'glob', fake)
    assert radar.find_ports({}) == ['/dev/serial/by-id/usb-FTDI-if00-port0',
                                    '/dev/rfcomm0']


def test_a_bluetooth_port_can_be_pinned_like_a_cabled_one(monkeypatch):
    from encoder import radar

    def fake(pat):
        if 'by-id' in pat:
            return ['/dev/serial/by-id/usb-FTDI-if00-port0']
        return ['/dev/rfcomm0'] if 'rfcomm' in pat else []
    monkeypatch.setattr(radar.glob, 'glob', fake)
    assert radar.find_ports({'radar': {'port': '/dev/rfcomm0'}})[0] \
        == '/dev/rfcomm0'


def test_nothing_plugged_in_is_an_empty_list(monkeypatch):
    from encoder import radar
    monkeypatch.setattr(radar.glob, 'glob', lambda pat: [])
    assert radar.find_ports({}) == []


def test_a_pin_whose_adapter_is_gone_says_so_once(monkeypatch, caplog):
    """It still lists the pin first (the loop decides what really talks
    like a gun) — but silently retrying a path that cannot exist is how
    a box spends a night logging the same line every five seconds."""
    import logging
    from encoder import radar
    monkeypatch.setattr(radar.glob, 'glob', lambda pat: (
        ['/dev/serial/by-id/usb-REAL-if00-port0'] if 'by-id' in pat else []))
    with caplog.at_level(logging.WARNING, logger='radar'):
        ports = radar.find_ports({'radar': {'port': '/dev/serial/by-id/GONE'}})
    assert '/dev/serial/by-id/usb-REAL-if00-port0' in ports
    assert 'is not there' in caplog.text


# ── the shipped Bluetooth plumbing ───────────────────────────────────────────

def _repo(*parts):
    import os
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), *parts)


def test_the_bt_binder_is_quiet_when_no_gun_is_configured():
    """Every box installs this unit; a cabled box must never see it fail."""
    src = open(_repo('scripts', 'radar_bt_bind.sh')).read()
    assert 'bluetooth_mac' in src
    assert 'nothing to bind' in src and 'exit 0' in src


def test_the_bt_unit_binds_before_the_radar_service_looks():
    import configparser
    cp = configparser.ConfigParser(strict=False)
    cp.optionxform = str
    cp.read(_repo('systemd', 'playcall-encoder-radarbt.service'))
    assert cp.get('Unit', 'Before') == 'playcall-encoder.service'
    assert cp.get('Service', 'Type') == 'oneshot'
    # systemd ignores this key in [Service] — which already cost the
    # other four units their crash protection
    assert cp.has_option('Unit', 'StartLimitIntervalSec')
    assert not cp.has_option('Service', 'StartLimitIntervalSec')


def test_the_installer_ships_bluetooth_and_quicksync():
    sh = open(_repo('install.sh')).read()
    assert 'bluez' in sh
    assert 'playcall-encoder-radarbt.service' in sh
    assert 'x86_64' in sh and 'intel-media-va-driver' in sh
    # the render-group grant has to come AFTER the user exists, or it
    # fails silently and every hardware encode looks like a codec bug
    assert sh.index('useradd --system') < sh.index('usermod -aG')
