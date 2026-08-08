"""Radar LAN feed: JSON-lines framing over a real socket (a client gets
`hello` first, then exactly what the radar pushes, one object per line),
the drop-oldest/never-block slow-client policy, and the radar.py hook —
LAN messages come out right AND the cloud POSTs stay byte-for-byte what
they were before the LAN existed.

Run: python -m pytest ndi-encoder/tests/test_radar_lan.py
"""
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from encoder.radar import ALIVE_INTERVAL, RadarService  # noqa: E402
from encoder.radar_lan import QUEUE_MAX, LanServer, _Client  # noqa: E402

# the same bench lines the radar tests run on (2026-07-26 capture)
IDLE = 'ˆRD   34C             5C     869     6A             '
LIVE_ONLY = 'ˆRD   34C 586         5C 589 869     6A             '
WITH_SPIN = 'ˆRD   34C 537         5C 589 869     9A15650        '


def _connect(srv, timeout=5):
    s = socket.create_connection(('127.0.0.1', srv.port), timeout=timeout)
    return s, s.makefile('r', encoding='utf-8')


# ── framing ──────────────────────────────────────────────────────────────────

def test_client_reads_hello_then_pushed_messages_one_json_per_line():
    """The whole protocol through a real socket: hello is provably the
    first line, every push is one complete JSON object per line, and
    the field names/shapes match protocol/radar-lan.schema.json."""
    srv = LanServer(port=0, version='9.9.9',
                    gun_connected=lambda: True).start()
    try:
        sock, rd = _connect(srv)
        hello = json.loads(rd.readline())
        assert hello['type'] == 'hello'
        assert hello['version'] == '9.9.9'
        assert hello['gunConnected'] is True
        assert isinstance(hello['atMs'], int)

        srv.send_live(58.9, 1565.0, at_ms=123)
        srv.send_live(80.1, at_ms=124)          # rpm not computed yet
        srv.send_burst({'kind': 'pitch', 'peak': 58.9, 'plate': 53.7,
                        'rpm': 1565.0, 'frames': 4, 'dur': 0.3},
                       at_ms=125)
        srv.send_alive(False, at_ms=126)

        # rpm rides as an INTEGER once present (schema), null before
        assert json.loads(rd.readline()) == {
            'type': 'live', 'atMs': 123, 'velo': 58.9, 'rpm': 1565}
        assert json.loads(rd.readline()) == {
            'type': 'live', 'atMs': 124, 'velo': 80.1, 'rpm': None}
        # the burst on the wire is the doc's message set exactly — kind,
        # peak, frames, durS — not the engine dict's internal extras
        assert json.loads(rd.readline()) == {
            'type': 'burst', 'atMs': 125, 'kind': 'pitch', 'peak': 58.9,
            'frames': 4, 'durS': 0.3}
        assert json.loads(rd.readline()) == {
            'type': 'alive', 'atMs': 126, 'gunConnected': False}
        sock.close()
    finally:
        srv.stop()


def test_every_connected_client_gets_the_fan_out():
    srv = LanServer(port=0).start()
    try:
        socks = [_connect(srv) for _ in range(3)]
        for _s, rd in socks:
            assert json.loads(rd.readline())['type'] == 'hello'
        assert srv.client_count == 3
        srv.send_alive(True, at_ms=1)
        for _s, rd in socks:
            assert json.loads(rd.readline())['gunConnected'] is True
        for s, _rd in socks:
            s.close()
    finally:
        srv.stop()


# ── slow / dead clients ──────────────────────────────────────────────────────

def test_slow_client_backlog_drops_oldest_never_blocks():
    """The deque's maxlen IS the policy: a reader that stops draining
    keeps only the freshest QUEUE_MAX lines — velo is only news for a
    few seconds, so gaps beat staleness — and enqueue can never block
    the gun-side pipeline."""
    c = _Client(sock=None, addr=('test', 0))    # no writer draining it
    for i in range(QUEUE_MAX + 5):
        c.enqueue(b'%d\n' % i)                  # returns instantly
    assert len(c.q) == QUEUE_MAX
    assert c.q[0] == b'5\n'                     # the 5 oldest evicted
    assert c.q[-1] == b'%d\n' % (QUEUE_MAX + 4)


def test_dead_client_is_dropped_and_the_feed_flows_on():
    srv = LanServer(port=0).start()
    try:
        dead_sock, dead_rd = _connect(srv)
        live_sock, live_rd = _connect(srv)
        for rd in (dead_rd, live_rd):
            assert json.loads(rd.readline())['type'] == 'hello'
        dead_rd.close()                         # client vanishes mid-game
        dead_sock.close()
        # keep broadcasting until the dead socket's writes error it out
        deadline = time.monotonic() + 5
        while srv.client_count > 1 and time.monotonic() < deadline:
            srv.send_alive(True, at_ms=7)
            time.sleep(0.02)
        assert srv.client_count == 1            # dropped, not wedged
        # ...and the surviving client heard every one of those alives
        assert json.loads(live_rd.readline())['type'] == 'alive'
        live_sock.close()
    finally:
        srv.stop()


