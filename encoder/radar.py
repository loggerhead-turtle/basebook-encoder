#!/usr/bin/env python3
"""Stalker Pro II s radar capture — the gun's serial cable lands on this
box via a USB→RS-232 adapter (/dev/serial/by-id/..., 19200 8N1).

Wire format (confirmed on the bench against the gun's display):

  Multi-value streaming frames, one per line:
      ˆRD   34C 537         5C 589 869     9A15650
  34C field = live/rolldown speed ×10 (the decel curve, settles on plate
  speed); 5C field = peak ×10; the last field is spin — `6A` while empty,
  `9A<rpm×10>` once the gun computes it (a few frames after peak lock).
  Idle keepalive frames carry the tags with empty values — they are the
  gun-connected heartbeat and are never stored.

  The tag SUFFIX letter varies with the gun's format/units setting: a
  field unit sent `34A … 5A … 6A` (no constant field while idle), which
  the original 34C/5C-only pattern silently rejected — a live, streaming
  gun read as "no radar". Any letter is accepted now. Guns in this mode
  also end frames with a bare \r, so one serial read can carry several
  frames glued together — handle_line() splits and feeds each.

  Format A fallback: a bare fixed-width speed per line (` 80.1`), idle
  keepalive `   . ` lines.

This module reduces the stream to:
  * LIVE readings (velo/rpm) pushed to the cloud within a frame or two of
    peak lock — the pad tile, score-bug overlay, and clips ride these;
  * BURST events (one per tracked object) classified pitch / throw /
    ghost by shape: velocity band + track duration (a pitch tracks ~0.5 s
    over 55 ft; a CF throw home tracks 2–3 s) + minimum frames.

Storage is bounded by design: idle frames are dropped at read time,
events are tiny, and nothing raw is retained here.

The cloud stamps events with the live game context on arrival. NOTHING
in this pipeline can write to the scorebook — velo is decoration.
"""

from __future__ import annotations

import glob
import logging
import re
import threading
import time
from collections import deque

log = logging.getLogger('radar')

BAUD = 19200
POST_PATH = '/api/encoder/radar'
LIVE_MIN_INTERVAL = 0.35      # throttle live pushes
ALIVE_INTERVAL = 10           # keepalive push while idle
GAP_S = 1.0                   # quiet gap that closes a burst
MIN_FRAMES = 3                # fewer = ghost
PITCH_MAX_DUR = 1.6           # longer in the beam = a throw, not a pitch
BAND = (30.0, 110.0)          # plausible pitch band (pref: set the GUN's
                              # LO threshold BELOW the slowest pitcher and
                              # let software filter — see the roadmap doc)

# ˆRD   34C [live]   5C [peak]   [const]   [6A | 9A<spin>]
# The letter after 34/5 depends on the gun's format/units setting (34C on
# the bench, 34A in the field). The constant field exists in C mode
# (every frame, idle included) and is absent in A mode — so after the
# 5-tag there may be zero, one, or two numbers, and a LONE number is
# ambiguous: C-idle's constant, or A-live's peak. parse_frame settles it
# by the 34 field: a frame with a live speed is a reading (lone number =
# peak); a frame with an empty 34 field is idle (lone number = const).
_FRAME = re.compile(
    r'RD\s+34[A-Z]\s+(?:(\d{2,5})\s+)?5[A-Z]\s+(?:(\d{2,5})\s+)?'
    r'(?:(\d{2,5})\s+)?(?:6A|9A(\d{3,7}))')
# Format A: one space-padded speed per line; idle = spaces + '.'
_FMT_A = re.compile(r'^\s*(\d{1,3}(?:\.\d)?)\s*$')


