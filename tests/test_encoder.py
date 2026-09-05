"""Unit tests for the PlayCall Encoder package (laptop-runnable:
no root, no network — see conftest.py sandbox fixture)."""

import json
import threading
import time
from pathlib import Path

from encoder import cloud_link, config, provisioning


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
    config.save({'record_hours': 3, 'extra': {'x': 1}})
    cfg = config.load()
    assert cfg['record_hours'] == 3
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
        # the box names the angle it wants to publish under (Multi-View)
        assert url == 'https://cloud/api/encoder/assignment?angle=main'
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
                       'pin', 'rtmp_urls', 'radar', 'ble_radar', 'temp_max', 'storage',
                       'transcode'}
    # a Pi (no hardware encoder) reports not-capable, copy target
    assert hb['transcode'] == {'capable': False, 'hevc': False,
                               'target_kbps': 0, 'codec': 'h264'}
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
    (root / 'comms').mkdir()
    (root / 'comms' / 'comms_ear.py').write_text('# ear\n')
    (root / 'comms' / 'install_comms.sh').write_text('#!/bin/bash\n')
    (root / 'comms' / 'README.md').write_text('# comms\n')


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
    # 🎧 comms is bundled — the ear's code updates with the encoder
    ear = install / 'comms' / 'comms_ear.py'
    assert ear.exists() and ear.stat().st_mode & 0o111
    assert (install / 'comms' / 'install_comms.sh').exists()


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
        if len(restarted) == 6:
            break
        time.sleep(0.01)
    # Siblings first (comms rides along); this web app's unit LAST.
    assert restarted == [('restart', 'playcall-encoder-mediamtx'),
                         ('restart', 'playcall-encoder-youtube'),
                         ('restart', 'playcall-encoder-clipper'),
                         ('restart', 'playcall-encoder-live'),
                         ('restart', 'playcall-comms'),
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


# ── the recordings drive that isn't mounted ──────────────────────────────────
# Sequel to the outage above. Once the drive was healthy again, the box
# reported storage "ok" with 113.9 GB free — on a 477 GB drive holding 6 GB.
# The fstab line had been commented out during the outage and never
# restored, so /var/lib/playcall-encoder was a plain directory on the SD
# card: every write succeeded, the probe went green, and the encoder was
# quietly filling its root filesystem with 72 hours of footage.

def test_storage_flags_a_recordings_drive_that_is_not_mounted(tmp_path,
                                                              monkeypatch):
    from encoder import system
    monkeypatch.setattr(system, '_labeled_device',
                        lambda label=None: '/dev/nvme0n1p1')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/mmcblk0p2')
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is False
    assert st['device'] == '/dev/mmcblk0p2'
    assert 'not mounted' in st['error'] and 'SD card' in st['error']


def test_storage_is_happy_when_the_labeled_drive_is_the_mounted_one(
        tmp_path, monkeypatch):
    from encoder import system
    monkeypatch.setattr(system, '_labeled_device',
                        lambda label=None: '/dev/nvme0n1p1')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/nvme0n1p1')
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is True and st['error'] == ''
    assert st['device'] == '/dev/nvme0n1p1'


def test_an_sd_card_only_box_is_not_nagged(tmp_path, monkeypatch):
    """No recordings drive fitted at all is a supported install (12 h of
    retention on the SD card) — it must stay quiet, not cry wolf."""
    from encoder import system
    monkeypatch.setattr(system, '_labeled_device', lambda label=None: '')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/mmcblk0p2')
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is True and st['error'] == ''


def test_mount_device_picks_the_longest_prefix(tmp_path):
    """/ and the recordings mount both match the path — the deeper mount
    is the one actually backing it."""
    from encoder import system
    mounts = tmp_path / 'mounts'
    data = tmp_path / 'data'
    data.mkdir()
    mounts.write_text(
        '/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n'
        f'/dev/nvme0n1p1 {data} ext4 rw,noatime 0 0\n')
    assert system._mount_device(str(data), mounts=str(mounts)) \
        == '/dev/nvme0n1p1'
    # unmounted: the path falls back to whatever holds it — the SD card
    mounts.write_text('/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n')
    assert system._mount_device(str(data), mounts=str(mounts)) \
        == '/dev/mmcblk0p2'


def test_units_exempt_restart_loops_in_the_section_systemd_reads():
    """systemd parses StartLimitIntervalSec in [Unit]; in [Service] it is
    ignored with a warning ("Unknown key ... in section [Service]") and
    the Restart=always units were never actually exempt."""
    import configparser
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / 'systemd'
    units = sorted(root.glob('playcall-encoder*.service'))
    assert units
    for u in units:
        cp = configparser.ConfigParser(strict=False)
        cp.optionxform = str
        cp.read(u)
        if not cp.has_option('Service', 'Restart'):
            continue
        assert not cp.has_option('Service', 'StartLimitIntervalSec'), u.name
        assert cp.has_option('Unit', 'StartLimitIntervalSec'), u.name


# ── the disk failure nobody could hear ───────────────────────────────────────
# The drive died mid-morning, the box recorded nothing for an entire
# afternoon game, and the only trace of it anywhere was MediaMTX's own
# mkdir errors. The alarm worked — but it only spoke through the heartbeat
# and the settings page, and the box was off the internet at the field with
# nobody looking at a settings page. It has to reach the journal too.

def test_a_dead_recording_disk_is_logged_loudly(monkeypatch, caplog):
    import logging
    from encoder import system
    _paired_cfg()
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': False, 'read_only': False, 'free_gb': None, 'path': '/v',
        'device': '/dev/mmcblk0p2',
        'error': 'cannot write to /v: Input/output error'})
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    with caplog.at_level(logging.WARNING, logger='cloud_link'):
        link.watch_storage()
    assert 'RECORDING DISK FAILURE' in caplog.text
    assert 'Input/output error' in caplog.text
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_the_failure_is_logged_once_not_every_beat(monkeypatch, caplog):
    """15 s of logging for the hours a drive stays dead buries itself —
    and buries whatever else happened that game."""
    import logging
    from encoder import system
    _paired_cfg()
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': False, 'read_only': True, 'free_gb': None, 'path': '/v',
        'device': '', 'error': 'filesystem is read-only'})
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    with caplog.at_level(logging.WARNING, logger='cloud_link'):
        for _ in range(5):
            link.watch_storage()
    assert caplog.text.count('RECORDING DISK FAILURE') == 1


