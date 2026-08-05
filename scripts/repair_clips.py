#!/usr/bin/env python3
"""One-shot: heal already-uploaded clips whose clock is broken.

Cuts made before encoder 1.2.6 inherited the rolling recording's
mid-stream timestamps — a video track starting at a NEGATIVE pts makes
phone browsers stutter and jump to the end of a clip whose media is
fine. This walks the box's local archive, remuxes each clip to a zeroed
clock (+faststart), and re-uploads it over the original
(?repair=1). Run on the box:

    sudo python3 /opt/playcall-encoder/scripts/repair_clips.py
"""
import json
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/playcall-encoder')
from encoder import config                                    # noqa: E402

CLIPS = Path('/var/lib/playcall-encoder/clips')


def first_vpts(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'packet=pts_time', '-of', 'csv=p=0',
             str(path)], capture_output=True, text=True, timeout=60)
        return float(out.stdout.split('\n', 1)[0])
    except Exception:
        return None


def main():
    cfg = config.load()
    cloud = cfg.get('cloud') or {}
    base = (cloud.get('base_url') or '').rstrip('/')
    key = cloud.get('api_key') or ''
    if not (base and key):
        sys.exit('box is not paired to a cloud')
    fixed = skipped = failed = 0
    for f in sorted(CLIPS.glob('clip_*.mp4')):
        if f.stat().st_size < 1024:
            continue
        cid = f.name.split('_')[0] + '_' + f.name.split('_')[1]
        pts = first_vpts(f)
        if pts is None or pts >= -0.05:
            skipped += 1
            continue
        print(f'{f.name}: video starts at {pts:.2f}s — repairing…')
        try:
            with tempfile.NamedTemporaryFile(suffix='.mp4') as out:
                subprocess.run(
                    ['ffmpeg', '-y', '-v', 'error', '-i', str(f),
                     '-c', 'copy', '-avoid_negative_ts', 'make_zero',
                     '-movflags', '+faststart', out.name],
                    check=True, timeout=180)
                data = Path(out.name).read_bytes()
            req = urllib.request.Request(
                f'{base}/api/pi/clips/{cid}/upload?repair=1', data=data,
                headers={'X-Api-Key': key, 'content-type': 'video/mp4'})
            with urllib.request.urlopen(req, timeout=600) as resp:
                r = json.loads(resp.read().decode() or '{}')
            if r.get('ok'):
                f.write_bytes(data)          # archive keeps the healed copy
                fixed += 1
                print(f'  ✓ repaired and re-uploaded ({len(data)//1048576} MB)')
            else:
                failed += 1
                print(f'  ✗ cloud said: {r}')
        except urllib.error.HTTPError as e:
            failed += 1
            try:
                body = e.read(300).decode('utf-8', 'replace').strip()
            except Exception:
                body = ''
            # the response body says WHY (storage limit, size…) — a bare
            # status line made the big-clip failures undiagnosable
            print(f'  ✗ HTTP {e.code}: {body or e.reason}')
        except Exception as e:
            failed += 1
            print(f'  ✗ {e}')
    print(f'\ndone: {fixed} repaired, {skipped} already clean, {failed} failed')


if __name__ == '__main__':
    main()