def parse_frame(line):
    """One serial line → {'live','peak','rpm','alive'} (values may be
    None) or None when the line carries nothing radar-shaped."""
    if not line:
        return None
    m = _FRAME.search(line)
    if m:
        live, n1, n2, spin = (m.group(1), m.group(2), m.group(3),
                              m.group(4))
        # two numbers after the 5-tag → peak + const; a lone number is
        # the peak only when the frame carries a live speed (see _FRAME)
        peak = n1 if (n2 or live) else None
        return {
            'live': int(live) / 10.0 if live else None,
            'peak': int(peak) / 10.0 if peak else None,
            'rpm': int(spin) / 10.0 if spin else None,
            'alive': True,
        }
    m = _FMT_A.match(line)
    if m:
        return {'live': float(m.group(1)), 'peak': None, 'rpm': None,
                'alive': True}
    if line.strip().rstrip('.') == '' and '.' in line:
        return {'live': None, 'peak': None, 'rpm': None, 'alive': True}
    return None


class BurstEngine:
    """Frames in → classified burst events out. A burst is a run of
    frames carrying values, closed by GAP_S of quiet."""

    def __init__(self, gap=GAP_S, min_frames=MIN_FRAMES,
                 pitch_max_dur=PITCH_MAX_DUR, band=BAND):
        self.gap = gap
        self.min_frames = min_frames
        self.pitch_max_dur = pitch_max_dur
        self.band = band
        self._frames = []            # (t, live, peak, rpm)
        self._last_value_t = None

    def feed(self, frame, t=None):
        """Feed one parsed frame; returns a closed event dict or None."""
        t = time.monotonic() if t is None else t
        ev = None
        has_value = frame and (frame.get('live') is not None
                               or frame.get('peak') is not None
                               or frame.get('rpm') is not None)
        if (self._frames and self._last_value_t is not None
                and t - self._last_value_t > self.gap):
            ev = self._close()
        if has_value:
            self._frames.append((t, frame.get('live'), frame.get('peak'),
                                 frame.get('rpm')))
            self._last_value_t = t
        return ev

    def flush(self, t=None):
        t = time.monotonic() if t is None else t
        if self._frames and self._last_value_t is not None \
                and t - self._last_value_t > self.gap:
            return self._close()
        return None

    def _close(self):
        frames = self._frames
        self._frames = []
        self._last_value_t = None
        speeds = [v for _t, lv, pk, _r in frames
                  for v in (lv, pk) if v is not None]
        rpms = [r for _t, _lv, _pk, r in frames if r is not None]
        lives = [lv for _t, lv, _pk, _r in frames if lv is not None]
        if not speeds:
            return None
        peak = max(speeds)
        dur = round(frames[-1][0] - frames[0][0], 2)
        if len(frames) < self.min_frames \
                or not self.band[0] <= peak <= self.band[1]:
            kind = 'ghost'
        elif dur > self.pitch_max_dur:
            kind = 'throw'
        else:
            kind = 'pitch'
        return {'kind': kind, 'peak': round(peak, 1),
                'plate': round(lives[-1], 1) if lives else None,
                'rpm': round(max(rpms), 1) if rpms else None,
                'frames': len(frames), 'dur': dur}


ROTATE_AFTER = 60.0   # silent this long with other adapters present → rotate


def find_ports(cfg=None):
    """Candidate USB-RS232 adapters, stable by-id paths preferred. A
    configured radar.port pins ONE and disables rotation."""
    want = ((cfg or {}).get('radar') or {}).get('port') or ''
    if want:
        return [want]
    byid = sorted(glob.glob('/dev/serial/by-id/*'))
    if byid:
        return byid
    return sorted(glob.glob('/dev/ttyUSB*'))


def find_port(cfg=None):
    """The first candidate (kept for callers that need exactly one)."""
    ports = find_ports(cfg)
    return ports[0] if ports else None


