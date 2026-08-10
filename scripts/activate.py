#!/usr/bin/env python3
"""CLI over encoder.activation — pair this box with a basebook.org team.

  sudo python3 activate.py HAWK-4823      # the website's one-liner uses this
  sudo python3 activate.py --retry=10     # first boot of a prebuilt image:
                                          # take the code off the boot
                                          # partition, wait out no-network

Exit codes matter to the callers: 0 paired (or already paired and left
alone), 2 nothing to do (no code anywhere), 1 a real failure with a
human-readable reason on stderr.
"""

import os
import sys

sys.path.insert(0, '/opt/playcall-encoder')

from encoder import activation, config      # noqa: E402


def main(argv):
    cfg = config.load()
    forced = bool(os.environ.get('PLAYCALL_FORCE_PAIR'))

    # Re-running the installer to upgrade a working box must not try to
    # spend a code again — that fails loudly and reads as a broken
    # upgrade. Pairing is deliberate; only PLAYCALL_FORCE_PAIR overrides.
    if activation.already_paired(cfg) and not forced:
        print(f"Already paired to {cfg['cloud']['base_url']} — leaving it "
              "alone.")
        activation.clear_disk_code()   # spent or irrelevant; off the card
        return 0

    args = [a for a in argv[1:] if not a.startswith('--')]
    tries = 1
    for a in argv[1:]:
        if a.startswith('--retry='):
            tries = int(a.split('=', 1)[1] or 1)

    code = activation.normalize(
        args[0] if args else os.environ.get('PLAYCALL_CODE', ''))
    if not code:
        # The portal stores what a coach typed on their phone while the
        # box had no internet yet; the boot partition is the imaged-card
        # route. Either way the code is waiting for us here.
        code = activation.normalize(cfg.get('pending_code') or '')
    if not code:
        code, _ = activation.code_from_disk()
    if not code:
        return 2

    try:
        out = activation.activate(code, cfg, tries=tries)
    except activation.Refused:
        # A stored code the site has rejected is dead. Drop it, or every
        # boot from here to the end of time re-asks the same question and
        # gets the same no.
        if cfg.get('pending_code'):
            cfg['pending_code'] = ''
            config.save(cfg)
        activation.clear_disk_code()
        raise
    activation.clear_disk_code()
    print(f"Paired to {out.get('team_name') or 'your team'} ✓")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
