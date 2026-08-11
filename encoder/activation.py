"""Trade a basebook.org activation code for this box's cloud key.

The code is the whole of onboarding now. A coach presses "Add an encoder"
in Score Bug Studio, gets four letters and four digits, and that code
reaches the box by whichever route suits them:

  a terminal        — the copy-paste one-liner runs scripts/activate.py
  the setup portal  — typed on a phone joined to the box's own hotspot,
                      stored, and spent as soon as the box has internet
  the boot partition— dropped into playcall-code.txt from any computer
                      with a card reader; the prebuilt-image path, where
                      nobody opens a terminal at all

All three land here so there is one set of rules about what a code is,
when it is spent, and what happens when the site says no.
"""

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request

from . import config

log = logging.getLogger('activation')

DEFAULT_CLOUD = 'https://basebook.org'

# Where a flashed image leaves the code. Raspberry Pi OS mounts the FAT
# boot partition at /boot/firmware on Bookworm and later, /boot before
# that — check both so one image works on either.
CODE_FILES = ('/boot/firmware/playcall-code.txt', '/boot/playcall-code.txt')


class Refused(RuntimeError):
    """The site answered and said no. Retrying changes nothing."""


class Unreachable(RuntimeError):
    """We never got an answer. On a box flashed at home and first powered
    on at the field, this is the ordinary first-boot state, not a fault —
    worth waiting out."""


def normalize(raw):
    """HAWK-4823 out of 'hawk 4823', 'HAWK4823', 'hawk-4823\\n'.

    Coaches read these off a phone screen and type them into a terminal
    or a phone keyboard; a code rejected over a missing dash is a support
    call. Returns '' for anything that isn't 8 alphanumerics.
    """
    s = ''.join(c for c in (raw or '') if c.isalnum()).upper()
    return f'{s[:4]}-{s[4:]}' if len(s) == 8 else ''


def code_from_disk():
    """(code, path) from the boot partition, or ('', '')."""
    for p in CODE_FILES:
        try:
            with open(p) as f:
                code = normalize(f.read())
        except OSError:
            continue
        if code:
            return code, p
    return '', ''


def clear_disk_code():
    """A spent code is a bearer credential sitting in plaintext on a FAT
    partition that any computer with a card reader can read."""
    for p in CODE_FILES:
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


REASONS = {
    'invalid_code': 'The site does not recognise {code}. Check for a typo '
                    '— four letters then four digits, e.g. HAWK-4823.',
    'code_already_used': '{code} has already been used by another box. '
                         'Generate a fresh one on the website.',
    # Deliberately no duration here: the site owns that number, and a
    # box repeating a stale one is how documentation starts lying.
    'code_expired': '{code} has expired. Generate a fresh one on the '
                    'website.',
    'missing_code': 'No code was sent.',
}


def cloud_base():
    return (os.environ.get('PLAYCALL_CLOUD') or DEFAULT_CLOUD).rstrip('/')


def redeem(code, base=None, device_name=None, timeout=30):
    """POST the code; return the site's JSON. Raises Refused or
    Unreachable, each carrying a sentence a coach can act on."""
    base = (base or cloud_base()).rstrip('/')
    payload = {'code': code,
               'device_name': device_name or socket.gethostname() or 'Encoder'}
    req = urllib.request.Request(
        f'{base}/api/pi/activate', data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            why = (json.load(e) or {}).get('error', '')
        except Exception:
            why = ''
        raise Refused(
            REASONS.get(why, 'The site refused the code '
                             f'(HTTP {e.code} {why}).').format(code=code))
    except Exception as e:
        raise Unreachable(
            f'Could not reach {base}: {e}\nCheck this Pi\'s internet, then '
            'try again with the same code.')


def redeem_with_retry(code, base=None, tries=1, delay=30, sleep=time.sleep,
                      **kw):
    """Retry only what retrying can fix. A wrong or spent code fails on
    the first attempt and stays failed; no internet yet is worth waiting
    out, which is exactly the first-boot case for an imaged card."""
    last = None
    for n in range(max(1, tries)):
        try:
            return redeem(code, base, **kw)
        except Unreachable as e:
            last = e
            if n + 1 < max(1, tries):
                sleep(delay)
    raise last


def already_paired(cfg=None):
    cur = ((cfg or config.load()).get('cloud') or {})
    return bool(cur.get('base_url') and cur.get('api_key'))


def apply(out, cfg=None, base=None):
    """Write the redeemed key into config. Returns the saved config."""
    cfg = cfg if cfg is not None else config.load()
    cfg['cloud'] = {'base_url': out.get('cloud_url') or base or cloud_base(),
                    'api_key': out.get('api_key') or '', 'feed_url': ''}
    cfg['pending_code'] = ''
    config.ensure_pin(cfg)
    config.save(cfg)
    return cfg


def activate(code, cfg=None, tries=1, **kw):
    """Redeem `code` and persist it. Returns the site's JSON."""
    out = redeem_with_retry(code, tries=tries, **kw)
    apply(out, cfg)
    log.info('paired to %s', out.get('team_name') or 'a team')
    return out