def test_recovery_is_logged_too(monkeypatch, caplog):
    import logging
    from encoder import system
    _paired_cfg()
    state = {'ok': False}
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': state['ok'], 'read_only': False, 'free_gb': 440.0,
        'path': '/v', 'device': '/dev/nvme0n1p1',
        'error': '' if state['ok'] else 'cannot write'})
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    with caplog.at_level(logging.WARNING, logger='cloud_link'):
        link.watch_storage()
        state['ok'] = True
        link.watch_storage()
    assert 'RECORDING DISK FAILURE' in caplog.text
    assert 'writable again' in caplog.text


def test_the_beat_reuses_the_watchers_probe(monkeypatch):
    """The watcher and the heartbeat must not each write a probe file
    every cycle — one probe per beat is enough."""
    from encoder import system
    _paired_cfg()
    calls = {'n': 0}

    def probe(path=None):
        calls['n'] += 1
        return {'ok': True, 'read_only': False, 'free_gb': 440.0,
                'path': '/v', 'device': '/dev/nvme0n1p1', 'error': ''}
    monkeypatch.setattr(system, 'storage_status', probe)
    link = cloud_link.CloudLink(http=lambda url, **kw: {'items': []})
    link.watch_storage()
    hb = link.heartbeat_payload()
    assert calls['n'] == 1                  # the beat reused it
    assert hb['storage']['ok'] is True


def test_the_watcher_runs_on_a_box_that_never_reaches_the_cloud(monkeypatch):
    """The offline box at the field is precisely the one whose disk
    failure would otherwise go unrecorded, so the watch must not sit
    behind the paired/reachable check."""
    from encoder import system
    seen = []
    monkeypatch.setattr(system, 'storage_status',
                        lambda path=None: seen.append(1) or {
                            'ok': True, 'read_only': False, 'free_gb': 1.0,
                            'path': '/v', 'device': '', 'error': ''})
    link = cloud_link.CloudLink(http=lambda *a, **kw: {})
    link.enabled = lambda: False            # unpaired / no cloud
    link.running = True

    def stop(_):
        link.running = False
    monkeypatch.setattr(cloud_link.time, 'sleep', stop)
    link.heartbeat_loop()
    assert seen                             # probed anyway


# ── recording to the SD card on purpose ──────────────────────────────────────
# With the NVMe out for repair the box still has to work, and the SD card
# holds ~12 h of footage. But the unmounted-drive check would then scream
# on every page load forever, and an alarm that is always on is one nobody
# reads — which is exactly what this whole feature exists to prevent.

def test_the_sd_fallback_is_a_failure_until_somebody_says_otherwise():
    from encoder import system
    cfg = config.load()
    cfg['record_fallback_ok'] = False
    config.save(cfg)
    assert system._fallback_ok() is False


def test_a_declared_fallback_reports_where_it_landed_instead_of_failing(
        tmp_path, monkeypatch):
    from encoder import system
    cfg = config.load()
    cfg['record_fallback_ok'] = True
    config.save(cfg)
    monkeypatch.setattr(system, '_labeled_device',
                        lambda label=None: '/dev/nvme0n1p1')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/mmcblk0p2')
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is True and st['error'] == ''
    assert st['fallback'] is True
    assert st['device'] == '/dev/mmcblk0p2'
    # still a real probe: a fallback that cannot be written to is a failure
    assert st['free_gb'] is not None


def test_a_declared_fallback_still_fails_when_the_card_is_unwritable(
        tmp_path, monkeypatch):
    """Saying 'the SD card is fine' must not switch the check off."""
    import errno
    import os as os_mod
    from encoder import system
    cfg = config.load()
    cfg['record_fallback_ok'] = True
    config.save(cfg)
    monkeypatch.setattr(system, '_labeled_device',
                        lambda label=None: '/dev/nvme0n1p1')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/mmcblk0p2')
    monkeypatch.setattr(os_mod, 'fsync', lambda fd: (_ for _ in ()).throw(
        OSError(errno.EIO, 'Input/output error')))
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is False and 'cannot write' in st['error']


def test_the_drive_being_back_is_not_a_fallback(tmp_path, monkeypatch):
    """Flag left on after the NVMe returns must not mislabel a healthy
    box — the mount matching is what decides, not the flag."""
    from encoder import system
    cfg = config.load()
    cfg['record_fallback_ok'] = True
    config.save(cfg)
    monkeypatch.setattr(system, '_labeled_device',
                        lambda label=None: '/dev/nvme0n1p1')
    monkeypatch.setattr(system, '_mount_device',
                        lambda path, mounts=None: '/dev/nvme0n1p1')
    st = system.storage_status(str(tmp_path))
    assert st['ok'] is True and st['fallback'] is False


def test_the_settings_page_shows_the_fallback_in_amber_not_red(monkeypatch):
    from encoder import system
    c = _pin_app()
    c.post('/login', data={'pin': '123456'})
    monkeypatch.setattr(system, 'storage_status', lambda path=None: {
        'ok': True, 'read_only': False, 'free_gb': 107.0, 'error': '',
        'path': '/var/lib/playcall-encoder', 'device': '/dev/mmcblk0p2',
        'fallback': True})
    html = c.get('/').get_data(as_text=True)
    assert 'Recording storage failure' not in html      # no red banner
    assert 'SD card' in html and 'fallback' in html
    assert 'dot warn' in html


def test_api_errors_quote_what_the_server_said():
    """A support bundle read 'HTTP Error 400: Bad Request' for a response
    whose body said exactly what was wrong. The throttled upload path had
    always quoted the body; this is the path a box with no bandwidth cap
    uses, which is most of them."""
    import io
    import json as _json
    import urllib.error
    import urllib.request
    from encoder.clipper import Clipper

    def _raise(body, code=400):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError('http://x/api', code, 'Bad Request',
                                         {}, io.BytesIO(body))
        return boom

    c = Clipper()
    real = urllib.request.urlopen
    try:
        urllib.request.urlopen = _raise(_json.dumps(
            {'error': 'the encoder sent an empty clip'}).encode())
        try:
            c._api('http://x', 'k', '/api/pi/clips/jobs')
            assert False, 'should have raised'
        except urllib.error.HTTPError as e:
            # SAME class and code, so the poll loop's "cloud unreachable"
            # branch and the 404 no-recording check both still work
            assert e.code == 400
            assert 'the encoder sent an empty clip' in str(e)
        # a non-JSON body still beats the status line
        urllib.request.urlopen = _raise(b'nginx: request entity too large')
        try:
            c._api('http://x', 'k', '/api/pi/clips/jobs')
        except urllib.error.HTTPError as e:
            assert 'entity too large' in str(e)
        # …and an empty body falls back to the original error untouched
        urllib.request.urlopen = _raise(b'')
        try:
            c._api('http://x', 'k', '/api/pi/clips/jobs')
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        urllib.request.urlopen = real


