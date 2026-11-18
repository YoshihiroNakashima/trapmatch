# -*- coding: utf-8 -*-
"""サーバーを実際に起動して、画面が使う API を試す。"""
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fixture  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client(object):
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None):
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            h.setdefault('Content-Type', 'application/json')
        con.request(method, path, body=data, headers=h)
        r = con.getresponse()
        raw = r.read()
        con.close()
        return r, raw

    def get(self, path, **kw):
        r, raw = self.request('GET', path, **kw)
        return r.status, json.loads(raw.decode('utf-8'))

    def post(self, path, body, **kw):
        r, raw = self.request('POST', path, body=body, **kw)
        return r.status, json.loads(raw.decode('utf-8'))

    def wait_job(self):
        for _ in range(400):
            st, j = self.get('/api/job')
            if j['job'] and j['job']['state'] != 'running':
                return j['job']
            time.sleep(0.05)
        raise AssertionError('job did not finish')


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    base = str(tmp_path_factory.mktemp('http'))
    tree = fixture.build(os.path.join(base, 'tree'))
    port = free_port()
    env = dict(os.environ, TRAPMATCH_NO_OPEN='1', PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py'), '--port', str(port),
                             '--data-dir', os.path.join(base, 'data'), '--no-browser', '--allow-quit'],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c = Client(port)
    for _ in range(100):
        try:
            c.get('/api/ping')
            break
        except OSError:
            time.sleep(0.05)
    st, cfg = c.post('/api/config', {'sources': [
        {'id': 'hdd', 'name': 'HDD', 'type': 'folder', 'path': tree['hdd']},
        {'id': 'nas', 'name': 'NAS', 'type': 'folder', 'path': tree['nas']},
        {'id': 'csv', 'name': 'CSV', 'type': 'csv', 'path': tree['csv']},
    ], 'settings': {'exts': ['avi', 'mp4', 'mov', 'webm']}})
    assert st == 200
    yield c, tree
    try:
        c.post('/api/quit', {})
    except OSError:
        pass
    proc.wait(timeout=10)


def ids(c):
    st, p = c.get('/api/items?season=' + fixture.SEASON)
    assert st == 200 and p['scanned']
    return {(p['stations'][a[1]], a[2]): a for a in p['items']}, p


def test_state_and_seasons(server):
    c, tree = server
    st, s = c.get('/api/state')
    assert st == 200 and s['app'] == 'TrapMatch' and len(s['config']['sources']) == 3
    st, s = c.get('/api/seasons')
    assert s['seasons'] == ['N260101', 'N250101']
    st, p = c.get('/api/items?season=N260101')
    assert p == {'season': 'N260101', 'scanned': False}


def test_scan_and_items(server):
    c, tree = server
    st, r = c.post('/api/scan', {'season': fixture.SEASON})
    assert st == 200
    job = c.wait_job()
    assert job['state'] == 'done' and job['result']['items'] == 19
    items, p = ids(c)
    assert len(items) == 19 and items[('BSN0001', 'IMG_0007')][3] == 5
    st, d = c.get('/api/item?season=%s&id=%d' % (fixture.SEASON, items[('BSN0001', 'IMG_0008')][0]))
    assert d['embedded']['text'] == '2026/01/02 03:04:05'
    st, e = c.get('/api/item?season=%s&id=99999' % fixture.SEASON)
    assert st == 404 and 'error' in e


