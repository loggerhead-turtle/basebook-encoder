#!/usr/bin/env python3
"""YouTube push leg — python port of the original stream_relay
youtube_push.sh so cloud_link can restart/repoint it dynamically
(`systemctl restart playcall-encoder-youtube` picks up the config's new
push target). The shell script is still shipped for manual/debug use.

Behavior preserved from the shell version:
  * default is video -c copy — byte-identical, ~0 CPU, the stable path;
  * each attempt PROBES the audio codec over loopback RTSP first: AAC
    (Mevo) → read over RTMP + -c:a copy; anything else (phone/Opus, which
    classic RTMP cannot carry at all) → read over RTSP + transcode audio
    to AAC 128k;
  * exits/reconnects freely with backoff — a flaky uplink only interrupts
    YouTube; the local recording lives in MediaMTX and never depends on
    this leg.

Additions: ffmpeg -progress parsing → live kbps, and a status JSON
(state dir push.json) that cloud_link folds into the heartbeat.
"""

import json
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from . import config, system

log = logging.getLogger('youtube_push')

RECONNECT_BASE = 3        # seconds; doubles per consecutive fast failure
RECONNECT_MAX = 30


def rtmp_in(cfg):
    return f"rtmp://127.0.0.1:1935/live/{cfg['local_ingest_key']}"


def rtsp_in(cfg):
    return f"rtsp://127.0.0.1:8554/live/{cfg['local_ingest_key']}"


def probe_codecs(cfg, runner=None):
    """(video, audio) codecs of the currently-published stream via loopback
    RTSP (RTSP sees the true track list; RTMP silently DROPS any track it
    cannot carry — an Opus audio track, and H.265 video: MediaMTX logs
    'skipping track (H265)' and hands the reader audio only). Both empty
    when nobody is publishing yet."""
    runner = runner or system.run
    r = runner(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries', 'stream=codec_type,codec_name',
                '-of', 'json', rtsp_in(cfg)],
               timeout=15)
    if r.returncode != 0:
        return '', ''
    try:
        streams = json.loads(r.stdout or '{}').get('streams') or []
    except (ValueError, AttributeError):
        return '', ''
    vcodec = acodec = ''
    for s in streams:
        if s.get('codec_type') == 'video' and not vcodec:
            vcodec = s.get('codec_name') or ''
        elif s.get('codec_type') == 'audio' and not acodec:
            acodec = s.get('codec_name') or ''
    return vcodec, acodec


def probe_audio_codec(cfg, runner=None):
    """Kept for parity with the shell script's audio-only probe."""
    return probe_codecs(cfg, runner)[1]


def push_bitrate(cfg, hw=None):
    """The kbps this box should TRANSCODE the push to, or 0 for copy.

    Non-zero only when both halves are true: somebody configured a
    bitrate AND this box owns a hardware encoder. A Pi 5 has none — its
    push is a stream copy by necessity — so a bitrate configured there
    (a setting synced from the cloud to a mixed fleet, say) degrades to
    copy with one log line, never an ffmpeg error loop. The local
    recording is untouched either way: MediaMTX records the camera's own
    stream, so clips keep full quality while YouTube gets a rate the
    field uplink can carry."""
    try:
        kbps = int(cfg.get('push_bitrate_kbps') or 0)
    except (TypeError, ValueError):
        kbps = 0
    if kbps <= 0:
        return 0
    kbps = max(1000, min(12000, kbps))
    hw = system.hw_encoder() if hw is None else hw
    if hw != 'vaapi':
        return 0
    return kbps


def push_codec(cfg, caps=None):
    """'hevc' only when it was ASKED FOR and this box PROVED it can —
    hardware encode plus an ffmpeg that muxes HEVC into flv (enhanced
    RTMP). Everything else is 'h264': the battle-tested path every
    YouTube ingest accepts, and what a mixed fleet safely degrades to."""
    if (cfg.get('push_codec') or 'h264') != 'hevc':
        return 'h264'
    caps = system.hw_encoders() if caps is None else caps
    return 'hevc' if caps.get('hevc') else 'h264'