# ── the transcode ladder ─────────────────────────────────────────────────────
# The whole reason to run this box on an N100/N150: the camera sends
# 1080p at 10 Mbps, the LOCAL recording keeps every bit of it (MediaMTX
# records the camera's own stream — clips stay crisp), and QuickSync
# hands YouTube 2–4 Mbps the field uplink can actually carry. A Pi 5
# owns no video encoder, so the same setting there degrades to copy
# with a log line — never an ffmpeg error loop.

def test_no_bitrate_means_copy_everywhere():
    from encoder import youtube_push as yp
    assert yp.push_bitrate({'push_bitrate_kbps': 0}, hw='vaapi') == 0
    assert yp.push_bitrate({}, hw='vaapi') == 0
    cmd = yp.build_ffmpeg_cmd({'local_ingest_key': 'k', 'push_bitrate_kbps': 0},
                              'aac', 'rtmp://yt/x', hw='vaapi')
    assert ['-c:v', 'copy'] == cmd[cmd.index('-c:v'):cmd.index('-c:v') + 2]


def test_a_bitrate_on_capable_hardware_transcodes():
    from encoder import youtube_push as yp
    cfg = {'local_ingest_key': 'k', 'push_bitrate_kbps': 3000}
    assert yp.push_bitrate(cfg, hw='vaapi') == 3000
    cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x', hw='vaapi')
    assert 'h264_vaapi' in cmd
    assert '-b:v' in cmd and cmd[cmd.index('-b:v') + 1] == '3000k'
    assert cmd[cmd.index('-maxrate') + 1] == '3000k'
    assert cmd[cmd.index('-bufsize') + 1] == '6000k'
    assert '-vaapi_device' in cmd
    # audio path untouched by the video ladder
    assert ['-c:a', 'copy'] == cmd[cmd.index('-c:a'):cmd.index('-c:a') + 2]


def test_a_bitrate_on_a_pi_degrades_to_copy():
    """Fleet-synced setting on a box with no encoder: right thing, no
    error loop."""
    from encoder import youtube_push as yp
    cfg = {'local_ingest_key': 'k', 'push_bitrate_kbps': 3000}
    assert yp.push_bitrate(cfg, hw='') == 0
    cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x', hw='')
    assert 'h264_vaapi' not in cmd and '-c:v' in cmd
    assert cmd[cmd.index('-c:v') + 1] == 'copy'


def test_bitrate_is_clamped_to_sanity():
    from encoder import youtube_push as yp
    assert yp.push_bitrate({'push_bitrate_kbps': 50}, hw='vaapi') == 1000
    assert yp.push_bitrate({'push_bitrate_kbps': 99999}, hw='vaapi') == 12000
    assert yp.push_bitrate({'push_bitrate_kbps': 'junk'}, hw='vaapi') == 0


def test_transcode_keeps_the_opus_audio_path():
    """Phone audio (Opus) still comes in over RTSP and transcodes to
    AAC — the video ladder must not disturb that fork."""
    from encoder import youtube_push as yp
    cfg = {'local_ingest_key': 'k', 'push_bitrate_kbps': 2500}
    cmd = yp.build_ffmpeg_cmd(cfg, 'opus', 'rtmp://yt/x', hw='vaapi')
    assert '-rtsp_transport' in cmd and 'h264_vaapi' in cmd
    assert 'aac' in cmd


def test_the_cloud_can_set_and_unset_push_quality(monkeypatch):
    """The site's selector rides the assignment poll: an int applies
    (and restarts the push), 0 turns transcoding back off, and None —
    an older cloud — leaves the box's local setting alone."""
    _paired_cfg()
    cmds = []
    link = cloud_link.CloudLink(
        on_feed_change=lambda f: None,
        runner=lambda cmd, **kw: cmds.append(cmd),
        http=lambda *a, **kw: {})
    base = {'assigned': True, 'team_id': 't1', 'team_name': 'W',
            'bug_feed_url': 'https://cloud/bug.json',
            'youtube_rtmp_url': 'rtmps://a.rtmps.youtube.com/live2/kkk',
            'game_id': None}
    assert link.handle_assignment(dict(base, push_bitrate_kbps=3000))
    assert config.load()['push_bitrate_kbps'] == 3000
    assert any('restart' in c for c in cmds)
    cmds.clear()
    # same value again → no change, no restart
    assert link.handle_assignment(dict(base, push_bitrate_kbps=3000)) is False
    assert cmds == []
    # 0 is authoritative: back to source copy
    assert link.handle_assignment(dict(base, push_bitrate_kbps=0))
    assert config.load()['push_bitrate_kbps'] == 0
    assert any('restart' in c for c in cmds)
    cmds.clear()
    # None (older cloud) leaves the local setting alone
    cfg = config.load()
    cfg['push_bitrate_kbps'] = 2500
    config.save(cfg)
    link.handle_assignment(dict(base))
    assert config.load()['push_bitrate_kbps'] == 2500


def test_hw_encoder_requires_a_real_test_encode(monkeypatch):
    """h264_vaapi being COMPILED INTO ffmpeg proves nothing: the free
    intel-media driver on N100/N150-class iGPUs decodes but exposes no
    encode entrypoint, and the push would die mid-stream. hw_encoder()
    must run an actual test encode and believe only its exit code."""
    from encoder import system

    class _R:
        pass

    def runner(probe_rc):
        def _run(cmd, **kw):
            r = _R()
            if '-encoders' in cmd:
                r.stdout, r.returncode = 'V..... h264_vaapi', 0
            else:                       # the five-frame probe encode
                assert 'h264_vaapi' in cmd and 'lavfi' in cmd
                r.stdout, r.returncode = '', probe_rc
            return r
        return _run

    monkeypatch.setattr(system, 'fake_mode', lambda: False)
    monkeypatch.setattr(system.os.path, 'exists', lambda p: True)
    # free driver: encoder listed, probe encode FAILS -> copy mode
    monkeypatch.setattr(system, '_HW_ENCODER', None)
    monkeypatch.setattr(system, '_HW_ENCODERS', None)
    monkeypatch.setattr(system, 'run', runner(probe_rc=1))
    assert system.hw_encoder() == ''
    # non-free driver: probe encode succeeds -> vaapi (and cached)
    monkeypatch.setattr(system, '_HW_ENCODER', None)
    monkeypatch.setattr(system, '_HW_ENCODERS', None)
    monkeypatch.setattr(system, 'run', runner(probe_rc=0))
    assert system.hw_encoder() == 'vaapi'
    assert system.hw_encoder() == 'vaapi'