def test_media_range(server):
    c, tree = server
    items, p = ids(c)
    i = items[('BSN0001', 'IMG_0008')][0]
    path = os.path.join(tree['nas'], fixture.SEASON, 'BSN0001', 'IMG_0008.MP4')
    raw_all = open(path, 'rb').read()
    url = '/media?season=%s&id=%d&src=nas&n=0' % (fixture.SEASON, i)
    r, raw = c.request('GET', url)
    assert r.status == 200 and raw == raw_all and r.getheader('Content-Type') == 'video/mp4'
    assert r.getheader('Accept-Ranges') == 'bytes'
    r, raw = c.request('GET', url, headers={'Range': 'bytes=100-299'})
    assert r.status == 206 and raw == raw_all[100:300]
    assert r.getheader('Content-Range') == 'bytes 100-299/%d' % len(raw_all)
    r, raw = c.request('GET', url, headers={'Range': 'bytes=-50'})
    assert r.status == 206 and raw == raw_all[-50:]
    r, raw = c.request('GET', url, headers={'Range': 'bytes=%d-' % (len(raw_all) - 10)})
    assert r.status == 206 and raw == raw_all[-10:]
    r, raw = c.request('GET', url, headers={'Range': 'bytes=%d-' % (len(raw_all) + 5)})
    assert r.status == 416
    r, raw = c.request('GET', '/media?season=%s&id=%d&src=hdd&n=0' % (fixture.SEASON, i))
    assert r.status == 404      # HDD にはない


def test_avi_frames_and_transcode(server):
    c, tree = server
    items, p = ids(c)
    i = items[('BSN0001', 'IMG_0001')][0]
    st, info = c.get('/api/avinfo?season=%s&id=%d&src=hdd&n=0' % (fixture.SEASON, i))
    assert st == 200 and info['mjpeg'] and info['n'] == 30 and abs(info['fps'] - 15) < 1e-6 and info['token']
    for k in (0, 29, 500):
        r, raw = c.request('GET', '/frame?t=%s&i=%d' % (info['token'], k))
        assert r.status == 200 and r.getheader('Content-Type') == 'image/jpeg'
        im = Image.open(io.BytesIO(raw))
        im.load()
        assert im.size == (160, 120)
    r, raw = c.request('GET', '/frame?t=nope&i=0')
    assert r.status == 404
    if info['ffmpeg']:
        r, raw = c.request('GET', '/transcode?season=%s&id=%d&src=hdd&n=0' % (fixture.SEASON, i))
        assert r.status == 200 and raw[4:8] == b'ftyp' and len(raw) > 1000


def test_decide_field_and_exports(server):
    c, tree = server
    items, p = ids(c)
    S = fixture.SEASON
    k = [items[('BSN0001', n)][0] for n in ('IMG_0008', 'IMG_0009')]
    st, r = c.post('/api/decide', {'season': S, 'ids': k, 'decision': 'keep'})
    assert r == {'ok': True, 'count': 2}
    st, r = c.post('/api/decide', {'season': S, 'ids': k, 'decision': 'bogus'})
    assert st == 400
    st, r = c.post('/api/field', {'season': S, 'id': k[0], 'field': 'memo', 'value': 'メモ'})
    assert st == 200
    st, r = c.post('/api/decide', {'season': S, 'pairs': [[k[1], '']]})
    assert st == 200
    items, p = ids(c)
    assert items[('BSN0001', 'IMG_0008')][5:7] == ['k', 'メモ'] and items[('BSN0001', 'IMG_0009')][5] == ''
    st, r = c.post('/api/export', {'season': S, 'kind': 'result'})
    assert st == 200 and os.path.isfile(r['path']) and r['rows'] == 19
    st, r2 = c.post('/api/openpath', {'path': r['path']})
    assert st == 200
    st, r2 = c.post('/api/openpath', {'path': tree['csv']})
    assert st == 400
    st, r = c.post('/api/export', {'season': S, 'kind': 'plan'})
    assert st == 200 and os.path.isfile(r['path'])
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv_counts'})
    assert r['add'] == 1 and r['ext'] == 1
    assert r['add_defaults'] == [['Species', 'Add']] and p['csv']['defaults'] == {'4': 'Add'}
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv', 'options': {}})
    assert st == 200
    job = c.wait_job()
    assert job['state'] == 'done' and job['result']['added'] == 1 and job['result']['ext_updated'] == 1
    assert os.path.isfile(job['result']['path'])
    out = open(job['result']['path'], encoding='utf-8-sig', newline='').read().split('\r\n')
    assert 'N260101,BSN0001,IMG_0008.MP4,2026/01/02 03:04:05,Add,' in out
    # 追加する行の既定値は設定で変えられ、設定のほかの項目を保存し直しても消えない
    st, state = c.get('/api/state')
    conf = state['config']
    assert conf['settings']['add_defaults']['Species'] == 'Add'
    saved = json.loads(json.dumps(conf))
    conf['settings']['add_defaults'] = {'Species': 'New', 'Note': 'added in {Season}'}
    st, r = c.post('/api/config', conf)
    assert st == 200 and r['config']['settings']['add_defaults'] == {'Species': 'New', 'Note': 'added in {Season}'}
    conf2 = r['config']
    conf2['settings']['autoplay'] = False
    st, r = c.post('/api/config', conf2)
    assert r['config']['settings']['add_defaults'] == {'Species': 'New', 'Note': 'added in {Season}'}
    items, p = ids(c)
    assert p['csv']['defaults'] == {'4': 'New', '5': 'added in N260101'}
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv', 'options': {}})
    job = c.wait_job()
    out = open(job['result']['path'], encoding='utf-8-sig', newline='').read().split('\r\n')
    assert 'N260101,BSN0001,IMG_0008.MP4,2026/01/02 03:04:05,New,added in N260101' in out
    c.post('/api/config', saved)
    st, r = c.post('/api/export', {'season': S, 'kind': 'dtcheck'})
    job = c.wait_job()
    assert job['state'] == 'done' and job['result']['sampled'] == 15


