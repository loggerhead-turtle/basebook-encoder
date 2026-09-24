"""The box sends its own recording to BaseStream — on a button.

21 Sep 2026 (Maeser): every clip landed, the Main angle on the watch
page had nothing playable. Clips are cut from the box's recording; the
watch page plays the live copy the box pushed. When the copy is missing,
the recording is the game, and this is how it gets there.
"""

import json
import time
from pathlib import Path

import pytest

from encoder import backfill, cloud_link


# ── the arithmetic ──────────────────────────────────────────────────────────

def test_holes_are_what_the_server_lacks_inside_the_window():
    cov = {'runs': 2, 'start': 1000.0, 'end': 4000.0,
           'gaps': [{'from': 2000.0, 'to': 2500.0, 'seconds': 500}]}
    assert backfill.holes(cov, 500.0, 5000.0) == [
        (500.0, 1000.0), (2000.0, 2500.0), (4000.0, 5000.0)]
    # the window clips every hole; a seam shorter than a segment is not a hole
    assert backfill.holes(cov, 1500.0, 2200.0) == [(2000.0, 2200.0)]
    assert backfill.holes(cov, 1990.0, 2001.0) == []


def test_a_server_with_nothing_lacks_the_whole_window():
    assert backfill.holes({'runs': 0}, 100.0, 700.0) == [(100.0, 700.0)]
    assert backfill.holes(None, 100.0, 700.0) == [(100.0, 700.0)]
    assert backfill.holes({'runs': 0}, 700.0, 100.0) == []


def test_pieces_are_cut_to_what_the_recording_has_in_bounded_lengths():
    gaps = [(0.0, 1500.0)]
    segs = [(100.0, 400.0), (600.0, 100.0), (900.0, 5000.0)]   # (start, dur)
    got = backfill.pieces(gaps, segs, piece_s=300)
    assert got == [(100.0, 400.0), (400.0, 500.0), (600.0, 700.0),
                   (900.0, 1200.0), (1200.0, 1500.0)]
    # nothing recorded there: nothing to send, however big the hole
    assert backfill.pieces([(0.0, 50.0)], segs, piece_s=300) == []


# ── the worker ──────────────────────────────────────────────────────────────

class _Server:
    """The stream server and MediaMTX's playback, faked."""

    def __init__(self, coverage, segments, refuse=0):
        self.coverage = coverage
        self.segments = segments
        self.posts = []
        self.fetched = []
        self.refuse = refuse
        self.sessions = 0

    def post(self, url, payload=None, headers=None, timeout=60):
        self.posts.append((url, payload if isinstance(payload, dict) else len(payload),
                           headers))
        if url.endswith('/coverage'):
            return self.coverage
        if url.endswith('/start'):
            if self.refuse:
                self.refuse -= 1
                return {'error': 'no'}
            self.sessions += 1
            return {'session': f'ls_{self.sessions}', 'run': 'r001', 'upload': True}
        return {'ok': True}

    def get(self, url, binary=False, timeout=60):
        if '/list?' in url:
            return json.dumps([{'start': backfill._iso(s), 'duration': d}
                               for s, d in self.segments])
        if '/get?' in url:
            self.fetched.append(url)
            return b'\x00' * 200_000              # "a stretch of fMP4"
        if url.endswith('/v3/paths/list'):
            return json.dumps({'items': []})      # no camera live
        raise AssertionError(url)


def _target(tmp_path, frm=1000.0, to=4000.0):
    t = {'id': 'bf1', 'ingest': 'https://live/ingest', 'token': 'tok',
         'game': 'g1', 'angle': 'main', 'from': frm, 'to': to}
    (tmp_path / 'target.json').write_text(json.dumps(t))
    return t


def _worker(tmp_path, server, cfg=None):
    slept = []
    w = backfill.Backfill(cfg_load=lambda: cfg or {'local_ingest_key': 'k'},
                          http=server.post, http_get=server.get,
                          sleep=slept.append,
                          status_file=tmp_path / 'status.json',
                          target_file=tmp_path / 'target.json',
                          live_status=lambda: {}, hw=None)
    return w, slept