def test_upload_cap_lifts_when_no_camera_is_publishing(monkeypatch):
    """The 250 KB/s upload cap exists so a mid-game upload never fights
    the live push for the uplink. Hours after the game the cap was
    still on and a 50-clip drain took all evening — the cap must apply
    only while a camera is actually publishing into MediaMTX."""
    import json as _json
    import io
    import urllib.request as _rq
    from encoder import clipper as clip_mod

    svc = clip_mod.Clipper(cfg_load=lambda: {})
    calls = []

    def fake_open(url, timeout=0):
        calls.append(url)

        class _R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R(_json.dumps(
            {'items': [{'name': 'live/x', 'ready': fake_open.ready}]}
        ).encode())

    fake_open.ready = False
    monkeypatch.setattr(_rq, 'urlopen', fake_open)
    # idle box → not live → the plain full-speed upload path is chosen
    assert svc._ingest_live(now=1000.0) is False
    # cached: a second ask inside 10 s costs no API call
    n = len(calls)
    assert svc._ingest_live(now=1005.0) is False and len(calls) == n
    # camera starts publishing → live again after the cache expires
    fake_open.ready = True
    assert svc._ingest_live(now=1011.0) is True
    # MediaMTX unreachable = the push cannot be running either

    def boom(url, timeout=0):
        raise OSError('down')
    monkeypatch.setattr(_rq, 'urlopen', boom)
    assert svc._ingest_live(now=1022.0) is False
    # and _throttled_upload consults it before choosing the slow path
    import inspect
    src = inspect.getsource(clip_mod.Clipper._throttled_upload)
    assert 'not UPLOAD_BPS or not self._ingest_live()' in src


def test_hw_encoders_prove_hevc_with_encode_and_flv_mux(monkeypatch):
    """HEVC capability means TWO proven facts: the silicon encodes it
    AND this ffmpeg can mux it into flv (enhanced RTMP is the whole
    point). The probe therefore writes flv to /dev/null — an ffmpeg too
    old to carry HEVC-in-flv (the Pi's Bookworm 5.1) fails that step
    and the box honestly reports h264-only."""
    from encoder import system

    class _R:
        pass

    def runner(h264_rc, hevc_rc):
        def _run(cmd, **kw):
            r = _R()
            if '-encoders' in cmd:
                r.stdout, r.returncode = 'V. h264_vaapi\nV. hevc_vaapi', 0
            elif 'hevc_vaapi' in cmd:
                assert cmd[-1] == '/dev/null' and 'flv' in cmd
                r.stdout, r.returncode = '', hevc_rc
            else:
                r.stdout, r.returncode = '', h264_rc
            return r
        return _run

    monkeypatch.setattr(system, 'fake_mode', lambda: False)
    monkeypatch.setattr(system.os.path, 'exists', lambda p: True)
    monkeypatch.setattr(system, '_HW_ENCODERS', None)
    monkeypatch.setattr(system, '_HW_ENCODER', None)
    monkeypatch.setattr(system, 'run', runner(0, 0))
    assert system.hw_encoders() == {'h264': True, 'hevc': True}
    assert system.hw_encoder() == 'vaapi'
    monkeypatch.setattr(system, '_HW_ENCODERS', None)
    monkeypatch.setattr(system, '_HW_ENCODER', None)
    monkeypatch.setattr(system, 'run', runner(0, 1))
    assert system.hw_encoders() == {'h264': True, 'hevc': False}
    assert system.hw_encoder() == 'vaapi'


def test_hevc_push_only_when_asked_and_proven():
    """push_codec=hevc is honored only on a box that PROVED hevc; every
    other combination lands on H.264 (or copy) — never an error loop."""
    from encoder import youtube_push as yp
    both = {'h264': True, 'hevc': True}
    h264only = {'h264': True, 'hevc': False}
    cfg = {'local_ingest_key': 'k', 'push_bitrate_kbps': 3000,
           'push_codec': 'hevc'}
    assert yp.push_codec(cfg, caps=both) == 'hevc'
    assert yp.push_codec(cfg, caps=h264only) == 'h264'      # degrade
    assert yp.push_codec({'push_codec': 'h264'}, caps=both) == 'h264'
    assert yp.push_codec({}, caps=both) == 'h264'           # default
    cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x',
                              hw='vaapi', caps=both)
    assert 'hevc_vaapi' in cmd and 'flv' in cmd
    cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x',
                              hw='vaapi', caps=h264only)
    assert 'h264_vaapi' in cmd and 'hevc_vaapi' not in cmd
    # copy mode ignores the codec choice entirely
    cmd = yp.build_ffmpeg_cmd({'local_ingest_key': 'k',
                               'push_codec': 'hevc'},
                              'aac', 'rtmp://yt/x', hw='vaapi', caps=both)
    assert 'copy' in cmd and 'hevc_vaapi' not in cmd


def test_the_cloud_can_set_the_push_codec(monkeypatch):
    """Same contract as the bitrate: None = no cloud opinion, a valid
    value applies and restarts the push, junk is ignored."""
    _paired_cfg()
    cmds = []
    link = cloud_link.CloudLink(
        on_feed_change=lambda f: None,
        runner=lambda cmd, **kw: cmds.append(cmd),
        http=lambda *a, **kw: {})
    base = {'assigned': True, 'team_id': 't1', 'team_name': 'W',
            'bug_feed_url': 'https://cloud/bug.json',
            'youtube_rtmp_url': 'rtmps://a.rtmps.youtube.com/live2/kkk',
            'game_id': None}
    assert link.handle_assignment(dict(base, push_codec='hevc'))
    assert config.load()['push_codec'] == 'hevc'
    assert any('restart' in c for c in cmds)
    cmds.clear()
    # junk never lands
    link.handle_assignment(dict(base, push_codec='av1'))
    assert config.load()['push_codec'] == 'hevc'
    # back to the safe default
    assert link.handle_assignment(dict(base, push_codec='h264'))
    assert config.load()['push_codec'] == 'h264'
    # None (older cloud) leaves the local setting alone
    cfg = config.load()
    cfg['push_codec'] = 'hevc'
    config.save(cfg)
    link.handle_assignment(dict(base))
    assert config.load()['push_codec'] == 'hevc'


def _settings_client(monkeypatch, cloud=None):
    from encoder import web
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    monkeypatch.setattr(web, 'comms_status',
                        lambda: {'state': 'absent', 'buds': []})
    calls = []
    monkeypatch.setattr(web.system, 'systemctl',
                        lambda *a: calls.append(a))
    monkeypatch.setattr(web.time, 'sleep', lambda s: None)
    provisioning.headless_setup()
    cfg = config.load()
    cfg['device']['pin'] = '123456'
    config.save(cfg)
    app = web.create_app(cloud=cloud)
    app.config['TESTING'] = True
    c = app.test_client()
    c.post('/login', data={'pin': '123456'})
    return c, calls


