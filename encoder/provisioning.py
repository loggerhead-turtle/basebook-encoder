#!/usr/bin/env python3
"""First-boot Wi-Fi hotspot + captive setup portal (evolved from the
Play-call pi/setup_server.py portal).

Unconfigured encoder → raises an open AP "PlayCall-Encoder-XXXX" (XXXX =
last 4 of the Pi serial) at 192.168.4.1, DNAT-captures port 80, and walks
the user through:
  1. Home network (scan + pick SSID, password)
  2. Optional game-day networks (field Wi-Fi / travel router) — up to two
     more, stored with lower autoconnect priority
  3. YouTube stream key (bare key OR full rtmps URL — normalized)
  4. Final screen: the custom RTMP URL for Mevo/Larix/OBS
     (rtmp://playcall-encoder.local:1935/live/<key> + IP fallback) and the
     device PIN that gates the LAN settings page later.

Networks land in NetworkManager (nmcli, autoconnect-priority) on Bookworm;
falls back to wpa_supplicant blocks (with priority=) when NM is absent.

Safety net: on a configured box, if there is NO working connectivity on
ANY interface (no default route, no associated Wi-Fi) for 90 s, the
hotspot comes back up automatically so the user can always get back in.
"""

import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, system

log = logging.getLogger('provisioning')

AP_IFACE = 'wlan0'
AP_IP = '192.168.4.1'
AP_CHANNEL = 6
DHCP_RANGE = '192.168.4.2,192.168.4.20,255.255.255.0,2h'
WPA_CONF = Path('/etc/wpa_supplicant/wpa_supplicant.conf')
OFFLINE_GRACE = 90        # seconds with no connectivity before AP re-raises

DEFAULT_YOUTUBE_URL = config.DEFAULT_YOUTUBE_URL


def ap_ssid():
    return f'PlayCall-Encoder-{system.serial_suffix()}'


# ── YouTube key normalization ────────────────────────────────────────────────

def normalize_youtube(value):
    """Accept whatever the user pastes from YouTube Studio — a bare stream
    key OR the full rtmp(s) ingest URL with the key on the end — and return
    (ingest_url, key). Empty input → ('', '')."""
    v = (value or '').strip().strip('"').strip()
    if not v:
        return '', ''
    if v.lower().startswith(('rtmp://', 'rtmps://')):
        v = v.rstrip('/')
        low = v.lower()
        # Split on the /live2/ or /live/ ingest PATH segment, so a key that
        # itself starts with "live" (e.g. "live-abcd-1234") parses correctly.
        for seg in ('/live2/', '/live/'):
            i = low.rfind(seg)
            if i > 0 and '://' in low[:i]:
                base, key = v[:i + len(seg) - 1], v[i + len(seg):]
                if key and '/' not in key:
                    return base, key
        # Not a …/live[2]/<key> URL. Generic RTMP endpoint: the last path
        # segment is the key when an app path precedes it; a bare app URL
        # ("rtmps://host/live2" — no key appended) stays the URL.
        base, _, tail = v.rpartition('/')
        if '://' in base and tail and tail.lower() not in ('live', 'live2'):
            return base, tail
        return v, ''
    return DEFAULT_YOUTUBE_URL, v


def youtube_push_url(cfg):
    yt = cfg.get('youtube') or {}
    url, key = (yt.get('url') or '').rstrip('/'), yt.get('key') or ''
    if not url:
        return ''
    return f'{url}/{key}' if key else url


def rtmp_urls(cfg):
    """The two ingest URLs shown to the user (mDNS + raw-IP fallback)."""
    key = cfg.get('local_ingest_key', '')
    host = system.hostname()
    ip = system.lan_ip()
    urls = [f'rtmp://{host}.local:1935/live/{key}']
    if ip:
        urls.append(f'rtmp://{ip}:1935/live/{key}')
    return urls


# ── network persistence (NetworkManager first, wpa_supplicant fallback) ──────

def apply_networks(cfg, runner=None):
    """Write every stored network into the OS network stack. Idempotent:
    existing playcall-* NM connections are replaced."""
    runner = runner or system.run
    nets = cfg.get('networks') or []
    if system.have_networkmanager():
        for n in nets:
            name = f"playcall-{n['ssid']}"
            runner(['nmcli', 'connection', 'delete', 'id', name])
            cmd = ['nmcli', 'connection', 'add', 'type', 'wifi',
                   'ifname', AP_IFACE, 'con-name', name,
                   'ssid', n['ssid'],
                   'connection.autoconnect', 'yes',
                   'connection.autoconnect-priority',
                   str(n.get('priority', 50))]
            if n.get('psk'):
                cmd += ['wifi-sec.key-mgmt', 'wpa-psk',
                        'wifi-sec.psk', n['psk']]
            runner(cmd)
        return 'networkmanager'
    _write_wpa_conf(nets)
    runner(['wpa_cli', '-i', AP_IFACE, 'reconfigure'])
    return 'wpa_supplicant'