def test_it_sends_the_holes_as_upload_runs_stamped_with_their_time(tmp_path):
    srv = _Server({'runs': 1, 'start': 1000.0, 'end': 2000.0, 'gaps': []},
                  segments=[(900.0, 3200.0)])
    _target(tmp_path)
    w, slept = _worker(tmp_path, srv)
    assert w.tick() is True
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'done' and st['id'] == 'bf1'
    # the server held 1000–2000; the box recorded 900–4100; the window
    # ends at 4000 → 2000–4000 in ten-minute pieces
    starts = [p for u, p, _ in srv.posts if u.endswith('/start')]
    assert [p['capture_start'] for p in starts] == [2000.0, 2600.0, 3200.0, 3800.0]
    assert all(p['upload'] is True and p['token'] == 'tok' for p in starts)
    assert st['sent_s'] == 2000 and st['total_s'] == 2000 and st['pieces'] == 4
    # every session was fed and stopped
    stops = [u for u, _, _ in srv.posts if u.endswith('/stop')]
    assert len(stops) == 4
    feeds = [u for u, _, _ in srv.posts if u.endswith('/feed')]
    assert len(feeds) == 4 and 'ls_1/feed' in feeds[0]
    assert '/get?' in srv.fetched[0] and 'format=fmp4' in srv.fetched[0]
    assert 'duration=600.0' in srv.fetched[0]
    # and it paced itself at the idle rate: 200 KB at 4 MB/s
    assert slept and abs(slept[0] - 200_000 / backfill.UPLOAD_BPS_IDLE) < 1e-6


def test_a_finished_request_is_not_run_twice(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 3000.0)])
    _target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    assert w.tick() is True
    n = len(srv.posts)
    assert w.tick() is False               # same id, already done
    assert len(srv.posts) == n
    # a new id runs again
    t = _target(tmp_path)
    t['id'] = 'bf2'
    (tmp_path / 'target.json').write_text(json.dumps(t))
    assert w.tick() is True


def test_nothing_to_send_is_done_with_a_reason(tmp_path):
    srv = _Server({'runs': 1, 'start': 900.0, 'end': 4100.0, 'gaps': []},
                  segments=[(900.0, 3200.0)])
    _target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'done' and 'already holds' in st['note']
    assert not [u for u, _, _ in srv.posts if u.endswith('/start')]
    # a box that recorded nothing says that instead
    srv2 = _Server({'runs': 0}, segments=[])
    t = _target(tmp_path); t['id'] = 'bf3'
    (tmp_path / 'target.json').write_text(json.dumps(t))
    w2, _ = _worker(tmp_path, srv2)
    w2.tick()
    assert 'no recording' in json.loads((tmp_path / 'status.json').read_text())['note']


def test_three_refusals_in_a_row_fail_the_request_with_the_reason(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 3000.0)], refuse=5)
    _target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'failed'
    assert '3 pieces failed in a row' in st['error'] and 'refused' in st['error']


def test_one_bad_piece_is_skipped_and_the_rest_still_go(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 3000.0)], refuse=1)
    _target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'done' and st['done_pieces'] == 4 and st['pieces'] == 5
    assert st['sent_s'] == 2400 and 'piece 1' in st['error']


def test_a_live_camera_slows_the_upload_to_a_crawl(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 700.0)])
    srv.get = (lambda orig: lambda url, **kw: json.dumps(
        {'items': [{'ready': True}]}) if url.endswith('/v3/paths/list')
        else orig(url, **kw))(srv.get)
    _target(tmp_path, 1000.0, 1700.0)
    w, slept = _worker(tmp_path, srv)
    w.tick()
    assert slept and abs(slept[0] - 200_000 / backfill.UPLOAD_BPS_LIVE) < 1e-6


def test_no_request_means_no_work(tmp_path):
    w, _ = _worker(tmp_path, _Server({'runs': 0}, []))
    assert w.tick() is False
    (tmp_path / 'target.json').write_text('{}')
    assert w.tick() is False


# ── the channel ─────────────────────────────────────────────────────────────

def _link(assignment, tmp_path, monkeypatch):
    monkeypatch.setenv('PLAYCALL_ENCODER_STATE', str(tmp_path))
    cfg = {'cloud': {'base_url': 'https://site', 'api_key': 'k'},
           'live_push': {'enabled': True, 'angle': 'main'},
           'youtube': {'url': '', 'key': ''}}
    link = cloud_link.CloudLink(cfg_load=lambda: cfg, cfg_save=lambda c: None,
                                runner=lambda *a, **k: None,
                                http=lambda url, headers=None, payload=None,
                                timeout=6: assignment)
    return link


