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
import os
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
    r'RD\s+34[A-Z]\s+(?:(\d{2,5})\s+)?5[A-Z]\s+(?:(\d{2,9})\s+)?'
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
        # GLUED FIELDS: the gun writes peak and the constant field at
        # fixed width, and once the constant reached four digits they
        # ran together — '5G 8571024' is peak 857 + const 1024, with no
        # separator. That failed the whole line, so a streaming gun read
        # as mostly-garbage and the LED board (fed only from parsed
        # frames) went dark. Split on the first plausible speed.
        if n1 and len(n1) > 5 and not n2:
            for cut in (3, 4):
                head = n1[:cut]
                if 200 <= int(head) <= 1200:      # 20.0–120.0 mph ×10
                    n1, n2 = head, n1[cut:]
                    break
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

    @staticmethod
    def _carries_a_ball(frame):
        """Is something actually moving through the beam right now?

        This is the whole burst boundary, and getting it wrong is silent
        and total. A Stalker in constant-on mode streams a frame many
        times a second forever, and between pitches it reports
        `live 0.0` while still LATCHING the last peak and rpm — so a
        frame that means "nothing is happening" arrives carrying three
        non-None numbers.

        Treating those as values kept every burst open. A whole game
        came back as SIX bursts of 22,500 frames, each minutes long,
        every one of them longer than PITCH_MAX_DUR and therefore filed
        as a throw. The velocity tile looked perfect the entire time,
        because the live reading is pushed separately — so the failure
        was invisible until the play-by-play had no speeds on it.

        `live` is the instantaneous reading and is authoritative when
        present: zero means no ball. peak and rpm are latched, so they
        may only speak for a frame that has no live field at all —
        which is what keeps guns that report peak alone working.
        """
        if not frame:
            return False
        live = frame.get('live')
        if live is not None:
            return live > 0
        return (frame.get('peak') or 0) > 0 or (frame.get('rpm') or 0) > 0

    def feed(self, frame, t=None):
        """Feed one parsed frame; returns a closed event dict or None."""
        t = time.monotonic() if t is None else t
        ev = None
        has_value = self._carries_a_ball(frame)
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


