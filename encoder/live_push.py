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

TWO ROADS, and neither is simply better:

    SRT    MediaMTX → ffmpeg -c copy -f mpegts → srt://server:port
    HTTPS  MediaMTX → ffmpeg -c copy -f mp4    → POST /start /feed /stop

  * SRT is for a link that LOSES PACKETS. TCP reads every loss as
    congestion and halves its window, so a Starlink or LTE uplink at one
    percent loss sawtooths a 6 Mb/s feed down to two and reports a
    backlog on a link with capacity to spare. SRT retransmits inside a
    fixed window without touching the send rate, and holds its bitrate.

  * HTTPS is for a link that GOES AWAY. SRT abandons anything it cannot
    recover inside that window — gone, permanently. The chunk queue holds
    unsent video and delivers it late. For a recording, late beats gone.

So `transport` is auto by default: ask the server for an SRT ticket, use
it if it answers, and fall back to chunked HTTPS if it does not — or if
SRT keeps dying, which is what a firewall eating UDP looks like from
here. Both land in the same session on the server, so the recording on
disk is identical either way.
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

# An SRT attempt that dies faster than this never really connected —
# almost always UDP blocked on the way out. A few of those in a row and we
# stop asking for a while, because HTTPS works and video matters more than
# being right about the transport.
SRT_MIN_ALIVE = 20
SRT_STRIKES = 3
SRT_COOLDOWN_S = 600


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


def probe_codecs(cfg, runner=None):
    """(video, audio) codecs of whatever is publishing right now, or two
    empty strings when nobody is — which is how this leg knows to wait.

    Deliberately self-contained rather than borrowed from youtube_push:
    that module is the YouTube leg's business, its helpers differ between
    the encoder release repo and the site repo, and an import of one of
    them crash-looped this service on a box where the name was absent.

    RTSP, not RTMP: MediaMTX's RTMP reader silently DROPS a track it
    cannot carry (H.265 video, Opus audio), so RTMP would report an HEVC
    camera as having no video at all.
    """
    runner = runner or system.run
    r = runner(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_type,codec_name',
                '-of', 'json', rtsp_in(cfg)], timeout=15)
    if getattr(r, 'returncode', 1) != 0:
        return '', ''
    try:
        streams = json.loads(r.stdout or '{}').get('streams') or []
    except (ValueError, AttributeError):
        return '', ''
    vcodec = acodec = ''
    for st in streams:
        if st.get('codec_type') == 'video' and not vcodec:
            vcodec = st.get('codec_name') or ''
        elif st.get('codec_type') == 'audio' and not acodec:
            acodec = st.get('codec_name') or ''
    if acodec and not _audio_flows(cfg, runner):
        # DECLARED is not the same as PRESENT, and the difference is
        # fatal downstream. mimoLive announces an AAC track and sends no
        # samples in it; ffmpeg copies the empty track, the server's HLS
        # carries it, and a browser then builds an audio SourceBuffer
        # that never fills. HTMLMediaElement.buffered is the INTERSECTION
        # of the source buffers, so a full video track and an empty audio
        # one leave the element with nothing playable anywhere: frames
        # decode, playback stalls, and not one error is raised by
        # anything. It cost a whole evening on 6 Sep 2026 and read as
        # five different faults, none of them this one.
        log.info('audio track is declared but carries no samples — '
                 'dropping it rather than shipping a track that can '
                 'never fill')
        acodec = ''
    return vcodec, acodec


def _audio_flows(cfg, runner):
    """Does the audio track actually carry samples? Asked separately
    because the stream listing cannot answer it — a track with no data
    still appears in the listing, with a codec name and everything."""
    try:
        r = runner(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                    '-select_streams', 'a:0', '-read_intervals', '%+3',
                    '-show_entries', 'packet=pts_time', '-of', 'csv=p=0',
                    rtsp_in(cfg)], timeout=20)
    except Exception:
        return True          # cannot tell → do not throw audio away
    if getattr(r, 'returncode', 1) != 0:
        return True
    return bool((getattr(r, 'stdout', '') or '').strip())