REQ = {'id': 'bf9', 'ingest': 'https://live/ingest', 'token': 't', 'game': 'g',
       'angle': 'main', 'from': 1.0, 'to': 2.0}


def test_the_request_rides_the_assignment_poll_to_tmpfs(tmp_path, monkeypatch):
    link = _link({'assigned': True, 'backfill': REQ}, tmp_path, monkeypatch)
    link.poll_assignment_once()
    assert backfill.read_target() == REQ
    # gone from the site: blanked here, and read_target says so
    link2 = _link({'assigned': True, 'backfill': None}, tmp_path, monkeypatch)
    link2.poll_assignment_once()
    assert backfill.read_target() is None


def test_progress_rides_the_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv('PLAYCALL_ENCODER_STATE', str(tmp_path))
    link = _link({'assigned': False}, tmp_path, monkeypatch)
    assert link.backfill_status() is None
    backfill._atomic_json(tmp_path / 'backfill.json',
                          {'id': 'bf9', 'state': 'sending', 'sent_s': 120.4,
                           'total_s': 3600, 'pieces': 6, 'done_pieces': 1,
                           'game': 'g', 'angle': 'main', 'error': ''})
    st = link.backfill_status()
    assert st['id'] == 'bf9' and st['state'] == 'sending'
    assert st['sent_s'] == 120 and st['total_s'] == 3600
    src = Path(cloud_link.__file__).read_text()
    assert "'backfill': self.backfill_status()," in src


def test_the_worker_starts_with_the_box_and_the_version_says_so():
    main = Path(backfill.__file__).with_name('__main__.py').read_text()
    assert '_backfill.Backfill().start()' in main
    ver = Path(backfill.__file__).parent.parent / 'VERSION'
    assert tuple(int(x) for x in ver.read_text().strip().split('.')) >= (1, 2, 95)


# ── replace: a clean copy of footage the server already holds ───────────────

class _HTTPError(Exception):
    def __init__(self, code):
        super().__init__(f'HTTP {code}')
        self.code = code


def _replace_target(tmp_path, frm=1000.0, to=2200.0):
    t = _target(tmp_path, frm, to)
    t['mode'] = 'replace'
    t['id'] = 'bfaa01'
    (tmp_path / 'target.json').write_text(json.dumps(t))
    return t


def test_replace_sends_the_whole_window_re_encoded_then_commits(tmp_path, monkeypatch):
    """The server holds all of it — a fill would send nothing. A replace
    sends everything, re-encoded on the chip, tagged with the batch, and
    commits at the end so the clean copy takes over in one step."""
    srv = _Server({'runs': 1, 'start': 900.0, 'end': 4000.0, 'gaps': []},
                  segments=[(900.0, 3200.0)])
    monkeypatch.setattr(backfill, 'REPLACE_KBPS', 1)   # the fake pieces are tiny
    _replace_target(tmp_path)
    encoded = []
    real = srv.post

    def post(url, payload=None, headers=None, timeout=60):
        out = real(url, payload, headers, timeout)       # recorded either way
        if url.endswith('/replace'):
            return {'ok': True, 'seconds': 1200, 'spans': [[1000.0, 2200.0]]}
        return out
    srv.post = post
    w, _ = _worker(tmp_path, srv)
    w.hw = 'hevc'
    w.transcode = lambda data, kbps, codec: encoded.append((kbps, codec)) \
        or b'\x01' * 50_000
    assert w.tick() is True
    starts = [p for u, p, _ in srv.posts if u.endswith('/start')]
    assert [p['capture_start'] for p in starts] == [1000.0, 1600.0]
    assert all(p['replace'] == 'bfaa01' and p['upload'] is True for p in starts)
    assert encoded == [(1, 'hevc'), (1, 'hevc')]
    # the re-encoded bytes are what went up, not the original
    feeds = [p for u, p, _ in srv.posts if u.endswith('/feed')]
    assert feeds == [50_000, 50_000]
    assert not [u for u, _, _ in srv.posts if u.endswith('/coverage')]
    commits = [p for u, p, _ in srv.posts if u.endswith('/replace')]
    assert commits == [{'token': 'tok', 'batch': 'bfaa01'}]
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'done' and st['mode'] == 'replace'
    assert 'replaced 20 min' in st['note']


def test_a_recording_already_light_is_sent_as_it_is(tmp_path):
    """200 KB for ten minutes is far under 5 Mb/s: nothing to gain."""
    srv = _Server({'runs': 0}, segments=[(1000.0, 1200.0)])
    _replace_target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.hw = 'hevc'
    w.transcode = lambda *a: (_ for _ in ()).throw(AssertionError('no re-encode'))
    w.tick()
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'done'