def _wait_for(calls, want, tries=100):
    import time as _t
    for _ in range(tries):
        if want in calls:
            return True
        _t.sleep(0.02)
    return False


def test_radar_settings_save_from_the_page(monkeypatch):
    """The settings page owns the radar knobs now: capture on/off, gun
    baud, the Bluetooth adapter MAC, board output — saved to config,
    the rfcomm binder re-run when the MAC changes, and the encoder
    service restarted so the radar loop reopens on the new settings."""
    c, calls = _settings_client(monkeypatch)
    r = c.post('/radar', data={'enabled': 'auto', 'baud': '9600',
                               'bluetooth_mac': 'aa:bb:cc:dd:ee:ff',
                               'display_format': 'raw'})
    assert r.status_code == 302 and 'err=' not in r.headers['Location']
    rd = config.load()['radar']
    assert rd['baud'] == 9600
    assert rd['bluetooth_mac'] == 'AA:BB:CC:DD:EE:FF'
    assert rd['display_format'] == 'raw'
    assert ('restart', 'playcall-encoder-radarbt') in calls
    assert _wait_for(calls, ('restart', 'playcall-encoder'))
    # junk MAC is refused with a human sentence, nothing saved
    r = c.post('/radar', data={'bluetooth_mac': 'not-a-mac'})
    assert 'err=' in r.headers['Location']
    assert config.load()['radar']['bluetooth_mac'] == 'AA:BB:CC:DD:EE:FF'


def test_forget_learned_cables_clears_the_pins(monkeypatch):
    c, calls = _settings_client(monkeypatch)
    cfg = config.load()
    cfg['radar'] = {'port': '/dev/serial/by-id/usb-x', 'display_port':
                    '/dev/serial/by-id/usb-y', 'baud': 19200}
    config.save(cfg)
    r = c.post('/radar/forget')
    assert r.status_code == 302
    rd = config.load()['radar']
    assert 'port' not in rd and 'display_port' not in rd
    assert rd['baud'] == 19200                    # the rest survives
    assert _wait_for(calls, ('restart', 'playcall-encoder'))


def test_settings_page_shows_radar_and_comms_cards(monkeypatch):
    from encoder import web

    class _Cloud:
        assignment = None
        latest_version = None

        def ingest_status(self):
            return {'connected': False, 'kbps': None}

        def push_status(self):
            return {'connected': False, 'kbps': None, 'reconnects_5m': 0}

        def radar_health(self):
            return {'connected': True, 'port': '/dev/rfcomm0',
                    'baud': 19200, 'gun_heard_s': 4.2, 'learned': True,
                    'pin_overridden': False, 'lines': 10, 'parsed': 9}
    c, _calls = _settings_client(monkeypatch, cloud=_Cloud())
    html = c.get('/').get_data(as_text=True)
    assert '🔫 Radar' in html and 'listening on /dev/rfcomm0' in html
    assert 'cables learned ✓' in html
    assert 'Forget learned cables' in html
    assert '🎧 Coach comms' in html
    assert 'install_comms.sh' in html             # absent → install hint


def test_comms_status_reads_systemd_and_buds(monkeypatch):
    from encoder import web

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def fake_run(cmd, **kw):
        if cmd[:2] == ['systemctl', 'cat']:
            return _R(0, 'unit file')
        if cmd[:2] == ['systemctl', 'is-active']:
            return _R(0, 'active\n')
        if cmd[0] == 'bluetoothctl':
            return _R(0, 'Device AA:BB:CC:DD:EE:FF Catcher buds\n'
                         'garbage line\n')
        raise AssertionError(cmd)
    monkeypatch.setattr(web.system, 'run', fake_run)
    st = web.comms_status()
    assert st['state'] == 'active'
    assert st['buds'] == [{'mac': 'AA:BB:CC:DD:EE:FF',
                           'name': 'Catcher buds'}]

    def fake_run_absent(cmd, **kw):
        return _R(4, '')
    monkeypatch.setattr(web.system, 'run', fake_run_absent)
    assert web.comms_status() == {'state': 'absent', 'buds': []}


def test_comms_is_bundled_with_the_encoder():
    """One box, one install: the installer lays the comms payload and —
    on a PAIRED box — runs the comms installer too, never failing the
    encoder install over it. Updates restart playcall-comms before the
    encoder itself (harmlessly absent on boxes without it)."""
    import os as _os
    from encoder import system
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    sh = open(_os.path.join(root, 'install.sh')).read()
    assert 'comms/install_comms.sh' in sh
    assert '$SRC/comms' in sh
    assert 'comms install did not finish — the encoder is unaffected' in sh
    units = list(system.UPDATE_UNITS)
    assert 'playcall-comms' in units
    assert units.index('playcall-comms') < units.index('playcall-encoder')


