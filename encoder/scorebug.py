#!/usr/bin/env python3
"""PlayCall scorebug renderer + NDI/HTTP sender (evolved from
pi/ndi_scorebug.py in the main Play-call repo).

Polls the cloud bug feed JSON, re-rasterizes only when the frame `seq` (or
the look) changes, and publishes the image three ways at once:
  * real NDI ("PlayCall Bug") when ndi-python + the NDI runtime are present,
  * always as http://<pi>:8765/bug.png and /bug.mjpg (vMix/OBS fallback),
  * SCOREBUG_FAKE=1 → PNG files on disk instead of NDI (laptop dev / tests).

FEED THEME SPEC v2 — the feed may carry a "theme" block that fully drives
the look; parsed defensively, every key optional:

    "theme": {
      "version": 2,
      "layout":  "bar|tv|tvbox|bottomline|lowerthird|sidestack",
      "pos":     "tl|tc|tr|bl|bc|br",
      "scale":   1.0,
      "colors":  {"bg": "#161b22", "accent": "#10b981",
                  "accent2": "#f5b301", "text": "#ffffff"},
      "font":    "oswald|block|serif|system",
      "show":    {"pitchcount": false, "outs": true, "count": true,
                  "bases": true, "inning": true},
      "name":    "My theme"
    }

Unknown layout falls back to 'bar'. No theme block at all → the legacy
behavior: layout/pos/scale/bandwidth from the feed's `ndi` config block
(or the CLI flags), default palette.
"""

import argparse
import io
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger('scorebug')

# NDI bindings are optional — the MJPEG/PNG fallback always works.
_NDI = None
try:
    import NDIlib as _NDI            # ndi-python
except ImportError:
    try:
        import ndi as _NDI           # some distros package it as `ndi`
    except ImportError:
        _NDI = None

W, H = 800, 450                      # NDI canvas; the bug lands in a corner.
                                      # Sized so the mixer (Mevo Studio) can
                                      # DECODE it on the same CPU budget as
                                      # every real camera input — see the
                                      # original pi/ndi_scorebug.py notes.
PAD = 16    # local-canvas margin around each layout's box, for drop shadows

BANDWIDTH_LEVELS = [
    {'label': 'Full',       'render_scale': 1.0,  'fps': None},  # ~2.3 Mbps
    {'label': 'Reduced',    'render_scale': 0.75, 'fps': 3},     # ~1.0 Mbps
    {'label': 'Data saver', 'render_scale': 0.6,  'fps': 2},     # ~0.4 Mbps
    {'label': 'Minimum',    'render_scale': 0.4,  'fps': 1},     # ~0.1 Mbps
]

# ── theme parsing ─────────────────────────────────────────────────────────────

LAYOUTS = ('bar', 'tv', 'tvbox', 'bottomline', 'lowerthird', 'sidestack')
POSITIONS = ('tl', 'tc', 'tr', 'bl', 'bc', 'br')

DEFAULT_THEME = {
    'version': 2,
    'layout': 'bar',
    'pos': 'bl',
    'scale': 1.0,
    'colors': {'bg': '#161b22', 'accent': '#10b981',
               'accent2': '#f5b301', 'text': '#ffffff'},
    'font': 'system',
    'show': {'pitchcount': True, 'outs': True, 'count': True,
             'bases': True, 'inning': True},
    'name': '',
}


def parse_theme(raw):
    """Defensive merge of a feed theme block onto the defaults. Returns None
    when the feed has no usable theme block (legacy feeds)."""
    if not isinstance(raw, dict):
        return None
    t = json.loads(json.dumps(DEFAULT_THEME))     # cheap deep copy
    layout = str(raw.get('layout', '')).lower()
    t['layout'] = layout if layout in LAYOUTS else 'bar'
    pos = str(raw.get('pos', '')).lower()
    if pos in POSITIONS:
        t['pos'] = pos
    elif pos == 'custom':
        # Dragging the bug in the Studio preview saves pos:'custom' with x/y
        # viewport percentages. Before this branch existed, 'custom' failed
        # the POSITIONS check and silently fell back to the default 'bl' —
        # so a bug dragged to the top-left rendered bottom-left here while
        # the browser overlay showed it correctly.
        try:
            t['xy'] = (float(raw.get('x', 0)), float(raw.get('y', 0)))
            t['pos'] = 'custom'
        except (TypeError, ValueError):
            log.warning('theme pos=custom without usable x/y — using %s',
                        t['pos'])
    elif pos:
        log.warning('unknown theme pos %r — using %s', pos, t['pos'])
    try:
        t['scale'] = max(0.4, min(1.3, float(raw.get('scale', 1.0))))
    except (TypeError, ValueError):
        pass
    colors = raw.get('colors')
    if isinstance(colors, dict):
        for k in t['colors']:
            v = colors.get(k)
            if isinstance(v, str) and v.strip():
                t['colors'][k] = v.strip()
    font = str(raw.get('font', '')).lower()
    if font in ('oswald', 'block', 'serif', 'system'):
        t['font'] = font
    show = raw.get('show')
    if isinstance(show, dict):
        for k in t['show']:
            if k in show:
                t['show'][k] = bool(show[k])
    t['name'] = str(raw.get('name', ''))[:80]
    return t


