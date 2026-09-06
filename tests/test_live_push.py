"""🎦 Multi-View push — the box speaking the phone's ingest protocol.

  * the ticket rides down on the assignment poll and lands in tmpfs, on
    every poll (its token is re-minted) and blanked when the game ends
  * ffmpeg is copy-mode fragmented MP4 off loopback RTSP
  * a push is one /start, many /feed, one /stop — with backlog measured
    from the age of the oldest unsent chunk
  * an assignment change (game over, next game) ends the session instead
    of streaming the new game into the old game's angle

Run: python -m pytest tests/test_live_push.py
"""

import json
import os
import time
from pathlib import Path

import pytest

from encoder import cloud_link, config, live_push


# ── the ticket ───────────────────────────────────────────────────────────────

TICKET = {'ingest': 'https://live.example.org/ingest', 'token': 'tok.sig',
          'game': 'skg_1', 'angle': 'main'}


def _link(assignment, cfg=None):
    """A CloudLink whose HTTP is the given assignment response."""
    cfg = cfg or {'cloud': {'base_url': 'https://site', 'api_key': 'k'},
                  'live_push': {'enabled': True, 'angle': 'main'},
                  'youtube': {'url': '', 'key': ''}}
    seen = {}

    def http(url, headers=None, payload=None, timeout=6):
        seen['url'] = url
        return assignment
    link = cloud_link.CloudLink(cfg_load=lambda: cfg, cfg_save=lambda c: None,
                                runner=lambda *a, **k: None, http=http)
    return link, seen


def test_ticket_lands_in_tmpfs_on_every_poll():
    link, seen = _link({'assigned': True, 'live': TICKET})
    link.poll_assignment_once()
    assert live_push.read_target() == TICKET
    assert 'angle=main' in seen['url']          # the box names its own angle

    # a re-minted token must not be mistaken for "nothing changed"
    fresh = dict(TICKET, token='tok2.sig2')
    link2, _ = _link({'assigned': True, 'live': fresh})
    link2.poll_assignment_once()
    assert live_push.read_target()['token'] == 'tok2.sig2'


def test_no_game_and_switched_off_both_blank_the_ticket():
    link, _ = _link({'assigned': True, 'live': TICKET})
    link.poll_assignment_once()
    assert live_push.read_target()

    link, _ = _link({'assigned': True, 'live': None})      # game ended
    link.poll_assignment_once()
    assert live_push.read_target() is None

    off = {'cloud': {'base_url': 'https://site', 'api_key': 'k'},
           'live_push': {'enabled': False, 'angle': 'main'},
           'youtube': {'url': '', 'key': ''}}
    link, _ = _link({'assigned': True, 'live': TICKET}, cfg=off)
    link.poll_assignment_once()
    assert live_push.read_target() is None


def test_a_half_written_ticket_is_not_a_ticket():
    p = config.state_dir() / 'live_target.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ingest": "https://x/ingest"}')          # no token
    assert live_push.read_target() is None
    p.write_text('not json at all')
    assert live_push.read_target() is None


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def test_an_absurd_h264_level_is_corrected_without_re_encoding():
    """mimoLive stamps Level 5.2 — the figure for 4K120 — on a 1080p30
    stream. An Android decoder reads that as a demand it cannot meet and
    refuses the codec: MSE threw bufferAppendError on the first append,
    nothing buffered, and the angle was black on every phone while
    ffmpeg, MediaMTX, the clipper and YouTube all played it happily
    (5 Sep 2026). h264_metadata is a bitstream filter — it rewrites the
    declaration, not a single frame."""
    h264 = live_push.build_ffmpeg_cmd({'local_ingest_key': 'k'}, 'h264')
    assert h264[h264.index('-bsf:v') + 1] == 'h264_metadata=level=auto'
    assert h264[h264.index('-c') + 1] == 'copy'      # still no encoding
    # the filter is H.264's; an HEVC camera must not be handed it
    hevc = live_push.build_ffmpeg_cmd({'local_ingest_key': 'k'}, 'hevc')
    assert '-bsf:v' not in hevc
    # and nothing at all before the codec is known
    assert '-bsf:v' not in live_push.build_ffmpeg_cmd({'local_ingest_key': 'k'})


