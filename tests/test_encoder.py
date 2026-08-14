"""Unit tests for the PlayCall NDI Encoder package (laptop-runnable:
no NDI, no root, no network — see conftest.py sandbox fixture)."""

import json
import threading
import time
from pathlib import Path

from encoder import cloud_link, config, provisioning, scorebug


FAKE_BUG = {
    'seq': 42,
    'away': {'abbr': 'WAR', 'runs': 4, 'color': '#1f6feb'},
    'home': {'abbr': 'SID', 'runs': 2, 'color': '#da3633'},
    'inning': 5, 'half': 'top', 'balls': 3, 'strikes': 2, 'outs': 2,
    'bases': [True, False, True],
    'batter': {'name': 'Sam Turner', 'slot': 4, 'line': '2-3'},
    'pitcher': {'name': 'Alex Reyes', 'pc': 67},
    'conf': {'away': 2, 'home': 3},
}


def _theme(layout, **over):
    t = {'version': 2, 'layout': layout, 'pos': 'bl', 'scale': 1.0,
         'colors': {'bg': '#101418', 'accent': '#e0352b',
                    'accent2': '#f5c518', 'text': '#ffffff'},
         'font': 'oswald',
         'show': {'pitchcount': True, 'outs': True, 'count': True,
                  'bases': True, 'inning': True}}
    t.update(over)
    return t


def _render_with_theme(layout):
    bug = dict(FAKE_BUG, theme=_theme(layout))
    lay, pos, scale, bw, theme = scorebug.resolve_look(bug)
    return scorebug.render_bug(bug, pos, scale, lay, theme)


def _sample(img):
    """Downsampled pixel fingerprint for cheap image comparison."""
    return img.resize((40, 24)).tobytes()


# ── rendering ────────────────────────────────────────────────────────────────

def test_all_layout_families_render_nonblank_and_distinct():
    samples = {}
    for layout in scorebug.LAYOUTS:
        img = _render_with_theme(layout)
        assert img.size == (scorebug.W, scorebug.H)
        # Non-blank: the bug drew opaque pixels somewhere.
        assert img.getchannel('A').getbbox() is not None, layout
        opaque = sum(1 for a in img.getchannel('A').tobytes() if a > 200)
        assert opaque > 500, f'{layout} rendered almost nothing'
        samples[layout] = _sample(img)
    # Pairwise distinct: every family must actually look different.
    keys = list(samples)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            assert samples[a] != samples[b], f'{a} and {b} render identically'


def test_unknown_layout_falls_back_to_bar():
    bug = dict(FAKE_BUG, theme=_theme('holographic-cube'))
    lay, pos, scale, bw, theme = scorebug.resolve_look(bug)
    assert lay == 'bar'
    img = scorebug.render_bug(bug, pos, scale, lay, theme)
    assert _sample(img) == _sample(_render_with_theme('bar'))


def test_theme_parsed_defensively():
    # Garbage everywhere → defaults, no exception.
    t = scorebug.parse_theme({'layout': 12, 'pos': 'zz', 'scale': 'huge',
                              'colors': 'red', 'font': ['x'],
                              'show': {'outs': 0}})
    assert t['layout'] == 'bar' and t['pos'] == 'bl' and t['scale'] == 1.0
    assert t['show']['outs'] is False
    # Not a dict at all → legacy (None) path.
    assert scorebug.parse_theme(None) is None
    assert scorebug.parse_theme('tv') is None


def test_no_theme_uses_legacy_ndi_cfg_keys():
    bug = dict(FAKE_BUG, ndi={'pos': 'tr', 'layout': 'tv',
                              'scale': 1.2, 'bandwidth': 2})
    lay, pos, scale, bw, theme = scorebug.resolve_look(bug)
    assert (lay, pos, scale, bw, theme) == ('tv', 'tr', 1.2, 2, None)


def test_theme_colors_change_the_pixels():
    a = _render_with_theme('bar')
    bug = dict(FAKE_BUG, theme=_theme('bar', colors={'bg': '#ffffff',
                                                     'accent': '#000000',
                                                     'accent2': '#111111',
                                                     'text': '#000000'}))
    lay, pos, scale, bw, theme = scorebug.resolve_look(bug)
    b = scorebug.render_bug(bug, pos, scale, lay, theme)
    assert _sample(a) != _sample(b)


def test_show_flags_hide_elements():
    on = _render_with_theme('bottomline')
    bug = dict(FAKE_BUG, theme=_theme('bottomline',
                                      show={'pitchcount': False,
                                            'outs': False, 'count': False,
                                            'bases': False, 'inning': False}))
    lay, pos, scale, bw, theme = scorebug.resolve_look(bug)
    off = scorebug.render_bug(bug, pos, scale, lay, theme)
    assert _sample(on) != _sample(off)


def test_sender_fake_mode_writes_png(tmp_path):
    s = scorebug.Sender(feed='http://x/feed.json', fake=True,
                        fake_dir=str(tmp_path))
    s.fetch = lambda: dict(FAKE_BUG, theme=_theme('tvbox'))
    assert s.poll_once() is True
    assert s.layout == 'tvbox'
    s.write_png(str(tmp_path / 'bug.png'))
    assert (tmp_path / 'bug.png').stat().st_size > 0
    # Same seq + same look → no re-render.
    assert s.poll_once() is False


# ── config ───────────────────────────────────────────────────────────────────

def test_config_atomic_write_read():
    cfg = config.load()
    assert cfg['youtube']['url'] == config.DEFAULT_YOUTUBE_URL
    cfg['networks'] = [{'ssid': 'Home', 'psk': 'pw', 'priority': 100,
                        'label': 'home'}]
    config.ensure_ingest_key(cfg)
    config.ensure_pin(cfg)
    config.save(cfg)
    # No stray temp files, and the write is really on disk.
    files = [p.name for p in config.config_dir().iterdir()]
    assert files == ['config.json']
    back = config.load()
    assert back['networks'][0]['ssid'] == 'Home'
    assert len(back['local_ingest_key']) == 8
    assert len(back['device']['pin']) == 6
    assert config.is_configured(back)


def test_config_merge_keeps_unknown_and_defaults():
    config.save({'bandwidth': 3, 'extra': {'x': 1}})
    cfg = config.load()
    assert cfg['bandwidth'] == 3
    assert cfg['extra'] == {'x': 1}          # forward-compat passthrough
    assert cfg['youtube']['url']             # defaults filled in


