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


def probe_audio_codec(cfg, runner=None):
    """Audio codec of the currently-published stream via loopback RTSP
    (RTSP sees the true track list; RTMP would just drop an Opus track).
    Empty string when nobody is publishing yet."""
    runner = runner or system.run
    r = runner(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-select_streams', 'a:0', '-show_entries',
                'stream=codec_name', '-of', 'csv=p=0', rtsp_in(cfg)],
               timeout=15)
    return (r.stdout or '').strip() if r.returncode == 0 else ''


def build_ffmpeg_cmd(cfg, acodec, push_url):
    if not acodec or acodec == 'aac':
        input_args = ['-rw_timeout', '10000000', '-i', rtmp_in(cfg)]
        audio_args = ['-c:a', 'copy']
    else:
        input_args = ['-rtsp_transport', 'tcp', '-i', rtsp_in(cfg)]
        audio_args = ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000']
    return (['ffmpeg', '-hide_banner', '-loglevel', 'warning',
             '-progress', 'pipe:1', '-nostats']
            + input_args + ['-c:v', 'copy'] + audio_args
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
        acodec = probe_audio_codec(cfg, self.runner)
        cmd = build_ffmpeg_cmd(cfg, acodec, url)
        log.info(f"push start (audio={acodec or 'assume-aac'})")
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
