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


def effective_video(cfg, vcodec='', hw=None, caps=None):
    """(kbps, encoder codec) the push will really use — (0, '') for copy.

    'Source quality' is a copy, byte-identical, ~0 CPU — including an
    HEVC camera, which rides enhanced RTMP as HEVC. Whether YouTube can
    read that is not this box's call to make blind: the site gates GO
    LIVE on YouTube's own health report and, if YouTube starves on the
    HEVC, sets this box to H.264 itself (the assignment poll picks it up
    inside ~5 s). So the box sends the best thing it has and the site
    holds the fallback, instead of the box quietly transcoding what the
    person chose not to."""
    hw = system.hw_encoder() if hw is None else hw
    kbps = push_bitrate(cfg, hw=hw)
    if kbps:
        return kbps, push_codec(cfg, caps=caps)
    return 0, ''


def outgoing_codec(cfg, vcodec='', hw=None, caps=None):
    """The codec that actually leaves for YouTube: the transcode target,
    or the camera's own when copying. Reported in the heartbeat so the
    site's go-live gate reasons about the truth, not the setting."""
    kbps, codec = effective_video(cfg, vcodec=vcodec, hw=hw, caps=caps)
    return codec if kbps else (vcodec or '')


def build_ffmpeg_cmd(cfg, acodec, push_url, hw=None, caps=None, vcodec='',
                     hw_decode=True):
    # The INPUT leg must carry every track. Loopback RTMP only carries
    # H.264 + AAC — MediaMTX drops anything else from an RTMP read
    # ('skipping track (H265)'), which handed ffmpeg an audio-only
    # stream from an HEVC camera and crash-looped the push while the
    # local ingest was perfectly healthy.
    #
    # So RTMP is taken only on POSITIVE confirmation of both tracks.
    # UNKNOWN is not safe — it is the case that broke: a probe that came
    # back empty (nobody publishing yet, or ffprobe timed out) used to
    # read the empty string as 'no obstacle' and pick RTMP, so an HEVC
    # camera died on an audio-only input in 200 ms, every 30 s, for a
    # whole pregame (Provo, 9/8: 'in=unknown … via rtmp', 7 minutes of
    # it, until one probe finally landed and the same box went straight
    # to work over RTSP). RTSP carries the true track list, so it is
    # what we fall back TO, never what we fall back FROM.
    rtmp_safe = vcodec == 'h264' and acodec == 'aac'
    if rtmp_safe:
        input_args = ['-rw_timeout', '10000000', '-i', rtmp_in(cfg)]
    else:
        input_args = ['-rtsp_transport', 'tcp', '-i', rtsp_in(cfg)]
    # Same rule for audio: copy only what we KNOW is AAC. Copying an
    # unidentified track into flv is how a phone's Opus killed the push;
    # a camera with no audio at all ignores the encoder option anyway.
    audio_args = (['-c:a', 'copy'] if acodec == 'aac'
                  else ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000'])
    kbps, codec = effective_video(cfg, vcodec=vcodec, hw=hw, caps=caps)
    if kbps:
        # QuickSync via VA-API, both halves on the chip. The decode used
        # to run on the CPU ('cheap') — which it is for H.264 and is NOT
        # for a Mevo's 1080p HEVC on an N150: the transcode ran at 0.63×
        # real time, YouTube received 2940 kbps of a 4665 kbps picture and
        # called it starved, on a gigabit line (Maeser, 9/9). Decoding on
        # the chip keeps the whole pipeline on the GPU; hw_decode=False is
        # the CPU path kept for a stream the chip refuses (run_once falls
        # back to it after one fast death). CBR-ish, 2x buffer, 2 s GOP —
        # what YouTube's ingest guidance wants for live. HEVC rides
        # enhanced RTMP at the same bitrate.
        enc = 'hevc_vaapi' if codec == 'hevc' else 'h264_vaapi'
        if hw_decode:
            input_args = ['-hwaccel', 'vaapi',
                          '-hwaccel_output_format', 'vaapi',
                          '-vaapi_device', '/dev/dri/renderD128'] + input_args
            video_args = ['-vf', 'scale_vaapi=format=nv12',
                          '-c:v', enc,
                          '-b:v', f'{kbps}k', '-maxrate', f'{kbps}k',
                          '-bufsize', f'{kbps * 2}k', '-g', '60']
        else:
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


def progress_speed(state):
    """ffmpeg's own 'speed=0.63x' from the progress block, as a float,
    or None. Below 1.0 the transcode is not keeping up with the camera —
    the ONE number that says so, and the kbps above cannot: it is bytes
    per media-second, not per wall-second, which is exactly how a box
    reported 4665 kbps while YouTube received 2940."""
    v = str(state.get('speed') or '').strip().rstrip('x')
    try:
        return float(v) if v else None
    except ValueError:
        return None


class StatusWriter:
    """Atomic push-status JSON the cloud_link heartbeat reads."""

    def __init__(self, path=None):
        self.path = Path(path or config.state_dir() / 'push.json')
        self.reconnect_times = deque(maxlen=100)
        self.stderr_tail = deque(maxlen=40)

    def write(self, connected, kbps=None, codec=None, speed=None,
              in_codec=None):
        if codec is not None:
            self.codec = codec
        if in_codec is not None:
            self.in_codec = in_codec
        if speed is not None:
            self.speed = speed
        data = {'connected': connected, 'kbps': kbps,
                'codec': getattr(self, 'codec', ''),
                'in_codec': getattr(self, 'in_codec', ''),
                'speed': getattr(self, 'speed', None) if connected else None,
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


# A hardware decode that LIVES but decodes nothing. Maeser, 9/9 15:58:
# ffmpeg stayed up on VAAPI HEVC with "hardware accelerator failed to
# decode picture" on every frame and 0 bytes out — alive, so the
# fast-death fallback never fired, and YouTube saw nothing. Either
# symptom below turns the chip off for the next attempt.
HW_DECODE_ERRORS_MAX = 25      # stderr decode failures in one attempt
HW_NO_OUTPUT_SECS = 20         # connected this long with no bytes out
_HW_DECODE_ERROR_MARKS = ('hardware accelerator failed to decode',
                          'Failed to end picture decode')


class YouTubePusher:
    def __init__(self, cfg_load=config.load, runner=None, status=None):
        self.cfg_load = cfg_load
        self.runner = runner or system.run
        self.status = status or StatusWriter()
        self.running = True
        self.proc = None
        self.fell_back = False

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
        if not (vcodec and acodec):
            log.info('could not read the camera tracks (nobody '
                     'publishing yet, or the probe timed out) — reading '
                     'over RTSP, which carries every track whatever the '
                     'camera turns out to be')
        hw_decode = getattr(self, 'hw_decode', True)
        cmd = build_ffmpeg_cmd(cfg, acodec, url, vcodec=vcodec,
                               hw_decode=hw_decode)
        kbps, codec = effective_video(cfg, vcodec=vcodec)
        want = int(cfg.get('push_bitrate_kbps') or 0)
        if want > 0 and not kbps:
            # configured for a box class this box is not — say so once
            # per attempt, then do the right thing anyway
            log.info(f'push_bitrate_kbps={want} configured but this box '
                     'has no hardware encoder — pushing source copy')
        if kbps and (cfg.get('push_codec') or 'h264') == 'hevc' \
                and codec != 'hevc' and want > 0:
            log.info('push_codec=hevc configured but this box cannot '
                     'encode/mux HEVC — transcoding to H.264 instead')
        if vcodec and vcodec != 'h264' and not kbps:
            log.info(f'camera sends {vcodec} and no transcode is '
                     'configured — copying it to YouTube as-is over '
                     'enhanced RTMP. If YouTube cannot read it, the site '
                     'switches this box to H.264 when GO LIVE is pressed.')
        log.info('push start ('
                 + f"in={vcodec or 'unknown'}/{acodec or 'unknown'}"
                 + (' via rtsp' if '-rtsp_transport' in cmd else ' via rtmp')
                 + ', video='
                 + (f'{kbps}k {codec} transcode' if kbps else 'copy') + ')')
        started = time.monotonic()
        state = {}
        self.status.codec = outgoing_codec(cfg, vcodec=vcodec)
        self.status.in_codec = vcodec or ''
        self.status.speed = None
        transcoding = bool(kbps)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        import threading

        hw_errors = [0]

        def _drain_stderr():
            for line in self.proc.stderr:
                self.status.stderr_tail.append(line.rstrip())
                if any(m in line for m in _HW_DECODE_ERROR_MARKS):
                    hw_errors[0] += 1
        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()

        self.fell_back = False
        for line in self.proc.stdout:
            kbps = parse_progress_line(line, state)
            if kbps is not None:
                self.status.write(True, kbps, speed=progress_speed(state))
            if not self.running:
                self.proc.terminate()
                break
            if hw_decode and transcoding and not self.fell_back:
                alive_s = time.monotonic() - started
                out_bytes = int(state.get('total_size') or 0)
                if hw_errors[0] >= HW_DECODE_ERRORS_MAX \
                        or (alive_s > HW_NO_OUTPUT_SECS and not out_bytes):
                    # alive and decoding nothing: the chip refused this
                    # stream without dying. Decode on the CPU instead.
                    self.fell_back = True
                    self.hw_decode = False
                    log.warning(
                        'hardware decode is producing nothing '
                        f'({hw_errors[0]} decode errors, {out_bytes} bytes '
                        f'out after {alive_s:.0f}s) — restarting with CPU '
                        'decode')
                    self.proc.terminate()
                    break
        self.proc.wait()
        # Let the stderr reader finish before anyone asks what it saw:
        # on a failure this fast the drain thread has often not run at
        # all yet, and the reason we are about to log would be an empty
        # deque.
        drain.join(timeout=2)
        self.status.write(False)
        alive = time.monotonic() - started
        if alive < 5 and transcoding and hw_decode:
            # the chip refused this stream (a profile it will not decode):
            # the next attempt decodes on the CPU, which is slow for HEVC
            # but is a picture rather than a crash loop
            self.hw_decode = False
            log.warning('hardware decode failed inside 5 s — the next '
                        'attempt decodes on the CPU')
        if alive < 5:
            # A push that dies this fast died on its INPUT, and ffmpeg
            # already said why on stderr — where it stayed, unread, while
            # the journal showed nothing but 'push ended — reconnecting'
            # for seven minutes of pregame. The reason belongs in the log
            # the first time, not after somebody drives to the ballpark.
            for line in list(self.status.stderr_tail)[-4:]:
                log.warning(f'push failed after {alive:.1f}s: {line}')
        return alive

    def run_forever(self):
        backoff = RECONNECT_BASE
        while self.running:
            alive = self.run_once()
            if not self.running:
                break
            # A push that survived a while resets the backoff; consecutive
            # instant failures (no publisher yet / bad key) back off so we
            # don't hammer YouTube.
            backoff = RECONNECT_BASE if alive > 30 or self.fell_back else \
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