def test_write_mediamtx_config(tmp_path):
    cfg = config.load()
    cfg['local_ingest_key'] = 'abcd1234'
    dest = config.write_mediamtx_config(cfg, dest=tmp_path / 'mtx.yml')
    text = dest.read_text()
    assert 'live/abcd1234:' in text
    assert '__INGEST_KEY__' not in text
    assert 'apiAddress: 127.0.0.1:9997' in text


# ── provisioning ─────────────────────────────────────────────────────────────

def test_normalize_youtube_bare_key():
    url, key = provisioning.normalize_youtube('abcd-1234-efgh-5678')
    assert url == 'rtmps://a.rtmps.youtube.com/live2'
    assert key == 'abcd-1234-efgh-5678'


def test_normalize_youtube_full_url():
    url, key = provisioning.normalize_youtube(
        'rtmps://a.rtmps.youtube.com/live2/abcd-1234-efgh-5678')
    assert url == 'rtmps://a.rtmps.youtube.com/live2'
    assert key == 'abcd-1234-efgh-5678'


def test_normalize_youtube_url_without_key_and_empty():
    url, key = provisioning.normalize_youtube(
        'rtmps://a.rtmps.youtube.com/live2/')
    assert url == 'rtmps://a.rtmps.youtube.com/live2'
    assert key == ''
    assert provisioning.normalize_youtube('  ') == ('', '')


def test_normalize_youtube_key_starting_with_live():
    # Regression: keys that themselves start with "live" used to be treated
    # as the app path ("no key appended") by the old startswith check.
    url, key = provisioning.normalize_youtube(
        'rtmps://a.rtmps.youtube.com/live2/live-abcd-1234')
    assert url == 'rtmps://a.rtmps.youtube.com/live2'
    assert key == 'live-abcd-1234'
    # /live/ (not /live2/) app path, key starting with "live2".
    url, key = provisioning.normalize_youtube(
        'rtmp://a.rtmp.youtube.com/live/live2-zz-99')
    assert (url, key) == ('rtmp://a.rtmp.youtube.com/live', 'live2-zz-99')
    # Bare key that starts with "live" is still just a key.
    url, key = provisioning.normalize_youtube('live-abcd-1234')
    assert url == config.DEFAULT_YOUTUBE_URL and key == 'live-abcd-1234'
    # Generic custom endpoint still splits app/key.
    url, key = provisioning.normalize_youtube('rtmp://example.com/app/sk-77')
    assert (url, key) == ('rtmp://example.com/app', 'sk-77')


def test_build_networks_priorities():
    nets, err = provisioning.build_networks(
        {'ssid': 'Home', 'password': 'pw',
         'ssid2': 'Field', 'password2': 'fw', 'ssid3': ''})
    assert err is None
    assert [n['ssid'] for n in nets] == ['Home', 'Field']
    assert nets[0]['priority'] > nets[1]['priority']
    assert nets[0]['label'] == 'home' and nets[1]['label'] == 'gameday'
    nets, err = provisioning.build_networks({'ssid': ''})
    assert nets is None and err


def test_complete_setup_persists_and_bakes_key(monkeypatch):
    monkeypatch.setattr(config, 'write_mediamtx_config', lambda cfg: None)
    cfg, err = provisioning.complete_setup(
        {'ssid': 'Home', 'password': 'pw',
         'youtube': 'rtmps://a.rtmps.youtube.com/live2/kk-11'})
    assert err is None
    saved = config.load()
    assert saved['youtube'] == {'url': 'rtmps://a.rtmps.youtube.com/live2',
                                'key': 'kk-11'}
    assert config.is_configured(saved)
    urls = provisioning.rtmp_urls(saved)
    assert any(f":1935/live/{saved['local_ingest_key']}" in u for u in urls)


def test_wpa_conf_fallback(tmp_path):
    p = provisioning._write_wpa_conf(
        [{'ssid': 'A "quoted"', 'psk': 'secret', 'priority': 100},
         {'ssid': 'Open', 'psk': '', 'priority': 90}],
        path=tmp_path / 'wpa.conf')
    text = p.read_text()
    assert 'ssid="A \\"quoted\\""' in text
    assert 'priority=100' in text
    assert 'key_mgmt=NONE' in text


def test_network_watchdog_raises_ap_after_grace():
    calls = []
    fired = provisioning.network_watchdog(
        interval=0, grace=0.05, is_online=lambda: False,
        on_offline=lambda: calls.append(1))
    assert fired is True and calls == [1]


class _RunResult:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.stderr = ''
        self.returncode = returncode


def test_watchdog_does_not_trigger_with_default_route(monkeypatch):
    # Ethernet-only box: no Wi-Fi association, but a default route exists —
    # the watchdog must treat it as online and never raise the AP.
    monkeypatch.setattr(provisioning, 'connected_ssid', lambda: None)
    monkeypatch.setattr(
        provisioning.system, 'run',
        lambda cmd, **kw: _RunResult('default via 192.168.1.1 dev eth0\n'))
    assert provisioning.has_connectivity() is True
    stop = threading.Event()
    seen = {'n': 0}
    real = provisioning.has_connectivity

    def online():                      # default logic + a test stop switch
        seen['n'] += 1
        if seen['n'] >= 8:
            stop.set()
        return real()

    fired = []
    res = provisioning.network_watchdog(
        interval=0, grace=0.01, is_online=online,
        on_offline=lambda: fired.append(1), stop_event=stop)
    assert res is False and fired == []


def test_watchdog_triggers_with_no_connectivity(monkeypatch):
    # No default route on any interface AND no Wi-Fi association → after the
    # grace period the default is_online path must fire on_offline.
    monkeypatch.setattr(provisioning, 'connected_ssid', lambda: None)
    monkeypatch.setattr(provisioning.system, 'run',
                        lambda cmd, **kw: _RunResult('', 0))
    assert provisioning.has_connectivity() is False
    fired = []
    res = provisioning.network_watchdog(
        interval=0, grace=0.02, on_offline=lambda: fired.append(1))
    assert res is True and fired == [1]


# ── adopt-existing-network mode (Speedify / Ethernet / not-fresh Pi) ─────────