def build_ffmpeg_cmd(cfg, vcodec='', acodec=''):
    """Read the published feed, write fragmented MP4 on stdout.

    +empty_moov puts a self-contained init segment (ftyp+moov) first and
    then moof/mdat pairs — the exact byte shape the server's splitter
    expects, and the same one MediaRecorder hands the phone. Fragmenting
    on keyframes keeps every fragment independently decodable, so a chunk
    boundary is never a broken segment.

    h264_metadata=level=auto recomputes the LEVEL the bitstream declares
    from what it actually contains. mimoLive stamps Level 5.2 — the
    figure you would use for 4K120 — on a 1080p30 stream, and an Android
    decoder reads that as a demand it cannot meet and refuses the codec
    outright: MSE threw bufferAppendError on the first append, nothing
    ever buffered, and the angle was a black rectangle on every phone
    while ffmpeg, MediaMTX, the clipper and YouTube all played it
    happily (5 Sep 2026). It is a bitstream filter, not an encoder: the
    frames are untouched and it costs nothing.
    """
    fix = ['-bsf:v', 'h264_metadata=level=auto'] if vcodec == 'h264' else []
    # -an, not just an unmapped optional stream: '-map 0:a:0?' still maps
    # a track that exists, and an existing EMPTY track is precisely the
    # thing that stalls a player forever (see probe_codecs).
    #
    # aac_adtstoasc is NOT optional here, and the reason is the flag on
    # the next line but one. ffmpeg inserts this filter automatically for
    # AAC entering MP4 — except that +empty_moov switches automatic
    # bitstream filtering OFF. It says so and carries on:
    #
    #   [mp4] Empty MOOV enabled; disabling automatic bitstream filtering
    #   Input  stream #0:1 (audio): 284 packets read
    #   Output stream #0:1 (audio):   0 packets muxed
    #
    # Exit 0. No error. The track is declared in the init and never
    # carries a sample, so a browser builds an audio SourceBuffer that
    # can never fill; buffered is the INTERSECTION of the source buffers,
    # so the video track is unreachable too and playback stalls with
    # nothing to report. That cost an evening on 6 Sep 2026 and was read
    # as five different faults. We cannot drop +empty_moov — it is what
    # makes the init segment self-contained for the server's splitter —
    # so the filter is named explicitly.
    if not acodec:
        audio = ['-an']
    elif acodec == 'aac':
        audio = ['-map', '0:a:0?', '-bsf:a', 'aac_adtstoasc']
    else:
        audio = ['-map', '0:a:0?']
    return ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-rtsp_transport', 'tcp', '-i', rtsp_in(cfg),
            '-map', '0:v:0'] + audio + ['-c', 'copy'] + fix + [
            '-f', 'mp4', '-movflags',
            '+frag_keyframe+empty_moov+default_base_moof',
            'pipe:1']


