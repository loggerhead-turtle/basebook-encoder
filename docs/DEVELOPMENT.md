# Developing on a laptop

Everything imports and runs without a Pi, without NDI, without root and
without touching `/etc` — hardware calls are isolated in
`encoder/system.py` and skipped in fake mode.

## Requirements

Python 3.11+, Pillow, Flask (both apt- or pip-installable). No other
dependencies; everything else is stdlib.

```bash
sudo apt install python3-pil python3-flask    # or: pip install pillow flask
```

## Run the whole stack in fake mode

```bash
cd ndi-encoder
export SCOREBUG_FAKE=1                        # no AP, no systemctl, no NDI
export PLAYCALL_ENCODER_DIR=/tmp/enc-config   # config sandbox (not /etc)
export PLAYCALL_ENCODER_STATE=/tmp/enc-state
export SCOREBUG_FAKE_DIR=/tmp/enc-frames      # rendered PNGs land here
mkdir -p /tmp/enc-config /tmp/enc-state /tmp/enc-frames
python3 -m encoder
```

* Scorebug renders to `/tmp/enc-frames/bug.png` (and still serves
  `http://localhost:8765/bug.png` + `/bug.mjpg`).
* Settings web app: `http://localhost:8080` — the PIN is in the generated
  `/tmp/enc-config/config.json`.
* A dev config is fabricated automatically on first run.

## Render layouts standalone

```python
from encoder import scorebug

bug = {
    'away': {'abbr': 'WAR', 'runs': 4, 'color': '#1f6feb'},
    'home': {'abbr': 'SID', 'runs': 2, 'color': '#da3633'},
    'inning': 5, 'half': 'top', 'balls': 3, 'strikes': 2, 'outs': 2,
    'bases': [True, False, True],
    'theme': {'version': 2, 'layout': 'bottomline', 'pos': 'bc',
              'colors': {'bg': '#101418', 'accent': '#e0352b'}},
}
layout, pos, scale, bw, theme = scorebug.resolve_look(bug)
scorebug.render_bug(bug, pos, scale, layout, theme).save('/tmp/bug.png')
```

## Point at a real feed

```bash
SCOREBUG_FAKE=1 python3 -m encoder.scorebug \
    --feed https://playsigns.net/api/sk/bug/<token>.json --layout tvbox
```

## Tests

```bash
python3 -m pytest ndi-encoder/tests -q
```

Tests monkeypatch `encoder.system.run` (and friends) so nothing shells
out; network calls in `cloud_link` are injected via the `http=` parameter.

## Repo layout

```
encoder/
  __main__.py       boot decision + thread supervisor
  scorebug.py       renderer (6 layout families, theme spec v2) + NDI/HTTP sender
  provisioning.py   setup hotspot + captive portal + network persistence
  config.py         atomic config.json + preconfig + mediamtx templating
  cloud_link.py     assignment polling + heartbeats + version check
  youtube_push.py   ffmpeg push leg with -progress stats
  web.py            PIN-gated settings/status app (:8080)
  system.py         every hardware/OS touchpoint (monkeypatch here)
```