def test_installer_wakes_wired_ports_the_os_never_configured():
    """Debian configures ONLY the interface used during install. A box
    installed over Wi-Fi boots with a dead Ethernet port — the field
    report blamed three cables before the OS. The installer adds an
    allow-hotplug DHCP stanza for any wired port with NO config at all,
    touches nothing that has one, and stays out of the way wherever
    NetworkManager or systemd-networkd is in charge."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    sh = open(_os.path.join(root, 'install.sh')).read()
    assert 'allow-hotplug %s\\niface %s inet dhcp' in sh
    assert '/etc/network/interfaces.d/playcall-' in sh
    assert 'NetworkManager' in sh and 'systemd-networkd' in sh
    # additive only: a port mentioned ANYWHERE in the config is skipped
    assert 'grep -rqsw "$ifc" /etc/network/interfaces' in sh
    # and an empty port's DHCP wait must not stall the install
    assert '(ifup "$ifc" >/dev/null 2>&1 || true) &' in sh


def test_comms_manager_embeds_without_a_second_pin(monkeypatch):
    """One PIN per box: the settings page mints a 5-minute pass (hmac
    of the shared activation key over a time bucket — see
    comms_ear._enc_token_ok) and embeds the whole comms manager
    pre-signed-in. No key paired yet → no token, plain link."""
    import hmac as _hmac
    from encoder import web
    monkeypatch.setattr(web.time, 'time', lambda: 1_000_000.0)
    tok = web.comms_token({'cloud': {'api_key': 'pce_secret'}})
    want = 'enc-' + _hmac.new(
        b'pce_secret', f'enc:{int(1_000_000 // 300)}'.encode(),
        'sha256').hexdigest()[:32]
    assert tok == want
    assert web.comms_token({'cloud': {}}) == ''


def test_settings_page_embeds_comms_when_installed(monkeypatch):
    from encoder import web
    c, _calls = _settings_client(monkeypatch)
    monkeypatch.setattr(web, 'comms_status',
                        lambda: {'state': 'active',
                                 'buds': [{'mac': 'M', 'name': 'Buds'}]})
    cfg = config.load()
    cfg['cloud']['api_key'] = 'pce_k123'
    cfg['cloud']['base_url'] = 'https://basebook.org'
    config.save(cfg)
    html = c.get('/').get_data(as_text=True)
    assert '<iframe' in html and ':8790/login?token=enc-' in html
    assert 'open full-screen' in html



# ── the scorebug sender is gone ──────────────────────────────────────────────

def test_settings_page_has_no_scorebug_bandwidth_form(monkeypatch):
    """The box no longer renders or publishes a bug (the NDI sender and
    its bandwidth slider are gone) — the settings page must not offer
    a knob that drives nothing, and the old POST must not be a route."""
    from encoder import web
    provisioning.headless_setup()
    monkeypatch.setattr(web.system, 'journal_tail', lambda *a, **k: [])
    app = web.create_app()
    client = app.test_client()
    client.post('/login', data={'pin': config.load()['device']['pin']})
    html = client.get('/').get_data(as_text=True)
    assert 'Scorebug bandwidth' not in html
    assert 'name="bandwidth"' not in html
    assert client.post('/bandwidth', data={'bandwidth': 2}).status_code == 404
    assert 'bandwidth' not in config.load()
    assert '/bandwidth' not in {r.rule for r in app.url_map.iter_rules()}


def test_entrypoint_starts_no_sender():
    import inspect
    from encoder import __main__ as entry
    src = inspect.getsource(entry)
    assert 'scorebug' not in src and 'Sender(' not in src
    assert 'cloud_link.CloudLink()' in src
    assert "kwargs={'cloud': link}" in src

def test_comms_cloud_voice_subscriber(monkeypatch, tmp_path):
    """The ☁ cloud voice channel: the box polls the cloud for a
    LiveKit ticket and subscribes; disabled answers idle the thread;
    a missing livekit library reports itself instead of dying; and a
    LIVE cloud link outranks the P2P state on the coach page."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, 'comms', 'comms_ear.py')).read()
    assert "/api/sk/voice/token" in src
    assert 'def lk_thread' in src
    assert 'target=lk_thread' in src               # actually started
    assert 're-run install_comms.sh' in src        # missing lib says so
    # billed by the participant-minute: join only while a game is on,
    # and leave when it ends — an always-connected box burns ~43k
    # min/month idling, a plan tier by itself
    assert "if not STATE.get('voice_wanted'):" in src
    assert 'cloud voice standing by' in src     # the gate names itself
    assert 'leave_when_game_ends' in src
    # playback must never block the event loop: a stalled paplay pipe
    # starved the keepalive and the server dropped the box mid-sentence
    # ('stops listening after about 5-10 seconds', field report) —
    # frames go through a bounded queue drained by a writer thread,
    # dropping the oldest when behind
    assert 'q = _queue.Queue(maxsize=' in src
    assert 'threading.Thread(target=writer' in src
    assert 'q.get_nowait()' in src              # drop-oldest, keep now
    assert 'await asyncio.to_thread(_route_to_bud)' in src
    # the absolute ceiling: no session outlives 5 h whatever the
    # signals say — the last backstop against unbounded minutes
    assert 'time.time() - t0 > 5 * 3600' in src
    # older clouds don't send voice_wanted — game presence is the
    # fallback so an un-updated site keeps the previous behavior
    assert "d.get('voice_wanted',\n" in src or \
        "d.get('voice_wanted'," in src
    spec = importlib.util.spec_from_file_location(
        'comms_ear_lk_test', _os.path.join(root, 'comms', 'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # the voice line the cloud sees: cloud LIVE outranks the P2P state
    mod.RTC_STATE['s'] = 'waiting for a coach'
    mod.LK_STATE['s'] = ''
    assert mod._voice_line() == 'waiting for a coach'
    mod.LK_STATE['s'] = 'cloud channel joined — waiting for the coach'
    assert 'cloud channel joined' in mod._voice_line()
    assert 'waiting for a coach ·' in mod._voice_line()
    mod.LK_STATE['s'] = '🎙 cloud LIVE — coach linked'
    assert mod._voice_line() == '🎙 cloud LIVE — coach linked'
    # the installer ships the client — and ships PIP first: a netinst
    # Debian has no pip3, so the livekit install died silently with
    # 'command not found' and the box reported 'livekit is not
    # installed' after every install (field report)
    sh = open(_os.path.join(root, 'comms', 'install_comms.sh')).read()
    assert 'livekit' in sh
    assert 'python3-pip' in sh
    assert sh.index('python3-pip') < sh.index(
        'pip3 install --break-system-packages -q livekit')


def test_comms_wired_transmitter_mode(monkeypatch, tmp_path):
    """An Avantree-style transmitter in the 3.5 mm jack: speech routes
    to the ANALOG sink (never a leftover bud), the cloud hears
    'line-out transmitter' as the earpiece (no false 'no earpiece
    paired' warning), and the bud-chaser stands down."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_lineout_test', _os.path.join(root, 'comms',
                                                'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'LINEOUT_FILE', str(tmp_path / 'lineout'))
    assert not mod.lineout_on()
    mod.set_lineout(True)
    assert mod.lineout_on()
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        out = ''
        if cmd[:3] == ['pactl', 'list', 'short']:
            out = ('55\tbluez_output.AA_BB.1\tmodule\ts16le\tIDLE\n'
                   '56\talsa_output.pci-0000_00_1f.3.analog-stereo\t'
                   'module\ts16le\tIDLE\n')
        return type('R', (), {'stdout': out, 'returncode': 0})()
    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    monkeypatch.setattr(mod, '_sink_audible', lambda s: None)
    assert mod._route_to_bud() is True         # mode redirects the router
    picked = [c for c in calls if c[:2] == ['pactl', 'set-default-sink']]
    assert picked and 'analog' in picked[-1][2]
    assert 'bluez' not in picked[-1][2]
    # readiness: the wired transmitter IS the ear
    src = open(_os.path.join(root, 'comms', 'comms_ear.py')).read()
    assert "ears.append('line-out transmitter')" in src
    assert 'if lineout_on():\n                continue' in src  # chaser
    assert 'action="/lineout"' in src          # and the card offers it
    # off again → the bluetooth path is back
    mod.set_lineout(False)
    calls.clear()
    assert mod._route_to_bud() is True
    picked = [c for c in calls if c[:2] == ['pactl', 'set-default-sink']]
    assert picked and 'bluez' in picked[-1][2]
    # the no-suspend drop-in now covers the analog jack too
    assert 'alsa_output' in mod._NO_SUSPEND_BODY


def test_comms_call_text_never_flips_by_handedness():
    """Absolute sides: 'right hand side' is the catcher's right whoever
    bats. The old batter-true swap made every call depend on the coach
    keeping the batter's handedness set (field report)."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_zone_test', _os.path.join(root, 'comms',
                                             'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    zones = {'A': 'right hand side, off the plate',
             '6': 'right hand side'}
    for bats in ('R', 'L', ''):
        assert mod.call_text({'pitch_type': 'Fastball', 'location': 'A',
                              'bats': bats}, zones) \
            == 'Fastball — right hand side, off the plate', bats
        assert mod.call_text({'pitch_type': 'Slider', 'location': '6',
                              'bats': bats}, zones) \
            == 'Slider — right hand side', bats


def test_comms_voice_is_pickable_and_survives_bad_picks(monkeypatch,
                                                            tmp_path):
    """The coach picks the ear's voice from a curated Piper set. The
    pick lives under the service user's home (no root), say() prefers
    it over the install default, and an unknown or mid-download pick is
    refused rather than queued."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_voice_test', _os.path.join(root, 'comms',
                                              'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.VOICES) >= {'lessac', 'amy', 'ryan', 'joe'}
    monkeypatch.setattr(mod, 'VOICE_DIR', str(tmp_path))
    # no pick yet → the install default speaks
    assert mod._voice_onnx() == mod.PIPER_VOICE
    # a pick with its model present wins
    (tmp_path / 'voice.onnx').write_bytes(b'onnx')
    (tmp_path / 'name').write_text('amy')
    assert mod._voice_onnx() == str(tmp_path / 'voice.onnx')
    assert mod.voice_current() == 'amy'
    # unknown / busy picks never start a thread
    started = []
    monkeypatch.setattr(mod.threading, 'Thread',
                        lambda *a, **k: started.append(1) or
                        type('T', (), {'start': lambda self: None})())
    mod.voice_download('not-a-voice')
    assert not started
    mod.VOICE_DL['busy'] = True
    mod.voice_download('ryan')
    assert not started
    mod.VOICE_DL['busy'] = False
    mod.voice_download('ryan')
    assert started
    # and the admin page offers the picker
    src = open(_os.path.join(root, 'comms', 'comms_ear.py')).read()
    assert 'action="/voice"' in src


def test_comms_audio_wakes_the_sink_before_the_first_word(monkeypatch,
                                                              tmp_path):
    """A suspended Bluetooth sink eats the first ~second of whatever is
    played while the link wakes ('cutting off the first second or so',
    field report). Speech gets leading silence baked into the wav, the
    coach's voice clips get an ffplay adelay, and a WirePlumber drop-in
    stops bluez sinks from suspending at all — written once, restart
    only on first write."""
    import importlib.util
    import os as _os
    import wave as _wave
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, 'comms', 'comms_ear.py')).read()
    assert "_pad_wav(wav)" in src                  # speech path pads
    assert "adelay=" in src                        # clip path pads
    spec = importlib.util.spec_from_file_location(
        'comms_ear_audio_test', _os.path.join(root, 'comms',
                                              'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    wav = str(tmp_path / 't.wav')
    with _wave.open(wav, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b'\x01\x02' * 2205)         # 0.1 s of "speech"
    mod._pad_wav(wav, ms=700)
    pad_n = int(22050 * 700 / 1000.0)
    with _wave.open(wav, 'rb') as r:
        n = r.getnframes()
        head = r.readframes(pad_n)
    assert n == 2205 + pad_n                       # padded, not replaced
    assert head == b'\x00' * len(head)             # and the pad is silence
    # the no-suspend drop-in: written once, restarted once
    calls = []
    monkeypatch.setattr(mod, '_NO_SUSPEND',
                        str(tmp_path / 'wp' / '51.conf'))
    monkeypatch.setattr(mod.subprocess, 'run',
                        lambda *a, **k: calls.append(a[0]) or
                        type('R', (), {'returncode': 0})())
    mod.ensure_bt_no_suspend()
    body = open(str(tmp_path / 'wp' / '51.conf')).read()
    assert 'suspend-timeout-seconds = 0' in body
    assert 'bluez_output' in body
    assert any('wireplumber' in ' '.join(c) for c in calls)
    calls.clear()
    mod.ensure_bt_no_suspend()                     # already there
    assert not calls                               # no second restart


def test_comms_picker_diagnoses_a_dead_dongle_instead_of_a_button(
        monkeypatch):
    """A dongle with no firmware enumerates as an hci with NO address.
    The old card drew it as a button whose value was the empty MAC —
    which the /adapter POST reads as UNPIN, so tapping the dongle
    silently cleared the pin: 'I click on the tp link and nothing
    happens' (field report). A dead radio gets a diagnosis — including
    the firmware-realtek fix — and never a button. The installer also
    ships firmware-realtek so sold boxes never hit this."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_card_test', _os.path.join(root, 'comms', 'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'adapters', lambda: [
        {'hci': 'hci0', 'mac': '28:A4:4A:F0:D7:13', 'usb': True,
         'blocked': False, 'name': 'box'},
        {'hci': 'hci1', 'mac': '', 'usb': True,
         'blocked': False, 'name': ''}])
    monkeypatch.setattr(mod, 'adapter_pref', lambda: '')
    monkeypatch.setattr(mod, 'active_adapter',
                        lambda: '28:A4:4A:F0:D7:13')
    monkeypatch.setattr(mod, '_usb_product',
                        lambda hci: 'TP-Link UB500 Adapter'
                        if hci == 'hci1' else '')
    card = mod._adapter_card()
    assert 'value=""' not in card          # no unpin-in-disguise button
    assert 'firmware-realtek' in card      # the fix, named on the card
    assert 'never initialized' in card
    # the working radio still gets its button, and unpinned ≠ 'picked'
    assert 'value="28:A4:4A:F0:D7:13"' in card
    assert 'picked, but NOT' not in card
    sh = open(_os.path.join(root, 'install.sh')).read()
    assert 'firmware-realtek' in sh