def test_ffmpeg_is_copy_mode_fragmented_mp4_off_rtsp():
    cmd = live_push.build_ffmpeg_cmd({'local_ingest_key': 'abc123'})
    assert '-c' in cmd and cmd[cmd.index('-c') + 1] == 'copy'
    assert 'rtsp://127.0.0.1:8554/live/abc123' in cmd
    assert '-rtsp_transport' in cmd                 # RTMP drops HEVC/Opus
    flags = cmd[cmd.index('-movflags') + 1]
    # the server's splitter needs a self-contained init segment first
    assert '+empty_moov' in flags and 'frag_keyframe' in flags
    assert cmd[-1] == 'pipe:1'
    assert '0:a:0?' in cmd                          # audio optional, never fatal


# ── one push ─────────────────────────────────────────────────────────────────

class FakeProc:
    """ffmpeg standing in: a fixed byte stream on stdout, then EOF."""

    def __init__(self, blocks):
        self.blocks = list(blocks)
        self.stdout = self
        self.stderr = iter(())
        self.terminated = False
        self._done = False

    def read1(self, _n):
        if self.blocks:
            return self.blocks.pop(0)
        self._done = True
        return b''

    def poll(self):
        return 0 if self._done else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _pusher(monkeypatch, blocks, replies=None, cfg=None):
    """A LivePusher wired to a fake ffmpeg and a recording fake server."""
    calls = []
    replies = replies or {}

    def http(url, payload=None, headers=None, timeout=None):
        calls.append((url, payload, headers))
        if url.endswith('/start'):
            return replies.get('start', {'session': 'ls_' + '0' * 24})
        return replies.get('feed', {'ok': True})

    p = live_push.LivePusher(
        cfg_load=lambda: (cfg or {'local_ingest_key': 'abc123'}),
        status=live_push.StatusWriter(), http=http)
    monkeypatch.setattr(p, '_probe', lambda cfg: ('hevc', 'aac'))
    monkeypatch.setattr(live_push.subprocess, 'Popen',
                        lambda *a, **k: FakeProc(blocks))
    monkeypatch.setattr(live_push, 'FLUSH_SECONDS', 0.01)
    return p, calls


def _write_ticket(t=TICKET):
    p = config.state_dir() / 'live_target.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(t))


def test_a_push_is_start_then_feeds_then_stop(monkeypatch):
    _write_ticket()
    p, calls = _pusher(monkeypatch, [b'a' * 700_000, b'b' * 700_000])
    p.run_once()
    urls = [c[0] for c in calls]
    # On 'auto' the SRT port is asked for first; this fake server has none,
    # so the chunk path takes over. That fallback IS the behaviour here.
    assert urls[0] == 'https://live.example.org/ingest/srt'
    assert urls[1] == 'https://live.example.org/ingest/start'
    assert calls[1][1]['token'] == 'tok.sig'
    assert calls[1][1]['capture_start'] > 0        # the box's clock, for PDT
    assert calls[1][1]['codec'] == 'hevc'          # what the camera sends
    feeds = [c for c in calls if c[0].endswith('/feed')]
    assert feeds and all(isinstance(c[1], bytes) for c in feeds)
    assert b''.join(c[1] for c in feeds) == b'a' * 700_000 + b'b' * 700_000
    assert all('X-Backlog-Ms' in (c[2] or {}) for c in feeds)
    assert urls[-1].endswith('/stop')
    st = live_push.status()
    assert st['connected'] is False                # the session is over


def test_nothing_to_do_is_quiet_not_an_error(monkeypatch):
    # no ticket → no ffmpeg, no session, and a reason on the settings page
    p, calls = _pusher(monkeypatch, [b'x'])
    assert p.run_once() == 0
    assert calls == []
    assert live_push.status()['reason'] == 'no live game'

    # a ticket but nobody publishing to the box → same
    _write_ticket()
    p, calls = _pusher(monkeypatch, [b'x'])
    monkeypatch.setattr(p, '_probe', lambda cfg: ('', ''))
    assert p.run_once() == 0
    assert calls == []
    assert live_push.status()['reason'] == 'no camera publishing'


