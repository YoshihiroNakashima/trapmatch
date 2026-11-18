# -*- coding: utf-8 -*-
"""設定、走査、照合、作業状態（SQLite）、書き出し。"""
import csv
import json
import ntpath
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import tm_csv
import tm_media

VERSION = '0.3.0'

# 状態のビット
F_MISSING = 1    # 欠け（一部の保存場所にしかない）
F_CSVONLY = 2    # CSV のみ（どのフォルダにも動画がない）
F_SIZE = 4       # サイズ違い（拡張子が同じなのにサイズが異なる）
F_DUP = 8        # 場所内の重複
F_EXTDIFF = 16   # 拡張子違い（保存場所のあいだで拡張子が異なる）
F_CSVEXT = 32    # CSV の拡張子が実ファイルと違う（読み込み時に計算）
F_ZERO = 64      # サイズ 0 のファイルがある
F_NEEDS = F_MISSING | F_CSVONLY | F_SIZE | F_DUP | F_ZERO   # 判断が必要な状態

STATE_LABELS = [
    (F_MISSING, '欠け'), (F_CSVONLY, 'CSV のみ'), (F_SIZE, 'サイズ違い'), (F_DUP, '場所内の重複'),
    (F_EXTDIFF, '拡張子違い'), (F_CSVEXT, 'CSV の拡張子違い'), (F_ZERO, 'サイズ 0'),
]
DECISION_LABELS = {'': '未判断', 'keep': '残す', 'delete': '削除する', 'except': '例外として残す'}
DECISIONS = ('', 'keep', 'delete', 'except')

# CSV に足す行の、列ごとの既定値。列名は大文字と小文字を区別せずに CSV の見出しと照らし、
# CSV にない列は無視する。Season、Station、File、DateTime はここでは扱わない（別に決まる）。
# ここにない列は空欄になる。設定（add_defaults）で変えられる。
# 値の中の {Season} は Season 名に、{Folder} はその動画（Station）の Season フォルダの名前に置き換わる。
# Season フォルダの名前は、N250102_P01 のように Season 名の後ろに文字が付いていることがある。
ADD_ROW_DEFAULTS = {
    'Season_sub_folder': '{Folder}',
    'DeleteFlag': 'FALSE', 'Pass': 'TRUE', 'Star': 'FALSE', 'Remarks': 'NA', 'PassCount': '0',
    'Indep': 'TRUE', 'LeftCens': 'FALSE', 'RightCens': 'FALSE',
    'StartTime': '999.9', 'EndTime': '999.9', 'Species': 'Add',
}
MAX_ADD_DEFAULTS = 100

DEFAULT_SETTINGS = {
    'ignore_ext': True,
    'exts': ['avi', 'mp4', 'mov'],
    'auto_next': True,
    'autoplay': True,
    'embedded_tz': 'asis',   # 'asis': 記録されたままの時刻 / 'utc': UTC とみなして PC の時刻帯に直す
    # CSV に足す行の DateTime の取得元
    #   'embedded':       動画に埋め込まれた撮影日時のみ（なければ NA）
    #   'embedded_mtime': 埋め込まれた撮影日時、なければファイルの更新日時
    'dt_source': 'embedded',
    'add_defaults': dict(ADD_ROW_DEFAULTS),   # CSV に足す行の、列ごとの既定値
    # CSV にしかない動画（どの保存場所にも動画がない）を「残す」と判断した行に付ける印。
    # mark_cols の列名を順に CSV の見出しと照らし、最初に見つかった列に書く。mark_text が空なら付けない。
    'mark_text': 'csvのみ',
    'mark_cols': ['Note', 'Remarks'],
    # Season 名を、先頭の英字 1 文字と数字 6 桁（N250102 など）だけで照合する。
    # N250102_P01 と N250102_P02 のようなフォルダは、どちらも N250102 としてまとめて扱う。
    'season_prefix': True,
    'ext_ref': 'auto',       # CSV の拡張子をそろえる基準の保存場所（'auto' は優先順）
    'ffmpeg': '',
}

# CSV の File 列にあっても照合の対象にしない拡張子
NON_VIDEO_EXTS = {
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tif', 'tiff', 'heic', 'txt', 'csv', 'tsv', 'xlsx', 'xls',
    'json', 'xml', 'db', 'ini', 'log', 'pdf', 'zip', 'wav', 'mp3', 'thm',
}

DEFAULT_DT_FORMAT = '%Y/%m/%d %H:%M:%S'

DT_SOURCES = ('embedded', 'embedded_mtime')

# NAS やOS が作る管理用のフォルダ（Synology の @eaDir、#recycle など）は読まない
SKIP_DIR_PREFIXES = ('@', '.', '$', '#recycle', '#snapshot')
# ドライブの直下を親フォルダにしたときに見える、Windows の管理用フォルダ
SKIP_DIR_NAMES = {'system volume information', 'recovery', 'config.msi', 'found.000', 'msdownld.tmp'}


def skip_dir(name):
    low = name.lower()
    return low.startswith(SKIP_DIR_PREFIXES) or low in SKIP_DIR_NAMES


def natural_key(s):
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r'(\d+)', s)]


def basename_any(p):
    return re.split(r'[\\/]', p)[-1]


def ext_of(name):
    return os.path.splitext(name)[1][1:]


def split_name(name, exts):
    """(名前, 拡張子)。対象の拡張子でなければ、全体を名前として扱う。"""
    base, e = os.path.splitext(name)
    if e[1:].lower() in exts:
        return base, e[1:]
    return name, ''


def make_key(station, name, ignore_ext, exts):
    """照合キー。(キー, 表示する名前, 拡張子) を返す。"""
    name = name.strip()
    stem, ext = split_name(name, exts)
    st = station.strip().casefold()
    if ignore_ext:
        return st + '\t' + stem.casefold(), stem, ext
    return st + '\t' + name.casefold(), name, ext


def season_path(path, season):
    """CSV の場所に書かれた {Season} を Season 名に置き換える（Season ごとに CSV が分かれている場合）。"""
    return re.sub(r'\{season\}', lambda m: season, path, flags=re.I)


def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def stamp():
    return datetime.now().strftime('%Y%m%d_%H%M%S')


def safe_name(s):
    return re.sub(r'[^0-9A-Za-z_\-぀-ヿ一-鿿]+', '_', s.strip()) or '_'


# ---------------------------------------------------------------- 日時の書式

