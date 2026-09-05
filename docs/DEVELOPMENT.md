# Developing on a laptop

Everything imports and runs without a Pi, without root and without
touching `/etc` — hardware calls are isolated in
`encoder/system.py` and skipped in fake mode.

## Requirements

Python 3.11+ and Flask (apt- or pip-installable). No other dependencies;
everything else is stdlib.

```bash
sudo apt install python3-flask    # or: pip install flask
```

## Run the whole stack in fake mode

```bash
cd ndi-encoder
export SCOREBUG_FAKE=1                        # no AP, no systemctl
export PLAYCALL_ENCODER_DIR=/tmp/enc-config   # config sandbox (not /etc)
export PLAYCALL_ENCODER_STATE=/tmp/enc-state
mkdir -p /tmp/enc-config /tmp/enc-state
python3 -m encoder
```

* Settings web app: `http://localhost:8080` — the PIN is in the generated
  `/tmp/enc-config/config.json`.
* A dev config is fabricated automatically on first run.

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
  provisioning.py   setup hotspot + captive portal + network persistence
  config.py         atomic config.json + preconfig + mediamtx templating
  cloud_link.py     assignment polling + heartbeats + version check
  youtube_push.py   ffmpeg push leg with -progress stats
  web.py            PIN-gated settings/status app (:8080)
  system.py         every hardware/OS touchpoint (monkeypatch here)
```
