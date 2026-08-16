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
import threading


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


# ── recording-storage health ──────────────────────────────────────────────
# The mount MediaMTX records into and the clipper cuts from. A box once
# streamed a whole evening into a dead NVMe — the controller dropped off
# the PCIe bus, ext4 went emergency-read-only, and every card still said
# "pushing" because the stream itself never touches the disk.
# (DATA_MOUNT is defined by the power-cut section above)
PROBE_TIMEOUT = 5.0
# The filesystem label the recordings volume is created with. When a
# device carrying it EXISTS but is not the one backing the recordings
# path, the drive is present and simply not mounted — and recording is
# silently falling back onto the SD card. A box with no such device is
# an SD-card-only install, which is supported and must stay quiet.
DATA_LABEL = 'playcall-video'

_probe_thread = None
_probe_lock = threading.Lock()


def _mounted_read_only(path, mounts='/proc/mounts'):
    """True when the filesystem holding `path` can no longer be written —
    mounted ro, or in ext4's emergency_ro/shutdown state. (Observed on a
    real failure: after the NVMe controller dropped, the options read
    'rw,noatime,emergency_ro,shutdown' — still claiming rw, so the plain
    ro flag alone is not enough.) Longest-prefix match over the mount
    table; unreadable table → assume writable and let the write probe
    decide."""
    try:
        real = os.path.realpath(path)
        best, ro = '', False
        for ln in open(mounts):
            parts = ln.split()
            if len(parts) < 4:
                continue
            mnt = parts[1].replace('\\040', ' ')
            if (real == mnt or real.startswith(mnt.rstrip('/') + '/')) \
                    and len(mnt) >= len(best):
                best = mnt
                ro = bool({'ro', 'emergency_ro', 'shutdown'}
                          & set(parts[3].split(',')))
        return ro
    except OSError:
        return False


def _mount_device(path, mounts='/proc/mounts'):
    """The device backing `path` (longest-prefix match over the mount
    table), '' when it can't be determined."""
    try:
        real = os.path.realpath(path)
        best, dev = '', ''
        for ln in open(mounts):
            parts = ln.split()
            if len(parts) < 4:
                continue
            mnt = parts[1].replace('\\040', ' ')
            if (real == mnt or real.startswith(mnt.rstrip('/') + '/')) \
                    and len(mnt) >= len(best):
                best, dev = mnt, parts[0]
        return dev
    except OSError:
        return ''


def _fallback_ok():
    """Has someone declared recording-to-the-SD-card deliberate?"""
    try:
        from . import config
        return bool(config.load().get('record_fallback_ok'))
    except Exception:
        return False


def _labeled_device(label=DATA_LABEL):
    """The device carrying the recordings-volume label, '' if none."""
    p = f'/dev/disk/by-label/{label}'
    try:
        if os.path.exists(p):
            return os.path.realpath(p)
    except OSError:
        pass
    return ''


def _probe_write(path, result):
    probe = os.path.join(path, '.storage-probe')
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b'playcall storage probe\n')
            os.fsync(fd)           # force real I/O — the page cache lies
        finally:
            os.close(fd)
        os.unlink(probe)
    except OSError as e:
        result['error'] = (f'cannot write to {path}: '
                           f'{e.strerror or e}')


def storage_status(path=None):
    """Can the recordings volume actually take a write, right now?

    Three answers, cheapest first: is the mount read-only (the state a
    dying drive leaves behind), does a real create-write-fsync-unlink
    succeed, and how much space is left. The probe runs on a helper
    thread with a timeout because a failing controller doesn't always
    error — sometimes it just hangs the caller in D-state, and this
    check must never wedge the heartbeat loop that reports it.

    Returns {'ok', 'error', 'read_only', 'free_gb', 'path'} and never
    raises; 'error' is a human sentence that goes straight onto the
    settings card and into the heartbeat."""
    global _probe_thread
    path = path or os.environ.get('PLAYCALL_ENCODER_DATA')
    if path is None:
        if fake_mode():            # laptop dev: no /var/lib mount to probe
            return {'ok': True, 'error': '', 'read_only': False,
                    'free_gb': None, 'path': '', 'device': '',
                    'fallback': False}
        path = DATA_MOUNT
    st = {'ok': False, 'error': '', 'read_only': False, 'free_gb': None,
          'path': path, 'device': '', 'fallback': False}
    if not os.path.isdir(path):
        st['error'] = (f'{path} is missing — is the recordings drive '
                       'mounted?')
        return st
    if _mounted_read_only(path):
        st['read_only'] = True
        st['error'] = ('filesystem is read-only — the kernel shut it '
                       'down after an I/O error (failing drive?)')
        return st
    try:
        s = os.statvfs(path)
        st['free_gb'] = round(s.f_bavail * s.f_frsize / 1e9, 1)
    except OSError:
        pass
    # Writable is not the same as writing to the RIGHT disk. A recordings
    # drive that fails to mount (dead controller, an fstab line commented
    # out during a previous outage and never restored) leaves this path a
    # plain directory on the SD card: every write succeeds, every check
    # looks green, and the box quietly fills its root filesystem with
    # footage until it takes itself down. Seen in the field at 113 GB
    # "free" on a 477 GB drive.
    st['device'] = _mount_device(path)
    want = _labeled_device()
    if want and st['device'] and os.path.realpath(st['device']) != want:
        if _fallback_ok():
            # Declared deliberate — keep probing writability and free
            # space, but say WHERE it is landing rather than cry wolf.
            st['fallback'] = True
        else:
            st['error'] = (f"recordings are landing on {st['device']}, not "
                           f'the recordings drive ({want}) — that drive is '
                           'not mounted, so this is the SD card filling up')
            return st
    with _probe_lock:
        if _probe_thread is not None and _probe_thread.is_alive():
            # the previous probe never came back — that IS the diagnosis
            st['error'] = (f'a write to {path} is hanging — the drive '
                           'is not answering')
            return st
        res = {}
        _probe_thread = threading.Thread(target=_probe_write,
                                         args=(path, res), daemon=True)
        _probe_thread.start()
        _probe_thread.join(PROBE_TIMEOUT)
        if _probe_thread.is_alive():
            st['error'] = (f'a write to {path} is hanging — the drive '
                           'is not answering')
            return st
    if res.get('error'):
        st['error'] = res['error']
        return st
    if st['free_gb'] is not None and st['free_gb'] < 2:
        st['error'] = (f"only {st['free_gb']} GB free — recording "
                       'is about to stop')
        return st
    st['ok'] = True
    return st


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