_DT_UNPADDED = re.compile(r'^\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}(:\d{2})?$')
_DT_KNOWN = [
    (re.compile(r'^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$'), '%Y/%m/%d %H:%M:%S'),
    (re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$'), '%Y-%m-%d %H:%M:%S'),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'), '%Y-%m-%dT%H:%M:%SZ'),
    (re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$'), '%Y-%m-%dT%H:%M:%S'),
    (re.compile(r'^\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}$'), '%Y:%m:%d %H:%M:%S'),
    (re.compile(r'^\d{4}/\d{2}/\d{2} \d{2}:\d{2}$'), '%Y/%m/%d %H:%M'),
    (re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$'), '%Y-%m-%d %H:%M'),
]


def detect_dt_format(samples):
    """CSV の既存の DateTime の値から書式を決める。"""
    counts = {}
    unpadded = 0
    unpadded_sec = 0
    for s in samples:
        s = s.strip()
        if not s or s.upper() == 'NA':
            continue
        hit = None
        for rx, fmt in _DT_KNOWN:
            if rx.match(s):
                hit = fmt
                break
        if hit:
            counts[hit] = counts.get(hit, 0) + 1
        elif _DT_UNPADDED.match(s):
            unpadded += 1
            if s.count(':') == 2:
                unpadded_sec += 1
    if unpadded:
        # 「2026/1/5 9:05:21」のような 0 埋めのない書式（Excel で保存した CSV など）
        slash_sec = counts.get('%Y/%m/%d %H:%M:%S', 0) + unpadded_sec
        slash_nosec = counts.get('%Y/%m/%d %H:%M', 0) + (unpadded - unpadded_sec)
        return 'unpadded/sec' if slash_sec >= slash_nosec else 'unpadded/nosec'
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return DEFAULT_DT_FORMAT


def format_dt(dt, fmt):
    if fmt == 'unpadded/sec':
        return '%d/%d/%d %d:%02d:%02d' % (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    if fmt == 'unpadded/nosec':
        return '%d/%d/%d %d:%02d' % (dt.year, dt.month, dt.day, dt.hour, dt.minute)
    return dt.strftime(fmt or DEFAULT_DT_FORMAT)


# ---------------------------------------------------------------- 設定と保存先

class Store(object):
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        for sub in ('seasons', 'exports', 'logs'):
            os.makedirs(os.path.join(self.data_dir, sub), exist_ok=True)
        self.lock = threading.RLock()
        self.cfg_path = os.path.join(self.data_dir, 'config.json')
        self.cfg = self._load()

    def _load(self):
        cfg = self.normalize({})          # 既定の設定（組み込みの値とは別の写しを持つ）
        if os.path.isfile(self.cfg_path):
            try:
                with open(self.cfg_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                cfg = self.normalize(raw)
            except (OSError, ValueError):
                pass
        return cfg

    @staticmethod
    def normalize(raw):
        out = {'sources': [], 'settings': json.loads(json.dumps(DEFAULT_SETTINGS))}
        seen = set()
        for s in (raw.get('sources') or []):
            if not isinstance(s, dict):
                continue
            typ = s.get('type')
            if typ not in ('folder', 'csv'):
                continue
            sid = str(s.get('id') or '')
            if not re.match(r'^[A-Za-z0-9_]{1,24}$', sid) or sid in seen:
                sid = 's' + uuid.uuid4().hex[:8]
            seen.add(sid)
            path = str(s.get('path') or '').strip().strip('"')
            if re.match(r'^[A-Za-z]:$', path):
                path += '\\'      # 「E:」だけだと Windows では別の意味になるため、ドライブの直下にする
            if path:
                path = os.path.normpath(path)   # Windows では E:/PRM_N を E:\PRM_N にそろえる
            name = str(s.get('name') or '').strip() or ('CSV' if typ == 'csv' else 'フォルダ')
            out['sources'].append({
                'id': sid, 'name': name[:40], 'type': typ, 'path': path,
                'enabled': bool(s.get('enabled', True)), 'readonly': bool(s.get('readonly', False)),
            })
        st = raw.get('settings') or {}
        o = out['settings']
        for k in ('ignore_ext', 'auto_next', 'autoplay'):
            if k in st:
                o[k] = bool(st[k])
        exts = st.get('exts')
        if isinstance(exts, str):
            exts = re.split(r'[\s,;、]+', exts)
        if isinstance(exts, list):
            cleaned = []
            for e in exts:
                e = str(e).strip().lstrip('.').lower()
                if re.match(r'^[a-z0-9]{1,8}$', e) and e not in cleaned:
                    cleaned.append(e)
            if cleaned:
                o['exts'] = cleaned
        if st.get('embedded_tz') in ('asis', 'utc'):
            o['embedded_tz'] = st['embedded_tz']
        if st.get('dt_source') in DT_SOURCES:
            o['dt_source'] = st['dt_source']
        # 設定に項目がなければ（以前の版の設定など）組み込みの既定値を使う。空の指定は「既定値なし」。
        # season_prefix の項目がない設定は、0.2.0 以前に保存されたもの
        o['add_defaults'] = clean_add_defaults(st['add_defaults'], migrate='season_prefix' not in st) \
            if 'add_defaults' in st else dict(ADD_ROW_DEFAULTS)
        if isinstance(st.get('season_prefix'), bool):
            o['season_prefix'] = st['season_prefix']
        if isinstance(st.get('mark_text'), str):
            t = st['mark_text'].strip()
            if len(t) <= 100 and not any(c in t for c in '\r\n\x00'):
                o['mark_text'] = t                       # 空にすると、印を付けない
        mc = st.get('mark_cols')
        if isinstance(mc, str):
            mc = re.split(r'[,;、\n]+', mc)
        if isinstance(mc, list):
            names = []
            for x in mc:
                x = str(x).strip()
                if x and len(x) <= 100 and not any(x.casefold() == y.casefold() for y in names):
                    names.append(x)
            o['mark_cols'] = names[:10]
        if isinstance(st.get('ext_ref'), str) and st['ext_ref']:
            o['ext_ref'] = st['ext_ref']
        if isinstance(st.get('ffmpeg'), str):
            o['ffmpeg'] = st['ffmpeg'].strip().strip('"')
        return out

    def config(self):
        with self.lock:
            return json.loads(json.dumps(self.cfg))

    def save(self, raw):
        cfg = self.normalize(raw)
        with self.lock:
            tmp = self.cfg_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.cfg_path)
            self.cfg = cfg
        return self.config()

    def db_path(self, season):
        d = os.path.join(self.data_dir, 'seasons')
        want = safe_name(season) + '.sqlite'
        try:
            for name in os.listdir(d):
                if name.casefold() == want.casefold():
                    return os.path.join(d, name)
        except OSError:
            pass
        return os.path.join(d, want)

    def has_project(self, season):
        return os.path.isfile(self.db_path(season))

    def projects(self):
        out = []
        d = os.path.join(self.data_dir, 'seasons')
        for name in sorted(os.listdir(d)):
            if name.endswith('.sqlite'):
                try:
                    con = sqlite3.connect(os.path.join(d, name))
                    try:
                        row = con.execute("SELECT value FROM meta WHERE key='season'").fetchone()
                        row2 = con.execute("SELECT value FROM meta WHERE key='scanned_at'").fetchone()
                    finally:
                        con.close()
                    if row:
                        out.append({'season': json.loads(row[0]),
                                    'scanned_at': json.loads(row2[0]) if row2 else ''})
                except (sqlite3.Error, ValueError):
                    continue
        return out

    def connect(self, season, create=False):
        path = self.db_path(season)
        if not create and not os.path.isfile(path):
            raise KeyError('この Season はまだ走査されていません: %s' % season)
        con = sqlite3.connect(path, timeout=30)
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=NORMAL')
        if create:
            con.executescript(SCHEMA)
        return con

    def export_dir(self, season):
        d = os.path.join(self.data_dir, 'exports', safe_name(season))
        os.makedirs(d, exist_ok=True)
        return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, ord INTEGER, station TEXT, stem TEXT,
  mask INTEGER, flags INTEGER, files TEXT, csv TEXT,
  decision TEXT DEFAULT '', memo TEXT DEFAULT '', pick TEXT DEFAULT '', csv_fields TEXT DEFAULT '',
  dt_emb TEXT, dt_kind TEXT, dt_src TEXT, decided_at TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS gone(
  key TEXT PRIMARY KEY, station TEXT, stem TEXT, decision TEXT, memo TEXT, pick TEXT,
  csv_fields TEXT, decided_at TEXT, gone_at TEXT);
"""


def get_meta(con):
    return {k: json.loads(v) for k, v in con.execute('SELECT key, value FROM meta')}


def set_meta(con, **kw):
    for k, v in kw.items():
        con.execute('INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)',
                    (k, json.dumps(v, ensure_ascii=False)))


# ---------------------------------------------------------------- ジョブ

class Job(object):
    def __init__(self, kind, season):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.season = season
        self.state = 'running'
        self.phase = ''
        self.done = 0
        self.total = 0
        self.result = None
        self.error = None
        self.started = time.time()
        self._lock = threading.Lock()

    def update(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def as_dict(self):
        with self._lock:
            return {'id': self.id, 'kind': self.kind, 'season': self.season, 'state': self.state,
                    'phase': self.phase, 'done': self.done, 'total': self.total,
                    'result': self.result, 'error': self.error,
                    'elapsed': round(time.time() - self.started, 1)}


class JobRunner(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None

    def start(self, kind, season, func):
        with self.lock:
            if self.job is not None and self.job.state == 'running':
                raise RuntimeError('別の処理が実行中です（%s）。終わるまでお待ちください。' % self.job.phase)
            job = Job(kind, season)
            self.job = job

        def run():
            try:
                res = func(job)
                job.update(result=res, state='done')
            except Exception as e:  # noqa: BLE001
                import traceback
                job.update(error='%s' % e, state='error')
                job.trace = traceback.format_exc()

        t = threading.Thread(target=run, name='job-' + kind, daemon=True)
        t.start()
        return job

    def current(self):
        with self.lock:
            return self.job


# ---------------------------------------------------------------- 走査

def list_seasons(cfg):
    """フォルダ型の保存場所の直下にあるフォルダ名（Season の候補）。

    Season 名を先頭だけで照合する設定のときは、N250102_P01 と N250102_P02 を N250102 の 1 つにまとめる。
    """
    by_prefix = bool(cfg['settings'].get('season_prefix', True))
    names = {}
    errors = []
    for s in cfg['sources']:
        if s['type'] != 'folder' or not s.get('enabled', True) or not s.get('path'):
            continue
        try:
            with os.scandir(s['path']) as it:
                for e in it:
                    try:
                        if e.is_dir() and not skip_dir(e.name):
                            key = season_key(e.name, by_prefix)
                            names.setdefault(key.casefold(), key)
                    except OSError:
                        continue
        except OSError as err:
            errors.append('%s: %s' % (s['name'], err.strerror or err))
    out = sorted(names.values(), key=natural_key, reverse=True)
    return out, errors


def scan_station(path, exts):
    videos = []
    nontarget = 0
    subdirs = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir():
                    if not skip_dir(e.name):
                        subdirs.append(e.path)
                    continue
                if e.name.startswith('._'):
                    nontarget += 1      # macOS が NAS に作る付属ファイル（._IMG_0001.AVI など）
                elif ext_of(e.name).lower() in exts:
                    st = e.stat()
                    videos.append((e.name, st.st_size, int(st.st_mtime)))
                else:
                    nontarget += 1
            except OSError:
                nontarget += 1
    return videos, nontarget, subdirs


def count_videos_below(path, exts, depth=5):
    """フォルダの中（下の階層を含む）にある動画の本数。想定外の階層の報告用。"""
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir():
                        if depth > 0 and not skip_dir(e.name):
                            n += count_videos_below(e.path, exts, depth - 1)
                    elif not e.name.startswith('._') and ext_of(e.name).lower() in exts:
                        n += 1
                except OSError:
                    continue
    except OSError:
        return 0
    return n


def norm_season(season):
    """入力された Season 名を整える（全角の英数字を半角にし、前後の空白を除く）。"""
    return unicodedata.normalize('NFKC', season or '').strip()


_SEASON_PREFIX = re.compile(r'[A-Za-z]\d{6}')


def season_key(name, by_prefix=True):
    """Season を見分ける名前。

    by_prefix のときは、先頭の英字 1 文字と数字 6 桁（N250102 など）だけを使い、その後ろは無視する。
    N250102_P01 も N250102_P02 も N250102 になる。この形で始まらない名前は、全体をそのまま使う。
    """
    s = norm_season(name)
    if by_prefix:
        m = _SEASON_PREFIX.match(s)
        if m:
            return m.group(0)
    return s


def season_dirs(src, season, by_prefix=True):
    """保存場所の直下にある、その Season のフォルダの名前（自然な順）。

    Season 名の後ろに文字が付いたフォルダ（N250102_P01 など）が複数あれば、すべて返す。
    """
    cf = season_key(season, by_prefix).casefold()
    out = []
    with os.scandir(src['path']) as it:
        for e in it:
            try:
                if e.is_dir() and not skip_dir(e.name) and season_key(e.name, by_prefix).casefold() == cf:
                    out.append(e.name)
            except OSError:
                continue
    out.sort(key=natural_key)
    return out


def season_dir_of(f, season):
    """ファイルの情報から、そのファイルが入っている Season フォルダの名前を返す。

    0.2.0 以前の走査結果には記録がないので、そのときは Season 名（当時はフォルダ名と同じ）。
    """
    return f[4] if len(f) > 4 and f[4] else season


def compute_flags(files, cj, nfold, mask, full_mask):
    flags = 0
    folder_mask = mask & ((1 << nfold) - 1)
    if folder_mask == 0:
        flags |= F_CSVONLY
    elif mask != full_mask:
        flags |= F_MISSING
    by_ext = {}
    for fl in files.values():
        if len(fl) > 1:
            flags |= F_DUP
        for f in fl:
            by_ext.setdefault(ext_of(f[0]).lower(), set()).add(f[1])
            if f[1] == 0:
                flags |= F_ZERO
    if any(len(v) > 1 for v in by_ext.values()):
        flags |= F_SIZE
    exts = set(by_ext.keys())
    exts.update(csv_exts(cj))
    if len(exts) > 1:
        flags |= F_EXTDIFF
    return flags


def csv_exts(cj):
    """CSV の行に書かれた拡張子（小文字）と行数。空の拡張子は含めない。"""
    if not cj:
        return {}
    if cj.get('extn'):
        return {e: n for e, n in cj['extn'].items() if e}
    return {cj['ext'].lower(): cj.get('n', 1)} if cj.get('ext') else {}


def run_scan(store, season, job):
    cfg = store.config()
    settings = cfg['settings']
    by_prefix = bool(settings.get('season_prefix', True))
    season = season_key(season, by_prefix)
    if not season:
        raise ValueError('Season を指定してください')
    season_cf = season.casefold()

    def is_season(value):
        return season_key(value, by_prefix).casefold() == season_cf

    exts = set(settings['exts'])
    ignore_ext = bool(settings['ignore_ext'])
    sources = [s for s in cfg['sources'] if s.get('enabled', True) and s.get('path')]
    folders = [s for s in sources if s['type'] == 'folder']
    csvs = [dict(s, path=season_path(s['path'], season)) for s in sources if s['type'] == 'csv'][:1]
    used = folders + csvs
    if len(used) < 2:
        raise ValueError('照合には保存場所が 2 つ以上必要です。設定で保存場所を登録してください。')
    for s in folders:
        if not os.path.isdir(s['path']):
            raise ValueError('保存場所「%s」のフォルダが見つかりません: %s\n'
                             '接続を確かめるか、設定で「使う」を外してください。' % (s['name'], s['path']))
    for s in csvs:
        if not os.path.isfile(s['path']):
            raise ValueError('保存場所「%s」の CSV が見つかりません: %s' % (s['name'], s['path']))
    bit = {s['id']: i for i, s in enumerate(used)}
    nfold = len(folders)
    full_mask = (1 << len(used)) - 1
    # season_dirs: 保存場所ごとの、この Season のフォルダ名（N250102_P01 と N250102_P02 のように複数のことがある）
    # station_dirs: 保存場所ごとの、Station（小文字にそろえた名前）-> [Season フォルダ名, Station フォルダ名]
    # station_multi: 保存場所ごとの、複数の Season フォルダにある Station の数
    info = {'missing_season': [], 'nontarget': {}, 'exceptions': [], 'files': {}, 'stations': {},
            'season_dirs': {}, 'station_dirs': {}, 'station_multi': {}}
    tasks = []
    job.update(phase='フォルダの一覧を取得中', done=0, total=0)
    spelling = None
    for s in folders:
        info['nontarget'][s['id']] = 0
        info['files'][s['id']] = 0
        info['stations'][s['id']] = 0
        info['station_dirs'][s['id']] = {}
        dirs = season_dirs(s, season, by_prefix)
        info['season_dirs'][s['id']] = dirs
        if not dirs:
            info['missing_season'].append(s['id'])
            continue
        if spelling is None:
            spelling = season_key(dirs[0], by_prefix)      # フォルダの実際の綴り（大文字小文字、全角と半角）
        seen_in = {}
        for dname in dirs:
            with os.scandir(os.path.join(s['path'], dname)) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if not skip_dir(e.name):
                                tasks.append((s, e.name, e.path, dname))
                                info['station_dirs'][s['id']].setdefault(e.name.casefold(), [dname, e.name])
                                seen_in.setdefault(e.name.casefold(), set()).add(dname)
                        elif ext_of(e.name).lower() in exts:
                            info['exceptions'].append(
                                {'src': s['id'], 'path': e.path, 'kind': 'season', 'count': 1})
                        else:
                            info['nontarget'][s['id']] += 1
                    except OSError:
                        continue
        info['stations'][s['id']] = len(info['station_dirs'][s['id']])
        info['station_multi'][s['id']] = sum(1 for v in seen_in.values() if len(v) > 1)
    if spelling:
        season = spelling

    items = {}

    def get_item(key, station, stem):
        it = items.get(key)
        if it is None:
            it = {'station': station, 'stem': stem, 'files': {}, 'csv': None, 'mask': 0}
            items[key] = it
        return it

    job.update(phase='動画の一覧を取得中', done=0, total=len(tasks))

    def work(t):
        try:
            return t, scan_station(t[2], exts), None
        except OSError as err:
            return t, None, err

    if tasks:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for n, (t, res, err) in enumerate(ex.map(work, tasks), 1):
                s, stname, spath, dname = t
                if err is not None:
                    info['exceptions'].append(
                        {'src': s['id'], 'path': spath, 'kind': 'error', 'count': 0,
                         'note': '%s' % (err.strerror or err)})
                else:
                    videos, nont, subdirs = res
                    info['nontarget'][s['id']] += nont
                    info['files'][s['id']] += len(videos)
                    for name, size, mtime in videos:
                        key, stem, _ext = make_key(stname, name, ignore_ext, exts)
                        it = get_item(key, stname, stem)
                        it['files'].setdefault(s['id'], []).append([name, size, mtime, stname, dname])
                        it['mask'] |= 1 << bit[s['id']]
                    for sd in subdirs:
                        c = count_videos_below(sd, exts)
                        if c:
                            info['exceptions'].append(
                                {'src': s['id'], 'path': sd, 'kind': 'subdir', 'count': c})
                if n % 5 == 0 or n == len(tasks):
                    job.update(done=n)

    csv_meta = None
    if csvs:
        s = csvs[0]
        job.update(phase='CSV を読み込み中', done=0, total=100)

        def prog(pos, size):
            job.update(done=int(100.0 * pos / max(1, size)))

        st = os.stat(s['path'])
        data = tm_csv.read_season(s['path'], season, prog, match=is_season)
        i_station, i_file, i_dt = data['i_station'], data['i_file'], data['i_dt']
        n_blank = n_nontarget = n_rows = 0
        dt_samples = []
        station_names = {}
        b = bit[s['id']]
        if data['rows'] and info['missing_season'] == [x['id'] for x in folders]:
            # どのフォルダにもこの Season がないときは、CSV に書かれた綴りを使う
            season = season_key(data['rows'][0][data['i_season']], by_prefix) or season
        # Season 列の綴りと、Season フォルダの名前の列（Season_sub_folder）
        season_names = {}
        cols_cf = [h.strip().casefold() for h in data['header']]
        i_sub = cols_cf.index('season_sub_folder') if 'season_sub_folder' in cols_cf else -1
        station_sub = {}
        station_season = {}
        for fields in data['rows']:
            sname = fields[data['i_season']].strip()
            season_names[sname] = season_names.get(sname, 0) + 1
            station = fields[i_station].strip()
            fname = basename_any(fields[i_file].strip())
            if not station or not fname:
                n_blank += 1
                continue
            if ext_of(fname).lower() in NON_VIDEO_EXTS and ext_of(fname).lower() not in exts:
                n_nontarget += 1
                continue
            key, stem, ext = make_key(station, fname, ignore_ext, exts)
            it = get_item(key, station, stem)
            dt = fields[i_dt].strip() if 0 <= i_dt < len(fields) else ''
            station_names.setdefault(station.casefold(), station)
            # Station ごとの Season 列の綴り（N250102_P01 のように書かれた CSV で、足す行を同じ綴りにするため）
            if station_season.setdefault(station.casefold(), sname) != sname:
                station_season[station.casefold()] = ''
            if 0 <= i_sub < len(fields):
                # Station ごとの Season フォルダ名。行によって違う Station は、決められないので空にする
                sub = fields[i_sub].strip()
                prev = station_sub.setdefault(station.casefold(), sub)
                if prev != sub:
                    station_sub[station.casefold()] = ''
            if it['csv'] is None:
                it['csv'] = {'n': 0, 'file': fname, 'ext': ext, 'dt': dt, 'rows': [], 'extn': {}}
            cj = it['csv']
            cj['n'] += 1
            cj['extn'][ext.lower()] = cj['extn'].get(ext.lower(), 0) + 1
            if not cj['dt'] and dt:
                cj['dt'] = dt
            if len(cj['rows']) < 3:
                cj['rows'].append(fields)
            it['mask'] |= 1 << b
            n_rows += 1
            if dt and len(dt_samples) < 400:
                dt_samples.append(dt)
        info['files'][s['id']] = n_rows
        csv_meta = {
            'path': s['path'], 'size': st.st_size, 'mtime': int(st.st_mtime),
            'encoding': data['encoding'], 'eol': data['eol'], 'delimiter': data['delimiter'],
            'quote_all': data['quote_all'], 'header': data['header'], 'i_season': data['i_season'], 'i_station': i_station,
            'i_file': i_file, 'i_dt': i_dt, 'records': data['records'], 'rows': n_rows,
            'blank': n_blank, 'nontarget': n_nontarget, 'dtfmt': detect_dt_format(dt_samples),
            'malformed': data.get('malformed', 0), 'station_names': station_names,
            # CSV に足す行の Season 列に使う綴り（この Season の行がすべて同じ綴りのときだけ）
            'season_name': list(season_names)[0] if len(season_names) == 1 else '',
            'i_sub': i_sub, 'station_sub': {k: v for k, v in station_sub.items() if v},
            # Season 列の綴りが 1 つでないときに使う、Station ごとの綴り（その Station の行がすべて同じときだけ）
            'station_season': ({k: v for k, v in station_season.items() if v} if len(season_names) > 1 else {}),
        }

    job.update(phase='照合中', done=0, total=len(items))
    ordered = sorted(items.items(), key=lambda kv: (natural_key(kv[1]['station']), natural_key(kv[1]['stem'])))
    for key, it in ordered:
        for fl in it['files'].values():
            fl.sort(key=lambda f: natural_key(f[0]))
        it['flags'] = compute_flags(it['files'], it['csv'], nfold, it['mask'], full_mask)

    if not ordered and not store.has_project(season):
        # 何も見つからない名前（入力違いなど）で、空の作業状態を作らない
        raise ValueError('「%s」という Season の動画は、どの保存場所にも CSV にも見つかりません。'
                         'Season 名を確かめてください。' % season)
    job.update(phase='作業状態を保存中', done=0, total=len(ordered))
    inherit, inherited_from = ({}, [])
    if by_prefix and not store.has_project(season):
        inherit, inherited_from = earlier_decisions(store, season_cf, by_prefix)
    con = store.connect(season, create=True)
    try:
        old = {}
        for r in con.execute('SELECT key, decision, memo, pick, csv_fields, dt_emb, dt_kind, dt_src,'
                             ' decided_at, files, station, stem, id FROM items'):
            old[r[0]] = r
        gone = {r[0]: r for r in con.execute(
            'SELECT key, decision, memo, pick, csv_fields, decided_at FROM gone')}
        # 動画の番号（id）は再走査しても変えない。古い画面から届いた判断が、別の動画に付かないようにするため。
        prev = get_meta(con)
        next_id = max([int(prev.get('next_id') or 1)] + [r[12] + 1 for r in old.values()])
        con.execute('BEGIN')
        con.execute('DELETE FROM items')
        rows = []
        for n, (key, it) in enumerate(ordered):
            files_json = json.dumps(it['files'], ensure_ascii=False, separators=(',', ':'))
            csv_json = json.dumps(it['csv'], ensure_ascii=False, separators=(',', ':')) if it['csv'] else ''
            dec = memo = pick = cf = decided = ''
            dt_emb = dt_kind = dt_src = None
            o = old.pop(key, None)
            if o is not None:
                item_id = o[12]
                dec, memo, pick, cf, decided = o[1] or '', o[2] or '', o[3] or '', o[4] or '', o[8] or ''
                if o[9] == files_json:
                    dt_emb, dt_kind, dt_src = o[5], o[6], o[7]
            else:
                item_id = next_id
                next_id += 1
                if key in gone:
                    g = gone[key]
                    dec, memo, pick, cf, decided = g[1] or '', g[2] or '', g[3] or '', g[4] or '', g[5] or ''
                    con.execute('DELETE FROM gone WHERE key=?', (key,))
                elif key in inherit:
                    g = inherit[key]
                    dec, memo, pick, cf, decided = g[1] or '', g[2] or '', g[3] or '', g[4] or '', g[5] or ''
            rows.append((item_id, key, n, it['station'], it['stem'], it['mask'], it['flags'], files_json,
                         csv_json, dec, memo, pick, cf, dt_emb, dt_kind, dt_src, decided))
        con.executemany(
            'INSERT INTO items(id, key, ord, station, stem, mask, flags, files, csv, decision, memo, pick,'
            ' csv_fields, dt_emb, dt_kind, dt_src, decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        # 走査から消えた動画の判断は、記録として残す
        for key, o in old.items():
            if o[1] or o[2]:
                con.execute('INSERT OR REPLACE INTO gone(key, station, stem, decision, memo, pick,'
                            ' csv_fields, decided_at, gone_at) VALUES(?,?,?,?,?,?,?,?,?)',
                            (key, o[10], o[11], o[1], o[2], o[3], o[4], o[8], now_text()))
        set_meta(con, season=season, scanned_at=now_text(), version=VERSION, next_id=next_id,
                 sources=[{'id': s['id'], 'name': s['name'], 'type': s['type'], 'path': s['path'],
                           'readonly': s.get('readonly', False)} for s in used],
                 ignore_ext=ignore_ext, exts=sorted(exts), info=info, csv=csv_meta,
                 season_prefix=by_prefix)
        set_meta(con, inherited_from=inherited_from)      # 引き継ぎの知らせは、次に走査するまで出す
        con.commit()
    finally:
        con.close()
    return {'season': season, 'items': len(ordered)}


def earlier_decisions(store, season_cf, by_prefix):
    """同じ Season に当たる、別の名前の作業状態（N250102_P01 など）にある判断とメモを集める。

    Season 名を先頭だけで照合するようになる前に、フォルダ名のまま走査した分を、初めての走査のときに引き継ぐ。
    元の作業状態は消さない。戻り値は ({動画のキー: 行}, [引き継ぎ元の Season 名])。
    """
    found = {}
    names = []
    for p in store.projects():
        name = p['season']
        if name.casefold() == season_cf or season_key(name, by_prefix).casefold() != season_cf:
            continue
        try:
            con = store.connect(name)
        except (KeyError, OSError, sqlite3.Error):
            continue
        try:
            n = 0
            for r in con.execute("SELECT key, decision, memo, pick, csv_fields, decided_at FROM items"
                                 " WHERE decision != '' OR memo != '' OR pick != '' OR csv_fields != ''"):
                if r[0] not in found:
                    found[r[0]] = r
                    n += 1
            if n:
                names.append(name)
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return found, names


# ---------------------------------------------------------------- 読み出し

def pick_id(sid, f, fl):
    """「これを正とする」で選んだファイルを表す文字列。

    ふつうは「保存場所:ファイル名」。同じ保存場所に同じ名前のファイルが複数あるとき（別の Season フォルダや、
    綴りの違う Station フォルダにある場合）は、「保存場所:Season フォルダ/Station フォルダ/ファイル名」にする。
    """
    if sum(1 for g in fl if g[0] == f[0]) > 1:
        return '%s:%s/%s/%s' % (sid, season_dir_of(f, ''), f[3], f[0])
    return '%s:%s' % (sid, f[0])


def picked_file(files, used, pick):
    """利用者が「これを正とする」で選んだファイル。(保存場所, ファイルの情報) か None。"""
    if not pick:
        return None
    sid, _, name = pick.partition(':')
    where = None
    if '/' in name:
        parts = name.split('/')
        if len(parts) == 3:
            where, name = (parts[0], parts[1]), parts[2]
    for s in used:
        if s['id'] == sid and s['type'] == 'folder':
            for f in files.get(sid) or []:
                if f[0] == name and (where is None or (season_dir_of(f, ''), f[3]) == where):
                    return s, f
    return None


def ref_file(files, used, ext_ref, pick=''):
    """CSV の File と拡張子の基準にするファイル。(保存場所, ファイルの情報) か None。

    利用者が選んだファイルがあればそれを、なければ設定の保存場所、なければ優先順で最初のもの。
    """
    hit = picked_file(files, used, pick)
    if hit is not None:
        return hit
    if ext_ref and ext_ref != 'auto' and files.get(ext_ref):
        for s in used:
            if s['id'] == ext_ref:
                return s, files[ext_ref][0]
    for s in used:
        if s['type'] == 'folder' and files.get(s['id']):
            return s, files[s['id']][0]
    return None


def csvext_rows(files, cj, used, ext_ref, pick=''):
    """拡張子が基準のファイルと違う CSV の行数（0 なら違いなし）。"""
    exts = csv_exts(cj)
    if not exts:
        return 0
    ref = ref_file(files, used, ext_ref, pick)
    if ref is None:
        return 0
    want = ext_of(ref[1][0]).lower()
    return sum(n for e, n in exts.items() if e != want)


def csvext_flag(files, cj, used, ext_ref, pick=''):
    return F_CSVEXT if csvext_rows(files, cj, used, ext_ref, pick) else 0


_DEC_CODE = {'': '', 'keep': 'k', 'delete': 'd', 'except': 'e'}


def load_payload(store, season):
    cfg = store.config()
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    con = store.connect(season)
    try:
        meta = get_meta(con)
        used = meta['sources']
        stations = []
        st_index = {}
        out = []
        for r in con.execute('SELECT id, station, stem, mask, flags, files, csv, decision, memo, pick'
                             ' FROM items ORDER BY ord'):
            files = json.loads(r[5])
            cj = json.loads(r[6]) if r[6] else None
            cells = []
            for s in used:
                if s['type'] == 'folder':
                    fl = files.get(s['id'])
                    cells.append([ext_of(fl[0][0]), fl[0][1], len(fl)] if fl else None)
                else:
                    cells.append([cj['ext'], cj['n']] if cj else None)
            si = st_index.get(r[1])
            if si is None:
                si = len(stations)
                st_index[r[1]] = si
                stations.append(r[1])
            out.append([r[0], si, r[2], r[3], r[4] | csvext_flag(files, cj, used, ext_ref, r[9] or ''),
                        _DEC_CODE.get(r[7] or '', ''), r[8] or '', cells])
        gone = con.execute('SELECT COUNT(*) FROM gone').fetchone()[0]
    finally:
        con.close()
    csv_meta = meta.get('csv')
    return {
        'season': meta.get('season', season), 'scanned_at': meta.get('scanned_at', ''),
        'sources': [{'id': s['id'], 'name': s['name'], 'type': s['type'], 'path': s['path'],
                     'readonly': s.get('readonly', False),
                     # この Season のフォルダ名（N250102_P01 と N250102_P02 のように複数のことがある）
                     'dirs': (meta.get('info', {}).get('season_dirs') or {}).get(s['id'], [])} for s in used],
        'ignore_ext': meta.get('ignore_ext', True), 'exts': meta.get('exts', []),
        # 走査したときに Season 名を先頭だけで照合したか（0.2.0 以前の走査結果は None）
        'season_prefix': meta.get('season_prefix'),
        'inherited_from': meta.get('inherited_from', []),
        'info': meta.get('info', {}),
        'csv': ({'path': csv_meta['path'], 'header': csv_meta['header'], 'rows': csv_meta['rows'],
                 'records': csv_meta['records'], 'blank': csv_meta['blank'],
                 'nontarget': csv_meta['nontarget'], 'dtfmt': csv_meta['dtfmt'],
                 'malformed': csv_meta.get('malformed', 0),
                 'has_dt': csv_meta['i_dt'] >= 0,
                 'fixed_cols': [csv_meta['i_season'], csv_meta['i_station'], csv_meta['i_file']],
                 # CSV に足す行の既定値（列番号 -> 値）。動画ごとに違う値は、動画の詳細のほうで返す
                 'defaults': {str(i): v for i, v in add_row_defaults(csv_meta, cfg['settings'], meta['season']).items()},
                 # CSV にしかない動画を「残す」とした行に付ける印
                 'mark': mark_info(csv_meta, cfg['settings'])}
                if csv_meta else None),
        'stations': stations, 'items': out, 'gone': gone,
    }


def _row_dict(con, item_id):
    r = con.execute('SELECT id, key, station, stem, mask, flags, files, csv, decision, memo, pick,'
                    ' csv_fields, dt_emb, dt_kind, dt_src, decided_at FROM items WHERE id=?',
                    (int(item_id),)).fetchone()
    if r is None:
        raise KeyError('動画が見つかりません（再走査で番号が変わった可能性があります）')
    return {
        'id': r[0], 'key': r[1], 'station': r[2], 'stem': r[3], 'mask': r[4], 'flags': r[5],
        'files': json.loads(r[6]), 'csv': json.loads(r[7]) if r[7] else None,
        'decision': r[8] or '', 'memo': r[9] or '', 'pick': r[10] or '',
        'csv_fields': json.loads(r[11]) if r[11] else {},
        'dt_emb': r[12], 'dt_kind': r[13], 'dt_src': r[14], 'decided_at': r[15] or '',
    }


def file_path(src, season, f):
    return os.path.join(src['path'], season_dir_of(f, season), f[3], f[0])


def resolve_file(store, season, item_id, src_id, n=0):
    """(パス, 保存場所) を返す。パスは登録された保存場所の下にあるものだけ。"""
    con = store.connect(season)
    try:
        meta = get_meta(con)
        it = _row_dict(con, item_id)
    finally:
        con.close()
    for s in meta['sources']:
        if s['id'] == src_id and s['type'] == 'folder':
            fl = it['files'].get(src_id) or []
            if 0 <= n < len(fl):
                return file_path(s, meta['season'], fl[n]), s
    raise KeyError('ファイルが見つかりません')


def ensure_embedded(con, meta, it):
    """埋め込みの撮影日時を読んで保存する。読んだあとの it を返す。"""
    it['dt_unreadable'] = False
    if it['dt_emb'] is not None:
        return it
    found = None
    unreadable = False
    for s in meta['sources']:
        if s['type'] != 'folder':
            continue
        for f in it['files'].get(s['id']) or []:
            try:
                dt, kind = tm_media.embedded_datetime(file_path(s, meta['season'], f))
            except OSError:
                unreadable = True       # 読めなかった（接続が切れているなど）。「日時なし」とは区別する
                continue
            if dt is not None:
                found = (dt.strftime('%Y-%m-%d %H:%M:%S'), kind, '%s: %s' % (s['name'], f[0]))
                break
        if found:
            break
    if found is None and unreadable:
        it['dt_unreadable'] = True      # 記録しない。次に読めたときに改めて調べる
        return it
    if found is None:
        found = ('NA', '', '')
    con.execute('UPDATE items SET dt_emb=?, dt_kind=?, dt_src=? WHERE id=?',
                (found[0], found[1], found[2], it['id']))
    it['dt_emb'], it['dt_kind'], it['dt_src'] = found
    return it


def embedded_text(it, tzmode, fmt):
    """埋め込まれた撮影日時を CSV の書式にした文字列。埋め込みがなければ 'NA'。"""
    if not it['dt_emb'] or it['dt_emb'] == 'NA':
        return 'NA'
    dt = datetime.strptime(it['dt_emb'], '%Y-%m-%d %H:%M:%S')
    return format_dt(tm_media.apply_tz(dt, it['dt_kind'], tzmode), fmt)


def local_time(ts):
    """ファイルの更新日時（PC の時刻帯）。扱えない値なら None。"""
    try:
        return datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def oldest_mtime(files, used):
    """動画の更新日時。(時刻, '保存場所: ファイル名', 保存場所で違うか) か None。

    保存場所のあいだで違うときは、最も古いものを使う。コピーや変換で更新日時が変わるときは
    新しくなるので、最も古いものが元の動画の日時である見込みが高いため。
    """
    best = None
    lo = hi = None
    for s in used:
        if s['type'] != 'folder':
            continue
        for f in files.get(s['id']) or []:
            ts = f[2]
            lo = ts if lo is None else min(lo, ts)
            hi = ts if hi is None else max(hi, ts)
            if best is None or ts < best[0]:
                best = (ts, '%s: %s' % (s['name'], f[0]))
    if best is None:
        return None
    return best[0], best[1], (hi - lo) > 2


def dt_profile(con, meta):
    """この Season の CSV の DateTime の性質を、走査の結果から調べる（動画は読まない）。

    - minute_only: 既存の DateTime が分単位か（秒がすべて 00）
    - mtime:       CSV に行がある動画で、ファイルの更新日時が CSV の DateTime と合う本数
    結果は走査ごとに 1 回だけ計算して覚えておく。
    """
    csv_meta = meta.get('csv')
    if not csv_meta or csv_meta.get('i_dt', -1) < 0:
        return None
    cached = meta.get('dt_profile')
    if cached and cached.get('scanned_at') == meta.get('scanned_at') and cached.get('v') == 1:
        return cached
    used = meta['sources']
    n = sec0 = unparsed = 0
    pairs = []
    disagree = 0
    for files_json, csv_json in con.execute("SELECT files, csv FROM items WHERE csv != ''"):
        cj = json.loads(csv_json)
        text = (cj.get('dt') or '').strip()
        if not text or text.upper() == 'NA':
            continue
        cdt = tm_media.parse_date_text(text)
        if cdt is None:
            unparsed += 1
            continue
        n += 1
        if cdt.second == 0:
            sec0 += 1
        m = oldest_mtime(json.loads(files_json), used)
        if m is not None:
            mdt = local_time(m[0])
            if mdt is not None:
                pairs.append((mdt, cdt))
                if m[2]:
                    disagree += 1
    # 秒が記録されていれば 00 になるのは 60 本に 1 本ほど。ほぼすべてが 00 なら分単位の列とみなす
    minute_only = csv_meta.get('dtfmt') in ('%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M', 'unpadded/nosec') \
        or (n >= 20 and sec0 >= 0.95 * n)
    match = near = differ = 0
    for mdt, cdt in pairs:
        if minute_only:
            mdt = mdt.replace(second=0)
        d = abs((mdt - cdt).total_seconds())
        if d <= (0 if minute_only else 2):
            match += 1
        elif d <= 120:
            near += 1
        else:
            differ += 1
    prof = {'v': 1, 'scanned_at': meta.get('scanned_at'), 'n_dt': n, 'sec0': sec0, 'unparsed': unparsed,
            'minute_only': bool(minute_only),
            'mtime': {'n': len(pairs), 'match': match, 'near': near, 'differ': differ, 'disagree': disagree}}
    set_meta(con, dt_profile=prof)
    con.commit()
    meta['dt_profile'] = prof
    return prof


def clean_add_defaults(raw, migrate=False):
    """設定の add_defaults を {列名: 値} に整える。値が空の列は「既定値なし」なので落とす。

    migrate は、0.2.0 以前に保存された設定を読むとき。そのころの組み込みの既定値だった
    Season_sub_folder={Season} を、{Folder} に読み替える。
    """
    out = {}
    if not isinstance(raw, dict):
        return dict(ADD_ROW_DEFAULTS)
    for k, v in raw.items():
        name = str(k).strip()
        val = '' if v is None else str(v).strip()
        if isinstance(v, bool):
            val = 'TRUE' if v else 'FALSE'
        if not name or not val or len(name) > 100 or len(val) > 500:
            continue
        if any(c in name + val for c in '\r\n\x00'):
            continue
        if any(name.casefold() == x.casefold() for x in out):
            continue
        if migrate and name.casefold() == 'season_sub_folder' and val.casefold() == '{season}':
            # フォルダ名が Season 名と同じなら結果は変わらず、N250102_P01 のようなフォルダのときは、その名前が入る
            val = '{Folder}'
        out[name] = val
        if len(out) >= MAX_ADD_DEFAULTS:
            break
    return out


FOLDER_LABEL = '（Season フォルダの名前）'      # 動画ごとに違う値を、一覧などで示すときの文


def season_folder_for(it, used, csv_meta, season, ext_ref='auto'):
    """その動画（Station）の Season フォルダの名前。既定値の {Folder} に入る。

    CSV に Season_sub_folder 列があり、同じ Station の行がすべて同じ値なら、その値を使う
    （Station のフォルダがどの Season フォルダにあるかを、CSV の記録に合わせる）。
    なければ、CSV に足す行のもとにするファイルが入っている Season フォルダの名前。
    """
    sub = (csv_meta or {}).get('station_sub') or {}
    v = sub.get(it['station'].casefold())
    if v:
        return v
    ref = ref_file(it['files'], used, ext_ref, it.get('pick') or '')
    if ref:
        return season_dir_of(ref[1], season)
    return season


def add_row_defaults(csv_meta, settings, season, folder=FOLDER_LABEL):
    """CSV に足す行の既定値を、列の位置ごとに返す（{列番号: 値}）。

    Season、Station、File、DateTime の列には入れない。CSV の見出しにない列名は無視する。
    同じ見出しが複数あるときは、最初の列に入れる。値の中の {Season} は Season 名に、
    {Folder} は folder（その動画の Season フォルダの名前。season_folder_for で求める）に置き換える。
    """
    header = csv_meta['header']
    cols = {h.strip().casefold(): i for i, h in reversed(list(enumerate(header)))}
    skip = {csv_meta['i_season'], csv_meta['i_station'], csv_meta['i_file'], csv_meta['i_dt']}
    out = {}
    for name, val in (settings.get('add_defaults') or {}).items():
        i = cols.get(str(name).strip().casefold())
        if i is not None and i not in skip and str(val) != '':
            out[i] = re.sub(r'\{folder\}', lambda m: folder, season_path(str(val), season), flags=re.I)
    return out


def mark_column(csv_meta, settings):
    """「csvのみ」の印を書く列の番号。印が無効か、候補の列が CSV になければ -1。

    候補（mark_cols）を順に見出しと照らし、最初に見つかった列を使う。
    Season、Station、File、DateTime の列には書かない。
    """
    if not csv_meta or not settings.get('mark_text'):
        return -1
    header = csv_meta['header']
    cols = {h.strip().casefold(): i for i, h in reversed(list(enumerate(header)))}
    skip = {csv_meta['i_season'], csv_meta['i_station'], csv_meta['i_file'], csv_meta['i_dt']}
    for name in settings.get('mark_cols') or []:
        i = cols.get(str(name).strip().casefold())
        if i is not None and i not in skip:
            return i
    return -1


def mark_info(csv_meta, settings):
    """画面に渡す、印の設定（書く列の名前、印の文字列、候補の列名）。"""
    i = mark_column(csv_meta, settings)
    return {'col': csv_meta['header'][i] if i >= 0 else '', 'text': settings.get('mark_text', ''),
            'cols': list(settings.get('mark_cols') or [])}


def fill_datetime(it, used, settings, fmt, minute_only):
    """CSV に足す行の DateTime。(文字列, 由来) を返す。

    由来: 'embedded'（埋め込まれた撮影日時）、'mtime'（ファイルの更新日時）、
          'na'（どちらも使えない）、'unreadable'（動画を読めなかった）
    既存の DateTime が分単位の CSV では、秒を 00 に切り捨てて既存の行にそろえる。
    """
    if it.get('dt_unreadable'):
        return 'NA', 'unreadable'
    dt = None
    origin = 'na'
    if it.get('dt_emb') and it['dt_emb'] != 'NA':
        dt = tm_media.apply_tz(datetime.strptime(it['dt_emb'], '%Y-%m-%d %H:%M:%S'),
                               it['dt_kind'], settings.get('embedded_tz', 'asis'))
        origin = 'embedded'
    elif settings.get('dt_source') == 'embedded_mtime':
        m = oldest_mtime(it['files'], used)
        dt = local_time(m[0]) if m is not None else None
        origin = 'mtime' if dt is not None else 'na'
    if dt is None:
        return 'NA', origin
    if minute_only:
        dt = dt.replace(second=0)
    return format_dt(dt, fmt), origin


def item_detail(store, season, item_id):
    cfg = store.config()
    con = store.connect(season)
    try:
        meta = get_meta(con)
        it = _row_dict(con, item_id)
        if any(it['files'].values()):
            ensure_embedded(con, meta, it)
            con.commit()
        prof = dt_profile(con, meta)
    finally:
        con.close()
    used = meta['sources']
    csv_meta = meta.get('csv')
    fmt = csv_meta['dtfmt'] if csv_meta else DEFAULT_DT_FORMAT
    minute_only = bool(prof and prof['minute_only'])
    has_file = any(it['files'].values())
    mt = oldest_mtime(it['files'], used)
    mt_dt = local_time(mt[0]) if mt is not None else None
    fill = fill_datetime(it, used, cfg['settings'], fmt, minute_only) if has_file else ('', '')
    srcs = []
    for s in used:
        if s['type'] == 'folder':
            fl = it['files'].get(s['id']) or []
            srcs.append({'id': s['id'], 'name': s['name'], 'type': 'folder', 'files': [
                {'n': i, 'name': f[0], 'size': f[1], 'mtime': f[2], 'ext': ext_of(f[0]),
                 'folder': season_dir_of(f, meta['season']), 'station_dir': f[3],
                 'pick': pick_id(s['id'], f, fl),
                 'path': file_path(s, meta['season'], f)} for i, f in enumerate(fl)]})
        else:
            srcs.append({'id': s['id'], 'name': s['name'], 'type': 'csv', 'csv': it['csv']})
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    ref = ref_file(it['files'], used, ext_ref, it['pick'])
    chosen = picked_file(it['files'], used, it['pick'])
    return {
        'id': it['id'], 'station': it['station'], 'stem': it['stem'], 'mask': it['mask'],
        'flags': it['flags'] | csvext_flag(it['files'], it['csv'], used, ext_ref, it['pick']),
        'decision': it['decision'], 'memo': it['memo'],
        # 選ばれているファイルの pick（画面のラジオボタンの値と同じ形にそろえる）
        'pick': (pick_id(chosen[0]['id'], chosen[1], it['files'].get(chosen[0]['id']) or [])
                 if chosen else it['pick']),
        'csv_fields': it['csv_fields'], 'decided_at': it['decided_at'], 'sources': srcs,
        'embedded': {
            'text': embedded_text(it, cfg['settings']['embedded_tz'], fmt) if any(it['files'].values()) else '',
            'src': it['dt_src'] or '', 'kind': it['dt_kind'] or '',
            'unreadable': bool(it.get('dt_unreadable')),
        },
        # ファイルの更新日時（保存場所で違うときは最も古いもの）
        'mtime': ({'text': mt_dt.strftime('%Y/%m/%d %H:%M:%S'), 'src': mt[1], 'differs': mt[2]}
                  if mt_dt is not None else None),
        # CSV に行を足すときに DateTime に入る値と、その由来
        'fill': {'text': fill[0], 'origin': fill[1], 'minute_only': minute_only,
                 'dt_source': cfg['settings'].get('dt_source', 'embedded')},
        'ref': {'src': ref[0]['id'], 'name': ref[1][0]} if ref else None,
        'header': csv_meta['header'] if csv_meta else [],
        # CSV に足す行の既定値（列番号 -> 値）。{Folder} は、この動画の Season フォルダの名前にしてある
        'defaults': ({str(i): v for i, v in add_row_defaults(
            csv_meta, cfg['settings'], meta['season'],
            season_folder_for(it, used, csv_meta, meta['season'], ext_ref)).items()} if csv_meta else {}),
    }


def set_decision(store, season, ids, decision):
    if decision not in DECISIONS:
        raise ValueError('判断の値が不正です')
    con = store.connect(season)
    try:
        t = now_text() if decision else ''
        con.execute('BEGIN')
        con.executemany('UPDATE items SET decision=?, decided_at=? WHERE id=?',
                        [(decision, t, int(i)) for i in ids])
        con.commit()
    finally:
        con.close()
    return len(ids)


def set_decisions_bulk(store, season, pairs):
    """[(id, decision), ...] をまとめて設定する（元に戻す操作用）。"""
    con = store.connect(season)
    try:
        t = now_text()
        con.execute('BEGIN')
        con.executemany('UPDATE items SET decision=?, decided_at=? WHERE id=?',
                        [(d, t if d else '', int(i)) for i, d in pairs if d in DECISIONS])
        con.commit()
    finally:
        con.close()
    return len(pairs)


def set_field(store, season, item_id, field, value):
    if field not in ('memo', 'pick', 'csv_fields'):
        raise ValueError('項目が不正です')
    if field == 'csv_fields':
        value = json.dumps({str(k): str(v) for k, v in (value or {}).items() if str(v) != ''},
                           ensure_ascii=False)
        if value == '{}':
            value = ''
    else:
        value = str(value or '')[:2000]
    con = store.connect(season)
    try:
        con.execute('UPDATE items SET %s=? WHERE id=?' % field, (value, int(item_id)))
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- 書き出し

def mtime_text(t):
    try:
        return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')
    except (OSError, OverflowError, ValueError):
        return ''       # Windows では 1970 年より前の日時を扱えない


def _state_text(flags):
    labels = [lab for bit, lab in STATE_LABELS if flags & bit]
    return '; '.join(labels) if labels else '一致'


def _iter_items(con):
    cur = con.execute('SELECT id FROM items ORDER BY ord')
    ids = [r[0] for r in cur]
    for i in ids:
        yield _row_dict(con, i)


def export_result(store, season):
    """照合結果の一覧（全動画の所在、状態、判断、メモ）を CSV に書き出す。"""
    cfg = store.config()
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    con = store.connect(season)
    try:
        meta = get_meta(con)
        used = meta['sources']
        path = os.path.join(store.export_dir(season), '照合結果_%s_%s.csv' % (safe_name(season), stamp()))
        header = ['Season', 'Station', 'File']
        for s in used:
            header += _source_columns(s)
        header += ['Pattern', 'Status', 'Decision', 'Memo', 'Pick', 'DecidedAt']
        n = 0
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header)
            for it in _iter_items(con):
                row = [meta['season'], it['station'], it['stem']]
                pat = []
                for b, s in enumerate(used):
                    present = bool(it['mask'] >> b & 1)
                    pat.append('%s:%s' % (s['name'], 'あり' if present else 'なし'))
                    row += _source_values(it, s, meta['season'])
                flags = it['flags'] | csvext_flag(it['files'], it['csv'], used, ext_ref, it['pick'])
                row += [' '.join(pat), _state_text(flags), DECISION_LABELS.get(it['decision'], ''),
                        it['memo'], it['pick'], it['decided_at']]
                w.writerow(row)
                n += 1
    finally:
        con.close()
    return {'path': path, 'rows': n}


# ---------------------------------------------------------------- 全 Season の書き出し

ALL_SCOPES = ('mismatch', 'all')
_ITEM_COLS = ('id, key, station, stem, mask, flags, files, csv, decision, memo, pick,'
              ' csv_fields, dt_emb, dt_kind, dt_src, decided_at')


def _source_columns(s):
    if s['type'] == 'folder':
        return [s['name'] + '_file', s['name'] + '_folder', s['name'] + '_size', s['name'] + '_mtime']
    return [s['name'] + '_file', s['name'] + '_rows', s['name'] + '_DateTime']


def _source_values(it, s, season):
    """照合結果の一覧の、保存場所ごとの列。_folder は、そのファイルが入っている Season フォルダの名前。"""
    if s['type'] == 'folder':
        fl = it['files'].get(s['id']) or []
        return [' | '.join(x[0] for x in fl), ' | '.join(season_dir_of(x, season) for x in fl),
                ' | '.join(str(x[1]) for x in fl), ' | '.join(mtime_text(x[2]) for x in fl)]
    cj = it['csv']
    return [cj['file'], cj['n'], cj['dt']] if cj else ['', '', '']


def season_counts(con):
    """1 つの Season の本数の集計（不一致、判断、状態ごと）。"""
    r = con.execute(
        "SELECT COUNT(*), SUM((flags & ?) != 0), SUM((flags & ?) != 0 AND decision = ''),"
        " SUM(decision = 'keep'), SUM(decision = 'delete'), SUM(decision = 'except'),"
        " SUM((flags & ?) != 0), SUM((flags & ?) != 0), SUM((flags & ?) != 0),"
        " SUM((flags & ?) != 0), SUM((flags & ?) != 0), SUM((flags & ?) != 0) FROM items",
        (F_NEEDS, F_NEEDS, F_MISSING, F_CSVONLY, F_SIZE, F_DUP, F_ZERO, F_EXTDIFF)).fetchone()
    keys = ('videos', 'mismatch', 'undecided', 'keep', 'delete', 'except',
            'missing', 'csv_only', 'size', 'dup', 'zero', 'extdiff')
    return {k: int(v or 0) for k, v in zip(keys, r)}


def all_seasons_summary(store):
    """走査済みの全 Season について、Season ごとの集計を返す。開けない Season は skipped に入れる。"""
    seasons, skipped = [], []
    for p in store.projects():
        try:
            con = store.connect(p['season'])
            try:
                meta = get_meta(con)
                c = season_counts(con)
            finally:
                con.close()
        except (sqlite3.Error, ValueError, KeyError, OSError) as e:
            skipped.append({'season': p['season'], 'error': '%s' % e})
            continue
        c.update(season=meta.get('season', p['season']), scanned_at=meta.get('scanned_at', ''),
                 sources=[{'name': x['name'], 'type': x['type'], 'path': x.get('path', '')}
                          for x in meta.get('sources', [])])
        seasons.append(c)
    totals = {k: sum(x[k] for x in seasons) for k in ('videos', 'mismatch', 'undecided', 'keep', 'delete', 'except')}
    return {'seasons': seasons, 'skipped': skipped, 'totals': totals}


def export_all(store, opts=None, job=None):
    """走査済みの全 Season の照合結果を 1 つの CSV にまとめ、Season ごとの集計も書き出す。

    opts['scope']: 'mismatch'（既定。不一致の動画と、判断またはメモのある動画）か 'all'（すべての動画）。
    保存場所の名前が Season によって違うときは、列を名前ごとに分け、その Season にない保存場所の列は空欄にする。
    """
    opts = opts or {}
    scope = opts.get('scope') if opts.get('scope') in ALL_SCOPES else 'mismatch'
    cfg = store.config()
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    summ = all_seasons_summary(store)
    if not summ['seasons']:
        raise ValueError('走査済みの Season がありません')
    # 保存場所の列: 全 Season の保存場所を、種類と名前でまとめる（出てきた順）
    col_of = {}
    src_header = []
    for c in summ['seasons']:
        for x in c['sources']:
            k = (x['type'], x['name'].casefold())
            if k not in col_of:
                col_of[k] = len(src_header)
                src_header += _source_columns(x)
    out_dir = store.export_dir('all_seasons')
    st = stamp()
    path = os.path.join(out_dir, '照合結果_全Season_%s.csv' % st)
    summary_path = os.path.join(out_dir, '集計_全Season_%s.csv' % st)
    header = ['Season', 'Station', 'File'] + src_header + ['Pattern', 'Status', 'Decision', 'Memo', 'Pick',
                                                           'DecidedAt', 'ScannedAt']
    where = '' if scope == 'all' else " WHERE (flags & %d) != 0 OR decision != '' OR memo != ''" % F_NEEDS
    n = 0
    tmp = path + '.part'
    try:
        with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header)
            for i, c in enumerate(summ['seasons']):
                if job:
                    job.update(phase='全 Season の書き出し（%s）' % c['season'], done=i, total=len(summ['seasons']))
                con = store.connect(c['season'])
                try:
                    meta = get_meta(con)
                    used = meta['sources']
                    rows = con.execute('SELECT ' + _ITEM_COLS + ' FROM items' + where + ' ORDER BY ord').fetchall()
                finally:
                    con.close()
                c['exported'] = len(rows)
                for r in rows:
                    it = {'station': r[2], 'stem': r[3], 'mask': r[4], 'flags': r[5], 'files': json.loads(r[6]),
                          'csv': json.loads(r[7]) if r[7] else None, 'decision': r[8] or '', 'memo': r[9] or '',
                          'pick': r[10] or '', 'decided_at': r[15] or ''}
                    cells = [''] * len(src_header)
                    pat = []
                    for b, x in enumerate(used):
                        pat.append('%s:%s' % (x['name'], 'あり' if it['mask'] >> b & 1 else 'なし'))
                        at = col_of[(x['type'], x['name'].casefold())]
                        vals = _source_values(it, x, meta['season'])
                        cells[at:at + len(vals)] = vals
                    flags = it['flags'] | csvext_flag(it['files'], it['csv'], used, ext_ref, it['pick'])
                    w.writerow([meta['season'], it['station'], it['stem']] + cells +
                               [' '.join(pat), _state_text(flags), DECISION_LABELS.get(it['decision'], ''),
                                it['memo'], it['pick'], it['decided_at'], c['scanned_at']])
                    n += 1
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    with open(summary_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['Season', 'ScannedAt', 'Videos', 'Mismatch', 'Undecided', 'Keep', 'Delete', 'Except',
                    'Missing', 'CsvOnly', 'SizeDiff', 'Duplicate', 'SizeZero', 'ExtDiff', 'Exported', 'Sources'])
        for c in summ['seasons']:
            w.writerow([c['season'], c['scanned_at'], c['videos'], c['mismatch'], c['undecided'], c['keep'],
                        c['delete'], c['except'], c['missing'], c['csv_only'], c['size'], c['dup'], c['zero'],
                        c['extdiff'], c['exported'],
                        ' | '.join('%s=%s' % (x['name'], x['path']) for x in c['sources'])])
    if job:
        job.update(done=len(summ['seasons']), total=len(summ['seasons']))
    return {'path': path, 'summary_path': summary_path, 'rows': n, 'seasons': len(summ['seasons']),
            'scope': scope, 'skipped': summ['skipped'], 'totals': summ['totals'],
            'empty': [c['season'] for c in summ['seasons'] if not c['videos']]}


def season_matcher(meta):
    """CSV の Season 列の値がこの Season のものかを決める関数。走査のときと同じ規則を使う。"""
    by_prefix = bool(meta.get('season_prefix', False))      # 0.2.0 以前の走査結果は、名前の全体で照合している
    cf = season_key(meta['season'], by_prefix).casefold()
    return lambda value: season_key(value, by_prefix).casefold() == cf


def _check_csv_unchanged(csv_meta):
    p = csv_meta['path']
    if not os.path.isfile(p):
        raise ValueError('CSV が見つかりません: %s' % p)
    st = os.stat(p)
    if st.st_size != csv_meta['size'] or int(st.st_mtime) != csv_meta['mtime']:
        raise ValueError('CSV が走査のあとに変更されています。再走査してから書き出してください。')


def csv_change_counts(store, season):
    """更新後の CSV で起こる変更の件数（書き出しの前の確認用）。"""
    cfg = store.config()
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    con = store.connect(season)
    try:
        meta = get_meta(con)
        if not meta.get('csv'):
            raise ValueError('CSV が保存場所に登録されていません')
        used = meta['sources']
        add = remove = remove_rows = ext = ext_rows = mark = mark_rows = 0
        for r in con.execute('SELECT files, csv, decision, pick FROM items'):
            files = json.loads(r[0])
            cj = json.loads(r[1]) if r[1] else None
            has_file = any(files.values())
            if r[2] == 'delete':
                if cj:
                    remove += 1
                    remove_rows += cj['n']
                continue
            n = csvext_rows(files, cj, used, ext_ref, r[3] or '') if cj else 0
            if n:
                ext += 1
                ext_rows += n
            if r[2] == 'keep' and not cj and has_file:
                add += 1
            if r[2] == 'keep' and cj and not has_file:
                mark += 1
                mark_rows += cj['n']
        prof = dt_profile(con, meta)
    finally:
        con.close()
    return {'add': add, 'remove': remove, 'remove_rows': remove_rows, 'ext': ext, 'ext_rows': ext_rows,
            # CSV にしかない動画を「残す」とした本数と行数、印の設定
            'mark': mark, 'mark_rows': mark_rows, 'mark_info': mark_info(meta['csv'], cfg['settings']),
            'path': meta['csv']['path'], 'has_dt': meta['csv']['i_dt'] >= 0, 'dtfmt': meta['csv']['dtfmt'],
            'dt_source': cfg['settings'].get('dt_source', 'embedded'),
            'dt_profile': ({'minute_only': prof['minute_only'], 'n_dt': prof['n_dt'], 'mtime': prof['mtime']}
                           if prof else None),
            # 追加する行に入る既定値（この CSV にある列だけ、見出しの順）
            'add_defaults': [[meta['csv']['header'][i], v] for i, v in
                             sorted(add_row_defaults(meta['csv'], cfg['settings'], meta['season']).items())]}


def export_csv(store, season, opts, job):
    """行の追加、行の削除、拡張子の更新を反映した CSV を、別名で書き出す。

    元の CSV は書き換えない。
    """
    cfg = store.config()
    settings = cfg['settings']
    ext_ref = settings.get('ext_ref', 'auto')
    do_add = bool(opts.get('add', True))
    do_remove = bool(opts.get('remove', True))
    do_ext = bool(opts.get('ext', True))
    do_mark = bool(opts.get('mark', True))
    con = store.connect(season)
    try:
        meta = get_meta(con)
        csv_meta = meta.get('csv')
        if not csv_meta:
            raise ValueError('CSV が保存場所に登録されていません')
        _check_csv_unchanged(csv_meta)
        used = meta['sources']
        exts = set(meta['exts'])
        ignore_ext = meta['ignore_ext']
        header = csv_meta['header']
        cols = {h.strip().casefold(): i for i, h in reversed(list(enumerate(header)))}
        fmt = csv_meta['dtfmt']
        remove_keys = set()
        ext_new = {}
        to_add = []
        i_mark = mark_column(csv_meta, settings) if do_mark else -1
        mark_keys = set()
        for it in _iter_items(con):
            cj = it['csv']
            has_file = any(it['files'].values())
            if i_mark >= 0 and it['decision'] == 'keep' and cj and not has_file:
                mark_keys.add(it['key'])
            if it['decision'] == 'delete':
                if cj and do_remove:
                    remove_keys.add(it['key'])
                continue
            if do_ext and cj and csvext_rows(it['files'], cj, used, ext_ref, it['pick']):
                ext_new[it['key']] = ext_of(ref_file(it['files'], used, ext_ref, it['pick'])[1][0])
            if do_add and it['decision'] == 'keep' and not cj and has_file:
                to_add.append(it)
        job.update(phase='撮影日時を読み取り中', done=0, total=len(to_add))
        add_rows = []
        n_na = n_unreadable = n_emb = n_mtime = 0
        prof = dt_profile(con, meta)
        minute_only = bool(prof and prof['minute_only'])
        station_names = csv_meta.get('station_names') or {}
        # Season 列は、CSV の既存の行と同じ綴りにする。行によって綴りが違うときは、同じ Station の行の綴り、
        # それも決まらなければ Season 名
        season_text = csv_meta.get('season_name') or meta['season']
        station_season = csv_meta.get('station_season') or {}
        for n, it in enumerate(to_add, 1):
            ensure_embedded(con, meta, it)
            if it.get('dt_unreadable'):
                n_unreadable += 1
            if n % 50 == 0:
                con.commit()
                job.update(done=n)
            fields = [''] * len(header)
            defaults = add_row_defaults(csv_meta, settings, meta['season'],
                                        season_folder_for(it, used, csv_meta, meta['season'], ext_ref))
            for i, val in defaults.items():
                fields[i] = val
            # 行ごとに入力された値は、既定値より優先する
            for name, val in (it['csv_fields'] or {}).items():
                i = cols.get(name.strip().casefold())
                if i is not None and i not in (csv_meta['i_season'], csv_meta['i_station'], csv_meta['i_file']):
                    fields[i] = val
            fields[csv_meta['i_season']] = station_season.get(it['station'].casefold()) or season_text
            # Station は、CSV にすでにある綴りがあればそれに合わせる
            fields[csv_meta['i_station']] = station_names.get(it['station'].casefold(), it['station'])
            fields[csv_meta['i_file']] = ref_file(it['files'], used, ext_ref, it['pick'])[1][0]
            if csv_meta['i_dt'] >= 0 and not fields[csv_meta['i_dt']]:
                text, origin = fill_datetime(it, used, settings, fmt, minute_only)
                if origin == 'embedded':
                    n_emb += 1
                elif origin == 'mtime':
                    n_mtime += 1
                else:
                    n_na += 1
                fields[csv_meta['i_dt']] = text
            add_rows.append(fields)
        con.commit()
    finally:
        con.close()

    def keyfunc(station, filefield):
        fname = basename_any(filefield.strip())
        if not station.strip() or not fname:
            return None, '', ''
        return make_key(station, fname, ignore_ext, exts)

    out_dir = store.export_dir(season)
    base = os.path.splitext(os.path.basename(csv_meta['path']))[0]
    ts = stamp()
    dst = os.path.join(out_dir, '%s_updated_%s.csv' % (base, ts))
    removed_path = os.path.join(out_dir, '%s_removed_%s.csv' % (base, ts))
    job.update(phase='CSV を書き出し中', done=0, total=100)

    def prog(pos, size):
        job.update(done=int(100.0 * pos / max(1, size)))

    try:
        res = tm_csv.write_updated(csv_meta['path'], dst, csv_meta, meta['season'], keyfunc,
                                   remove_keys, ext_new, add_rows, removed_path, prog,
                                   marks=(i_mark, settings['mark_text'], mark_keys) if mark_keys else None,
                                   match=season_matcher(meta))
    except UnicodeEncodeError as e:
        raise ValueError('追加する行に、CSV の文字コード（%s）で表せない文字があります: %r'
                         % (csv_meta['encoding'], e.object[e.start:e.end]))
    res.update({'path': dst, 'removed_path': removed_path if res['removed'] else '',
                'added_na': n_na, 'added_unreadable': n_unreadable, 'added_embedded': n_emb,
                'added_mtime': n_mtime, 'minute_only': minute_only, 'source': csv_meta['path'],
                'mark_col': header[i_mark] if i_mark >= 0 else '', 'mark_text': settings.get('mark_text', ''),
                'mark_items': len(mark_keys)})
    return res


def quarantine_root(src):
    """退避フォルダの場所。親フォルダと同じ階層の「<親フォルダ名>_removed」。

    親フォルダがドライブや共有フォルダの直下そのもの（E:\\ や \\\\NAS\\share）のときは、
    同じ階層に作れないので、その中の _removed とする。
    """
    p = src['path'].rstrip('\\/')
    drive, tail = ntpath.splitdrive(p)
    if not tail.strip('\\/'):
        return (p or src['path'].rstrip('\\/')) + os.sep + '_removed'
    return p + '_removed'


def export_plan(store, season):
    """判断から導かれる作業の一覧（確認用。このアプリは実行しない）。"""
    cfg = store.config()
    ext_ref = cfg['settings'].get('ext_ref', 'auto')
    con = store.connect(season)
    try:
        meta = get_meta(con)
        used = meta['sources']
        folders = [s for s in used if s['type'] == 'folder']
        ssn = meta['season']
        rows = []
        summary = {'copy': {}, 'quarantine': {}, 'csv_add': 0, 'csv_remove': 0, 'csv_ext': 0,
                   'csv_mark': 0, 'manual': 0, 'undecided': 0}
        i_mark = mark_column(meta.get('csv'), cfg['settings'])
        info = meta.get('info') or {}
        sdirs = info.get('season_dirs') or {}
        stdirs = info.get('station_dirs') or {}

        def copy_to(s, it, f):
            """コピー先のパスと、確かめてほしいことがあるときの注記。

            コピー先にその Station のフォルダがあれば、そこへ入れる。なければ、コピー先の Season フォルダ
            （1 つならそれ、複数ならコピー元と同じ名前のもの）の下に Station のフォルダを作る形にする。
            """
            known = (stdirs.get(s['id']) or {}).get(it['station'].casefold())
            if known:
                return os.path.join(s['path'], known[0], known[1], f[0]), ''
            dirs = sdirs.get(s['id']) or []
            from_dir = season_dir_of(f, ssn)
            if len(dirs) == 1:
                return os.path.join(s['path'], dirs[0], it['station'], f[0]), ''
            if not dirs:
                return os.path.join(s['path'], from_dir, it['station'], f[0]), ''
            same = [d for d in dirs if d.casefold() == from_dir.casefold()]
            if same:
                return os.path.join(s['path'], same[0], it['station'], f[0]), ''
            return (os.path.join(s['path'], dirs[0], it['station'], f[0]),
                    'コピー先に Season フォルダが複数あります（%s）。入れるフォルダを確かめてください' % '、'.join(dirs))

        for it in _iter_items(con):
            flags = it['flags']
            cj = it['csv']
            if flags & F_NEEDS and not it['decision']:
                summary['undecided'] += 1
            if cj and it['decision'] != 'delete' and csvext_rows(it['files'], cj, used, ext_ref, it['pick']):
                summary['csv_ext'] += 1
                rows.append(['CSV の拡張子を更新', it['station'], it['stem'], cj['file'],
                             ref_file(it['files'], used, ext_ref, it['pick'])[1][0], '', ''])
            if it['decision'] == 'keep':
                if not any(it['files'].values()):
                    if cj and i_mark >= 0:
                        # CSV にしかない動画を残す: 行はそのまま残し、印を付ける
                        rows.append(['CSV の行に印を付ける', it['station'], it['stem'], cj['file'], '', '',
                                     '%s 列に「%s」（%d 行）。動画はどの保存場所にもありません'
                                     % (meta['csv']['header'][i_mark], cfg['settings']['mark_text'], cj['n'])])
                        summary['csv_mark'] += 1
                    else:
                        rows.append(['手作業で確認', it['station'], it['stem'], '', '', '',
                                     '動画がどの保存場所にもありません'])
                        summary['manual'] += 1
                    continue
                pick = picked_file(it['files'], used, it['pick'])
                if flags & (F_SIZE | F_DUP) and pick is None:
                    rows.append(['手作業で確認', it['station'], it['stem'], '', '', '',
                                 '正とするファイルが選ばれていません'])
                    summary['manual'] += 1
                    continue
                src, f = pick if pick else ref_file(it['files'], used, 'auto')
                for s in folders:
                    fl = it['files'].get(s['id']) or []
                    note = '参照のみの保存場所（手作業）' if s.get('readonly') else ''
                    dst, check = copy_to(s, it, f)
                    cnote = '。'.join(x for x in (note, check) if x)
                    if not fl:
                        rows.append(['コピー', it['station'], it['stem'], file_path(src, ssn, f), dst, f[1], cnote])
                        c = summary['copy'].setdefault(s['name'], [0, 0])
                        c[0] += 1
                        c[1] += f[1]
                    elif pick is not None:
                        for g in fl:
                            if s['id'] == src['id'] and g is f:
                                continue
                            same = ext_of(g[0]).lower() == ext_of(f[0]).lower()
                            if (same and g[1] != f[1]) or (s['id'] == src['id']):
                                q = os.path.join(quarantine_root(s), season_dir_of(g, ssn), g[3], g[0])
                                rows.append(['退避', it['station'], it['stem'], file_path(s, ssn, g), q, g[1], note])
                                c = summary['quarantine'].setdefault(s['name'], [0, 0])
                                c[0] += 1
                                c[1] += g[1]
                                if same and s['id'] != src['id']:
                                    # 置き換え: 退避したファイルと同じフォルダへ入れる
                                    rdst = os.path.join(s['path'], season_dir_of(g, ssn), g[3], f[0])
                                    rows.append(['コピー', it['station'], it['stem'], file_path(src, ssn, f), rdst, f[1], note])
                                    c = summary['copy'].setdefault(s['name'], [0, 0])
                                    c[0] += 1
                                    c[1] += f[1]
                if not cj and any(s['type'] == 'csv' for s in used):
                    rows.append(['CSV に行を追加', it['station'], it['stem'], '',
                                 ref_file(it['files'], used, ext_ref, it['pick'])[1][0], '', ''])
                    summary['csv_add'] += 1
            elif it['decision'] == 'delete':
                for s in folders:
                    note = '参照のみの保存場所（手作業）' if s.get('readonly') else ''
                    for g in it['files'].get(s['id']) or []:
                        q = os.path.join(quarantine_root(s), season_dir_of(g, ssn), g[3], g[0])
                        rows.append(['退避', it['station'], it['stem'], file_path(s, ssn, g), q, g[1], note])
                        c = summary['quarantine'].setdefault(s['name'], [0, 0])
                        c[0] += 1
                        c[1] += g[1]
                if cj:
                    rows.append(['CSV の行を削除', it['station'], it['stem'], cj['file'], '', '', '%d 行' % cj['n']])
                    summary['csv_remove'] += 1
        path = os.path.join(store.export_dir(season), '実行計画_%s_%s.csv' % (safe_name(season), stamp()))
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['Action', 'Station', 'File', 'From', 'To', 'Size', 'Note'])
            w.writerows(rows)
    finally:
        con.close()
    return {'path': path, 'rows': len(rows), 'summary': summary}


def dt_check(store, season, job, limit=300):
    """CSV の既存の DateTime と、動画に埋め込まれた撮影日時を突き合わせる。"""
    cfg = store.config()
    tzmode = cfg['settings']['embedded_tz']
    con = store.connect(season)
    try:
        meta = get_meta(con)
        if not meta.get('csv') or meta['csv']['i_dt'] < 0:
            raise ValueError('CSV に DateTime 列がありません')
        cand = []
        for r in con.execute('SELECT id, files, csv FROM items ORDER BY ord'):
            if not r[2]:
                continue
            cj = json.loads(r[2])
            if cj.get('dt') and cj['dt'].strip().upper() != 'NA' and r[1] != '{}':
                cand.append(r[0])
        step = max(1, len(cand) // limit)
        sample = cand[::step][:limit]
        job.update(phase='撮影日時を突き合わせ中', done=0, total=len(sample))
        res = {'sampled': len(sample), 'candidates': len(cand), 'no_embedded': 0, 'unparsed': 0,
               'unreadable': 0,
               'match': 0, 'within_minute': 0, 'differ': 0, 'offsets': {}, 'kinds': {}, 'tzmode': tzmode,
               'examples': []}
        for n, item_id in enumerate(sample, 1):
            it = _row_dict(con, item_id)
            ensure_embedded(con, meta, it)
            if n % 20 == 0:
                con.commit()
                job.update(done=n)
            if it.get('dt_unreadable') or it['dt_emb'] is None:
                res['unreadable'] += 1
                continue
            if it['dt_emb'] == 'NA':
                res['no_embedded'] += 1
                continue
            csv_dt = tm_media.parse_date_text(it['csv']['dt'])
            if csv_dt is None:
                res['unparsed'] += 1
                continue
            emb = tm_media.apply_tz(datetime.strptime(it['dt_emb'], '%Y-%m-%d %H:%M:%S'), it['dt_kind'], tzmode)
            res['kinds'][it['dt_kind']] = res['kinds'].get(it['dt_kind'], 0) + 1
            diff = (emb - csv_dt).total_seconds()
            if abs(diff) < 1:
                res['match'] += 1
            elif abs(diff) < 60:
                res['within_minute'] += 1
            else:
                res['differ'] += 1
                if len(res['examples']) < 5:
                    res['examples'].append({'station': it['station'], 'file': it['stem'],
                                            'csv': it['csv']['dt'], 'embedded': emb.strftime('%Y-%m-%d %H:%M:%S')})
            mins = int(round(diff / 60.0))
            res['offsets'][mins] = res['offsets'].get(mins, 0) + 1
        con.commit()
    finally:
        con.close()
    res['offsets'] = sorted(([k, v] for k, v in res['offsets'].items()), key=lambda kv: -kv[1])[:6]
    return res