def test_the_session_ends_when_the_assignment_moves_on(monkeypatch):
    _write_ticket()
    p, calls = _pusher(monkeypatch, [b'a' * 700_000] * 6)

    real_feed = p.feed
    state = {'n': 0}

    def feed(target, sid, data, backlog_ms):
        state['n'] += 1
        if state['n'] == 2:                         # the next game starts
            _write_ticket(dict(TICKET, game='skg_2', token='t2.s2'))
        return real_feed(target, sid, data, backlog_ms)
    monkeypatch.setattr(p, 'feed', feed)
    p.run_once()
    feeds = [c for c in calls if c[0].endswith('/feed')]
    assert len(feeds) == 2                          # stopped, not carried on
    assert calls[-1][0].endswith('/stop')


def test_backlog_is_the_age_of_the_oldest_unsent_chunk():
    box = live_push.Outbox()
    assert box.backlog_s() == 0.0
    box.put(b'one')
    box.put(b'two')
    born = box.q[0][0]
    assert box.backlog_s(now=born + 4.0) == pytest.approx(4.0)  # oldest, not
    #                                                       newest
    box.get()
    assert box.backlog_s(now=born + 4.0) < 4.0


def test_angle_names_are_safe_and_stable():
    assert live_push.safe_angle('Behind Plate') == 'behind-plate'
    assert live_push.safe_angle('  3B / Line  ') == '3b-line'
    assert live_push.safe_angle('') == 'main'
    assert live_push.safe_angle('../../etc/passwd') == 'etc-passwd'
    assert len(live_push.safe_angle('x' * 90)) == 24


# ── the settings card ────────────────────────────────────────────────────────

def test_settings_card_renders_and_saves(monkeypatch):
    from encoder import web
    cfg = config.load()
    cfg.update({'local_ingest_key': 'abc123',
                'cloud': {'base_url': 'https://site', 'api_key': 'k',
                          'feed_url': ''},
                'device': {'pin': '123456', 'name': 'Field box'}})
    config.save(cfg)
    restarts = []
    monkeypatch.setattr(web.system, 'systemctl',
                        lambda *a: restarts.append(a))
    monkeypatch.setattr(web, 'PIN_FAIL_DELAY', 0)
    app = web.create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    c.post('/login', data={'pin': '123456'})
    page = c.get('/').get_data(as_text=True)
    assert 'Multi-View' in page and 'name="angle"' in page
    assert 'name="transport"' in page

    r = c.post('/livepush', data={'angle': 'Third Base', 'enabled': '1',
                                  'transport': 'srt'})
    assert r.status_code == 302
    saved = config.load()['live_push']
    assert saved == {'enabled': True, 'angle': 'third-base',
                     'transport': 'srt'}

    # An unknown value must not become the stored setting: 'auto' is the
    # one that always has a working answer.
    c.post('/livepush', data={'angle': 'main', 'enabled': '1',
                              'transport': 'carrier-pigeon'})
    assert config.load()['live_push']['transport'] == 'auto'
    assert ('restart', 'playcall-encoder-live') in restarts

    c.post('/livepush', data={'angle': 'main'})       # box unchecked
    assert config.load()['live_push']['enabled'] is False


def test_the_card_reports_what_the_leg_is_doing():
    from encoder import web
    live_push.StatusWriter().write(True, angle='main', kbps=4200,
                                   backlog_ms=5000)
    view = web.live_push_view({'live_push': {'enabled': True,
                                             'angle': 'main'}})
    assert "Sending as 'main'" in view['status']
    assert '4200 kbps' in view['status'] and '5s behind' in view['status']
    live_push.StatusWriter().write(False, reason='no live game')
    assert web.live_push_view({})['status'] == 'no live game'


# ── the update path ──────────────────────────────────────────────────────────

