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
import urllib.request
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from . import config, system

MEDIAMTX_API = os.environ.get('MEDIAMTX_API', 'http://127.0.0.1:9997').rstrip('/')


def _http_json(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode() or '{}')

log = logging.getLogger('youtube_push')

RECONNECT_BASE = 3        # seconds; doubles per consecutive fast failure
RECONNECT_MAX = 30
# How often the camera's audio is asked about while a push runs. On
# 21 Sep 2026 (Maeser) the camera's AAC track was named, copied, and
# for half-hour stretches carried NO packets: YouTube reported "audio
# bitrate (0)" and held the broadcast at liveStarting for three hours
# with the picture arriving the whole time — it will not start a stream
# without audio. A copied track cannot fill a gap the camera leaves, so
# the box now watches for one and generates silence in its place until
# the camera's sound comes back.
AUDIO_RECHECK_S = 60



def rtmp_in(cfg):
    return f"rtmp://127.0.0.1:1935/live/{cfg['local_ingest_key']}"


def rtsp_in(cfg):
    return f"rtsp://127.0.0.1:8554/live/{cfg['local_ingest_key']}"


def probe_streams(cfg, runner=None):
    """(video, audio, audio_detail) of the currently-published stream via
    loopback RTSP (RTSP sees the true track list; RTMP silently DROPS any
    track it cannot carry — an Opus audio track, and H.265 video: MediaMTX
    logs 'skipping track (H265)' and hands the reader audio only). All
    empty when nobody is publishing yet.

    audio_detail is {'codec', 'sample_rate', 'channels'} or None: what the
    camera is actually sending, so the box can SAY it. On 15 Sep 2026
    YouTube reported audio bitrate 0 from a camera that was sending AAC,
    the desk told the operator to check the camera's audio source, and
    the operator was right that it was fine. The box knew the track was
    there and never said so."""
    runner = runner or system.run
    r = runner(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                '-show_entries',
                'stream=codec_type,codec_name,sample_rate,channels',
                '-of', 'json', rtsp_in(cfg)],
               timeout=15)
    if r.returncode != 0:
        return '', '', None
    try:
        streams = json.loads(r.stdout or '{}').get('streams') or []
    except (ValueError, AttributeError):
        return '', '', None
    vcodec = acodec = ''
    detail = None
    for s in streams:
        if s.get('codec_type') == 'video' and not vcodec:
            vcodec = s.get('codec_name') or ''
        elif s.get('codec_type') == 'audio' and not acodec:
            acodec = s.get('codec_name') or ''
            try:
                detail = {'codec': acodec,
                          'sample_rate': int(s.get('sample_rate') or 0),
                          'channels': int(s.get('channels') or 0)}
            except (TypeError, ValueError):
                detail = {'codec': acodec, 'sample_rate': 0, 'channels': 0}
    return vcodec, acodec, detail


