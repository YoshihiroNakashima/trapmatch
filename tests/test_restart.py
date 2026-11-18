# -*- coding: utf-8 -*-
"""起動し直しの試験: 新しい版を起動すると、動いたままの古い版が終了して切り替わる。"""
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import tm_core  # noqa: E402
from test_http import Client, free_port  # noqa: E402

ENV = dict(os.environ, TRAPMATCH_NO_OPEN='1', PYTHONIOENCODING='utf-8')
# 版だけを差し替えて起動する（別の版のプログラムに置き換えた状態を再現する）
AS_VERSION = ("import sys; sys.path.insert(0, %r); import tm_core; tm_core.VERSION = %r; import app; "
              "sys.exit(app.main(sys.argv[1:]))")


def start(port, data, version=None, extra=()):
    cmd = [sys.executable, os.path.join(ROOT, 'app.py')] if version is None \
        else [sys.executable, '-c', AS_VERSION % (ROOT, version)]
    return subprocess.Popen(cmd + ['--port', str(port), '--data-dir', data, '--no-browser'] + list(extra),
                            env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_up(port, version=None, timeout=15):
    c = Client(port)
    end = time.time() + timeout
    while time.time() < end:
        try:
            st, info = c.get('/api/ping')
            if version is None or info['version'] == version:
                return info
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError('server did not come up on port %d' % port)


def port_info(data):
    with open(os.path.join(data, 'server.json'), encoding='utf-8') as f:
        return json.load(f)


def stop(proc, port, data):
    if proc.poll() is None:
        try:
            Client(port).post('/api/quit', {'token': port_info(data)['token']})
        except (OSError, ValueError, KeyError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_ui_version_matches_program_version():
    js = open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8').read()
    assert re.search(r"const UI_VERSION = '([^']+)'", js).group(1) == tm_core.VERSION


def test_new_version_replaces_the_running_one(tmp_path):
    data = str(tmp_path / 'data')
    port = free_port()
    old = start(port, data)
    new = None
    try:
        info = wait_up(port)
        assert info['version'] == tm_core.VERSION and info['pid'] == old.pid
        pf = port_info(data)
        assert pf['port'] == port and pf['pid'] == old.pid and pf['version'] == tm_core.VERSION and len(pf['token']) >= 32
        c = Client(port)
        # 合言葉なしや、違う合言葉では終了しない
        assert c.post('/api/quit', {})[0] == 403
        assert c.post('/api/quit', {'token': 'x' * 32})[0] == 403
        assert c.get('/api/ping')[0] == 200 and old.poll() is None
        # 同じ版をもう一度起動しても、動いているほうはそのまま（画面を開くだけで終わる）
        same = start(port, data)
        out = same.communicate(timeout=20)[0].decode('utf-8', 'replace')
        assert same.returncode == 0 and 'すでに起動しています' in out
        assert old.poll() is None and port_info(data)['pid'] == old.pid
        # 別の版を起動すると、動いていた版が終了して、新しい版が同じポートで動く
        new = start(port, data, version='9.9.9')
        info = wait_up(port, '9.9.9')
        assert old.wait(timeout=10) == 0                    # 古い側は正常に終了する（窓が残らない）
        assert info['pid'] == new.pid and new.poll() is None
        pf2 = port_info(data)
        assert pf2['pid'] == new.pid and pf2['version'] == '9.9.9' and pf2['token'] != pf['token'] and pf2['port'] == port
        assert '終了させて、この版（9.9.9）で起動し直します' in new.stdout.readline().decode('utf-8', 'replace')
    finally:
        stop(old, port, data)
        if new is not None:
            stop(new, port, data)
    assert not os.path.exists(os.path.join(data, 'server.json'))        # 終了時に片づける


class _OldServer(http.server.BaseHTTPRequestHandler):
    """合言葉での終了に対応していない、以前の版のふりをするサーバー。"""
    data_dir = ''
    quits = 0

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send(200, {'app': 'TrapMatch', 'version': '0.1.3', 'data_dir': type(self).data_dir})

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        type(self).quits += 1
        self._send(403, {'error': '終了は許可されていません'})


def test_old_version_without_quit_support_is_left_running(tmp_path):
    data = str(tmp_path / 'data')
    os.makedirs(data)
    port = free_port()
    _OldServer.data_dir = os.path.abspath(data)
    _OldServer.quits = 0
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', port), _OldServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 以前の版の server.json には合言葉がない
        with open(os.path.join(data, 'server.json'), 'w', encoding='utf-8') as f:
            json.dump({'port': port, 'pid': os.getpid()}, f)
        new = start(port, data)
        out = new.communicate(timeout=20)[0].decode('utf-8', 'replace')
        assert new.returncode == 0
        assert '別の版の TrapMatch（0.1.3）が動いています' in out and '自動では終了できませんでした' in out
        assert '黒い窓' in out and _OldServer.quits == 0             # 合言葉がなければ、終了を頼まない
        assert Client(port).get('/api/ping')[1]['version'] == '0.1.3'   # 動いていたほうには手を付けない
        # 合言葉があっても断られた場合（処理の途中など）も、同じ案内を出して終わる
        with open(os.path.join(data, 'server.json'), 'w', encoding='utf-8') as f:
            json.dump({'port': port, 'pid': os.getpid(), 'token': 'a' * 32}, f)
        new = start(port, data)
        out = new.communicate(timeout=20)[0].decode('utf-8', 'replace')
        assert new.returncode == 0 and '自動では終了できませんでした' in out and _OldServer.quits == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_quit_is_refused_while_a_job_runs(tmp_path, monkeypatch):
    import app
    a = app.App(str(tmp_path / 'data'))
    monkeypatch.setattr(app, 'APP', a)
    gate = threading.Event()
    a.jobs.start('scan', 'N1', lambda job: gate.wait(10))

    class H(object):
        sent = None

        def read_json(self):
            return {'token': a.quit_token}

        def send_json(self, obj):
            H.sent = obj
    try:
        try:
            app.p_quit(H(), {})
            raise AssertionError('quit was accepted during a job')
        except app.HttpError as e:
            assert e.status == 409
        assert H.sent is None
    finally:
        gate.set()