class RadarService:
    """Serial → parse → bursts → cloud. Runs as a daemon thread; silently
    idles when no adapter is plugged in or pyserial is missing, and
    re-scans so plugging the gun in mid-game just works."""

    def __init__(self, link, cfg_load=None):
        self.link = link                    # CloudLink (auth + http)
        self.cfg_load = cfg_load or (lambda: {})
        self.engine = BurstEngine()
        self.running = True
        self.pending = deque(maxlen=200)    # events awaiting a good POST
        self._last_live_post = 0.0
        self._last_alive_post = 0.0
        self._last_live = (None, None)
        self.port = None
        self.connected = False
        # Observability: "no velo showed up" was undiagnosable from the
        # journal because everything below INFO was silent. Counters +
        # first-N samples make one `journalctl | grep radar` the answer.
        self.lines_seen = 0
        self.frames_parsed = 0
        self.unparsed = 0
        self._post_fails = 0
        # Display passthrough: the SECOND adapter on this box is the
        # Stalker LED display board for the fans. The gun's raw lines
        # are forwarded to it verbatim, so the board behaves exactly as
        # if it were cabled straight to the gun.
        self._serial_cls = None        # set once pyserial imports
        self._disp = None
        self._disp_port = None
        self._disp_warned = 0.0

    # ── cloud ────────────────────────────────────────────────────────────────
    def _post(self, payload):
        base, _ = self.link._cloud()
        if not base:
            return False
        try:
            self.link.http(f'{base}{POST_PATH}',
                           headers=self.link._headers(), payload=payload)
            self._post_fails = 0
            return True
        except Exception as e:
            # first failure and every 50th at WARN — visible in the
            # journal without letting a dead uplink flood it
            self._post_fails += 1
            if self._post_fails == 1 or self._post_fails % 50 == 0:
                log.warning(f'radar post failed x{self._post_fails} '
                            f'(retrying): {e}')
            else:
                log.debug(f'radar post failed (retrying): {e}')
            return False

    def push(self, live=None, event=None, force_alive=False, now=None):
        now = time.monotonic() if now is None else now
        if event:
            self.pending.append(event)
        want_live = (live is not None
                     and now - self._last_live_post >= LIVE_MIN_INTERVAL
                     and live != self._last_live)
        want_alive = force_alive \
            or now - self._last_alive_post >= ALIVE_INTERVAL
        if not (want_live or self.pending or want_alive):
            return
        payload = {'alive': True}
        if want_live:
            payload['live'] = {'velo': live[0], 'rpm': live[1]}
        if self.pending:
            payload['events'] = list(self.pending)
        if self._post(payload):
            if want_live:
                self._last_live_post = now
                self._last_live = live
            self._last_alive_post = now
            if 'events' in payload:
                self.pending.clear()

    # ── display board passthrough ────────────────────────────────────────────
    def _close_display(self):
        d, self._disp = self._disp, None
        self._disp_port = None
        if d is not None:
            try:
                d.close()
            except Exception:
                pass

    def forward_display(self, raw, target):
        """Write one raw gun line to the LED board's adapter. Best
        effort forever: the board vanishing mid-game must never touch
        the capture side."""
        if not raw or not target or self._serial_cls is None:
            return
        try:
            if self._disp is None or self._disp_port != target:
                self._close_display()
                self._disp = self._serial_cls(target, BAUD, timeout=0,
                                              write_timeout=0.2)
                self._disp_port = target
                log.info(f'forwarding radar to the display board on {target}')
            self._disp.write(raw)
        except Exception as e:
            self._close_display()
            now = time.monotonic()
            if now - self._disp_warned > 30:
                self._disp_warned = now
                log.warning(f'display board forward failed ({e}) — '
                            'will keep retrying')

    # ── serial loop ──────────────────────────────────────────────────────────
    def handle_line(self, line, t=None):
        """One serial READ through the whole pipeline (test entrypoint).
        A-mode guns end frames with a bare \r and no \n, so one
        readline() chunk can carry several frames glued together —
        split and feed each; the last event wins the return."""
        segs = [s for s in (line or '').split('\r') if s.strip()]
        if len(segs) > 1:
            ev = None
            for seg in segs:
                got = self.handle_line(seg, t=t)
                if got is not None:
                    ev = got
            return ev
        self.lines_seen += 1
        if self.lines_seen <= 3:
            # the first few RAW lines, escaped — one glance settles
            # "is the gun talking, and in which format?"
            log.info(f'radar rx sample: {line[:80]!r}')
        frame = parse_frame(line)
        if frame is None:
            self.unparsed += 1
            if self.unparsed <= 3:
                log.info(f'radar line did not parse: {line[:80]!r}')
            elif self.unparsed % 200 == 0:
                log.warning(f'radar: {self.unparsed} unparsed lines of '
                            f'{self.lines_seen} — wrong format/baud?')
            return None
        self.frames_parsed += 1
        if self.frames_parsed % 500 == 0:
            log.info(f'radar: {self.frames_parsed} frames parsed '
                     f'({self.lines_seen} lines, {self.unparsed} unparsed)')
        ev = self.engine.feed(frame, t=t)
        if ev and ev.get('kind') != 'ghost':
            log.info(f"radar {ev['kind']}: {ev['peak']} mph"
                     + (f", {ev['rpm']} rpm" if ev.get('rpm') else '')
                     + f" ({ev['frames']} frames, {ev['dur']}s)")
        live = None
        cur = frame.get('peak') if frame.get('peak') is not None \
            else frame.get('live')
        if cur is not None:
            live = (cur, frame.get('rpm'))
        self.push(live=live, event=ev, now=t)
        return ev

    def loop(self):
        try:
            import serial
        except ImportError:
            log.info('pyserial not installed — radar capture disabled')
            return
        self._serial_cls = serial.Serial
        # A box can carry more than one USB-serial adapter, and a whole
        # game was lost to picking the wrong one alphabetically and
        # sitting on its silence. With multiple candidates we now rotate
        # to the next after ROTATE_AFTER seconds without a byte — the
        # gun's keepalive frames mean the RIGHT port is never silent.
        port_idx = 0
        missing_logged = False
        while self.running:
            cfg = self.cfg_load()
            if ((cfg.get('radar') or {}).get('enabled') or 'auto') == 'off':
                time.sleep(10)
                continue
            ports = find_ports(cfg)
            if not ports:
                self.connected = False
                if not missing_logged:
                    log.info('no USB-serial adapter present — radar idle, '
                             'watching for one')
                    missing_logged = True
                time.sleep(5)
                continue
            missing_logged = False
            self.port = ports[port_idx % len(ports)]
            if len(ports) > 1:
                log.info(f'{len(ports)} serial adapters present: {ports} — '
                         f'listening on {self.port}, rotating after '
                         f'{int(ROTATE_AFTER)}s of silence')
            # the LED board's adapter: pinned by config, else whichever
            # candidate we are NOT reading the gun on
            disp_pin = (cfg.get('radar') or {}).get('display_port') or None
            try:
                with serial.Serial(self.port, BAUD, timeout=1) as ser:
                    log.info(f'radar listening on {self.port} @ {BAUD}')
                    self.connected = True
                    last_rx = time.monotonic()
                    while self.running:
                        raw = ser.readline()
                        line = raw.decode('ascii', 'replace') if raw else ''
                        if line:
                            last_rx = time.monotonic()
                            self.handle_line(line)
                            others = [p for p in ports if p != self.port]
                            self.forward_display(
                                raw, disp_pin or (others[0] if others
                                                  else None))
                        else:
                            ev = self.engine.flush()
                            self.push(event=ev)
                            if (len(ports) > 1 and time.monotonic()
                                    - last_rx > ROTATE_AFTER):
                                port_idx += 1
                                log.info(f'no data on {self.port} for '
                                         f'{int(ROTATE_AFTER)}s — trying '
                                         'the next adapter')
                                break
            except Exception as e:
                self.connected = False
                port_idx += 1          # a dead adapter shouldn't wedge us
                log.warning(f'radar serial dropped ({e}) — rescanning')
                time.sleep(3)
            finally:
                self._close_display()

    def start_thread(self):
        t = threading.Thread(target=self.loop, daemon=True)
        t.start()
        return t
