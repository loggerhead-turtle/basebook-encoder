"""Power cuts: survive the yanked cord, and offer a better way out.

FIELD REPORT, after the recordings drive repair: the box's own smart-log
showed 24 unsafe shutdowns in 30 power cycles — it lives on a USB power
bank and gets unplugged, not shut down. One of those cuts put the ext4
on the NVMe into emergency shutdown and killed the clips pipeline until
a hand-run fsck.

Two answers, both here:
  * harden_storage(): boot-time fsck (fs_passno 2 + fsck.repair=yes) and
    crash-safe mount options for the recordings mount, applied
    idempotently on every boot;
  * the pad's power button: the assignment poll may answer
    {"shutdown": true} and the box powers off CLEANLY.

Run: python3 -m pytest tests/test_power_safety.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop('SCOREBUG_FAKE', None)

from encoder import cloud_link, system                    # noqa: E402

MOUNT = '/var/lib/playcall-encoder'


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    """conftest fakes the whole box; storage hardening is exactly the
    kind of thing fake mode must skip, so these tests run it real."""
    monkeypatch.setenv('SCOREBUG_FAKE', '0')


@pytest.fixture
def fstab(tmp_path):
    p = tmp_path / 'fstab'
    p.write_text(
        'proc            /proc           proc    defaults          0       0\n'
        'PARTUUID=aaaa-01  /boot/firmware  vfat    defaults          0       2\n'
        'PARTUUID=aaaa-02  /               ext4    defaults,noatime  0       1\n'
        f'/dev/nvme0n1p1 {MOUNT} ext4 defaults,noatime 0 0\n'
        '# a comment line stays a comment line\n')
    return p


@pytest.fixture
def cmdline(tmp_path):
    p = tmp_path / 'cmdline.txt'
    p.write_text('console=serial0,115200 console=tty1 root=PARTUUID=aaaa-02 '
                 'rootfstype=ext4 rootwait\n')
    return p


def _data_line(path):
    return next(ln for ln in path.read_text().splitlines()
                if MOUNT in ln and not ln.lstrip().startswith('#'))


# ── the fstab repair ─────────────────────────────────────────────────────

def test_the_recordings_mount_gets_fsck_and_crash_safe_options(fstab,
                                                               cmdline):
    changed = system.harden_storage(str(fstab), str(cmdline), MOUNT)
    assert str(fstab) in changed
    ln = _data_line(fstab).split()
    assert ln[5] == '2', 'fs_passno 2 = systemd fscks it before mounting'
    opts = ln[3].split(',')
    for want in ('noatime', 'nofail', 'commit=1',
                 'x-systemd.device-timeout=10'):
        assert want in opts, want
    assert opts.count('noatime') == 1, 'no duplicate options'


def test_every_other_line_is_untouched(fstab, cmdline):
    before = fstab.read_text().splitlines()
    system.harden_storage(str(fstab), str(cmdline), MOUNT)
    after = fstab.read_text().splitlines()
    assert len(before) == len(after)
    for b, a in zip(before, after):
        if MOUNT not in b:
            assert b == a, 'only the recordings mount line may change'
    assert after[-1].startswith('#')


def test_running_it_again_changes_nothing(fstab, cmdline):
    system.harden_storage(str(fstab), str(cmdline), MOUNT)
    once = fstab.read_text()
    changed = system.harden_storage(str(fstab), str(cmdline), MOUNT)
    assert str(fstab) not in changed
    assert fstab.read_text() == once


def test_an_existing_commit_value_is_respected(tmp_path, cmdline):
    """commit=5 is somebody's explicit choice — add what is missing,
    never fight what is set."""
    p = tmp_path / 'fstab'
    p.write_text(f'/dev/nvme0n1p1 {MOUNT} ext4 defaults,commit=5 0 2\n')
    system.harden_storage(str(p), str(cmdline), MOUNT)
    opts = _data_line(p).split()[3].split(',')
    assert 'commit=5' in opts and 'commit=1' not in opts


def test_a_box_without_the_mount_line_is_left_alone(tmp_path, cmdline):
    p = tmp_path / 'fstab'
    p.write_text('PARTUUID=aaaa-02  /  ext4  defaults,noatime  0  1\n')
    before = p.read_text()
    changed = system.harden_storage(str(p), str(cmdline), MOUNT)
    assert str(p) not in changed
    assert p.read_text() == before


# ── the kernel command line ──────────────────────────────────────────────

def test_fsck_repair_yes_is_appended_once(fstab, cmdline):
    system.harden_storage(str(fstab), str(cmdline), MOUNT)
    raw = cmdline.read_text()
    assert raw.count('fsck.repair=yes') == 1
    assert len(raw.splitlines()) == 1, 'cmdline.txt must stay one line'
    changed = system.harden_storage(str(fstab), str(cmdline), MOUNT)
    assert str(cmdline) not in changed


def test_an_explicit_fsck_answer_is_kept(fstab, tmp_path):
    p = tmp_path / 'cmdline.txt'
    p.write_text('console=tty1 root=PARTUUID=aaaa-02 fsck.repair=no\n')
    system.harden_storage(str(fstab), str(p), MOUNT)
    assert 'fsck.repair=no' in p.read_text()
    assert 'fsck.repair=yes' not in p.read_text()


def test_missing_files_never_raise(tmp_path):
    assert system.harden_storage(str(tmp_path / 'nope'),
                                 str(tmp_path / 'also_nope'), MOUNT) == []


def test_fake_mode_touches_nothing(fstab, cmdline, monkeypatch):
    monkeypatch.setenv('SCOREBUG_FAKE', '1')
    before = fstab.read_text()
    assert system.harden_storage(str(fstab), str(cmdline), MOUNT) == []
    assert fstab.read_text() == before


# ── the pad's power button, box side ─────────────────────────────────────

def _link(response, calls):
    cfg = {'cloud': {'base_url': 'http://cloud', 'api_key': 'k'},
           'youtube': {}}
    return cloud_link.CloudLink(
        cfg_load=lambda: dict(cfg, youtube=dict(cfg['youtube'])),
        cfg_save=lambda c: None,
        runner=lambda cmd, **kw: calls.append(cmd),
        http=lambda url, **kw: dict(response))


def test_a_shutdown_answer_powers_the_box_off_cleanly(caplog):
    calls = []
    link = _link({'assigned': True, 'team_id': 't1', 'shutdown': True},
                 calls)
    assert link.poll_assignment_once() is True
    assert ['systemctl', 'poweroff'] in calls
    assert link.running is False, 'the poll loop must stop asking'


def test_a_normal_answer_never_touches_the_power(caplog):
    calls = []
    link = _link({'assigned': False, 'team_id': None, 'team_name': None,
                  'bug_feed_url': None, 'youtube_rtmp_url': None,
                  'game_id': None}, calls)
    link.poll_assignment_once()
    assert ['systemctl', 'poweroff'] not in calls
    assert link.running is True


def test_shutdown_wins_before_any_assignment_side_effects():
    """The box must not restart the push service on its way down —
    shutdown short-circuits the assignment handling entirely."""
    calls = []
    link = _link({'assigned': True, 'team_id': 't1', 'shutdown': True,
                  'youtube_rtmp_url': 'rtmp://a.rtmp.youtube.com/live2/x'},
                 calls)
    link.poll_assignment_once()
    assert calls == [['systemctl', 'poweroff']]
