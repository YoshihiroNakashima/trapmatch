# -*- coding: utf-8 -*-
"""試験用の保存場所（HDD、NAS、CSV）を作る。"""
import io
import os
import struct
import subprocess

from PIL import Image, ImageDraw

SEASON = 'N260101'


def jpeg_frame(i, w=160, h=120):
    im = Image.new('RGB', (w, h), (30 + (i * 7) % 200, 90, 140))
    d = ImageDraw.Draw(im)
    d.rectangle([10 + i * 3 % (w - 40), 30, 40 + i * 3 % (w - 40), 60], fill=(250, 220, 60))
    d.text((6, 6), 'frame %d' % i, fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=80)
    return buf.getvalue()


def strip_dht(jpeg):
    """JPEG からハフマン表（DHT）を取り除く（AVI の MJPEG によくある形）。"""
    out = bytearray(jpeg[:2])
    pos = 2
    while pos < len(jpeg):
        if jpeg[pos] != 0xFF:
            raise ValueError('bad jpeg')
        m = jpeg[pos + 1]
        if m == 0xDA:
            out += jpeg[pos:]
            break
        ln = struct.unpack('>H', jpeg[pos + 2:pos + 4])[0]
        if m != 0xC4:
            out += jpeg[pos:pos + 2 + ln]
        pos += 2 + ln
    return bytes(out)


def _chunk(cid, data):
    return cid + struct.pack('<I', len(data)) + data + (b'\x00' if len(data) & 1 else b'')


def _list(ltype, payload):
    return b'LIST' + struct.pack('<I', len(payload) + 4) + ltype + payload


def synth_avi(path, frames, fps=15, width=160, height=120, idit=None, absolute_idx=False,
              with_index=True):
    """JPEG のコマから最小の MJPEG AVI を作る。"""
    n = len(frames)
    avih = struct.pack('<14I', int(1e6 / fps), 0, 0, 0x10, n, 0, 1, 0, width, height, 0, 0, 0, 0)
    strh = struct.pack('<4s4sIHHIIIIIIII4h', b'vids', b'MJPG', 0, 0, 0, 0, 1, fps, 0, n, 0,
                       0xFFFFFFFF, 0, 0, 0, width, height)
    strf = struct.pack('<IiiHH4sIiiII', 40, width, height, 1, 24, b'MJPG', width * height * 3, 0, 0, 0, 0)
    hdrl_payload = _chunk(b'avih', avih) + _list(b'strl', _chunk(b'strh', strh) + _chunk(b'strf', strf))
    if idit:
        hdrl_payload += _chunk(b'IDIT', idit.encode('ascii') + b'\x00')
    hdrl = _list(b'hdrl', hdrl_payload)
    movi_tag_pos = 12 + len(hdrl) + 8
    payload = b''
    entries = b''
    for fr in frames:
        off = 4 + len(payload)
        if absolute_idx:
            off = movi_tag_pos + off
        entries += struct.pack('<4sIII', b'00dc', 0x10, off, len(fr))
        payload += _chunk(b'00dc', fr)
    body = hdrl + _list(b'movi', payload)
    if with_index:
        body += _chunk(b'idx1', entries)
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body) + 4) + b'AVI ' + body)


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y'] + list(args), check=True)


