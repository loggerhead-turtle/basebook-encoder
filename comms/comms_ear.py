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
    req = urllib.request.Request(
        BASE + '/api/sk/device/comms',
        headers={'X-Api-Key': KEY, 'Authorization': 'Bearer ' + KEY,
                 'X-Pi-Hostname': HOSTNAME, 'X-Pi-Ip': _lan_ip(),
                 'X-Pi-Comms-Port': str(PORT),
                 'X-Pi-Name': box_name()[:40],
                 'X-Pi-Ears': ','.join(ears)[:120],
                 'X-Pi-Voice': str(RTC_STATE.get('s', ''))[:60]})
    with urllib.request.urlopen(req, timeout=6) as r:
        d = json.load(r)
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
PIPER = '/opt/piper/piper'
PIPER_VOICE = '/opt/piper/voice.onnx'


def say(text):
    """Speak, and RECORD which path actually produced sound — the admin
    page's 'voice out' line is how a silent Test explains itself."""
    _route_to_bud()
    err = ''
    if os.path.exists(PIPER) and os.path.exists(PIPER_VOICE):
        try:
            wav = tempfile.NamedTemporaryFile(suffix='.wav',
                                              delete=False).name
            r = subprocess.run([PIPER, '--model', PIPER_VOICE,
                                '--output_file', wav],
                               input=text.encode(), capture_output=True,
                               timeout=20)
            if r.returncode == 0:
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
                           'quiet', path], check=False).returncode != 0:
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


# ── bluetoothctl plumbing for the admin page ─────────────────────────────

def _bt(*args, timeout=12):
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
    two later); they show by MAC and are still pairable."""
    _bt('power', 'on')
    scan_out = _bt('--timeout', '12', 'scan', 'on', timeout=20)
    found = {}
    for mac, name in (list(_dev_lines(scan_out, tag='NEW]'))
                      + list(_dev_lines(_bt('devices')))):
        if name.replace(':', '-').upper() == mac.replace(':', '-'):
            name = ''                       # "name" is just the MAC again
        found[mac] = name or found.get(mac, '')
    return [{'mac': m, 'name': n} for m, n in found.items()]


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
            if not BT_LOCK.acquire(blocking=False):
                continue                     # someone's pairing — stand down
            try:
                for mac in missing:
                    if PAIRING['busy']:
                        break
                    out = _bt('connect', mac, timeout=8)
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
    FIRST new audio-capable device the instant it appears — no list to
    read, no MAC to recognize, no racing the bud's pairing-mode timeout
    (a failed attempt knocks most buds out of pairing mode, so speed is
    the whole game)."""
    PAIRING['busy'] = True
    scanner = None
    try:
        with BT_LOCK:
            _bt('power', 'on')
            _bt('pairable', 'on')
            known = set(_paired_macs())
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
                if 'NEW]' not in line:
                    continue
                m = re.search(r'Device ((?:[0-9A-Fa-f]{2}:){5}'
                              r'[0-9A-Fa-f]{2}) ?(.*)', line)
                if not m:
                    continue
                mac = m.group(1).upper()
                name = (m.group(2) or '').strip()
                if mac in known or not name:
                    continue
                if name.replace(':', '-').upper() == mac.replace(':', '-'):
                    continue                     # unnamed so far — wait
                up = name.upper()
                if up.endswith('-BLE') or up.endswith(' LE'):
                    continue                     # the no-audio twin
                target = (mac, name)
                break
            if not target:
                return ('no new earbud appeared — is it flashing in '
                        'pairing mode? (tap ⚡ again the moment it is)')
            mac, name = target
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
    if q.get('scanned'):
        devs = bt_scan()
        named = [d for d in devs if d['name']]
        anon = [d for d in devs if not d['name']]
        # the -BLE twin pairs fine and carries NO audio — steer around it
        ble = [d for d in named if d['name'].upper().endswith('-BLE')
               or d['name'].upper().endswith(' LE')]
        good = [d for d in named if d not in ble]
        rows = ''.join(
            f'<form method="post" action="/pair" style="margin:.2rem 0">'
            f'<button class="go" name="mac" value="{d["mac"]}">🎧 Pair '
            f'{html.escape(d["name"])}</button></form>'
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
            f'device {html.escape(d["mac"])}</button></form>'
            for d in anon)
        if not devs:
            rows = ('<span class="dim">nothing found — is the bud in '
                    'pairing mode (flashing)?</span>')
        b.append(
            f'<div class="card"><b>Found nearby:</b><br>{rows}'
            '<form method="post" action="/scan" style="margin-top:.5rem">'
            '<button class="go">🔍 Scan again</button></form>'
            '<span class="dim">each scan runs ~12 s; WiFi and Bluetooth '
            'share the Pi\'s antenna, so 2–3 scans is normal. A bud '
            'showing as "unnamed device" is usually yours — its name '
            'often arrives a scan later.</span></div>')
    else:
        b.append('<div class="card">'
                 '<form method="post" action="/autopair">'
                 '<button class="go">⚡ Pair the flashing bud '
                 '(automatic)</button></form>'
                 '<span class="dim">put the bud in pairing mode FIRST '
                 '(hold its button until it flashes), then tap — the '
                 'box grabs it the moment it appears (~20 s)</span>'
                 '<form method="post" action="/scan" '
                 'style="margin-top:.6rem">'
                 '<button>🔍 Scan and pick from a list instead</button>'
                 '</form></div>')
    return ''.join(b)


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
            if _token_ok(urllib.parse.unquote(q.get('token', ''))):
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
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=rtc_thread, daemon=True).start()
    threading.Thread(target=reconnect_loop, daemon=True).start()
    say('comms box on')
    srv = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Admin)
    srv.serve_forever()


if __name__ == '__main__':
    main()
