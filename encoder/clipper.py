#!/usr/bin/env python3
"""
PlayCall Encoder — auto-clip cutter/uploader.

One box, both jobs. The YouTube leg (encoder/youtube_push.py) streams the
game out; this daemon cuts highlight clips from the SAME rolling local
recording MediaMTX keeps — so a clip's video never crossed the uplink and
is clean even when the live stream stuttered.

The scoring app is the edit desk: booking a play queues a clip window
(wall-clock start/end epochs) in the cloud. This daemon:

  1. polls   GET  {cloud}/api/pi/clips/jobs            (X-Api-Key auth)
  2. cuts    each window from the local recording via MediaMTX's playback
             server (GET /get?path=…&start=…&duration=…&format=mp4)
  3. keeps   a local copy in CLIPS_DIR (pruned after RETAIN_DAYS)
  4. uploads POST {cloud}/api/pi/clips/<id>/upload     (raw video/mp4),
             rate-capped by UPLOAD_BPS so a mid-game upload never starves
             the live stream

Ported from the original relay daemon (pi/clipper.py) — the cutting logic
is unchanged and battle-tested; what differs is configuration. Instead of
/etc/playcall/relay.env this reads /etc/playcall-encoder/config.json fresh
on every cycle, so pairing the box (or rotating its ingest key) is picked
up live with no restart. An unpaired box simply waits.

Standard library only.
"""

import http.client
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config, system

log = logging.getLogger('clipper')

PLAYBACK_URL = os.environ.get('PLAYBACK_URL',
                              'http://127.0.0.1:9996').rstrip('/')
MTX_API = os.environ.get('MEDIAMTX_API', 'http://127.0.0.1:9997').rstrip('/')
POLL_SECONDS = float(os.environ.get('POLL_SECONDS', '5'))
CLIPS_DIR = Path(os.environ.get('CLIPS_DIR',
                                '/var/lib/playcall-encoder/clips'))
RETAIN_DAYS = int(os.environ.get('RETAIN_DAYS', '7'))
UPLOAD_BPS = int(os.environ.get('UPLOAD_BPS', '250000'))

# A window still uncuttable this long after its end has fallen out of the
# rolling recording (or was never recorded) — report it failed rather than
# retry forever.
CUT_GIVE_UP_AFTER = 15 * 60
# Pi throttling starts ~80 °C; shed the heavy optional work before the
# whole box slows down (field report: an overheat killed radar capture).
CLIP_HOT_C = 78.0

_PRUNE_EVERY = 3600
# How long to wait between "still not paired" log lines (seconds).
_UNPAIRED_LOG_EVERY = 300


def cloud_link(cfg):
    """(base_url, api_key) from the encoder config, or (None, None) when the
    box has not been paired to PlayCall yet."""
    cloud = cfg.get('cloud') or {}
    base = (cloud.get('base_url') or '').rstrip('/')
    key = cloud.get('api_key') or ''
    return (base, key) if (base and key) else (None, None)


def record_path(cfg):
    """MediaMTX path the recording lives under — mirrors mediamtx.yml's
    `live/__INGEST_KEY__`."""
    return f"live/{cfg.get('local_ingest_key') or ''}"