def test_headless_setup_adopts_network_unmanaged():
    cfg = provisioning.headless_setup()
    # configured with NO stored networks: the OS (Speedify/Ethernet) owns them
    assert cfg['network_managed'] is False
    assert cfg['networks'] == []
    assert cfg['local_ingest_key'] and cfg['device']['pin']
    assert config.is_configured(config.load()) is True
    # idempotent: a second run keeps the same key + PIN
    again = provisioning.headless_setup()
    assert again['local_ingest_key'] == cfg['local_ingest_key']
    assert again['device']['pin'] == cfg['device']['pin']


def test_managed_box_still_needs_networks_to_be_configured():
    cfg = config.load()
    config.ensure_ingest_key(cfg)
    config.save(cfg)
    assert config.is_configured(config.load()) is False   # managed, no wifi


def test_speedify_detection_via_service_and_iface(monkeypatch):
    from encoder import system as enc_system
    monkeypatch.setenv('SCOREBUG_FAKE', '0')     # detection is real-mode only
    # service active → True (no /sys peek needed)
    monkeypatch.setattr(enc_system, 'run',
                        lambda cmd, **kw: _RunResult('', 0))
    assert enc_system.speedify_active() is True
    # service inactive, bonding iface present → True
    monkeypatch.setattr(enc_system, 'run',
                        lambda cmd, **kw: _RunResult('', 3))
    monkeypatch.setattr(enc_system.os, 'listdir',
                        lambda p: ['lo', 'eth0', 'connectify0'])
    assert enc_system.speedify_active() is True
    # neither → False
    monkeypatch.setattr(enc_system.os, 'listdir',
                        lambda p: ['lo', 'eth0', 'wlan0'])
    assert enc_system.speedify_active() is False


def test_preconfig_can_mark_network_unmanaged(tmp_path):
    p = tmp_path / 'playcall-encoder.json'
    p.write_text(json.dumps({'network_managed': False,
                             'device_name': 'Speedify Box'}))
    assert config.apply_preconfig(paths=[p]) is True
    cfg = config.load()
    assert cfg['network_managed'] is False
    assert config.is_configured(cfg) is True     # no networks needed
    assert not p.exists()                        # consumed


def test_pair_flow_pin_gated_then_saves_cloud_config(monkeypatch):
    from encoder import web
    cfg = config.load()
    config.ensure_ingest_key(cfg)
    config.ensure_pin(cfg)
    cfg['networks'] = [{'ssid': 'x', 'psk': '', 'priority': 100,
                        'label': 'home'}]
    config.save(cfg)
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    app = web.create_app()
    client = app.test_client()
    url = '/pair?cloud=https%3A%2F%2Fplaysigns.net&key=pce_abc123'
    # not signed in: bounced to the PIN page, WITH the pair link preserved
    r = client.get(url)
    assert r.status_code == 302 and '/login' in r.headers['Location']
    assert 'next=' in r.headers['Location']
    # sign in carrying next → back on the pairing confirm
    r = client.post('/login', data={'pin': cfg['device']['pin'],
                                    'next': url}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert 'Pair this encoder to PlayCall?' in html
    assert 'playsigns.net' in html
    # confirm → cloud config saved; pairing live on the next 5s poll
    r = client.post('/pair', data={'cloud': 'https://playsigns.net',
                                   'key': 'pce_abc123'})
    assert b'paired to playsigns.net' in r.data
    saved = config.load()['cloud']
    assert saved['base_url'] == 'https://playsigns.net'
    assert saved['api_key'] == 'pce_abc123'


def test_pair_rejects_junk(monkeypatch):
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    app = web.create_app()
    client = app.test_client()
    client.post('/login', data={'pin': config.load()['device']['pin']})
    # missing key / non-http cloud → back to settings, nothing saved
    assert client.get('/pair?cloud=https%3A%2F%2Fx').status_code == 302
    r = client.post('/pair', data={'cloud': 'javascript:alert(1)',
                                   'key': 'k'})
    assert r.status_code == 302
    assert config.load()['cloud']['api_key'] == ''


def test_unmanaged_box_settings_page_hides_wifi_forms(monkeypatch):
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    app = web.create_app()
    client = app.test_client()
    cfg = config.load()
    client.post('/login', data={'pin': cfg['device']['pin']})
    html = client.get('/').get_data(as_text=True)
    assert 'uses its own network setup' in html
    assert 'Add network' not in html
    # and the network-write endpoints refuse to touch anything
    calls = []
    monkeypatch.setattr(provisioning, 'apply_networks',
                        lambda *a, **k: calls.append(1))
    client.post('/networks/add', data={'ssid': 'X', 'psk': 'y'})
    client.post('/networks/remove', data={'ssid': 'X'})
    assert calls == [] and config.load()['networks'] == []


# ── cloud_link ───────────────────────────────────────────────────────────────

ASSIGNMENT = {
    'assigned': True, 'team_id': 't1', 'team_name': 'Warriors',
    'bug_feed_url': 'https://cloud/api/sk/bug/bug_abc.json',
    'youtube_rtmp_url': 'rtmps://a.rtmps.youtube.com/live2/war-key',
    'game_id': 'g9',
}


def _paired_cfg():
    cfg = config.load()
    cfg['cloud'] = {'base_url': 'https://cloud', 'api_key': 'k123',
                    'feed_url': ''}
    cfg['local_ingest_key'] = 'abcd1234'
    config.save(cfg)
    return cfg


def test_assignment_change_triggers_repoint():
    _paired_cfg()
    feeds, cmds = [], []

    def http(url, headers=None, payload=None, **kw):
        assert headers == {'X-Api-Key': 'k123'}
        assert url == 'https://cloud/api/encoder/assignment'
        return dict(ASSIGNMENT)

    link = cloud_link.CloudLink(on_feed_change=feeds.append,
                                runner=lambda cmd, **kw: cmds.append(cmd),
                                http=http)
    assert link.poll_assignment_once() is True
    assert feeds == ['https://cloud/api/sk/bug/bug_abc.json']
    assert ['systemctl', 'restart', 'playcall-encoder-youtube'] in cmds
    cfg = config.load()
    assert cfg['youtube'] == {'url': 'rtmps://a.rtmps.youtube.com/live2',
                              'key': 'war-key'}
    assert cfg['cloud']['feed_url'] == ASSIGNMENT['bug_feed_url']
    # Same assignment again → no churn.
    cmds.clear()
    assert link.poll_assignment_once() is False
    assert cmds == []


def test_unassignment_clears_push_target():
    _paired_cfg()
    cmds = []
    link = cloud_link.CloudLink(on_feed_change=lambda f: None,
                                runner=lambda cmd, **kw: cmds.append(cmd),
                                http=lambda *a, **kw: dict(ASSIGNMENT))
    link.poll_assignment_once()
    link.http = lambda *a, **kw: {'assigned': False, 'team_id': None,
                                  'team_name': None, 'bug_feed_url': None,
                                  'youtube_rtmp_url': None, 'game_id': None}
    assert link.poll_assignment_once() is True
    assert config.load()['youtube']['key'] == ''


def test_heartbeat_payload_shape():
    _paired_cfg()
    # Fake MediaMTX API + a fresh push status file.
    def http(url, headers=None, payload=None, **kw):
        if '/v3/paths/list' in url:
            return {'itemCount': 1, 'pageCount': 1, 'items': [
                {'name': 'live/abcd1234', 'ready': True,
                 'bytesReceived': 1_000_000}]}
        raise AssertionError(f'unexpected url {url}')

    state = config.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / 'push.json').write_text(json.dumps(
        {'connected': True, 'kbps': 3050, 'updated': time.time(),
         'reconnect_times': [time.time() - 60, time.time() - 3600],
         'stderr_tail': []}))

    link = cloud_link.CloudLink(http=http)
    hb = link.heartbeat_payload()
    assert hb['state'] == 'pushing'
    assert set(hb) == {'state', 'ingest', 'push', 'cpu', 'temp',
                       'version', 'log_tail', 'hostname', 'ip', 'clips',
                       'pin', 'rtmp_urls', 'radar', 'temp_max', 'storage'}
    # fake mode (conftest) has no recordings mount — storage rides as a
    # benign stub so a dev laptop never fakes an outage
    assert hb['storage']['ok'] is True
    # a box with no gun still beats — radar rides as None
    assert hb['radar'] is None
    # camera-facing ingest URLs ride the beat, raw IP first — the
    # address that still works when mDNS dies on a field hotspot
    assert isinstance(hb['rtmp_urls'], list) and hb['rtmp_urls']
    assert all(u.startswith('rtmp://') for u in hb['rtmp_urls'])
    assert hb['rtmp_urls'][-1].split('/live/')[0].endswith('.local:1935')
    assert set(hb['ingest']) == {'connected', 'kbps'}
    assert set(hb['push']) == {'connected', 'kbps', 'reconnects_5m'}
    assert hb['push']['reconnects_5m'] == 1          # only the recent one
    assert hb['push']['kbps'] == 3050
    assert isinstance(hb['cpu'], float)
    assert isinstance(hb['version'], str) and hb['version']
    assert isinstance(hb['log_tail'], list)
    # ingest connected, but kbps needs two samples for a delta
    assert hb['ingest']['connected'] is True

    # heartbeat POST goes to the right endpoint
    posted = {}

    def http2(url, headers=None, payload=None, **kw):
        if '/v3/paths/list' in url:
            return {'items': []}
        posted['url'], posted['payload'] = url, payload
        return {}
    link.http = http2
    link.send_heartbeat_once()
    assert posted['url'] == 'https://cloud/api/encoder/heartbeat'
    assert posted['payload']['state'] in ('idle', 'receiving', 'pushing')


