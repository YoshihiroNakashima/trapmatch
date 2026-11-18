# -*- coding: utf-8 -*-
"""まとめた実行ファイル（dist/TrapMatch.exe など）が実際に動くかを確かめる。

使い方:  python packaging/smoke_test.py dist/TrapMatch.exe
確かめること: 起動、画面のファイル、保存場所の登録、走査、MJPEG の AVI の 1 コマ、書き出し、
二重起動、別の版への切り替え、終了。Pillow が必要（試験用の動画を作るため）。
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import fixture  # noqa: E402
import tm_core  # noqa: E402

ENV = dict(os.environ, TRAPMATCH_NO_OPEN='1', PYTHONIOENCODING='utf-8')
AS_VERSION = ("import sys; sys.path.insert(0, %r); import tm_core; tm_core.VERSION = %r; import app; "
              "sys.exit(app.main(sys.argv[1:]))")
problems = []


def check(ok, text):
    print(('ok   ' if ok else 'NG   ') + text)
    sys.stdout.flush()
    if not ok:
        problems.append(text)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def call(port, method, path, body=None):
    con = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    data = None if body is None else json.dumps(body).encode('utf-8')
    con.request(method, path, body=data, headers={'Content-Type': 'application/json'} if data else {})
    r = con.getresponse()
    raw = r.read()
    con.close()
    return r.status, r.getheader('Content-Type') or '', raw


def get_json(port, path):
    st, ct, raw = call(port, 'GET', path)
    return st, json.loads(raw.decode('utf-8'))


def post_json(port, path, body):
    st, ct, raw = call(port, 'POST', path, body)
    return st, json.loads(raw.decode('utf-8'))


def wait_up(port, version=None, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            st, info = get_json(port, '/api/ping')
            if version is None or info['version'] == version:
                return info
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError('起動しませんでした（ポート %d）' % port)


def wait_job(port, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        st, j = get_json(port, '/api/job')
        if j['job'] and j['job']['state'] != 'running':
            return j['job']
        time.sleep(0.1)
    raise AssertionError('処理が終わりませんでした')


def main():
    exe = os.path.abspath(sys.argv[1])
    check(os.path.isfile(exe), '実行ファイルがある（%.1f MB）' % (os.path.getsize(exe) / 1e6))
    out = subprocess.run([exe, '--version'], env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, timeout=120)
    text = out.stdout.decode('utf-8', 'replace').strip()
    check(out.returncode == 0 and text == 'TrapMatch ' + tm_core.VERSION, '--version が版を返す（%s）' % text)

    base = tempfile.mkdtemp(prefix='tm_smoke_')
    data = os.path.join(base, 'data')
    tree = fixture.build(os.path.join(base, 'tree'))
    port = free_port()
    args = ['--port', str(port), '--data-dir', data, '--no-browser']
    proc = subprocess.Popen([exe] + args, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    newer = None
    try:
        info = wait_up(port)
        check(info['version'] == tm_core.VERSION and info['pid'] != os.getpid(), '起動して応答する（版 %s）' % info['version'])
        st, ct, raw = call(port, 'GET', '/')
        check(st == 200 and b'<title>TrapMatch</title>' in raw, '画面（index.html）が同梱されている')
        st, ct, raw = call(port, 'GET', '/static/app.js')
        check(st == 200 and ("const UI_VERSION = '%s'" % tm_core.VERSION).encode() in raw, '画面のプログラムの版が合っている')
        st, ct, raw = call(port, 'GET', '/static/icon.png')
        check(st == 200 and raw[:8] == b'\x89PNG\r\n\x1a\n', 'アイコンが同梱されている')
        st, cfg = post_json(port, '/api/config', {'sources': [
            {'id': 'hdd', 'name': 'HDD', 'type': 'folder', 'path': tree['hdd']},
            {'id': 'nas', 'name': 'NAS', 'type': 'folder', 'path': tree['nas']},
            {'id': 'csv', 'name': 'CSV', 'type': 'csv', 'path': tree['csv']},
        ], 'settings': {'exts': ['avi', 'mp4', 'mov', 'webm']}})
        check(st == 200 and os.path.isfile(os.path.join(data, 'config.json')), '保存場所を登録できる')
        S = fixture.SEASON
        st, r = post_json(port, '/api/scan', {'season': S})
        job = wait_job(port)
        check(st == 200 and job['state'] == 'done', '走査できる（%s）' % (job.get('error') or job['state']))
        st, p = get_json(port, '/api/items?season=' + S)
        items = {(p['stations'][a[1]], a[2]): a for a in p['items']}
        n_mis = sum(1 for a in p['items'] if a[4] & tm_core.F_NEEDS)
        check(len(items) == 19 and n_mis == 10, '照合の結果が出る（%d 本、不一致 %d 本）' % (len(items), n_mis))
        # MJPEG の AVI を 1 コマずつ返せる
        k = items[('BSN0001', 'IMG_0001')][0]
        st, d = get_json(port, '/api/item?season=%s&id=%d' % (S, k))
        check(st == 200 and d['stem'] == 'IMG_0001', '動画の詳細を取り出せる')
        st, av = get_json(port, '/api/avinfo?season=%s&id=%d&src=hdd&n=0' % (S, k))
        st2, ct, raw = call(port, 'GET', '/frame?t=%s&i=0' % av.get('token', ''))
        check(st == 200 and av.get('n', 0) > 0 and st2 == 200 and raw[:2] == b'\xff\xd8',
              'AVI（MJPEG）の 1 コマを取り出せる（%s コマ）' % av.get('n'))
        # 判断と書き出し
        post_json(port, '/api/decide', {'season': S, 'ids': [items[('BSN0001', 'IMG_0008')][0]], 'decision': 'keep'})
        st, r = post_json(port, '/api/export', {'season': S, 'kind': 'csv', 'options': {}})
        job = wait_job(port)
        ok = st == 200 and job['state'] == 'done' and os.path.isfile(job['result']['path'])
        check(ok and job['result']['added'] == 1, '更新後の CSV を書き出せる')
        st, r = post_json(port, '/api/export', {'season': S, 'kind': 'all', 'options': {}})
        job = wait_job(port)
        check(st == 200 and job['state'] == 'done' and os.path.isfile(job['result']['path']) and job['result']['rows'] >= n_mis,
              '全 Season をまとめて書き出せる')
        # 同じ版をもう一度起動しても、二重には動かない
        again = subprocess.run([exe] + args, env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=120)
        check(again.returncode == 0 and proc.poll() is None, '二重に起動しない')
        # 別の版を起動すると、動いていた実行ファイルが終了して切り替わる
        newer = subprocess.Popen([sys.executable, '-c', AS_VERSION % (ROOT, '99.0.0')] + args, env=ENV,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        info = wait_up(port, '99.0.0')
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = None
        check(code == 0 and info['pid'] == newer.pid, '別の版を起動すると、実行ファイルが正常に終了して切り替わる（終了コード %s）' % code)
    finally:
        for pr in (newer, proc):
            if pr is not None and pr.poll() is None:
                try:
                    with open(os.path.join(data, 'server.json'), encoding='utf-8') as f:
                        post_json(port, '/api/quit', {'token': json.load(f)['token']})
                    pr.wait(timeout=20)
                except Exception:  # noqa: BLE001
                    pr.kill()
        shutil.rmtree(base, ignore_errors=True)
    print('%d problem(s)' % len(problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
