# -*- coding: utf-8 -*-
"""動画ファイルの読み取り。

AVI の索引（コマの位置）、MP4 / MOV と AVI に埋め込まれた撮影日時、
ハフマン表のない MJPEG のコマの補正を扱う。標準ライブラリだけで動く。
"""
import os
import re
import shutil
import struct
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- JPEG

_DC_LUM_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
_DC_CHR_BITS = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
_DC_VALS = list(range(12))
_AC_LUM_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
_AC_LUM_VALS = [
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06,
    0x13, 0x51, 0x61, 0x07, 0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0, 0x24, 0x33, 0x62, 0x72,
    0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45,
    0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74, 0x75,
    0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3,
    0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9,
    0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
]
_AC_CHR_BITS = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
_AC_CHR_VALS = [
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41,
    0x51, 0x07, 0x61, 0x71, 0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0, 0x15, 0x62, 0x72, 0xD1,
    0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44,
    0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x73, 0x74,
    0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A,
    0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7,
    0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4,
    0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0xFA,
]


def _build_std_dht():
    body = b''
    for tc_th, bits, vals in (
        (0x00, _DC_LUM_BITS, _DC_VALS),
        (0x10, _AC_LUM_BITS, _AC_LUM_VALS),
        (0x01, _DC_CHR_BITS, _DC_VALS),
        (0x11, _AC_CHR_BITS, _AC_CHR_VALS),
    ):
        if sum(bits) != len(vals):
            raise AssertionError('ハフマン表の定義が不正です')
        body += bytes([tc_th]) + bytes(bits) + bytes(vals)
    return b'\xff\xc4' + struct.pack('>H', len(body) + 2) + body


STD_DHT = _build_std_dht()


def fix_jpeg(data):
    """AVI の MJPEG のコマを、ブラウザが表示できる JPEG にする。

    AVI の MJPEG はハフマン表（DHT）を省くことがある。その場合は標準の表を
    走査開始（SOS）の直前に足す。すでに表があるコマはそのまま返す。
    """
    if data[:2] != b'\xff\xd8':
        i = data.find(b'\xff\xd8')
        if i < 0:
            return data
        data = data[i:]
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            return data
        m = data[pos + 1]
        if m == 0xFF:
            pos += 1
            continue
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            pos += 2
            continue
        if m == 0xC4 or m == 0xD9:
            return data
        if m == 0xDA:
            return data[:pos] + STD_DHT + data[pos:]
        seglen = struct.unpack('>H', data[pos + 2:pos + 4])[0]
        if seglen < 2:
            return data
        pos += 2 + seglen
    return data


# ---------------------------------------------------------------- 日付の文字列