def test_ingest_kbps_from_bytes_delta(monkeypatch):
    _paired_cfg()
    rx = {'v': 0}

    def http(url, **kw):
        return {'items': [{'name': 'live/abcd1234', 'ready': True,
                           'bytesReceived': rx['v']}]}
    link = cloud_link.CloudLink(http=http)
    t = {'v': 100.0}
    monkeypatch.setattr(cloud_link.time, 'monotonic', lambda: t['v'])
    assert link.ingest_status()['kbps'] is None      # first sample
    rx['v'], t['v'] = 125_000, 101.0                 # +125 kB over 1 s
    assert link.ingest_status()['kbps'] == 1000


def test_version_check_uses_cloud_base():
    _paired_cfg()
    seen = []

    def http(url, **kw):
        seen.append(url)
        return {'latest': '1.2.3'}
    link = cloud_link.CloudLink(http=http)
    assert link.check_version_once() == '1.2.3'
    assert seen == ['https://cloud/api/encoder/version']


# ── preconfig ────────────────────────────────────────────────────────────────

def test_preconfig_apply_and_consume(tmp_path):
    p = tmp_path / 'playcall-encoder.json'
    p.write_text(json.dumps({
        'networks': [{'ssid': 'Home', 'psk': 'pw'},
                     {'ssid': 'Field', 'password': 'fw', 'label': 'gameday'}],
        'youtube_key': 'rtmps://a.rtmps.youtube.com/live2/pre-key',
        'cloud': {'base_url': 'https://playsigns.net/', 'api_key': 'enc_1'},
        'device_name': 'Warriors Encoder 1',
    }))
    assert config.apply_preconfig([p]) is True
    assert not p.exists()                            # consumed
    cfg = config.load()
    assert [n['ssid'] for n in cfg['networks']] == ['Home', 'Field']
    assert cfg['networks'][0]['priority'] > cfg['networks'][1]['priority']
    assert cfg['youtube']['key'] == 'pre-key'
    assert cfg['cloud'] == {'base_url': 'https://playsigns.net',
                            'api_key': 'enc_1', 'feed_url': ''}
    assert cfg['device']['name'] == 'Warriors Encoder 1'
    assert config.is_configured(cfg)                 # key+pin auto-generated
    assert cfg['device']['pin']


def test_preconfig_malformed_is_ignored(tmp_path):
    p = tmp_path / 'playcall-encoder.json'
    p.write_text('{not json')
    assert config.apply_preconfig([p]) is False
    assert not config.is_configured()
    missing = tmp_path / 'nope.json'
    assert config.apply_preconfig([missing]) is False


# ── web (smoke: PIN gate + log bundle redaction) ─────────────────────────────

