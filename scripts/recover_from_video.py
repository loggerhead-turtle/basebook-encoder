#!/usr/bin/env python3
"""Rebuild a game's clips from a whole-game video shot on something else.

When the recording drive dies, the FOOTAGE is lost but the index is not.
Every play still has its window, its label, its inning and the players
tagged in it — all of that came from the scorer's taps, not the camera.
So if somebody also filmed the game on an iPad, a phone or a camcorder,
the clips can be rebuilt: cut that recording at the same timestamps and
upload the pieces.

Runs on your COMPUTER, not the box — the whole-game file never has to
move anywhere, and only the finished clips (a few MB each) go up.

    # 1. which games have clips that were never cut?
    python3 recover_from_video.py --list

    # 2. try ONE clip and check it landed on the right play
    python3 recover_from_video.py --game skg_abc123 --video game.mov --limit 1

    # 3. if the play is late by 8 seconds, shift and do the rest
    python3 recover_from_video.py --game skg_abc123 --video game.mov \
        --offset -8

Credentials come from the encoder's own config (--base/--key, or copy
them out of /etc/playcall-encoder/config.json on the box). Needs ffmpeg
and ffprobe on PATH.

A camera that already records H.264/AAC — a Mevo, most action cams, OBS
— is stream-copied: about 15x faster over a whole game and with no
generation of quality lost. HEVC (what iPhones and iPads shoot by
default) is transcoded instead, because a clip a browser refuses to
play is not a recovered clip. --copy / --reencode override the guess.

THE ANCHOR IS THE WHOLE PROBLEM. Clip windows are absolute wall-clock
times; a video file is just a timeline. Line them up wrongly by twenty
seconds and every clip catches the wrong pitch. This reads the file's
creation_time for a first guess, checks it against the plays both ways
round (some cameras stamp the START of a recording, some the END), and
prints how many windows the guess actually covers. Always cut one clip
first and look at it before committing to the batch.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone


def api(base, key, path, data=None, ctype='application/json', timeout=120):
    req = urllib.request.Request(
        base.rstrip('/') + path, data=data,
        headers={'X-Api-Key': key, 'Content-Type': ctype},
        method='POST' if data is not None else 'GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def probe(video):
    """(duration_seconds, creation_epoch or None)."""
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-print_format', 'json',
         '-show_format', str(video)],
        capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        sys.exit(f'ffprobe could not read {video}:\n{out.stderr.strip()}')
    fmt = (json.loads(out.stdout or '{}').get('format') or {})
    dur = float(fmt.get('duration') or 0)
    raw = (fmt.get('tags') or {}).get('creation_time') or ''
    made = None
    if raw:
        try:
            made = datetime.fromisoformat(
                raw.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return dur, made


def choose_anchor(clips, dur, made, forced=None):
    """Wall-clock time of the video's first frame.

    A camera's creation_time may be when recording STARTED or when the
    file was CLOSED — a whole game apart, and picking wrong puts every
    cut in the wrong half of the afternoon. Rather than trust either,
    score both against the plays we are trying to find and take the one
    that actually contains them."""
    if forced is not None:
        return forced, 'you gave it'
    if made is None:
        sys.exit('This file carries no creation time, so there is nothing '
                 'to line the plays up against.\nPass the moment recording '
                 'started with --start "YYYY-MM-DD HH:MM:SS" (local time).')

    def covered(anchor):
        return sum(1 for c in clips
                   if anchor <= c['start'] and c['end'] <= anchor + dur)
    as_start, as_end = made, made - dur
    n_start, n_end = covered(as_start), covered(as_end)
    if n_start >= n_end:
        return as_start, f'file says recording began then ({n_start}/' \
                         f'{len(clips)} plays fall inside the video)'
    return as_end, f'file time looks like when recording STOPPED ' \
                   f'({n_end}/{len(clips)} plays fall inside the video)'


def probe_codecs(video):
    """(video_codec, audio_codec), lowercased, '' when a track is absent."""
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-print_format', 'json',
         '-show_streams', str(video)],
        capture_output=True, text=True, timeout=120)
    v = a = ''
    for s in (json.loads(out.stdout or '{}').get('streams') or []):
        kind, name = s.get('codec_type'), (s.get('codec_name') or '').lower()
        if kind == 'video' and not v:
            v = name
        elif kind == 'audio' and not a:
            a = name
    return v, a


def pick_mode(vcodec, acodec, force=None):
    """(stream_copy?, why). Copy when the source is already what a
    browser wants.

    A camera that records H.264/AAC — a Mevo, most action cams, OBS —
    needs no transcode at all: copying is ~15x faster over a whole game
    and avoids a generation of quality loss. The cost is that a copy can
    only start on a keyframe, so ffmpeg backs up to the one before the
    window; the clip runs a beat long at the front, which the pre-roll
    padding was already there to absorb.

    HEVC (iPhones and iPads default to it) has to be re-encoded — a fair
    number of browsers refuse to play it, and a clip nobody can watch is
    not a recovered clip."""
    if force is not None:
        return force, 'you asked for it'
    if vcodec == 'h264' and acodec in ('aac', ''):
        return True, f'source is already {vcodec}/{acodec or "no audio"}'
    return False, f'source is {vcodec or "?"}/{acodec or "no audio"} — ' \
                  'transcoding so every browser can play it'


def cut_args(video, offset, duration, dest, copy):
    """The ffmpeg argv for one clip. Split out so the choice of codec
    path is testable without running ffmpeg over a whole game."""
    codec = (['-c', 'copy'] if copy else
             ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
              '-c:a', 'aac', '-ac', '2'])
    return ['ffmpeg', '-y', '-v', 'error', '-ss', f'{offset:.2f}',
            '-i', str(video), '-t', f'{duration:.2f}', *codec,
            '-avoid_negative_ts', 'make_zero', '-movflags', '+faststart',
            str(dest)]


def cut(video, offset, duration, dest, copy=False):
    r = subprocess.run(cut_args(video, offset, duration, dest, copy),
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or 'ffmpeg failed').strip()[-300:])


def human(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M:%S')


def main():
    p = argparse.ArgumentParser(
        description='Rebuild a game\'s clips from a whole-game video.')
    p.add_argument('--base', default=os.environ.get('PLAYCALL_BASE',
                                                    'https://basebook.org'))
    p.add_argument('--key', default=os.environ.get('PLAYCALL_KEY', ''),
                   help="the encoder's cloud api_key")
    p.add_argument('--list', action='store_true',
                   help='show games with clips that were never cut')
    p.add_argument('--game', help='game id to recover')
    p.add_argument('--video', help='the whole-game recording')
    p.add_argument('--start', help='when recording started, local time, '
                                   '"YYYY-MM-DD HH:MM:SS" — overrides the '
                                   "file's own timestamp")
    p.add_argument('--offset', type=float, default=0.0,
                   help='shift every cut by N seconds (negative = earlier)')
    p.add_argument('--limit', type=int, default=0,
                   help='stop after N clips — use 1 to check the alignment')
    p.add_argument('--dry-run', action='store_true',
                   help='work out the cuts and print them, upload nothing')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--copy', dest='force', action='store_const', const=True,
                   help='stream-copy even if the codec looks unplayable')
    g.add_argument('--reencode', dest='force', action='store_const',
                   const=False, help='always transcode to H.264/AAC')
    p.set_defaults(force=None)
    a = p.parse_args()
    if not a.key:
        sys.exit('No API key. Pass --key, or set PLAYCALL_KEY. It is the '
                 'cloud.api_key in /etc/playcall-encoder/config.json on '
                 'the box.')

    if a.list or not a.game:
        d = api(a.base, a.key, '/api/pi/clips/recoverable')
        games = d.get('games') or []
        if not games:
            print('No games have uncut clips — nothing to recover.')
            return
        print(f'{"GAME":<26} {"OPPONENT":<22} CLIPS  PLAYED')
        for g in games:
            print(f'{g["game_id"]:<26} {g["opponent"][:21]:<22} '
                  f'{g["clips"]:>5}  '
                  f'{datetime.fromtimestamp(g["first"]):%Y-%m-%d %H:%M}'
                  f'–{human(g["last"])}')
        print('\nThen: --game <GAME> --video <file> --limit 1')
        return

    if not a.video:
        sys.exit('--video is required with --game')
    clips = (api(a.base, a.key,
                 f'/api/pi/clips/recoverable?game={a.game}').get('clips')
             or [])
    if not clips:
        sys.exit(f'{a.game} has no uncut clips — nothing to do.')

    dur, made = probe(a.video)
    forced = None
    if a.start:
        try:
            forced = datetime.strptime(a.start,
                                       '%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            sys.exit('--start must look like "2026-08-15 16:05:00"')
    anchor, why = choose_anchor(clips, dur, made, forced)
    anchor += a.offset

    vcodec, acodec = probe_codecs(a.video)
    copy, mode_why = pick_mode(vcodec, acodec, a.force)

    print(f'{len(clips)} uncut plays in {a.game}')
    print(f'video     {os.path.basename(a.video)} — {dur / 60:.1f} min')
    print(f'starts at {datetime.fromtimestamp(anchor):%Y-%m-%d %H:%M:%S} '
          f'({why})')
    print(f'cutting   {"stream copy" if copy else "re-encode"} — {mode_why}')
    if a.offset:
        print(f'offset    {a.offset:+.1f}s applied')

    todo = []
    for c in clips:
        off = c['start'] - anchor
        length = max(1.0, c['end'] - c['start'])
        if off < 0 or off + length > dur:
            continue                      # this play is outside the video
        todo.append((c, off, length))
    print(f'{len(todo)} of them fall inside this recording'
          + ('' if len(todo) == len(clips) else
             f' ({len(clips) - len(todo)} outside it — wrong video, or the '
             'camera was not rolling yet)'))
    if not todo:
        sys.exit('\nNothing lines up. If the video IS the right game, the '
                 'anchor is wrong:\ncheck --start, or try --offset to shift '
                 'it.')
    if a.limit:
        todo = todo[:a.limit]
        print(f'--limit {a.limit}: doing the first {len(todo)}')

    ok = bad = 0
    tmp = tempfile.mkdtemp(prefix='recover-')
    try:
        for c, off, length in todo:
            label = (c['label'] or c['id'])[:48]
            when = human(c['start'])
            if a.dry_run:
                print(f'  would cut {when} at {off / 60:6.1f} min in — '
                      f'{label}')
                ok += 1
                continue
            dest = os.path.join(tmp, f'{c["id"]}.mp4')
            print(f'  {when}  {off / 60:6.1f} min in  {label} … ', end='',
                  flush=True)
            try:
                cut(a.video, off, length, dest, copy=copy)
                with open(dest, 'rb') as fh:
                    body = fh.read()
                api(a.base, a.key, f'/api/pi/clips/{c["id"]}/upload',
                    data=body, ctype='video/mp4', timeout=600)
                print(f'{len(body) / 1e6:.1f} MB uploaded')
                ok += 1
            except (urllib.error.URLError, RuntimeError, OSError) as e:
                print(f'FAILED — {e}')
                bad += 1
            finally:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    finally:
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    print(f'\n{ok} recovered, {bad} failed')
    if a.limit and ok:
        print('Now open that game on the Videos page and watch the clip.\n'
              'Right play? Re-run without --limit.\n'
              'Play happens EARLY in the clip? The anchor is late — try '
              '--offset -5.\nPlay happens LATE, or is missed? try '
              '--offset +5.')


if __name__ == '__main__':
    main()
