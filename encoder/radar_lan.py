#!/usr/bin/env python3
"""Radar LAN feed — newline-delimited JSON over plain TCP.

The app should never need the internet to show velo. This server gives
the radar capture (encoder/radar.py) a LAN mouth: it listens on
0.0.0.0:8791 (RADAR_LAN_PORT overrides), is advertised over mDNS as
`_basebook-radar._tcp` by an Avahi service file (the Pi ships Avahi; no
Python dependency), and fans whatever the radar module pushes at it out
to every connected client, one JSON object per line, UTF-8:

    hello   first line on every new connection — service version +
            gun-connected state
    live    a reading: velo (mph) + rpm (null until the gun computes it)
    burst   one tracked object, classified pitch/throw/ghost on this box
    alive   idle heartbeat, so a client can tell "gun asleep" (a Stalker
            dozes between pitches) from "box gone"

Wire format is protocol/radar-lan.schema.json in the basebook-stream
repo — the box classifies, the apps only display. JSON-lines over a
bare socket keeps this standard-library-only (the repo's rule — see
clipper.py) and lets the apps read it with a plain socket too.

Memory is bounded by design, and the feed NEVER blocks on a client:
each client gets a small deque of pending lines (drop-OLDEST — a velo
reading is only news for a few seconds, so the freshest line always
wins) and its own writer thread. A client whose TCP window stays wedged
past SEND_TIMEOUT is dropped; the gun-side pipeline never notices
either way.

Nothing here can write to the scorebook — velo is decoration.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque

from . import __version__

log = logging.getLogger('radar_lan')

DEFAULT_PORT = 8791           # must match preconfig/avahi-basebook-radar.service
QUEUE_MAX = 64                # per-client backlog; past this the OLDEST drops
SEND_TIMEOUT = 10             # a send wedged this long = a gone client


def _now_ms():
    """Sender epoch ms — wall clock, because the cloud (and the clip
    windows) speak wall clock and atMs must line up with them."""
    return int(time.time() * 1000)


class _Client:
    """One connection: a bounded line queue + the writer that drains it.
    The deque's maxlen IS the drop-oldest policy — an append past
    QUEUE_MAX silently evicts the stalest line, so a slow reader sees
    gaps, never staleness, and enqueue never blocks the radar side."""

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.q = deque(maxlen=QUEUE_MAX)
        self.cond = threading.Condition()
        self.closed = False

    def enqueue(self, data):
        with self.cond:
            if self.closed:
                return
            self.q.append(data)
            self.cond.notify()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()
        try:
            self.sock.close()
        except OSError:
            pass


class LanServer:
    """Accepts N clients, speaks `hello` to each on connect, then relays
    whatever send_live / send_burst / send_alive are fed. Thread-based
    like the rest of the codebase; every thread is a daemon, so the
    server never holds a shutdown hostage."""

    def __init__(self, port=None, version=None, gun_connected=None):
        self.port = (int(os.environ.get('RADAR_LAN_PORT') or DEFAULT_PORT)
                     if port is None else int(port))
        self.version = version or __version__
        # Sampled at hello/alive time so the message says what is true
        # NOW, not what was true when the server was built.
        self.gun_connected = gun_connected or (lambda: False)
        self.running = True
        self._sock = None
        self._clients = []
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        """Bind + listen + spawn the accept thread. Returns self so
        `svc.lan = LanServer(...).start()` reads as one line. Raises
        OSError on a taken port — the caller decides whether that is
        fatal (radar box) or a warning (full encoder)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', self.port))
        s.listen(8)
        self._sock = s
        self.port = s.getsockname()[1]      # resolves port=0 (tests)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        log.info(f'radar LAN feed listening on :{self.port} '
                 '(_basebook-radar._tcp)')
        return self

    def stop(self):
        self.running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()

    @property
    def client_count(self):
        with self._lock:
            return len(self._clients)

    # ── accept / write / drop ────────────────────────────────────────────────
    def _accept_loop(self):
        while self.running:
            try:
                sock, addr = self._sock.accept()
            except OSError:
                return                      # listener closed = shutdown
            try:
                # Readings race staleness — never let Nagle sit on one.
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.settimeout(SEND_TIMEOUT)
            c = _Client(sock, addr)
            # hello is queued BEFORE the client joins the broadcast list,
            # so it is provably the first line every client ever reads.
            c.enqueue(self._encode({
                'type': 'hello', 'atMs': _now_ms(),
                'version': self.version,
                'gunConnected': bool(self.gun_connected()),
            }))
            with self._lock:
                self._clients.append(c)
            threading.Thread(target=self._writer, args=(c,),
                             daemon=True).start()
            log.info(f'radar LAN client connected: {addr[0]}:{addr[1]} '
                     f'({self.client_count} total)')

    def _writer(self, c):
        try:
            while self.running:
                with c.cond:
                    while not c.q and not c.closed:
                        c.cond.wait(1.0)
                    if c.closed:
                        return
                    data = c.q.popleft()
                # Outside the lock: a wedged send stalls THIS client's
                # thread only; the queue keeps absorbing (drop-oldest)
                # and every other client keeps flowing.
                c.sock.sendall(data)
        except OSError:
            pass                            # gone or too slow — same fate
        finally:
            self._drop(c)

    def _drop(self, c):
        with self._lock:
            try:
                self._clients.remove(c)
            except ValueError:
                return                      # already dropped
        c.close()
        log.info(f'radar LAN client dropped: {c.addr[0]}:{c.addr[1]} '
                 f'({self.client_count} left)')

    # ── the message set (protocol/radar-lan.schema.json) ─────────────────────
    @staticmethod
    def _encode(msg):
        return (json.dumps(msg, separators=(',', ':'))
                .encode('utf-8') + b'\n')

    def broadcast(self, msg):
        data = self._encode(msg)
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.enqueue(data)

    def send_live(self, velo, rpm=None, at_ms=None):
        """One reading. rpm rides as an integer once the gun computes it
        (a few frames after peak lock) and null until then — the schema
        says integer, the gun's tenths are display noise at 1500+ rpm."""
        self.broadcast({
            'type': 'live', 'atMs': at_ms or _now_ms(),
            'velo': velo,
            'rpm': int(round(rpm)) if rpm is not None else None,
        })

    def send_burst(self, ev, at_ms=None):
        """One closed burst from the engine (radar.py event dict). The
        classification made here is final — the apps never re-classify;
        display code drops ghosts."""
        self.broadcast({
            'type': 'burst', 'atMs': at_ms or _now_ms(),
            'kind': ev['kind'], 'peak': ev['peak'],
            'frames': ev['frames'], 'durS': ev['dur'],
        })

    def send_alive(self, gun_connected=None, at_ms=None):
        self.broadcast({
            'type': 'alive', 'atMs': at_ms or _now_ms(),
            'gunConnected': bool(self.gun_connected()
                                 if gun_connected is None else gun_connected),
        })