def test_self_update_enables_a_brand_new_unit(tmp_path, monkeypatch):
    """playcall-encoder-live did not exist when older boxes were installed,
    so the one-button update has to ENABLE what it copies — otherwise the
    new leg runs until the next reboot and then quietly disappears."""
    from encoder import system
    monkeypatch.delenv('SCOREBUG_FAKE', raising=False)   # exercise the real path
    install = tmp_path / 'opt'
    (install / 'encoder').mkdir(parents=True)
    units = tmp_path / 'systemd'
    units.mkdir()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == 'git':
            src = Path(cmd[5])
            (src / 'encoder').mkdir(parents=True)
            (src / 'VERSION').write_text('9.9.9\n')
            (src / 'mediamtx.yml').write_text('paths:\n')
            (src / 'systemd').mkdir()
            for u in ('playcall-encoder.service',
                      'playcall-encoder-live.service'):
                (src / 'systemd' / u).write_text('[Unit]\n')

        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(system, 'run', fake_run)
    real_copy2 = system.shutil.copy2      # captured BEFORE patching

    def copy2(src, dst):                  # /etc is not ours to write in a test
        return None if str(dst).startswith('/etc/') \
            else real_copy2(src, dst)
    monkeypatch.setattr(system.shutil, 'copy2', copy2)
    ok, detail = system.self_update(install_dir=str(install))
    assert ok and detail == '9.9.9'
    enabled = [c[2] for c in calls if c[:2] == ['systemctl', 'enable']]
    assert 'playcall-encoder-live' in enabled
    assert ['systemctl', 'daemon-reload'] in calls
    # and the caller bounces it, before the box's own service goes last
    assert 'playcall-encoder-live' in system.UPDATE_UNITS
    assert system.UPDATE_UNITS[-1] == 'playcall-encoder'


# ── the probe ────────────────────────────────────────────────────────────────

def test_probe_reads_the_true_track_list_over_rtsp():
    """Self-contained on purpose: importing youtube_push.probe_codecs
    crash-looped the service on a box whose youtube_push predated that
    helper (5 Sep 2026). This leg must not depend on the YouTube leg's
    internals."""
    seen = {}

    class R:
        returncode = 0
        stdout = json.dumps({'streams': [
            {'codec_type': 'video', 'codec_name': 'hevc'},
            {'codec_type': 'audio', 'codec_name': 'aac'}]})

    def runner(cmd, **kw):
        seen['cmd'] = cmd
        return R()
    cfg = {'local_ingest_key': 'abc123'}
    assert live_push.probe_codecs(cfg, runner) == ('hevc', 'aac')
    assert '-rtsp_transport' in seen['cmd']            # RTMP drops HEVC/Opus
    assert 'rtsp://127.0.0.1:8554/live/abc123' in seen['cmd']

    class Bad:
        returncode = 1
        stdout = ''
    assert live_push.probe_codecs(cfg, lambda *a, **k: Bad()) == ('', '')

    class Junk:
        returncode = 0
        stdout = 'not json'
    assert live_push.probe_codecs(cfg, lambda *a, **k: Junk()) == ('', '')


def test_live_push_imports_nothing_from_the_youtube_leg():
    """The guard for the bug above: prose may MENTION the YouTube leg, but
    nothing here may import from it."""
    import ast as _ast
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'encoder', 'live_push.py')).read()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.ImportFrom):
            assert 'youtube_push' not in (node.module or '')
        elif isinstance(node, _ast.Import):
            assert all('youtube_push' not in a.name for a in node.names)


# ── SRT: the road for links that lose packets ────────────────────────────

class _Err(Exception):
    def __init__(self, code):
        self.code = code


def _pusher_with(monkeypatch, tmp_path, http, cfg=None, transport='auto'):
    cfg = cfg or {'local_ingest_key': 'k',
                  'live_push': {'enabled': True, 'angle': 'main',
                                'transport': transport}}
    monkeypatch.setenv('PLAYCALL_ENCODER_STATE', str(tmp_path))
    p = live_push.LivePusher(cfg_load=lambda: cfg,
                             status=live_push.StatusWriter(
                                 tmp_path / 'livepush.json'),
                             http=http)
    return p


def test_srt_sends_mpegts_and_keeps_the_level_fix():
    """The server remuxes what arrives straight through, so a bitstream
    that lies about its H.264 level would carry that lie all the way to
    the phone that then refuses to decode it. The fix belongs here."""
    cmd = live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'h264',
                                  'srt://s:8890?x=1', 'aac')
    assert cmd[cmd.index('-f') + 1] == 'mpegts'
    assert cmd[-1] == 'srt://s:8890?x=1'
    assert cmd[cmd.index('-c:v') + 1] == 'copy'
    assert cmd[cmd.index('-c:a') + 1] == 'copy'      # already AAC
    assert '-bsf:v' in cmd and 'h264_metadata=level=auto' in cmd
    assert cmd[cmd.index('-i') + 1].startswith('rtsp://127.0.0.1:8554/')
    # Counters for the settings card: with SRT the box is not the one
    # queueing bytes, so ffmpeg's own numbers are the only honest source.
    assert '-progress' in cmd and cmd[cmd.index('-progress') + 1] == 'pipe:1'


