#!/usr/bin/env python3
"""The field Pi as a catcher's ear — comms without a phone.

Runs on the SAME Pi that already shows the play-card board: it reuses
the device key the Pi got when you activated it (/etc/playcall.env), so
there is nothing new to pair with the cloud. It polls
/api/sk/device/comms, finds the team's live game by itself, and SPEAKS
every called pitch ("Curveball — down and away", batter-true in/away)
into a Bluetooth earpiece paired to the Pi. Coach push-to-talk clips
play as recorded audio. PRIVATE AUDIO ONLY — never an open speaker:
pitch calls read aloud at the backstop belong to the other dugout too.

EASY MODE — the local admin page. This script also serves a small
phone-friendly page ON THE PI:

        http://<pi-hostname>.local:8790

Open it from any phone on the same WiFi, enter the 4-digit PIN
(printed by install_comms.sh; also in /etc/playcall.env as
PLAYCALL_COMMS_PIN), and manage everything with buttons: scan for the
earbud, pair/trust/connect it, test the voice, and 🔒 LOCK the
Bluetooth (discoverable off + pairable off) so nobody else at the field
can pair their own headset and listen in. Locking after pairing is the
whole security story — do it.

EVEN EASIER — no PIN at all: team staff get a "⚙ Open settings — no
PIN" button on the team comms page (the same one-click sign-in the
encoder's Settings button uses). The site mints a short-lived nonce,
this box learns it on its own authenticated poll, and the link's
/login?token=… matches the two. The box self-reports its address on
every poll (X-Pi-Hostname / X-Pi-Ip / X-Pi-Comms-Port headers) so the
site always knows where this page lives.

Install (once, on the display Pi):   sudo bash pi/install_comms.sh
"""
import base64
import hmac
import html
import http.server
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

ENV_FILE = '/etc/playcall.env'
# the display app (main.py) keeps its activation here — read it as the
# fallback so a working display box needs no re-activation
DISPLAY_ENV = '/var/lib/playcall/.playcall.env'


def _env_file(path):
    out = {}
    try:
        for line in open(path):
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


_E = {**_env_file(DISPLAY_ENV), **_env_file(ENV_FILE)}
# The service runs as the audio user, which cannot read the root-owned
# env file directly — systemd injects its values as environment vars
# (EnvironmentFile is read by root before privileges drop), so the
# os.environ names are checked FIRST.
BASE = (os.environ.get('PLAYCALL_URL')
        or os.environ.get('PLAYCALL_CLOUD_URL')
        or _E.get('PLAYCALL_CLOUD_URL')
        or 'https://basebook.org').rstrip('/')
KEY = (os.environ.get('PLAYCALL_DEVICE_KEY')
       or os.environ.get('PLAYCALL_API_KEY')
       or _E.get('PLAYCALL_API_KEY') or '')
PIN = (os.environ.get('PLAYCALL_COMMS_PIN')
       or _E.get('PLAYCALL_COMMS_PIN') or '')
PORT = int(os.environ.get('PLAYCALL_COMMS_PORT', '8790'))
POLL_S = 1.5

STATE = {'cloud': 'starting…', 'game': None, 'opponent': '',
         'last_call': '—', 'spoken': 0}
seen = set()
primed = False

# One-click sign-in from the team's comms page: the site mints a nonce,
# we learn it on our own authenticated poll (below), and /login on the
# admin page compares the two. TOK is this boot's session cookie value.
NONCE = None
TOK = secrets.token_urlsafe(16)
HOSTNAME = socket.gethostname()

# The box's NAME (what the coach page shows after "LIVE →") and the
# per-earpiece labels (which bud is the catcher's, which the pitcher's)
# live in the service user's home — the root-owned env file isn't
# writable once privileges drop, and these are field preferences anyway.
NAME_FILE = os.path.expanduser('~/.playcall-comms-name')
EARS_FILE = os.path.expanduser('~/.playcall-comms-ears.json')
ADAPTER_FILE = os.path.expanduser('~/.playcall-comms-adapter')


def box_name():
    try:
        n = open(NAME_FILE).read().strip()
        if n:
            return n[:40]
    except Exception:
        pass
    return HOSTNAME


def ear_labels():
    try:
        return json.load(open(EARS_FILE))
    except Exception:
        return {}


def set_ear_label(mac, label):
    labs = ear_labels()
    if label:
        labs[mac] = label[:20]
    else:
        labs.pop(mac, None)
    try:
        json.dump(labs, open(EARS_FILE, 'w'))
    except Exception:
        pass


