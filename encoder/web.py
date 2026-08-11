#!/usr/bin/env python3
"""Local settings/status app — http://playcall-encoder.local:8080.

Reached one of two ways: the site's ⚙ Settings button, which signs the
coach in with a one-time nonce, or directly at this address with the
box's recovery PIN (stored in config) — the fallback for when the cloud
link is the broken thing.
Status: camera-ingest state, YouTube push state (+ cloud assignment when
paired), a log viewer, and a "Copy logs for AI help" button that copies a
structured plaintext bundle (config summary minus secrets + last 200 log
lines + ffmpeg stderr tail). Settings: Wi-Fi networks, YouTube key,
bandwidth, rotate local ingest key, factory reset.
"""

import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path

from flask import (Flask, redirect, render_template_string, request,
                   session, url_for)

from . import __version__, config, provisioning, system

log = logging.getLogger('web')

WEB_PORT = 8080

# PIN brute-force protection: a global (all clients) failure counter with an
# exponential lockout. 5 straight failures → 60 s lock, doubling per further
# failure, capped at 15 min; any success resets. Plus a per-request damper.
PIN_FAIL_THRESHOLD = 5
PIN_LOCKOUT_BASE = 60          # seconds
PIN_LOCKOUT_MAX = 15 * 60
PIN_FAIL_DELAY = 1.0           # per-failed-request sleep (damper)

CSS = provisioning.PORTAL_CSS + """
.wrap{width:100%;max-width:680px}
.row{display:flex;gap:.6rem;align-items:center;margin:.35rem 0}
.dot{width:.7rem;height:.7rem;border-radius:50%;background:#444}
.dot.ok{background:#10b981}.dot.warn{background:#f5b301}
.k{color:#8b949e;font-size:.85rem;min-width:9rem}
.v{font-size:.9rem}
pre{background:#0d1117;border:1px solid #30363d;border-radius:6px;
    padding:.7rem;font-size:.72rem;overflow-x:auto;max-height:340px;
    color:#8b949e;white-space:pre-wrap}
.card{max-width:none;margin-bottom:1rem}
.btn2{display:inline-block;padding:.5rem .9rem;background:#21262d;
      color:#eee;border:1px solid #30363d;border-radius:6px;font-size:.85rem;
      cursor:pointer;text-decoration:none;margin:.2rem .2rem .2rem 0}
.btn2.danger{border-color:#c62828;color:#ef9a9a}
.netrow{display:flex;justify-content:space-between;align-items:center;
        padding:.35rem 0;border-bottom:1px solid #21262d;font-size:.9rem}
table{width:100%;font-size:.85rem}
body{justify-content:flex-start;padding-top:2rem}
"""

LOGIN_PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Encoder settings</title><style>""" + CSS + """</style></head><body>
<div class="logo">PlayCall Encoder</div><div class="sub">Encoder settings</div>
<div class="card" style="max-width:340px">
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <form method="post" action="/login">
    <label>Recovery PIN</label>
    <input type="password" name="pin" inputmode="numeric" autofocus
           placeholder="6-digit recovery PIN">
    <input type="hidden" name="next" value="{{ next or '' }}">
    <button class="btn" type="submit">Unlock</button>
  </form>
  <p class="hint" style="margin-top:.75rem">You usually don't need this.
     On basebook.org, Score Bug Studio &rarr; Encoders &rarr;
     <b>&#9881; Settings</b> opens this page already signed in.</p>
  <p class="hint">Need the digits anyway? The same card has a
     <b>&#128273; Recovery PIN</b> button. (Or read them out of
     /etc/playcall-encoder/config.json on the SD card.)</p>