def test_a_box_without_a_hardware_encoder_sends_full_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, 'REPLACE_KBPS', 1)
    srv = _Server({'runs': 0}, segments=[(1000.0, 1200.0)])
    _replace_target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.hw = None
    w.transcode = lambda *a: (_ for _ in ()).throw(AssertionError('no chip'))
    w._hw = lambda: None
    w.tick()
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'done'


def test_a_failed_re_encode_sends_the_original_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, 'REPLACE_KBPS', 1)
    srv = _Server({'runs': 0}, segments=[(1000.0, 600.0)])
    _replace_target(tmp_path, 1000.0, 1600.0)
    w, _ = _worker(tmp_path, srv)
    w.hw = 'hevc'
    w.transcode = lambda *a: (_ for _ in ()).throw(RuntimeError('vaapi died'))
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'done' and 'sent full quality' in st['error']
    assert [p for u, p, _ in srv.posts if u.endswith('/feed')]


def test_it_waits_while_this_box_is_live_on_that_game(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 1200.0)])
    _replace_target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.live_status = lambda: {'connected': True, 'game': 'g1'}
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'waiting' and 'still live' in st['note']
    assert not srv.posts and not srv.fetched
    # a live push on ANOTHER game is not this angle
    w.live_status = lambda: {'connected': True, 'game': 'g2'}
    w._wait_until = 0
    w.tick()
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'done'


def test_a_busy_angle_is_a_wait_not_a_failure(tmp_path):
    """The server says 409 — a phone or a reconnect is live there."""
    srv = _Server({'runs': 0}, segments=[(1000.0, 1200.0)])
    real = srv.post

    def post(url, payload=None, headers=None, timeout=60):
        if url.endswith('/start'):
            raise _HTTPError(409)
        return real(url, payload, headers, timeout)
    srv.post = post
    _replace_target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'waiting' and 'after it stops' in st['note']
    # and it does not hammer: the next tick inside the wait does nothing
    n = len(srv.posts)
    assert w.tick() is False and len(srv.posts) == n


def test_a_commit_refused_as_busy_is_retried_without_resending(tmp_path):
    srv = _Server({'runs': 0}, segments=[(1000.0, 1200.0)])
    real = srv.post
    calls = {'n': 0}

    def post(url, payload=None, headers=None, timeout=60):
        if url.endswith('/replace'):
            calls['n'] += 1
            if calls['n'] == 1:
                raise _HTTPError(409)
            return {'ok': True, 'seconds': 1200}
        return real(url, payload, headers, timeout)
    srv.post = post
    _replace_target(tmp_path)
    w, _ = _worker(tmp_path, srv)
    w.tick()
    st = json.loads((tmp_path / 'status.json').read_text())
    assert st['state'] == 'waiting'
    starts = len([u for u, _, _ in srv.posts if u.endswith('/start')])
    w._wait_until = 0
    # the waiting state kept 'committing' out of the file; the worker
    # remembers the sends by resuming at the commit
    json_st = json.loads((tmp_path / 'status.json').read_text())
    json_st['state'] = 'committing'
    (tmp_path / 'status.json').write_text(json.dumps(json_st))
    w.tick()
    assert len([u for u, _, _ in srv.posts if u.endswith('/start')]) == starts
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'done'


def test_the_transcode_runs_on_the_chip_with_a_cpu_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, 'TMP_DIR', tmp_path)
    seen = []

    def runner(argv):
        seen.append(argv)
        if len(seen) == 1:
            raise RuntimeError('hw decode refused')
        Path(argv[-1]).write_bytes(b'\x02' * 4096)
    out = backfill.transcode_piece(b'\x00' * 4096, 5000, 'hevc', runner=runner)
    assert out == b'\x02' * 4096
    assert '-hwaccel' in seen[0] and '-hwaccel' not in seen[1]
    for argv in seen:
        assert argv[argv.index('-c:v') + 1] == 'hevc_vaapi'
        assert argv[argv.index('-b:v') + 1] == '5000k'
        assert '+frag_keyframe+empty_moov+default_base_moof' in argv
        assert argv[argv.index('-c:a') + 1] == 'aac'
    assert list(tmp_path.iterdir()) == []            # temp files cleaned up