# ── the radar.py hook ────────────────────────────────────────────────────────

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


class _DeadLink:
    def _cloud(self):
        return '', ''


class _FakeLan:
    def __init__(self):
        self.lives, self.bursts, self.alives = [], [], []

    def send_live(self, velo, rpm=None, at_ms=None):
        self.lives.append((velo, rpm))

    def send_burst(self, ev, at_ms=None):
        self.bursts.append(ev)

    def send_alive(self, gun_connected=None, at_ms=None):
        self.alives.append(gun_connected)


def _bench_pitch(svc, t=100.0):
    """The bench throw through the service; returns the closing time."""
    for line in (LIVE_ONLY, WITH_SPIN, WITH_SPIN):
        svc.handle_line(line, t=t)
        t += 0.4                    # past the live-push throttle window
    svc.handle_line(IDLE, t=t + 2.5)
    return t + 2.5


def test_hook_feeds_lan_live_and_burst_without_altering_cloud_posts():
    """The invariant the whole feature hangs on: a service with a LAN
    mouth attached POSTs byte-for-byte what a LAN-less service posts —
    LAN is additive — while the LAN sees the throttled live readings
    (peak channel, spin once present) and the classified burst."""
    plain, wired = RadarService(_FakeLink()), RadarService(_FakeLink())
    lan = _FakeLan()
    wired.lan = lan
    _bench_pitch(plain)
    _bench_pitch(wired)
    assert wired.link.posts == plain.link.posts     # cloud untouched
    assert lan.lives == [(58.9, None), (58.9, 1565.0)]
    assert len(lan.bursts) == 1
    assert lan.bursts[0]['kind'] == 'pitch'
    assert lan.bursts[0]['peak'] == 58.9


def test_lan_failure_never_costs_the_cloud_a_post():
    """A LAN mouth that throws (bug, port storm, whatever) is contained
    in the hook — the cloud shadow must not lose a single POST."""
    class _Explodes:
        def send_live(self, *a, **k):
            raise RuntimeError('lan boom')
        send_burst = send_alive = send_live

    plain, wired = RadarService(_FakeLink()), RadarService(_FakeLink())
    wired.lan = _Explodes()
    _bench_pitch(plain)
    _bench_pitch(wired)
    assert wired.link.posts == plain.link.posts


def test_lan_alive_flows_at_the_cloud_cadence_while_idle():
    """Keepalives ride the LAN at the same ALIVE_INTERVAL as the cloud
    path, carrying the service's gun-connected state — and a reading
    counts as proof of life, so alive only flows while the gun is
    quiet (the "gun asleep vs box gone" signal)."""
    svc = RadarService(_DeadLink())         # unpaired box: LAN-only
    lan = _FakeLan()
    svc.lan = lan
    svc.connected = True
    svc.handle_line(IDLE, t=1000.0)                     # first idle → alive
    svc.handle_line(IDLE, t=1000.1)                     # within cadence
    svc.handle_line(IDLE, t=1000.0 + ALIVE_INTERVAL)    # cadence elapsed
    assert lan.alives == [True, True]
    svc.handle_line(WITH_SPIN, t=1000.0 + ALIVE_INTERVAL + 1)
    svc.handle_line(IDLE, t=1000.0 + ALIVE_INTERVAL + 2)
    assert lan.lives and lan.alives == [True, True]     # reading, no alive


def test_hook_end_to_end_over_a_real_socket():
    """Serial line in, JSON line out: parsed bench frames through
    RadarService reach a real TCP client as hello → live → burst."""
    svc = RadarService(_DeadLink())
    srv = LanServer(port=0, gun_connected=lambda: svc.connected).start()
    svc.lan = srv
    try:
        sock, rd = _connect(srv)
        assert json.loads(rd.readline())['gunConnected'] is False
        _bench_pitch(svc, t=50.0)
        live = json.loads(rd.readline())
        assert live['type'] == 'live' and live['velo'] == 58.9
        assert json.loads(rd.readline())['rpm'] == 1565
        burst = json.loads(rd.readline())
        assert burst['type'] == 'burst' and burst['kind'] == 'pitch'
        assert burst['durS'] == 0.8
        sock.close()
    finally:
        srv.stop()


def test_port_defaults_to_8791_and_env_overrides(monkeypatch):
    assert LanServer().port == 8791
    monkeypatch.setenv('RADAR_LAN_PORT', '9021')
    assert LanServer().port == 9021
    assert LanServer(port=1234).port == 1234    # explicit beats env


def test_standalone_wiring_attaches_lan_to_the_capture_service():
    """radar_standalone.build_service: the same RadarService, a LAN
    server whose hello tracks the service's gun state, and a cloud link
    that reads the same config — unstarted, so nothing binds or POSTs
    here."""
    from encoder import radar_standalone
    svc = radar_standalone.build_service(cfg_load=lambda: {}, lan_port=0)
    assert isinstance(svc, RadarService)
    assert isinstance(svc.lan, LanServer)
    svc.connected = True
    assert svc.lan.gun_connected() is True      # hello follows the gun