_MONTHS = {m: i + 1 for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'])}
_RE_YMD = re.compile(
    r'(\d{4})[-:/.](\d{1,2})[-:/.](\d{1,2})[ T/]+(\d{1,2}):(\d{2})(?::(\d{2}))?')
_RE_CTIME = re.compile(
    r'([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})')


def parse_date_text(s):
    """日時の文字列を datetime にする。読めなければ None。"""
    if not s:
        return None
    s = s.replace('\x00', ' ').strip()
    m = _RE_YMD.search(s)
    try:
        if m:
            y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
            sec = int(m.group(6)) if m.group(6) else 0
            return datetime(y, mo, d, h, mi, sec)
        m = _RE_CTIME.search(s)
        if m:
            mo = _MONTHS.get(m.group(1).lower())
            if mo:
                return datetime(int(m.group(6)), mo, int(m.group(2)),
                                int(m.group(3)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------- AVI

def _chunks(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size = struct.unpack('<I', hdr[4:8])[0]
        yield hdr[0:4], pos + 8, size
        pos += 8 + size + (size & 1)


def _parse_strl(f, start, end, streams):
    st = {'type': b'', 'handler': b'', 'scale': 0, 'rate': 0, 'length': 0, 'comp': b''}
    for cid, data, size in _chunks(f, start, end):
        if cid == b'strh':
            f.seek(data)
            d = f.read(min(size, 56))
            if len(d) >= 36:
                st['type'] = d[0:4]
                st['handler'] = d[4:8]
                st['scale'], st['rate'] = struct.unpack_from('<II', d, 20)
                st['length'] = struct.unpack_from('<I', d, 32)[0]
        elif cid == b'strf':
            f.seek(data)
            d = f.read(min(size, 40))
            if st['type'] == b'vids' and len(d) >= 20:
                st['width'], st['height'] = struct.unpack_from('<ii', d, 4)
                st['comp'] = d[16:20]
    streams.append(st)


def _parse_hdrl(f, start, end, info, streams):
    for cid, data, size in _chunks(f, start, end):
        if cid == b'avih':
            f.seek(data)
            d = f.read(min(size, 56))
            if len(d) >= 40:
                info['usec'] = struct.unpack_from('<I', d, 0)[0]
                info['total'] = struct.unpack_from('<I', d, 16)[0]
                info['width'], info['height'] = struct.unpack_from('<II', d, 32)
        elif cid == b'LIST':
            f.seek(data)
            if f.read(4) == b'strl':
                _parse_strl(f, data + 4, data + size, streams)
        elif cid == b'IDIT':
            f.seek(data)
            info['idit'] = f.read(min(size, 128)).decode('latin-1', 'replace')


def _parse_info_list(f, start, end, info):
    for cid, data, size in _chunks(f, start, end):
        if cid == b'ICRD':
            f.seek(data)
            info['icrd'] = f.read(min(size, 128)).decode('latin-1', 'replace')


def _frames_from_idx1(f, idx1, movi, tags, fsize):
    f.seek(idx1[0])
    raw = f.read(idx1[1])
    entries = []
    for i in range(len(raw) // 16):
        ckid, _flags, off, ln = struct.unpack_from('<4sIII', raw, i * 16)
        if ckid in tags:
            entries.append((off, ln))
    probe = next((e for e in entries if e[1] > 0), None)
    if probe is None:
        return None
    base = None
    for cand in (movi[0], 0):
        f.seek(cand + probe[0])
        h = f.read(8)
        if len(h) == 8 and h[0:4] in tags and struct.unpack('<I', h[4:8])[0] == probe[1]:
            base = cand
            break
    if base is None:
        return None
    return [(base + off + 8, ln) for off, ln in entries if base + off + 8 + ln <= fsize]


def _frames_from_movi(f, start, end, tags, out, depth=0):
    for cid, data, size in _chunks(f, start, end):
        if cid == b'LIST' and depth < 2:
            _frames_from_movi(f, data + 4, min(data + size, end), tags, out, depth + 1)
        elif cid in tags and data + size <= end:
            out.append((data, size))


def parse_avi(path, want_frames=True):
    """AVI のヘッダーと、映像のコマの位置の一覧を読む。"""
    with open(path, 'rb') as f:
        fsize = os.fstat(f.fileno()).st_size
        head = f.read(12)
        if len(head) < 12 or head[0:4] != b'RIFF' or head[8:12] != b'AVI ':
            raise ValueError('AVI ファイルではありません')
        riff_end = 8 + struct.unpack('<I', head[4:8])[0]
        if riff_end < 12 or riff_end > fsize:
            riff_end = fsize
        info = {'usec': 0, 'total': 0, 'width': 0, 'height': 0, 'idit': None, 'icrd': None}
        streams = []
        movi = None
        idx1 = None
        for cid, data, size in _chunks(f, 12, riff_end):
            if cid == b'LIST':
                f.seek(data)
                ltype = f.read(4)
                lend = min(data + size, fsize)
                if ltype == b'hdrl':
                    _parse_hdrl(f, data + 4, lend, info, streams)
                elif ltype == b'movi':
                    movi = (data, lend)
                elif ltype == b'INFO':
                    _parse_info_list(f, data + 4, lend, info)
            elif cid == b'idx1':
                idx1 = (data, min(size, max(0, fsize - data)))
            elif cid == b'IDIT':
                f.seek(data)
                info['idit'] = f.read(min(size, 128)).decode('latin-1', 'replace')
        vid = next((i for i, s in enumerate(streams) if s['type'] == b'vids'), None)
        if vid is None:
            raise ValueError('映像のストリームがありません')
        vs = streams[vid]
        fps = 0.0
        if vs['scale'] and vs['rate']:
            fps = vs['rate'] / float(vs['scale'])
        elif info['usec']:
            fps = 1e6 / info['usec']
        if not (0.5 <= fps <= 240):
            fps = 30.0
        codec = (vs['comp'] or vs['handler']).decode('latin-1', 'replace').strip('\x00 ')
        out = {
            'fps': fps,
            'width': abs(vs.get('width') or info['width']),
            'height': abs(vs.get('height') or info['height']),
            'codec': codec,
            'mjpeg': codec.lower() in ('mjpg', 'jpeg', 'avrn', 'ljpg', 'jpgl', 'dmb1'),
            'idit': info['idit'],
            'icrd': info['icrd'],
            'frames': None,
        }
        if want_frames and movi is not None:
            tags = (b'%02ddc' % vid, b'%02ddb' % vid)
            frames = None
            if idx1 is not None and idx1[1] >= 16:
                frames = _frames_from_idx1(f, idx1, movi, tags, fsize)
            if not frames:
                frames = []
                _frames_from_movi(f, movi[0] + 4, movi[1], tags, frames)
            # 長さ 0 のコマ（落ちたコマ）は直前のコマで置き換える
            fixed = []
            last = None
            for fr in frames:
                if fr[1] > 0:
                    last = fr
                if last is not None:
                    fixed.append(last)
            out['frames'] = fixed
            if fixed and not out['mjpeg']:
                f.seek(fixed[0][0])
                if f.read(2) == b'\xff\xd8':
                    out['mjpeg'] = True
        return out


_avi_cache = OrderedDict()
_avi_lock = threading.Lock()


def avi_info_cached(path):
    st = os.stat(path)
    key = (path, st.st_size, int(st.st_mtime))
    with _avi_lock:
        hit = _avi_cache.get(key)
        if hit is not None:
            _avi_cache.move_to_end(key)
            return hit
    info = parse_avi(path)
    with _avi_lock:
        _avi_cache[key] = info
        while len(_avi_cache) > 12:
            _avi_cache.popitem(last=False)
    return info


def read_avi_frame(path, info, i):
    frames = info['frames'] or []
    if not frames:
        raise ValueError('コマがありません')
    i = max(0, min(len(frames) - 1, int(i)))
    off, ln = frames[i]
    with open(path, 'rb') as f:
        f.seek(off)
        data = f.read(ln)
    return fix_jpeg(data)


# ---------------------------------------------------------------- MP4 / MOV

_EPOCH_1904 = datetime(1904, 1, 1)


def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack('>I4s', hdr[0:8])
        hlen = 8
        if size == 1:
            if len(hdr) < 16:
                return
            size = struct.unpack('>Q', hdr[8:16])[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def mp4_creation_time(path):
    """MP4 / MOV の mvhd に記録された作成日時（記録されたままの値）。なければ None。"""
    with open(path, 'rb') as f:
        fsize = os.fstat(f.fileno()).st_size
        for typ, body, end in _boxes(f, 0, fsize):
            if typ != b'moov':
                continue
            for ctyp, cbody, _cend in _boxes(f, body, min(end, fsize)):
                if ctyp != b'mvhd':
                    continue
                f.seek(cbody)
                d = f.read(12)
                if len(d) < 8:
                    return None
                if d[0] == 1 and len(d) >= 12:
                    ct = struct.unpack('>Q', d[4:12])[0]
                else:
                    ct = struct.unpack('>I', d[4:8])[0]
                if ct == 0:
                    return None
                try:
                    dt = _EPOCH_1904 + timedelta(seconds=ct)
                except OverflowError:
                    return None
                if 1990 <= dt.year <= 2100:
                    return dt
                return None
            return None
    return None


def embedded_datetime(path):
    """動画に埋め込まれた撮影日時。(datetime, 種類) を返す。なければ (None, '')。

    種類は 'mp4'（mvhd の作成日時。規格上は UTC）か 'avi'（IDIT / ICRD の文字列）。
    ファイルを読めなかったとき（接続が切れているなど）は OSError をそのまま上げる。
    「日時が入っていない」と「読めなかった」を取り違えないため。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.avi':
            info = parse_avi(path, want_frames=False)
            dt = parse_date_text(info.get('idit')) or parse_date_text(info.get('icrd'))
            return (dt, 'avi') if dt else (None, '')
        if ext in ('.mp4', '.mov', '.m4v'):
            dt = mp4_creation_time(path)
            return (dt, 'mp4') if dt else (None, '')
    except (ValueError, struct.error):
        return (None, '')      # 中身が動画として読めない: 日時なしとして扱う
    return (None, '')


def apply_tz(dt, kind, tzmode):
    """埋め込み日時の解釈。'utc' のとき、MP4 の値を UTC とみなして PC の時刻帯に直す。"""
    if dt is None:
        return None
    if kind == 'mp4' and tzmode == 'utc':
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- ffmpeg

def find_ffmpeg(configured=''):
    """ffmpeg の場所。設定、PATH、imageio-ffmpeg の順に探す。なければ ''。"""
    if configured and os.path.isfile(configured):
        return configured
    hit = shutil.which('ffmpeg')
    if hit:
        return hit
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return ''
