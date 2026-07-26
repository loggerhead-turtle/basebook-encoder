"""Thin, monkeypatchable wrappers around every hardware / OS touchpoint.

Everything that shells out (nmcli, hostapd, systemctl, vcgencmd, journalctl)
goes through these functions so the rest of the package imports and unit-tests
cleanly on a laptop — tests just monkeypatch `run` (or the individual helper)
and no subprocess is ever spawned.
"""

import os
import shutil
import subprocess


def fake_mode():
    """SCOREBUG_FAKE=1 — run everything render-to-PNG, no hardware calls."""
    return os.environ.get('SCOREBUG_FAKE') == '1'


def run(cmd, **kw):
    kw.setdefault('capture_output', True)
    kw.setdefault('text', True)
    return subprocess.run(cmd, **kw)


def spawn(cmd):
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def have(name):
    return shutil.which(name) is not None


def systemctl(*args):
    return run(['systemctl', *args])


def reboot():
    run(['reboot'])


def have_networkmanager():
    """NetworkManager is the default stack on Raspberry Pi OS Bookworm."""
    if not have('nmcli'):
        return False
    r = run(['systemctl', 'is-active', '--quiet', 'NetworkManager'])
    return r.returncode == 0


def speedify_active():
    """True when Speedify (channel-bonding VPN some streamers run to bond
    cellular + local links) is managing this box's connectivity — its
    service is running, or its virtual bonding adapter exists. A box under
    Speedify must NEVER have its network stack touched by us: no hotspot,
    no wpa_supplicant kills, no nmcli writes."""
    if fake_mode():
        return False
    r = run(['systemctl', 'is-active', '--quiet', 'speedify'])
    if r.returncode == 0:
        return True
    try:
        return any(name.startswith(('connectify', 'speedify'))
                   for name in os.listdir('/sys/class/net'))
    except OSError:
        return False


def hostname():
    try:
        return open('/etc/hostname').read().strip()
    except OSError:
        return 'playcall-encoder'


def lan_ip():
    r = run(['hostname', '-I'])
    parts = (r.stdout or '').split()
    return parts[0] if parts else ''


def serial_suffix():
    """Last 4 hex chars of the Pi serial (for the setup-AP SSID). Falls back
    to a stable-ish value on non-Pi hardware so dev laptops still work."""
    try:
        for line in open('/proc/cpuinfo'):
            if line.lower().startswith('serial'):
                s = line.split(':')[-1].strip()
                if s:
                    return s[-4:].upper()
    except OSError:
        pass
    import uuid
    return f'{uuid.getnode() & 0xffff:04X}'


def cpu_percent():
    """Cheap CPU estimate from 1-min loadavg — heartbeat telemetry, not a
    profiler; avoids a 1s sampling sleep in the poll loop."""
    try:
        load = float(open('/proc/loadavg').read().split()[0])
        ncpu = os.cpu_count() or 1
        return round(min(100.0, load / ncpu * 100.0), 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def cpu_temp():
    """Degrees C from vcgencmd (Pi) or sysfs, else None."""
    if have('vcgencmd'):
        r = run(['vcgencmd', 'measure_temp'])
        try:
            return float(r.stdout.split('=')[1].split("'")[0])
        except (IndexError, ValueError, AttributeError):
            pass
    try:
        raw = open('/sys/class/thermal/thermal_zone0/temp').read().strip()
        return round(int(raw) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def journal_tail(lines=20, units=('playcall-encoder',
                                  'playcall-encoder-youtube',
                                  'playcall-encoder-mediamtx')):
    if fake_mode() or not have('journalctl'):
        return []
    cmd = ['journalctl', '--no-pager', '-n', str(lines), '-o', 'short-iso']
    for u in units:
        cmd += ['-u', u]
    r = run(cmd)
    return (r.stdout or '').splitlines()[-lines:]