def test_web_pin_gate_and_bundle(monkeypatch):
    from encoder import web
    monkeypatch.setattr(web, 'PIN_FAIL_DELAY', 0)
    cfg = _paired_cfg()
    cfg['device']['pin'] = '123456'
    cfg['youtube']['key'] = 'super-secret'
    config.save(cfg)
    app = web.create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    assert c.get('/').status_code == 302             # not authed → login
    assert c.post('/login', data={'pin': '000000'}).status_code == 200
    r = c.post('/login', data={'pin': '123456'})
    assert r.status_code == 302
    assert c.get('/').status_code == 200
    bundle = c.get('/logs/bundle').get_data(as_text=True)
    assert 'SUPPORT BUNDLE' in bundle
    assert 'super-secret' not in bundle              # secrets redacted
    assert '123456' not in bundle


def test_clip_404_reads_as_camera_not_connected(monkeypatch, tmp_path):
    """The playback server's 404 means exactly one thing — no recording
    covers the play's window, i.e. the camera was not feeding the box
    when it happened. The failure reason says that now, instead of the
    bare "HTTP Error 404: Not Found" a coach cannot act on."""
    import urllib.error
    from encoder import clipper as cl
    c = cl.Clipper(cfg_load=lambda: {},
                   status=cl.StatusWriter(tmp_path / 's.json'))
    monkeypatch.setattr(c, '_fetch_clip', lambda *a, **k: (_ for _ in ())
                        .throw(urllib.error.HTTPError(
                            'u', 404, 'Not Found', None, None)))
    seen = {}
    monkeypatch.setattr(c, '_mark_failed',
                        lambda b, k, cid, why: seen.setdefault('why', why))
    job = {'id': 'c1', 'start': 0.0, 'end': 10.0, 'not_before': 0.0,
           'label': '1B'}                    # end long past the give-up
    assert c.process_job({}, 'https://x', 'k', job, skew=0) is True
    assert 'no recording covers this play' in seen['why']
    assert 'camera' in seen['why'] and '404' not in seen['why']
    assert 'no recording covers this play' in c.status.last_error


def test_main_runs_clipper_in_process_when_unit_is_missing():
    """A box installed before the clipper unit existed never got it —
    self-update cannot write /etc/systemd/system as the service user,
    so the unit copy silently fails and every clip job sat "pending"
    forever (field report: "12 uploading" all night on the Videos
    page). The main service now supervises a clipper thread whenever
    systemd is not running one."""
    import inspect
    from encoder import __main__ as entry
    src = inspect.getsource(entry)
    assert "'playcall-encoder-clipper'" in src
    assert '_clipper.Clipper().run_forever' in src
    assert 'cutting clips in-process' in src
    # never in laptop/dev fake mode, and every boot LOGS the decision
    assert 'if not fake:' in src
    assert 'systemd runs the cutter' in src
    # a broken cutter is loud but never fatal to the box's other jobs
    assert "log.exception('in-process clipper failed to start')" in src


def test_copy_logs_button_copies_on_plain_http():
    """The settings page is served over LAN http, where
    navigator.clipboard does not exist (secure contexts only) — the old
    copyBundle always fell into its catch and bounced every phone to a
    select-all page. The execCommand fallback copies in place; the new
    tab is the LAST resort, not the first."""
    import inspect
    from encoder import web
    src = inspect.getsource(web)
    assert 'window.isSecureContext' in src           # gate the modern API
    assert "document.execCommand('copy')" in src     # the http fallback
    assert 'ta.setSelectionRange(0, t.length)' in src
    # the old page stays only as the final fallback
    assert src.count("window.open('/logs/bundle','_blank')") == 1


class _StubCloud:
    """Minimal cloud_link stand-in for web/bundle tests."""
    latest_version = None
    assignment = {'assigned': True, 'team_name': 'Warriors',
                  'youtube_rtmp_url':
                      'rtmps://a.rtmps.youtube.com/live2/live-abcd-1234'}

    def ingest_status(self):
        return {'connected': True, 'kbps': 900}

    def push_status(self):
        return {'connected': False, 'kbps': None, 'reconnects_5m': 1}


def test_bundle_redacts_planted_stream_key_and_rtmp_urls(monkeypatch):
    # Regression for the support-bundle leak: ffmpeg's stderr routinely
    # prints the full push URL (rtmps://…/live2/<KEY>) on failure. Plant it
    # in stderr_tail, the journal, and the cloud assignment — none of it may
    # reach the "Copy logs for AI help" bundle.
    from encoder import web
    cfg = _paired_cfg()
    cfg['device']['pin'] = '123456'
    cfg['youtube'] = {'url': config.DEFAULT_YOUTUBE_URL,
                      'key': 'live-abcd-1234'}
    config.save(cfg)
    ikey = cfg['local_ingest_key']                   # 'abcd1234'
    state = config.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / 'push.json').write_text(json.dumps({
        'connected': False, 'kbps': None, 'updated': time.time(),
        'reconnect_times': [], 'stderr_tail': [
            'rtmps://a.rtmps.youtube.com/live2/live-abcd-1234: '
            'Operation timed out',
            f'Input #0, flv, from rtmp://127.0.0.1:1935/live/{ikey}: err',
            'stray mention of live-abcd-1234 outside a url',
        ]}))
    monkeypatch.setattr(
        web.system, 'journal_tail',
        lambda lines=20, **kw: ['ffmpeg[99]: push to rtmps://a.rtmps.'
                                'youtube.com/live2/live-abcd-1234 failed'])
    bundle = web.log_bundle(cloud=_StubCloud())
    assert 'live-abcd-1234' not in bundle            # yt key gone everywhere
    assert ikey not in bundle                        # local ingest key gone
    assert config.REDACTED in bundle                 # visibly redacted
    assert 'Operation timed out' in bundle           # useful context kept


def test_heartbeat_log_tail_is_redacted(monkeypatch):
    _paired_cfg()
    cfg = config.load()
    cfg['youtube']['key'] = 'live-abcd-1234'
    config.save(cfg)
    monkeypatch.setattr(
        cloud_link.system, 'journal_tail',
        lambda lines=20, **kw: [
            'push start rtmps://a.rtmps.youtube.com/live2/live-abcd-1234',
            'plain line'])
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    tail = link.heartbeat_payload()['log_tail']
    assert not any('live-abcd-1234' in l for l in tail)
    assert tail[1] == 'plain line'