def test_comms_product_walk_never_credits_the_root_hub(monkeypatch,
                                                       tmp_path):
    """The Intel card carries no product string, and the sysfs walk was
    crediting it with the xHCI root hub's name — 'xHCI Host Controller'
    on the picker (field report screenshot). A Linux Foundation root
    hub (idVendor 1d6b) ends the walk empty-handed."""
    import importlib.util
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_walk_test', _os.path.join(root, 'comms', 'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hub = tmp_path / 'usb1'
    dev = hub / '1-3'
    iface = dev / '1-3:1.0' / 'bluetooth' / 'hci9'
    iface.mkdir(parents=True)
    (hub / 'product').write_text('xHCI Host Controller\n')
    (hub / 'idVendor').write_text('1d6b\n')
    monkeypatch.setattr(mod.os.path, 'realpath',
                        lambda p: str(iface) if 'hci9' in p else p)
    # no product anywhere below the root hub → empty, not the hub's name
    assert mod._usb_product('hci9') == ''
    # a REAL device's product still wins before the walk reaches the hub
    (dev / 'product').write_text('TP-Link UB500 Adapter\n')
    (dev / 'idVendor').write_text('2357\n')
    assert mod._usb_product('hci9') == 'TP-Link UB500 Adapter'


def test_comms_bt_calls_select_the_pinned_radio(monkeypatch, tmp_path):
    """With an adapter pinned, every bluetoothctl call runs as a scripted
    session that SELECTs the pinned controller first — powering the
    others down is not enough on an N150, where BlueZ keeps a blocked
    controller as [default] and every one-shot call talked to a radio
    that was off ('picked, but NOT the one in use', field report)."""
    import importlib.util
    import os as _os
    import subprocess as _sp
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_bt_test', _os.path.join(root, 'comms', 'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, 'adapter_pref', lambda: 'AA:BB:CC:DD:EE:FF')
    sent = {}

    class _P:
        def __init__(self, *a, **k):
            self.stdin = self

        def write(self, txt):
            sent['in'] = sent.get('in', '') + txt

        def flush(self):
            pass

        def communicate(self, timeout=None):
            return ('\x1b[0;94m[bluetooth]\x1b[0m# Controller '
                    'AA:BB:CC:DD:EE:FF box [default]\r\n', '')

        def kill(self):
            pass
    monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **k: _P())
    out = mod._bt('show')
    assert sent['in'].startswith('select AA:BB:CC:DD:EE:FF\n')
    assert 'exit' in sent['in']
    assert '\x1b[' not in out and '\r' not in out    # ANSI/CR stripped
    assert 'AA:BB:CC:DD:EE:FF' in out
    # the scan form holds the session open, then turns discovery off
    monkeypatch.setattr(mod.time, 'sleep', lambda s: None)
    sent.clear()
    mod._bt('--timeout', '12', 'scan', 'on', timeout=20)
    assert 'scan on' in sent['in'] and 'scan off' in sent['in']
    # unpinned boxes keep the one-shot path untouched
    monkeypatch.setattr(mod, 'adapter_pref', lambda: '')
    calls = []
    monkeypatch.setattr(mod.subprocess, 'run',
                        lambda *a, **k: (calls.append(a[0]),
                                         type('R', (), {'stdout': '',
                                                        'stderr': ''}))[1])
    mod._bt('show')
    assert calls and calls[0][:2] == ['bluetoothctl', 'show']