def _write_wpa_conf(nets, path=None):
    path = Path(path or WPA_CONF)
    header = ('ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n'
              'update_config=1\ncountry=US\n')
    blocks = []
    for n in nets:
        ssid = n['ssid'].replace('"', '\\"')
        if n.get('psk'):
            psk = n['psk'].replace('"', '\\"')
            sec = f'    psk="{psk}"\n    key_mgmt=WPA-PSK\n'
        else:
            sec = '    key_mgmt=NONE\n'
        blocks.append('network={\n'
                      f'    ssid="{ssid}"\n{sec}'
                      f"    priority={n.get('priority', 50)}\n"
                      '}\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + '\n' + '\n'.join(blocks))
    path.chmod(0o640)
    return path


def scan_networks():
    """Visible SSIDs, best-effort via nmcli then iwlist."""
    if system.fake_mode():
        return []
    ssids = []
    if system.have('nmcli'):
        r = system.run(['nmcli', '-t', '-f', 'SSID', 'device', 'wifi',
                        'list', '--rescan', 'yes'])
        for line in (r.stdout or '').splitlines():
            s = line.strip()
            if s and s not in ssids:
                ssids.append(s)
        if ssids:
            return ssids
    system.run(['ip', 'link', 'set', AP_IFACE, 'up'])
    r = system.run(['iwlist', AP_IFACE, 'scan'])
    for line in (r.stdout or '').splitlines():
        m = re.search(r'ESSID:"(.*)"', line)
        if m and m.group(1) and m.group(1) not in ssids:
            ssids.append(m.group(1))
    return ssids


def connected_ssid():
    if not system.have('nmcli'):
        r = system.run(['iwgetid', '-r'])
        return (r.stdout or '').strip() or None
    r = system.run(['nmcli', '-t', '-f', 'active,ssid', 'device', 'wifi'])
    for line in (r.stdout or '').splitlines():
        if line.startswith('yes:'):
            return line.split(':', 1)[1] or None
    return None


def has_default_route(runner=None):
    """True when the kernel has a default route on ANY interface (Ethernet,
    Wi-Fi, USB tether…) — i.e. the box has a way out to the world."""
    runner = runner or system.run
    try:
        r = runner(['ip', 'route', 'show', 'default'])
    except OSError:
        return False
    return r.returncode == 0 and bool((r.stdout or '').strip())


def has_connectivity(runner=None):
    """A configured box counts as online when it has working connectivity
    via ANY interface: a default route (covers Ethernet / travel routers /
    tethering), or at least an associated Wi-Fi network (DHCP may still be
    settling). Only when neither holds should the recovery AP come up."""
    return has_default_route(runner) or connected_ssid() is not None


# ── adopt-existing-network setup (Speedify / Ethernet / tether) ──────────────

def headless_setup():
    """Setup for a box that is ALREADY online when we first run — a not-fresh
    Pi with Ethernet, a USB tether, or a Speedify cellular bond. Instead of
    raising the hotspot (which would kill wpa_supplicant and flush wlan0 —
    tearing down a Speedify bond member), we adopt the existing network
    untouched: generate the ingest key + settings PIN, mark the network
    unmanaged, and let the box run. Idempotent."""
    cfg = config.load()
    cfg['network_managed'] = False
    config.ensure_ingest_key(cfg)
    config.ensure_pin(cfg)
    config.save(cfg)
    try:
        config.write_mediamtx_config(cfg)
    except OSError as e:
        log.warning(f'mediamtx config not written: {e}')
    log.info('Adopted existing network (unmanaged mode) — '
             f'settings at http://{system.hostname()}.local:8080, '
             f"PIN {cfg['device']['pin']}")
    return cfg


# ── access point up/down ─────────────────────────────────────────────────────

HOSTAPD_TMPL = """\
interface={iface}
driver=nl80211
ssid={ssid}
hw_mode=g
channel={channel}
wmm_enabled=0
auth_algs=1
ignore_broadcast_ssid=0
"""

DNSMASQ_TMPL = """\
interface={iface}
dhcp-range={range}
dhcp-option=3,{ip}
dhcp-option=6,{ip}
address=/#/{ip}
no-resolv
"""

# Dedicated NAT chain for the captive-portal DNAT so setup/teardown only ever
# touches rules the portal itself added — never a blanket PREROUTING flush.
NAT_CHAIN = 'PLAYCALL_PORTAL'


def _install_captive_nat(runner):
    runner(['iptables', '-t', 'nat', '-N', NAT_CHAIN])       # noop if exists
    runner(['iptables', '-t', 'nat', '-F', NAT_CHAIN])
    runner(['iptables', '-t', 'nat', '-A', NAT_CHAIN,
            '-i', AP_IFACE, '-p', 'tcp', '--dport', '80',
            '-j', 'DNAT', '--to-destination', f'{AP_IP}:80'])
    # De-dupe the jump before (re-)adding it.
    runner(['iptables', '-t', 'nat', '-D', 'PREROUTING', '-j', NAT_CHAIN])
    runner(['iptables', '-t', 'nat', '-A', 'PREROUTING', '-j', NAT_CHAIN])


def _remove_captive_nat(runner):
    runner(['iptables', '-t', 'nat', '-D', 'PREROUTING', '-j', NAT_CHAIN])
    runner(['iptables', '-t', 'nat', '-F', NAT_CHAIN])
    runner(['iptables', '-t', 'nat', '-X', NAT_CHAIN])


def start_ap(runner=None, spawner=None):
    runner = runner or system.run
    spawner = spawner or system.spawn
    log.info(f'Starting AP: SSID={ap_ssid()} IP={AP_IP}')
    if system.have_networkmanager():
        # hostapd needs the radio; take wlan0 away from NM for the duration.
        runner(['nmcli', 'device', 'set', AP_IFACE, 'managed', 'no'])
    runner(['killall', 'wpa_supplicant'])
    runner(['killall', 'hostapd'])
    runner(['killall', 'dnsmasq'])
    time.sleep(1)
    runner(['ip', 'link', 'set', AP_IFACE, 'up'])
    runner(['ip', 'addr', 'flush', 'dev', AP_IFACE])
    runner(['ip', 'addr', 'add', f'{AP_IP}/24', 'dev', AP_IFACE])

    hostapd_path = Path('/tmp/playcall_encoder_hostapd.conf')
    dnsmasq_path = Path('/tmp/playcall_encoder_dnsmasq.conf')
    hostapd_path.write_text(HOSTAPD_TMPL.format(
        iface=AP_IFACE, ssid=ap_ssid(), channel=AP_CHANNEL))
    dnsmasq_path.write_text(DNSMASQ_TMPL.format(
        iface=AP_IFACE, range=DHCP_RANGE, ip=AP_IP))

    spawner(['hostapd', str(hostapd_path)])
    time.sleep(2)
    spawner(['dnsmasq', '--no-daemon', f'--conf-file={dnsmasq_path}'])
    time.sleep(1)
    # Captive-portal check URLs (iOS/Android/Windows) all land on us.
    _install_captive_nat(runner)
    log.info(f'AP running — connect to {ap_ssid()}')


def stop_ap(runner=None):
    runner = runner or system.run
    log.info('Stopping AP')
    runner(['killall', 'hostapd'])
    runner(['killall', 'dnsmasq'])
    _remove_captive_nat(runner)
    runner(['ip', 'addr', 'flush', 'dev', AP_IFACE])
    if system.have_networkmanager():
        runner(['nmcli', 'device', 'set', AP_IFACE, 'managed', 'yes'])
    time.sleep(1)


# ── captive portal (Flask) ───────────────────────────────────────────────────

PORTAL_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#eee;font-family:system-ui,sans-serif;
     min-height:100vh;display:flex;flex-direction:column;align-items:center;
     justify-content:center;padding:1rem}