def test_pin_lockout_after_failures_and_compare_digest(monkeypatch):
    from encoder import web
    monkeypatch.setattr(web, 'PIN_FAIL_DELAY', 0)
    cfg = _paired_cfg()
    cfg['device']['pin'] = '123456'
    config.save(cfg)
    digest_calls = []
    real_cd = web.hmac.compare_digest
    monkeypatch.setattr(
        web.hmac, 'compare_digest',
        lambda a, b: (digest_calls.append(1), real_cd(a, b))[1])
    app = web.create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    for _ in range(web.PIN_FAIL_THRESHOLD):
        assert c.post('/login', data={'pin': '000000'}).status_code == 200
    assert len(digest_calls) == web.PIN_FAIL_THRESHOLD   # via compare_digest
    # Locked out: even the CORRECT pin is rejected, with no comparison at
    # all (the lockout check runs before any handling).
    r = c.post('/login', data={'pin': '123456'})
    assert r.status_code == 429
    assert len(digest_calls) == web.PIN_FAIL_THRESHOLD
    assert c.get('/').status_code == 302                 # still not authed
    lock = app._pin_lockout
    assert lock['fails'] == web.PIN_FAIL_THRESHOLD
    remaining = lock['until'] - time.monotonic()
    assert 0 < remaining <= web.PIN_LOCKOUT_BASE         # 5 fails → 60 s
    # Lock expired → correct PIN unlocks and resets the counter.
    lock['until'] = 0.0
    assert c.post('/login', data={'pin': '123456'}).status_code == 302
    assert lock['fails'] == 0 and lock['until'] == 0.0
    assert c.get('/').status_code == 200


def test_factory_reset_removes_nm_connections_before_unlink(monkeypatch):
    from encoder import web
    monkeypatch.setattr(web, 'PIN_FAIL_DELAY', 0)
    cfg = config.load()
    cfg['networks'] = [
        {'ssid': 'Home', 'psk': 'pw', 'priority': 100, 'label': 'home'},
        {'ssid': 'Field', 'psk': 'fw', 'priority': 90, 'label': 'gameday'}]
    config.ensure_ingest_key(cfg)
    cfg['device']['pin'] = '123456'
    config.save(cfg)
    events = []      # (cmd tuple, config file still existed at call time?)
    monkeypatch.setattr(web.system, 'have_networkmanager', lambda: True)
    monkeypatch.setattr(
        web.system, 'run',
        lambda cmd, **kw: events.append((tuple(cmd),
                                         config.config_path().exists())))
    monkeypatch.setattr(
        web.system, 'reboot',
        lambda: events.append((('reboot',), config.config_path().exists())))
    app = web.create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    assert c.post('/login', data={'pin': '123456'}).status_code == 302
    assert c.post('/factory-reset').status_code == 200
    deletes = [e for e in events
               if e[0][:3] == ('nmcli', 'connection', 'delete')]
    # Both stored networks' NM profiles deleted…
    assert {e[0][-1] for e in deletes} == {'playcall-Home', 'playcall-Field'}
    # …and every delete ran BEFORE the config was unlinked (capture order).
    assert all(existed for _, existed in deletes)
    # By reboot time the config file is gone.
    assert events[-1][0] == ('reboot',) and events[-1][1] is False


# ── self-update (the one-button "Update now") ───────────────────────────────

def _fake_release_tree(root):
    (root / 'encoder').mkdir(parents=True)
    (root / 'encoder' / '__init__.py').write_text("__version__ = '9.9.9'\n")
    (root / 'VERSION').write_text('9.9.9\n')
    (root / 'mediamtx.yml').write_text('paths:\n')
    (root / 'scripts').mkdir()
    (root / 'scripts' / 'youtube_push.sh').write_text('#!/bin/bash\n')
    (root / 'systemd').mkdir()
    (root / 'systemd' / 'playcall-encoder.service').write_text('[Unit]\n')


def test_self_update_lays_installer_payload(tmp_path, monkeypatch):
    from encoder import system
    install = tmp_path / 'opt'
    (install / 'encoder' / '__pycache__').mkdir(parents=True)
    (install / 'encoder' / '__pycache__' / 'stale.pyc').write_text('x')
    (install / 'encoder' / '__init__.py').write_text("__version__ = '1.0.0'\n")

    cloned = []

    def fake_run(cmd, **kw):
        assert cmd[:4] == ['git', 'clone', '--depth', '1']
        cloned.append(cmd[4])
        _fake_release_tree(Path(cmd[5]))

        class R:
            returncode = 0
            stderr = ''
        return R()
    monkeypatch.setattr(system, 'run', fake_run)
    ok, detail = system.self_update(install_dir=str(install))
    assert ok and detail == '9.9.9'
    assert cloned == [system.UPDATE_REPO]
    # Same payload install.sh lays: encoder/, VERSION, mediamtx.yml, scripts/
    assert (install / 'VERSION').read_text().strip() == '9.9.9'
    assert '9.9.9' in (install / 'encoder' / '__init__.py').read_text()
    assert (install / 'mediamtx.yml').exists()
    push = install / 'scripts' / 'youtube_push.sh'
    assert push.exists() and push.stat().st_mode & 0o111
    # Stale bytecode purged so old .pyc can't shadow the new tree.
    assert not (install / 'encoder' / '__pycache__').exists()


def test_self_update_reports_download_failure(tmp_path, monkeypatch):
    from encoder import system

    def fake_run(cmd, **kw):
        class R:
            returncode = 128
            stderr = 'fatal: unable to access repo'
        return R()
    monkeypatch.setattr(system, 'run', fake_run)
    install = tmp_path / 'opt'
    install.mkdir()
    ok, detail = system.self_update(install_dir=str(install))
    assert not ok and 'unable to access' in detail
    assert not (install / 'VERSION').exists()      # nothing half-laid


class _CloudStub:
    latest_version = '9.9.9'
    assignment = None

    def ingest_status(self):
        return {'connected': False, 'kbps': None}

    def push_status(self):
        return {'connected': False, 'kbps': None, 'reconnects_5m': 0}


