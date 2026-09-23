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
                          target_file=tmp_path / 'target.json')
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
    assert ver.read_text().strip() == '1.2.94'