def test_hevc_keeps_the_level_filter_off():
    """h264_metadata on an HEVC stream is not a no-op, it is an error."""
    cmd = live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'hevc', 'srt://s')
    assert '-bsf:v' not in cmd


def test_a_server_without_srt_is_not_asked_again_all_game(monkeypatch,
                                                          tmp_path):
    """501 is a property of the server, not of the attempt. Re-asking
    every few seconds would be noise on every poll for two hours."""
    calls = []

    def http(url, payload=None, headers=None, timeout=None):
        calls.append(url)
        raise _Err(501)
    p = _pusher_with(monkeypatch, tmp_path, http)
    t = {'ingest': 'https://s/ingest', 'token': 'tok', 'game': 'g',
         'angle': 'main'}
    assert p.request_srt(t, 'hevc') is None
    assert p.transport(p.cfg_load()) == 'https'      # cooled off
    assert p.request_srt(t, 'hevc') is None
    assert len(calls) == 2                            # transport() is the gate


def test_a_busy_pool_is_retried_but_a_missing_one_is_not(monkeypatch,
                                                          tmp_path):
    """All ports busy is temporary — another angle will finish."""
    p = _pusher_with(monkeypatch, tmp_path,
                     lambda *a, **k: (_ for _ in ()).throw(_Err(503)))
    t = {'ingest': 'https://s/ingest', 'token': 'tok'}
    assert p.request_srt(t, 'hevc') is None
    assert p.transport(p.cfg_load()) == 'auto'        # still willing


def test_a_ticket_is_used_as_given(monkeypatch, tmp_path):
    def http(url, payload=None, headers=None, timeout=None):
        assert url.endswith('/ingest/srt') and payload['token'] == 'tok'
        return {'ok': True, 'session': 'ls_1', 'run': 'r003',
                'srt': {'url': 'srt://s:8891?mode=caller', 'port': 8891,
                        'passphrase': 'p' * 32, 'latency_ms': 2000}}
    p = _pusher_with(monkeypatch, tmp_path, http)
    got = p.request_srt({'ingest': 'https://s/ingest', 'token': 'tok'}, 'hevc')
    assert got['session'] == 'ls_1' and got['port'] == 8891
    assert got['url'] == 'srt://s:8891?mode=caller'


def test_a_reply_without_a_url_is_not_a_ticket(monkeypatch, tmp_path):
    p = _pusher_with(monkeypatch, tmp_path,
                     lambda *a, **k: {'ok': True, 'session': 'ls_1',
                                      'srt': {}})
    assert p.request_srt({'ingest': 'https://s/ingest', 'token': 't'},
                         'hevc') is None


@pytest.mark.parametrize('mode, want', [
    ('auto', 'auto'), ('srt', 'srt'), ('https', 'https'),
    ('SRT', 'srt'), ('', 'auto'), ('nonsense', 'auto'),
])
def test_transport_setting(monkeypatch, tmp_path, mode, want):
    p = _pusher_with(monkeypatch, tmp_path, lambda *a, **k: {},
                     transport=mode)
    assert p.transport(p.cfg_load()) == want


def test_srt_only_never_silently_posts_chunks(monkeypatch, tmp_path):
    """Someone who chose SRT deliberately should be told it is not
    happening, not quietly given a different transport."""
    p = _pusher_with(monkeypatch, tmp_path,
                     lambda *a, **k: (_ for _ in ()).throw(_Err(501)),
                     transport='srt')
    monkeypatch.setattr(live_push, 'read_target',
                        lambda *a: {'ingest': 'https://s/ingest',
                                    'token': 't', 'game': 'g',
                                    'angle': 'main'})
    monkeypatch.setattr(p, '_probe', lambda cfg: ('hevc', 'aac'))
    monkeypatch.setattr(p, 'push_https',
                        lambda *a: pytest.fail('fell back without saying so'))
    assert p.run_once() == 0
    assert 'SRT' in (live_push.status(tmp_path / 'livepush.json')
                     .get('reason') or '')


