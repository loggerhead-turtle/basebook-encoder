#!/usr/bin/env python3
"""Encoder entrypoint — `python3 -m encoder` (playcall-encoder.service).

Boot decision:
  1. Zero-touch preconfig on the boot partition → apply + delete it.
  2. Unconfigured → provisioning hotspot + captive portal (blocks; the
     portal exits the process when done and systemd restarts us configured).
  3. Configured → start the scorebug sender + local web app + cloud link
     + offline watchdog threads and run forever.

SCOREBUG_FAKE=1 runs the whole stack on a laptop: no AP, no systemctl, the
scorebug renders to PNG files instead of NDI.
"""

import logging
import signal
import sys
import threading
import time

from . import __version__, cloud_link, config, provisioning, scorebug
from . import system, web

log = logging.getLogger('encoder')


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(name)-12s %(levelname)-7s  %(message)s')
    log.info(f'PlayCall Encoder v{__version__}')
    fake = system.fake_mode()

    if config.apply_preconfig():
        log.info('Applied zero-touch preconfig from the boot partition')

    cfg = config.load()
    if not config.is_configured(cfg):
        if fake:
            # Laptop dev: fabricate a config instead of raising an AP.
            config.ensure_ingest_key(cfg)
            config.ensure_pin(cfg)
            cfg['networks'] = [{'ssid': 'dev', 'psk': '', 'priority': 100,
                                'label': 'home'}]
            config.save(cfg)
            log.info('SCOREBUG_FAKE: wrote a dev config')
        elif (system.speedify_active()
                or provisioning.has_connectivity()):
            # Not-fresh Pi: the box is already online (Speedify bond,
            # Ethernet, tether). Raising the hotspot here would kill
            # wpa_supplicant and flush wlan0 — clobbering the existing
            # setup — so adopt the network as-is instead.
            log.info('Already online at first run — adopting the existing '
                     'network, skipping the setup hotspot')
            cfg = provisioning.headless_setup()
        else:
            log.info('Unconfigured — starting provisioning portal')
            provisioning.run_portal()      # blocks; exits process when done
            return

    # Make sure mediamtx has a config with the current ingest key (covers
    # the preconfig path where install.sh ran before a key existed).
    try:
        config.write_mediamtx_config(cfg)
    except OSError as e:
        log.warning(f'mediamtx config not written: {e}')

    sender = scorebug.Sender(feed=cfg['cloud'].get('feed_url') or None,
                             layout='bar', fake=fake)
    sender.bandwidth = cfg.get('bandwidth', 0)
    sender.start_threads()

    link = cloud_link.CloudLink(on_feed_change=sender.set_feed)
    link.start_threads()

    threading.Thread(target=web.serve,
                     kwargs={'cloud': link, 'sender': sender},
                     daemon=True).start()

    if not fake and cfg.get('network_managed', True) is not False:
        # Online = working connectivity on ANY interface (default route /
        # associated Wi-Fi), a recent successful cloud round-trip, OR an
        # active Speedify bond (its tunnel may briefly drop the default
        # route while cellular reconnects — never tear that down). On a
        # network-unmanaged box the watchdog doesn't run at all: the
        # recovery hotspot must never touch a network stack we don't own.
        def _online():
            return (provisioning.has_connectivity() or link.recently_ok()
                    or system.speedify_active())
        threading.Thread(target=provisioning.network_watchdog,
                         kwargs={'is_online': _online},
                         daemon=True).start()

    def stop(*a):
        sender.running = False
        link.running = False
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