def _lan_ip():
    """This box's LAN address (a UDP connect() picks the outbound
    interface without sending a packet) — self-reported on every poll so
    the site can build the one-click settings link."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''


# ── cloud poll + speech ──────────────────────────────────────────────────

_HDR_PLAIN = {'—': '-', '–': '-', '…': '...',
              '‘': "'", '’': "'", '“': '"', '”': '"',
              ' ': ' '}


def hdr(v, limit=120):
    """A value safe to put in an HTTP header. Never raises.

    HTTP header values are latin-1 (http.client encodes them that way),
    and ONE character outside it raises UnicodeEncodeError at send time —
    which does not fail that header, it fails the whole request. The box
    then reports `cloud: unreachable` and stays off the cloud for as long
    as the offending text is on screen.

    Three of the strings this box reports about ITSELF were unsendable:
    'starting…', 'answered — connecting…', and '🎙 LIVE — coach linked'.
    The last one is the worst of them — the box dropped off the cloud for
    exactly as long as a coach was talking to his catcher.

    And the ear names are worse still, because they are not ours: a
    Bluetooth device carries whatever name a phone gave it, and iOS names
    them "Erik's AirPods" with a curly apostrophe (U+2019). Pairing a bud
    could take the box off the cloud until somebody renamed it.

    So: fold the punctuation that has a plain equivalent, drop anything
    else that will not encode, and strip control characters — a device
    name is somebody else's text arriving in a header, and CR/LF in there
    is header injection, not a typo.
    """
    s = str(v if v is not None else '')
    for a, b in _HDR_PLAIN.items():
        s = s.replace(a, b)
    s = s.encode('latin-1', 'ignore').decode('latin-1')
    s = ''.join(c for c in s if c == ' ' or 33 <= ord(c) <= 126
                or 160 <= ord(c) <= 255)
    return s[:limit].strip()


def fetch():
    global NONCE
    # The box reports its OWN readiness on every poll — earpiece
    # connected (and which role it carries), audio engine alive, live
    # link state. Without this the site could only say "a box exists";
    # a coach had to walk to it to learn whether the catcher would hear
    # anything. Cheap: three short headers, no extra request.
    ears = []
    try:
        st = bt_status()
        labs = ear_labels()
        for d in (st.get('connected') or []):
            lab = labs.get(d['mac'].upper()) or labs.get(d['mac']) or ''
            ears.append(lab or d.get('name') or 'bud')
    except Exception:
        pass
    auth = {'X-Api-Key': KEY, 'Authorization': 'Bearer ' + KEY}
    told = {'X-Pi-Hostname': hdr(HOSTNAME, 60), 'X-Pi-Ip': hdr(_lan_ip(), 45),
            'X-Pi-Comms-Port': hdr(PORT, 8),
            'X-Pi-Name': hdr(box_name(), 40),
            'X-Pi-Ears': hdr(','.join(ears), 120),
            'X-Pi-Voice': hdr(RTC_STATE.get('s', ''), 60)}

    def _get(headers):
        req = urllib.request.Request(BASE + '/api/sk/device/comms',
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.load(r)

    try:
        d = _get(dict(auth, **told))
    except UnicodeEncodeError:
        # hdr() should have made this impossible. If something still gets
        # through, the box telling the cloud about itself is the part we
        # give up — never the part where it collects the coach's calls.
        # Telemetry may not be allowed to break the control path.
        d = _get(auth)
    # captured every poll; cleared when the cloud stops offering one
    NONCE = d.get('login_nonce') or None
    return d


_COMBINE = {'slaves': None, 'mod': None}


def _route_to_bud():
    """Point the default audio sink at the Bluetooth bud(s). One bud →
    straight at it. TWO buds on one box (catcher + pitcher on the plate
    Pi) → a combined sink so every word lands in BOTH ears; rebuilt
    whenever the set of connected buds changes (dugout walks). Cheap and
    idempotent; called before every utterance."""
    try:
        out = subprocess.run(['pactl', 'list', 'short', 'sinks'],
                             capture_output=True, text=True,
                             timeout=5).stdout
        sinks = [ln.split('\t')[1] for ln in out.splitlines()
                 if 'bluez' in ln and 'playcall_both' not in ln]
        if not sinks:
            return False
        if len(sinks) == 1:
            subprocess.run(['pactl', 'set-default-sink', sinks[0]],
                           check=False, timeout=5)
            _sink_audible(sinks[0])
            return True
        key = ','.join(sorted(sinks))
        if _COMBINE['slaves'] != key:
            if _COMBINE['mod']:
                subprocess.run(['pactl', 'unload-module', _COMBINE['mod']],
                               check=False, timeout=5)
                _COMBINE['mod'] = None
            r = subprocess.run(
                ['pactl', 'load-module', 'module-combine-sink',
                 'sink_name=playcall_both', 'slaves=' + key],
                capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                _COMBINE['mod'] = r.stdout.strip()
                _COMBINE['slaves'] = key
        subprocess.run(['pactl', 'set-default-sink', 'playcall_both'],
                       check=False, timeout=5)
        for s in sinks:
            _sink_audible(s)
        _sink_audible('playcall_both')
        return True
    except Exception:
        pass
    return False


def _sink_audible(sink):
    """A sink that exists but is muted or at 4% volume is silence with
    extra steps — every route unmutes and sets a solid level."""
    subprocess.run(['pactl', 'set-sink-mute', sink, '0'],
                   check=False, timeout=5)
    subprocess.run(['pactl', 'set-sink-volume', sink, '85%'],
                   check=False, timeout=5)


def _default_sink():
    try:
        r = subprocess.run(['pactl', 'get-default-sink'],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ''


# Piper (neural TTS, installed by install_comms.sh) sounds like a
# person; espeak-ng sounds like 1982 and stays only as the fallback.
# The coach picks the VOICE the ear speaks with. Curated Piper voices
# (all en_US, medium quality — ~60 MB each, fetched on selection and
# kept under the service user's home so no root is needed):
VOICE_DIR = os.path.expanduser('~/.playcall-piper')
VOICES = {
    'lessac': ('Lessac — clear, neutral', 'lessac'),
    'amy': ('Amy — bright female', 'amy'),
    'ryan': ('Ryan — deep male', 'ryan'),
    'joe': ('Joe — plain male', 'joe'),
}
VOICE_DL = {'busy': False, 'msg': ''}


def _voice_base(name):
    return ('https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0'
            f'/en/en_US/{name}/medium/en_US-{name}-medium')


def voice_current():
    try:
        return open(os.path.join(VOICE_DIR, 'name')).read().strip()
    except Exception:
        return ''


def _voice_onnx():
    """The model say() speaks with: the coach's pick, else the install
    default."""
    sel = os.path.join(VOICE_DIR, 'voice.onnx')
    if voice_current() and os.path.exists(sel):
        return sel
    return PIPER_VOICE


def voice_download(name):
    """Fetch a voice in the background; speaks a sample when it lands."""
    if VOICE_DL['busy'] or name not in VOICES:
        return
    VOICE_DL['busy'] = True
    VOICE_DL['msg'] = f'downloading {VOICES[name][0]}…'

    def _run():
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            base = _voice_base(VOICES[name][1])
            for suff, dst in (('.onnx', 'voice.onnx.new'),
                              ('.onnx.json', 'voice.onnx.json.new')):
                req = urllib.request.Request(base + suff)
                with urllib.request.urlopen(req, timeout=300) as r, \
                        open(os.path.join(VOICE_DIR, dst), 'wb') as fh:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
            # both halves landed — swap atomically, then remember the pick
            for src, dst in (('voice.onnx.new', 'voice.onnx'),
                             ('voice.onnx.json.new', 'voice.onnx.json')):
                os.replace(os.path.join(VOICE_DIR, src),
                           os.path.join(VOICE_DIR, dst))
            with open(os.path.join(VOICE_DIR, 'name'), 'w') as fh:
                fh.write(name)
            VOICE_DL['msg'] = f'✓ {VOICES[name][0]}'
            say('this is the new voice. fastball, low and away.')
        except Exception as exc:
            VOICE_DL['msg'] = f'download failed: {exc}'
        finally:
            VOICE_DL['busy'] = False
    threading.Thread(target=_run, daemon=True).start()


PIPER = '/opt/piper/piper'
PIPER_VOICE = '/opt/piper/voice.onnx'


# A suspended Bluetooth sink takes the best part of a second to wake,
# and PipeWire suspends idle nodes after a few seconds — so the FIRST
# word of every call was eaten while the link spun up (field report:
# "cutting off the first second or so of the audio"). Two belts:
# leading silence on everything we play, and a WirePlumber drop-in
# that stops bluez sinks from suspending at all.
PAD_MS = 700


def _pad_wav(path, ms=PAD_MS):
    """Prepend silence in the file's own format. The silence wakes the
    sink; by the time speech starts, the link is up."""
    import wave
    try:
        with wave.open(path, 'rb') as r:
            prm = r.getparams()
            frames = r.readframes(r.getnframes())
        pad = b'\x00' * (int(prm.framerate * ms / 1000.0)
                          * prm.nchannels * prm.sampwidth)
        with wave.open(path, 'wb') as w:
            w.setparams(prm)
            w.writeframes(pad + frames)
    except Exception:
        pass                      # an unpadded call still beats silence


_NO_SUSPEND = os.path.expanduser(
    '~/.config/wireplumber/wireplumber.conf.d/'
    '51-playcall-bt-no-suspend.conf')


def ensure_bt_no_suspend():
    """Keep bluez audio nodes from suspending between calls. Written
    once; wireplumber restarts only the first time so a healthy boot
    never bounces audio."""
    if os.path.exists(_NO_SUSPEND):
        return
    try:
        os.makedirs(os.path.dirname(_NO_SUSPEND), exist_ok=True)
        with open(_NO_SUSPEND, 'w') as fh:
            fh.write(
                'monitor.bluez.rules = [\n'
                '  { matches = [ { node.name = "~bluez_output.*" } ]\n'
                '    actions = { update-props = {\n'
                '      session.suspend-timeout-seconds = 0\n'
                '    } } }\n'
                ']\n')
        subprocess.run(['systemctl', '--user', 'restart', 'wireplumber'],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def say(text):
    """Speak, and RECORD which path actually produced sound — the admin
    page's 'voice out' line is how a silent Test explains itself."""
    _route_to_bud()
    err = ''
    onnx = _voice_onnx()
    if os.path.exists(PIPER) and os.path.exists(onnx):
        try:
            wav = tempfile.NamedTemporaryFile(suffix='.wav',
                                              delete=False).name
            r = subprocess.run([PIPER, '--model', onnx,
                                '--output_file', wav],
                               input=text.encode(), capture_output=True,
                               timeout=20)
            if r.returncode == 0:
                _pad_wav(wav)
                p = subprocess.run(['paplay', wav], capture_output=True,
                                   timeout=30)
                os.unlink(wav)
                if p.returncode == 0:
                    STATE['voice'] = ('piper → '
                                      + (_default_sink() or 'default'))
                    return
                err = ('paplay failed: '
                       + (p.stderr.decode(errors="replace").strip()[-120:]
                          or f'exit {p.returncode}'))
            else:
                os.unlink(wav)
                err = ('piper failed: '
                       + (r.stderr.decode(errors="replace").strip()[-120:]
                          or f'exit {r.returncode}'))
        except Exception as exc:
            err = f'piper error: {exc}'
    e = subprocess.run(['espeak-ng', '-s', '150', text],
                       capture_output=True)
    STATE['voice'] = ((err + ' → ') if err else '') + 'espeak' \
        + ('' if e.returncode == 0 else f' (exit {e.returncode})')


def play_clip(data_uri):
    try:
        raw = base64.b64decode(data_uri.split(',', 1)[1])
        with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as f:
            f.write(raw)
            path = f.name
        if subprocess.run(['ffplay', '-nodisp', '-autoexit', '-loglevel',
                           'quiet', '-af', f'adelay={PAD_MS}:all=1',
                           path], check=False).returncode != 0:
            subprocess.run(['mpg123', '-q', path], check=False)
        os.unlink(path)
    except Exception:
        pass


def call_text(c, zones):
    bits = []
    if c.get('pitch_type'):
        bits.append(c['pitch_type'])
    loc = zones.get(c.get('location') or '')
    if loc:
        if c.get('bats') == 'L':      # batter-true: in/away swap for a lefty
            loc = ' '.join('away' if w == 'in' else 'in' if w == 'away'
                           else w for w in loc.split(' '))
        bits.append(loc)
    return ' — '.join(bits)


LAST_KEY = {'k': None}


def _correction(c, txt):
    """A second call on the SAME pitch (pitcher + pitch #) is the coach
    changing his mind — speak it as a correction so the catcher drops
    the first call. No start-over tap needed."""
    key = (f"{c.get('pitcher_id') or ''}#"
           f"{c.get('pitch_ct') if c.get('pitch_ct') is not None else 'x'}")
    if txt and key == LAST_KEY['k'] and '#x' not in key:
        txt = 'Scratch that. ' + txt
    if txt:
        LAST_KEY['k'] = key
    return txt


def poll_loop():
    global primed
    while True:
        try:
            d = fetch()
            STATE['cloud'] = 'connected ✓'
        except Exception as exc:
            STATE['cloud'] = f'unreachable ({exc})'
            time.sleep(5)
            continue
        STATE['game'] = d.get('game')
        STATE['opponent'] = d.get('opponent') or ''
        if d.get('zones'):
            STATE['zones'] = d['zones']     # cached for the live link too
        if d.get('game'):
            for c in reversed(d.get('calls') or []):   # oldest first
                if c['id'] in seen:
                    continue
                seen.add(c['id'])
                if not primed:
                    continue                            # history: stay quiet
                if c.get('audio'):
                    STATE['last_call'] = '🔴 coach voice'
                    STATE['spoken'] += 1
                    play_clip(c['audio'])
                    continue
                txt = _correction(c, call_text(c, d.get('zones') or {}))
                if txt:
                    STATE['last_call'] = txt
                    STATE['spoken'] += 1
                    say(txt)
            primed = True
        # pacing = what we're waiting for: idle box → nothing to speak,
        # check for a game every 8 s; live voice link up → calls arrive
        # over the data channel instantly, the poll is just the net
        nap = POLL_S
        if not d.get('game'):
            nap = 8.0
        elif str(RTC_STATE.get('s', '')).startswith('🎙'):
            nap = POLL_S * 4
        time.sleep(nap)


# ── LIVE voice: the box registers as a NAMED ear ─────────────────────────
# Multi-ear: every listener registers itself (this box shows on the
# coach page as "LIVE → <box name>") and the coach holds one peer link
# per ear — catcher box, pitcher box, and phones all live at once, so
# nobody defers to anybody. The coach's tap-to-talk streams straight
# into the earbud; called pitches ride the data channel instantly.

