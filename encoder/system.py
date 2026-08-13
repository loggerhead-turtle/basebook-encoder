"""Thin, monkeypatchable wrappers around every hardware / OS touchpoint.

Everything that shells out (nmcli, hostapd, systemctl, vcgencmd, journalctl)
goes through these functions so the rest of the package imports and unit-tests
cleanly on a laptop — tests just monkeypatch `run` (or the individual helper)
and no subprocess is ever spawned.
"""

import os
import shutil
import subprocess
import tempfile


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


UPDATE_REPO = 'https://github.com/loggerhead-turtle/basebook-encoder'
INSTALL_DIR = '/opt/playcall-encoder'
# Restart order matters: the box's own service (which hosts this web UI)
# goes LAST, so the siblings come up on new code before we kill ourselves
# and systemd revives us on the new tree.
UPDATE_UNITS = ('playcall-encoder-mediamtx', 'playcall-encoder-youtube',
                'playcall-encoder-clipper', 'playcall-encoder')


def self_update(repo_url=None, install_dir=None):
    """One-button code update: shallow-clone the release repo and lay the
    same payload the installer lays (encoder/, VERSION, mediamtx.yml,
    scripts/, systemd units) over the install dir. Pure file copy — config
    in /etc/playcall-encoder is never touched, so this is exactly a re-run
    of the installer's copy step. Returns (ok, detail): detail is the new
    VERSION string on success, an error message on failure. The caller
    restarts UPDATE_UNITS afterwards."""
    repo_url = repo_url or os.environ.get('PLAYCALL_ENCODER_REPO',
                                          UPDATE_REPO)
    install_dir = install_dir or INSTALL_DIR
    tmp = tempfile.mkdtemp(prefix='playcall-update-')
    try:
        try:
            r = run(['git', 'clone', '--depth', '1', repo_url,
                     os.path.join(tmp, 'src')], timeout=180)
        except subprocess.TimeoutExpired:
            return False, 'download timed out — check this box’s internet'
        if r.returncode != 0:
            return False, ('download failed: '
                           + (r.stderr or 'git clone error').strip()[-200:])
        src = os.path.join(tmp, 'src')
        if not (os.path.isdir(os.path.join(src, 'encoder'))
                and os.path.isfile(os.path.join(src, 'VERSION'))):
            return False, 'download was missing the encoder payload'
        shutil.copytree(os.path.join(src, 'encoder'),
                        os.path.join(install_dir, 'encoder'),
                        dirs_exist_ok=True)
        for name in ('VERSION', 'mediamtx.yml'):
            shutil.copy2(os.path.join(src, name), install_dir)
        sdir = os.path.join(src, 'scripts')
        if os.path.isdir(sdir):
            os.makedirs(os.path.join(install_dir, 'scripts'), exist_ok=True)
            for name in os.listdir(sdir):
                if name.endswith(('.sh', '.py')):
                    dst = os.path.join(install_dir, 'scripts', name)
                    shutil.copy2(os.path.join(sdir, name), dst)
                    os.chmod(dst, 0o755)
        # Stale bytecode from removed/renamed modules must not shadow the
        # new tree on restart.
        for root, dirs, _files in os.walk(os.path.join(install_dir,
                                                       'encoder')):
            for d in list(dirs):
                if d == '__pycache__':
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    dirs.remove(d)
        # New/changed units ship with the code; refresh them like install.sh
        # does. Best-effort — a dev checkout without /etc write access still
        # gets the code update.
        sysd = os.path.join(src, 'systemd')
        if os.path.isdir(sysd) and not fake_mode():
            try:
                for f in os.listdir(sysd):
                    if f.startswith('playcall-encoder') and \
                            f.endswith('.service'):
                        shutil.copy2(os.path.join(sysd, f),
                                     '/etc/systemd/system/')
                systemctl('daemon-reload')
            except OSError:
                pass
        with open(os.path.join(src, 'VERSION')) as fh:
            return True, fh.read().strip()
    except OSError as e:
        return False, f'update failed: {e}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── power-cut survival ────────────────────────────────────────────────────
FSTAB = '/etc/fstab'
CMDLINE = '/boot/firmware/cmdline.txt'
DATA_MOUNT = '/var/lib/playcall-encoder'
# noatime: no metadata write per read; nofail: a dead drive must not hang
# boot; commit=1: the journal flushes every second, so a power cut costs
# ~1 s of video instead of the filesystem; the device-timeout keeps a
# missing NVMe from stalling systemd for its default 90 s.
FSTAB_OPTS = ('noatime', 'nofail', 'commit=1', 'x-systemd.device-timeout=10')


def harden_storage(fstab=FSTAB, cmdline=CMDLINE, mount=DATA_MOUNT):
    """Make a yanked power cord survivable.

    The field box runs off a USB power bank and gets unplugged rather
    than shut down (its own smart-log: 24 unsafe shutdowns in 30 power
    cycles). One of those cuts left the recordings filesystem in ext4's
    emergency shutdown and killed the clips pipeline until a hand-run
    fsck. Two idempotent config repairs, applied at every boot:

      * the recordings mount's fstab entry gains crash-safe options and
        fs_passno 2, so systemd fscks it BEFORE mounting it;
      * fsck.repair=yes on the kernel command line, so that boot-time
        fsck repairs what it finds instead of giving up to preen mode.

    Returns the list of files changed (for the boot log). Existing
    explicit choices are respected: a commit= already present keeps its
    value, an fsck.repair= already present keeps its answer. Never
    raises — a read-only /boot or a nonstandard fstab must not stop the
    encoder from encoding."""
    if fake_mode():
        return []
    changed = []
    try:
        if os.path.exists(fstab) and _fstab_harden(fstab, mount):
            changed.append(fstab)
    except OSError:
        pass
    try:
        if os.path.exists(cmdline) and _cmdline_fsck_repair(cmdline):
            changed.append(cmdline)
    except OSError:
        pass
    return changed


def _fstab_harden(path, mount):
    out, dirty = [], False
    for ln in open(path).read().splitlines():
        parts = ln.split()
        if len(parts) >= 4 and not ln.lstrip().startswith('#') \
                and parts[1] == mount:
            opts = [o for o in parts[3].split(',') if o]
            have = {o.split('=')[0] for o in opts}
            opts += [o for o in FSTAB_OPTS if o.split('=')[0] not in have]
            while len(parts) < 6:
                parts.append('0')
            fields = parts[:3] + [','.join(opts), parts[4], '2']
            if fields != parts:
                ln = '  '.join(fields)
                dirty = True
        out.append(ln)
    if dirty:
        open(path, 'w').write('\n'.join(out) + '\n')
    return dirty


def _cmdline_fsck_repair(path):
    raw = open(path).read()
    if 'fsck.repair=' in raw:
        return False               # an explicit choice — keep it
    open(path, 'w').write(raw.rstrip('\n') + ' fsck.repair=yes\n')
    return True


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
                                  'playcall-encoder-mediamtx',
                                  'playcall-encoder-clipper')):
    if fake_mode() or not have('journalctl'):
        return []
    cmd = ['journalctl', '--no-pager', '-n', str(lines), '-o', 'short-iso']
    for u in units:
        cmd += ['-u', u]
    r = run(cmd)
    return (r.stdout or '').splitlines()[-lines:]