def test_repeated_short_srt_attempts_fall_back(monkeypatch, tmp_path):
    """UDP blocked on the way out looks exactly like this: a ticket
    issued, ffmpeg up, and nothing ever connecting. A working feed beats
    being right about the transport."""
    p = _pusher_with(monkeypatch, tmp_path, lambda *a, **k: {})
    for _ in range(live_push.SRT_STRIKES - 1):
        p._note_srt_attempt(1.0)
        assert p.transport(p.cfg_load()) == 'auto'
    p._note_srt_attempt(1.0)
    assert p.transport(p.cfg_load()) == 'https'


def test_an_srt_push_that_lasts_clears_the_strikes(monkeypatch, tmp_path):
    p = _pusher_with(monkeypatch, tmp_path, lambda *a, **k: {})
    p._note_srt_attempt(1.0)
    p._note_srt_attempt(live_push.SRT_MIN_ALIVE + 1)
    for _ in range(live_push.SRT_STRIKES - 1):
        p._note_srt_attempt(1.0)
    assert p.transport(p.cfg_load()) == 'auto'


def test_auto_uses_srt_when_the_server_offers_it(monkeypatch, tmp_path):
    used = {}

    def http(url, payload=None, headers=None, timeout=None):
        return {'ok': True, 'session': 'ls_9',
                'srt': {'url': 'srt://s:8890', 'port': 8890,
                        'latency_ms': 2000, 'passphrase': 'p' * 32}}
    p = _pusher_with(monkeypatch, tmp_path, http)
    monkeypatch.setattr(live_push, 'read_target',
                        lambda *a: {'ingest': 'https://s/ingest',
                                    'token': 't', 'game': 'g',
                                    'angle': 'main'})
    monkeypatch.setattr(p, '_probe', lambda cfg: ('hevc', 'aac'))
    def _srt(cfg, t, ticket, v, a=''):
        used['url'] = ticket['url']
        return 99
    monkeypatch.setattr(p, 'push_srt', _srt)
    monkeypatch.setattr(p, 'push_https',
                        lambda *a: pytest.fail('ignored a good ticket'))
    assert p.run_once() == 99
    assert used['url'] == 'srt://s:8890'


def test_audio_always_leaves_the_box_as_aac_over_srt():
    """The server remuxes MPEG-TS to fragmented MP4 and must convert AAC's
    framing on the way (ADTS → ASC). It applies that filter blind — it
    cannot know the codec before the stream arrives — and the filter
    errors outright on anything that is not AAC. A WHIP publisher on this
    box's MediaMTX sends Opus. So the end that knows the codec is the one
    that has to guarantee it."""
    for codec in ('opus', 'mp3', 'ac3'):
        cmd = live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'hevc',
                                      'srt://s', codec)
        assert cmd[cmd.index('-c:a') + 1] == 'aac', codec
        assert '-b:a' in cmd and '-ar' in cmd
    for codec in ('aac', ''):
        cmd = live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'hevc',
                                      'srt://s', codec)
        assert cmd[cmd.index('-c:a') + 1] == 'copy', codec
    # video is never re-encoded whatever the audio does
    assert live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'hevc',
                                   'srt://s', 'opus')[
        live_push.build_srt_cmd({'local_ingest_key': 'k'}, 'hevc',
                                'srt://s', 'opus').index('-c:v') + 1] == 'copy'


def test_the_probed_audio_codec_reaches_the_srt_push(monkeypatch, tmp_path):
    """Probing it and then not passing it on would be the same bug with
    extra steps."""
    seen = {}

    def http(url, payload=None, headers=None, timeout=None):
        return {'ok': True, 'session': 'ls_1',
                'srt': {'url': 'srt://s:8890', 'port': 8890,
                        'latency_ms': 2000, 'passphrase': 'p' * 32}}
    p = _pusher_with(monkeypatch, tmp_path, http)
    monkeypatch.setattr(live_push, 'read_target',
                        lambda *a: {'ingest': 'https://s/ingest',
                                    'token': 't', 'game': 'g',
                                    'angle': 'main'})
    monkeypatch.setattr(p, '_probe', lambda cfg: ('hevc', 'opus'))
    monkeypatch.setattr(p, 'push_srt',
                        lambda cfg, t, ticket, v, a='': seen.update(
                            v=v, a=a) or 5)
    assert p.run_once() == 5
    assert seen == {'v': 'hevc', 'a': 'opus'}
