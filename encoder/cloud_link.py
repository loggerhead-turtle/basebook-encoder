#!/usr/bin/env python3
"""Optional cloud pairing — the encoder "talks back home".

Only active when config.cloud.api_key is set. Two loops:

  * assignment poll (5 s): GET {base}/api/encoder/assignment (X-Api-Key)
        → {"assigned": bool, "team_id": str|null, "team_name": str|null,
           "bug_feed_url": str|null, "youtube_rtmp_url": str|null,
           "game_id": str|null}
    On change: repoint the scorebug feed live (no restart), rewrite the
    YouTube push target in config, and restart the push service. This is
    how ONE encoder hops between teams (Warriors at 3, Sidewinders at 5)
    with zero user action.

  * heartbeat (15 s): POST {base}/api/encoder/heartbeat (X-Api-Key)
        {"state": "idle|receiving|pushing",
         "ingest": {"connected": bool, "kbps": int|null},
         "push": {"connected": bool, "kbps": int|null, "reconnects_5m": int},
         "cpu": float, "temp": float|null, "version": str,
         "log_tail": [last 20 log lines]}

Ingest detection reads the MediaMTX control API
(GET http://127.0.0.1:9997/v3/paths/list — enabled in our mediamtx.yml);
push stats come from the status JSON youtube_push.py maintains.

Every failure is a silent retry — the encoder must keep encoding when the
cloud is down, on a dead uplink, or before pairing ever happened.
"""

import json
import logging
import threading
import time
import urllib.request

from . import __version__, config, system

log = logging.getLogger('cloud_link')

ASSIGNMENT_INTERVAL = 5
HEARTBEAT_INTERVAL = 15
VERSION_CHECK_INTERVAL = 6 * 3600
MEDIAMTX_API = 'http://127.0.0.1:9997'
YOUTUBE_UNIT = 'playcall-encoder-youtube'


