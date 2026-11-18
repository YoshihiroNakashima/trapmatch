# -*- coding: utf-8 -*-
"""TrapMatch: カメラトラップ動画の保存場所間照合アプリ（第 1 版）。

PC の中だけで動く小さなサーバーを立て、ブラウザの画面から操作する。
動画のファイルは一切変更しない。元の CSV に書くのは、確認のうえで行を末尾に足すときだけ（tm_core.csv_add_rows）。
それ以外は、読み取りと別フォルダへの書き出しのみ。

使い方:  python app.py [--port 8770] [--data-dir フォルダ] [--no-browser]
exe にまとめた版（TrapMatch.exe）も、同じ引数を受け取る。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener

# exe にまとめた版（PyInstaller）では、同梱したファイルは展開先（sys._MEIPASS）にある
FROZEN = bool(getattr(sys, 'frozen', False))
HERE = getattr(sys, '_MEIPASS', '') if FROZEN else ''
if not HERE:
    HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tm_core  # noqa: E402
import tm_media  # noqa: E402

STATIC_DIR = os.path.join(HERE, 'static')
STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.ico': 'image/x-icon', '.json': 'application/json; charset=utf-8',
}
VIDEO_TYPES = {
    'mp4': 'video/mp4', 'm4v': 'video/mp4', 'mov': 'video/mp4', 'webm': 'video/webm',
    'ogv': 'video/ogg', 'mkv': 'video/x-matroska', 'avi': 'video/x-msvideo',
}
DISCONNECT = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class HttpError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


class App(object):
    def __init__(self, data_dir, allow_quit=False):
        self.store = tm_core.Store(data_dir)
        self.jobs = tm_core.JobRunner()
        self.tokens = OrderedDict()
        self.transcoded = OrderedDict()     # 変換した動画（少数だけ覚えておく）
        self.lock = threading.Lock()
        self.allow_quit = allow_quit
        self.quit_token = uuid.uuid4().hex      # 起動し直しのとき、新しい側がこの版を終了させるための合言葉
        self.server = None
        self.port = 0

    def ffmpeg(self):
        return tm_media.find_ffmpeg(self.store.config()['settings'].get('ffmpeg', ''))

    def log(self, text):
        try:
            with open(os.path.join(self.store.data_dir, 'logs', 'app.log'), 'a', encoding='utf-8') as f:
                f.write('[%s] %s\n' % (tm_core.now_text(), text))
        except OSError:
            pass

    def new_token(self, path, info):
        tok = uuid.uuid4().hex[:12]
        with self.lock:
            self.tokens[tok] = (path, info)
            while len(self.tokens) > 8:
                self.tokens.popitem(last=False)
        return tok

    def token(self, tok):
        with self.lock:
            hit = self.tokens.get(tok)
            if hit is not None:
                self.tokens.move_to_end(tok)
            return hit


APP = None  # type: App


# ---------------------------------------------------------------- OS まわり

def list_roots():
    roots = []
    if sys.platform.startswith('win'):
        try:
            import ctypes
            import string
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            for i, c in enumerate(string.ascii_uppercase):
                if mask >> i & 1:
                    roots.append({'name': c + ':', 'path': c + ':\\'})
        except Exception:  # noqa: BLE001
            roots.append({'name': 'C:', 'path': 'C:\\'})
    else:
        roots.append({'name': '/', 'path': '/'})
    home = os.path.expanduser('~')
    for label, p in (('ホーム', home), ('Desktop', os.path.join(home, 'Desktop')),
                     ('Documents', os.path.join(home, 'Documents'))):
        if os.path.isdir(p):
            roots.append({'name': label, 'path': p})
    return roots


def browse(path, mode):
    if not path:
        return {'path': '', 'parent': None, 'dirs': list_roots(), 'files': []}
    p = os.path.abspath(path)
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if not os.path.isdir(p):
        raise HttpError(404, 'フォルダが見つかりません: %s' % path)
    dirs = []
    files = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if tm_core.skip_dir(e.name):
                        continue
                    if e.is_dir():
                        dirs.append({'name': e.name, 'path': e.path})
                    elif mode == 'csv' and e.name.lower().endswith(('.csv', '.tsv', '.txt')):
                        files.append({'name': e.name, 'path': e.path})
                    elif mode == 'file':
                        files.append({'name': e.name, 'path': e.path})
                except OSError:
                    continue
    except OSError as err:
        raise HttpError(400, 'フォルダを開けません: %s' % (err.strerror or err))
    dirs.sort(key=lambda d: tm_core.natural_key(d['name']))
    files.sort(key=lambda d: tm_core.natural_key(d['name']))
    parent = os.path.dirname(p)
    if parent == p:
        parent = ''
    return {'path': p, 'parent': parent, 'dirs': dirs[:2000], 'files': files[:2000]}


def open_in_os(path, reveal=False):
    if os.environ.get('TRAPMATCH_NO_OPEN'):
        return
    if sys.platform.startswith('win'):
        if reveal and os.path.isfile(path):
            subprocess.Popen(['explorer', '/select,', path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', path] if reveal else ['open', path])
    else:
        subprocess.Popen(['xdg-open', os.path.dirname(path) if reveal and os.path.isfile(path) else path])


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'TrapMatch/' + tm_core.VERSION

    def log_message(self, fmt, *args):  # 画面には出さない
        pass

    def send_response(self, code, message=None):
        self._started = True     # ここから先は、別の応答を重ねて送れない
        BaseHTTPRequestHandler.send_response(self, code, message)

    # ---- 共通

    def _host_ok(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]').lower()
        return host in ('127.0.0.1', 'localhost', '::1')

    def send_bytes(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_bytes(body, 'application/json; charset=utf-8', status, {'Cache-Control': 'no-store'})

    def read_json(self):
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            raise HttpError(415, 'Content-Type は application/json にしてください')
        raw = getattr(self, '_body', b'') or b'{}'
        try:
            obj = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise HttpError(400, 'JSON を読めません')
        if not isinstance(obj, dict):
            raise HttpError(400, 'JSON の形式が不正です')
        return obj

    def _dispatch(self, table):
        self._started = False
        if not self._host_ok():
            self.send_json({'error': 'このアドレスからは使えません'}, 403)
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        fn = table.get(u.path)
        try:
            if fn is None:
                if self.command == 'GET' and (u.path == '/' or u.path.startswith('/static/')):
                    self.serve_static(u.path)
                    return
                raise HttpError(404, '見つかりません: %s' % u.path)
            fn(self, q)
        except DISCONNECT:
            self.close_connection = True
        except HttpError as e:
            self._fail(e.status, e.message)
        except (KeyError, ValueError, RuntimeError) as e:
            msg = e.args[0] if e.args else str(e)
            self._fail(400 if not isinstance(e, KeyError) else 404, '%s' % msg)
        except Exception as e:  # noqa: BLE001
            APP.log('%s %s\n%s' % (self.command, self.path, traceback.format_exc()))
            self._fail(500, '内部エラー: %s' % e)

    def _fail(self, status, message):
        if getattr(self, '_started', False):
            # 応答の途中で失敗した（動画の読み取り中に接続先が切れたなど）。続きは送れないので接続を閉じる
            self.close_connection = True
            return
        try:
            self.send_json({'error': message}, status)
        except Exception:  # noqa: BLE001
            self.close_connection = True

    def do_GET(self):
        self._dispatch(GET_ROUTES)

    def do_POST(self):
        # 本文は必ず読み切る（読み残すと、同じ接続の次の要求が壊れる）
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n < 0 or n > 64 * 1024 * 1024:
            self._started = False
            self._fail(413 if n > 0 else 400, 'データの大きさが不正です')
            self.close_connection = True
            return
        self._body = self.rfile.read(n) if n else b''
        self._dispatch(POST_ROUTES)

    # ---- 静的ファイル

    def serve_static(self, upath):
        rel = 'index.html' if upath == '/' else upath[len('/static/'):]
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            raise HttpError(404, '見つかりません')
        with open(full, 'rb') as f:
            body = f.read()
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower(), 'application/octet-stream')
        self.send_bytes(body, ctype, 200, {'Cache-Control': 'no-cache'})

    # ---- 動画

    def send_range(self, size, ctype, read):
        """Range 付きの応答。read(位置, 長さ) で中身を取り出す。"""
        start, end, status = 0, size - 1, 200
        rng = (self.headers.get('Range') or '').strip()
        m = re.match(r'^bytes=(\d*)-(\d*)$', rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1) == '':
                start = max(0, size - int(m.group(2)))
            else:
                start = int(m.group(1))
                if m.group(2):
                    end = min(size - 1, int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(length))
        if status == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        pos, left = start, length
        while left > 0:
            chunk = read(pos, min(1048576, left))
            if not chunk:
                self.close_connection = True    # 途中で読めなくなった
                break
            self.wfile.write(chunk)
            pos += len(chunk)
            left -= len(chunk)

    def serve_file(self, path, ctype):
        try:
            size = os.path.getsize(path)
        except OSError as err:
            raise HttpError(404, 'ファイルを開けません: %s' % (err.strerror or err))

        def read(pos, n):
            # 送るたびに開いて閉じる。ブラウザが続きを取りに来るまでのあいだファイルをつかんだままにすると、
            # Windows ではその動画をエクスプローラーで動かしたり消したりできなくなるため。
            try:
                with open(path, 'rb') as f:
                    f.seek(pos)
                    return f.read(n)
            except OSError:
                return b''

        self.send_range(size, ctype, read)


def _season(q):
    s = (q.get('season') or '').strip()
    if not s:
        raise HttpError(400, 'Season が指定されていません')
    return s


def _resolve(q):
    try:
        return tm_core.resolve_file(APP.store, _season(q), int(q.get('id', '0')), q.get('src', ''),
                                    int(q.get('n', '0') or 0))
    except KeyError as e:
        raise HttpError(404, e.args[0])


# ---- GET

def g_ping(h, q):
    h.send_json({'app': 'TrapMatch', 'version': tm_core.VERSION, 'data_dir': APP.store.data_dir,
                 'pid': os.getpid()})


def g_state(h, q):
    job = APP.jobs.current()
    h.send_json({
        'app': 'TrapMatch', 'version': tm_core.VERSION, 'data_dir': APP.store.data_dir,
        'config': APP.store.config(), 'projects': APP.store.projects(),
        'ffmpeg': bool(APP.ffmpeg()), 'platform': sys.platform,
        'job': job.as_dict() if job else None,
    })


def g_seasons(h, q):
    seasons, errors = tm_core.list_seasons(APP.store.config())
    h.send_json({'seasons': seasons, 'errors': errors, 'projects': APP.store.projects()})


def g_job(h, q):
    job = APP.jobs.current()
    if job is not None and job.state == 'error' and getattr(job, 'trace', None) and not getattr(job, 'logged', False):
        job.logged = True
        APP.log('job %s (%s)\n%s' % (job.kind, job.season, job.trace))
    h.send_json({'job': job.as_dict() if job else None})


def g_items(h, q):
    season = _season(q)
    if not APP.store.has_project(season):
        h.send_json({'season': season, 'scanned': False})
        return
    data = tm_core.load_payload(APP.store, season)
    data['scanned'] = True
    h.send_json(data)


def g_item(h, q):
    h.send_json(tm_core.item_detail(APP.store, _season(q), int(q.get('id', '0'))))


def g_browse(h, q):
    h.send_json(browse(q.get('path', ''), q.get('mode', 'folder')))


def g_media(h, q):
    path, _src = _resolve(q)
    ext = tm_core.ext_of(path).lower()
    h.serve_file(path, VIDEO_TYPES.get(ext, 'application/octet-stream'))


def g_avinfo(h, q):
    path, _src = _resolve(q)
    try:
        info = tm_media.avi_info_cached(path)
    except (OSError, ValueError) as e:
        raise HttpError(400, 'AVI を読めません: %s' % e)
    n = len(info['frames'] or [])
    tok = APP.new_token(path, info) if (info['mjpeg'] and n) else ''
    h.send_json({'token': tok, 'fps': info['fps'], 'n': n, 'width': info['width'],
                 'height': info['height'], 'mjpeg': bool(info['mjpeg'] and n), 'codec': info['codec'],
                 'ffmpeg': bool(APP.ffmpeg())})


def g_frame(h, q):
    hit = APP.token(q.get('t', ''))
    if hit is None:
        raise HttpError(404, '再生の準備が切れました。もう一度選び直してください。')
    path, info = hit
    try:
        data = tm_media.read_avi_frame(path, info, int(q.get('i', '0') or 0))
    except (OSError, ValueError) as e:
        raise HttpError(404, 'コマを読めません: %s' % e)
    h.send_bytes(data, 'image/jpeg', 200, {'Cache-Control': 'private, max-age=300'})


def g_transcode(h, q):
    path, _src = _resolve(q)
    ff = APP.ffmpeg()
    if not ff:
        raise HttpError(501, 'ffmpeg が見つかりません')
    cmd = [ff, '-v', 'error', '-nostdin', '-i', path, '-map', '0:v:0', '-map', '0:a:0?',
           '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '27', '-pix_fmt', 'yuv420p',
           '-vf', "scale='min(1280,iw)':-2", '-c:a', 'aac', '-b:a', '96k',
           '-movflags', 'frag_keyframe+empty_moov+default_base_moof', '-f', 'mp4', 'pipe:1']
    try:
        st = os.stat(path)
    except OSError as err:
        raise HttpError(404, 'ファイルを開けません: %s' % (err.strerror or err))
    key = (path, st.st_size, int(st.st_mtime))
    with APP.lock:
        data = APP.transcoded.get(key)
    if data is None:
        # 変換は最後まで済ませてから送る。ブラウザの読み取りを待つあいだ ffmpeg が元の動画を
        # 開いたままにならないようにするため（短い動画を想定）。
        flags = 0x08000000 if sys.platform.startswith('win') else 0  # コンソールの窓を出さない
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=flags)
        try:
            data, _ = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise HttpError(500, '変換に時間がかかりすぎました')
        if proc.returncode != 0 or not data:
            raise HttpError(500, 'ffmpeg で変換できませんでした')
        with APP.lock:
            APP.transcoded[key] = data
            while len(APP.transcoded) > 3 or sum(len(v) for v in APP.transcoded.values()) > 300 * 1048576:
                if len(APP.transcoded) <= 1:
                    break
                APP.transcoded.popitem(last=False)
    h.send_range(len(data), 'video/mp4', lambda pos, n: data[pos:pos + n])


# ---- POST

def p_config(h, q):
    body = h.read_json()
    cfg = APP.store.save(body)
    h.send_json({'config': cfg, 'ffmpeg': bool(APP.ffmpeg())})


def p_scan(h, q):
    body = h.read_json()
    season = (body.get('season') or '').strip()
    # 走査する Season の名前（N250102_P01 と入力されても、設定によっては N250102 として走査する）。
    # 走査中にその Season の判断を変えられないようにするため、ここで決めておく。
    by_prefix = bool(APP.store.config()['settings'].get('season_prefix', True))
    season = tm_core.season_key(season, by_prefix)
    if not season:
        raise HttpError(400, 'Season を指定してください')
    job = APP.jobs.start('scan', season, lambda job: tm_core.run_scan(APP.store, season, job))
    h.send_json({'job': job.as_dict()})


def _not_while_scanning(season):
    """走査の最後に作業状態を書き直すので、その Season の走査中は変更を受け付けない。

    CSV への書き足し（csv_add）も、CSV を読み直して作業状態を書き直すので同じ扱いにする。
    """
    job = APP.jobs.current()
    if job is not None and job.state == 'running' and job.kind in ('scan', 'csv_add') \
            and job.season.casefold() == season.casefold():
        raise HttpError(409, '走査が終わるまで、判断とメモは変更できません' if job.kind == 'scan'
                        else 'CSV への書き足しが終わるまで、判断とメモは変更できません')


def p_decide(h, q):
    body = h.read_json()
    season = (body.get('season') or '').strip()
    _not_while_scanning(season)
    if 'pairs' in body:
        n = tm_core.set_decisions_bulk(APP.store, season, [(int(i), str(d)) for i, d in body['pairs']])
    else:
        ids = [int(i) for i in (body.get('ids') or [])]
        n = tm_core.set_decision(APP.store, season, ids, str(body.get('decision') or ''))
    h.send_json({'ok': True, 'count': n})


def p_field(h, q):
    body = h.read_json()
    season = (body.get('season') or '').strip()
    _not_while_scanning(season)
    tm_core.set_field(APP.store, season, int(body.get('id', 0)),
                      str(body.get('field') or ''), body.get('value'))
    h.send_json({'ok': True})


def p_open(h, q):
    body = h.read_json()
    try:
        path, _src = tm_core.resolve_file(APP.store, (body.get('season') or '').strip(),
                                          int(body.get('id', 0)), str(body.get('src') or ''),
                                          int(body.get('n', 0) or 0))
    except KeyError as e:
        raise HttpError(404, e.args[0])
    if not os.path.exists(path):
        raise HttpError(404, 'ファイルが見つかりません: %s' % path)
    open_in_os(path, reveal=(body.get('mode') == 'folder'))
    h.send_json({'ok': True})


def p_openpath(h, q):
    body = h.read_json()
    path = os.path.abspath(str(body.get('path') or ''))
    root = APP.store.data_dir
    if not (path == root or path.startswith(root + os.sep)) or not os.path.exists(path):
        raise HttpError(400, 'このフォルダは開けません')
    open_in_os(path, reveal=os.path.isfile(path))
    h.send_json({'ok': True})


def p_export(h, q):
    body = h.read_json()
    season = (body.get('season') or '').strip()
    kind = body.get('kind')
    if kind == 'result':
        h.send_json(tm_core.export_result(APP.store, season))
    elif kind == 'plan':
        h.send_json(tm_core.export_plan(APP.store, season))
    elif kind == 'csv_counts':
        h.send_json(tm_core.csv_change_counts(APP.store, season))
    elif kind == 'csv':
        opts = body.get('options') or {}
        job = APP.jobs.start('export_csv', season, lambda job: tm_core.export_csv(APP.store, season, opts, job))
        h.send_json({'job': job.as_dict()})
    elif kind == 'dtcheck':
        job = APP.jobs.start('dtcheck', season, lambda job: tm_core.dt_check(APP.store, season, job))
        h.send_json({'job': job.as_dict()})
    elif kind == 'csv_add_counts':
        h.send_json(tm_core.csv_add_counts(APP.store, season))
    elif kind == 'csv_add':
        # 元の CSV に書き足す。画面で行数を確かめて「実行」を押したときだけ受け付ける
        if body.get('confirm') is not True:
            raise HttpError(400, '確認が済んでいません')
        opts = body.get('options') or {}
        job = APP.jobs.start('csv_add', season, lambda job: tm_core.csv_add_rows(APP.store, season, opts, job))
        h.send_json({'job': job.as_dict()})
    elif kind == 'all_counts':
        h.send_json(tm_core.all_seasons_summary(APP.store))
    elif kind == 'all':
        opts = body.get('options') or {}
        job = APP.jobs.start('export_all', season, lambda job: tm_core.export_all(APP.store, opts, job))
        h.send_json({'job': job.as_dict()})
    else:
        raise HttpError(400, '書き出しの種類が不正です')


def p_quit(h, q):
    """サーバーを終了する。試験用の起動か、起動時に作業用フォルダへ書いた合言葉を示したときだけ。

    合言葉での終了は、新しい版を起動したときに、動いたままの古い版を止めるために使う。
    走査や書き出しの途中では止めない。
    """
    body = h.read_json()
    token = str(body.get('token') or '')
    by_token = bool(token) and token == APP.quit_token
    if not (APP.allow_quit or by_token):
        raise HttpError(403, '終了は許可されていません')
    job = APP.jobs.current()
    if by_token and job is not None and job.state == 'running':
        raise HttpError(409, '処理の途中のため、終了できません（%s）' % (job.phase or job.kind))
    h.send_json({'ok': True})
    threading.Thread(target=APP.server.shutdown, daemon=True).start()


GET_ROUTES = {
    '/api/ping': g_ping, '/api/state': g_state, '/api/seasons': g_seasons, '/api/job': g_job,
    '/api/items': g_items, '/api/item': g_item, '/api/browse': g_browse, '/api/avinfo': g_avinfo,
    '/media': g_media, '/frame': g_frame, '/transcode': g_transcode,
}
POST_ROUTES = {
    '/api/config': p_config, '/api/scan': p_scan, '/api/decide': p_decide, '/api/field': p_field,
    '/api/open': p_open, '/api/openpath': p_openpath, '/api/export': p_export, '/api/quit': p_quit,
}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows では SO_REUSEADDR があると同じポートを二重に開けてしまうため使わない
    allow_reuse_address = not sys.platform.startswith('win')

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECT + (TimeoutError,)):
            return      # ブラウザ側が接続を閉じただけ
        if APP is not None:
            APP.log('connection error %s\n%s' % (client_address, traceback.format_exc()))


def running_info(port, data_dir):
    """このポートで、同じ作業用フォルダの TrapMatch が動いていれば、その情報（版など）を返す。"""
    try:
        opener = build_opener(ProxyHandler({}))     # プロキシの設定があっても、この PC に直接つなぐ
        with opener.open('http://127.0.0.1:%d/api/ping' % port, timeout=1.5) as r:
            info = json.loads(r.read().decode('utf-8'))
        if info.get('app') == 'TrapMatch' and \
                os.path.normcase(info.get('data_dir', '')) == os.path.normcase(data_dir):
            return info
    except Exception:  # noqa: BLE001
        pass
    return None


def already_running(port, data_dir):
    return running_info(port, data_dir) is not None


def port_file(data_dir):
    return os.path.join(data_dir, 'server.json')


def read_port_file(data_dir):
    try:
        with open(port_file(data_dir), 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def last_port(data_dir):
    try:
        return int(read_port_file(data_dir).get('port') or 0)
    except (ValueError, TypeError):
        return 0


def stop_running(port, data_dir, wait=10.0):
    """動いている TrapMatch に終了を頼み、止まったら True。

    合言葉は、動いている側が起動時に作業用フォルダへ書いたものを使う。
    合言葉に対応していない古い版や、処理の途中の版は止められないので False。
    """
    token = str(read_port_file(data_dir).get('token') or '')
    if not token:
        return False
    try:
        opener = build_opener(ProxyHandler({}))
        req = Request('http://127.0.0.1:%d/api/quit' % port,
                      data=json.dumps({'token': token}).encode('utf-8'),
                      headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=3) as r:
            r.read()
    except Exception:  # noqa: BLE001
        return False
    end = time.time() + wait
    while time.time() < end:
        if running_info(port, data_dir) is None:
            time.sleep(0.4)             # ポートが空くのを少し待つ
            return True
        time.sleep(0.25)
    return False


def wait_for_enter():
    """窓がすぐ閉じて文が読めなくならないよう、Enter を待つ（画面から起動したときだけ）。"""
    sys.stdout.flush()
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input('Enter キーを押すと、この窓を閉じます。')
    except (EOFError, OSError, ValueError):
        pass


def set_console_title(title):
    """黒い窓の題を付ける（exe をダブルクリックしたときは、題が exe の場所になるため）。"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:  # noqa: BLE001
        pass