def _hex_rgba(color, fallback):
    try:
        c = color.lstrip('#')
        return tuple(int(c[j:j + 2], 16) for j in (0, 2, 4)) + (255,)
    except Exception:
        return fallback


def _blend(a, b, t):
    """Opaque blend of two RGBA colors — PIL's ImageDraw overwrites alpha
    rather than compositing, so 'dim' colors must be pre-blended opaque or
    they punch see-through holes in the panel (see the original renderer's
    hard-won comment)."""
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,)


def _palette(theme):
    c = theme['colors']
    bg = _hex_rgba(c.get('bg'), (22, 27, 34, 255))
    text = _hex_rgba(c.get('text'), (255, 255, 255, 255))
    accent = _hex_rgba(c.get('accent'), (16, 185, 129, 255))
    accent2 = _hex_rgba(c.get('accent2'), (245, 179, 1, 255))
    return {
        'panel': bg[:3] + (242,),
        'bg': bg,
        'text': text,
        'accent': accent,
        'accent2': accent2,
        'off': _blend(bg, text, 0.18),      # unlit bases/outs/dividers
        'dim': _blend(bg, text, 0.55),
        'ink': _blend(bg, (0, 0, 0, 255), 0.5),
        'shadow': (0, 0, 0, 90),
    }


# DejaVu ships on every Raspberry Pi OS / Debian; each requested face maps
# to the closest DejaVu variant so the theme picker works with zero font
# downloads. First entry that loads wins.
_FONT_FILES = {
    'oswald': ['DejaVuSansCondensed-Bold.ttf', 'DejaVuSansCondensed.ttf',
               'DejaVuSans-Bold.ttf'],
    'block':  ['DejaVuSans-Bold.ttf'],
    'serif':  ['DejaVuSerif-Bold.ttf', 'DejaVuSerif.ttf'],
    'system': ['DejaVuSans-Bold.ttf'],
}
_FONT_DIRS = ['', '/usr/share/fonts/truetype/dejavu/']
_font_cache = {}


def _font(theme, size, bold=True):
    fam = theme['font'] if theme else 'system'
    key = (fam, size, bold)
    if key in _font_cache:
        return _font_cache[key]
    names = list(_FONT_FILES.get(fam, _FONT_FILES['system']))
    if not bold:
        names = [n.replace('-Bold', '') for n in names] + names
    f = None
    for n in names:
        for d in _FONT_DIRS:
            try:
                f = ImageFont.truetype(d + n, size)
                break
            except Exception:
                continue
        if f:
            break
    f = f or ImageFont.load_default()
    _font_cache[key] = f
    return f


def _last_name(name):
    return str(name or '').strip().split(' ')[-1].upper()


def _team_color(t, fallback):
    return _hex_rgba((t or {}).get('color') or '', fallback)