</div></body></html>"""

PAIR_PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair encoder</title><style>""" + CSS + """</style></head><body>
<div class="wrap">
<div class="logo">PlayCall Encoder</div><div class="sub">Cloud pairing</div>
{% if done %}
<div class="card">
  <div class="step">Paired</div>
  <h2>✓ This encoder is paired to {{ host }}</h2>
  <p class="hint">Within a few seconds it starts checking in. From here
     everything is driven from PlayCall:</p>
  <ol style="font-size:.85rem;color:#8b949e;line-height:1.8;padding-left:1.2rem">
    <li>In PlayCall, open <b>Score Bug Studio → Encoders</b> — this box now
        appears in the list for your team.</li>
    <li>Connect each team's YouTube in PlayCall (team stream settings).
        When this box streams a team, the cloud hands it that team's
        channel automatically — Warriors games go to the Warriors channel,
        Provo games to Provo's.</li>
    <li>Use <b>Stream here</b> on a team's Encoders card to pin this box
        to that team, or leave it on auto-follow.</li>
  </ol>
  <a class="btn2" href="/">Back to settings</a>
</div>
{% else %}
<div class="card">
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <div class="step">Confirm pairing</div>
  <h2>Pair this encoder to PlayCall?</h2>
  <p class="hint">Cloud: <b>{{ host }}</b>{% if current %}<br>
     ⚠ This box is currently paired to <b>{{ current }}</b> — pairing
     replaces that.{% endif %}</p>
  <p class="hint">Pairing lets your PlayCall teams point this box at their
     games and YouTube channels.</p>
  <form method="post" action="/pair">
    <input type="hidden" name="cloud" value="{{ cloud }}">
    <input type="hidden" name="key" value="{{ key }}">
    <button class="btn" type="submit">Pair this encoder</button>
  </form>
  <a class="btn2" href="/" style="margin-top:.6rem">Cancel</a>
</div>
{% endif %}
</div></body></html>"""