def build_ffmpeg_cmd(cfg, acodec, push_url, hw=None, caps=None, vcodec=''):
    # The INPUT leg must carry every track. Loopback RTMP only carries
    # H.264 + AAC — MediaMTX drops anything else from an RTMP read
    # ('skipping track (H265)'), which handed ffmpeg an audio-only
    # stream from an HEVC camera and crash-looped the push while the
    # local ingest was perfectly healthy. So: RTMP only when BOTH tracks
    # are RTMP-safe; any other camera (HEVC video, Opus audio) is read
    # over loopback RTSP, which carries the true track list.
    rtmp_safe = (not acodec or acodec == 'aac') \
        and (not vcodec or vcodec == 'h264')
    if rtmp_safe:
        input_args = ['-rw_timeout', '10000000', '-i', rtmp_in(cfg)]
    else:
        input_args = ['-rtsp_transport', 'tcp', '-i', rtsp_in(cfg)]
    audio_args = (['-c:a', 'copy'] if not acodec or acodec == 'aac'
                  else ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000'])
    kbps = push_bitrate(cfg, hw=hw)
    if kbps:
        # QuickSync via VA-API: decode on CPU (cheap), upload frames to
        # the GPU, encode there. CBR-ish with a 2x buffer and a 2 s GOP —
        # what YouTube's ingest guidance wants for live. HEVC rides
        # enhanced RTMP (ffmpeg writes the hvc1 fourcc into flv) at the
        # same bitrate — ~35% more quality per bit on the same uplink.
        enc = ('hevc_vaapi' if push_codec(cfg, caps=caps) == 'hevc'
               else 'h264_vaapi')
        video_args = ['-vaapi_device', '/dev/dri/renderD128',
                      '-vf', 'format=nv12,hwupload',
                      '-c:v', enc,
                      '-b:v', f'{kbps}k', '-maxrate', f'{kbps}k',
                      '-bufsize', f'{kbps * 2}k', '-g', '60']
    else:
        video_args = ['-c:v', 'copy']
    return (['ffmpeg', '-hide_banner', '-loglevel', 'warning',
             '-progress', 'pipe:1', '-nostats']
            + input_args + video_args + audio_args
            + ['-f', 'flv', push_url])


def parse_progress_line(line, state):
    """Fold one `-progress` key=value line into state; returns kbps when a
    progress block completes (ffmpeg emits total_size + out_time_us every
    ~half second)."""
    line = line.strip()
    if '=' not in line:
        return None
    k, _, v = line.partition('=')
    state[k] = v
    if k != 'progress':
        return None
    try:
        size = int(state.get('total_size', 0))
        t_us = int(state.get('out_time_us') or state.get('out_time_ms') or 0)
    except ValueError:
        return None
    prev_size, prev_t = state.get('_prev', (0, 0))
    state['_prev'] = (size, t_us)
    dt = (t_us - prev_t) / 1e6
    if dt <= 0:
        return None
    return int((size - prev_size) * 8 / dt / 1000)     # kbps


class StatusWriter:
    """Atomic push-status JSON the cloud_link heartbeat reads."""

    def __init__(self, path=None):
        self.path = Path(path or config.state_dir() / 'push.json')
        self.reconnect_times = deque(maxlen=100)
        self.stderr_tail = deque(maxlen=40)

    def write(self, connected, kbps=None):
        data = {'connected': connected, 'kbps': kbps,
                'updated': time.time(),
                'reconnect_times': list(self.reconnect_times)[-50:],
                'stderr_tail': list(self.stderr_tail)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass


class YouTubePusher:
    def __init__(self, cfg_load=config.load, runner=None, status=None):
        self.cfg_load = cfg_load
        self.runner = runner or system.run
        self.status = status or StatusWriter()
        self.running = True
        self.proc = None

    def push_url(self):
        from .provisioning import youtube_push_url
        return youtube_push_url(self.cfg_load())

    def run_once(self):
        """One ffmpeg attempt. Returns seconds the attempt survived."""
        cfg = self.cfg_load()
        url = self.push_url()
        if not url or not cfg.get('youtube', {}).get('key'):
            self.status.write(False)
            return 0
        vcodec, acodec = probe_codecs(cfg, self.runner)
        cmd = build_ffmpeg_cmd(cfg, acodec, url, vcodec=vcodec)
        kbps = push_bitrate(cfg)
        want = int(cfg.get('push_bitrate_kbps') or 0)
        if want > 0 and not kbps:
            # configured for a box class this box is not — say so once
            # per attempt, then do the right thing anyway
            log.info(f'push_bitrate_kbps={want} configured but this box '
                     'has no hardware encoder — pushing source copy')
        codec = push_codec(cfg) if kbps else ''
        if kbps and (cfg.get('push_codec') or 'h264') == 'hevc' \
                and codec != 'hevc':
            log.info('push_codec=hevc configured but this box cannot '
                     'encode/mux HEVC — transcoding to H.264 instead')
        if vcodec and vcodec != 'h264' and not kbps:
            # copying non-H.264 into flv needs enhanced-RTMP muxing; a
            # too-old ffmpeg will fail here, so leave a breadcrumb that
            # names the camera codec instead of a bare reconnect loop
            log.info(f'camera sends {vcodec} and no transcode is '
                     'configured — copying it to YouTube as-is (needs '
                     'an enhanced-RTMP-capable ffmpeg)')
        log.info('push start ('
                 + f"in={vcodec or 'unknown'}/{acodec or 'assume-aac'}"
                 + (' via rtsp' if '-rtsp_transport' in cmd else ' via rtmp')
                 + ', video='
                 + (f'{kbps}k {codec} transcode' if kbps else 'copy') + ')')
        started = time.monotonic()
        state = {}
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        import threading

        def _drain_stderr():
            for line in self.proc.stderr:
                self.status.stderr_tail.append(line.rstrip())
        threading.Thread(target=_drain_stderr, daemon=True).start()

        for line in self.proc.stdout:
            kbps = parse_progress_line(line, state)
            if kbps is not None:
                self.status.write(True, kbps)
            if not self.running:
                self.proc.terminate()
                break
        self.proc.wait()
        self.status.write(False)
        return time.monotonic() - started

    def run_forever(self):
        backoff = RECONNECT_BASE
        while self.running:
            alive = self.run_once()
            if not self.running:
                break
            # A push that survived a while resets the backoff; consecutive
            # instant failures (no publisher yet / bad key) back off so we
            # don't hammer YouTube.
            backoff = RECONNECT_BASE if alive > 30 else \
                min(RECONNECT_MAX, backoff * 2)
            self.status.reconnect_times.append(time.time())
            log.info(f'push ended — reconnecting in {backoff}s')
            time.sleep(backoff)

    def stop(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    pusher = YouTubePusher()
    import signal

    def _stop(*a):
        pusher.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    pusher.run_forever()


if __name__ == '__main__':
    main()
