"""/etc/playcall-encoder/config.json — the single source of truth.

Atomic read/write (temp file + os.replace on the same filesystem) so a power
cut mid-save can never leave a half-written config: the device either has the
old file or the new one. Directory overridable via PLAYCALL_ENCODER_DIR so
tests and laptop dev never touch /etc.
"""

import copy
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from . import __version__

DEFAULT_YOUTUBE_URL = 'rtmps://a.rtmps.youtube.com/live2'

DEFAULTS = {
    # [{"ssid": str, "psk": str, "priority": int, "label": "home"|"gameday"}]
    'networks': [],
    # True  → we own the box's Wi-Fi (portal setup, recovery hotspot).
    # False → the box was already networked when we arrived (Ethernet,
    #         Speedify cellular bonding, USB tether…) and we ADOPT that
    #         network untouched: no hotspot, no nmcli/wpa writes, ever.
    'network_managed': True,
    # Secret path segment of the local RTMP ingest URL; generated once.
    'local_ingest_key': '',
    'youtube': {'url': DEFAULT_YOUTUBE_URL, 'key': ''},
    'cloud': {'base_url': '', 'api_key': '', 'feed_url': ''},
    'device': {'pin': '', 'name': 'PlayCall Encoder'},
    # An activation code typed into the setup portal on a phone that was
    # joined to this box's own hotspot — i.e. with no internet to spend it
    # on yet. It waits here until the box joins a real network, then
    # encoder.activation spends it and blanks this. Also the landing spot
    # for a code taken off the boot partition of a prebuilt image.
    'pending_code': '',
    # YouTube push bitrate, kbps. 0 = push the camera's own stream
    # untouched (the only mode a Pi 5 has — it owns no video encoder).
    # On a box with QuickSync (N100/N150), a non-zero value transcodes
    # the push down while the LOCAL recording keeps the camera's full
    # quality: send the box 1080p at 10 Mbps for crisp clips, hand
    # YouTube 3 Mbps the field uplink can actually carry.
    'push_bitrate_kbps': 0,
    # 'h264' (default — every YouTube ingest and every box accepts it)
    # or 'hevc': ~35% better quality per bit via enhanced RTMP. Only
    # honored when transcoding (push_bitrate_kbps > 0) on a box whose
    # hardware AND ffmpeg both prove HEVC capable; everywhere else it
    # degrades to H.264 with a log line, never an error loop.
    'push_codec': 'h264',
    # True = recording to the SD card instead of the NVMe is a CHOICE
    # (the drive is out for repair, or this box never had one). The
    # storage check then reports the fallback instead of raising a
    # failure: a red banner that is always on is one nobody reads, and
    # this box's whole point is that a real storage failure gets
    # noticed. Turn it back off when the drive returns.
    'record_fallback_ok': False,
    # 🎦 Multi-View: post this box's camera feed to the site's stream
    # server as one angle, so it plays on the game's Multi-View page beside
    # the phones. The site hands down the signed ingest ticket on the
    # assignment poll; with no ticket (no stream server, or no live game)
    # this leg sits idle, which is why it can default on. 'angle' is the
    # name viewers see — one per box, so two boxes at one game do not
    # collide.
    'live_push': {'enabled': True, 'angle': 'main'},
    'version_check': {'url': '', 'enabled': True},
}


def config_dir():
    return Path(os.environ.get('PLAYCALL_ENCODER_DIR', '/etc/playcall-encoder'))


def config_path():
    return config_dir() / 'config.json'


def state_dir():
    """Runtime (non-persistent) state — push stats etc."""
    return Path(os.environ.get('PLAYCALL_ENCODER_STATE',
                               '/run/playcall-encoder'))


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    try:
        raw = json.loads(config_path().read_text())
    except (OSError, ValueError):
        raw = {}
    return _merge(DEFAULTS, raw)