STATUS_PAGE = """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Encoder settings</title><style>""" + CSS + """</style></head><body>
<div class="wrap">
<div class="logo">PlayCall Encoder</div>
<div class="sub">v{{ version }}{% if latest and latest != version %}
  · update {{ latest }} available{% endif %}</div>

{% if latest and latest != version %}
<div class="card">
  <h2>Update available</h2>
  <p class="hint">v{{ version }} → v{{ latest }}. Downloads the new software
    and restarts the encoder — the stream and this page blip for a few
    seconds. Your settings and pairing are kept.</p>
  <form method="post" action="/update"
        onsubmit="return confirm('Download v{{ latest }} and restart the encoder now?')">
    <button class="btn" type="submit">&#11015; Update now</button>
  </form>
</div>
{% endif %}

<div class="card">
  <h2>Status</h2>
  <div class="row"><span class="dot {{ 'ok' if ingest.connected }}"></span>
    <span class="k">Camera ingest</span>
    <span class="v">{{ 'Receiving' if ingest.connected else 'Waiting for camera' }}
      {% if ingest.kbps %}· {{ ingest.kbps }} kbps{% endif %}</span></div>
  <div class="row"><span class="dot {{ 'ok' if push.connected }}"></span>
    <span class="k">YouTube push</span>
    <span class="v">{{ 'Pushing' if push.connected else 'Not pushing' }}
      {% if push.kbps %}· {{ push.kbps }} kbps{% endif %}
      {% if push.reconnects_5m %}· {{ push.reconnects_5m }} reconnects/5m{% endif %}
    </span></div>
  {% if assignment %}
  <div class="row"><span class="dot {{ 'ok' if assignment.assigned }}"></span>
    <span class="k">Cloud assignment</span>
    <span class="v">{{ assignment.team_name or 'Unassigned' }}
      {% if assignment.game_id %}· game {{ assignment.game_id }}{% endif %}</span></div>
  {% endif %}
  <div class="row"><span class="dot ok"></span><span class="k">This encoder</span>
    <span class="v">{{ hostname }}.local · {{ ip or 'no LAN IP' }}
      · CPU {{ cpu }}%{% if temp %} · {{ temp }}°C{% endif %}</span></div>
  {% if msg %}<div class="hint" style="color:#4ade80">✅ {{ msg }}</div>{% endif %}
  {% if err %}<div class="alert">⚠ {{ err }}</div>{% endif %}
</div>

<div class="card">
  <h2>Camera app RTMP URL</h2>
  <p class="hint">Paste into Mevo / Larix / OBS as a Custom RTMP destination:</p>
  {% for u in rtmp_urls %}<div class="url">{{ u }}</div>{% endfor %}
</div>

<div class="card">
  <h2>Wi-Fi networks</h2>
  {% if not managed %}
  <p class="hint">This box uses its own network setup (Ethernet, Speedify
     bonding, or a tether that was already configured) — PlayCall doesn't
     manage its Wi-Fi and will never change it. Manage connections with
     your own tools (Speedify dashboard, nmcli, raspi-config).</p>
  {% else %}
  {% for n in networks %}
  <div class="netrow"><span>{{ n.ssid }}
      <span class="hint">({{ n.label }}, priority {{ n.priority }})</span></span>
    <form method="post" action="/networks/remove" style="margin:0">
      <input type="hidden" name="ssid" value="{{ n.ssid }}">
      <button class="btn2 danger" type="submit">Remove</button></form>
  </div>
  {% endfor %}
  <form method="post" action="/networks/add">
    <label>Add network</label>
    <input type="text" name="ssid" placeholder="Network name">
    <input type="password" name="psk" placeholder="Password"
           style="margin-top:.4rem">
    <select name="label" style="margin-top:.4rem">
      <option value="gameday">Game-day network</option>
      <option value="home">Home network</option>
    </select>
    <button class="btn" type="submit">Add network</button>
  </form>
  {% endif %}
</div>

<div class="card">
  <h2>YouTube</h2>
  <form method="post" action="/youtube">
    <label>Stream key or full RTMP URL</label>
    <input type="text" name="youtube" placeholder="{{ yt_placeholder }}">
    <button class="btn" type="submit">Save YouTube key</button>
  </form>
</div>

<div class="card">
  <h2>Recording retention</h2>
  <p class="hint">How long the rolling game recording (the source of every
    clip) stays on this box before pruning. 4 Mbps &asymp; 1.8 GB/h — keep
    12 h on an SD card; 72 h wants NVMe. Survives updates.</p>
  <form method="post" action="/retention">
    <select name="hours">
      {% for h in [12, 24, 48, 72] %}
      <option value="{{ h }}" {{ 'selected' if h == record_hours }}>{{ h }} hours</option>
      {% endfor %}
    </select>
    <button class="btn" type="submit">Save retention</button>
  </form>
</div>

<div class="card">
  <h2>Scorebug bandwidth</h2>
  <form method="post" action="/bandwidth">
    <select name="bandwidth">
      {% for lvl in bandwidth_levels %}
      <option value="{{ loop.index0 }}"
        {{ 'selected' if loop.index0 == bandwidth }}>{{ lvl.label }}</option>
      {% endfor %}
    </select>
    <button class="btn" type="submit">Save</button>
  </form>
</div>

<div class="card">
  <h2>Logs</h2>
  <button class="btn2" onclick="copyBundle()">&#128203; Copy logs for AI help</button>
  <a class="btn2" href="/logs">Refresh</a>
  <pre id="logs">{{ logs }}</pre>
</div>

<div class="card">
  <h2>Recovery PIN</h2>
  <p class="hint">A fallback, not the front door: the normal way in is
    Score Bug Studio → Encoders → ⚙ Settings, which signs you in with one
    click and needs no PIN. This one is for reaching the box directly at
    its own address — which is exactly what you do when the cloud link is
    the thing that's broken. Pick something you'll remember, 4–32
    characters.</p>
  <form method="post" action="/pin">
    <input type="password" name="pin" placeholder="New PIN" required>
    <input type="password" name="pin2" placeholder="Repeat it" required>
    <button class="btn2" type="submit">Change PIN</button>
  </form>
</div>

<div class="card">
  <h2>Maintenance</h2>
  <form method="post" action="/update" style="display:inline"
        onsubmit="return confirm('Re-download the encoder software and restart? The stream blips for a few seconds.')">
    <button class="btn2" type="submit">Update software</button></form>
  <form method="post" action="/rotate-key" style="display:inline"
        onsubmit="return confirm('Rotate the local ingest key? Your camera app RTMP URL will change.')">
    <button class="btn2" type="submit">Rotate ingest key</button></form>
  <form method="post" action="/factory-reset" style="display:inline"
        onsubmit="return confirm('Erase ALL settings and return to first-time setup?')">
    <button class="btn2 danger" type="submit">Factory reset</button></form>
  <a class="btn2" href="/logout">Lock</a>
</div>
</div>
<script>
async function copyBundle(){
  const r = await fetch('/logs/bundle'); const t = await r.text();
  /* This page is served over plain LAN http, where navigator.clipboard
     does not exist (secure contexts only) — so the "modern" path always
     failed and every phone got bounced to a select-all page. The hidden
     textarea + execCommand path still copies fine on http. */
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(t);
          alert('Copied — paste it into your support chat.'); return; }
    catch(e){}
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.setAttribute('readonly','');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, t.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) { alert('Copied — paste it into your support chat.'); return; }
  } catch(e){}
  window.open('/logs/bundle','_blank');   /* last resort: the old page */
}
</script>
</body></html>"""