def test_web_update_button_and_route(monkeypatch):
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    app = web.create_app(cloud=_CloudStub())
    c = app.test_client()
    # PIN-gated like every settings route.
    assert c.post('/update').status_code == 302
    c.post('/login', data={'pin': config.load()['device']['pin']})
    html = c.get('/').get_data(as_text=True)
    assert 'update 9.9.9 available' in html
    assert 'Update available' in html and 'action="/update"' in html
    assert 'Update software' in html               # maintenance fallback

    restarted = []
    monkeypatch.setattr(web.system, 'self_update', lambda: (True, '9.9.9'))
    monkeypatch.setattr(web.system, 'systemctl',
                        lambda *a: restarted.append(a))
    monkeypatch.setattr(web.time, 'sleep', lambda s: None)
    r = c.post('/update')
    assert r.status_code == 302
    assert 'msg=' in r.headers['Location'] and '9.9.9' in r.headers['Location']
    for _ in range(300):                           # restart thread finishes
        if len(restarted) == 4:
            break
        time.sleep(0.01)
    # Siblings first; the unit hosting this web app restarts LAST.
    assert restarted == [('restart', 'playcall-encoder-mediamtx'),
                         ('restart', 'playcall-encoder-youtube'),
                         ('restart', 'playcall-encoder-clipper'),
                         ('restart', 'playcall-encoder')]


def test_web_update_failure_no_restarts(monkeypatch):
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    monkeypatch.setattr(web.system, 'self_update',
                        lambda: (False, 'download timed out'))
    restarted = []
    monkeypatch.setattr(web.system, 'systemctl',
                        lambda *a: restarted.append(a))
    app = web.create_app(cloud=_CloudStub())
    c = app.test_client()
    c.post('/login', data={'pin': config.load()['device']['pin']})
    r = c.post('/update')
    assert r.status_code == 302 and 'err=' in r.headers['Location']
    time.sleep(0.05)
    assert restarted == []


def test_web_no_update_card_when_current(monkeypatch):
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])

    class Cur(_CloudStub):
        from encoder import __version__ as latest_version
    app = web.create_app(cloud=Cur())
    c = app.test_client()
    c.post('/login', data={'pin': config.load()['device']['pin']})
    html = c.get('/').get_data(as_text=True)
    assert 'Update available' not in html
    assert 'Update software' in html               # fallback always there


def test_web_never_offers_a_downgrade(monkeypatch):
    """A misconfigured cloud reporting an OLDER release (the literal field
    bug: fresh 1.2.1 box told 'update 1.0.0 available') shows no update
    card and no header nag."""
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])

    class Old(_CloudStub):
        latest_version = '1.0.0'
    app = web.create_app(cloud=Old())
    c = app.test_client()
    c.post('/login', data={'pin': config.load()['device']['pin']})
    html = c.get('/').get_data(as_text=True)
    assert 'update 1.0.0 available' not in html
    assert 'Update available' not in html
    assert 'Update software' in html          # manual reinstall still there


def test_heartbeat_carries_the_settings_pin():
    """The PIN rides the authenticated heartbeat so team staff can recover
    it from the site instead of SSHing into the box."""
    _paired_cfg()
    cfg = config.load()
    cfg['device']['pin'] = '431905'
    config.save(cfg)
    link = cloud_link.CloudLink(http=lambda *a, **k: {})
    hb = link.heartbeat_payload()
    assert hb['pin'] == '431905'


def test_recording_retention_is_a_setting_that_survives_updates(tmp_path,
                                                                monkeypatch):
    """record_hours bakes into mediamtx.yml from config — hand edits used
    to be reverted by every install/update; a setting survives them."""
    from encoder import web
    cfg = config.load()
    cfg['local_ingest_key'] = 'k1'
    cfg['record_hours'] = 72
    config.save(cfg)
    dest = config.write_mediamtx_config(config.load())
    text = dest.read_text()
    assert 'recordDeleteAfter: 72h' in text and '__RECORD_HOURS__' not in text
    # default stays 12h; junk clamps sanely
    cfg['record_hours'] = None
    config.save(cfg)
    assert 'recordDeleteAfter: 12h' in \
        config.write_mediamtx_config(config.load()).read_text()
    # the settings page saves it and restarts mediamtx
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(web.system, 'systemctl', lambda *a: calls.append(a))
    app = web.create_app()
    c = app.test_client()
    c.post('/login', data={'pin': config.load()['device']['pin']})
    r = c.post('/retention', data={'hours': '48'})
    assert r.status_code == 302
    assert config.load()['record_hours'] == 48
    assert ('restart', 'playcall-encoder-mediamtx') in calls
    html = c.get('/').get_data(as_text=True)
    assert 'Recording retention' in html and 'selected' in html


# ── one-click sign-in from the site (nonce token) ────────────────────────────

class _FakeCloud:
    """A CloudLink stand-in for the token flow: enabled, and a poll that
    delivers the nonce — optionally blowing up AFTER delivery, the way a
    real poll can when applying the assignment fails mid-request."""

    def __init__(self, nonce, explode=False):
        self._nonce = nonce
        self._explode = explode
        self.login_nonce = None
        self.polls = 0

    def enabled(self):
        return True

    def poll_assignment_once(self):
        self.polls += 1
        self.login_nonce = self._nonce
        if self._explode:
            raise RuntimeError('assignment apply died after capture')


def _pin_app(cloud=None):
    from encoder import web
    cfg = _paired_cfg()
    cfg['device']['pin'] = '123456'
    config.save(cfg)
    app = web.create_app(cloud=cloud)
    app.config['TESTING'] = True
    return app.test_client()


def test_one_click_token_forces_a_poll_and_signs_in():
    cloud = _FakeCloud('nonce-abc')
    c = _pin_app(cloud=cloud)
    r = c.get('/login?token=nonce-abc')
    assert r.status_code == 302                      # straight in, no PIN
    assert cloud.login_nonce is None                 # single use — consumed


def test_one_click_survives_a_poll_that_dies_after_delivery():
    """The nonce is captured before the assignment is applied, so an apply
    failure must not bounce the coach to the PIN form."""
    cloud = _FakeCloud('nonce-abc', explode=True)
    c = _pin_app(cloud=cloud)
    r = c.get('/login?token=nonce-abc')
    assert r.status_code == 302
    assert cloud.polls == 1


def test_a_dead_token_explains_itself_instead_of_a_silent_pin_form():
    c = _pin_app(cloud=_FakeCloud('different-nonce', explode=True))
    r = c.get('/login?token=stale-token')
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'didn’t go through' in body               # guidance, not silence