def test_comms_ear_accepts_the_encoders_pass(monkeypatch):
    """The two halves of the handshake actually shake: a token minted
    by encoder.web.comms_token is accepted by comms_ear._enc_token_ok
    when both hold the same activation key, refused with a different
    key or junk. (Both windows are accepted, so a bucket flip between
    mint and check cannot flake this.)"""
    import importlib.util
    import os as _os
    from encoder import web
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        'comms_ear_test', _os.path.join(root, 'comms', 'comms_ear.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv('PLAYCALL_API_KEY', 'pce_secret')
    tok = web.comms_token({'cloud': {'api_key': 'pce_secret'}})
    assert mod._enc_token_ok(tok) is True
    assert mod._enc_token_ok('enc-junk') is False
    assert mod._enc_token_ok('') is False
    monkeypatch.setenv('PLAYCALL_API_KEY', 'different')
    assert mod._enc_token_ok(tok) is False


# Field report (Maeser Prep, N150 v1.2.52): camera sending H.265 + AAC,
# ingest healthy at 3.7 Mbps, but the push crash-looped every ~15 s and
# YouTube refused GO LIVE with 'stream inactive'. The input leg chose
# loopback RTMP because the AUDIO was AAC — and MediaMTX drops H.265
# from an RTMP read ('skipping track 1 (H265)'), so ffmpeg pushed an
# audio-only stream. The input choice must consider BOTH tracks.

def test_an_hevc_camera_is_read_over_rtsp():
    from encoder import youtube_push as yp
    cfg = {'local_ingest_key': 'k', 'push_bitrate_kbps': 3000,
           'push_codec': 'hevc'}
    cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x', hw='vaapi',
                              caps={'h264': True, 'hevc': True},
                              vcodec='hevc')
    assert '-rtsp_transport' in cmd            # video survives the read
    assert 'rtsp://127.0.0.1:8554/live/k' in cmd
    assert 'hevc_vaapi' in cmd                 # …and transcodes to HEVC
    # AAC audio still copies — the RTSP hop does not force a re-encode
    assert ['-c:a', 'copy'] == cmd[cmd.index('-c:a'):cmd.index('-c:a') + 2]


def test_an_h264_camera_keeps_the_rtmp_copy_path():
    """The battle-tested default is untouched: H.264 + AAC (Mevo) still
    reads over loopback RTMP, byte-identical copy."""
    from encoder import youtube_push as yp
    cfg = {'local_ingest_key': 'k'}
    for vcodec in ('', 'h264'):                # '' = probe saw nobody yet
        cmd = yp.build_ffmpeg_cmd(cfg, 'aac', 'rtmp://yt/x', hw='',
                                  vcodec=vcodec)
        assert 'rtmp://127.0.0.1:1935/live/k' in cmd
        assert '-rtsp_transport' not in cmd
        assert cmd[cmd.index('-c:v') + 1] == 'copy'


def test_probe_codecs_reads_both_tracks_and_fails_closed():
    from encoder import youtube_push as yp

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out
    ok = json.dumps({'streams': [
        {'codec_type': 'video', 'codec_name': 'hevc'},
        {'codec_type': 'audio', 'codec_name': 'aac'}]})
    cfg = {'local_ingest_key': 'k'}
    assert yp.probe_codecs(cfg, lambda c, **kw: _R(0, ok)) == ('hevc', 'aac')
    # audio-only publisher, ffprobe failure, junk: all degrade to ''
    aud = json.dumps({'streams': [{'codec_type': 'audio',
                                   'codec_name': 'opus'}]})
    assert yp.probe_codecs(cfg, lambda c, **kw: _R(0, aud)) == ('', 'opus')
    assert yp.probe_codecs(cfg, lambda c, **kw: _R(1, '')) == ('', '')
    assert yp.probe_codecs(cfg, lambda c, **kw: _R(0, 'junk')) == ('', '')
    # the shell-parity wrapper still answers audio-only
    assert yp.probe_audio_codec(cfg, lambda c, **kw: _R(0, ok)) == 'aac'
