#!/usr/bin/env python3
"""🎦 Multi-View push — the camera feed, from this box to the site's server.

The phone camera page streams to the stream server by POSTing fragmented
MP4 in chunks (start → feed… → stop). This leg makes the BOX speak that
same protocol, so whatever is publishing to MediaMTX — a Mevo through the
Mevo/mimoLive app, Larix, OBS — lands on the game's Multi-View page as an
angle beside the phones, with the same DVR, the same desk card and the
same play markers. Nothing downstream knows the difference.

    MediaMTX (loopback RTSP)  →  ffmpeg -c copy -f mp4 (fragmented)
                              →  POST {ingest}/start, /feed…, /stop

Copy mode throughout: no decode, no encode, no quality lost, near-zero CPU.
Reading over RTSP rather than RTMP for the reason youtube_push.py does —
RTMP silently drops an HEVC video track or an Opus audio track, RTSP
carries the true track list.

The ingest ticket (server URL + signed token + angle) comes down on the
cloud assignment poll and is left in /run/playcall-encoder/live_target.json
by cloud_link. No ticket means no live game or no stream server, and this
leg simply waits — which is why it is safe to have on by default.

Backlog is measured, not guessed: chunks are queued by a reader thread and
drained by a poster thread, so "how far behind are we" is the age of the
oldest chunk still waiting. The stream desk reads it in the source card,
and a backlog past MAX_BACKLOG_S means the uplink is gone — the run is
restarted rather than left to rot, and the server marks the seam with a
playlist discontinuity.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from . import config, system

log = logging.getLogger('live_push')

RECONNECT_BASE = 3          # seconds; doubles per consecutive fast failure
RECONNECT_MAX = 30
POLL_IDLE = 5               # no ticket / nobody publishing → look again
READ_BLOCK = 64 * 1024      # ffmpeg stdout read size
FLUSH_BYTES = 512 * 1024    # post a chunk once it reaches this…
FLUSH_SECONDS = 1.0         # …or this much wall time, whichever comes first
MAX_BACKLOG_S = 30          # unsent video past this = the uplink is gone
HTTP_TIMEOUT = 20


_ANGLE_OK = 'abcdefghijklmnopqrstuvwxyz0123456789-_'


def safe_angle(raw):
    """'Behind Plate' → 'behind-plate'. The cloud sanitizes again with
    live_angles.angle_name(); doing it here too means the settings page
    shows the name viewers will actually see."""
    s = ''.join(c if c in _ANGLE_OK else '-'
                for c in str(raw or '').strip().lower()).strip('-')
    while '--' in s:
        s = s.replace('--', '-')
    return s[:24] or 'main'


def status(path=None):
    """What the push leg last wrote, for the settings page."""
    try:
        return json.loads(Path(path or config.state_dir()
                               / 'livepush.json').read_text())
    except (OSError, ValueError):
        return {}


def rtsp_in(cfg):
    return f"rtsp://127.0.0.1:8554/live/{cfg['local_ingest_key']}"


def target_path():
    return config.state_dir() / 'live_target.json'


def read_target(path=None):
    """The ingest ticket cloud_link last wrote, or None."""
    try:
        t = json.loads(Path(path or target_path()).read_text() or '{}')
    except (OSError, ValueError):
        return None
    if not isinstance(t, dict) or not (t.get('ingest') and t.get('token')):
        return None
    return t


def build_ffmpeg_cmd(cfg):
    """Read the published feed, write fragmented MP4 on stdout.

    +empty_moov puts a self-contained init segment (ftyp+moov) first and
    then moof/mdat pairs — the exact byte shape the server's splitter
    expects, and the same one MediaRecorder hands the phone. Fragmenting
    on keyframes keeps every fragment independently decodable, so a chunk
    boundary is never a broken segment.
    """
    return ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-rtsp_transport', 'tcp', '-i', rtsp_in(cfg),
            '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
            '-f', 'mp4', '-movflags',
            '+frag_keyframe+empty_moov+default_base_moof',
            'pipe:1']


class StatusWriter:
    """Atomic live-push status JSON, for the settings page and heartbeat."""

    def __init__(self, path=None):
        self.path = Path(path or config.state_dir() / 'livepush.json')
        self.stderr_tail = deque(maxlen=40)

    def write(self, connected, **fields):
        data = {'connected': connected, 'updated': time.time(),
                'stderr_tail': list(self.stderr_tail), **fields}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass


class Outbox:
    """Chunks waiting to be posted, oldest first, with their birth times."""

    def __init__(self):
        self.q = deque()
        self.lock = threading.Lock()
        self.closed = False

    def put(self, data):
        with self.lock:
            self.q.append((time.monotonic(), data))

    def get(self):
        with self.lock:
            return self.q.popleft() if self.q else None

    def backlog_s(self, now=None):
        with self.lock:
            if not self.q:
                return 0.0
            return (now or time.monotonic()) - self.q[0][0]

    def close(self):
        self.closed = True


class LivePusher:
    def __init__(self, cfg_load=config.load, runner=None, status=None,
                 http=None):
        self.cfg_load = cfg_load
        self.runner = runner or system.run
        self.status = status or StatusWriter()
        self.http = http or self._http
        self.running = True
        self.proc = None
        self.session = None

    # ── the stream server ───────────────────────────────────────────────
    @staticmethod
    def _http(url, payload=None, headers=None, timeout=HTTP_TIMEOUT):
        """POST JSON (dict payload) or raw bytes; returns parsed JSON."""
        if isinstance(payload, dict):
            body = json.dumps(payload).encode()
            ctype = 'application/json'
        else:
            body = payload or b''
            ctype = 'video/mp4'
        req = urllib.request.Request(url, data=body, headers={
            'Content-Type': ctype, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read().decode()
        return json.loads(out) if out.strip() else {}

    def start_session(self, target, codec):
        r = self.http(target['ingest'] + '/start',
                      {'token': target['token'], 'capture_start': time.time(),
                       'tier_kbps': 0, 'codec': codec})
        sid = (r or {}).get('session')
        if not sid:
            raise RuntimeError(f'stream server refused the ticket: {r}')
        return sid

    def feed(self, target, sid, data, backlog_ms):
        return self.http(f"{target['ingest']}/{sid}/feed", data,
                         {'X-Backlog-Ms': str(int(backlog_ms))})

    def stop_session(self, target, sid):
        try:
            self.http(f"{target['ingest']}/{sid}/stop", {})
        except Exception:
            pass                    # a session the server already reaped

    # ── one push ────────────────────────────────────────────────────────
    def run_once(self):
        """One ffmpeg + one ingest session. Returns seconds it survived."""
        cfg = self.cfg_load()
        target = read_target()
        if not target:
            self.status.write(False, reason='no live game')
            return 0
        if not cfg.get('local_ingest_key'):
            self.status.write(False, reason='box not provisioned')
            return 0
        vcodec, _acodec = self._probe(cfg)
        if not vcodec:
            self.status.write(False, reason='no camera publishing')
            return 0

        started = time.monotonic()
        self.proc = subprocess.Popen(build_ffmpeg_cmd(cfg),
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        outbox = Outbox()
        reader = threading.Thread(target=self._read_loop,
                                  args=(outbox,), daemon=True)
        reader.start()
        log.info(f"live push start (angle={target['angle']}, "
                 f"game={target['game']}, in={vcodec}, copy)")
        try:
            self._post_loop(target, outbox, vcodec)
        except Exception as e:
            log.info(f'live push ended: {e}')
        finally:
            outbox.close()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            if self.session:
                self.stop_session(target, self.session)
                self.session = None
            self.status.write(False)
        return time.monotonic() - started

    def _probe(self, cfg):
        from .youtube_push import probe_codecs
        return probe_codecs(cfg, self.runner)

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.status.stderr_tail.append(line.decode(errors='replace')
                                           .rstrip())

    def _read_loop(self, outbox):
        """ffmpeg stdout → chunks, grouped so the server sees about one
        second of video per POST rather than one per 64 KB read."""
        buf = bytearray()
        last = time.monotonic()
        stdout = self.proc.stdout
        while self.running and not outbox.closed:
            block = stdout.read1(READ_BLOCK)   # what's ready, not a full block
            if not block:
                break
            buf += block
            now = time.monotonic()
            if len(buf) >= FLUSH_BYTES or (now - last) >= FLUSH_SECONDS:
                outbox.put(bytes(buf))
                buf.clear()
                last = now
        if buf:
            outbox.put(bytes(buf))
        outbox.close()

    def _post_loop(self, target, outbox, vcodec):
        """Drain the outbox to the server, tracking how far behind we are."""
        sent = 0
        window = deque()             # (monotonic, bytes) for the kbps figure
        while self.running:
            item = outbox.get()
            if item is None:
                if outbox.closed and self.proc.poll() is not None:
                    return           # ffmpeg is gone and the queue is empty
                time.sleep(0.05)
                continue
            _born, data = item
            # The ticket is re-read as we go: when the game ends (or the
            # next one starts) the token changes underneath us, and this
            # session belongs to the old game. Restarting is the whole
            # correction — the next attempt picks up the new ticket.
            if _changed(target, read_target()):
                raise RuntimeError('assignment changed')
            if self.session is None:
                self.session = self.start_session(target, vcodec)
                log.info(f'ingest session {self.session}')
            backlog = outbox.backlog_s()
            if backlog > MAX_BACKLOG_S:
                raise RuntimeError(f'backlog {backlog:.0f}s — restarting')
            r = self.feed(target, self.session, data, backlog * 1000)
            sent += len(data)
            now = time.monotonic()
            window.append((now, len(data)))
            while window and window[0][0] < now - 10:
                window.popleft()
            self.status.write(True, kbps=_kbps(window), bytes=sent,
                              backlog_ms=int(backlog * 1000),
                              angle=target['angle'], game=target['game'],
                              session=self.session,
                              dropped=(r or {}).get('dropped') or 0)

    # ── forever ─────────────────────────────────────────────────────────
    def run_forever(self):
        backoff = RECONNECT_BASE
        while self.running:
            try:
                alive = self.run_once()
            except Exception:
                log.exception('live push attempt failed')
                alive = 0
            if not self.running:
                break
            if alive == 0:
                # Nothing to do yet (no game, no camera) — this is the
                # normal state between games, so wait quietly.
                time.sleep(POLL_IDLE)
                continue
            backoff = RECONNECT_BASE if alive > 30 else \
                min(RECONNECT_MAX, backoff * 2)
            log.info(f'live push ended — reconnecting in {backoff}s')
            time.sleep(backoff)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def _changed(target, now):
    """True when the ticket no longer points where this session is going.
    The token itself is re-minted every poll and is deliberately not part
    of the comparison."""
    if not now:
        return True
    return (now.get('game'), now.get('angle'), now.get('ingest')) != \
        (target.get('game'), target.get('angle'), target.get('ingest'))


def _kbps(window):
    if len(window) < 2:
        return 0
    span = window[-1][0] - window[0][0]
    if span <= 0:
        return 0
    return int(sum(n for _, n in window) * 8 / span / 1000)


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    pusher = LivePusher()
    import signal

    def _stop(*a):
        pusher.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    pusher.run_forever()


if __name__ == '__main__':
    main()