def main(argv=None):
    global APP
    ap = argparse.ArgumentParser(description='TrapMatch: カメラトラップ動画の保存場所間照合アプリ')
    ap.add_argument('--port', type=int, default=8770)
    ap.add_argument('--data-dir', default=os.path.join(os.path.expanduser('~'), 'TrapMatchData'),
                    help='設定と作業状態の保存先（既定: ホームフォルダの TrapMatchData）')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--allow-quit', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--version', action='version', version='TrapMatch ' + tm_core.VERSION)
    args = ap.parse_args(argv)
    data_dir = os.path.abspath(args.data_dir)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')    # 文字コードの違う端末でも止まらないようにする
        except (AttributeError, ValueError):
            pass

    # 同じ保存先で二重に起動しないよう、前回使ったポートと既定のポートを確かめる
    for port in dict.fromkeys([last_port(data_dir), args.port]):
        info = running_info(port, data_dir) if port else None
        if info is None:
            continue
        url = 'http://127.0.0.1:%d/' % port
        running = str(info.get('version') or '?')
        if running == tm_core.VERSION:
            print('TrapMatch はすでに起動しています: ' + url)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        # 別の版が動いたまま（プログラムを更新したあとなど）: 終了させて、この版で起動し直す
        print('別の版の TrapMatch（%s）が動いています。終了させて、この版（%s）で起動し直します。'
              % (running, tm_core.VERSION))
        sys.stdout.flush()
        if stop_running(port, data_dir):
            break
        print('')
        print('動いている TrapMatch（%s）を自動では終了できませんでした。' % running)
        print('走査や書き出しの途中でなければ、その黒い窓（タイトルが TrapMatch の窓）を閉じてから、')
        print('もう一度 TrapMatch を起動してください。')
        wait_for_enter()
        return 0

    set_console_title('TrapMatch')
    APP = App(data_dir, allow_quit=args.allow_quit)
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = Server(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print('ポートを開けませんでした（%d から %d）' % (args.port, args.port + 19))
        return 1
    APP.server = server
    APP.port = server.server_address[1]
    try:
        with open(port_file(data_dir), 'w', encoding='utf-8') as f:
            json.dump({'port': APP.port, 'pid': os.getpid(), 'version': tm_core.VERSION,
                       'token': APP.quit_token}, f)
    except OSError:
        pass
    url = 'http://127.0.0.1:%d/' % APP.port
    print('TrapMatch %s' % tm_core.VERSION)
    print('  画面:       ' + url)
    print('  データ:     ' + data_dir)
    print('  終了するには、この窓を閉じてください。')
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.remove(port_file(data_dir))
        except OSError:
            pass
    return 0


def run():
    """起動の入口。exe では、エラーで終わるときに窓がすぐ閉じないようにする。"""
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        code = 1
    if code and FROZEN:
        print('')
        print('TrapMatch はエラーで終了しました。上の文を確かめてください。')
        wait_for_enter()
    return code


if __name__ == '__main__':
    sys.exit(run())
