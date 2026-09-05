#!/usr/bin/env python3
"""Optional cloud pairing — the encoder "talks back home".

Only active when config.cloud.api_key is set. Two loops:

  * assignment poll (5 s): GET {base}/api/encoder/assignment (X-Api-Key)
        → {"assigned": bool, "team_id": str|null, "team_name": str|null,
           "bug_feed_url": str|null, "youtube_rtmp_url": str|null,
           "game_id": str|null,
           "live": {"ingest","token","game","angle"}|null}
    On change: record the team's bug feed URL in config (any hook that
    wants it is told live, no restart), rewrite the YouTube push target in
    config, and restart the push service. This is
    how ONE encoder hops between teams (Warriors at 3, Sidewinders at 5)
    with zero user action.

    "live" is the 🎦 Multi-View ingest ticket — present only while this
    box's team has a live game and the site has a stream server. It is
    written straight through to /run/playcall-encoder/live_target.json on
    every poll (encoder/live_push.py reads it there); the token inside is
    re-minted each time, so it is deliberately NOT part of the change
    signature that restarts services.

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
import os
import threading
import time
import urllib.parse
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
        self.on_feed_change = on_feed_change   # optional live hook
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
        a = self.http(f'{base}/api/encoder/assignment'
                      f'?angle={urllib.parse.quote(self.live_angle())}',
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
        # Multi-View ticket → tmpfs, on EVERY poll rather than in
        # handle_assignment: the token is re-minted each time and would
        # otherwise churn that method's change signature every 5 seconds.
        # After the shutdown check: a box on its way down needs no ticket.
        self.write_live_target(a.get('live'))
        return self.handle_assignment(a)

    def live_angle(self):
        """The angle name this box publishes under ('main' unless the
        settings page says otherwise)."""
        lp = self.cfg_load().get('live_push') or {}
        return str(lp.get('angle') or 'main')[:24]

    def write_live_target(self, live):
        """Hand the Multi-View ingest ticket to the live-push leg through
        a tmpfs file. Written every poll (it carries a fresh token) and
        blanked the moment the site stops offering one — a box whose game
        ended stops publishing without anyone restarting anything."""
        lp = self.cfg_load().get('live_push') or {}
        if not lp.get('enabled', True) or not isinstance(live, dict):
            live = None
        path = config.state_dir() / 'live_target.json'
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(live or {}))
            os.replace(tmp, path)
        except OSError:
            pass

    def handle_assignment(self, a):
        """Apply an assignment response. Returns True when anything changed
        (feed repointed / push target rewritten / service restarted)."""
        sig = (bool(a.get('assigned')), a.get('team_id'),
               a.get('bug_feed_url'), a.get('youtube_rtmp_url'),
               a.get('game_id'), a.get('push_bitrate_kbps'),
               a.get('push_codec'))
        if sig == self.last_assignment:
            return False
        self.last_assignment = sig

        cfg = self.cfg_load()
        feed = a.get('bug_feed_url') or ''
        cfg['cloud']['feed_url'] = feed
        if self.on_feed_change:
            self.on_feed_change(feed)          # live — nothing restarts

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

        # YouTube quality, set from the site's encoder card. None means
        # the cloud has no opinion (older cloud, or never set) — the
        # box's local setting stands. An int, INCLUDING 0 (= source
        # copy), is authoritative: the selector must be able to turn
        # transcoding off again.
        pk = a.get('push_bitrate_kbps')
        if pk is not None:
            try:
                pk = max(0, min(12000, int(pk)))
            except (TypeError, ValueError):
                pk = None
        if pk is not None and pk != int(cfg.get('push_bitrate_kbps') or 0):
            cfg['push_bitrate_kbps'] = pk
            restart_push = True
            log.info(f'push quality set from the cloud: '
                     + (f'{pk} kbps transcode' if pk else 'source copy'))
        # Push codec, same contract: None = no cloud opinion; 'h264' or
        # 'hevc' is authoritative. An incapable box stores it and
        # degrades at push time with a log line (see youtube_push).
        pc = a.get('push_codec')
        if pc in ('h264', 'hevc') \
                and pc != (cfg.get('push_codec') or 'h264'):
            cfg['push_codec'] = pc
            restart_push = True
            log.info(f'push codec set from the cloud: {pc}')

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

    def watch_storage(self):
        """Probe the recording disk and LOG a change of state.

        The heartbeat carries this to the site, but at a field the uplink
        is usually down — on the afternoon this was written for, the disk
        died mid-morning, the box recorded nothing all game, and the only
        trace anywhere was MediaMTX's own mkdir errors. The journal is the
        one place a failure can still be found afterwards, so it gets
        written there too. On the transition only: a line every 15 s for
        hours buries itself."""
        st = system.storage_status()
        was = getattr(self, '_storage_ok', None)
        now = bool(st.get('ok'))
        if now != was:
            if now:
                log.warning('recording disk is writable again (%s)',
                            st.get('device') or st.get('path'))
            else:
                log.error('RECORDING DISK FAILURE: %s — nothing is being '
                          'recorded and no clips can be cut from this game',
                          st.get('error') or 'not writable')
            self._storage_ok = now
        self._storage = st
        self._storage_at = time.monotonic()
        return st

    def _storage_now(self, max_age=20):
        """The last probe if it is fresh, else a new one — so the beat and
        the watcher don't each write a probe file every cycle."""
        if getattr(self, '_storage', None) is not None and \
                time.monotonic() - getattr(self, '_storage_at', 0) < max_age:
            return self._storage
        return self.watch_storage()

    def heartbeat_payload(self):
        cfg = self.cfg_load()
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
            # Recording-disk health. The stream never touches the disk,
            # so a box can push a perfect feed into a dead NVMe all
            # evening — this is the field that finally says so.
            'storage': self._storage_now(),
            # Can this box transcode the push, and to what target? The
            # site's quality selector only shows for capable boxes — a
            # Pi 5 owns no video encoder and stays copy-mode for life.
            'transcode': {
                'capable': bool(system.hw_encoder()),
                'hevc': bool(system.hw_encoders().get('hevc')),
                'target_kbps': int(cfg.get('push_bitrate_kbps') or 0),
                'codec': cfg.get('push_codec') or 'h264',
            },
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
            # Smart Coach (BLE) health, same idea — a separate key so
            # the two guns never impersonate each other on the site
            'ble_radar': (self.ble_radar_health() if callable(
                getattr(self, 'ble_radar_health', None)) else None),
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
            # Watched every beat whether or not this box is paired, and
            # whether or not the cloud is reachable — an unpaired or
            # offline box is exactly the one whose disk failure nobody
            # would otherwise ever hear about.
            try:
                self.watch_storage()
            except Exception as e:
                log.debug(f'storage watch failed: {e}')
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