def save(cfg):
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Same-directory temp file so os.replace is an atomic rename.
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix='.config-', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
        # Holds WiFi passwords + stream keys: root rw, playcall service
        # group read (the youtube push unit runs unprivileged), no world.
        os.chmod(tmp, 0o640)
        try:
            import shutil
            shutil.chown(tmp, group='playcall')
        except (LookupError, PermissionError, OSError):
            pass
        os.replace(tmp, config_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cfg


def is_configured(cfg=None):
    cfg = cfg or load()
    if not cfg.get('local_ingest_key'):
        return False
    # A network-unmanaged box (Speedify / Ethernet / tether adoption) is
    # configured without any stored Wi-Fi networks — the OS owns those.
    if cfg.get('network_managed', True) is False:
        return True
    return bool(cfg.get('networks'))


def ensure_ingest_key(cfg):
    if not cfg.get('local_ingest_key'):
        cfg['local_ingest_key'] = secrets.token_hex(4)   # 8 hex chars
    return cfg['local_ingest_key']


def ensure_pin(cfg):
    if not cfg['device'].get('pin'):
        cfg['device']['pin'] = f'{secrets.randbelow(1000000):06d}'
    return cfg['device']['pin']


def rotate_ingest_key(cfg):
    cfg['local_ingest_key'] = secrets.token_hex(4)
    return cfg['local_ingest_key']


def version_check_url(cfg):
    """Explicit URL wins; else derived from the paired cloud."""
    url = (cfg.get('version_check') or {}).get('url')
    if url:
        return url
    base = (cfg.get('cloud') or {}).get('base_url', '').rstrip('/')
    return f'{base}/api/encoder/version' if base else ''


def redacted(cfg=None):
    """Config summary safe to paste into a support thread / AI chat —
    secrets replaced, structure kept."""
    cfg = copy.deepcopy(cfg or load())
    for n in cfg.get('networks', []):
        if n.get('psk'):
            n['psk'] = '********'
    if cfg.get('local_ingest_key'):
        cfg['local_ingest_key'] = cfg['local_ingest_key'][:2] + '******'
    if cfg['youtube'].get('key'):
        cfg['youtube']['key'] = '********'
    if cfg['cloud'].get('api_key'):
        cfg['cloud']['api_key'] = '********'
    if cfg['device'].get('pin'):
        cfg['device']['pin'] = '****'
    if cfg.get('pending_code'):
        # Unspent, and it pairs a box to a team — never in a log bundle.
        cfg['pending_code'] = '********'
    cfg['version'] = __version__
    return cfg


# ── log redaction ─────────────────────────────────────────────────────────────

# Any rtmp(s) URL whose path ends in /live/<key> or /live2/<key> — the shape
# ffmpeg prints on push failures ("rtmps://a.rtmps.youtube.com/live2/KEY:
# Operation timed out") and the local ingest URL.
_RTMP_KEY_RE = re.compile(r"rtmps?://[^\s'\"]*/live2?/[^\s'\"]+",
                          re.IGNORECASE)
REDACTED = 'rtmp…/•••'


def redact_text(text, cfg=None):
    """Scrub stream keys / RTMP push URLs out of arbitrary log text before
    it leaves the box (support bundle, cloud heartbeat log_tail)."""
    if not text:
        return text
    cfg = cfg or load()
    text = _RTMP_KEY_RE.sub(REDACTED, text)
    for secret in ((cfg.get('youtube') or {}).get('key'),
                   cfg.get('local_ingest_key')):
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact_lines(lines, cfg=None):
    cfg = cfg or load()
    return [redact_text(line, cfg) for line in lines]


# ── mediamtx config templating ────────────────────────────────────────────────

def package_dir():
    return Path(__file__).resolve().parent.parent


def write_mediamtx_config(cfg, template=None, dest=None):
    """Bake the local ingest key into mediamtx.yml (same approach as the
    original pi/install_relay.sh sed). Called by the installer, by
    provisioning when the key is first generated, and by the settings page
    when the key is rotated."""
    template = Path(template or package_dir() / 'mediamtx.yml')
    dest = Path(dest or config_dir() / 'mediamtx.yml')
    key = cfg.get('local_ingest_key') or 'setup'
    try:
        hours = int(cfg.get('record_hours') or 12)
    except (TypeError, ValueError):
        hours = 12
    hours = max(1, min(168, hours))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(template.read_text()
                    .replace('__INGEST_KEY__', key)
                    .replace('__RECORD_HOURS__', str(hours)))
    return dest


# ── zero-touch preconfig (boot partition) ─────────────────────────────────────

# Bookworm mounts the FAT boot partition at /boot/firmware; older images /boot.
PRECONFIG_FILES = [
    Path('/boot/firmware/playcall-encoder.json'),
    Path('/boot/playcall-encoder.json'),
]


def apply_preconfig(paths=None):
    """Apply (then delete) a playcall-encoder.json dropped on the boot
    partition when the SD card was flashed — lets a shipped/pre-imaged Pi
    come up fully configured with no captive-portal step. Never raises: a
    malformed file is ignored and the normal provisioning portal runs.

    Returns True if a preconfig was applied."""
    for p in (paths or PRECONFIG_FILES):
        p = Path(p)
        try:
            if not p.exists():
                continue
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        cfg = load()
        nets = []
        for i, n in enumerate(data.get('networks') or []):
            if isinstance(n, dict) and n.get('ssid'):
                nets.append({'ssid': str(n['ssid']),
                             'psk': str(n.get('psk') or n.get('password') or ''),
                             'priority': int(n.get('priority', 100 - i * 10)),
                             'label': str(n.get('label', 'home'))})
        if nets:
            cfg['networks'] = nets
        if data.get('youtube_key'):
            from .provisioning import normalize_youtube
            url, key = normalize_youtube(str(data['youtube_key']))
            cfg['youtube'] = {'url': url, 'key': key}
        cloud = data.get('cloud') or {}
        if isinstance(cloud, dict):
            if cloud.get('base_url'):
                cfg['cloud']['base_url'] = str(cloud['base_url']).rstrip('/')
            if cloud.get('api_key'):
                cfg['cloud']['api_key'] = str(cloud['api_key'])
        if data.get('device_name'):
            cfg['device']['name'] = str(data['device_name'])
        if 'network_managed' in data:
            cfg['network_managed'] = bool(data['network_managed'])
        ensure_ingest_key(cfg)
        ensure_pin(cfg)
        save(cfg)
        try:
            # The file holds WiFi passwords in plaintext on a FAT partition —
            # consume it so it isn't left readable on the card.
            p.unlink()
        except OSError:
            pass
        return True
    return False