def log_bundle(cloud=None, lines=200):
    """Structured plaintext support bundle: redacted config + recent service
    logs + the ffmpeg stderr tail. Every free-text section (journal, ffmpeg
    stderr, live status) is scrubbed of stream keys / RTMP push URLs via
    config.redact_text before it enters the bundle — safe to paste
    anywhere."""
    cfg = config.load()

    def red(s):
        return config.redact_text(s, cfg)

    parts = [
        'PLAYCALL ENCODER SUPPORT BUNDLE',
        f'version: {__version__}',
        f'time: {time.strftime("%Y-%m-%d %H:%M:%S %z")}',
        f'host: {system.hostname()} ip: {system.lan_ip()}',
        f'cpu: {system.cpu_percent()}% temp: {system.cpu_temp()}',
        '',
        '── config (secrets redacted) ──',
        json.dumps(config.redacted(cfg), indent=2),
        '',
    ]
    if cloud is not None:
        parts += ['── live status ──',
                  red(json.dumps({'ingest': cloud.ingest_status(),
                                  'push': cloud.push_status(),
                                  'assignment': cloud.assignment}, indent=2)),
                  '']
    # Clip-cutter health: which mode runs it, what it thinks it's doing,
    # and whether there is even a recording to cut from. "The clips
    # never uploaded" was undiagnosable without these three facts.
    try:
        unit = system.run(['systemctl', 'is-active',
                           'playcall-encoder-clipper'])
        unit_state = (unit.stdout or '').strip() or 'unknown'
    except Exception:
        unit_state = 'unknown'
    try:
        clips = (config.state_dir() / 'clips.json').read_text()
    except OSError:
        clips = '(no clips.json — no clipper has ever written status)'
    seg_line = '(no segments dir)'
    try:
        segs = sorted(Path('/var/lib/playcall-encoder/segments').rglob('*'),
                      key=lambda p: p.stat().st_mtime)
        files = [p for p in segs if p.is_file()]
        if files:
            age = int(time.time() - files[-1].stat().st_mtime)
            seg_line = f'{len(files)} segment file(s), newest {age}s old'
        else:
            seg_line = '0 segment files — nothing recorded yet'
    except OSError:
        pass
    parts += ['── clips ──',
              f'clipper unit: {unit_state}',
              f'status: {red(clips)}',
              f'recordings: {seg_line}',
              '']
    parts += [f'── last {lines} service log lines ──',
              *config.redact_lines(system.journal_tail(lines), cfg), '']
    try:
        push = json.loads((config.state_dir() / 'push.json').read_text())
        parts += ['── ffmpeg stderr tail ──',
                  *[red(l) for l in push.get('stderr_tail', [])]]
    except (OSError, ValueError):
        parts += ['── ffmpeg stderr tail ──', '(no push status file)']
    return '\n'.join(parts) + '\n'


def _session_secret():
    """A signing secret that SURVIVES restarts. os.urandom-per-boot signed
    every coach out whenever the service restarted — which is exactly what
    a self-update does, so 'update, then change the PIN' bounced through
    login and died on a 405. Stored beside the config (0600, never in the
    support bundle); per-process fallback when the dir isn't writable."""
    path = config.config_dir() / 'web_secret'
    try:
        data = path.read_bytes()
        if len(data) >= 24:
            return data
    except OSError:
        pass
    data = os.urandom(24)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
    except OSError:
        pass                       # dev checkout: sessions die with process
    return data