# ── the update-then-change-PIN 405 (session survives restarts) ───────────────

def test_sessions_survive_a_service_restart():
    """Real outage: sign in, self-update restarts the service, submit the
    new PIN — the fresh process minted a fresh random secret, saw you as
    signed out, and the bounce ended on a 405. The secret is now stored
    beside the config, so a cookie from before the restart still works."""
    from encoder import web
    cfg = _paired_cfg()
    cfg['device']['pin'] = '123456'
    config.save(cfg)
    c1 = web.create_app().test_client()
    assert c1.post('/login', data={'pin': '123456'}).status_code == 302
    cookie = c1.get_cookie('session')
    c2 = web.create_app().test_client()          # "restarted" process
    c2.set_cookie('session', cookie.value)
    assert c2.get('/').status_code == 200        # still signed in


def test_bounced_pin_change_lands_on_settings_not_a_405():
    c = _pin_app()
    # signed out: the POST bounces to plain login (no next= to replay)
    r = c.post('/pin', data={'pin': '9999', 'pin2': '9999'})
    assert r.status_code == 302 and 'next' not in r.headers['Location']
    # a stale GET /pin (old next= link) goes to the settings page
    c.post('/login', data={'pin': '123456'})
    r = c.get('/pin')
    assert r.status_code == 302 and r.headers['Location'].endswith('/')


# ── recording-storage watchdog ───────────────────────────────────────────────
# The outage that motivated this: mid-season, the NVMe controller dropped
# off the PCIe bus under write load, ext4 went emergency-read-only, and
# the box streamed a whole game into a dead disk while every card said
# "pushing". Nothing was recorded, no clips cut, no alarm anywhere.

def test_storage_status_healthy_and_probe_cleans_up(tmp_path):
    from encoder import system
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is True and st['error'] == ''
    assert st['read_only'] is False
    assert st['free_gb'] and st['free_gb'] > 0
    assert list(tmp_path.iterdir()) == []        # probe file removed


def test_storage_status_missing_mount_is_a_failure(tmp_path):
    from encoder import system
    st = system.storage_status(str(tmp_path / 'never-mounted'))
    assert st['ok'] is False
    assert 'missing' in st['error']


def test_storage_status_write_error_is_a_failure(tmp_path, monkeypatch):
    """A dead controller answers EIO even while the mount still says rw."""
    import errno
    import os as os_mod
    from encoder import system

    def eio(fd):
        raise OSError(errno.EIO, 'Input/output error')
    monkeypatch.setattr(os_mod, 'fsync', eio)
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is False
    assert 'cannot write' in st['error']


def test_storage_status_hanging_write_is_a_failure(tmp_path, monkeypatch):
    """The other way drives die: the write never returns. The probe runs
    on a helper thread so the heartbeat loop reporting the failure can
    never itself be wedged by it."""
    from encoder import system
    monkeypatch.setattr(system, 'PROBE_TIMEOUT', 0.05)
    monkeypatch.setattr(system, '_probe_write',
                        lambda path, result: time.sleep(0.4))
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is False and 'hanging' in st['error']
    # while the stuck probe is still out there, the next check reports
    # the hang immediately instead of stacking another thread on it
    st2 = system.storage_status(str(tmp_path))
    assert st2['ok'] is False and 'hanging' in st2['error']
    system._probe_thread.join(1.0)               # let it drain


def test_mounted_read_only_sees_emergency_ro(tmp_path):
    """Real /proc/mounts capture from the failure: the options still led
    with rw — ext4 flags the shutdown as emergency_ro,shutdown instead of
    flipping ro, so matching 'ro' alone misses it."""
    from encoder import system
    mounts = tmp_path / 'mounts'
    data = tmp_path / 'data'
    data.mkdir()
    mounts.write_text(
        '/dev/root / ext4 rw,noatime 0 0\n'
        f'/dev/nvme0n1p1 {data} ext4 rw,noatime,emergency_ro,shutdown 0 0\n')
    assert system._mounted_read_only(str(data), mounts=str(mounts)) is True
    mounts.write_text(
        '/dev/root / ext4 rw,noatime 0 0\n'
        f'/dev/nvme0n1p1 {data} ext4 rw,noatime 0 0\n')
    assert system._mounted_read_only(str(data), mounts=str(mounts)) is False
    # plain ro (post-reboot mount -o ro rescue state) also counts
    mounts.write_text(f'/dev/nvme0n1p1 {data} ext4 ro,noatime 0 0\n')
    assert system._mounted_read_only(str(data), mounts=str(mounts)) is True


def test_settings_page_screams_about_dead_storage(monkeypatch):
    """The card must be loud when the disk is gone — and silent when not."""
    from encoder import system, web
    c = _pin_app()
    c.post('/login', data={'pin': '123456'})
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': False, 'read_only': True, 'free_gb': None,
        'path': '/var/lib/playcall-encoder',
        'error': 'filesystem is read-only — the kernel shut it down '
                 'after an I/O error (failing drive?)'})
    html = c.get('/').get_data(as_text=True)
    assert 'Recording storage failure' in html
    assert 'read-only' in html
    assert 'Nothing is being recorded' in html
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': True, 'read_only': False, 'free_gb': 440.5, 'path': '/x',
        'error': ''})
    html = c.get('/').get_data(as_text=True)
    assert 'Recording storage failure' not in html
    assert 'Recording disk' in html and '440.5' in html


def test_heartbeat_carries_storage_failure(monkeypatch):
    """The site's encoder card and Field check read this field — it must
    ride every beat, dead or alive."""
    from encoder import system
    _paired_cfg()
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': False, 'read_only': False, 'free_gb': None, 'path': '/v',
        'error': 'cannot write to /v: Input/output error'})
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    hb = link.heartbeat_payload()
    assert hb['storage']['ok'] is False
    assert 'Input/output error' in hb['storage']['error']


def test_support_bundle_has_a_storage_section(monkeypatch):
    from encoder import system, web
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': False, 'read_only': False, 'free_gb': None, 'path': '/v',
        'error': 'a write to /v is hanging — the drive is not answering'})
    bundle = web.log_bundle()
    assert '── storage ──' in bundle
    assert 'hanging' in bundle
