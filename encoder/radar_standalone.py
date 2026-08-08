#!/usr/bin/env python3
"""Radar-box entrypoint — `python3 -m encoder.radar_standalone`
(playcall-encoder-radar.service).

The whole appliance, sized for a Pi Zero 2 W: serial capture
(encoder/radar.py, unchanged semantics), the LAN feed
(encoder/radar_lan.py, advertised over mDNS by the Avahi service file
the installer drops in /etc/avahi/services), and the cloud shadow —
POST /api/encoder/radar exactly as the full encoder does, whenever the
config carries a cloud pairing. No MediaMTX, no YouTube push, no
clipper, no provisioning portal, no settings page: a radar box that is
not paired is still a complete product, serving velo to the app over
the LAN with zero internet.

The CloudLink here is deliberately NOT thread-started: its assignment
loop repoints scorebug feeds and restarts a YouTube unit this box does
not have, and its heartbeat probes a MediaMTX that is not running. The
radar service only borrows the link's auth + HTTP plumbing, so the one
kind of cloud traffic a radar box originates is the radar POST itself
(and radar.py already buffers events across outages).

Inherited, non-negotiable: NOTHING in this process can write to the
scorebook — velo is decoration.
"""

import logging
import signal
import sys
import time

from . import __version__, cloud_link, config, radar, radar_lan

log = logging.getLogger('radar_box')


def build_service(cfg_load=config.load, link=None, lan_port=None):
    """Assemble capture + LAN + cloud shadow, unstarted — the seam the
    tests use. `link` defaults to a real CloudLink reading the same
    config, so pairing the box later is picked up live, no restart."""
    link = link or cloud_link.CloudLink(cfg_load=cfg_load)
    svc = radar.RadarService(link, cfg_load=cfg_load)
    svc.lan = radar_lan.LanServer(port=lan_port,
                                  gun_connected=lambda: svc.connected)
    return svc


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(name)-12s %(levelname)-7s  %(message)s')
    log.info(f'PlayCall radar box v{__version__} — capture + LAN feed '
             '+ cloud shadow')
    svc = build_service()
    # A radar box that cannot bind its one port is broken — crash loud
    # and let systemd's Restart= retry, instead of capturing into a void.
    svc.lan.start()
    svc.start_thread()

    def stop(*a):
        svc.running = False
        svc.lan.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