def _http_json(url, headers=None, payload=None, timeout=6):
    """GET (payload None) or POST-JSON; returns parsed JSON or raises."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


class CloudLink:
    def __init__(self, cfg_load=config.load, cfg_save=config.save,
                 on_feed_change=None, runner=None, http=None):
        self.cfg_load = cfg_load
        self.cfg_save = cfg_save
        self.on_feed_change = on_feed_change   # e.g. sender.set_feed
        self.runner = runner or system.run
        self.http = http or _http_json         # monkeypatch point for tests
        self.running = True
        self.last_assignment = None
        self.assignment = None                 # last raw response (for web UI)
        self.latest_version = None             # from the version check
        self._ingest_prev = None               # (bytesReceived, monotonic)
        self.last_ok = 0.0                     # monotonic of last cloud reply
        # One-click sign-in nonce handed down by the site (see
        # /api/sk/encoder/<id>/login-link). Memory only — it is short-lived
        # by design and must not survive a reboot.
        self.login_nonce = None

    # ── config plumbing ──────────────────────────────────────────────────────
    def _cloud(self):
        c = self.cfg_load().get('cloud') or {}
        base = (c.get('base_url') or '').rstrip('/')
        return base, c.get('api_key') or ''

    def enabled(self):
        base, key = self._cloud()
        return bool(base and key)

    def _headers(self):
        return {'X-Api-Key': self._cloud()[1]}

    # ── assignment ───────────────────────────────────────────────────────────
    def poll_assignment_once(self):
        base, _ = self._cloud()
        a = self.http(f'{base}/api/encoder/assignment',
                      headers=self._headers())
        self.last_ok = time.monotonic()        # cloud reachable
        if not isinstance(a, dict):
            return False
        self.assignment = a
        # Captured here rather than in handle_assignment: that method
        # short-circuits when the assignment itself is unchanged, which is
        # the normal case when a coach asks for a sign-in link.
        nonce = a.get('login_nonce')
        self.login_nonce = str(nonce) if nonce else None
        if a.get('shutdown'):
            # The coach pressed "power off" on the pad — the box lives on
            # a power bank with no keyboard, and unplugging it hot is what
            # corrupted the recordings drive. The cloud clears the flag AS
            # it serves it (read-once), so the next boot's first poll can
            # never re-kill the box. poweroff, not halt: systemd stops
            # mediamtx and the push cleanly and unmounts the recordings
            # drive, which is the entire point of the button.
            log.warning('cloud requested shutdown — powering off')
            self.running = False
            self.runner(['systemctl', 'poweroff'])
            return True
        return self.handle_assignment(a)

    def handle_assignment(self, a):
        """Apply an assignment response. Returns True when anything changed
        (feed repointed / push target rewritten / service restarted)."""
        sig = (bool(a.get('assigned')), a.get('team_id'),
               a.get('bug_feed_url'), a.get('youtube_rtmp_url'),
               a.get('game_id'))
        if sig == self.last_assignment:
            return False
        self.last_assignment = sig

        cfg = self.cfg_load()
        feed = a.get('bug_feed_url') or ''
        cfg['cloud']['feed_url'] = feed
        if self.on_feed_change:
            self.on_feed_change(feed)          # live — scorebug never restarts

        restart_push = False
        yt_url = a.get('youtube_rtmp_url')
        if yt_url:
            from .provisioning import normalize_youtube
            url, key = normalize_youtube(yt_url)
            if {'url': url, 'key': key} != cfg.get('youtube'):
                cfg['youtube'] = {'url': url, 'key': key}
                restart_push = True
        elif not a.get('assigned'):
            # Unassigned → stop pushing whatever the previous team streamed.
            if cfg['youtube'].get('key'):
                cfg['youtube']['key'] = ''
                restart_push = True

        self.cfg_save(cfg)
        if restart_push:
            self.runner(['systemctl', 'restart', YOUTUBE_UNIT])
        log.info(f"assignment: {a.get('team_name') or 'none'} "
                 f"(game {a.get('game_id')})")
        return True

    def assignment_loop(self):
        while self.running:
            try:
                if self.enabled():
                    self.poll_assignment_once()
            except Exception as e:
                log.debug(f'assignment poll failed (retrying): {e}')
            time.sleep(ASSIGNMENT_INTERVAL)

    # ── telemetry ────────────────────────────────────────────────────────────
    def ingest_status(self):
        """{'connected': bool, 'kbps': int|None} from the MediaMTX API."""
        cfg = self.cfg_load()
        want = f"live/{cfg.get('local_ingest_key', '')}"
        try:
            data = self.http(f'{MEDIAMTX_API}/v3/paths/list')
        except Exception:
            return {'connected': False, 'kbps': None}
        for item in (data or {}).get('items') or []:
            if item.get('name') != want:
                continue
            connected = bool(item.get('ready'))
            kbps = None
            rx = item.get('bytesReceived')
            now = time.monotonic()
            if isinstance(rx, int):
                if self._ingest_prev:
                    p_rx, p_t = self._ingest_prev
                    dt = now - p_t
                    if dt > 0 and rx >= p_rx:
                        kbps = int((rx - p_rx) * 8 / dt / 1000)
                self._ingest_prev = (rx, now)
            return {'connected': connected, 'kbps': kbps}
        self._ingest_prev = None
        return {'connected': False, 'kbps': None}

    def push_status(self):
        """{'connected','kbps','reconnects_5m'} from youtube_push's status
        file; stale (>30 s) files count as disconnected."""
        path = config.state_dir() / 'push.json'
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {'connected': False, 'kbps': None, 'reconnects_5m': 0}
        fresh = time.time() - data.get('updated', 0) < 30
        cutoff = time.time() - 300
        reconnects = sum(1 for t in data.get('reconnect_times', [])
                         if t >= cutoff)
        return {'connected': bool(data.get('connected')) and fresh,
                'kbps': data.get('kbps') if fresh else None,
                'reconnects_5m': reconnects}

    def clips_status(self):
        """{'pending','uploaded','failed','last_error'} from clipper.py's
        status file; a stale (>120 s) file means the clipper isn't running.
        Its poll loop is 5 s, so 120 s is generous slack for a busy cut."""
        path = config.state_dir() / 'clips.json'
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {'running': False, 'pending': 0, 'uploaded': 0,
                    'failed': 0, 'last_error': ''}
        return {'running': time.time() - data.get('updated', 0) < 120,
                'pending': int(data.get('pending') or 0),
                'uploaded': int(data.get('uploaded') or 0),
                'failed': int(data.get('failed') or 0),
                'last_error': data.get('last_error') or ''}

    def _rtmp_urls(self):
        """Camera-facing ingest URLs, raw IP FIRST — the address that keeps
        working when mDNS doesn't (phone hotspots and field routers
        routinely drop multicast, which is exactly the .local failure seen
        at the field). Same trust level as the PIN that already rides the
        heartbeat: the site reveals them to team staff only."""
        try:
            key = self.cfg_load().get('local_ingest_key', '')
            urls = []
            ip = system.lan_ip()
            if ip:
                urls.append(f'rtmp://{ip}:1935/live/{key}')
            urls.append(f'rtmp://{system.hostname()}.local:1935/live/{key}')
            return urls
        except Exception:
            return []

    def _temp_peak(self):
        """Highest temperature seen since the previous heartbeat, then
        reset. Sampled by the heartbeat loop; an instantaneous reading
        misses the spikes that actually throttle the box."""
        t = system.cpu_temp()
        peak = max([v for v in (t, getattr(self, '_temp_hi', None))
                    if v is not None] or [None])
        self._temp_hi = None
        return peak

    def note_temp(self):
        """Cheap sample between beats — call from any loop."""
        t = system.cpu_temp()
        if t is not None:
            hi = getattr(self, '_temp_hi', None)
            self._temp_hi = t if hi is None else max(hi, t)

    def heartbeat_payload(self):
        ingest = self.ingest_status()
        push = self.push_status()
        state = ('pushing' if push['connected']
                 else 'receiving' if ingest['connected'] else 'idle')
        return {
            'state': state,
            'ingest': {'connected': ingest['connected'],
                       'kbps': ingest['kbps']},
            'push': push,
            'clips': self.clips_status(),
            'cpu': system.cpu_percent(),
            'temp': system.cpu_temp(),
            # the PEAK since the last beat: a box that spikes to 82 °C
            # between 15 s samples is throttling in ways an instant
            # reading hides
            'temp_max': self._temp_peak(),
            # Radar chain health (gun parse rate, board writes). Set by
            # __main__ when the radar service starts; absent on boxes
            # without a gun.
            'radar': (self.radar_health() if callable(
                getattr(self, 'radar_health', None)) else None),
            'version': __version__,
            # So the site can link straight to this box's settings page
            # instead of assuming playcall-encoder.local resolves.
            'ip': system.lan_ip(),
            'hostname': system.hostname(),
            # The full camera-facing ingest URLs (IP first, mDNS second).
            # Field routers hand out a NEW address most weeks and mDNS
            # regularly fails on hotspots, so the site shows THESE — always
            # current as of the last heartbeat — instead of a stale note
            # from install day.
            'rtmp_urls': self._rtmp_urls(),
            # The settings PIN rides the authenticated heartbeat so team
            # staff can recover it from the site ("Show settings PIN" on
            # the encoder card) instead of SSHing into the box. This link
            # is already trusted with the stream assignment; the site
            # gates the reveal to staff.
            'pin': str((config.load().get('device') or {}).get('pin') or ''),
            # Journald lines can contain ffmpeg's push URL (stream key) —
            # scrub before anything leaves the box.
            'log_tail': config.redact_lines(system.journal_tail(20),
                                            self.cfg_load()),
        }

    def send_heartbeat_once(self):
        base, _ = self._cloud()
        self.http(f'{base}/api/encoder/heartbeat',
                  headers=self._headers(), payload=self.heartbeat_payload())
        self.last_ok = time.monotonic()

    def recently_ok(self, window=60):
        """True when a cloud call succeeded within `window` seconds — the
        box clearly has working connectivity, whatever the interface."""
        return bool(self.last_ok) and time.monotonic() - self.last_ok < window

    def heartbeat_loop(self):
        while self.running:
            try:
                if self.enabled():
                    self.send_heartbeat_once()
            except Exception as e:
                log.debug(f'heartbeat failed (retrying): {e}')
            time.sleep(HEARTBEAT_INTERVAL)

    # ── version check ────────────────────────────────────────────────────────
    def check_version_once(self):
        cfg = self.cfg_load()
        if not (cfg.get('version_check') or {}).get('enabled', True):
            return None
        url = config.version_check_url(cfg)
        if not url:
            return None
        data = self.http(url)
        if isinstance(data, dict) and data.get('latest'):
            self.latest_version = data['latest']
        return self.latest_version

    def version_loop(self):
        while self.running:
            try:
                self.check_version_once()
            except Exception:
                pass
            time.sleep(VERSION_CHECK_INTERVAL)

    def start_threads(self):
        threads = [threading.Thread(target=self.assignment_loop, daemon=True),
                   threading.Thread(target=self.heartbeat_loop, daemon=True),
                   threading.Thread(target=self.version_loop, daemon=True)]
        for t in threads:
            t.start()
        return threads
