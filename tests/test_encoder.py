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
                       'version', 'log_tail', 'hostname', 'ip', 'clips'}
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