.logo{font-size:1.6rem;font-weight:700;color:#10b981;margin-bottom:.25rem}
.sub{font-size:.8rem;color:#666;margin-bottom:1.5rem}
.card{background:#161b22;border:1px solid #2a313c;border-radius:12px;
      padding:1.5rem;width:100%;max-width:400px}
h2{font-size:1rem;font-weight:600;margin-bottom:1rem;color:#ccc}
label{display:block;font-size:.75rem;color:#8b949e;margin:.6rem 0 .3rem}
input,select{display:block;width:100%;padding:.65rem .9rem;background:#0d1117;
  border:1px solid #30363d;border-radius:6px;color:#eee;font-size:1rem;
  -webkit-appearance:none}
input:focus,select:focus{outline:none;border-color:#10b981}
.btn{display:block;width:100%;padding:.8rem;background:#10b981;color:#000;
     border:none;border-radius:8px;font-size:1rem;font-weight:700;
     cursor:pointer;margin-top:1rem}
.step{font-size:.7rem;color:#10b981;text-transform:uppercase;
      letter-spacing:.1em;margin-bottom:.5rem}
.alert{background:#2d1214;border:1px solid #c62828;border-radius:6px;
       padding:.75rem;margin-bottom:1rem;font-size:.85rem;color:#ef9a9a}
.hint{font-size:.75rem;color:#666;margin-top:.25rem}
.url{background:#0d1117;border:1px solid #30363d;border-radius:6px;
     padding:.6rem;font-family:monospace;font-size:.8rem;word-break:break-all;
     margin:.4rem 0;color:#79c0ff}
.pin{font-family:monospace;font-size:1.6rem;letter-spacing:.3em;
     color:#f5b301;text-align:center;margin:.5rem 0}
.opt{color:#8b949e;font-size:.8rem;margin:.75rem 0 .25rem}
"""

PORTAL_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PlayCall Encoder Setup</title>
<style>""" + PORTAL_CSS + """</style>
</head><body>
<div class="logo">PlayCall Encoder</div>
<div class="sub">First-time setup</div>

{% if done %}
<div class="card">
  <div class="step">Setup complete</div>
  <h2>Point your camera app here</h2>
  <p class="hint">In Mevo / Larix / OBS choose "Custom RTMP" and use:</p>
  {% for u in urls %}<div class="url">{{ u }}</div>{% endfor %}
  <p class="hint">The first address works once your phone/laptop is on
     <strong>{{ home_ssid }}</strong> with the encoder. If it doesn't
     resolve, use the second (IP) address.</p>
  {% if code %}
  <p class="hint" style="margin-top:1rem"><strong>Joining your team.</strong>
     This box connects itself to basebook.org as soon as it is on your
     Wi-Fi — usually within a minute. Watch it arrive on
     <strong>Score Bug Studio → Encoders</strong>. Nothing else to do.</p>
  {% else %}
  <p class="hint" style="margin-top:1rem"><strong>Next — connect it to
     your team:</strong> on basebook.org go to <strong>Score Bug Studio →
     Encoders → + Add an encoder</strong> and follow the one command it
     gives you.</p>
  {% endif %}
  <label>Recovery PIN</label>
  <div class="pin">{{ pin }}</div>
  <p class="hint">You will not normally need this — the site's
     <strong>⚙ Settings</strong> button opens
     <strong>http://{{ host }}.local:8080</strong> already signed in. The
     PIN is for reaching the box directly, and the site can show it to you
     again any time.</p>
  <p class="hint" style="margin-top:1rem">The encoder is now joining
     <strong>{{ home_ssid }}</strong> — this hotspot will disappear in a
     few seconds. You can close this page.</p>
</div>

{% else %}
<div class="card">
  {% if error %}<div class="alert">{{ error }}</div>{% endif %}
  <form method="post" action="/setup">
    <div class="step">Step 1 · Your team</div>
    <label>Activation code</label>
    <input type="text" name="code" autocomplete="off" autocapitalize="characters"
           placeholder="HAWK-4823">
    <p class="hint">From basebook.org &rarr; Score Bug Studio &rarr;
       Encoders &rarr; <strong>&plus; Add an encoder</strong>. This box
       joins your team on its own as soon as it is online — nothing else
       to do. Leave blank to connect it later.</p>

    <div class="step" style="margin-top:1.25rem">Step 2 · Home network</div>
    <label>Wi-Fi network</label>
    {% if networks %}
    <select name="ssid" id="s0" onchange="oth(this,'m0')">
      {% for n in networks %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
      <option value="__other__">Other (type manually)…</option>
    </select>
    <input type="text" name="ssid_manual" id="m0" placeholder="Network name"
           style="display:none">
    {% else %}
    <input type="text" name="ssid_manual" placeholder="Network name" required>
    {% endif %}
    <label>Password</label>
    <input type="password" name="password" autocomplete="off">

    <div class="step" style="margin-top:1.25rem">Step 3 · Game-day network
      <span style="color:#666;text-transform:none">(optional)</span></div>
    <p class="hint">Field Wi-Fi or a travel router you stream through at
       games. The encoder joins whichever known network it finds.</p>
    <div class="opt">Game-day network #1</div>
    <input type="text" name="ssid2" placeholder="Network name (optional)">
    <input type="password" name="password2" placeholder="Password"
           autocomplete="off" style="margin-top:.4rem">
    <div class="opt">Game-day network #2</div>
    <input type="text" name="ssid3" placeholder="Network name (optional)">
    <input type="password" name="password3" placeholder="Password"
           autocomplete="off" style="margin-top:.4rem">

    <div class="step" style="margin-top:1.25rem">Step 4 · YouTube</div>
    <label>Stream key or full RTMP URL</label>
    <input type="text" name="youtube" autocomplete="off"
           placeholder="xxxx-xxxx-xxxx-xxxx  or  rtmps://…/live2/xxxx">
    <p class="hint">YouTube Studio → Go live → copy the Stream key. Leave
       blank to add it later from the settings page.</p>

    <button class="btn" type="submit">Save &amp; Connect</button>
  </form>
</div>
{% endif %}

<script>
function oth(sel,id){var m=document.getElementById(id);
  if(sel.value==='__other__'){m.style.display='block';m.required=true;}
  else{m.style.display='none';m.required=false;}}
</script>
</body></html>"""


def build_networks(form):
    """Turn the portal form fields into the config networks list.
    Home network gets the highest autoconnect priority, game-day networks
    next — so at home it prefers home, at the field it takes what exists."""
    ssid = (form.get('ssid') or '').strip()
    if ssid in ('', '__other__'):
        ssid = (form.get('ssid_manual') or '').strip()
    if not ssid:
        return None, 'Please enter a Wi-Fi network name.'
    nets = [{'ssid': ssid, 'psk': form.get('password') or '',
             'priority': 100, 'label': 'home'}]
    for i, (s_key, p_key, prio) in enumerate(
            (('ssid2', 'password2', 90), ('ssid3', 'password3', 80))):
        s = (form.get(s_key) or '').strip()
        if s and all(n['ssid'] != s for n in nets):
            nets.append({'ssid': s, 'psk': form.get(p_key) or '',
                         'priority': prio, 'label': 'gameday'})
    return nets, None


def complete_setup(form):
    """Validate the portal form, persist config, bake the mediamtx key.
    Returns (cfg, error)."""
    nets, err = build_networks(form)
    if err:
        return None, err
    cfg = config.load()
    cfg['networks'] = nets
    # The phone typing this is joined to the BOX's hotspot, so there is no
    # internet to spend the code on yet. Store it; _finish() redeems it a
    # few seconds later, once the box is on the network the coach just
    # gave us. A typo has to be caught here — after the hotspot drops,
    # nobody is looking at this page any more.
    raw_code = (form.get('code') or '').strip()
    if raw_code:
        from . import activation
        code = activation.normalize(raw_code)
        if not code:
            return None, (f'"{raw_code}" is not an activation code — four '
                          'letters then four digits, like HAWK-4823. Leave '
                          'it blank to connect this box later.')
        cfg['pending_code'] = code
    url, key = normalize_youtube(form.get('youtube') or '')
    if url or key:
        cfg['youtube'] = {'url': url or DEFAULT_YOUTUBE_URL, 'key': key}
    config.ensure_ingest_key(cfg)
    config.ensure_pin(cfg)
    config.save(cfg)
    try:
        config.write_mediamtx_config(cfg)
    except OSError as e:
        log.warning(f'mediamtx config not written: {e}')
    return cfg, None


def redeem_pending(cfg=None, tries=6, delay=10):
    """Spend a stored activation code now that the box has a network.

    Called on the way out of the setup portal, where the code was typed
    on a phone with no route to the internet. Never raises: this runs on
    a background thread that is about to exit the process, and a box that
    fails to pair is still a working local encoder.
    """
    from . import activation
    cfg = cfg if cfg is not None else config.load()
    code = activation.normalize(cfg.get('pending_code') or '')
    if not code or activation.already_paired(cfg):
        return cfg
    try:
        out = activation.redeem_with_retry(code, tries=tries, delay=delay)
    except activation.Refused as e:
        # The site has looked at this code and said no. Asking again on
        # every boot from here to eternity gets the same no — drop it.
        log.warning(f'activation code refused: {e}')
        cfg['pending_code'] = ''
        config.save(cfg)
        return cfg
    except activation.Unreachable as e:
        # Keep the code. The first-boot unit retries next boot, and the
        # coach may simply have given us Wi-Fi that is not in range yet.
        log.warning(f'activation deferred: {e}')
        return cfg
    log.info('paired to %s', out.get('team_name') or 'a team')
    return activation.apply(out, cfg)


def run_portal():
    """Bring up the AP and serve the captive portal. Blocks until setup
    completes, then joins the chosen network and returns the new config."""
    from flask import Flask, request, render_template_string

    # Was this box already configured (watchdog re-provisioning) or is this
    # first-boot setup? Decides how we hand control back at the end.
    was_configured = config.is_configured()
    start_ap()
    app = Flask(__name__)
    done = threading.Event()
    result = {}

    def _page(**kw):
        base = dict(networks=scan_networks(), error=None, done=False,
                    urls=[], pin='', host=system.hostname(), home_ssid='',
                    code='')
        base.update(kw)
        return render_template_string(PORTAL_PAGE, **base)

    @app.route('/', defaults={'path': ''})
    @app.route('/<path:path>')
    def index(path):
        return _page()

    @app.route('/setup', methods=['POST'])
    def setup():
        cfg, err = complete_setup(request.form)
        if err:
            return _page(error=err)
        result['cfg'] = cfg
        resp = _page(done=True, urls=rtmp_urls(cfg),
                     pin=cfg['device']['pin'],
                     code=cfg.get('pending_code') or '',
                     home_ssid=cfg['networks'][0]['ssid'])
        done.set()
        return resp

    # Captive-portal detection endpoints → redirect into the portal.
    for path in ['/generate_204', '/hotspot-detect.html', '/connecttest.txt',
                 '/ncsi.txt', '/redirect']:
        app.add_url_rule(path, path.lstrip('/') or 'gen204',
                         lambda: ('', 302, {'Location': f'http://{AP_IP}/'}))

    def _finish():
        done.wait()
        time.sleep(4)          # let the success page render on the phone
        stop_ap()
        cfg = result.get('cfg') or config.load()
        apply_networks(cfg)
        # Kick the relay stack now that config exists.
        system.systemctl('restart', 'playcall-encoder-mediamtx')
        system.systemctl('restart', 'playcall-encoder-youtube')
        time.sleep(2)
        # The activation code the coach typed on the portal could not be
        # spent then — their phone was on our hotspot and the box had no
        # internet. It does now, or will within a few seconds of joining
        # their Wi-Fi, so spend it here rather than waiting for the next
        # reboot. Retries cover a slow join; a refusal is logged and the
        # code dropped, because re-asking gets the same answer forever.
        cfg = redeem_pending(cfg)
        if was_configured:
            # Watchdog re-provisioning on an already-configured box: ask
            # systemd for a graceful restart of the encoder service (SIGTERM
            # → clean shutdown of the sender/cloud threads) instead of
            # killing the process mid-stream with os._exit.
            system.systemctl('restart', 'playcall-encoder')
            time.sleep(15)     # systemd's SIGTERM lands during this window
        # First-boot path (or systemctl unavailable): flask has no clean
        # stop, so exit hard — the systemd unit restarts the entrypoint,
        # which now sees a configured device and boots normally.
        import os
        os._exit(0)

    threading.Thread(target=_finish, daemon=True).start()
    app.run(host='0.0.0.0', port=80, threaded=True, use_reloader=False)
    return result.get('cfg')


# ── offline watchdog ─────────────────────────────────────────────────────────

def network_watchdog(interval=10, grace=OFFLINE_GRACE, on_offline=None,
                     is_online=None, stop_event=None):
    """Background loop for a CONFIGURED box: if the box has had NO working
    connectivity on ANY interface (no default route, no associated Wi-Fi —
    see has_connectivity) for `grace` seconds, re-raise the setup hotspot so
    the user can always get back in. A box happily streaming over Ethernet
    or a travel router is online and must never have its network stack torn
    down. Injection points (`is_online`, `on_offline`) exist for tests."""
    is_online = is_online or has_connectivity
    offline_since = None
    while not (stop_event and stop_event.is_set()):
        if is_online():
            offline_since = None
        else:
            offline_since = offline_since or time.monotonic()
            if time.monotonic() - offline_since >= grace:
                log.warning(f'No known network for {grace}s — '
                            'raising setup hotspot')
                if on_offline:
                    on_offline()
                else:
                    run_portal()
                return True
        time.sleep(interval)
    return False