RTC_STATE = {'s': 'starting…'}


def _api_json(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={'X-Api-Key': KEY, 'Authorization': 'Bearer ' + KEY,
                 **({'Content-Type': 'application/json'} if data else {})})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def rtc_thread():
    try:
        import asyncio                                  # noqa: F401
        import aiortc                                   # noqa: F401
        import av                                       # noqa: F401
    except Exception:
        RTC_STATE['s'] = 'clip mode (aiortc not installed — re-run ' \
                         'install_comms.sh)'
        return
    import asyncio
    while True:
        try:
            asyncio.run(_rtc_main())
        except Exception as exc:
            RTC_STATE['s'] = f'link error: {exc}'
        time.sleep(5)


async def _rtc_main():
    import asyncio
    import av
    from aiortc import (RTCConfiguration, RTCIceServer,
                        RTCPeerConnection, RTCSessionDescription)
    seen_gen = None
    pc = None
    player = None
    ear_id = 'box_' + HOSTNAME[:32]
    ear_q = urllib.parse.quote(ear_id)
    RTC_STATE['s'] = 'waiting for a coach'
    while True:
        await asyncio.sleep(3)
        gid = STATE.get('game')
        if not gid:
            continue
        try:
            await asyncio.to_thread(
                _api_json, f'/api/sk/game/{gid}/rtc',
                {'role': 'register', 'ear': ear_id, 'label': box_name()})
            d = await asyncio.to_thread(
                _api_json,
                f'/api/sk/game/{gid}/rtc?want=offer&ear={ear_q}')
        except Exception:
            continue
        peer = (d or {}).get('peer') or {}
        gen = peer.get('gen')
        if not gen or gen == seen_gen:
            continue
        seen_gen = gen
        try:
            if pc:
                await pc.close()
            if player:
                try:
                    player.kill()
                except Exception:
                    pass
                player = None
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[
                RTCIceServer(urls='stun:stun.l.google.com:19302')]))
            resampler = av.AudioResampler(format='s16', layout='stereo',
                                          rate=48000)

            @pc.on('track')
            def on_track(track):
                async def pump():
                    nonlocal player
                    _route_to_bud()
                    player = subprocess.Popen(
                        ['paplay', '--raw', '--format=s16le',
                         '--rate=48000', '--channels=2'],
                        stdin=subprocess.PIPE)
                    RTC_STATE['s'] = '🎙 LIVE — coach linked'
                    while True:
                        try:
                            frame = await track.recv()
                        except Exception:
                            break
                        out = resampler.resample(frame)
                        frames = out if isinstance(out, list) else [out]
                        for f in frames:
                            if f is None:
                                continue
                            try:
                                player.stdin.write(
                                    f.to_ndarray().tobytes())
                            except Exception:
                                return
                asyncio.ensure_future(pump())

            @pc.on('datachannel')
            def on_dc(ch):
                @ch.on('message')
                def on_msg(m):
                    try:
                        c = json.loads(m)
                    except Exception:
                        return
                    if c.get('kind') != 'call' or c.get('id') in seen:
                        return
                    seen.add(c.get('id'))
                    txt = _correction(
                        c, call_text(c, STATE.get('zones') or {}))
                    if txt:
                        STATE['last_call'] = txt
                        STATE['spoken'] += 1
                        threading.Thread(target=say, args=(txt,),
                                         daemon=True).start()

            @pc.on('connectionstatechange')
            def on_cs():
                if pc.connectionState in ('failed', 'closed',
                                          'disconnected'):
                    RTC_STATE['s'] = 'link dropped — back to polling'

            await pc.setRemoteDescription(
                RTCSessionDescription(**json.loads(peer['sdp'])))
            await pc.setLocalDescription(await pc.createAnswer())
            await asyncio.to_thread(
                _api_json, f'/api/sk/game/{gid}/rtc',
                {'role': 'answer', 'ear': ear_id, 'gen': gen,
                 'sdp': json.dumps({'sdp': pc.localDescription.sdp,
                                    'type': pc.localDescription.type})})
            RTC_STATE['s'] = 'answered — connecting…'
        except Exception as exc:
            RTC_STATE['s'] = f'link error: {exc}'