def find_ports(cfg=None):
    """Candidate serial adapters, stable by-id paths preferred.

    A configured radar.port pins the GUN to one of them — but only while
    that path still EXISTS. A pin is a by-id path carrying the adapter's
    own serial number, so an adapter that is unplugged, swapped, or has
    died takes the pin down with it. This used to return that one dead
    path unconditionally: the open failed, no handles were left, the
    service slept five seconds and tried the same missing path again,
    for ever. A field box logged that line every five seconds all night
    and never once looked at the adapter that was actually plugged in.
    A pin is a preference, not a suicide pact.

    /dev/rfcomm* is included so a gun on the far end of a Bluetooth
    serial adapter is found the same way a cabled one is — an rfcomm
    binding is an ordinary tty, it just lives nowhere near
    /dev/serial/by-id."""
    want = ((cfg or {}).get('radar') or {}).get('port') or ''
    if want:
        if os.path.exists(want):
            return [want]
        log.warning('pinned radar port %s is not there — scanning for any '
                    'adapter instead (plug it back in, or clear '
                    'radar.port)', want)
    found = (sorted(glob.glob('/dev/serial/by-id/*'))
             or sorted(glob.glob('/dev/ttyUSB*')))
    return found + sorted(glob.glob('/dev/rfcomm*'))


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
        self.disp_writes = 0
        self.disp_fails = 0
        self.gun_baud = BAUD
        self.cfg_pinned = False
        self.cfg_disp_pinned = False
        self._disp_warned = 0.0
        self.last_frame = None         # newest parse result, for the board

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

    # More than this queued for the board's UART = it is behind; DROP the
    # frame instead of queueing. The board only ever shows the LATEST
    # reading — queued history plays back as a strobe of stale numbers,
    # and blocked writes chop frames mid-byte, which desyncs the board's
    # parser into garbage segments ("8 / JN / H" flicker, field report).
    DISP_MAX_BACKLOG = 256

    @staticmethod
    def display_line(frame):
        """What the LED board is SENT for one parsed gun frame, in the
        board's own language. The gun's streaming output is the
        multi-tag EA format — feeding that to a Stalker display renders
        garbage fragments, because the board expects the classic
        display protocol: a bare right-aligned speed (' 57.8'), exactly
        the parser's format-A. Value frames only — idle keepalives are
        not display traffic, the board holds its last number."""
        if not frame:
            return None
        v = frame.get('live') if frame.get('live') is not None \
            else frame.get('peak')
        if v is None:
            return None
        return ('%5.1f\r' % v).encode('ascii')

    def health(self):
        """One dict the heartbeat carries so the SITE can show whether
        the gun and the board are actually working — today's whole
        outage class ('velocity fine, board dark', '80% of frames
        rejected') was invisible from the dugout and cost an afternoon
        of SSH. Everything here is cheap and already counted."""
        lines = self.lines_seen
        parsed = self.frames_parsed
        return {
            'connected': bool(self.connected),
            'port': self.port,
            'baud': self.gun_baud,
            'lines': lines,
            'parsed': parsed,
            # the number that would have named the glued-field bug in
            # one glance instead of a log dive
            'parse_pct': (round(100.0 * parsed / lines, 1)
                          if lines else None),
            'display_port': (self._disp_port[0] if self._disp_port
                             else None),
            'display_baud': (self._disp_port[1] if self._disp_port
                             else None),
            'display_writes': self.disp_writes,
            'display_fails': self.disp_fails,
            # CONFIG DRIFT: 'auto' means the roles are being guessed
            # from whatever enumerated first — which is how a replug
            # once handed the gun's speeds to the gun's own adapter.
            # The site says pinned/auto so drift is visible, not
            # discovered at a field.
            'pinned': bool(self.cfg_pinned),
            'display_pinned': bool(self.cfg_disp_pinned),
        }

    def forward_display(self, raw, target, baud=BAUD):
        """Write one raw gun line to the LED board's adapter. Best
        effort forever: the board vanishing mid-game must never touch
        the capture side, and a slow board never queues history — a
        frame the board has no room for is dropped whole.

        BAUD IS SEPARATE from the gun's: display boards commonly run
        slower than the gun (2400/9600 are both in the wild), and a
        mismatched board shows NOTHING rather than garbage — set
        radar.display_baud when a board stays dark."""
        if not raw or not target or self._serial_cls is None:
            return
        try:
            if self._disp is None or self._disp_port != (target, baud):
                self._close_display()
                self._disp = self._serial_cls(target, baud, timeout=0,
                                              write_timeout=0.2)
                self._disp_port = (target, baud)
                log.info(f'forwarding radar to the display board on '
                         f'{target} @ {baud}')
            try:
                behind = self._disp.out_waiting
            except Exception:
                behind = 0
            if behind > self.DISP_MAX_BACKLOG:
                return                      # board is behind — drop, whole
            self._disp.write(raw)
            self.disp_writes += 1
        except Exception as e:
            self.disp_fails += 1
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
        self.last_frame = frame          # the loop drives the LED board off this
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
            # EVERY reading, in a form a person can read back and a script
            # can parse. A game's velocities once reached the cloud as six
            # merged bursts and the per-pitch numbers were simply gone —
            # not mis-filed, gone, because the only durable copy was the
            # one the cloud kept. The box sees each reading first; a line
            # in the journal costs ~24 KB a game and makes the same loss
            # recoverable with `journalctl -u playcall-encoder | grep
            # 'radar pitch'` instead of unrecoverable.
            #
            # The trailing key=value tail is deliberate: it survives being
            # pasted into a support thread and reads as a table.
            log.info(f"radar {ev['kind']}: {ev['peak']} mph"
                     + (f", {ev['rpm']} rpm" if ev.get('rpm') else '')
                     + f" ({ev['frames']} frames, {ev['dur']}s)"
                     + f" | peak={ev['peak']}"
                     + f" plate={ev.get('plate')}"
                     + f" rpm={ev.get('rpm')}"
                     + f" frames={ev['frames']} dur={ev['dur']}")
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
        # A box can carry more than one USB-serial adapter (the gun AND
        # its LED display board). The old design ROTATED to the next
        # adapter after 60s of silence — but a Stalker SLEEPS between
        # pitches, sending nothing while it dozes, so the service spent
        # half of every game parked on the display board and the pitch
        # landed while we listened to a screen ("a velo popped up, then
        # nothing" — field report). Listen to EVERY adapter at once:
        # whichever port produces gun-shaped frames is the gun (sticky),
        # and its raw bytes are forwarded to the rest.
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
            disp_pin = (cfg.get('radar') or {}).get('display_port') or None
            # Same rule for the board: a pinned port that has gone away
            # must not send every frame into a path that cannot open.
            if disp_pin and not os.path.exists(disp_pin):
                log.warning('pinned display port %s is not there — falling '
                            'back to the other adapter', disp_pin)
                disp_pin = None
            # The GUN's rate (radar.baud) and the BOARD's rate
            # (radar.display_baud) are independent: a Stalker set to LO
            # talks 9600 while its display board may want something
            # else entirely, and a mismatch on either side is silence or
            # character salad, never a useful error.
            gun_baud = int((cfg.get('radar') or {}).get('baud') or BAUD)
            self.gun_baud = gun_baud
            self.cfg_pinned = bool((cfg.get('radar') or {}).get('port'))
            self.cfg_disp_pinned = bool(disp_pin)
            disp_baud = int((cfg.get('radar') or {}).get('display_baud')
                            or gun_baud)
            disp_fmt = ((cfg.get('radar') or {}).get('display_format')
                        or 'speed')
            handles, bufs = {}, {}
            claims = {'lines': 0, 'ok': 0}
            try:
                for p in ports:
                    try:
                        handles[p] = serial.Serial(p, gun_baud, timeout=0)
                        bufs[p] = b''
                    except Exception as e:
                        log.warning(f'radar port {p} failed to open ({e})')
                if not handles:
                    self.connected = False
                    time.sleep(5)
                    continue
                # one adapter = it IS the gun; a pinned radar.port wins;
                # otherwise the first port that parses claims the title
                gun = ((cfg.get('radar') or {}).get('port') or None)
                if gun not in handles:
                    gun = list(handles)[0] if len(handles) == 1 else None
                self.port = gun or sorted(handles)[0]
                log.info(f'radar listening on {len(handles)} adapter(s) '
                         f'{sorted(handles)} @ {gun_baud}'
                         + (f' — gun on {gun}' if gun else
                            ' — waiting for the gun to speak first'))
                self.connected = True
                rescan_at = time.monotonic() + 10
                while self.running:
                    got_any = False
                    for p, ser in list(handles.items()):
                        n = ser.in_waiting
                        if not n:
                            continue
                        data = ser.read(n)
                        if not data:
                            continue
                        got_any = True
                        bufs[p] += data
                        *lines, bufs[p] = re.split(rb'[\r\n]+', bufs[p])
                        # A huge single drain is STALE HISTORY — a
                        # buffer that backed up while the process was
                        # blocked or restarting (2,000 frames landed in
                        # 0.2s on the field). Parsing it fabricates
                        # compressed phantom bursts and replaying it
                        # strobes the LED board with minutes-old
                        # numbers; keep the newest few, let history go.
                        if len(lines) > 25:
                            log.info(f'{len(lines)} buffered frames on '
                                     f'{p} — stale backlog, keeping the '
                                     'newest 5')
                            lines = lines[-5:]
                        for raw in lines:
                            if not raw.strip():
                                continue
                            if gun is None or p == gun:
                                before = self.frames_parsed
                                self.handle_line(
                                    raw.decode('ascii', 'replace'))
                                if gun is None \
                                        and self.frames_parsed > before:
                                    gun = p
                                    self.port = p
                                    claims = {'lines': 0, 'ok': 0}
                                    log.info(f'gun identified on {p}')
                                elif gun == p:
                                    # A WRONG claim is self-correcting: a
                                    # display board chatters status back
                                    # up its own cable, and one lucky
                                    # parse could crown it — after which
                                    # the real gun was never read and its
                                    # speeds were forwarded to itself,
                                    # leaving the board dark (field
                                    # report: 2000 unparsed of 2485).
                                    # Sustained garbage releases the
                                    # title so the other adapter can take
                                    # it. Pinning radar.port skips this.
                                    claims['lines'] += 1
                                    if self.frames_parsed > before:
                                        claims['ok'] += 1
                                    if claims['lines'] >= 300 and \
                                            claims['ok'] * 5 < claims['lines'] \
                                            and len(handles) > 1:
                                        log.warning(
                                            f'{p} claimed the gun but only '
                                            f'{claims["ok"]}/{claims["lines"]} '
                                            'lines parse — releasing it; '
                                            'the adapters are probably '
                                            'swapped (pin radar.port and '
                                            'radar.display_port to settle '
                                            'this permanently)')
                                        gun = None
                                        claims = {'lines': 0, 'ok': 0}
                            if gun == p:
                                others = [q for q in handles if q != p]
                                tgt = disp_pin or (others[0] if others
                                                   else None)
                                # 'speed' (default) drives the board in
                                # the display protocol it understands;
                                # radar.display_format='raw' restores
                                # the verbatim passthrough
                                if disp_fmt == 'raw':
                                    self.forward_display(raw + b'\r', tgt, disp_baud)
                                else:
                                    out = self.display_line(self.last_frame)
                                    if out:
                                        self.forward_display(out, tgt, disp_baud)
                    if not got_any:
                        ev = self.engine.flush()
                        self.push(event=ev)
                        time.sleep(0.05)
                    if time.monotonic() > rescan_at:
                        if sorted(find_ports(cfg)) != sorted(handles):
                            log.info('serial adapters changed — reopening')
                            break
                        rescan_at = time.monotonic() + 10
            except Exception as e:
                self.connected = False
                log.warning(f'radar serial dropped ({e}) — rescanning')
                time.sleep(3)
            finally:
                for ser in handles.values():
                    try:
                        ser.close()
                    except Exception:
                        pass
                self._close_display()

    def start_thread(self):
        t = threading.Thread(target=self.loop, daemon=True)
        t.start()
        return t