class StatusWriter:
    """Atomic clip-status JSON that cloud_link folds into the heartbeat, so
    the website can show clip health next to stream health."""

    def __init__(self, path=None):
        self.path = Path(path or config.state_dir() / 'clips.json')
        self.uploaded = 0
        self.failed = 0
        self.last_error = ''

    def write(self, pending=0):
        data = {'updated': time.time(), 'pending': pending,
                'uploaded': self.uploaded, 'failed': self.failed,
                'last_error': self.last_error[:200]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass


class Clipper:
    def __init__(self, cfg_load=config.load, status=None):
        self.cfg_load = cfg_load
        self.status = status or StatusWriter()
        self.running = True
        self._last_unpaired_log = 0.0
        self._last_hot_log = 0.0
        self._ingest_checked = 0.0     # _ingest_live cache (10 s TTL)
        self._ingest_last = False

    # ── cloud ────────────────────────────────────────────────────────────

    def _api(self, base, key, path, data=None, ctype='application/json',
             timeout=30):
        req = urllib.request.Request(
            base + path, data=data,
            headers={'X-Api-Key': key, 'content-type': ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or '{}')
        except urllib.error.HTTPError as e:
            # urllib's own message is the status line and nothing else, so
            # a support bundle read "HTTP Error 400: Bad Request" for a
            # response whose body said exactly what was wrong. The
            # throttled upload path has always quoted the body; this one
            # is what a box with no bandwidth cap uses, which is most of
            # them. Carry the server's sentence into the error.
            detail = body = ''
            try:
                body = e.read().decode(errors='replace').strip()
            except Exception:
                pass
            try:
                detail = (json.loads(body or '{}') or {}).get('error') or ''
            except Exception:
                detail = ''          # not JSON — a proxy's HTML or plain text
            detail = detail or body[:200]
            if not detail:
                raise
            # Re-raise as the SAME class, with the server's sentence as the
            # reason. Every caller still sees an HTTPError with its code —
            # the poll loop's "cloud unreachable" branch and the 404
            # no-recording check both keep working — it just finally says
            # what the server said.
            raise urllib.error.HTTPError(
                e.url, e.code, detail[:200], e.headers, None) from None

    def _mark_failed(self, base, key, cid, why):
        try:
            self._api(base, key, f'/api/pi/clips/{cid}/fail',
                      data=json.dumps({'error': why[:300]}).encode())
        except Exception:
            log.warning('could not report failure for %s', cid)

    def _ingest_live(self, now=None):
        """Is a camera publishing into MediaMTX right now? Only then can
        the live YouTube push be using the uplink — which is the whole
        reason the upload cap exists. Hours after the game the cap was
        still on, and a 50-clip drain at 250 KB/s took all evening.
        Cached for 10 s so a burst of uploads costs one API call. When
        MediaMTX itself is unreachable the push cannot be running either,
        so the answer is honestly no."""
        now = time.time() if now is None else now
        if now - self._ingest_checked < 10:
            return self._ingest_last
        self._ingest_checked = now
        live = False
        try:
            with urllib.request.urlopen(f'{MTX_API}/v3/paths/list',
                                        timeout=5) as r:
                items = (json.loads(r.read().decode()) or {}).get('items')
            live = any(p.get('ready') for p in items or [])
        except Exception:
            live = False
        self._ingest_last = live
        return live

    def _throttled_upload(self, base, key, cid, data):
        """POST the MP4 — capped at UPLOAD_BPS bytes/second while the
        camera is live (a mid-game upload must never compete with the
        push for the uplink), full speed the rest of the time."""
        if not UPLOAD_BPS or not self._ingest_live():
            return self._api(base, key, f'/api/pi/clips/{cid}/upload',
                             data=data, ctype='video/mp4', timeout=600)
        u = urllib.parse.urlsplit(base)
        conn_cls = (http.client.HTTPSConnection if u.scheme == 'https'
                    else http.client.HTTPConnection)
        conn = conn_cls(u.netloc, timeout=600)
        try:
            conn.putrequest('POST', f'/api/pi/clips/{cid}/upload')
            conn.putheader('X-Api-Key', key)
            conn.putheader('content-type', 'video/mp4')
            conn.putheader('content-length', str(len(data)))
            conn.endheaders()
            chunk = max(8192, UPLOAD_BPS // 4)        # 4 sends/second
            for i in range(0, len(data), chunk):
                conn.send(data[i:i + chunk])
                time.sleep(len(data[i:i + chunk]) / UPLOAD_BPS)
            resp = conn.getresponse()
            body = resp.read().decode()
            if resp.status != 200:
                raise RuntimeError(f'upload HTTP {resp.status}: {body[:200]}')
            return json.loads(body or '{}')
        finally:
            conn.close()

    # ── local recording ──────────────────────────────────────────────────

    def _fetch_clip(self, cfg, start_epoch, duration):
        """Exact-range MP4 from MediaMTX's playback server. Raises on any
        failure (including 'recording not flushed yet', which is retried)."""
        start_iso = datetime.fromtimestamp(start_epoch, timezone.utc) \
            .isoformat().replace('+00:00', 'Z')
        qs = urllib.parse.urlencode({
            'path': record_path(cfg),
            'start': start_iso,
            'duration': f'{duration:.1f}',
            'format': 'mp4',
        })
        with urllib.request.urlopen(f'{PLAYBACK_URL}/get?{qs}',
                                    timeout=120) as resp:
            return resp.read()

    @staticmethod
    def _clip_duration(data):
        """Actual duration (seconds) of a fetched clip, or None if it can't
        be probed (caller then trusts the byte-count sanity check)."""
        try:
            with tempfile.NamedTemporaryFile(suffix='.mp4') as f:
                f.write(data)
                f.flush()
                probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries',
                     'format=duration', '-of', 'csv=p=0', f.name],
                    capture_output=True, text=True, timeout=30)
                return float(probe.stdout.strip())
        except Exception:
            return None

    @staticmethod
    def _media_seconds(data):
        """Seconds of video actually PRESENT, from the packet count. The
        container duration lies when the rolling recording has holes (the
        stream dropped mid-game): timestamps are preserved, so a clip cut
        across a dropout probes as the full window while holding a few
        seconds of media — it plays 10s then 'jumps to the end'. Counting
        packets against the frame rate measures what's really there."""
        try:
            with tempfile.NamedTemporaryFile(suffix='.mp4') as f:
                f.write(data)
                f.flush()
                probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-count_packets',
                     '-select_streams', 'v:0', '-show_entries',
                     'stream=nb_read_packets,r_frame_rate',
                     '-of', 'csv=p=0', f.name],
                    capture_output=True, text=True, timeout=60)
                rate_s, n_s = probe.stdout.strip().split(',')[:2]
                num, den = (rate_s.split('/') + ['1'])[:2]
                fps = float(num) / float(den or 1)
                if fps <= 0:
                    return None
                return int(n_s) / fps
        except Exception:
            return None

    @staticmethod
    def _fix_audio_for_ios(data):
        """Normalize EVERY cut, not just Opus-audio ones. Two jobs in one
        remux: (1) Opus phone audio → AAC so iPhones can play it; (2) the
        clock — a cut inherits the rolling recording's mid-stream
        timestamps, and a video track starting at a NEGATIVE pts (observed:
        -1.9s under a 0-based audio track) makes phone browsers stutter
        and jump to the end of a clip whose media is actually perfect.
        avoid_negative_ts zeroes the clock; faststart lets playback begin
        before the download finishes. Falls back to the original bytes on
        any failure — better an awkward clip than none."""
        try:
            with tempfile.NamedTemporaryFile(suffix='.mp4') as src:
                src.write(data)
                src.flush()
                probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                     '-show_entries', 'stream=codec_name', '-of', 'csv=p=0',
                     src.name],
                    capture_output=True, text=True, timeout=30)
                aud = ['-c:a', 'copy'] if probe.stdout.strip() in ('', 'aac') \
                    else ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000']
                with tempfile.NamedTemporaryFile(suffix='.mp4') as out:
                    subprocess.run(
                        ['ffmpeg', '-y', '-v', 'error', '-i', src.name,
                         '-c:v', 'copy'] + aud +
                        ['-avoid_negative_ts', 'make_zero',
                         '-movflags', '+faststart', out.name],
                        check=True, timeout=120)
                    fixed = Path(out.name).read_bytes()
                    return fixed if len(fixed) > 1024 else data
        except Exception as e:
            log.warning('clip normalize failed (%s) — uploading original', e)
            return data

    # ── jobs ─────────────────────────────────────────────────────────────

    @staticmethod
    def _slug(label):
        keep = [c if c.isalnum() else '_' for c in (label or 'play')[:40]]
        return ''.join(keep).strip('_') or 'play'

    def process_job(self, cfg, base, key, job, skew):
        """Cut + store + upload one clip. True when settled (uploaded or
        reported failed), False to retry on a later poll."""
        cid = job['id']
        now = time.time()
        # Server epochs → local recording clock. NTP keeps both close;
        # `skew` (server_now − local_now at poll time) absorbs the rest.
        start = job['start'] - skew
        end = job['end'] - skew
        if now < (job['not_before'] - skew):
            return False                   # recording hasn't flushed past it
        requested = end - start
        try:
            data = self._fetch_clip(cfg, start, requested)
            if len(data) < 1024:
                raise RuntimeError('playback returned an empty clip')
            # MediaMTX can return a short file when the recording isn't
            # fully flushed for this window; accepting it silently would
            # upload a permanently truncated clip (right metadata, wrong
            # video). Treat it as "not cuttable yet".
            actual = self._clip_duration(data)
            if actual is not None and actual < requested * 0.8:
                raise RuntimeError(
                    f'playback returned a truncated clip '
                    f'({actual:.1f}s of {requested:.1f}s requested)')
            # …and a clip whose CONTAINER spans the window but whose media
            # has holes (recording gaps from a stream dropout) is just as
            # wrong — the label says a minute, the video holds ten seconds.
            media = self._media_seconds(data)
            if media is not None and media < requested * 0.6:
                raise RuntimeError(
                    f'recording has holes under this window '
                    f'({media:.1f}s of media across {requested:.1f}s)')
        except Exception as e:
            # the playback server's 404 means ONE thing: no recording
            # covers this window — the camera wasn't feeding the box
            # when the play happened. Say that, not "HTTP Error 404".
            why = str(e)
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                why = ('no recording covers this play — the camera was '
                       'not connected when it happened')
            if now - end > CUT_GIVE_UP_AFTER:
                log.error('giving up on %s: %s', cid, why)
                self._mark_failed(base, key, cid, f'cut failed: {why}')
                self.status.failed += 1
                self.status.last_error = why
                return True
            log.info('clip %s not cuttable yet (%s) — will retry', cid, why)
            return False

        data = self._fix_audio_for_ios(data)

        local = CLIPS_DIR / f'{cid}_{self._slug(job.get("label"))}.mp4'
        try:
            CLIPS_DIR.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
        except OSError as e:
            log.warning('local archive write failed (%s) — uploading anyway', e)

        try:
            out = self._throttled_upload(base, key, cid, data)
            log.info('uploaded %s (%.1f MB) — %s', cid, len(data) / 1048576,
                     job.get('label', ''))
            self.status.uploaded += 1
            return bool(out.get('ok'))
        except Exception as e:
            log.warning('upload failed for %s: %s — will retry', cid, e)
            self.status.last_error = str(e)
            return False

    def prune(self):
        cutoff = time.time() - RETAIN_DAYS * 86400
        try:
            for f in CLIPS_DIR.glob('*.mp4'):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
        except OSError:
            pass

    def poll_once(self):
        """One poll cycle. Returns the number of jobs seen (0 when idle or
        unpaired)."""
        cfg = self.cfg_load()
        base, key = cloud_link(cfg)
        if not base:
            # Not paired yet — the box still streams; clips just wait.
            now = time.time()
            if now - self._last_unpaired_log > _UNPAIRED_LOG_EVERY:
                log.info('not paired to PlayCall yet — clip polling idle')
                self._last_unpaired_log = now
            self.status.write(0)
            return 0
        if not cfg.get('local_ingest_key'):
            self.status.write(0)
            return 0
        # THERMAL SELF-DEFENSE: cutting and uploading clips is the
        # heaviest thing this box does, and a Pi past ~80 °C throttles
        # everything — including the radar read and the live push. An
        # overheat took a box down mid-setup in the field. Clips are the
        # one workload that can wait, so above the line we skip the cut
        # cycle entirely and let the jobs sit server-side until it cools.
        _t = system.cpu_temp()
        if _t is not None and _t >= CLIP_HOT_C:
            if time.time() - self._last_hot_log > 60:
                self._last_hot_log = time.time()
                log.warning('%.0f°C — pausing clip cutting until the box '
                            'cools (streaming and radar keep running)', _t)
            self.status.write(0)
            return 0
        out = self._api(base, key, '/api/pi/clips/jobs')
        skew = float(out.get('now', time.time())) - time.time()
        jobs = [j for j in out.get('jobs', []) if not j.get('hold')]
        for job in jobs:
            self.process_job(cfg, base, key, job, skew)
        self.status.write(len(jobs))
        return len(jobs)

    def run_forever(self):
        log.info('clipper up — playback=%s poll=%ss upload_cap=%sB/s clips=%s',
                 PLAYBACK_URL, POLL_SECONDS, UPLOAD_BPS or '∞', CLIPS_DIR)
        last_prune = 0.0
        while self.running:
            try:
                self.poll_once()
            except urllib.error.URLError as e:
                log.warning('cloud unreachable (%s) — jobs wait server-side', e)
            except Exception:
                log.exception('poll cycle failed')
            if time.time() - last_prune > _PRUNE_EVERY:
                self.prune()
                last_prune = time.time()
            time.sleep(POLL_SECONDS)

    def stop(self):
        self.running = False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    clipper = Clipper()
    import signal

    def _stop(*a):
        clipper.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    clipper.run_forever()


if __name__ == '__main__':
    main()