# ── one-button code update (the encoder's cloud-portal trick) ───────────
# The box runs straight from the git checkout, so updating IS a git
# pull. On success with new code the process exits — systemd
# (Restart=always) revives it ~5 s later on the new tree, no root
# needed. Reached from the cloud portal via the one-click ⚙ Settings
# link on the team comms page.

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def code_version():
    try:
        r = subprocess.run(['git', '-C', REPO_DIR, 'log', '-1',
                            '--format=%h · %cs'], capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip() or '?'
    except Exception:
        return '?'


def do_update():
    """(changed, message). --ff-only: a box must only ever fast-forward —
    if the checkout diverged somehow, say so instead of merging."""
    try:
        r = subprocess.run(['git', '-C', REPO_DIR, 'pull', '--ff-only'],
                           capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, f'update failed: {exc}'
    if r.returncode != 0:
        return False, ('update failed: '
                       + (r.stderr or r.stdout or 'git error').strip()[-200:])
    if 'Already up to date' in (r.stdout or ''):
        return False, '✓ already up to date (' + code_version() + ')'
    return True, '✓ updated to ' + code_version()


# ── which radio ──────────────────────────────────────────────────────────
#
# A box with a USB dongle plugged in has TWO Bluetooth controllers, and
# every bluetoothctl call here runs against BlueZ's "default" one. That
# default is not stable: on this box it was the dongle before a reboot
# and the built-in radio after, with nothing changed in between. So a
# coach who buys a dongle for the external antenna gets the antenna on
# some boots and not others, and cannot tell which — the range is just
# worse sometimes.
#
# bluetoothctl has no per-invocation adapter flag (`select` lasts one
# interactive session and every call here is one-shot), so pinning is done
# the only way that holds for all of them: POWER DOWN the adapters we did
# not choose. One controller up means "default" has nothing to be
# ambiguous about, and every existing call lands on the right radio
# without knowing this code exists.

ADAPTER_ERR = {'no_permission': False, 'did_not_take': False}


def _rfkill_rows():
    """[{'id', 'dev', 'blocked'}] from rfkill, Bluetooth devices only."""
    out = []
    try:
        r = subprocess.run(['rfkill', '--noheadings', '--output',
                            'ID,DEVICE,TYPE,SOFT'],
                           capture_output=True, text=True, timeout=6)
        for line in (r.stdout or '').splitlines():
            f = line.split()
            if len(f) >= 4 and f[2] == 'bluetooth':
                out.append({'id': f[0], 'dev': f[1],
                            'blocked': f[3] == 'blocked'})
    except Exception:
        pass
    return out


def _rfkill(action, ident):
    """block/unblock a radio. True if it took.

    READING rfkill works as any user; WRITING needs root, and that is
    what made this so quiet. The picker could read the block state
    perfectly, print it on the card, and fail every attempt to change it
    with "Operation not permitted" into output nothing looked at —
    subprocess.run does not raise on a non-zero exit, so the whole
    failure was one unchecked returncode. The card said "picked" and the
    radio never moved.
    """
    for cmd in (['rfkill', action, str(ident)],
                ['sudo', '-n', 'rfkill', action, str(ident)]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=6)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _adapter_address(hci):
    """The MAC of hciN. '' if it cannot be established.

    sysfs used to publish this and on his kernel no longer does —
    /sys/class/bluetooth/hci0/address is 'No such file or directory'. BlueZ
    still knows, and its D-Bus object paths ARE the controller indices, so
    /org/bluez/hci1's Address property is an exact answer where reading
    `bluetoothctl list` and pairing it up by order is a guess.
    """
    try:
        return open(f'/sys/class/bluetooth/{hci}/address').read().strip().upper()
    except Exception:
        pass
    try:
        r = subprocess.run(['busctl', '--system', 'get-property', 'org.bluez',
                            '/org/bluez/' + hci, 'org.bluez.Adapter1',
                            'Address'], capture_output=True, text=True,
                           timeout=6)
        # → s "AC:A7:F1:29:A9:29"
        parts = (r.stdout or '').strip().split('"')
        if len(parts) >= 2 and ':' in parts[1]:
            return parts[1].strip().upper()
    except Exception:
        pass
    return ''


def _usb_product(hci):
    """The adapter's own USB product string ("TP-Link UB500 Adapter",
    "AX201 Bluetooth"), walked up sysfs from the hci device. On an x86
    box the BUILT-IN Bluetooth also hangs off an internal USB bus, so
    "USB dongle vs built-in" stops being a distinction at all — two
    identical rows on the one card a coach uses to pick a radio. The
    product string is the difference the hardware itself declares."""
    try:
        p = os.path.realpath('/sys/class/bluetooth/' + hci)
        for _ in range(8):
            p = os.path.dirname(p)
            if not p or p == '/':
                break
            f = os.path.join(p, 'product')
            if os.path.isfile(f):
                # a root hub's product ("xHCI Host Controller") is the
                # BUS's name, not the radio's — the Intel card carries
                # no product string and the walk was crediting it with
                # the host controller's (field report screenshot)
                try:
                    vend = open(os.path.join(p, 'idVendor')).read().strip()
                except Exception:
                    vend = ''
                if vend == '1d6b':          # Linux Foundation = root hub
                    return ''
                return open(f).read().strip()
    except Exception:
        pass
    return ''


def adapters():
    """Every Bluetooth controller on the box, with enough to choose by.

    `usb` is read from the sysfs device path rather than the name or the
    MAC: a dongle hangs off a USB bus and the Pi's own radio hangs off the
    SoC's serial line, and that is the one difference no vendor can get
    wrong.
    """
    macs = {}
    for line in _bt('list').splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == 'Controller':
            macs[f[1].upper()] = ' '.join(f[2:]).replace('[default]', '').strip()
    blocked = {r['dev']: r['blocked'] for r in _rfkill_rows()}
    out = []
    try:
        names = sorted(os.listdir('/sys/class/bluetooth'))
    except Exception:
        names = []
    for hci in names:
        # hciN and nothing else. /sys/class/bluetooth also carries child
        # nodes like 'hci0:2' — an rfcomm/LE sub-device, not a radio —
        # and a startswith('hci') let one through as a second adapter
        # with no MAC. The card then drew "USB dongle · hci0:2 · ?" and
        # flagged it in red as picked-but-not-in-use: an invented fault,
        # on the one page a coach consults when something is wrong.
        if not re.fullmatch(r'hci\d+', hci):
            continue
        path = ''
        try:
            path = os.path.realpath('/sys/class/bluetooth/' + hci)
        except Exception:
            pass
        mac = _adapter_address(hci)
        out.append({'hci': hci, 'mac': mac,
                    'usb': '/usb' in path or 'xhci' in path,
                    'blocked': blocked.get(hci, False),
                    'name': macs.get(mac, '')})
    # Last resort: if exactly one controller is still nameless and exactly
    # one MAC from bluetoothctl is unaccounted for, they are each other.
    # Never guess beyond that — pairing the two LISTS by order looks
    # reasonable and is wrong, because bluetoothctl sorts the default
    # controller first and the default is not hci0.
    unknown = [d for d in out if not d['mac']]
    spare = [m for m in macs if m not in {d['mac'] for d in out if d['mac']}]
    if len(unknown) == 1 and len(spare) == 1:
        unknown[0]['mac'] = spare[0]
        unknown[0]['name'] = macs.get(spare[0], '')
    return out


def active_adapter():
    """The controller bluetoothctl is ACTUALLY talking to, by MAC.

    Powering the others down is meant to leave it no choice, and it is
    not quite a guarantee: this box has printed a controller as
    `[default]` while that same controller read `PowerState: off-blocked`.
    So the pin is a request, and this is the answer — without it the card
    can say "in use" about a radio that nothing is using, which is the
    one failure that makes every other reading on the page a lie.
    """
    for line in _bt('show').splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == 'Controller':
            return f[1].upper()
    return ''


def _adapter_card():
    """The radio picker. Only drawn when there is a choice to make — one
    controller is the normal box, and a card offering to pick it would be
    a question with one answer."""
    ads = adapters()
    if len(ads) < 2:
        return ''
    want = adapter_pref()
    actual = active_adapter()
    rows = []
    for a in ads:
        # Say what we can SEE. Whether a dongle has an external antenna is
        # not visible from here, and printing it because the coach said so
        # when he ordered it turns the card into a mirror: the UB500 in
        # this box is the nano model with an internal antenna, and the
        # card was congratulating him on range he had not installed.
        prod = _usb_product(a['hci'])
        kind = (prod or 'USB dongle') if a['usb'] else \
            (prod and f'built-in radio ({prod})' or 'built-in radio')
        # "In use" is now READ, not assumed. The pin is a request; this
        # is the answer, and they can disagree — see active_adapter().
        if not a['mac']:
            # An adapter with NO address never initialized — on a USB
            # dongle that is almost always missing firmware (the UB500's
            # Realtek chip needs firmware-realtek, which a netinst
            # Debian doesn't ship). The old row still drew a button, and
            # its value was the empty MAC — which /adapter reads as
            # UNPIN. So the coach tapped the dongle, the page silently
            # cleared the pin, and "nothing happens" (field report).
            # A dead radio gets a diagnosis, never a button.
            rows.append(
                f'<div style="margin:.25rem 0;padding:.5rem .6rem;'
                f'border:1px dashed #666;border-radius:.4rem">'
                + f'{html.escape(kind)}<br>'
                + f'<span class="dim">{html.escape(a["hci"])} · no '
                'address — the adapter never initialized. On a USB '
                'dongle this is almost always missing firmware: '
                '<code>sudo apt install -y firmware-realtek</code>, '
                'then unplug and replug the dongle (no reboot needed) '
                'and reload this page.</span></div>')
            continue
        tag = ''
        if a['mac'] == actual:
            tag = ' <b class="ok">← in use</b>'
        elif want and a['mac'] == want:
            tag = ' <b class="bad">← picked, but NOT the one in use</b>'
        elif a['blocked']:
            tag = ' <span class="dim">(powered down)</span>'
        rows.append(
            f'<form method="post" action="/adapter" '
            f'style="margin:.25rem 0">'
            f'<button name="mac" value="{html.escape(a["mac"])}" '
            f'style="width:100%;text-align:left;padding:.5rem .6rem"'
            + (' disabled' if a['mac'] == want else '') + '>'
            + f'{html.escape(kind)}<br>'
            + f'<span class="dim">{html.escape(a["hci"])} · '
            + f'{html.escape(a["mac"])}</span>{tag}</button></form>')
    # BlueZ keys pairings by ADAPTER (/var/lib/bluetooth/<adapter>/<bud>),
    # so changing adapters hands you an empty pairing list and a bud that
    # will not show up in a scan unless it is put back into pairing mode.
    # Discovering that on your own, at a field, is an afternoon.
    mismatch = ''
    if want and actual and actual != want:
        # Name the CAUSE where we know it. "Unpin and try the other one"
        # is useless advice when the pin was never applied in the first
        # place — the coach would swap adapters forever.
        why = ('<b class="bad">The box is not allowed to power a radio '
               'down on this install.</b> <span class="dim">Re-run '
               '<code>install_comms.sh</code> (it grants exactly that and '
               'nothing else), then pick the adapter again.</span>'
               if ADAPTER_ERR.get('no_permission') else
               '<b class="bad">The pin would not take, so every radio was '
               'left switched on rather than stranding the box on one that '
               'carries none of its pairings.</b> <span class="dim">Unpin '
               'and use the adapter shown as in use.</span>'
               if ADAPTER_ERR.get('did_not_take') else
               '<span class="dim">Unpin and try the other adapter.</span>')
        mismatch = ('<b class="bad">⚠ BlueZ is still using a different '
                    'radio than the one picked.</b> <span class="dim">'
                    'Everything below — scans, pairing, the earpiece — is '
                    'happening on ' + html.escape(actual) + '.</span> '
                    + why + '<br>')
    note = mismatch + ('<span class="dim">Pinned — re-applied every time '
            'the box starts.<br>Pairings belong to the adapter: after a '
            'switch, put each bud back in pairing mode and pair it again.'
            '</span>'
            if want else
            '<b class="warn">Not pinned.</b> <span class="dim">BlueZ picks '
            'one at boot and the choice changes between reboots, so the '
            'dongle is in use on some days and not others.</span>')
    return ('<div class="card">bluetooth adapter:<br>' + ''.join(rows)
            + note
            + ('<form method="post" action="/adapter" '
               'style="margin-top:.4rem">'
               '<button name="mac" value="" style="padding:.3rem .7rem">'
               '↩ unpin — let BlueZ choose</button></form>' if want else '')
            + '</div>')


def adapter_pref():
    try:
        v = open(ADAPTER_FILE).read().strip().upper()
        return v if ':' in v else ''
    except Exception:
        return ''


def set_adapter_pref(mac):
    try:
        if mac:
            with open(ADAPTER_FILE, 'w') as fh:
                fh.write(mac.strip().upper())
        elif os.path.exists(ADAPTER_FILE):
            os.remove(ADAPTER_FILE)
    except Exception:
        pass


def enforce_adapter():
    """Power down every controller except the chosen one. Returns the
    adapter now in use, or None when no choice is stored.

    Called at startup as well as on selection, because the thing being
    corrected is a boot-time race — a preference that is only applied when
    a human taps a button is a preference that is wrong every morning.
    """
    want = adapter_pref()
    ids = {r['dev']: r['id'] for r in _rfkill_rows()}
    if not want:
        # UNPINNING HAS TO UNDO THE PINNING. Returning early left the
        # radios exactly as the last pin had powered them, so "let BlueZ
        # choose" handed BlueZ one choice — and the coach who taps it is
        # a coach trying to get back to the setup that worked.
        for a in adapters():
            if ids.get(a['hci']) is not None:
                _rfkill('unblock', ids[a['hci']])
        _bt('power', 'on')
        return None
    found = [a for a in adapters() if a['mac'] == want]
    if not found:
        return None                    # dongle unplugged; leave BlueZ alone
    ok = True
    for a in adapters():
        rid = ids.get(a['hci'])
        if rid is None:
            ok = False
            continue
        if not _rfkill('unblock' if a['mac'] == want else 'block', rid):
            ok = False
    _bt('power', 'on')
    ADAPTER_ERR['no_permission'] = not ok
    # VERIFY, THEN KEEP. Powering the others down is meant to leave BlueZ
    # no choice and is not a guarantee — and the failure mode is vicious:
    # the box comes up on a radio that carries none of its pairings, with
    # the earpiece it needs bonded to the radio we just switched off. So
    # if the pin did not actually take, put every radio back rather than
    # stranding the box somewhere it cannot hear its own bud.
    if ok and active_adapter() not in ('', want):
        for a in adapters():
            if ids.get(a['hci']) is not None:
                _rfkill('unblock', ids[a['hci']])
        _bt('power', 'on')
        ADAPTER_ERR['did_not_take'] = True
        return None
    ADAPTER_ERR['did_not_take'] = False
    return found[0]


# ── bluetoothctl plumbing for the admin page ─────────────────────────────

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m|\r')


def _bt(*args, timeout=12):
    """One bluetoothctl call, landed on the PICKED radio.

    Powering the other controllers down (enforce_adapter) was meant to
    leave BlueZ's "default" no choice, and on the N150 it simply is not
    true: bluetoothctl keeps a soft-blocked controller as [default], so
    every one-shot call here talked to a radio that was off — the pin
    verified as "did not take" and rolled back forever, and the card
    said "picked, but NOT the one in use" no matter what was tapped
    (field report). bluetoothctl has no per-invocation adapter flag,
    but its interactive mode has `select`, which holds for the session.
    So with an adapter pinned, every call becomes a tiny scripted
    session: select the pinned controller, run the command, exit. The
    scan's `--timeout N scan on` form holds the session open for N
    seconds first — discovery needs wall-clock, not a flag.
    ANSI color and prompt noise are stripped so the parsers see the
    same text the one-shot form produced.
    """
    want = adapter_pref()
    if not want or (args and args[0] == 'list'):
        return _bt_oneshot(*args, timeout=timeout)
    try:
        hold = 0
        cmds = list(args)
        if cmds and cmds[0] == '--timeout':
            hold = int(cmds[1])
            cmds = cmds[2:]
        p = subprocess.Popen(['bluetoothctl'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        p.stdin.write(f'select {want}\n' + ' '.join(cmds) + '\n')
        p.stdin.flush()
        if hold:
            time.sleep(hold)
            try:
                p.stdin.write('scan off\n')
                p.stdin.flush()
            except Exception:
                pass
        try:
            p.stdin.write('exit\n')
            p.stdin.flush()
        except Exception:
            pass
        out, _ = p.communicate(timeout=timeout)
        return _ANSI_RE.sub('', out or '')
    except FileNotFoundError:
        return '__NO_BT__'
    except Exception as exc:
        try:
            p.kill()
        except Exception:
            pass
        return str(exc)


def _bt_oneshot(*args, timeout=12):
    try:
        r = subprocess.run(['bluetoothctl'] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except FileNotFoundError:
        return '__NO_BT__'
    except Exception as exc:
        return str(exc)


def audio_ok():
    """Is the PipeWire engine reachable? Without it a bud pairs, connects
    at the Bluetooth level, finds no audio service, and silently drops —
    the admin page must SAY that instead of playing dead."""
    try:
        r = subprocess.run(['pactl', 'info'], capture_output=True,
                           timeout=4)
        return r.returncode == 0
    except Exception:
        return False


def bt_audio_problem():
    """The PRECISE check, not the lazy one.

    'auto_null' alone means nothing on a box with no speakers or HDMI
    audio — an encoder Pi legitimately owns no sinks until a bud
    connects (an earlier build cried wolf about exactly that). What
    actually makes a bud fail with profile-unavailable is BlueZ having
    no A2DP endpoint, and that endpoint comes from ONE thing: the
    PipeWire bluez5 SPA plugin. Test for it directly, then for the
    connected-but-silent case. Returns (headline, fix) or None."""
    import glob
    if not glob.glob('/usr/lib/*/spa-0.2/bluez5/libspa-bluez5.so') \
            and not glob.glob('/usr/lib/spa-0.2/bluez5/libspa-bluez5.so'):
        return ('⚠ BLUETOOTH AUDIO PLUGIN MISSING',
                'BlueZ has no audio profile to give a bud, so every '
                'connect fails with profile-unavailable. On the box: '
                '<code>sudo apt install -y libspa-0.2-bluetooth</code> '
                'then reboot.')
    # No A2DP endpoint on the adapter = no bud can ever connect. The
    # classic headless cause is wireplumber's bluez monitor parked
    # waiting for a logind SEAT that a lingering session never has.
    try:
        show = _bt('show')
        if '__NO_BT__' not in show and 'Powered: yes' in show \
                and 'Audio Source' not in show:
            return ('⚠ NO BLUETOOTH AUDIO PROFILE',
                    'The adapter offers no A2DP endpoint, so every bud '
                    'connect fails. Usually wireplumber is waiting for '
                    'a login seat this headless box will never have — '
                    're-run <code>install_comms.sh</code> (it writes the '
                    'headless fix) or reboot.')
    except Exception:
        pass
    st = bt_status()
    if st.get('ok') and st.get('connected'):
        try:
            r = subprocess.run(['pactl', 'list', 'short', 'sinks'],
                               capture_output=True, text=True, timeout=5)
            if 'bluez' not in r.stdout:
                return ('⚠ BUD CONNECTED, NO AUDIO SINK',
                        'The plugin is installed but did not attach to '
                        'this bud — reboot the box, or if it persists '
                        'the bud connected as its no-audio "-BLE" twin.')
        except Exception:
            pass
    return None


def reboot_box():
    for cmd in (['sudo', '-n', 'systemctl', 'reboot'],
                ['systemctl', 'reboot']):
        try:
            if subprocess.run(cmd, capture_output=True,
                              timeout=10).returncode == 0:
                return True
        except Exception:
            pass
    return False


def bt_status():
    show = _bt('show')
    if '__NO_BT__' in show:
        return {'ok': False}
    st = {'ok': True,
          'pairable': 'Pairable: yes' in show,
          'discoverable': 'Discoverable: yes' in show,
          'connected': []}
    for line in _bt('devices', 'Connected').splitlines():
        m = re.match(r'Device (\S+) (.*)', line.strip())
        if m:
            st['connected'].append({'mac': m.group(1), 'name': m.group(2)})
    return st


_MAC_RE = r'((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})'


def _live_macs(text):
    """MACs that ANSWERED this scan.

    `[NEW]` was used for this and does not mean it. BlueZ prints [NEW]
    when it CREATES the D-Bus object for a device — the first time it
    ever meets it. A device it already has an object for answers the very
    same scan with

        [CHG] Device 60:AB:D2:11:22:33 RSSI: -47

    and every one of those lines was being thrown away. Objects persist
    for every PAIRED device forever, and for anything else until
    bluetoothd purges it a while after discovery stops — so a bud you
    paired yesterday, or a fridge you scanned ten minutes ago, can never
    be [NEW] again. It reads as "nothing answered" while the thing is
    shouting from two feet away, and it explains why this worked the day
    it was set up and never afterwards.

    RSSI is the right evidence: it only arrives on an actual
    advertisement or inquiry response, so it means "heard, just now" in a
    way that no other property change does.
    """
    live = set()
    for line in (text or '').splitlines():
        if 'NEW]' in line:
            m = re.search('Device ' + _MAC_RE, line)
            if m:
                live.add(m.group(1).upper())
        elif 'CHG]' in line and 'RSSI' in line:
            m = re.search('Device ' + _MAC_RE, line)
            if m:
                live.add(m.group(1).upper())
    return live


def _dev_lines(text, tag=None):
    """(mac, name) pairs from bluetoothctl output. tag filters to lines
    carrying it (e.g. 'NEW]' from a live scan) so RSSI/UUID change
    chatter never masquerades as a device name."""
    for line in text.splitlines():
        if tag and tag not in line:
            continue
        m = re.search(r'Device ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})'
                      r' ?(.*)', line)
        if m:
            yield m.group(1).upper(), (m.group(2) or '').strip()


def bt_scan():
    """A real-world earbud scan. 12 s of discovery — the Pi's WiFi and
    Bluetooth share one antenna, so short scans miss. Devices come from
    BOTH the scan's own [NEW] lines and the adapter list afterwards, and
    UNNAMED devices stay in (a bud often advertises its name a scan or
    two later); they show by MAC and are still pairable.

    `live` separates those two sources, and it matters more than it
    looks. `bluetoothctl devices` is the adapter's CACHE — every device
    that radio has ever seen, kept in /var/lib/bluetooth/<adapter>/cache
    — so a box that has sat in a house for months lists the neighbours'
    phones and televisions whether or not they are here today. Pinning a
    new adapter empties that cache, the list drops from ten devices to
    two, and it reads as a radio that has gone deaf. It hasn't: those two
    are the only things ACTUALLY in range and announcing themselves. So
    the page says which is which instead of running them together.
    """
    # THE CHASER HAS TO STAND DOWN FOR THIS. bt_pair takes PAIRING['busy']
    # and BT_LOCK for exactly this reason and bt_scan did not, so a
    # 12-second discovery ran straight through the reconnect loop's
    # `connect` attempts — and BlueZ will not run an inquiry and a
    # connection on one controller at once. Discovery lost, silently: the
    # error goes into output nobody parses and the scan returns the cache
    # plus whatever BLE advertising leaked past.
    #
    # It only bites once a bud is PAIRED AND ABSENT, which is the exact
    # state of a headset sitting in pairing mode waiting to be re-paired.
    # The chaser wakes every 7 s and each connect can take 8, so it holds
    # the controller more than half the time, forever, and the scan that
    # would fix it can never see anything. Reported as "my headset says
    # 'ready to pair' and then nothing... it is not the Bluetooth
    # headset." It was not.
    PAIRING['busy'] = True
    try:
        with BT_LOCK:
            _bt('power', 'on')
            scan_out = _bt('--timeout', '12', 'scan', 'on', timeout=20)
            known_out = _bt('devices')
    finally:
        PAIRING['busy'] = False
    live = _live_macs(scan_out)
    found = {}
    for mac, name in (list(_dev_lines(scan_out, tag='NEW]'))
                      + list(_dev_lines(known_out))):
        if name.replace(':', '-').upper() == mac.replace(':', '-'):
            name = ''                       # "name" is just the MAC again
        found[mac] = name or found.get(mac, '')
    return [{'mac': m, 'name': n, 'live': m in live}
            for m, n in found.items()]


def _bt_tail(out):
    lines = [ln.strip() for ln in (out or '').splitlines() if ln.strip()]
    return lines[-1] if lines else 'no reply'


PAIRING = {'busy': False}
BT_LOCK = threading.Lock()      # one bluetoothctl operation at a time —
#                                 the reconnect chaser and the Pair button
#                                 colliding is org.bluez.Error.InProgress

_AUDIO_BOUNCE = {'t': 0.0}


def _bounce_audio(force=False):
    """br-connection-profile-unavailable = the engine runs but its
    Bluetooth half missed the bus (a once-per-setup hiccup). The box
    heals itself: bounce the PipeWire trio and give it a beat. Rate-
    limited for the background chaser; a human-initiated pair forces."""
    if not force and time.time() - _AUDIO_BOUNCE['t'] < 300:
        return
    _AUDIO_BOUNCE['t'] = time.time()
    subprocess.run(['systemctl', '--user', 'restart', 'pipewire',
                    'pipewire-pulse', 'wireplumber'], check=False,
                   timeout=25)
    time.sleep(5)


def _paired_pairs():
    out = _bt('devices', 'Paired')
    if 'Invalid command' in out or not out.strip():
        out = _bt('paired-devices')          # older bluez spelling
    return list(_dev_lines(out))


def _paired_macs():
    return [m for m, _n in _paired_pairs()]


_CHASE = {}          # mac -> [consecutive misses, next attempt (monotonic)]
_CHASE_MAX = 120.0   # a bud walking back to the dugout is picked up inside
#                      two minutes, which is faster than anyone notices


def _chase_due(mac, now=None):
    now = time.monotonic() if now is None else now
    rec = _CHASE.get(mac.upper())
    return True if not rec else now >= rec[1]


def _chase_mark(mac, ok, now=None):
    """Success resets to instant; misses back off 7 s, 14, 28... to two
    minutes. Without this an earpiece left at home holds the controller
    for eight seconds out of every fifteen, all day."""
    now = time.monotonic() if now is None else now
    key = mac.upper()
    if ok:
        _CHASE.pop(key, None)
        return
    misses = _CHASE.get(key, [0, 0.0])[0] + 1
    _CHASE[key] = [misses, now + min(_CHASE_MAX, 7.0 * (2 ** (misses - 1)))]


def reconnect_loop():
    """Buds walk to the dugout and back all game: when a labeled (or any
    paired) earpiece drops, chase it every few seconds so it's live again
    moments after re-entering range — nobody taps anything at the fence."""
    while True:
        time.sleep(7)
        try:
            if PAIRING['busy']:
                continue                     # never fight an active pair
            st = bt_status()
            if not st.get('ok'):
                continue
            connected = {d['mac'].upper() for d in st['connected']}
            want = {m.upper() for m in ear_labels()} \
                or {m.upper() for m in _paired_macs()}
            missing = sorted(want - connected)
            if not missing:
                continue
            # A bud that has gone home for the night is chased every 7 s
            # for 8 s at a time, forever, and each attempt holds the
            # controller — so an absent earpiece quietly costs half the
            # radio and blocks the discovery that would replace it. Back
            # off per bud after repeated misses; a bud walking back into
            # range is still picked up within a couple of minutes, and
            # any success resets it to instant.
            due = [m for m in missing if _chase_due(m)]
            if not due:
                continue
            if not BT_LOCK.acquire(blocking=False):
                continue                     # someone's pairing — stand down
            try:
                for mac in due:
                    if PAIRING['busy']:
                        break
                    out = _bt('connect', mac, timeout=8)
                    _chase_mark(mac, 'Connection successful' in out)
                    if 'profile-unavailable' in out:
                        _bounce_audio()        # self-heal, rate-limited
            finally:
                BT_LOCK.release()
            if {d['mac'].upper() for d in
                    bt_status().get('connected', [])} - connected:
                _route_to_bud()              # a bud came home — route to it
        except Exception:
            pass


def bt_pair(mac):
    """Pair + trust + connect. PAIRING['busy'] + BT_LOCK stop the
    reconnect chaser from colliding with us (that collision is
    org.bluez.Error.InProgress); the finally guarantees the chaser is
    never left believing a pair is still running."""
    PAIRING['busy'] = True
    try:
        with BT_LOCK:
            return _bt_pair_inner(mac)
    finally:
        PAIRING['busy'] = False


def _bt_pair_inner(mac):
    """The actual pair flow, with the radio actively scanning DURING
    the attempt — BlueZ forgets unpaired devices ~30 s after a scan
    ends, so pairing from a read-the-list-first tap fails with 'not
    available' unless the bud is brought back into view. Returns a
    short human-readable outcome for the page to show."""
    _bt('power', 'on')
    _bt('pairable', 'on')
    scanner = None
    try:
        scanner = subprocess.Popen(
            ['bluetoothctl', '--timeout', '30', 'scan', 'on'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)                    # let the bud reappear in view
    except Exception:
        pass
    msg = _pair_seq(mac)
    if scanner:
        try:
            scanner.terminate()
        except Exception:
            pass
    return msg


def _pair_seq(mac):
    """pair → trust → connect → PROVE an audio sink appeared. The shared
    tail of the manual Pair button and the ⚡ auto-pair."""
    out_p = _bt('pair', mac, timeout=35)
    if 'InProgress' in out_p:
        # an operation was mid-flight when we started — let it settle
        # and try once more before reporting anything
        time.sleep(6)
        out_p = _bt('pair', mac, timeout=35)
    ok_pair = ('Pairing successful' in out_p
               or 'AlreadyExists' in out_p          # tapped again — fine
               or 'already paired' in out_p.lower())
    _bt('trust', mac)
    out_c = _bt('connect', mac, timeout=25)
    if 'profile-unavailable' in out_c:
        _bounce_audio(force=True)
        out_c = _bt('connect', mac, timeout=25)
    ok_conn = 'Connection successful' in out_c
    if ok_conn:
        # "connected" is not enough — the bud's "-BLE" twin connects too
        # and brings NO audio. Proof of life is an audio sink appearing.
        time.sleep(2)
        routed = _route_to_bud()
        if not routed:
            time.sleep(3)
            routed = _route_to_bud()
        if not routed:
            return ('⚠ connected, but the device brought no AUDIO — '
                    'that is the bud\'s "-BLE" twin. Scan again and '
                    'pair the entry WITHOUT -BLE, and 🗑 forget this '
                    'one below.')
        return '✓ paired and connected'
    if ok_pair:
        return 'paired, but connect failed: ' + _bt_tail(out_c)
    return 'pairing failed: ' + _bt_tail(out_p)


def bt_autopair():
    """⚡ One tap while the bud flashes: watch the live scan and pair the
    FIRST audio-capable device that answers — no list to read, no MAC to
    recognize, no racing the bud's pairing-mode timeout (a failed attempt
    knocks most buds out of pairing mode, so speed is the whole game).

    'ANSWERS' used to mean a [NEW] line, and a bud you have paired before
    can never produce one — BlueZ keeps its object forever, so it reports
    [CHG] … RSSI instead. This watched for a line that would never come,
    timed out, and said "no new earbud appeared — is it flashing in
    pairing mode?" to a coach holding a flashing earbud. Re-pairing the
    bud you already own is the single most common thing anyone does on
    this page, and it was the one case that could not work.

    A bud already bonded but NOT connected is a stale bond — usually the
    reason the coach is here — so the bond is dropped and rebuilt. One
    that is CONNECTED is a working earpiece and is left alone.
    """
    PAIRING['busy'] = True
    scanner = None
    try:
        with BT_LOCK:
            _bt('power', 'on')
            _bt('pairable', 'on')
            bonded = {m.upper() for m in _paired_macs()}
            busy = {d['mac'].upper()
                    for d in (bt_status().get('connected') or [])}
            # [CHG] lines carry no name, so keep the adapter's own list to
            # look one up — and a bud that has been seen before always has
            # a name there.
            names = {m: n for m, n in _dev_lines(_bt('devices'))}
            try:
                scanner = subprocess.Popen(
                    ['bluetoothctl', '--timeout', '30', 'scan', 'on'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
            except Exception as exc:
                return f'scan failed: {exc}'
            target = None
            t0 = time.time()
            while time.time() - t0 < 22:
                line = scanner.stdout.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                heard = _live_macs(line)
                if not heard:
                    continue
                mac = heard.pop()
                if mac in busy:
                    continue                     # a working earpiece
                m = re.search('Device ' + _MAC_RE + ' ?(.*)', line)
                name = ((m.group(2) or '').strip() if m else '')
                if name.replace(':', '-').upper() == mac.replace(':', '-') \
                        or 'RSSI' in name or 'TxPower' in name:
                    name = ''                    # a property, not a name
                name = name or names.get(mac, '')
                if not name:
                    continue                     # unnamed so far — wait
                up = name.upper()
                if up.endswith('-BLE') or up.endswith(' LE'):
                    continue                     # the no-audio twin
                target = (mac, name)
                break
            if not target:
                return ('nothing answered — is it flashing in pairing '
                        'mode? (tap ⚡ again the moment it is)')
            mac, name = target
            if mac in bonded:
                # the bond it is refusing to honour, out of the way first
                _bt('remove', mac)
            return f'🎧 {name}: ' + _pair_seq(mac)
    finally:
        if scanner:
            try:
                scanner.terminate()
            except Exception:
                pass
        PAIRING['busy'] = False


def bt_lock(locked):
    _bt('discoverable', 'off' if locked else 'on')
    _bt('pairable', 'off' if locked else 'on')


# ── the local admin page (phone on the same WiFi + the PIN) ──────────────

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🎧 Comms box</title><style>
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
padding:1rem;max-width:30rem;margin:0 auto}
.card{background:#161b22;border:1px solid #2a313c;border-radius:12px;
padding:.8rem 1rem;margin-bottom:.7rem}
b.ok{color:#3ddc84}b.warn{color:#e8c25a}b.bad{color:#f87171}
button{background:#1c232d;color:#e6edf3;border:1px solid #2a313c;
border-radius:9px;padding:.55rem 1rem;font-size:.95rem;font-weight:700;
margin:.2rem .2rem .2rem 0;cursor:pointer}
button.go{background:#10b981;color:#04150d;border-color:#10b981}
button.lock{background:#7f1d1d;color:#fecaca;border-color:#991b1b}
input{background:#0d1117;color:#e6edf3;border:1px solid #2a313c;
border-radius:8px;padding:.55rem;font-size:1.1rem;width:8rem}
.dim{color:#8b949e;font-size:.82rem}
</style></head><body>__BODY__</body></html>"""


def _bt_audio_warning():
    prob = bt_audio_problem()
    if not prob:
        return ''
    head, fix = prob
    return (f'<br><b class="bad">{head}</b> — {fix}'
            '<form method="post" action="/reboot" style="margin:.3rem 0">'
            '<button class="lock">🔄 Reboot the box</button></form>')


def _ghost_rows(st):
    """Paired-but-absent devices, each with a 🗑 forget — the parking lot
    for dead -BLE twins and last season's buds (the reconnect chaser
    stops hunting whatever gets forgotten here)."""
    conn_macs = {d['mac'].upper() for d in st.get('connected', [])}
    ghosts = [(m, n) for m, n in _paired_pairs() if m not in conn_macs]
    if not ghosts:
        return ''
    return ('<div style="margin-top:.5rem"><span class="dim">paired, '
            'not here:</span>'
            + ''.join(
                f'<div style="margin:.15rem 0" class="dim">💤 '
                f'{html.escape(n or m)} '
                f'<form method="post" action="/btforget" '
                f'style="display:inline">'
                f'<input type="hidden" name="mac" value="{m}">'
                f'<button style="padding:.15rem .5rem;font-size:.7rem">'
                f'🗑 forget</button></form></div>'
                for m, n in ghosts)
            + '</div>')


def _page_body(q):
    st = bt_status()
    cloud = STATE['cloud']
    up_msg = ''
    if q.get('updated'):
        m = urllib.parse.unquote(q['updated'])
        up_msg = (f'<div class="card"><b class="'
                  f'{"ok" if m.startswith("✓") else "bad"}">'
                  f'{html.escape(m)}</b>'
                  + ('<br><span class="dim">coming back — this page '
                     'reloads itself shortly</span>'
                     '<script>setTimeout(()=>location="/",'
                     + ('75000' if 'boot' in m else '12000')
                     + ')</script>'
                     if q.get('restarting') else '')
                  + '</div>')
    paired_msg = ''
    if q.get('paired'):
        m = urllib.parse.unquote(q['paired'])
        good = '✓' in m and '⚠' not in m
        paired_msg = (f'<div class="card"><b class="'
                      f'{"ok" if good else "bad"}">{html.escape(m)}</b>'
                      + ('' if good else
                         '<br><span class="dim">put the bud back in '
                         'pairing mode and tap ⚡ again. If it fails '
                         'repeatedly with an authentication error, '
                         'factory-reset the buds — they may still trust '
                         'an old box or phone.</span>')
                      + '</div>')
    game = (f"live vs <b>{html.escape(STATE['opponent'])}</b>"
            if STATE['game'] else 'no live game right now')
    b = [f'<h2>🎧 Comms box</h2>{up_msg}{paired_msg}'
         f'<div class="card">cloud: <b class="'
         f'{"ok" if "✓" in cloud else "bad"}">{html.escape(cloud)}</b>'
         f'<br>game: {game}<br>last call: '
         f'<b>{html.escape(STATE["last_call"])}</b> '
         f'<span class="dim">({STATE["spoken"]} spoken)</span>'
         f'<br>audio engine: '
         + ('<b class="ok">✓ running</b>' if audio_ok() else
            '<b class="bad">⚠ NOT RUNNING</b> — buds will pair then '
            'drop and nothing plays. On the box, as your user, run: '
            '<code>systemctl --user enable --now pipewire '
            'pipewire-pulse wireplumber</code>')
         + _bt_audio_warning()
         + f'<br>voice link: {html.escape(str(RTC_STATE.get("s", "—")))}'
         f'<br><span class="dim">voice out: '
         f'{html.escape(str(STATE.get("voice", "— (tap Test)")))} · '
         f'sink: {html.escape(_default_sink() or "?")}</span>'
         f'<br><span class="dim">box code: {html.escape(code_version())}'
         f'</span> <form method="post" action="/update" '
         f'style="display:inline">'
         f'<button style="padding:.3rem .7rem;font-size:.75rem">'
         f'⬆ Update box</button></form>'
         f'<form method="post" action="/test" style="margin-top:.4rem">'
         f'<button class="go">🔊 Test the earpiece</button></form></div>']
    if not st['ok']:
        b.append('<div class="card"><b class="bad">bluetoothctl not '
                 'found</b> — run install_comms.sh</div>')
        return ''.join(b)
    # each connected bud with its label chips — 🧢/⚾ is how the family
    # of devices knows which ear is whose
    labs = ear_labels()
    if st['connected']:
        conn = ''
        for dv in st['connected']:
            lab = labs.get(dv['mac'].upper()) or labs.get(dv['mac']) or ''
            conn += (
                f'<div style="margin:.2rem 0">🎧 {html.escape(dv["name"])} '
                + (f'<b class="ok">— {html.escape(lab)}</b> ' if lab
                   else '<span class="warn">— unlabeled</span> ')
                + f'<form method="post" action="/earlabel" '
                f'style="display:inline">'
                f'<input type="hidden" name="mac" value="{dv["mac"]}">'
                f'<button name="v" value="catcher" style="padding:.2rem '
                f'.5rem;font-size:.72rem">🧢 catcher</button> '
                f'<button name="v" value="pitcher" style="padding:.2rem '
                f'.5rem;font-size:.72rem">⚾ pitcher</button>'
                + (f' <button name="v" value="" style="padding:.2rem '
                   f'.5rem;font-size:.72rem">✕</button>' if lab else '')
                + '</form></div>')
    else:
        conn = '<b class="warn">no earpiece connected</b>'
    locked = not st['pairable'] and not st['discoverable']
    b.append(
        f'<div class="card">earpiece: {conn}<br>bluetooth: '
        + ('<b class="ok">🔒 locked — nobody else can pair</b>'
           if locked else
           '<b class="warn">🔓 OPEN TO PAIRING — lock it after setup!</b>')
        + '<form method="post" action="/lock" style="margin-top:.4rem">'
        + (f'<button name="v" value="0">🔓 Allow pairing (to set up '
           f'a bud)</button>' if locked else
           f'<button class="lock" name="v" value="1">🔒 LOCK Bluetooth '
           f'now</button>')
        + '</form>'
        + _ghost_rows(st)
        + f'<form method="post" action="/boxname" style="margin-top:.5rem">'
        f'box name: <input name="v" value="{html.escape(box_name())}" '
        f'style="width:9rem;font-size:.9rem;padding:.3rem"> '
        f'<button style="padding:.3rem .7rem">save</button>'
        f'<br><span class="dim">what the coach page shows after '
        f'"LIVE →"</span></form></div>')
    b.append(_adapter_card())
    # ── the voice the ear speaks with ──
    cur = voice_current() or ('install default' if os.path.exists(
        PIPER_VOICE) else 'espeak fallback')
    vrows = ''.join(
        f'<form method="post" action="/voice" style="margin:.2rem 0">'
        f'<button name="v" value="{k}" '
        + ('disabled' if k == voice_current() else '')
        + f'>{html.escape(label)}'
        + (' ✓' if k == voice_current() else '')
        + '</button></form>'
        for k, (label, _slug) in VOICES.items())
    st = html.escape(VOICE_DL['msg'])
    b.append(
        '<div class="card"><b>🗣 Voice</b><br>'
        f'<span class="dim">speaking with: {html.escape(cur)}</span><br>'
        + vrows
        + (f'<span class="dim">{st}</span>' if st else
           '<span class="dim">picking one downloads it (~60 MB, a '
           'minute on field WiFi) and speaks a sample when it lands. '
           'Calls keep flowing on the old voice until then.</span>')
        + '</div>')
    if q.get('scanned'):
        devs = bt_scan()
        # Answering just now is the only evidence that matters. The rest
        # are this adapter's memory of devices it met once, which is not
        # the same claim at all and used to be printed as though it were.
        devs.sort(key=lambda d: (not d['live'], (d['name'] or '~').lower()))
        named = [d for d in devs if d['name']]
        anon = [d for d in devs if not d['name']]
        # the -BLE twin pairs fine and carries NO audio — steer around it
        ble = [d for d in named if d['name'].upper().endswith('-BLE')
               or d['name'].upper().endswith(' LE')]
        good = [d for d in named if d not in ble]
        rows = ''.join(
            f'<form method="post" action="/pair" style="margin:.2rem 0">'
            f'<button class="{"go" if d["live"] else ""}" name="mac" '
            f'value="{d["mac"]}">🎧 Pair '
            f'{html.escape(d["name"])}</button>'
            + ('' if d['live'] else '<span class="dim"> · remembered, '
                                    'not answering now</span>')
            + '</form>'
            for d in good)
        rows += ''.join(
            f'<form method="post" action="/pair" style="margin:.2rem 0">'
            f'<button name="mac" value="{d["mac"]}">'
            f'{html.escape(d["name"])}</button> '
            f'<span class="dim">← BLE twin, no audio — pair the one '
            f'without -BLE</span></form>'
            for d in ble)
        rows += ''.join(
            f'<form method="post" action="/pair" style="margin:.2rem 0">'
            f'<button name="mac" value="{d["mac"]}">🎧 Pair unnamed '
            f'device {html.escape(d["mac"])}</button>'
            + ('' if d['live'] else '<span class="dim"> · remembered, '
                                    'not answering now</span>')
            + '</form>'
            for d in anon)
        if not devs:
            rows = ('<span class="dim">nothing found — is the bud in '
                    'pairing mode (flashing)?</span>')
        nlive = sum(1 for d in devs if d['live'])
        nold = len(devs) - nlive
        tally = (f'<span class="dim">{nlive} answered this scan'
                 + (f' · {nold} remembered from before (marked ·)'
                    if nold else '')
                 + '</span><br>')
        # A bud that is merely REMEMBERED cannot be paired — it is not
        # here. Saying so beats a coach tapping it and reading a timeout.
        b.append(
            f'<div class="card"><b>Found nearby:</b><br>{tally}{rows}'
            '<form method="post" action="/scan" style="margin-top:.5rem" '
            # the scan runs synchronously in the NEXT page load
            # (~15 s) — without this the press reads as nothing
            # happening (field report, N150 settings iframe)
            "onsubmit=\"var b=this.querySelector('button');setTimeout(function(){b.disabled=true;b.textContent='🔍 Scanning… about 15 s';},0)\"" '>'
            '<button class="go">🔍 Scan again</button></form>'
            '<span class="dim">each scan runs ~12 s; WiFi and Bluetooth '
            'share the Pi\'s antenna, so 2–3 scans is normal. A bud '
            'showing as "unnamed device" is usually yours — its name '
            'often arrives a scan later.<br><br>'
            'Only earpieces in PAIRING MODE answer a scan — ordinary '
            'Bluetooth kit is invisible unless it is flashing, so a short '
            'list is normal and is not the radio. And a freshly pinned '
            'adapter has met nobody yet, so it lists only what is here '
            'right now.</span></div>')
    else:
        b.append('<div class="card">'
                 '<form method="post" action="/autopair" '
                 "onsubmit=\"var b=this.querySelector('button');setTimeout(function(){b.disabled=true;b.textContent='⚡ Listening for the flashing bud… about 20 s';},0)\"" '>'
                 '<button class="go">⚡ Pair the flashing bud '
                 '(automatic)</button></form>'
                 '<span class="dim">put the bud in pairing mode FIRST '
                 '(hold its button until it flashes), then tap — the '
                 'box grabs it the moment it appears (~20 s)</span>'
                 '<form method="post" action="/scan" '
                 'style="margin-top:.6rem" '
                 "onsubmit=\"var b=this.querySelector('button');setTimeout(function(){b.disabled=true;b.textContent='🔍 Scanning… about 15 s';},0)\"" '>'
                 '<button>🔍 Scan and pick from a list instead</button>'
                 '</form></div>')
    return ''.join(b)


def _enc_token_ok(token):
    """A sign-in minted by the ENCODER on this same box. Both apps hold
    the box's activation key (comms imports it at install), so the
    encoder settings page — already behind its own PIN/site auth — can
    hand its user straight through without a second PIN. The token is
    hmac(key, 'enc:<5-minute bucket>'); current and previous bucket
    accepted, so a link is good for 5–10 minutes. The PIN keeps
    guarding direct visits."""
    key = os.environ.get('PLAYCALL_API_KEY') or ''
    token = str(token or '')
    if not key or not token.startswith('enc-'):
        return False
    now = int(time.time() // 300)
    for b in (now, now - 1):
        want = 'enc-' + hmac.new(key.encode(), f'enc:{b}'.encode(),
                                 'sha256').hexdigest()[:32]
        if hmac.compare_digest(token, want):
            return True
    return False


def _token_ok(token):
    """Validate a one-click sign-in link from the team's comms page. A
    matching nonce proves the link came from someone with staff access on
    the site — no PIN needed. On a miss, force one poll and re-check: the
    coach taps the link the instant it is minted, which can beat our next
    scheduled poll."""
    global NONCE
    if not token:
        return False
    for attempt in (0, 1):
        if NONCE and hmac.compare_digest(str(token), str(NONCE)):
            NONCE = None                              # single use
            return True
        if attempt == 0:
            try:
                fetch()                               # refreshes NONCE
            except Exception:
                pass
    return False


class Admin(http.server.BaseHTTPRequestHandler):
    def _authed(self):
        c = self.headers.get('Cookie') or ''
        return (f'tok={TOK}' in c) or bool(PIN and f'pin={PIN}' in c)

    def _send(self, body, cookie=None):
        data = PAGE.replace('__BODY__', body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        if cookie:
            self.send_header('Set-Cookie', cookie + '; HttpOnly')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _pin_page(self, wrong=False, expired=False):
        self._send(
            '<h2>🎧 Comms box</h2><div class="card">'
            + ('<b class="bad">wrong PIN</b><br>' if wrong else '')
            + ('<b class="warn">that one-click link didn\'t verify '
               '(expired, or the box was mid-sync) — go back to the '
               'comms page and tap it again, or enter the PIN</b><br>'
               if expired else '')
            + '<form method="post" action="/pin">PIN: '
            '<input name="pin" inputmode="numeric" autofocus> '
            '<button class="go">Open</button></form>'
            '<span class="dim">printed when install_comms.sh ran; also '
            'in /etc/playcall.env</span></div>')

    def do_GET(self):
        q = {}
        if '?' in self.path:
            q = dict(p.split('=', 1) for p in
                     self.path.split('?', 1)[1].split('&') if '=' in p)
        if self.path.split('?', 1)[0] == '/login':
            # one-click link from the team's comms page (⚙ Open settings)
            _tok = urllib.parse.unquote(q.get('token', ''))
            # the encoder's pass first: local, cheap, no cloud round-trip
            if _enc_token_ok(_tok) or _token_ok(_tok):
                self.send_response(303)
                self.send_header('Set-Cookie', f'tok={TOK}; HttpOnly')
                self.send_header('Location', '/')
                self.end_headers()
                return
            return self._pin_page(expired=True)
        if not self._authed():
            return self._pin_page()
        self._send(_page_body(q))

    def do_POST(self):
        ln = int(self.headers.get('Content-Length') or 0)
        # unquote_plus matters: a MAC's colons arrive as %3A — the pair
        # button was handing bluetoothctl a percent-encoded address
        form = {k: urllib.parse.unquote_plus(v)
                for k, v in (p.split('=', 1) for p in
                             self.rfile.read(ln).decode().split('&')
                             if '=' in p)}
        if self.path == '/pin':
            if form.get('pin') == PIN:
                self.send_response(303)
                self.send_header('Set-Cookie', f'pin={PIN}; HttpOnly')
                self.send_header('Location', '/')
                self.end_headers()
                return
            return self._pin_page(wrong=True)
        if not self._authed():
            return self._pin_page()
        if self.path == '/test':
            threading.Thread(target=say, args=('radio check',),
                             daemon=True).start()
            loc = '/'
        elif self.path == '/scan':
            loc = '/?scanned=1'
        elif self.path == '/pair':
            msg = bt_pair(form.get('mac', ''))
            loc = '/?paired=' + urllib.parse.quote(msg)
        elif self.path == '/autopair':
            msg = bt_autopair()
            loc = '/?paired=' + urllib.parse.quote(msg)
        elif self.path == '/reboot':
            threading.Timer(1.5, reboot_box).start()
            loc = ('/?updated=' + urllib.parse.quote('🔄 rebooting — '
                   'this page comes back in about a minute')
                   + '&restarting=1')
        elif self.path == '/btforget':
            mac = form.get('mac', '')
            _bt('remove', mac)
            set_ear_label(mac.upper(), '')   # no chasing a forgotten bud
            loc = '/'
        elif self.path == '/earlabel':
            set_ear_label(form.get('mac', '').upper(), form.get('v', ''))
            loc = '/'
        elif self.path == '/boxname':
            try:
                open(NAME_FILE, 'w').write(form.get('v', '').strip()[:40])
            except Exception:
                pass
            loc = '/'
        elif self.path == '/update':
            changed, msg = do_update()
            if changed:
                # hand the browser its redirect first, THEN exit —
                # systemd (Restart=always) revives us on the new code
                threading.Timer(1.5, lambda: os._exit(0)).start()
                loc = ('/?updated=' + urllib.parse.quote(msg)
                       + '&restarting=1')
            else:
                loc = '/?updated=' + urllib.parse.quote(msg)
        elif self.path == '/lock':
            bt_lock(form.get('v') == '1')
            loc = '/'
        elif self.path == '/adapter':
            set_adapter_pref((form.get('mac') or '').strip())
            enforce_adapter()
            loc = '/'
        elif self.path == '/voice':
            voice_download((form.get('v') or '').strip())
            loc = '/'
        else:
            loc = '/'
        self.send_response(303)
        self.send_header('Location', loc)
        self.end_headers()

    def log_message(self, *a):
        pass


def main():
    if not KEY:
        raise SystemExit('no device key — activate the Pi first '
                         '(PLAYCALL_API_KEY in /etc/playcall.env)')
    # BEFORE anything reaches for a radio. Which controller BlueZ calls
    # "default" changes between boots, so a pinned adapter that is only
    # applied when a human taps a button is wrong every morning.
    try:
        enforce_adapter()
    except Exception:
        pass                    # a picker that fails must not cost the box
    ensure_bt_no_suspend()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=rtc_thread, daemon=True).start()
    threading.Thread(target=reconnect_loop, daemon=True).start()
    say('comms box on')
    srv = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Admin)
    srv.serve_forever()


if __name__ == '__main__':
    main()