def probe_codecs(cfg, runner=None):
    """(video, audio) — see probe_streams."""
    v, a, _d = probe_streams(cfg, runner)
    return v, a


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
    # The maps are EXPLICIT: first video, first audio if there is one
    # ('?' makes a camera with no audio track not fatal). Default stream
    # selection usually does this, and 'usually' is the problem — it is
    # one of the ways an audio track that was there can fail to reach
    # the output with nothing in the log.
    map_args = ['-map', '0:v:0', '-map', '0:a:0?']
    # A camera with NO audio track gets one: silence, generated here.
    # YouTube will not start a broadcast without audio (17 Sep 2026:
    # a Mevo with its mic off, video active at YouTube, "audio bitrate
    # (0)", and the game stuck at 'armed' — the box knew the track was
    # missing and still sent a stream YouTube would never start). Only
    # on a POSITIVE probe: video named, audio absent. An empty probe is
    # unknown, not silent, and keeps the optional map — a real track
    # must never be talked over.
    silent = bool(vcodec) and not acodec
    # Audio with a track behind it is DECODED AND RE-ENCODED here, AAC
    # included — see live_push.AUDIO_OUT. It was copied for a month on
    # the reasoning that a re-encode of a good track buys nothing; what
    # the copy bought was three games of "audio bitrate (0)" at YouTube
    # and, on 21 Sep 2026, a track the stream server's ffmpeg could not
    # describe, while the clips cut from the same feed had sound. A copy
    # carries the camera's audio header; a re-encode writes a standard
    # one (AAC-LC, 48 kHz, stereo) on a steady clock, and the status
    # block below says which the box sent.
    if silent:
        input_args = input_args + ['-f', 'lavfi', '-i',
                                   'anullsrc=channel_layout=stereo:sample_rate=48000']
        map_args = ['-map', '0:v:0', '-map', '1:a:0']
        audio_args = ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
                      '-ac', '2', '-shortest']    # ends with the picture
    else:
        # Decoded and re-encoded, AAC or not — see live_push.AUDIO_OUT for
        # why a copy of a named AAC track is the thing that kept losing
        # the sound (21 Sep 2026: "audio bitrate (0)" at YouTube and a
        # track the stream server could not describe, in the same game,
        # with sound in the clips the whole time).
        audio_args = ['-c:a', 'aac', '-b:a', '128k', '-ar', '48000',
                      '-ac', '2', '-af', 'aresample=async=1:first_pts=0']
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
            + input_args + map_args + video_args + audio_args
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
              in_codec=None, waiting=None):
        if codec is not None:
            self.codec = codec
        if in_codec is not None:
            self.in_codec = in_codec
        if speed is not None:
            self.speed = speed
        # 'waiting': why a disconnected push is disconnected, for the
        # heartbeat and the desk — 'camera' means nobody is publishing to
        # this box, which is not a YouTube problem and must not read as one.
        # 'audio': what the camera sends and what the push does with it,
        # so 'YouTube hears no audio' can be met with 'the box is sending
        # AAC 48 kHz stereo' instead of 'check the camera'.
        data = {'connected': connected, 'kbps': kbps, 'waiting': waiting,
                'audio': getattr(self, 'audio', None),
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
    def __init__(self, cfg_load=config.load, runner=None, status=None, http=None):
        self.cfg_load = cfg_load
        self.runner = runner or system.run
        self.status = status or StatusWriter()
        self.http = http or _http_json
        self.running = True
        self.proc = None
        self.fell_back = False

    def push_url(self):
        from .provisioning import youtube_push_url
        return youtube_push_url(self.cfg_load())

    def publisher_ready(self, cfg):
        """Is anything actually publishing to this box's own MediaMTX?

        True, False, or None when the API could not be asked — and None
        must fail OPEN: an API hiccup on a box mid-game must never take
        down a working push. Only a definite 'nobody is publishing' holds
        the dial.
        """
        want = f"live/{cfg.get('local_ingest_key', '')}"
        try:
            data = self.http(f'{MEDIAMTX_API}/v3/paths/list')
        except Exception:
            return None
        for item in (data or {}).get('items') or []:
            if item.get('name') == want:
                return bool(item.get('ready'))
        return False

    def run_once(self):
        """One ffmpeg attempt. Returns seconds the attempt survived."""
        cfg = self.cfg_load()
        url = self.push_url()
        if not url or not cfg.get('youtube', {}).get('key'):
            self.status.write(False)
            return 0
        # NOBODY PUBLISHING, NO DIAL. This used to start ffmpeg anyway:
        # ffmpeg died on the RTSP 404 in 200 ms, the loop retried in 30 s,
        # and every retry was a connect-then-vanish against YouTube's
        # ingest — nine in five minutes on 15 Sep 2026, on a box whose
        # last input was six days old. YouTube marked the broadcast
        # starved (correctly), and worse, each blink set push.connected
        # long enough for the website to decide this box OWNED the
        # broadcast, hand BaseStream an empty URL, and stand its own push
        # down: a phone that was carrying a perfectly good picture went
        # dark on YouTube because an empty box kept knocking. The red
        # light the operator saw was the retry loop, not the camera.
        ready = self.publisher_ready(cfg)
        if ready is False:
            log.info('no camera is publishing to this box yet — not '
                     'dialling YouTube until one is')
            self.status.write(False, waiting='camera')
            return 0
        vcodec, acodec, adetail = probe_streams(cfg, self.runner)
        if not (vcodec and acodec):
            log.info('could not read the camera tracks (nobody '
                     'publishing yet, or the probe timed out) — reading '
                     'over RTSP, which carries every track whatever the '
                     'camera turns out to be')
        # A named track is only worth copying if it carries something.
        # 'silent' here means the box makes the audio itself; 'camera'
        # that the camera's own track rides across; 'unknown' that the
        # question could not be asked (a probe failure is never a reason
        # to talk over a real track).
        self.audio_verdict = 'unknown'
        self.audio_restart = False
        cam_acodec = acodec
        if acodec:
            sound = self._audio_sound(cfg)
            if sound is False:
                log.info(f'audio track ({acodec}) is declared but carries '
                         'nothing YouTube can use — no packets, or only '
                         'silence frames. Sending silence in its place so '
                         'the broadcast starts; asking the camera again '
                         f'every {AUDIO_RECHECK_S} s')
                acodec = ''
                self.audio_verdict = 'silent'
            else:
                self.audio_verdict = 'camera'
        self.status.audio = {
            'in': acodec or '',
            'sample_rate': (adetail or {}).get('sample_rate') or 0,
            'channels': (adetail or {}).get('channels') or 0,
            # 'aac': the camera's sound, decoded and re-encoded here (a
            # copy was never safe — see live_push.AUDIO_OUT); 'silence'
            # is the box's own track, generated because the camera has
            # none (video named, audio absent); '' means the probe could
            # not read the camera at all.
            'out': ('aac' if acodec else ('silence' if vcodec else '')),
            'mapped': bool(acodec or vcodec),
        }
        if cam_acodec and not acodec:
            # the camera HAS a track; it is the box's choice to replace it
            self.status.audio['in'] = cam_acodec
            self.status.audio['why'] = 'camera track carries no sound'
        log.info('audio: ' + (f"{acodec} {self.status.audio['sample_rate']} Hz "
                              f"{self.status.audio['channels']}ch → "
                              f"{self.status.audio['out']}" if acodec
                              else ('NO AUDIO TRACK from the camera — sending '
                                    'silence so YouTube will start; for sound, '
                                    'turn on the mic in the camera app' if vcodec
                                    else 'camera tracks unknown')))
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
        self._audio_next_t = time.monotonic() + AUDIO_RECHECK_S
        for line in self.proc.stdout:
            kbps = parse_progress_line(line, state)
            if kbps is not None:
                self.status.write(True, kbps, speed=progress_speed(state))
            if not self.running:
                self.proc.terminate()
                break
            if self._audio_watch(cfg):
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

    def _audio_sound(self, cfg):
        """True / False / None (could not ask): does the camera's audio
        track carry real frames right now? Shared with the BaseStream
        push, which has judged its track this way since 1.2.91."""
        from .live_push import audio_has_sound
        return audio_has_sound(cfg, self.runner)

    def _audio_packets(self, cfg):
        """The sizes of a few seconds of the camera's audio packets, or
        None when the question could not be asked."""
        from .live_push import _audio_frame_sizes
        return _audio_frame_sizes(cfg, self.runner)

    def _audio_watch(self, cfg):
        """Once a minute, the question YouTube is asking: is audio flowing?

        Generating silence for a camera whose track went quiet: the
        camera's sound coming back ends this push so the next one carries
        it. Copying the camera's track: that track carrying NO packets at
        all ends this push so the next one generates silence — the gap
        YouTube will not start through (21 Sep 2026). A track that merely
        went quiet is left alone: a quiet inning is not a reason to
        replace a mic. Returns True when the push should end now."""
        now = time.monotonic()
        if now < getattr(self, '_audio_next_t', 0):
            return False
        self._audio_next_t = now + AUDIO_RECHECK_S
        verdict = getattr(self, 'audio_verdict', 'unknown')
        if verdict == 'silent':
            if self._audio_sound(cfg):
                log.info('sound has arrived on the camera\'s audio track — '
                         'restarting the push with it')
                self.audio_restart = True
                return True
        elif verdict == 'camera':
            sizes = self._audio_packets(cfg)
            if sizes is not None and not sizes:
                log.warning('the camera\'s audio track has stopped carrying '
                            'packets — YouTube reports this as "audio '
                            'bitrate (0)" and will not start a broadcast '
                            'through it. Restarting the push with silence '
                            'in its place; the camera\'s sound is asked for '
                            f'again every {AUDIO_RECHECK_S} s')
                self.audio_restart = True
                return True
        return False

    def run_forever(self):
        backoff = RECONNECT_BASE
        while self.running:
            alive = self.run_once()
            if not self.running:
                break
            # A push that survived a while resets the backoff; consecutive
            # instant failures (no publisher yet / bad key) back off so we
            # don't hammer YouTube. A push the box ended on purpose (a
            # decode fallback, an audio change) is not a failure at all.
            backoff = RECONNECT_BASE if alive > 30 or self.fell_back \
                or getattr(self, 'audio_restart', False) else \
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