def test_guards(server):
    c, tree = server
    r, raw = c.request('GET', '/api/state', headers={'Host': 'evil.example:80'})
    assert r.status == 403
    r, raw = c.request('POST', '/api/decide', headers={'Content-Type': 'text/plain'})
    assert r.status == 415
    r, raw = c.request('GET', '/static/../app.py')
    assert r.status == 404
    r, raw = c.request('GET', '/')
    assert r.status == 200 and b'TrapMatch' in raw
    st, b = c.get('/api/browse?mode=csv&path=' + os.path.dirname(tree['csv']).replace(' ', '%20'))
    assert st == 200 and any(f['name'] == 'list.csv' for f in b['files'])
    st, b = c.get('/api/browse?path=')
    assert st == 200 and b['parent'] is None and b['dirs']


def test_scan_error_is_reported(server):
    c, tree = server
    st, cfg = c.get('/api/state')
    conf = cfg['config']
    saved = json.loads(json.dumps(conf))
    conf['sources'][1]['path'] = os.path.join(tree['nas'], 'missing')
    c.post('/api/config', conf)
    st, r = c.post('/api/scan', {'season': fixture.SEASON})
    job = c.wait_job()
    assert job['state'] == 'error' and 'NAS' in job['error']
    c.post('/api/config', saved)


def test_transcode_supports_range(server):
    c, tree = server
    items, p = ids(c)
    i = items[('BSN0001', 'IMG_0001')][0]
    st, info = c.get('/api/avinfo?season=%s&id=%d&src=hdd&n=0' % (fixture.SEASON, i))
    if not info['ffmpeg']:
        pytest.skip('ffmpeg がない')
    url = '/transcode?season=%s&id=%d&src=hdd&n=0' % (fixture.SEASON, i)
    r, whole = c.request('GET', url)
    assert r.status == 200 and r.getheader('Content-Length') == str(len(whole))
    r, part = c.request('GET', url, headers={'Range': 'bytes=10-99'})
    assert r.status == 206 and part == whole[10:100]