def build_srt_cmd(cfg, vcodec, url, acodec=''):
    """Read the published feed, write MPEG-TS straight into an SRT socket.

    MPEG-TS rather than fragmented MP4 because SRT carries a stream of
    fixed-size packets with no framing of its own, and TS is the container
    every SRT receiver expects. The server remuxes it back to fragmented
    MP4 on arrival, still in copy mode, so the recording is byte-for-byte
    the video this box would have posted.

    The same h264_metadata level fix as the HTTPS path: it corrects what
    the BITSTREAM declares, so it has to happen here — the server's remux
    copies the level along with the frames and would carry a bad one all
    the way to the phone that refuses to decode it.

    -progress pipe:1 is how the settings card gets a bitrate. With SRT the
    box is not the one queueing bytes, so there is no outbox to measure;
    ffmpeg's own counters are the honest source.

    Audio leaves here as AAC, always. The server remuxes MPEG-TS to
    fragmented MP4 and must convert AAC's framing on the way (ADTS to
    ASC) — a filter it has to apply blind, because it cannot know the
    codec before the stream arrives, and which errors outright on
    anything that is not AAC. A WHIP publisher on this box's MediaMTX
    sends Opus; youtube_push transcodes it for its own reasons. So the
    end that KNOWS the codec is the one that guarantees it.
    """
    fix = ['-bsf:v', 'h264_metadata=level=auto'] if vcodec == 'h264' else []
    if not acodec:
        audio = ['-an']
    elif acodec == 'aac':
        audio = ['-map', '0:a:0?', '-c:a', 'copy']
    else:
        audio = ['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k',
                 '-ar', '48000']
    return ['ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-progress', 'pipe:1', '-nostats',
            '-rtsp_transport', 'tcp', '-i', rtsp_in(cfg),
            '-map', '0:v:0',
            '-c:v', 'copy'] + fix + audio + [
            '-muxdelay', '0', '-muxpreload', '0',
            '-f', 'mpegts', url]


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
        self.progress = {}
        # SRT is asked for again after this; see _note_srt_attempt.
        self._srt_off_until = 0.0
        self._srt_strikes = 0

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

    def request_srt(self, target, codec):
        """Ask for an SRT port. None means "post chunks instead".

        A server without SRT configured answers 501, and that is a
        property of the server, not of this attempt — so it is cached,
        rather than re-asked every few seconds for the rest of the game.
        """
        try:
            r = self.http(target['ingest'] + '/srt',
                          {'token': target['token'],
                           'capture_start': time.time(),
                           'tier_kbps': 0, 'codec': codec})
        except Exception as e:
            code = getattr(e, 'code', None)
            if code == 501:
                self._srt_off_until = time.time() + SRT_COOLDOWN_S
                log.info('stream server has no SRT ingest — posting chunks')
            elif code == 503:
                log.info('stream server SRT ports all busy — posting chunks')
            else:
                log.info(f'srt ticket refused ({e}) — posting chunks')
            return None
        t = (r or {}).get('srt') or {}
        if not (t.get('url') and (r or {}).get('session')):
            return None
        return dict(t, session=r['session'], run=r.get('run'))

    def transport(self, cfg):
        """'srt', 'https', or 'auto' resolved against the cooldown."""
        mode = ((cfg.get('live_push') or {}).get('transport')
                or 'auto').lower()
        if mode not in ('auto', 'srt', 'https'):
            mode = 'auto'
        if mode == 'auto' and time.time() < self._srt_off_until:
            return 'https'
        return mode

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
        vcodec, acodec = self._probe(cfg)
        if not vcodec:
            self.status.write(False, reason='no camera publishing')
            return 0

        mode = self.transport(cfg)
        if mode != 'https':
            ticket = self.request_srt(target, vcodec)
            if ticket:
                return self.push_srt(cfg, target, ticket, vcodec, acodec)
            if mode == 'srt':
                # Asked for explicitly, so do not quietly do something
                # else — say why nothing is going out.
                self.status.write(False,
                                  reason='SRT unavailable on the server')
                return 0
        return self.push_https(cfg, target, vcodec, acodec)

    # ── SRT: ffmpeg holds the socket, we watch ──────────────────────────
    def push_srt(self, cfg, target, ticket, vcodec, acodec=''):
        started = time.monotonic()
        self.session = ticket['session']
        self.progress = {}
        self.proc = subprocess.Popen(
            build_srt_cmd(cfg, vcodec, ticket['url'], acodec),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._read_progress, daemon=True).start()
        log.info(f"live push start via SRT (angle={target['angle']}, "
                 f"game={target['game']}, port={ticket.get('port')}, "
                 f"latency={ticket.get('latency_ms')}ms, "
                 f"in={vcodec}/{acodec or 'none'})")
        try:
            self._watch_srt(target, ticket)
        except Exception as e:
            log.info(f'live push (srt) ended: {e}')
        finally:
            self._teardown(target)
        alive = time.monotonic() - started
        self._note_srt_attempt(alive)
        return alive

    def _watch_srt(self, target, ticket):
        """Once a second: is ffmpeg still there, is the ticket still ours,
        and what do the counters say. There is no outbox to drain — SRT
        does the queueing, inside its own latency window — so backlog is
        reported as zero rather than invented."""
        sent = 0
        window = deque()
        while self.running:
            rc = self.proc.poll()
            if rc is not None:
                raise RuntimeError(f'ffmpeg exited ({rc})')
            if _changed(target, read_target()):
                raise RuntimeError('assignment changed')
            total = int(self.progress.get('total_size') or 0)
            if total > sent:
                now = time.monotonic()
                window.append((now, total - sent))
                sent = total
                while window and window[0][0] < now - 10:
                    window.popleft()
            self.status.write(True, transport='srt', kbps=_kbps(window),
                              bytes=sent, backlog_ms=0,
                              angle=target['angle'], game=target['game'],
                              session=self.session, dropped=0,
                              srt_port=ticket.get('port'),
                              srt_latency_ms=ticket.get('latency_ms'))
            time.sleep(1)

    def _read_progress(self):
        """ffmpeg -progress writes key=value lines on stdout."""
        try:
            for raw in self.proc.stdout:
                line = raw.decode(errors='replace').strip()
                key, _, val = line.partition('=')
                if key:
                    self.progress[key] = val
        except (OSError, ValueError):
            pass

    def _note_srt_attempt(self, alive):
        """UDP blocked on the way out looks exactly like this: a ticket
        issued, ffmpeg up, and nothing ever connecting. After a few of
        those, take the HTTPS road for a while — a working feed beats
        being right about the transport."""
        if alive >= SRT_MIN_ALIVE:
            self._srt_strikes = 0
            return
        self._srt_strikes += 1
        if self._srt_strikes >= SRT_STRIKES:
            self._srt_strikes = 0
            self._srt_off_until = time.time() + SRT_COOLDOWN_S
            log.warning('SRT failed %d times in a row — posting chunks for '
                        'the next %d minutes. UDP blocked on the way out is '
                        'the usual cause.', SRT_STRIKES, SRT_COOLDOWN_S // 60)

    # ── HTTPS: we do the queueing ───────────────────────────────────────
    def push_https(self, cfg, target, vcodec, acodec=''):
        started = time.monotonic()
        self.proc = subprocess.Popen(build_ffmpeg_cmd(cfg, vcodec, acodec),
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
            self._teardown(target)
        return time.monotonic() - started

    def _teardown(self, target):
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

    def _probe(self, cfg):
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
            self.status.write(True, transport='https', kbps=_kbps(window),
                              bytes=sent, backlog_ms=int(backlog * 1000),
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