def _place(local, pos, scale, xy=None):
    """Scale a bug rendered at local (0,0) origin and paste it onto a
    transparent WxH canvas at the requested corner. All layouts share this
    so position/scale math lives in exactly one place.

    pos 'custom' uses `xy` — the (x, y) viewport PERCENTAGES the Score Bug
    Studio theme editor stores when you drag the bug in its preview. Those
    are resolution-independent, so the same drag lands in the same relative
    spot here as on the browser overlay."""
    scale = max(0.4, min(1.3, float(scale or 1.0)))
    if scale != 1.0:
        local = local.resize((max(1, round(local.width * scale)),
                              max(1, round(local.height * scale))),
                             Image.LANCZOS)
    canvas = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    if pos == 'custom' and xy:
        x0 = round(W * float(xy[0]) / 100.0)
        y0 = round(H * float(xy[1]) / 100.0)
        # Keep it on-canvas even if the drag went near an edge.
        x0 = max(0, min(x0, W - local.width))
        y0 = max(0, min(y0, H - local.height))
    else:
        x0 = 40 if 'l' in pos else (W - local.width - 40 if 'r' in pos
                                    else (W - local.width) // 2)
        y0 = 40 if pos.startswith('t') else H - local.height - 40
    canvas.paste(local, (x0, y0), local)
    return canvas


def _diamond(d, cx, cy, size, on, pal, gap=None):
    """1B/2B/3B diamond cluster centered near (cx, cy)."""
    gap = gap or round(size * 1.55)
    spots = [(cx + gap, cy), (cx, cy - gap), (cx - gap, cy)]   # 1B 2B 3B
    for i, (px, py) in enumerate(spots):
        pts = [(px, py - size), (px + size, py),
               (px, py + size), (px - size, py)]
        lit = i < len(on) and on[i]
        d.polygon(pts, fill=pal['accent'] if lit else pal['off'])


def _outs(d, x, y, n, pal, r=7, step=20):
    for i in range(3):
        d.ellipse((x + i * step, y, x + i * step + r * 2, y + r * 2),
                  fill=pal['accent2'] if i < (n or 0) else pal['off'])


# ── layout family renderers ──────────────────────────────────────────────────
# Each takes (bug, theme) and returns the local RGBA image (pre-_place).

BUG_W, BUG_H = 560, 132


def _render_bar(bug, theme):
    """Classic horizontal bar: team rows | inning/count/outs | bases."""
    pal, show = _palette(theme), theme['show']
    f_abbr, f_runs = _font(theme, 34), _font(theme, 44)
    f_inn, f_cnt = _font(theme, 26), _font(theme, 28)
    img = Image.new('RGBA', (BUG_W + PAD, BUG_H + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 6, BUG_W + 4, BUG_H + 6), 18, fill=pal['shadow'])
    d.rounded_rectangle((0, 0, BUG_W, BUG_H), 18, fill=pal['panel'])
    row_h = BUG_H // 2
    for i, side in enumerate(('away', 'home')):
        t = bug.get(side) or {}
        y = i * row_h
        fallback = (31, 111, 235, 255) if side == 'away' else (218, 54, 51, 255)
        d.rounded_rectangle((12, y + 12, 18, y + row_h - 12), 3,
                            fill=_team_color(t, fallback))
        d.text((32, y + row_h // 2), str(t.get('abbr', ''))[:4],
               font=f_abbr, fill=pal['text'], anchor='lm')
        d.text((210, y + row_h // 2), str(t.get('runs', 0)),
               font=f_runs, fill=pal['text'], anchor='mm')
    d.line((260, 14, 260, BUG_H - 14), fill=pal['off'], width=2)
    cx = 330
    if bug.get('finished'):
        d.text((cx, BUG_H // 2), 'FINAL', font=f_inn,
               fill=pal['accent'], anchor='mm')
    else:
        if show['inning']:
            half = '▲' if bug.get('half') == 'top' else '▼'
            d.text((cx, 34), f"{half} {bug.get('inning', 1)}",
                   font=f_inn, fill=pal['accent'], anchor='mm')
        if show['count']:
            d.text((cx, 72), f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}",
                   font=f_cnt, fill=pal['accent2'], anchor='mm')
        if show['outs']:
            _outs(d, cx - 26, 96, bug.get('outs'), pal, r=7, step=22)
        pc = (bug.get('pitcher') or {}).get('pc')
        if show['pitchcount'] and pc is not None:
            d.text((cx + 78, BUG_H - 24), f'P:{pc}', font=_font(theme, 18),
                   fill=pal['dim'], anchor='mm')
    if show['bases'] and not bug.get('finished'):
        _diamond(d, 470, BUG_H // 2, 22, bug.get('bases') or [], pal, gap=34)
    return img


TV_W, TV_HDR, TV_MAIN = 500, 78, 104


def _render_tv(bug, theme):
    """Network-telecast bug: batter/pitcher header, conference tabs, team
    color blocks + score boxes, inning arrows, count, bases, outs, pitch
    velocity (blank unless a radar reading is on the feed)."""
    pal, show = _palette(theme), theme['show']
    # Themed tv maps: red→accent, yellow→accent2, white→text.
    red, yellow = pal['accent'], pal['accent2']
    tv_bg = _blend(pal['bg'], (0, 0, 0, 255), 0.12)[:3] + (245,)
    tv_panel = pal['panel'][:3] + (245,)
    sep = _blend(tv_panel, pal['text'], 0.25)
    f_hdr, f_ab, f_sc = _font(theme, 22), _font(theme, 28), _font(theme, 34)
    f_big, f_inn = _font(theme, 32), _font(theme, 26)
    total_h = TV_HDR + TV_MAIN
    img = Image.new('RGBA', (TV_W + PAD, total_h + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bat, pit = bug.get('batter') or {}, bug.get('pitcher') or {}
    conf = bug.get('conf') or {}

    hx0, hx1 = 16, TV_W - 16
    d.rounded_rectangle((hx0 + 3, 4, hx1 + 3, TV_HDR + 4), 8,
                        fill=pal['shadow'])
    d.rounded_rectangle((hx0, 0, hx1, TV_HDR), 8, fill=tv_bg)
    slot = f"{bat.get('slot')}. " if bat.get('slot') else ''
    d.text((hx0 + 14, 20), f"{slot}{_last_name(bat.get('name'))}",
           font=f_hdr, fill=pal['text'], anchor='lm')
    d.text((hx1 - 14, 20), str(bat.get('line') or ''),
           font=f_hdr, fill=pal['text'], anchor='rm')
    d.line((hx0 + 12, TV_HDR // 2, hx1 - 12, TV_HDR // 2), fill=sep, width=2)
    d.text((hx0 + 14, TV_HDR - 20), _last_name(pit.get('name')),
           font=f_hdr, fill=pal['text'], anchor='lm')
    pc = pit.get('pc')
    if show['pitchcount'] and pc is not None:
        d.text((hx1 - 14, TV_HDR - 20), f'P: {pc}',
               font=f_hdr, fill=pal['text'], anchor='rm')

    my0 = TV_HDR
    d.rounded_rectangle((4, my0 + 5, TV_W + 4, my0 + TV_MAIN + 5), 10,
                        fill=pal['shadow'])
    d.rounded_rectangle((0, my0, TV_W, my0 + TV_MAIN), 10, fill=tv_panel)
    row_h = TV_MAIN // 2
    tabs_w, block_w, score_w = 22, 118, 52
    for i, side in enumerate(('away', 'home')):
        t = bug.get(side) or {}
        y = my0 + i * row_h
        left = conf.get(side)
        n = 3 if left is None else max(0, min(4, int(left)))
        d.rectangle((0, y, tabs_w, y + row_h), fill=tv_bg)
        for k in range(n):
            ty = y + row_h // 2 + (k - (n - 1) / 2) * 14
            d.rounded_rectangle((7, ty - 5, 15, ty + 5), 2, fill=pal['text'])
        fallback = (38, 41, 47, 255) if side == 'away' else (27, 47, 82, 255)
        d.rectangle((tabs_w, y, tabs_w + block_w, y + row_h),
                    fill=_team_color(t, fallback))
        d.text((tabs_w + 12, y + row_h // 2), str(t.get('abbr', ''))[:4],
               font=f_ab, fill=pal['text'], anchor='lm')
        d.rectangle((tabs_w + block_w, y, tabs_w + block_w + score_w,
                     y + row_h), fill=(255, 255, 255, 255))
        d.text((tabs_w + block_w + score_w // 2, y + row_h // 2),
               str(t.get('runs', 0)), font=f_sc, fill=(17, 17, 17, 255),
               anchor='mm')
    ix = tabs_w + block_w + score_w + 26
    cy = my0 + TV_MAIN // 2
    if bug.get('finished'):
        d.text((tabs_w + block_w + score_w +
                (TV_W - tabs_w - block_w - score_w) // 2, cy), 'FINAL',
               font=f_big, fill=yellow, anchor='mm')
        return img
    top = bug.get('half') == 'top'
    if show['inning']:
        d.polygon([(ix, cy - 36), (ix - 8, cy - 24), (ix + 8, cy - 24)],
                  fill=red if top else pal['off'])
        d.text((ix, cy), str(bug.get('inning', 1)), font=f_inn,
               fill=pal['text'], anchor='mm')
        d.polygon([(ix, cy + 36), (ix - 8, cy + 24), (ix + 8, cy + 24)],
                  fill=pal['off'] if top else red)
    d.line((ix + 20, my0 + 12, ix + 20, my0 + TV_MAIN - 12), fill=sep, width=1)
    sx = ix + 36
    if show['count']:
        d.text((sx, my0 + row_h // 2),
               f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}",
               font=f_big, fill=pal['text'], anchor='lm')
    if show['bases']:
        on = bug.get('bases') or [False, False, False]
        bx, by, sz = TV_W - 64, my0 + 26, 13
        spots = [(bx + 24, by + 10), (bx, by - 8), (bx - 24, by + 10)]
        for i, (px, py) in enumerate(spots):
            pts = [(px, py - sz), (px + sz, py), (px, py + sz), (px - sz, py)]
            if i < len(on) and on[i]:
                d.polygon(pts, fill=red)
            else:
                d.polygon(pts, outline=red, width=3)
    if show['outs']:
        for i in range(3):
            onq = i < (bug.get('outs') or 0)
            d.ellipse((sx + i * 20, my0 + TV_MAIN - 36,
                       sx + 14 + i * 20, my0 + TV_MAIN - 22),
                      fill=red if onq else pal['off'])
    velo = bug.get('velo')
    if velo is not None:
        d.text((TV_W - 24, my0 + TV_MAIN - 28), str(velo),
               font=f_big, fill=yellow, anchor='rm')
    return img


TVBOX_W, TVBOX_ROW, TVBOX_SIT = 340, 56, 118


def _render_tvbox(bug, theme):
    """Stacked 2-row corner box with a right-hand situation column and an
    accent underline across the bottom."""
    pal, show = _palette(theme), theme['show']
    f_ab, f_sc = _font(theme, 30), _font(theme, 36)
    f_sm, f_inn = _font(theme, 19), _font(theme, 24)
    h = TVBOX_ROW * 2 + 6                      # +6 accent underline
    img = Image.new('RGBA', (TVBOX_W + PAD, h + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 5, TVBOX_W + 4, h + 5), 8, fill=pal['shadow'])
    d.rounded_rectangle((0, 0, TVBOX_W, h), 8, fill=pal['panel'])
    team_w = TVBOX_W - TVBOX_SIT
    for i, side in enumerate(('away', 'home')):
        t = bug.get(side) or {}
        y = i * TVBOX_ROW
        fallback = (38, 41, 47, 255) if side == 'away' else (27, 47, 82, 255)
        d.rectangle((0, y + (0 if i else 4), 8, y + TVBOX_ROW),
                    fill=_team_color(t, fallback))
        d.text((22, y + TVBOX_ROW // 2), str(t.get('abbr', ''))[:4],
               font=f_ab, fill=pal['text'], anchor='lm')
        d.text((team_w - 26, y + TVBOX_ROW // 2), str(t.get('runs', 0)),
               font=f_sc, fill=pal['text'], anchor='mm')
        if i == 0:
            d.line((14, TVBOX_ROW, team_w - 10, TVBOX_ROW),
                   fill=pal['off'], width=2)
    # right situation column
    sx = team_w
    d.line((sx, 10, sx, h - 12), fill=pal['off'], width=2)
    cx = sx + TVBOX_SIT // 2
    if bug.get('finished'):
        d.text((cx, h // 2), 'FINAL', font=f_inn, fill=pal['accent'],
               anchor='mm')
    else:
        if show['inning']:
            half = '▲' if bug.get('half') == 'top' else '▼'
            d.text((cx, 18), f"{half}{bug.get('inning', 1)}", font=f_inn,
                   fill=pal['accent'], anchor='mm')
        if show['bases']:
            _diamond(d, cx, 52, 9, bug.get('bases') or [], pal, gap=14)
        if show['count']:
            txt = f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}"
            pc = (bug.get('pitcher') or {}).get('pc')
            if show['pitchcount'] and pc is not None:
                txt += f'  P{pc}'
            d.text((cx, 82), txt, font=f_sm, fill=pal['accent2'], anchor='mm')
        if show['outs']:
            _outs(d, cx - 25, 94, bug.get('outs'), pal, r=5, step=20)
    # accent underline
    d.rounded_rectangle((0, h - 6, TVBOX_W, h), 3, fill=pal['accent'])
    return img


BL_W, BL_H = 720, 64


def _render_bottomline(bug, theme):
    """Full-width lower bar: team blocks left, diamond + outs center,
    count + inning right, parallelogram accent separators."""
    pal, show = _palette(theme), theme['show']
    f_ab, f_sc = _font(theme, 26), _font(theme, 32)
    f_sm = _font(theme, 22)
    img = Image.new('RGBA', (BL_W + PAD, BL_H + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((4, 5, BL_W + 4, BL_H + 5), fill=pal['shadow'])
    d.rectangle((0, 0, BL_W, BL_H), fill=pal['panel'])
    cy = BL_H // 2

    def _slant(x, w=10):
        # parallelogram accent separator, leaning right
        d.polygon([(x + 6, 0), (x + 6 + w, 0), (x + w, BL_H), (x, BL_H)],
                  fill=pal['accent'])

    x = 0
    for side in ('away', 'home'):
        t = bug.get(side) or {}
        fallback = (31, 111, 235, 255) if side == 'away' else (218, 54, 51, 255)
        d.rectangle((x, 0, x + 6, BL_H), fill=_team_color(t, fallback))
        d.text((x + 18, cy), str(t.get('abbr', ''))[:4], font=f_ab,
               fill=pal['text'], anchor='lm')
        d.text((x + 116, cy), str(t.get('runs', 0)), font=f_sc,
               fill=pal['text'], anchor='mm')
        x += 148
    _slant(x)
    if bug.get('finished'):
        d.text((BL_W - 160, cy), 'FINAL', font=f_sc, fill=pal['accent'],
               anchor='mm')
        return img
    # center: diamond + outs
    x += 42
    if show['bases']:
        _diamond(d, x + 24, cy, 10, bug.get('bases') or [], pal, gap=16)
    x += 76
    if show['outs']:
        _outs(d, x, cy - 7, bug.get('outs'), pal, r=6, step=20)
        d.text((x + 62, cy), 'OUT', font=_font(theme, 16), fill=pal['dim'],
               anchor='lm')
    x += 108
    _slant(x)
    # right: count + inning
    x += 34
    if show['count']:
        d.text((x, cy), f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}",
               font=f_sm, fill=pal['accent2'], anchor='lm')
        x += 66
    if show['inning']:
        half = '▲' if bug.get('half') == 'top' else '▼'
        d.text((x, cy), f"{half} {bug.get('inning', 1)}", font=f_sm,
               fill=pal['accent'], anchor='lm')
        x += 66
    pc = (bug.get('pitcher') or {}).get('pc')
    if show['pitchcount'] and pc is not None:
        d.text((BL_W - 14, cy), f'P:{pc}', font=_font(theme, 18),
               fill=pal['dim'], anchor='rm')
    return img


LT_W, LT_TOP, LT_MAIN = 620, 30, 84


def _render_lowerthird(bug, theme):
    """Elongated rounded center plate: teams left/right of a center divider,
    small strip on top for inning/count/outs."""
    pal, show = _palette(theme), theme['show']
    f_ab, f_sc = _font(theme, 30), _font(theme, 40)
    f_top = _font(theme, 18)
    h = LT_TOP + LT_MAIN
    img = Image.new('RGBA', (LT_W + PAD, h + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # top strip (narrower, centered)
    tx0, tx1 = LT_W // 2 - 150, LT_W // 2 + 150
    d.rounded_rectangle((tx0, 0, tx1, LT_TOP + 12), 8,
                        fill=_blend(pal['bg'], (0, 0, 0, 255), 0.25)[:3] + (245,))
    bits = []
    if bug.get('finished'):
        bits.append('FINAL')
    else:
        if show['inning']:
            half = '▲' if bug.get('half') == 'top' else '▼'
            bits.append(f"{half} {bug.get('inning', 1)}")
        if show['count']:
            bits.append(f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}")
        if show['outs']:
            bits.append(f"{bug.get('outs', 0)} OUT")
        pc = (bug.get('pitcher') or {}).get('pc')
        if show['pitchcount'] and pc is not None:
            bits.append(f'P:{pc}')
    d.text((LT_W // 2, LT_TOP // 2 + 3), '   '.join(bits), font=f_top,
           fill=pal['accent2'], anchor='mm')
    # main plate
    my0 = LT_TOP
    d.rounded_rectangle((4, my0 + 5, LT_W + 4, my0 + LT_MAIN + 5), 22,
                        fill=pal['shadow'])
    d.rounded_rectangle((0, my0, LT_W, my0 + LT_MAIN), 22, fill=pal['panel'])
    cy = my0 + LT_MAIN // 2
    mid = LT_W // 2
    for i, side in enumerate(('away', 'home')):
        t = bug.get(side) or {}
        fallback = (31, 111, 235, 255) if side == 'away' else (218, 54, 51, 255)
        stripe = _team_color(t, fallback)
        if i == 0:      # away on the left half
            d.rounded_rectangle((20, my0 + 16, 26, my0 + LT_MAIN - 16), 3,
                                fill=stripe)
            d.text((40, cy), str(t.get('abbr', ''))[:4], font=f_ab,
                   fill=pal['text'], anchor='lm')
            d.text((mid - 46, cy), str(t.get('runs', 0)), font=f_sc,
                   fill=pal['text'], anchor='mm')
        else:           # home on the right half, mirrored
            d.rounded_rectangle((LT_W - 26, my0 + 16, LT_W - 20,
                                 my0 + LT_MAIN - 16), 3, fill=stripe)
            d.text((LT_W - 40, cy), str(t.get('abbr', ''))[:4], font=f_ab,
                   fill=pal['text'], anchor='rm')
            d.text((mid + 46, cy), str(t.get('runs', 0)), font=f_sc,
                   fill=pal['text'], anchor='mm')
    d.rounded_rectangle((mid - 2, my0 + 14, mid + 2, my0 + LT_MAIN - 14), 2,
                        fill=pal['accent'])
    if show['bases'] and not bug.get('finished'):
        _diamond(d, mid, my0 + LT_MAIN - 16, 6, bug.get('bases') or [],
                 pal, gap=10)
    return img


SS_W, SS_ROW, SS_SIT = 190, 62, 128


def _render_sidestack(bug, theme):
    """Vertical panel: two stacked team rows, situation column underneath."""
    pal, show = _palette(theme), theme['show']
    f_ab, f_sc = _font(theme, 26), _font(theme, 32)
    f_sm, f_inn = _font(theme, 20), _font(theme, 24)
    h = SS_ROW * 2 + SS_SIT
    img = Image.new('RGBA', (SS_W + PAD, h + PAD), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 5, SS_W + 4, h + 5), 12, fill=pal['shadow'])
    d.rounded_rectangle((0, 0, SS_W, h), 12, fill=pal['panel'])
    for i, side in enumerate(('away', 'home')):
        t = bug.get(side) or {}
        y = i * SS_ROW
        fallback = (31, 111, 235, 255) if side == 'away' else (218, 54, 51, 255)
        d.rounded_rectangle((10, y + 10, 16, y + SS_ROW - 10), 3,
                            fill=_team_color(t, fallback))
        d.text((28, y + SS_ROW // 2), str(t.get('abbr', ''))[:4],
               font=f_ab, fill=pal['text'], anchor='lm')
        d.text((SS_W - 34, y + SS_ROW // 2), str(t.get('runs', 0)),
               font=f_sc, fill=pal['text'], anchor='mm')
    d.line((12, SS_ROW * 2, SS_W - 12, SS_ROW * 2), fill=pal['accent'],
           width=3)
    sy = SS_ROW * 2
    cx = SS_W // 2
    if bug.get('finished'):
        d.text((cx, sy + SS_SIT // 2), 'FINAL', font=f_inn,
               fill=pal['accent'], anchor='mm')
        return img
    if show['inning']:
        half = '▲' if bug.get('half') == 'top' else '▼'
        d.text((cx, sy + 20), f"{half} {bug.get('inning', 1)}", font=f_inn,
               fill=pal['accent'], anchor='mm')
    if show['bases']:
        _diamond(d, cx, sy + 56, 10, bug.get('bases') or [], pal, gap=16)
    if show['count']:
        txt = f"{bug.get('balls', 0)}-{bug.get('strikes', 0)}"
        pc = (bug.get('pitcher') or {}).get('pc')
        if show['pitchcount'] and pc is not None:
            txt += f'  P{pc}'
        d.text((cx, sy + 90), txt, font=f_sm, fill=pal['accent2'],
               anchor='mm')
    if show['outs']:
        _outs(d, cx - 28, sy + 104, bug.get('outs'), pal, r=6, step=22)
    return img


RENDERERS = {
    'bar': _render_bar,
    'tv': _render_tv,
    'tvbox': _render_tvbox,
    'bottomline': _render_bottomline,
    'lowerthird': _render_lowerthird,
    'sidestack': _render_sidestack,
}


def render_bug(bug, pos='bl', scale=1.0, layout='bar', theme=None):
    """Render one feed frame to the full WxH transparent canvas."""
    theme = theme or parse_theme({})
    fn = RENDERERS.get(layout, _render_bar)
    return _place(fn(bug, theme), pos, scale, (theme or {}).get('xy'))


def resolve_look(bug, default_pos='bl', default_layout='bar'):
    """Work out (layout, pos, scale, bandwidth, theme) for a feed frame.
    Theme block (v2) wins; else the legacy `ndi` config block; else the
    passed defaults (CLI flags). Bandwidth stays on the ndi block either
    way — it's a transport knob, not a look."""
    ndi_cfg = bug.get('ndi') or {}
    bw = 0
    try:
        bw = max(0, min(len(BANDWIDTH_LEVELS) - 1,
                        int(ndi_cfg.get('bandwidth', 0) or 0)))
    except (TypeError, ValueError):
        pass
    theme = parse_theme(bug.get('theme'))
    if theme is not None:
        return theme['layout'], theme['pos'], theme['scale'], bw, theme
    pos = ndi_cfg.get('pos', default_pos)
    layout = ndi_cfg.get('layout', default_layout)
    if layout not in RENDERERS:
        layout = 'bar'
    try:
        scale = float(ndi_cfg.get('scale', 1.0))
    except (TypeError, ValueError):
        scale = 1.0
    return layout, pos, scale, bw, None


# ── sender ────────────────────────────────────────────────────────────────────

PLACEHOLDER_BUG = {'away': {'abbr': 'AWY'}, 'home': {'abbr': 'HOM'},
                   'inning': 1, 'half': 'top'}


class Sender:
    """Poll the feed, keep the latest rendered frame, publish it over NDI
    and HTTP (and to PNG files in fake mode). `feed` may be empty at start
    and repointed live via set_feed() — that's how cloud assignment hopping
    swaps a single encoder between teams with no restart."""

    def __init__(self, feed=None, name='PlayCall Bug', fps=4, poll=1.0,
                 pos='bl', layout='bar', port=8765, fake=None, fake_dir=None):
        self.feed = feed
        self.name = name
        self.base_fps = fps
        self.fps = fps
        self.poll = poll
        self.default_pos = pos
        self.default_layout = layout
        self.port = port
        self.fake = os.environ.get('SCOREBUG_FAKE') == '1' if fake is None \
            else fake
        self.fake_dir = fake_dir or os.environ.get('SCOREBUG_FAKE_DIR', '.')
        self.pos, self.layout, self.scale, self.bandwidth = pos, layout, 1.0, 0
        self.last_seq = None
        self.last_sig = None
        self.lock = threading.Lock()
        self.running = True
        self.ndi_send = None
        self.ndi_frame = None
        self.img = self._render(PLACEHOLDER_BUG, None)

    def set_feed(self, url):
        self.feed = url
        self.last_seq = None      # force a re-render from the new feed

    def _render(self, bug, theme):
        img = render_bug(bug, self.pos, self.scale, self.layout, theme)
        rs = BANDWIDTH_LEVELS[self.bandwidth]['render_scale']
        if rs != 1.0:
            img = img.resize((max(1, round(W * rs)), max(1, round(H * rs))),
                             Image.LANCZOS)
        return img

    def fetch(self):
        with urllib.request.urlopen(self.feed, timeout=6) as r:
            return json.loads(r.read().decode())

    def poll_once(self):
        """One poll cycle; separated from the loop for testability."""
        if not self.feed:
            return False
        bug = self.fetch()
        layout, pos, scale, bw, theme = resolve_look(
            bug, self.default_pos, self.default_layout)
        theme_sig = json.dumps(theme, sort_keys=True) if theme else None
        sig = (pos, layout, scale, bw, theme_sig)
        seq = bug.get('seq')
        # Re-render on a score change OR any look change — without the
        # second half, dragging the bug / editing the theme mid-inning
        # would silently do nothing until the next play.
        if seq == self.last_seq and sig == self.last_sig:
            return False
        self.last_seq, self.last_sig = seq, sig
        self.pos, self.layout, self.scale, self.bandwidth = pos, layout, \
            scale, bw
        level = BANDWIDTH_LEVELS[self.bandwidth]
        self.fps = level['fps'] or self.base_fps
        img = self._render(bug, theme)
        with self.lock:
            self.img = img
        print(f"frame {seq}: "
              f"{(bug.get('away') or {}).get('runs', 0)}-"
              f"{(bug.get('home') or {}).get('runs', 0)} "
              f"{bug.get('half', '')} {bug.get('inning', '')} "
              f"[{self.pos}/{self.layout}/{self.scale}x/"
              f"{level['label']}@{self.fps}fps]")
        return True

    def poll_loop(self):
        while self.running:
            try:
                self.poll_once()
            except Exception as e:
                print(f'feed error (retrying): {e}', file=sys.stderr)
            time.sleep(self.poll)

    # ── NDI output (or PNG files in fake mode) ───────────────────────────────
    def ndi_init(self):
        if _NDI is None:
            print('ndi-python not installed — MJPEG/PNG fallback only.\n'
                  '  pip install ndi-python   to enable the real NDI source.')
            return False
        if not _NDI.initialize():
            print('NDI runtime failed to initialize — MJPEG fallback only.',
                  file=sys.stderr)
            return False
        st = _NDI.SendCreate()
        st.ndi_name = self.name
        self.ndi_send = _NDI.send_create(st)
        self.ndi_frame = _NDI.VideoFrameV2()
        return self.ndi_send is not None

    def ndi_loop(self):
        if self.fake:
            return self._fake_loop()
        if not self.ndi_init():
            return
        import numpy as np
        while self.running:
            with self.lock:
                rgba = self.img
            arr = np.array(rgba, dtype=np.uint8)          # H×W×4 RGBA
            bgra = arr[..., [2, 1, 0, 3]].copy()
            f = self.ndi_frame
            f.data = bgra
            f.FourCC = _NDI.FOURCC_VIDEO_TYPE_BGRA
            _NDI.send_send_video_v2(self.ndi_send, f)
            time.sleep(1.0 / max(1, self.fps))
        _NDI.send_destroy(self.ndi_send)
        _NDI.destroy()

    def _fake_loop(self):
        out = os.path.join(self.fake_dir, 'bug.png')
        while self.running:
            self.write_png(out)
            time.sleep(1.0 / max(1, self.fps))

    def write_png(self, path):
        with self.lock:
            img = self.img
        tmp = path + '.tmp'
        img.save(tmp, 'PNG')
        os.replace(tmp, path)

    # ── HTTP fallback (always on): /bug.png + /bug.mjpg ──────────────────────
    def http_serve(self):
        sender = self

        class BugHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith('/bug.png'):
                    with sender.lock:
                        buf = io.BytesIO()
                        sender.img.save(buf, 'PNG')
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(buf.getvalue())
                    return
                if self.path.startswith('/bug.mjpg'):
                    self.send_response(200)
                    self.send_header('Content-Type',
                                     'multipart/x-mixed-replace; boundary=f')
                    self.end_headers()
                    try:
                        while sender.running:
                            with sender.lock:
                                buf = io.BytesIO()
                                sender.img.convert('RGB').save(
                                    buf, 'JPEG', quality=85)
                            self.wfile.write(
                                b'--f\r\nContent-Type: image/jpeg\r\n\r\n')
                            self.wfile.write(buf.getvalue())
                            self.wfile.write(b'\r\n')
                            time.sleep(1.0 / max(1, sender.fps))
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    return
                self.send_response(404)
                self.end_headers()

        srv = ThreadingHTTPServer(('0.0.0.0', self.port), BugHandler)
        print(f'HTTP fallback: http://0.0.0.0:{self.port}/bug.png '
              f'and /bug.mjpg')
        while self.running:
            srv.handle_request()

    def start_threads(self):
        threads = [threading.Thread(target=self.poll_loop, daemon=True),
                   threading.Thread(target=self.ndi_loop, daemon=True),
                   threading.Thread(target=self.http_serve, daemon=True)]
        for t in threads:
            t.start()
        return threads


def main():
    ap = argparse.ArgumentParser(description='PlayCall scorebug sender')
    ap.add_argument('--feed', required=True,
                    help='the /api/sk/bug/<token>.json URL')
    ap.add_argument('--name', default='PlayCall Bug', help='NDI source name')
    ap.add_argument('--fps', type=int, default=4)
    ap.add_argument('--poll', type=float, default=1.0)
    ap.add_argument('--pos', default='bl', choices=list(POSITIONS))
    ap.add_argument('--layout', default='bar', choices=list(LAYOUTS))
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args()

    s = Sender(feed=args.feed, name=args.name, fps=args.fps, poll=args.poll,
               pos=args.pos, layout=args.layout, port=args.port)
    s.start_threads()

    def stop(*a):
        s.running = False
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