def test_no_changes_while_scanning(server, monkeypatch):
    c, tree = server
    items, p = ids(c)
    i = items[('BSN0001', 'IMG_0001')][0]
    # 大きめの CSV を足して、走査に少し時間がかかるようにする
    extra = os.path.join(os.path.dirname(tree['csv']), 'big.csv')
    with open(tree['csv'], 'r', encoding='utf-8-sig', newline='') as f:
        base = f.read()
    with open(extra, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(base)
        for k in range(400000):
            f.write('N2%05d,BSN%04d,IMG_%04d.AVI,2025/01/08 10:00:00,deer,\r\n' % (k % 40, k % 200, k % 9000))
    st, cfg = c.get('/api/state')
    conf = cfg['config']
    saved = json.loads(json.dumps(conf))
    conf['sources'][2]['path'] = extra
    c.post('/api/config', conf)
    st, r = c.post('/api/scan', {'season': fixture.SEASON})
    assert st == 200
    st, r = c.post('/api/decide', {'season': fixture.SEASON, 'ids': [i], 'decision': 'keep'})
    st2, r2 = c.post('/api/field', {'season': fixture.SEASON.lower(), 'id': i, 'field': 'memo', 'value': 'x'})
    job = c.wait_job()
    assert job['state'] == 'done'
    assert st == 409 and st2 == 409, (st, st2)
    st, r = c.post('/api/decide', {'season': fixture.SEASON, 'ids': [i], 'decision': ''})
    assert st == 200
    c.post('/api/config', saved)
    c.post('/api/scan', {'season': fixture.SEASON})
    assert c.wait_job()['state'] == 'done'


def test_mark_csv_only_rows(server):
    """CSV にしかない動画を「残す」とした行に、書き出しで印が付く。"""
    c, tree = server
    S = fixture.SEASON
    items, p = ids(c)
    assert p['csv']['mark'] == {'col': 'Note', 'text': 'csvのみ', 'cols': ['Note', 'Remarks']}
    k = items[('BSN0001', 'IMG_0010')][0]
    st, d = c.get('/api/item?season=%s&id=%d' % (S, k))
    assert st == 200 and not any(s.get('files') for s in d['sources'] if s['type'] == 'folder')
    c.post('/api/decide', {'season': S, 'ids': [k], 'decision': 'keep'})
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv_counts'})
    assert (r['mark'], r['mark_rows']) == (1, 1) and r['mark_info']['col'] == 'Note'
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv', 'options': {'mark': False}})
    job = c.wait_job()
    assert job['state'] == 'done' and job['result']['marked'] == 0
    assert 'csvのみ' not in open(job['result']['path'], encoding='utf-8-sig', newline='').read()
    time.sleep(1.1)                                    # 書き出しのファイル名は秒単位
    st, r = c.post('/api/export', {'season': S, 'kind': 'csv', 'options': {}})
    job = c.wait_job()
    assert job['state'] == 'done' and (job['result']['marked'], job['result']['mark_col']) == (1, 'Note')
    assert 'empty,"line one\nline two; csvのみ"\r\n' in open(job['result']['path'], encoding='utf-8-sig', newline='').read()
    st, r = c.post('/api/export', {'season': S, 'kind': 'plan'})
    assert r['summary']['csv_mark'] == 1
    c.post('/api/decide', {'season': S, 'ids': [k], 'decision': ''})


def test_export_all_seasons(server):
    """走査済みの全 Season をまとめて書き出す。"""
    c, tree = server
    S = fixture.SEASON
    st, r = c.post('/api/export', {'season': S, 'kind': 'all_counts'})
    assert st == 200 and [x['season'] for x in r['seasons']] == [S] and r['skipped'] == []
    assert r['totals']['videos'] == r['seasons'][0]['videos'] > 0 and r['totals']['mismatch'] > 0
    st, r = c.post('/api/export', {'season': S, 'kind': 'all', 'options': {'scope': 'mismatch'}})
    assert st == 200
    job = c.wait_job()
    assert job['state'] == 'done' and job['kind'] == 'export_all'
    res = job['result']
    assert res['seasons'] == 1 and os.path.isfile(res['path']) and os.path.isfile(res['summary_path'])
    lines = open(res['path'], encoding='utf-8-sig', newline='').read().split('\r\n')
    assert lines[0].startswith('Season,Station,File,HDD_file,') and lines[0].endswith(',DecidedAt,ScannedAt')
    assert res['rows'] == len([ln for ln in lines[1:] if ln]) >= r_mismatch(c, S)


def r_mismatch(c, season):
    st, p = c.get('/api/items?season=' + season)
    return sum(1 for a in p['items'] if a[4] & 0x4f)