def create_app(cloud=None, sender=None):
    app = Flask(__name__)
    app.secret_key = _session_secret()

    # Global (not per-request) brute-force state, shared by every client.
    lockout = {'fails': 0, 'until': 0.0}
    lockout_lock = threading.Lock()
    app._pin_lockout = lockout          # exposed for tests

    def _authed():
        return session.get('authed') is True

    def _token_ok(token):
        """One-click sign-in from the PlayCall site. The site minted a nonce
        and handed it to us on our own authenticated assignment poll, so a
        matching nonce proves the link came from someone with staff access to
        this box's team — no PIN needed.

        On a miss we force one poll and re-check: the coach clicks the link
        the instant it is minted, which is usually before our next scheduled
        poll, and without this the first click would always fail."""
        if not (token and cloud):
            return False
        for attempt in (0, 1):
            want = getattr(cloud, 'login_nonce', None)
            if want and hmac.compare_digest(str(token), str(want)):
                cloud.login_nonce = None          # single use
                return True
            if attempt == 0 and getattr(cloud, 'enabled', lambda: False)():
                try:
                    cloud.poll_assignment_once()
                except Exception as e:
                    # The nonce is captured BEFORE the assignment is
                    # applied, so a failed apply (or a slow uplink mid-
                    # request) may still have delivered it — re-check
                    # instead of giving up and bouncing the coach to the
                    # PIN form.
                    log.warning(f'one-click sign-in poll hiccup: {e}')
        log.warning('one-click sign-in link refused: the cloud nonce '
                    'never matched (one arrived: '
                    f'{bool(getattr(cloud, "login_nonce", None))})')
        return False

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        token = (request.args.get('token') or '').strip()
        if token and _token_ok(token):
            session['authed'] = True
            log.info('signed in via one-click link from the PlayCall site')
            return redirect(url_for('index'))
        if token and request.method == 'GET':
            # The link was real but didn't verify (nonce expired, box
            # mid-sync, uplink hiccup). Say so — a silent PIN form reads
            # as "the button is broken".
            return render_template_string(
                LOGIN_PAGE, error='That sign-in link didn’t go '
                'through — give the box a few seconds and tap the '
                'site’s ⚙ Settings button again, or enter the '
                'PIN (the \U0001f511 PIN button on the site’s '
                'Encoders card shows it).',
                next=request.args.get('next') or '')
        if request.method == 'POST':
            # Lockout check FIRST — while locked, no comparison happens at
            # all, so parallel requests can't race past the damper.
            with lockout_lock:
                locked = time.monotonic() < lockout['until']
            if locked:
                return render_template_string(
                    LOGIN_PAGE,
                    error='Too many attempts — try again later.'), 429
            cfg = config.load()
            pin = (request.form.get('pin') or '').strip()
            stored = str(cfg['device'].get('pin') or '')
            if pin and stored and hmac.compare_digest(pin, stored):
                with lockout_lock:
                    lockout['fails'] = 0
                    lockout['until'] = 0.0
                session['authed'] = True
                # Local-path next only (e.g. a /pair link that arrived
                # before the PIN was entered) — no offsite redirects.
                nxt = request.form.get('next') or ''
                if nxt.startswith('/') and not nxt.startswith('//'):
                    return redirect(nxt)
                return redirect(url_for('index'))
            with lockout_lock:
                lockout['fails'] += 1
                if lockout['fails'] >= PIN_FAIL_THRESHOLD:
                    dur = min(PIN_LOCKOUT_MAX, PIN_LOCKOUT_BASE *
                              (2 ** (lockout['fails'] - PIN_FAIL_THRESHOLD)))
                    lockout['until'] = time.monotonic() + dur
                    log.warning(f"PIN lockout: {lockout['fails']} failures, "
                                f'locked for {dur}s')
            time.sleep(PIN_FAIL_DELAY)      # brute-force damper
            return render_template_string(LOGIN_PAGE, error='Wrong PIN.',
                                          next=request.form.get('next') or '')
        return render_template_string(LOGIN_PAGE, error=None,
                                      next=request.args.get('next') or '')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.before_request
    def _gate():
        if request.endpoint in ('login', 'static'):
            return None
        if not _authed():
            # Preserve where the user was headed (the /pair deep link from
            # the PlayCall site carries its params through the PIN step) —
            # but only for GETs. Replaying a bounced POST's path as a GET
            # after sign-in hit 405 on POST-only routes (a coach changing
            # the PIN right after an update landed on a bare error page).
            if request.method == 'GET':
                return redirect(url_for('login', next=request.full_path))
            return redirect(url_for('login'))
        return None

    def _is_newer(a, b):
        """True when version a is strictly newer than b. A cloud that
        reports an OLDER release (misconfigured, or a rollback) must
        never make this box offer a downgrade as an 'update'."""
        try:
            return ([int(x) for x in str(a).split('.')]
                    > [int(x) for x in str(b).split('.')])
        except (ValueError, AttributeError):
            return False

    @app.route('/')
    def index():
        from .scorebug import BANDWIDTH_LEVELS
        cfg = config.load()
        ingest = cloud.ingest_status() if cloud else \
            {'connected': False, 'kbps': None}
        push = cloud.push_status() if cloud else \
            {'connected': False, 'kbps': None, 'reconnects_5m': 0}
        latest = cloud.latest_version if cloud else None
        return render_template_string(
            STATUS_PAGE, version=__version__,
            latest=latest if _is_newer(latest, __version__) else None,
            ingest=ingest, push=push,
            assignment=cloud.assignment if cloud else None,
            hostname=system.hostname(), ip=system.lan_ip(),
            cpu=system.cpu_percent(), temp=system.cpu_temp(),
            msg=request.args.get('msg'), err=request.args.get('err'),
            rtmp_urls=provisioning.rtmp_urls(cfg),
            networks=cfg.get('networks') or [],
            managed=cfg.get('network_managed', True) is not False,
            yt_placeholder='(saved)' if cfg['youtube'].get('key')
            else 'xxxx-xxxx-xxxx-xxxx',
            bandwidth=cfg.get('bandwidth', 0),
            record_hours=int(cfg.get('record_hours') or 12),
            bandwidth_levels=BANDWIDTH_LEVELS,
            logs='\n'.join(config.redact_lines(system.journal_tail(60), cfg))
                 or '(no logs)')

    @app.route('/logs')
    def logs():
        return redirect(url_for('index'))

    @app.route('/logs/bundle')
    def bundle():
        return log_bundle(cloud), 200, {'Content-Type':
                                        'text/plain; charset=utf-8'}

    @app.route('/pair')
    def pair():
        """Landing for the one-click pair link minted on the PlayCall site
        (http://<this-box>:8080/pair?cloud=…&key=…). PIN-gated like every
        settings route — pairing changes where this box streams, so a
        stranger on the LAN can't re-point it. Shows a confirm; the POST
        below does the write."""
        cloud_url = (request.args.get('cloud') or '').strip().rstrip('/')
        key = (request.args.get('key') or '').strip()
        if not (cloud_url.startswith(('http://', 'https://')) and key):
            return redirect(url_for('index'))
        cur = (config.load().get('cloud') or {})
        current = cur.get('base_url') if (cur.get('base_url')
                                          and cur.get('api_key')) else ''
        host = cloud_url.split('://', 1)[1].split('/')[0]
        return render_template_string(PAIR_PAGE, done=False, error=None,
                                      cloud=cloud_url, key=key, host=host,
                                      current=current)

    @app.route('/pair', methods=['POST'])
    def pair_post():
        cloud_url = (request.form.get('cloud') or '').strip().rstrip('/')
        key = (request.form.get('key') or '').strip()
        if not (cloud_url.startswith(('http://', 'https://')) and key):
            return redirect(url_for('index'))
        cfg = config.load()
        cfg['cloud'] = {'base_url': cloud_url, 'api_key': key,
                        'feed_url': ''}
        config.save(cfg)
        log.info(f'Paired to cloud {cloud_url}')
        host = cloud_url.split('://', 1)[1].split('/')[0]
        # The cloud link re-reads config every poll — pairing is live
        # within ~5s, no restart needed.
        return render_template_string(PAIR_PAGE, done=True, host=host,
                                      error=None, cloud='', key='',
                                      current='')

    @app.route('/networks/add', methods=['POST'])
    def networks_add():
        if config.load().get('network_managed', True) is False:
            return redirect(url_for('index'))   # unmanaged: hands off
        ssid = (request.form.get('ssid') or '').strip()
        if ssid:
            label = request.form.get('label') or 'gameday'
            cfg = config.load()
            nets = [n for n in cfg['networks'] if n['ssid'] != ssid]
            nets.append({'ssid': ssid, 'psk': request.form.get('psk') or '',
                         'priority': 100 if label == 'home' else 90,
                         'label': label})
            cfg['networks'] = nets
            config.save(cfg)
            provisioning.apply_networks(cfg)
        return redirect(url_for('index'))

    @app.route('/networks/remove', methods=['POST'])
    def networks_remove():
        cfg = config.load()
        if cfg.get('network_managed', True) is False:
            return redirect(url_for('index'))   # unmanaged: hands off
        ssid = (request.form.get('ssid') or '').strip()
        cfg['networks'] = [n for n in cfg['networks'] if n['ssid'] != ssid]
        config.save(cfg)
        if system.have_networkmanager():
            system.run(['nmcli', 'connection', 'delete', 'id',
                        f'playcall-{ssid}'])
        else:
            provisioning.apply_networks(cfg)
        return redirect(url_for('index'))

    @app.route('/youtube', methods=['POST'])
    def youtube():
        url, key = provisioning.normalize_youtube(
            request.form.get('youtube') or '')
        if url or key:
            cfg = config.load()
            cfg['youtube'] = {'url': url or config.DEFAULT_YOUTUBE_URL,
                              'key': key}
            config.save(cfg)
            system.systemctl('restart', 'playcall-encoder-youtube')
        return redirect(url_for('index'))

    @app.route('/retention', methods=['POST'])
    def retention():
        try:
            hours = max(1, min(168, int(request.form.get('hours', 12))))
        except (TypeError, ValueError):
            hours = 12
        cfg = config.load()
        cfg['record_hours'] = hours
        config.save(cfg)
        try:
            config.write_mediamtx_config(cfg)
        except OSError as e:
            log.warning(f'mediamtx config not written: {e}')
        system.systemctl('restart', 'playcall-encoder-mediamtx')
        log.info(f'recording retention set to {hours}h')
        return redirect(url_for('index',
                                msg=f'Recording retention: {hours} hours'))

    @app.route('/bandwidth', methods=['POST'])
    def bandwidth():
        try:
            level = max(0, min(3, int(request.form.get('bandwidth', 0))))
        except ValueError:
            level = 0
        cfg = config.load()
        cfg['bandwidth'] = level
        config.save(cfg)
        if sender is not None:
            sender.bandwidth = level
        return redirect(url_for('index'))

    @app.route('/pin', methods=['GET', 'POST'])
    def set_pin():
        """Replace the auto-generated PIN with one the coach picks. Nobody
        remembers six random digits months after installing, and the old
        alternatives were SSH or a factory reset. GET lands here only via
        a stale next= link — go to the settings page, never a 405."""
        if request.method == 'GET':
            return redirect(url_for('index'))
        new = (request.form.get('pin') or '').strip()
        again = (request.form.get('pin2') or '').strip()
        if len(new) < 4 or len(new) > 32:
            return redirect(url_for('index', err='PIN must be 4–32 characters'))
        if new != again:
            return redirect(url_for('index', err="The two entries didn't match"))
        cfg = config.load()
        cfg['device']['pin'] = new
        config.save(cfg)
        log.info('settings PIN changed')
        return redirect(url_for('index', msg='PIN updated'))

    @app.route('/rotate-key', methods=['POST'])
    def rotate_key():
        cfg = config.load()
        config.rotate_ingest_key(cfg)
        config.save(cfg)
        try:
            config.write_mediamtx_config(cfg)
        except OSError as e:
            log.warning(f'mediamtx config not written: {e}')
        system.systemctl('restart', 'playcall-encoder-mediamtx')
        system.systemctl('restart', 'playcall-encoder-youtube')
        return redirect(url_for('index'))

    @app.route('/update', methods=['POST'])
    def update():
        """One-button software update. The download+copy runs inline (a few
        seconds on a shallow clone); the service restarts are deferred to a
        background thread so this response reaches the browser before
        playcall-encoder — the unit hosting this very web app — is bounced.
        systemd revives us on the new code."""
        ok, detail = system.self_update()
        if not ok:
            log.warning(f'self-update failed: {detail}')
            return redirect(url_for('index', err=f'Update failed — {detail}'))
        log.info(f'self-update laid down v{detail}; restarting services')

        def _restart():
            time.sleep(1.5)
            for unit in system.UPDATE_UNITS:
                system.systemctl('restart', unit)
        threading.Thread(target=_restart, daemon=True).start()
        return redirect(url_for(
            'index',
            msg=f'Updated to v{detail} — restarting now, this page will '
                'blip for a few seconds'))

    @app.route('/factory-reset', methods=['POST'])
    def factory_reset():
        # Capture the configured networks BEFORE unlinking the config —
        # their NetworkManager profiles (with stored PSKs) must be deleted
        # too, or the "reset" box silently auto-joins the old Wi-Fi.
        cfg_nets = config.load().get('networks') or []
        if system.have_networkmanager():
            for n in cfg_nets:
                system.run(['nmcli', 'connection', 'delete', 'id',
                            f"playcall-{n['ssid']}"])
        try:
            config.config_path().unlink()
        except OSError:
            pass
        system.reboot()        # boots straight into the provisioning portal
        return 'Resetting…', 200

    return app


def serve(cloud=None, sender=None, port=WEB_PORT):
    app = create_app(cloud=cloud, sender=sender)
    app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)
