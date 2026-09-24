"""Send the box's own recording to BaseStream after the game — on request.

The box records everything its camera sends (MediaMTX keeps a rolling
fragmented-MP4 recording; clipper.py cuts clips from it), and it pushes
a live copy to the stream server while the game is on. Those are two
different things, and on 21 Sep 2026 (Maeser) the difference was the
evening: every clip landed, and the Main angle on the watch page had
nothing playable, because the live copy is all the stream server ever
gets. The phones have had the answer since September — a take recorded
on the phone is sent afterwards as an UPLOAD session, and the server
files it as ordinary runs. This is the box's version of that.

ON A BUTTON, never on its own: a staff member presses "Send full
recording to BaseStream" on the stream status page, the site stamps a
request on the box's assignment channel, and the box collects it on its
next poll. The request names the game, the stretch of time that matters
and carries an ingest ticket for the box's angle.

WHAT IS SENT is the holes: the box asks the server what it already
holds for the angle (/ingest/coverage), intersects the gaps with what
its own recording actually has, and sends only those stretches, in
pieces of at most PIECE_S, each piece its own upload run stamped with
when the video was shot. Nothing is re-encoded — the recording is read
back from MediaMTX's playback server as fragmented MP4, the same byte
shape the live push posts. The upload is throttled hard while a camera
is publishing (the uplink belongs to the game) and runs near full speed
otherwise.

Progress rides the heartbeat (cloud_link.backfill_status) so the page
that asked can watch it, and the request is retired by the site when
the box reports done or failed for its id.

TWO MODES. 'fill' (the default) sends only what the server lacks.
'replace' sends the WHOLE window as a clean copy — for a live copy that
dropped packets or ran at a low bitrate (21-22 Sep 2026) — re-encoded on
the box's hardware encoder to REPLACE_KBPS so it still plays on a phone
at the ballpark, while the clips (cut from the untouched recording by
clipper.py) stay at full quality. The pieces go up as a replacement
BATCH the stream server keeps out of the playlist until this box commits
it; then the time they cover is theirs and the old footage is left out
(kept on the server, not deleted).

NEVER UNDER A LIVE CAMERA. The stream server refuses an upload on an
angle that has a live camera (409), because starting a session on an
angle ends the one already there. The box waits instead: while its own
live push is on that game, and whenever the server says busy.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config, system

log = logging.getLogger('backfill')

PLAYBACK_URL = os.environ.get('PLAYBACK_URL', 'http://127.0.0.1:9996')
MTX_API = os.environ.get('MEDIAMTX_API', 'http://127.0.0.1:9997').rstrip('/')
PIECE_S = 600                  # one upload run per ten minutes of recording
MIN_PIECE_S = 3.0              # shorter than a segment is a seam, not a hole
CHUNK = 512 * 1024
UPLOAD_BPS_LIVE = 250_000      # while a camera is publishing: the game comes first
UPLOAD_BPS_IDLE = 4_000_000    # otherwise ~32 Mb/s — an hour of 3 Mb/s in a few minutes
STRIKES = 3                    # pieces failed in a row before giving up
HTTP_TIMEOUT = 60
TICK_S = 5.0
WAIT_S = 60.0                  # a busy angle is asked about again after this
REPLACE_KBPS = 5000            # the game footage: clean, and a phone still plays it
COPY_SLACK = 1.15              # a recording already this light is copied
TMP_DIR = Path(os.environ.get('BACKFILL_TMP',
                              '/var/lib/playcall-encoder/backfill-tmp'))


class Busy(Exception):
    """The angle has a live camera: wait, do not count it as a failure."""


def target_path():
    return config.state_dir() / 'backfill_target.json'


def status_path():
    return config.state_dir() / 'backfill.json'


def read_target(path=None):
    try:
        t = json.loads(Path(path or target_path()).read_text())
    except (OSError, ValueError):
        return None
    return t if isinstance(t, dict) and t.get('id') else None


def status(path=None):
    try:
        return json.loads(Path(path or status_path()).read_text())
    except (OSError, ValueError):
        return {}


def _atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def record_path(cfg):
    return f"live/{cfg.get('local_ingest_key') or ''}"


# ── the arithmetic ──────────────────────────────────────────────────────
def holes(coverage, frm, to, min_s=MIN_PIECE_S):
    """[(a, b)] inside [frm, to] that the server does NOT hold, from its
    coverage answer: everything before its first run, each gap it
    reports, everything after its last run. No runs at all: the whole
    window."""
    frm, to = float(frm), float(to)
    if to <= frm:
        return []
    cov = coverage or {}
    start, end = float(cov.get('start') or 0), float(cov.get('end') or 0)
    if not cov.get('runs') or not start or end <= start:
        out = [(frm, to)]
    else:
        out = []
        if start > frm:
            out.append((frm, min(start, to)))
        for g in cov.get('gaps') or []:
            try:
                a, b = float(g['from']), float(g['to'])
            except (KeyError, TypeError, ValueError):
                continue
            a, b = max(a, frm), min(b, to)
            if b > a:
                out.append((a, b))
        if end < to:
            out.append((max(end, frm), to))
    return [(a, b) for a, b in out if b - a >= min_s]


def pieces(gaps, segments, piece_s=PIECE_S, min_s=MIN_PIECE_S):
    """Cut what the server lacks down to what the recording HAS, in
    pieces of at most piece_s seconds. `segments` is [(start, dur)] of
    the local recording."""
    have = sorted((float(s), float(s) + float(d)) for s, d in segments if d)
    out = []
    for a, b in gaps:
        for s0, s1 in have:
            lo, hi = max(a, s0), min(b, s1)
            if hi - lo < min_s:
                continue
            t = lo
            while t < hi:
                u = min(t + piece_s, hi)
                if u - t >= min_s:
                    out.append((t, u))
                t = u
    return out


# ── the recording ───────────────────────────────────────────────────────
def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() \
        .replace('+00:00', 'Z')


def _epoch(iso):
    s = str(iso).replace('Z', '+00:00')
    return datetime.fromisoformat(s).timestamp()


def record_segments(cfg, http_get=None):
    """[(start_epoch, duration_s)] of the local recording, from MediaMTX's
    playback server (/list)."""
    get = http_get or _http_get
    qs = urllib.parse.urlencode({'path': record_path(cfg)})
    items = json.loads(get(f'{PLAYBACK_URL}/list?{qs}') or '[]')
    out = []
    for it in items or []:
        try:
            out.append((_epoch(it['start']), float(it['duration'])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_piece(cfg, start_epoch, seconds, http_get=None):
    """Fragmented MP4 of one stretch of the recording — init segment
    first, then moof/mdat pairs, which is what an ingest session eats."""
    get = http_get or _http_get
    qs = urllib.parse.urlencode({'path': record_path(cfg),
                                 'start': _iso(start_epoch),
                                 'duration': f'{seconds:.1f}',
                                 'format': 'fmp4'})
    return get(f'{PLAYBACK_URL}/get?{qs}', binary=True, timeout=300)


def _http_get(url, binary=False, timeout=HTTP_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode()


def _http_post(url, payload=None, headers=None, timeout=HTTP_TIMEOUT):
    if isinstance(payload, dict):
        body, ctype = json.dumps(payload).encode(), 'application/json'
    else:
        body, ctype = payload or b'', 'video/mp4'
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': ctype, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read().decode()
    return json.loads(out) if out.strip() else {}


def _live_push_status():
    try:
        from . import live_push
        return live_push.status()
    except Exception:
        return {}


def transcode_piece(data, kbps, codec, runner=None):
    """Fragmented MP4 in, fragmented MP4 out: video re-encoded on the
    QuickSync chip (live_push.video_args — the same pipeline the live
    copy uses), audio to AAC so every piece has the same track layout.
    Temp files on the recordings disk, never tmpfs: ten minutes of a
    Mevo is most of a gigabyte. Hardware decode first; the CPU decode if
    the chip refuses the stream."""
    from .live_push import video_args
    run = runner or (lambda argv: subprocess.run(argv, check=True,
                                                 capture_output=True,
                                                 timeout=1800))
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as tmp:
        src, dst = Path(tmp) / 'in.mp4', Path(tmp) / 'out.mp4'
        src.write_bytes(data)
        last = None
        for hw_decode in (True, False):
            pre, vid = video_args({'kbps': int(kbps), 'codec': codec}, hw_decode)
            argv = (['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y'] + pre
                    + ['-i', str(src), '-map', '0:v:0', '-map', '0:a:0?'] + vid
                    + ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
                       '-f', 'mp4', '-movflags',
                       '+frag_keyframe+empty_moov+default_base_moof', str(dst)])
            try:
                run(argv)
                if dst.exists() and dst.stat().st_size > 1024:
                    return dst.read_bytes()
            except Exception as e:
                last = e
        raise RuntimeError(f'ffmpeg could not re-encode the piece: {last}')


def camera_live(http_get=None):
    """Is a camera publishing into MediaMTX right now? Then the uplink
    belongs to the live push and this upload crawls."""
    get = http_get or _http_get
    try:
        items = (json.loads(get(f'{MTX_API}/v3/paths/list')) or {}).get('items')
        return any(p.get('ready') for p in items or [])
    except Exception:
        return False


# ── the worker ──────────────────────────────────────────────────────────
class Backfill:
    def __init__(self, cfg_load=config.load, http=None, http_get=None,
                 sleep=time.sleep, status_file=None, target_file=None,
                 transcode=None, live_status=None, hw=None):
        self.cfg_load = cfg_load
        self.http = http or _http_post
        self.http_get = http_get
        self.sleep = sleep
        self.status_file = Path(status_file) if status_file else None
        self.target_file = Path(target_file) if target_file else None
        self.transcode = transcode or transcode_piece
        self.live_status = live_status or _live_push_status
        self.hw = hw                       # None → ask the hardware
        self.running = True
        self.busy = False
        self._wait_until = 0.0

    # ── status ──────────────────────────────────────────────────────
    def _status(self):
        return status(self.status_file)

    def _write(self, **fields):
        st = dict(self._status())
        st.update(fields, updated=time.time())
        _atomic_json(self.status_file or status_path(), st)
        return st

    # ── one look ────────────────────────────────────────────────────
    def tick(self):
        """Run the request on the channel, once. False when there is
        nothing to do — no request, or one this box already finished."""
        t = read_target(self.target_file)
        if not t:
            return False
        st = self._status()
        if st.get('id') == t['id'] and st.get('state') in ('done', 'failed'):
            return False
        if time.monotonic() < self._wait_until:
            return False
        self.busy = True
        try:
            if st.get('id') == t['id'] and st.get('state') == 'committing':
                self.commit(t)             # everything sent; only the commit is owed
            else:
                self.run(t)
        except Busy as e:
            self._wait_until = time.monotonic() + WAIT_S
            self._write(id=t['id'], state='waiting', note=str(e)[:200])
        except Exception as e:
            log.exception('backfill %s failed', t.get('id'))
            self._write(id=t['id'], state='failed', error=str(e)[:200])
        finally:
            self.busy = False
        return True

    def _own_push_live(self, t):
        """Is THIS box's live push still on that game? Then the angle is
        busy by definition, and asking the server would only confirm it."""
        st = self.live_status() or {}
        return bool(st.get('connected')) and (
            not st.get('game') or str(st.get('game')) == str(t.get('game')))

    def run(self, t):
        cfg = self.cfg_load()
        mode = 'replace' if t.get('mode') == 'replace' else 'fill'
        if self._own_push_live(t):
            raise Busy('this box is still live on that game — the recording '
                       'goes up after the live push stops')
        frm, to = float(t.get('from') or 0), float(t.get('to') or 0)
        self._write(id=t['id'], game=t.get('game'), angle=t.get('angle'),
                    mode=mode, state='planning', sent_s=0, total_s=0, pieces=0,
                    done_pieces=0, error='', note='')
        segs = record_segments(cfg, self.http_get)
        if mode == 'replace':
            plan = pieces([(frm, to)], segs)
        else:
            cov = self.http(t['ingest'] + '/coverage', {'token': t['token']})
            plan = pieces(holes(cov, frm, to), segs)
        total = sum(b - a for a, b in plan)
        have = sum(d for _, d in segs)
        if not plan:
            note = ('the server already holds everything this box recorded '
                    'for that stretch' if have and mode == 'fill' else
                    'this box has no recording for that stretch')
            log.info('backfill %s: nothing to send — %s', t['id'], note)
            self._write(state='done', total_s=0, note=note)
            return
        log.info('backfill %s: %d piece(s), %.0f s of %s to send for '
                 '%s/%s', t['id'], len(plan), total, t.get('angle'),
                 t.get('game'), t.get('angle'))
        self._write(state='sending', total_s=round(total), pieces=len(plan))
        sent, strikes, ok = 0.0, 0, 0
        batch = t['id'] if mode == 'replace' else ''
        for i, (a, b) in enumerate(plan):
            try:
                self.send_piece(cfg, t, a, b, batch=batch)
                strikes = 0
            except Busy:
                raise                     # a camera came on: wait, resend later
            except Exception as e:
                strikes += 1
                log.warning('backfill piece %d/%d (%s +%.0fs) failed: %s',
                            i + 1, len(plan), _iso(a), b - a, e)
                self._write(error=f'piece {i + 1}: {str(e)[:160]}')
                if strikes >= STRIKES:
                    raise RuntimeError(f'{STRIKES} pieces failed in a row — '
                                       f'last: {str(e)[:120]}')
                continue
            sent += b - a
            ok += 1
            self._write(sent_s=round(sent), done_pieces=ok)
        note = (f'{ok} piece(s) sent' if ok == len(plan) else
                f'{ok} of {len(plan)} piece(s) sent — '
                f'{len(plan) - ok} could not be')
        if batch and ok:
            # Everything that could go has gone: now it takes over. Only
            # the time the new pieces actually cover changes — a piece
            # that failed leaves the old footage in its place.
            self._write(state='committing', sent_s=round(sent), note=note)
            self.commit(t)
            return
        self._write(state='done', sent_s=round(sent), note=note)
        log.info('backfill %s done: %.0f s sent', t['id'], sent)

    def commit(self, t):
        try:
            r = self.http(t['ingest'] + '/replace',
                          {'token': t['token'], 'batch': t['id']})
        except Exception as e:
            if getattr(e, 'code', None) == 409:
                raise Busy('a camera is live on that angle — the clean copy '
                           'takes over once it stops')
            raise
        secs = int((r or {}).get('seconds') or 0)
        st = self._status()
        self._write(state='done',
                    note=(st.get('note') or '') + f' · replaced {secs // 60} min '
                    'of the angle with the clean copy')
        log.info('backfill %s committed: %d s replaced', t['id'], secs)

    def _hw(self):
        if self.hw is not None:
            return self.hw
        try:
            if system.hw_encoder() != 'vaapi':
                return None
            return 'hevc' if system.hw_encoders().get('hevc') else 'h264'
        except Exception:
            return None

    def send_piece(self, cfg, t, a, b, batch=''):
        data = fetch_piece(cfg, a, b - a, self.http_get)
        if len(data) < 1024:
            raise RuntimeError('the recording came back empty')
        if batch:
            data = self._clean_copy(data, b - a)
        body = {'token': t['token'], 'capture_start': a,
                'tier_kbps': REPLACE_KBPS if batch else 0, 'codec': '',
                'upload': True}
        if batch:
            body['replace'] = batch
        try:
            r = self.http(t['ingest'] + '/start', body)
        except Exception as e:
            if getattr(e, 'code', None) == 409:
                raise Busy('a camera is live on that angle — the recording '
                           'goes up after it stops')
            raise
        sid = (r or {}).get('session')
        if not sid:
            raise RuntimeError(f'stream server refused the upload: {r}')
        try:
            bps = UPLOAD_BPS_LIVE if camera_live(self.http_get) else UPLOAD_BPS_IDLE
            for i in range(0, len(data), CHUNK):
                chunk = data[i:i + CHUNK]
                self.http(f'{t["ingest"]}/{sid}/feed', chunk,
                          {'X-Backlog-Ms': '0'})
                self.sleep(len(chunk) / bps)
        finally:
            try:
                self.http(f'{t["ingest"]}/{sid}/stop', {})
            except Exception:
                pass                      # a session the server already reaped

    def _clean_copy(self, data, seconds):
        """The replacement footage: re-encoded on the chip to REPLACE_KBPS
        unless the recording is already that light, in which case it is
        sent as it is. A transcode that fails is sent as it is too — a
        heavy clean copy beats none — and the status says so."""
        kbps = len(data) * 8 / max(1.0, seconds) / 1000
        codec = self._hw()
        if not codec or kbps <= REPLACE_KBPS * COPY_SLACK:
            return data
        try:
            out = self.transcode(data, REPLACE_KBPS, codec)
            if out and len(out) > 1024:
                return out
            raise RuntimeError('the re-encode came back empty')
        except Exception as e:
            log.warning('backfill re-encode failed (%s) — sending the '
                        'original for this piece', e)
            self._write(error=f're-encode failed, sent full quality: {str(e)[:120]}')
            return data

    # ── forever ─────────────────────────────────────────────────────
    def loop(self):
        while self.running:
            try:
                self.tick()
            except Exception:
                log.exception('backfill tick failed')
            self.sleep(TICK_S)

    def start(self):
        t = threading.Thread(target=self.loop, name='backfill', daemon=True)
        t.start()
        return t