def put(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def build(base, real_videos=True):
    """試験用の保存場所を base の下に作る。場所の一覧を返す。"""
    os.makedirs(base, exist_ok=True)
    hdd = os.path.join(base, 'hdd', 'PRM_N')
    nas = os.path.join(base, 'nas', 'PRM_N')
    H = lambda st, name: os.path.join(hdd, SEASON, st, name)   # noqa: E731
    N = lambda st, name: os.path.join(nas, SEASON, st, name)   # noqa: E731
    frames = [jpeg_frame(i) for i in range(30)]
    avi_plain = os.path.join(base, '_plain.avi')
    synth_avi(avi_plain, [strip_dht(f) for f in frames], idit='Mon Jan 05 06:07:08 2026')
    avi_bytes = open(avi_plain, 'rb').read()
    mp4_bytes = b'm' * 1000
    mp4_dated = mp4_bytes
    webm_bytes = b'w' * 500
    if real_videos:
        p = os.path.join(base, '_dated.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=15', '-t', '2', '-c:v', 'libx264',
               '-pix_fmt', 'yuv420p', '-metadata', 'creation_time=2026-01-02T03:04:05Z', p)
        mp4_dated = open(p, 'rb').read()
        p = os.path.join(base, '_nodate.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=15', '-t', '2', '-c:v', 'libx264',
               '-pix_fmt', 'yuv420p', p)
        mp4_bytes = open(p, 'rb').read()
        p = os.path.join(base, '_clip.webm')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=15', '-t', '2', '-c:v', 'libvpx', '-b:v', '200k', p)
        webm_bytes = open(p, 'rb').read()

    # BSN0001: 一致 5 本
    for i in range(1, 6):
        put(H('BSN0001', 'IMG_%04d.AVI' % i), avi_bytes)
        put(N('BSN0001', 'IMG_%04d.AVI' % i), avi_bytes)
    # 0006: 拡張子違い（HDD は AVI、NAS は MP4、CSV は .AVI）
    put(H('BSN0001', 'IMG_0006.AVI'), avi_bytes)
    put(N('BSN0001', 'IMG_0006.MP4'), mp4_bytes)
    # 0007: NAS に欠け
    put(H('BSN0001', 'IMG_0007.AVI'), avi_bytes)
    # 0008: NAS だけ（HDD と CSV に欠け）。埋め込み日時あり
    put(N('BSN0001', 'IMG_0008.MP4'), mp4_dated)
    # 0009: CSV に欠け（HDD と NAS にあり）。埋め込み日時なし
    put(H('BSN0001', 'IMG_0009.MP4'), mp4_bytes)
    put(N('BSN0001', 'IMG_0009.MP4'), mp4_bytes)
    # 0010: CSV のみ
    # 0011: サイズ違い
    put(H('BSN0001', 'IMG_0011.AVI'), avi_bytes)
    put(N('BSN0001', 'IMG_0011.AVI'), avi_bytes[:len(avi_bytes) // 2])
    # 0012: 場所内の重複（HDD に AVI と MP4）
    put(H('BSN0001', 'IMG_0012.AVI'), avi_bytes)
    put(H('BSN0001', 'IMG_0012.MP4'), mp4_bytes)
    put(N('BSN0001', 'IMG_0012.MP4'), mp4_bytes)
    # 0013: 両方 MP4、CSV は .AVI（CSV の拡張子違い）
    put(H('BSN0001', 'IMG_0013.MP4'), mp4_bytes)
    put(N('BSN0001', 'IMG_0013.MP4'), mp4_bytes)
    # 0014: CSV に欠け、AVI（IDIT の日時あり）
    put(H('BSN0001', 'IMG_0014.AVI'), avi_bytes)
    put(N('BSN0001', 'IMG_0014.AVI'), avi_bytes)
    # 0015: webm（ブラウザの <video> の試験用）
    put(H('BSN0001', 'IMG_0015.webm'), webm_bytes)
    put(N('BSN0001', 'IMG_0015.webm'), webm_bytes)
    # BSN0002: HDD にだけある Station
    for i in range(1, 4):
        put(H('BSN0002', 'IMG_%04d.AVI' % i), avi_bytes)
    # bsn0003: 大文字小文字の違い
    put(H('BSN0003', 'IMG_0001.AVI'), avi_bytes)
    put(N('bsn0003', 'img_0001.avi'), avi_bytes)
    # 対象外のファイル、想定外の階層
    put(H('BSN0001', 'Thumbs.db'))
    put(H('BSN0001', 'notes.txt'))
    put(os.path.join(hdd, SEASON, 'stray.AVI'), avi_bytes)
    put(H('BSN0001', os.path.join('sub', 'deep.AVI')), avi_bytes)
    # ほかの Season
    put(os.path.join(hdd, 'N250101', 'BSN0001', 'IMG_0001.AVI'), avi_bytes)

    csv_path = os.path.join(base, 'list.csv')
    lines = ['Season,Station,File,DateTime,Species,Note']
    for i in range(1, 6):
        lines.append('%s,BSN0001,IMG_%04d.AVI,2026/01/05 06:07:08,deer,' % (SEASON, i))
    lines.append('%s,BSN0001,IMG_0001.AVI,2026/01/05 06:07:08,boar,"two species, same video"' % SEASON)
    lines.append('%s,BSN0001,IMG_0006.AVI,2026/01/05 06:10:00,deer,' % SEASON)
    lines.append('%s,BSN0001,IMG_0007.AVI,2026/01/05 06:11:00,deer,' % SEASON)
    lines.append('%s,BSN0001,IMG_0010.AVI,2026/01/05 06:12:00,empty,"line one\nline two"' % SEASON)
    lines.append('%s,BSN0001,IMG_0011.AVI,2026/01/05 06:13:00,deer,' % SEASON)
    lines.append('%s,BSN0001,IMG_0012.AVI,2026/01/05 06:14:00,deer,' % SEASON)
    lines.append('%s,BSN0001,IMG_0013.AVI,2026/01/05 06:15:00,deer,' % SEASON)
    lines.append('%s,BSN0001,IMG_0015.webm,2026/01/05 06:16:00,deer,' % SEASON)
    for i in range(1, 4):
        lines.append('%s,BSN0002,IMG_%04d.AVI,2026/01/06 01:00:00,macaque,' % (SEASON, i))
    lines.append('%s,BSN0003,IMG_0001.AVI,2026/01/07 01:00:00,hare,' % SEASON)
    lines.append('%s,BSN0001,photo.JPG,,,' % SEASON)
    lines.append('%s,,IMG_9999.AVI,,,' % SEASON)
    lines.append('N250101,BSN0001,IMG_0001.AVI,2025/01/05 06:07:08,deer,"other season"')
    lines.append('N250101,BSN0001,IMG_0002.AVI,2025/01/05 06:07:08,deer,' + SEASON + ' in a note')
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        f.write('\r\n'.join(lines) + '\r\n')
    return {'hdd': hdd, 'nas': nas, 'csv': csv_path, 'season': SEASON, 'avi': avi_plain,
            'frames': frames}
