# -*- coding: utf-8 -*-
import csv
import io
import json
import os
import struct
import sys
from datetime import datetime

import pytest
from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402
import tm_core  # noqa: E402
import tm_csv  # noqa: E402
import tm_media  # noqa: E402

F = tm_core


@pytest.fixture(scope='module')
def tree(tmp_path_factory):
    base = str(tmp_path_factory.mktemp('tree'))
    return fixture.build(base)


def make_store(tmp_path, tree, **settings):
    store = tm_core.Store(str(tmp_path / 'data'))
    st = {'exts': ['avi', 'mp4', 'mov', 'webm']}
    st.update(settings)
    store.save({'sources': [
        {'id': 'hdd', 'name': 'HDD', 'type': 'folder', 'path': tree['hdd']},
        {'id': 'nas', 'name': 'NAS', 'type': 'folder', 'path': tree['nas']},
        {'id': 'csv', 'name': 'CSV', 'type': 'csv', 'path': tree['csv']},
    ], 'settings': st})
    return store


def scan(store, season=fixture.SEASON):
    return tm_core.run_scan(store, season, tm_core.Job('scan', season))


def by_name(payload):
    return {(payload['stations'][a[1]], a[2]): a for a in payload['items']}


# ---------------------------------------------------------------- JPEG / AVI / MP4

def _dht_tables(jpeg):
    tables = {}
    pos = 2
    while pos < len(jpeg):
        m = jpeg[pos + 1]
        if m == 0xDA:
            break
        ln = struct.unpack('>H', jpeg[pos + 2:pos + 4])[0]
        if m == 0xC4:
            seg = jpeg[pos + 4:pos + 2 + ln]
            i = 0
            while i < len(seg):
                bits = seg[i + 1:i + 17]
                n = sum(bits)
                tables[seg[i]] = seg[i + 1:i + 17 + n]
                i += 17 + n
        pos += 2 + ln
    return tables


def test_std_dht_matches_libjpeg_tables():
    im = Image.new('RGB', (64, 48), (120, 60, 30))
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=80)
    theirs = _dht_tables(buf.getvalue())
    ours = _dht_tables(b'\xff\xd8' + tm_media.STD_DHT + b'\xff\xda')
    assert set(ours) == {0x00, 0x10, 0x01, 0x11}
    assert ours == theirs
    assert tm_media.STD_DHT[:4] == b'\xff\xc4\x01\xa2'


def test_fix_jpeg_restores_decodable_frame():
    orig = fixture.jpeg_frame(3)
    stripped = fixture.strip_dht(orig)
    assert b'\xff\xc4' not in stripped[:stripped.find(b'\xff\xda')]
    fixed = tm_media.fix_jpeg(stripped)
    a = Image.open(io.BytesIO(orig)).convert('RGB')
    b = Image.open(io.BytesIO(fixed)).convert('RGB')
    assert ImageChops.difference(a, b).getbbox() is None
    assert tm_media.fix_jpeg(orig) == orig  # 表があるコマは変えない


@pytest.mark.parametrize('kw', [{}, {'absolute_idx': True}, {'with_index': False}])
def test_parse_avi_variants(tmp_path, kw):
    frames = [fixture.strip_dht(fixture.jpeg_frame(i)) for i in range(12)]
    p = str(tmp_path / 'a.avi')
    fixture.synth_avi(p, frames, fps=15, idit='2026:02:03 04:05:06', **kw)
    info = tm_media.parse_avi(p)
    assert info['mjpeg'] and info['codec'] == 'MJPG'
    assert abs(info['fps'] - 15) < 1e-6
    assert len(info['frames']) == 12
    for i in (0, 5, 11):
        data = tm_media.read_avi_frame(p, info, i)
        im = Image.open(io.BytesIO(data))
        im.load()
        assert im.size == (160, 120)
    assert tm_media.embedded_datetime(p) == (datetime(2026, 2, 3, 4, 5, 6), 'avi')


def test_avi_without_date(tmp_path):
    p = str(tmp_path / 'b.avi')
    fixture.synth_avi(p, [fixture.jpeg_frame(0)])
    assert tm_media.embedded_datetime(p) == (None, '')


def test_ffmpeg_mjpeg_avi(tmp_path):
    p = str(tmp_path / 'ff.avi')
    fixture.ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10', '-t', '1', '-c:v', 'mjpeg', '-q:v', '5', p)
    info = tm_media.parse_avi(p)
    assert info['mjpeg'] and len(info['frames']) == 10 and (info['width'], info['height']) == (320, 240)
    Image.open(io.BytesIO(tm_media.read_avi_frame(p, info, 9))).load()


def test_mp4_creation_time(tree):
    base = os.path.dirname(tree['csv'])
    assert tm_media.mp4_creation_time(os.path.join(base, '_dated.mp4')) == datetime(2026, 1, 2, 3, 4, 5)
    assert tm_media.mp4_creation_time(os.path.join(base, '_nodate.mp4')) is None
    assert tm_media.embedded_datetime(os.path.join(base, '_dated.mp4'))[1] == 'mp4'


@pytest.mark.parametrize('text,want', [
    ('Mon Jan 05 06:07:08 2026\n\x00', datetime(2026, 1, 5, 6, 7, 8)),
    ('2026-01-05 06:07:08', datetime(2026, 1, 5, 6, 7, 8)),
    ('2026/1/5 6:07', datetime(2026, 1, 5, 6, 7, 0)),
    ('2026-01-05T06:07:08Z', datetime(2026, 1, 5, 6, 7, 8)),
    ('2026-01-05', None), ('', None), ('garbage', None), ('2026-13-45 99:99:99', None),
])
def test_parse_date_text(text, want):
    assert tm_media.parse_date_text(text) == want


def test_dt_format_detection():
    assert F.detect_dt_format(['2026/01/05 06:07:08'] * 3) == '%Y/%m/%d %H:%M:%S'
    assert F.detect_dt_format(['2026-01-05 06:07:08']) == '%Y-%m-%d %H:%M:%S'
    assert F.detect_dt_format(['2026/1/5 6:07:08', '2026/10/15 16:07:08']) == 'unpadded/sec'
    assert F.detect_dt_format(['2026/1/5 6:07']) == 'unpadded/nosec'
    assert F.detect_dt_format([]) == F.DEFAULT_DT_FORMAT
    dt = datetime(2026, 1, 5, 6, 7, 8)
    assert F.format_dt(dt, 'unpadded/sec') == '2026/1/5 6:07:08'
    assert F.format_dt(dt, '%Y-%m-%dT%H:%M:%SZ') == '2026-01-05T06:07:08Z'


# ---------------------------------------------------------------- CSV

def test_csv_read_and_noop_roundtrip(tree, tmp_path):
    data = tm_csv.read_season(tree['csv'], fixture.SEASON)
    assert data['encoding'] == 'utf-8-sig' and data['eol'] == '\r\n'
    assert data['header'][:3] == ['Season', 'Station', 'File']
    assert len(data['rows']) == 19   # この Season の行（空欄や対象外の行を含む）
    multi = [r for r in data['rows'] if r[2] == 'IMG_0010.AVI'][0]
    assert multi[5] == 'line one\nline two'
    dst = str(tmp_path / 'out.csv')
    res = tm_csv.write_updated(tree['csv'], dst, data, fixture.SEASON,
                               lambda s, f: (None, '', ''), set(), {}, [])
    assert res['removed'] == 0 and res['ext_updated'] == 0
    assert open(dst, 'rb').read() == open(tree['csv'], 'rb').read()


@pytest.mark.parametrize('enc,eol,final', [('cp932', '\n', True), ('utf-8', '\r\n', False), ('utf-8', '\n', True)])
def test_csv_encodings_and_newlines(tmp_path, enc, eol, final):
    p = str(tmp_path / 'c.csv')
    body = eol.join(['Season,Station,File,メモ', 'S1,A,x.AVI,鹿', 'S2,A,y.AVI,猪', 'S1,B,z.AVI,"a,b"']) + (eol if final else '')
    with open(p, 'w', encoding=enc, newline='') as f:
        f.write(body)
    data = tm_csv.read_season(p, 's1')
    assert data['encoding'] == enc and data['eol'] == eol
    assert [r[2] for r in data['rows']] == ['x.AVI', 'z.AVI']
    dst = str(tmp_path / 'd.csv')

    def keyfunc(st, fl):
        return (st + '/' + os.path.splitext(fl)[0]).lower(), os.path.splitext(fl)[0], os.path.splitext(fl)[1][1:]

    res = tm_csv.write_updated(p, dst, data, 'S1', keyfunc, {'a/x'}, {'b/z': 'MP4'},
                               [['S1', 'C', 'new.MP4', 'NA']], str(tmp_path / 'r.csv'))
    assert res == {'removed': 1, 'ext_updated': 1, 'added': 1, 'records': 3, 'marked': 0, 'mark_kept': 0}
    out = open(dst, encoding=enc, newline='').read()
    assert out == eol.join(['Season,Station,File,メモ', 'S2,A,y.AVI,猪', 'S1,B,z.MP4,"a,b"', 'S1,C,new.MP4,NA']) + eol
    removed = open(str(tmp_path / 'r.csv'), encoding=enc, newline='').read()
    assert removed == eol.join(['Season,Station,File,メモ', 'S1,A,x.AVI,鹿']) + eol


def test_csv_missing_columns(tmp_path):
    p = str(tmp_path / 'bad.csv')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('Season,Camera,File\nS1,A,x.AVI\n')
    with pytest.raises(ValueError) as e:
        tm_csv.read_season(p, 'S1')
    assert 'Station' in str(e.value)


# ---------------------------------------------------------------- 走査と照合

def test_scan_classification(tree, tmp_path):
    store = make_store(tmp_path, tree)
    res = scan(store)
    assert res['items'] == 19
    p = tm_core.load_payload(store, fixture.SEASON)
    assert [s['name'] for s in p['sources']] == ['HDD', 'NAS', 'CSV']
    items = by_name(p)
    want = {
        'IMG_0001': (7, 0), 'IMG_0005': (7, 0),
        'IMG_0006': (7, F.F_EXTDIFF),
        'IMG_0007': (5, F.F_MISSING),
        'IMG_0008': (2, F.F_MISSING),
        'IMG_0009': (3, F.F_MISSING),
        'IMG_0010': (4, F.F_CSVONLY),
        'IMG_0011': (7, F.F_SIZE),
        'IMG_0012': (7, F.F_DUP | F.F_EXTDIFF),
        'IMG_0013': (7, F.F_EXTDIFF | F.F_CSVEXT),
        'IMG_0014': (3, F.F_MISSING),
        'IMG_0015': (7, 0),
    }
    for stem, (mask, flags) in want.items():
        a = items[('BSN0001', stem)]
        assert (a[3], a[4]) == (mask, flags), stem
    for i in range(1, 4):
        a = items[('BSN0002', 'IMG_%04d' % i)]
        assert (a[3], a[4]) == (5, F.F_MISSING)
    a = items[('BSN0003', 'IMG_0001')]          # 大文字小文字の違いは同じ動画
    assert (a[3], a[4]) == (7, 0)
    assert items[('BSN0001', 'IMG_0001')][7][2] == ['AVI', 2]   # CSV に 2 行
    assert items[('BSN0001', 'IMG_0012')][7][0][2] == 2          # HDD に 2 本
    info = p['info']
    assert info['nontarget']['hdd'] == 2
    kinds = sorted(x['kind'] for x in info['exceptions'])
    assert kinds == ['season', 'subdir']
    assert p['csv']['rows'] == 17 and p['csv']['blank'] == 1 and p['csv']['nontarget'] == 1
    assert p['csv']['dtfmt'] == '%Y/%m/%d %H:%M:%S'
    assert info['missing_season'] == []
    # 並びは Station、ファイル名の自然順
    names = [(p['stations'][a[1]], a[2]) for a in p['items']]
    assert names == sorted(names, key=lambda t: (F.natural_key(t[0]), F.natural_key(t[1])))


def test_projects_list(tree, tmp_path):
    store = make_store(tmp_path, tree)
    assert store.projects() == []
    scan(store)
    pr = store.projects()
    assert [x['season'] for x in pr] == [fixture.SEASON] and pr[0]['scanned_at'][:2] == '20'


def test_ext_ref_changes_csvext(tree, tmp_path):
    store = make_store(tmp_path, tree, ext_ref='nas')
    scan(store)
    items = by_name(tm_core.load_payload(store, fixture.SEASON))
    assert items[('BSN0001', 'IMG_0006')][4] == F.F_EXTDIFF | F.F_CSVEXT   # NAS は MP4、CSV は AVI
    assert items[('BSN0001', 'IMG_0012')][4] == F.F_DUP | F.F_EXTDIFF | F.F_CSVEXT


def test_strict_extension_mode(tree, tmp_path):
    store = make_store(tmp_path, tree, ignore_ext=False)
    scan(store)
    items = by_name(tm_core.load_payload(store, fixture.SEASON))
    assert items[('BSN0001', 'IMG_0006.AVI')][3] == 5     # HDD と CSV
    assert items[('BSN0001', 'IMG_0006.MP4')][3] == 2     # NAS のみ
    assert ('BSN0001', 'IMG_0006') not in items


def test_missing_season_and_missing_root(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store, 'N250101')                                  # NAS にはこの Season がない
    p = tm_core.load_payload(store, 'N250101')
    assert p['info']['missing_season'] == ['nas']
    assert len(p['items']) == 2
    cfg = store.config()
    cfg['sources'][1]['path'] = os.path.join(tree['nas'], 'nowhere')
    store.save(cfg)
    with pytest.raises(ValueError) as e:
        scan(store)
    assert 'NAS' in str(e.value)


def test_decisions_survive_rescan(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store)
    items = by_name(tm_core.load_payload(store, fixture.SEASON))
    i7 = items[('BSN0001', 'IMG_0007')][0]
    i8 = items[('BSN0001', 'IMG_0008')][0]
    tm_core.set_decision(store, fixture.SEASON, [i7], 'delete')
    tm_core.set_decision(store, fixture.SEASON, [i8], 'keep')
    tm_core.set_field(store, fixture.SEASON, i7, 'memo', '空撃ち')
    scan(store)
    items = by_name(tm_core.load_payload(store, fixture.SEASON))
    assert items[('BSN0001', 'IMG_0007')][5:7] == ['d', '空撃ち']
    assert items[('BSN0001', 'IMG_0008')][5] == 'k'
    # 動画が消えたあとも、判断は記録に残り、戻ってくれば復元される
    cfg = store.config()
    cfg['sources'][0]['enabled'] = False
    store.save(cfg)
    scan(store)
    p = tm_core.load_payload(store, fixture.SEASON)
    assert p['gone'] == 0 or ('BSN0001', 'IMG_0007') in by_name(p)   # 0007 は CSV に残るので消えない
    cfg['sources'][0]['enabled'] = True
    store.save(cfg)
    scan(store)
    assert by_name(tm_core.load_payload(store, fixture.SEASON))[('BSN0001', 'IMG_0007')][5] == 'd'


def test_item_detail_and_embedded(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store)
    items = by_name(tm_core.load_payload(store, fixture.SEASON))
    d = tm_core.item_detail(store, fixture.SEASON, items[('BSN0001', 'IMG_0008')][0])
    assert d['embedded']['text'] == '2026/01/02 03:04:05' and d['embedded']['kind'] == 'mp4'
    assert d['sources'][0]['files'] == [] and d['sources'][1]['files'][0]['name'] == 'IMG_0008.MP4'
    assert os.path.isfile(d['sources'][1]['files'][0]['path'])
    d = tm_core.item_detail(store, fixture.SEASON, items[('BSN0001', 'IMG_0009')][0])
    assert d['embedded']['text'] == 'NA'
    d = tm_core.item_detail(store, fixture.SEASON, items[('BSN0001', 'IMG_0014')][0])
    assert d['embedded']['text'] == '2026/01/05 06:07:08' and d['embedded']['kind'] == 'avi'
    d = tm_core.item_detail(store, fixture.SEASON, items[('BSN0001', 'IMG_0010')][0])
    assert d['embedded']['text'] == '' and d['sources'][2]['csv']['n'] == 1
    path, src = tm_core.resolve_file(store, fixture.SEASON, items[('BSN0003', 'IMG_0001')][0], 'nas', 0)
    assert path.endswith(os.path.join('bsn0003', 'img_0001.avi')) and os.path.isfile(path)
    with pytest.raises(KeyError):
        tm_core.resolve_file(store, fixture.SEASON, items[('BSN0001', 'IMG_0008')][0], 'hdd', 0)


def test_export_csv(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    items = by_name(tm_core.load_payload(store, S))
    ident = lambda stem: items[('BSN0001', stem)][0]   # noqa: E731
    tm_core.set_decision(store, S, [ident('IMG_0008'), ident('IMG_0009'), ident('IMG_0014')], 'keep')
    tm_core.set_decision(store, S, [ident('IMG_0007'), ident('IMG_0010')], 'delete')
    tm_core.set_field(store, S, ident('IMG_0009'), 'csv_fields', {'Species': 'boar', 'Season': 'ignored'})
    counts = tm_core.csv_change_counts(store, S)
    assert (counts['add'], counts['remove'], counts['remove_rows'], counts['ext'], counts['ext_rows']) == (3, 2, 2, 1, 1)
    res = tm_core.export_csv(store, S, {}, tm_core.Job('export_csv', S))
    assert (res['added'], res['removed'], res['ext_updated'], res['added_na']) == (3, 2, 1, 1)
    out = open(res['path'], 'rb').read()
    orig = open(tree['csv'], 'rb').read()
    assert out.startswith(b'\xef\xbb\xbf') and b'\r\n' in out
    text = out.decode('utf-8-sig')
    lines = text.split('\r\n')
    assert 'N260101,BSN0001,IMG_0013.MP4,2026/01/05 06:15:00,deer,' in lines
    assert not any('IMG_0007' in ln or 'IMG_0010' in ln for ln in lines)
    assert 'line one' not in text
    # Species には既定値（Add）が入る。行ごとに入力した値（boar）はそれより優先する
    assert 'N260101,BSN0001,IMG_0008.MP4,2026/01/02 03:04:05,Add,' in lines
    assert 'N260101,BSN0001,IMG_0009.MP4,NA,boar,' in lines
    assert 'N260101,BSN0001,IMG_0014.AVI,2026/01/05 06:07:08,Add,' in lines
    # ほかの Season の行と、変更のない行は元のまま
    for ln in orig.decode('utf-8-sig').split('\r\n'):
        if ln.startswith('N250101') or 'IMG_0001' in ln or 'photo.JPG' in ln:
            assert ln in lines
    assert open(tree['csv'], 'rb').read() == orig          # 元の CSV は変わらない
    removed = open(res['removed_path'], encoding='utf-8-sig', newline='').read()
    assert 'IMG_0007.AVI' in removed and 'line one\nline two' in removed
    # 選択を外せば、その変更は入らない
    res2 = tm_core.export_csv(store, S, {'add': False, 'remove': False, 'ext': False}, tm_core.Job('x', S))
    assert open(res2['path'], 'rb').read() == orig


def test_export_csv_rejects_changed_csv(tree, tmp_path):
    import shutil
    csv2 = str(tmp_path / 'copy.csv')
    shutil.copy(tree['csv'], csv2)
    t2 = dict(tree, csv=csv2)
    store = make_store(tmp_path, t2)
    scan(store)
    with open(csv2, 'ab') as f:
        f.write(b'N260101,BSN0009,IMG_0001.AVI,,,\r\n')
    with pytest.raises(ValueError):
        tm_core.export_csv(store, fixture.SEASON, {}, tm_core.Job('x', fixture.SEASON))


def test_export_result_and_plan(tree, tmp_path):
    import csv as _csv
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    items = by_name(tm_core.load_payload(store, S))
    ident = lambda stem: items[('BSN0001', stem)][0]   # noqa: E731
    tm_core.set_decision(store, S, [ident('IMG_0007'), ident('IMG_0009'), ident('IMG_0011'), ident('IMG_0012')], 'keep')
    tm_core.set_decision(store, S, [ident('IMG_0008')], 'delete')
    tm_core.set_field(store, S, ident('IMG_0011'), 'pick', 'hdd:IMG_0011.AVI')
    r = tm_core.export_result(store, S)
    rows = list(_csv.reader(open(r['path'], encoding='utf-8-sig', newline='')))
    assert r['rows'] == 19 and len(rows) == 20
    head = rows[0]
    row7 = [x for x in rows if x[2] == 'IMG_0007'][0]
    assert row7[head.index('Status')] == '欠け' and row7[head.index('Decision')] == '残す'
    assert row7[head.index('NAS_file')] == ''
    plan = tm_core.export_plan(store, S)
    acts = list(_csv.reader(open(plan['path'], encoding='utf-8-sig', newline='')))[1:]
    by = {}
    for a in acts:
        by.setdefault(a[2], []).append(a)
    assert [a[0] for a in by['IMG_0007']] == ['コピー']
    assert by['IMG_0007'][0][4] == os.path.join(tree['nas'], S, 'BSN0001', 'IMG_0007.AVI')
    assert [a[0] for a in by['IMG_0009']] == ['CSV に行を追加']
    assert sorted(a[0] for a in by['IMG_0011']) == ['コピー', '退避']        # NAS の小さいほうを退避して入れ替える
    q = [a for a in by['IMG_0011'] if a[0] == '退避'][0]
    assert q[3].startswith(tree['nas']) and q[4].startswith(tree['nas'] + '_removed')
    assert [a[0] for a in by['IMG_0012']] == ['手作業で確認']               # 重複は正とするファイルが未選択
    assert [a[0] for a in by['IMG_0008']] == ['退避']
    assert [a[0] for a in by['IMG_0013']] == ['CSV の拡張子を更新']
    sm = plan['summary']
    assert sm['copy']['NAS'][0] == 2 and sm['quarantine']['NAS'][0] == 2 and sm['csv_add'] == 1 and sm['manual'] == 1


def test_dt_check(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store)
    res = tm_core.dt_check(store, fixture.SEASON, tm_core.Job('dtcheck', fixture.SEASON))
    # CSV に DateTime があり動画もある 15 本。AVI は IDIT の 2026/01/05 06:07:08 を持つ
    assert res['sampled'] == res['candidates'] == 15
    assert res['match'] == 5             # BSN0001 の 0001 から 0005
    assert res['no_embedded'] == 2       # 0013（日時なしの MP4）と 0015（webm）
    assert res['differ'] == 8


def test_config_normalize(tmp_path):
    store = tm_core.Store(str(tmp_path / 'd'))
    cfg = store.save({'sources': [{'type': 'folder', 'path': ' "C:\\PRM_N" ', 'name': ''}, {'type': 'bogus'}],
                      'settings': {'exts': '.AVI, mp4;MOV', 'embedded_tz': 'x', 'ignore_ext': 0}})
    assert cfg['sources'][0]['path'] == 'C:\\PRM_N' and cfg['sources'][0]['name'] == 'フォルダ'
    assert len(cfg['sources']) == 1 and cfg['sources'][0]['id']
    assert cfg['settings']['exts'] == ['avi', 'mp4', 'mov'] and cfg['settings']['embedded_tz'] == 'asis'
    assert cfg['settings']['ignore_ext'] is False
    again = tm_core.Store(str(tmp_path / 'd')).config()
    assert again == json.loads(json.dumps(cfg))


def test_ffmpeg_avi_with_audio_and_non_mjpeg(tmp_path):
    p = str(tmp_path / 'aud.avi')
    fixture.ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440',
                   '-t', '2', '-c:v', 'mjpeg', '-q:v', '4', '-c:a', 'pcm_s16le', '-metadata', 'creation_time=2026-03-04T05:06:07Z', p)
    info = tm_media.parse_avi(p)
    assert info['mjpeg'] and len(info['frames']) == 60 and abs(info['fps'] - 30) < 1e-6
    Image.open(io.BytesIO(tm_media.read_avi_frame(p, info, 59))).load()
    q = str(tmp_path / 'h264.avi')
    fixture.ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=30', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', q)
    info = tm_media.parse_avi(q)
    assert not info['mjpeg'] and info['codec'].lower() in ('h264', 'x264', 'avc1')
    print('ffmpeg avi date:', tm_media.embedded_datetime(p))


def test_field_spans_and_replace():
    raw = '"S1","A","x.AVI","a, ""b""",NA,\r\n'
    body = raw[:-2]
    spans = tm_csv.field_spans(body, ',')
    assert [body[a:b] for a, b, q in spans] == ['"S1"', '"A"', '"x.AVI"', '"a, ""b"""', 'NA', '']
    assert [q for a, b, q in spans] == [True, True, True, True, False, False]
    assert tm_csv.replace_field(raw, ',', 2, 'x.MP4') == '"S1","A","x.MP4","a, ""b""",NA,\r\n'
    assert tm_csv.replace_field('S1,A,x.AVI,note\n', ',', 2, 'x.MP4') == 'S1,A,x.MP4,note\n'
    assert tm_csv.replace_field('S1,A,x.AVI', ',', 2, 'x,y.MP4') == 'S1,A,"x,y.MP4"'
    assert tm_csv.replace_field('S1,A', ',', 2, 'z') is None
    assert [raw_[a:b] for raw_ in ['a,,c'] for a, b, q in tm_csv.field_spans(raw_, ',')] == ['a', '', 'c']
    assert tm_csv.field_spans('', ',') == [(0, 0, False)]


def test_quoted_csv_keeps_its_style(tmp_path):
    p = str(tmp_path / 'r.csv')
    body = '\n'.join(['"Season","Station","File","DateTime","Count"', '"S1","A","x.AVI","2026-01-05 06:07:08",3',
                      '"S1","B","y.AVI",NA,1']) + '\n'
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(body)
    data = tm_csv.read_season(p, 'S1')
    assert data['quote_all'] and data['header'] == ['Season', 'Station', 'File', 'DateTime', 'Count']

    def keyfunc(st, fl):
        return (st + '/' + os.path.splitext(fl)[0]).lower(), os.path.splitext(fl)[0], os.path.splitext(fl)[1][1:]

    dst = str(tmp_path / 'o.csv')
    tm_csv.write_updated(p, dst, data, 'S1', keyfunc, set(), {'a/x': 'MP4'}, [['S1', 'C', 'z.MP4', 'NA', '']])
    out = open(dst, encoding='utf-8', newline='').read()
    assert out == '\n'.join(['"Season","Station","File","DateTime","Count"', '"S1","A","x.MP4","2026-01-05 06:07:08",3',
                             '"S1","B","y.AVI",NA,1', '"S1","C","z.MP4",NA,']) + '\n'


def test_nas_housekeeping_folders_are_ignored(tree, tmp_path):
    import shutil
    base = str(tmp_path / 't2')
    shutil.copytree(os.path.dirname(os.path.dirname(tree['hdd'])), base)
    hdd = os.path.join(base, 'hdd', 'PRM_N')
    nas = os.path.join(base, 'nas', 'PRM_N')
    fixture.put(os.path.join(nas, '@eaDir', 'x', 'thumb.jpg'))
    fixture.put(os.path.join(nas, '#recycle', 'old.AVI'))
    fixture.put(os.path.join(nas, fixture.SEASON, '@eaDir', 'IMG_0001.AVI', 'SYNOVIDEO_FILM.avi'))
    fixture.put(os.path.join(nas, fixture.SEASON, 'BSN0001', '@eaDir', 'IMG_0001.AVI', 'film.avi'))
    t2 = dict(tree, hdd=hdd, nas=nas, csv=os.path.join(base, 'list.csv'))
    store = make_store(tmp_path, t2)
    seasons, errors = tm_core.list_seasons(store.config())
    assert seasons == ['N260101', 'N250101'] and not errors
    assert scan(store)['items'] == 19
    p = tm_core.load_payload(store, fixture.SEASON)
    assert sorted(x['kind'] for x in p['info']['exceptions']) == ['season', 'subdir']
    assert '@eaDir' not in p['stations']


def test_drive_root_paths(tmp_path):
    cfg = tm_core.Store.normalize({'sources': [{'type': 'folder', 'path': 'E:'}, {'type': 'folder', 'path': 'F:\\PRM_N\\'}]})
    assert cfg['sources'][0]['path'] == 'E:\\'
    assert tm_core.quarantine_root({'path': 'E:\\'}) == 'E:' + os.sep + '_removed'
    assert tm_core.quarantine_root({'path': 'F:\\PRM_N\\'}) == 'F:\\PRM_N_removed'
    assert tm_core.quarantine_root({'path': '/data/PRM_N'}) == '/data/PRM_N_removed'


def test_csv_path_with_season_placeholder(tree, tmp_path):
    import shutil
    d = tmp_path / 'lists'
    d.mkdir()
    shutil.copy(tree['csv'], str(d / (fixture.SEASON + '.csv')))
    t2 = dict(tree, csv=str(d / '{Season}.csv'))
    store = make_store(tmp_path, t2)
    assert scan(store)['items'] == 19
    p = tm_core.load_payload(store, fixture.SEASON)
    assert p['sources'][2]['path'] == str(d / (fixture.SEASON + '.csv'))
    with pytest.raises(ValueError):
        scan(store, 'N250101')       # この Season の CSV はない


# ---------------------------------------------------------------- 独立レビューで見つかった点の確認

def copy_tree(tree, tmp_path, name='copy'):
    import shutil
    base = str(tmp_path / name)
    shutil.copytree(os.path.dirname(os.path.dirname(tree['hdd'])), base)
    return dict(tree, hdd=os.path.join(base, 'hdd', 'PRM_N'), nas=os.path.join(base, 'nas', 'PRM_N'),
                csv=os.path.join(base, 'list.csv'))


def snapshot(*roots):
    out = {}
    for root in roots:
        if os.path.isfile(root):
            st = os.stat(root)
            out[root] = (st.st_size, st.st_mtime_ns, open(root, 'rb').read())
            continue
        for d, _dirs, names in os.walk(root):
            for n in names:
                p = os.path.join(d, n)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_storage_is_never_modified(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    before = snapshot(t['hdd'], t['nas'], t['csv'])
    store = make_store(tmp_path, t)
    scan(store)
    S = fixture.SEASON
    p = tm_core.load_payload(store, S)
    ids = [a[0] for a in p['items']]
    tm_core.set_decision(store, S, ids[::2], 'keep')
    tm_core.set_decision(store, S, ids[1::2], 'delete')
    for i in ids:
        tm_core.item_detail(store, S, i)
    tm_core.export_result(store, S)
    tm_core.export_plan(store, S)
    tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    tm_core.dt_check(store, S, tm_core.Job('d', S))
    scan(store)
    assert snapshot(t['hdd'], t['nas'], t['csv']) == before
    assert not os.path.exists(t['hdd'] + '_removed') and not os.path.exists(t['nas'] + '_removed')


def test_zero_size_needs_decision(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    for root in (t['hdd'], t['nas']):
        open(os.path.join(root, fixture.SEASON, 'BSN0001', 'IMG_0002.AVI'), 'wb').close()
    store = make_store(tmp_path, t)
    scan(store)
    a = by_name(tm_core.load_payload(store, fixture.SEASON))[('BSN0001', 'IMG_0002')]
    assert a[4] == F.F_ZERO and a[4] & F.F_NEEDS


def test_season_spelling_is_canonical(tree, tmp_path):
    store = make_store(tmp_path, tree)
    res = scan(store, 'n260101')
    assert res['season'] == 'N260101'
    p = tm_core.load_payload(store, 'n260101')          # 大文字小文字が違っても同じ作業状態を開く
    assert p['season'] == 'N260101' and len(p['items']) == 19
    assert [x['season'] for x in store.projects()] == ['N260101']
    i14 = by_name(p)[('BSN0001', 'IMG_0014')][0]
    tm_core.set_decision(store, 'n260101', [i14], 'keep')
    res = tm_core.export_csv(store, 'n260101', {}, tm_core.Job('x', 'n260101'))
    text = open(res['path'], encoding='utf-8-sig', newline='').read()
    assert 'N260101,BSN0001,IMG_0014.AVI,2026/01/05 06:07:08,Add,' in text.split('\r\n')
    assert 'n260101' not in text


def test_added_row_uses_station_spelling_of_csv(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    # フォルダの名前を小文字にする（Windows のように大文字と小文字を区別しない場所でも同じ結果になるよう、名前の変更で行う）
    os.rename(os.path.join(t['hdd'], fixture.SEASON, 'BSN0003'), os.path.join(t['hdd'], fixture.SEASON, 'bsn0003'))
    assert 'bsn0003' in os.listdir(os.path.join(t['hdd'], fixture.SEASON))
    fixture.put(os.path.join(t['hdd'], fixture.SEASON, 'bsn0003', 'IMG_0002.AVI'), b'x')
    store = make_store(tmp_path, t)
    scan(store)
    S = fixture.SEASON
    p = tm_core.load_payload(store, S)
    item = [a for a in p['items'] if a[2] == 'IMG_0002' and p['stations'][a[1]].lower() == 'bsn0003'][0]
    tm_core.set_decision(store, S, [item[0]], 'keep')
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    text = open(res['path'], encoding='utf-8-sig', newline='').read()
    assert 'N260101,BSN0003,IMG_0002.AVI,NA,Add,' in text.split('\r\n')       # CSV にある綴り（BSN0003）で足す


def test_ids_are_stable_across_rescans(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    store = make_store(tmp_path, t)
    scan(store)
    S = fixture.SEASON
    before = {k: a[0] for k, a in by_name(tm_core.load_payload(store, S)).items()}
    fixture.put(os.path.join(t['hdd'], S, 'BSN0000', 'IMG_0001.AVI'), b'x')     # 並びの先頭に入る Station
    os.remove(os.path.join(t['hdd'], S, 'BSN0001', 'IMG_0009.MP4'))
    os.remove(os.path.join(t['nas'], S, 'BSN0001', 'IMG_0009.MP4'))
    scan(store)
    after = {k: a[0] for k, a in by_name(tm_core.load_payload(store, S)).items()}
    assert ('BSN0001', 'IMG_0009') not in after
    for k, i in after.items():
        if k in before:
            assert before[k] == i, k
    new_id = after[('BSN0000', 'IMG_0001')]
    assert new_id not in before.values()                 # 消えた動画の番号も使い回さない
    fixture.put(os.path.join(t['hdd'], S, 'BSN0001', 'IMG_0009.MP4'), b'y')
    scan(store)
    again = {k: a[0] for k, a in by_name(tm_core.load_payload(store, S)).items()}
    assert again[('BSN0001', 'IMG_0009')] not in list(before.values()) + [new_id]


def test_pick_is_used_for_csv_file_name(tree, tmp_path):
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    i12 = by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0012')][0]
    assert not by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0012')][4] & F.F_CSVEXT
    tm_core.set_decision(store, S, [i12], 'keep')
    tm_core.set_field(store, S, i12, 'pick', 'hdd:IMG_0012.MP4')
    assert by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0012')][4] & F.F_CSVEXT
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    text = open(res['path'], encoding='utf-8-sig', newline='').read()
    assert 'N260101,BSN0001,IMG_0012.MP4,2026/01/05 06:14:00,deer,' in text.split('\r\n')
    import csv as _csv
    plan = tm_core.export_plan(store, S)
    acts = [a for a in _csv.reader(open(plan['path'], encoding='utf-8-sig', newline='')) if a[2] == 'IMG_0012']
    assert sorted(a[0] for a in acts) == ['CSV の拡張子を更新', '退避']
    assert [a[3] for a in acts if a[0] == '退避'][0].endswith('IMG_0012.AVI')
    assert [a[4] for a in acts if a[0] == 'CSV の拡張子を更新'] == ['IMG_0012.MP4']


def test_unreadable_video_is_not_recorded_as_no_date(tree, tmp_path, monkeypatch):
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    i14 = by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0014')][0]
    real = tm_media.embedded_datetime

    def broken(path):
        raise OSError(5, 'network path not found')

    monkeypatch.setattr(tm_media, 'embedded_datetime', broken)
    d = tm_core.item_detail(store, S, i14)
    assert d['embedded']['unreadable'] and d['embedded']['text'] == 'NA'
    tm_core.set_decision(store, S, [i14], 'keep')
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert res['added'] == 1 and res['added_unreadable'] == 1
    monkeypatch.setattr(tm_media, 'embedded_datetime', real)       # つながり直した
    d = tm_core.item_detail(store, S, i14)
    assert not d['embedded']['unreadable'] and d['embedded']['text'] == '2026/01/05 06:07:08'
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert res['added_unreadable'] == 0 and res['added_na'] == 0


def test_failed_csv_export_leaves_no_file(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    raw = open(t['csv'], encoding='utf-8-sig', newline='').read().replace(',hare,', ',ノウサギ,')
    with open(t['csv'], 'w', encoding='cp932', newline='') as f:
        f.write(raw)
    store = make_store(tmp_path, t)
    scan(store)
    S = fixture.SEASON
    i14 = by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0014')][0]
    tm_core.set_decision(store, S, [i14], 'keep')
    tm_core.set_field(store, S, i14, 'csv_fields', {'Note': 'deer \U0001F98C'})    # cp932 で表せない文字
    with pytest.raises(ValueError) as e:
        tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert 'cp932' in str(e.value)
    assert os.listdir(store.export_dir(S)) == []
    tm_core.set_field(store, S, i14, 'csv_fields', {'Note': 'deer'})
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert sorted(os.listdir(store.export_dir(S))) == [os.path.basename(res['path'])]


def test_csv_rows_with_mixed_extensions(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    with open(t['csv'], 'a', encoding='utf-8', newline='') as f:
        f.write('N260101,BSN0001,IMG_0002.MP4,2026/01/05 06:07:08,boar,\r\n')    # 実ファイルは AVI
    store = make_store(tmp_path, t)
    scan(store)
    S = fixture.SEASON
    a = by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0002')]
    assert a[4] == F.F_EXTDIFF | F.F_CSVEXT
    c = tm_core.csv_change_counts(store, S)
    assert (c['ext'], c['ext_rows']) == (2, 2)          # IMG_0013 の 1 行と、IMG_0002 の .MP4 の 1 行
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    lines = open(res['path'], encoding='utf-8-sig', newline='').read().split('\r\n')
    assert 'N260101,BSN0001,IMG_0002.AVI,2026/01/05 06:07:08,deer,' in lines
    assert 'N260101,BSN0001,IMG_0002.AVI,2026/01/05 06:07:08,boar,' in lines
    assert res['ext_updated'] == 2


def test_stray_quote_does_not_swallow_rows(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    raw = open(t['csv'], encoding='utf-8-sig', newline='').read()
    raw = raw.replace('N260101,BSN0001,IMG_0002.AVI,2026/01/05 06:07:08,deer,',
                      'N260101,BSN0001,IMG_0002.AVI,2026/01/05 06:07:08,deer,5" antler')
    with open(t['csv'], 'w', encoding='utf-8-sig', newline='') as f:
        f.write(raw)
    data = tm_csv.read_season(t['csv'], fixture.SEASON)
    assert data['malformed'] >= 1
    store = make_store(tmp_path, t)
    scan(store)
    p = tm_core.load_payload(store, fixture.SEASON)
    assert p['csv']['malformed'] >= 1
    items = by_name(p)
    for stem in ('IMG_0002', 'IMG_0003', 'IMG_0004', 'IMG_0005', 'IMG_0010', 'IMG_0011', 'IMG_0013'):
        assert items[('BSN0001', stem)][3] & 4, stem                # CSV の行として読めている
    assert items[('BSN0002', 'IMG_0001')][3] & 4
    dst = str(tmp_path / 'noop.csv')
    tm_csv.write_updated(t['csv'], dst, data, fixture.SEASON, lambda s, f: (None, '', ''), set(), {}, [])
    assert open(dst, 'rb').read() == open(t['csv'], 'rb').read()


def test_quarantine_root_for_share_roots():
    assert tm_core.quarantine_root({'path': '\\\\DS2422\\PRM_N'}) == '\\\\DS2422\\PRM_N' + os.sep + '_removed'
    assert tm_core.quarantine_root({'path': '\\\\DS2422\\video\\PRM_N'}) == '\\\\DS2422\\video\\PRM_N_removed'
    assert tm_core.quarantine_root({'path': 'Z:\\BOSO\\MOVIE\\PRM_N'}) == 'Z:\\BOSO\\MOVIE\\PRM_N_removed'


def test_apple_double_and_deep_folders(tree, tmp_path):
    t = copy_tree(tree, tmp_path)
    S = fixture.SEASON
    fixture.put(os.path.join(t['nas'], S, 'BSN0001', '._IMG_0001.AVI'), b'meta')
    fixture.put(os.path.join(t['nas'], S, 'BSN0001', 'DCIM', '100MEDIA', 'IMG_7777.AVI'), b'x')
    store = make_store(tmp_path, t)
    assert scan(store)['items'] == 19
    p = tm_core.load_payload(store, S)
    assert ('BSN0001', '._IMG_0001') not in by_name(p)
    deep = [x for x in p['info']['exceptions'] if x['src'] == 'nas']
    assert len(deep) == 1 and deep[0]['count'] == 1 and deep[0]['path'].endswith('DCIM')


# ---------------------------------------------------------------- DateTime の取得元（0.1.1）

REAL_HEADER = ('Season,Season_sub_folder,Station,File,DateTime,DateTimeCorrected,DateTimeIsOk,DateTimeChange,'
               'DeleteFlag,Pass,Star,Remarks,PassCount,Indep,LeftCens,RightCens,StartTime,EndTime,Species')


# 追加する行の、DeleteFlag から Species までに入る既定値
REAL_DEFAULT_TAIL = 'FALSE,TRUE,FALSE,NA,0,TRUE,FALSE,FALSE,999.9,999.9,Add'


def build_browning_like(base, n=30, extra=('IMG_0031.AVI', 'IMG_0032.AVI')):
    """実データに似せた保存場所を作る。

    日時の埋め込みがない AVI、分単位の ISO 表記の DateTime（ファイルの更新日時を分に切り捨てた値）、
    cp932 で改行が LF の CSV。extra の動画は HDD と NAS にあって CSV にない。
    """
    S = 'N180812'
    hdd = os.path.join(base, 'hdd')              # ドライブの直下に Season が並ぶ形
    nas = os.path.join(base, 'nas', 'PRM_N')
    avi = str(os.path.join(base, 'plain.avi'))
    os.makedirs(base, exist_ok=True)
    fixture.synth_avi(avi, [fixture.jpeg_frame(i) for i in range(3)])      # IDIT なし
    data = open(avi, 'rb').read()
    t0 = 1528701000                               # 2018-06-11 ごろ
    lines = [REAL_HEADER]
    times = {}
    names = ['IMG_%04d.AVI' % i for i in range(1, n + 1)] + list(extra)
    for k, name in enumerate(names):
        ts = t0 + k * 137 + (k * 7) % 60          # 秒はばらばら
        ts -= ts % 2                              # FAT の更新日時は 2 秒きざみ
        times[name] = ts
        for root in (hdd, nas):
            p = os.path.join(root, S, 'BSN1053', name)
            fixture.put(p, data)
            os.utime(p, (ts, ts))
        if name not in extra:
            dt = datetime.fromtimestamp(ts).replace(second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
            lines.append('%s,%s,BSN1053,%s,%s,%s,TRUE,TRUE,FALSE,TRUE,FALSE,NA,0,TRUE,FALSE,FALSE,999.9,999.9,Empty'
                         % (S, S, name, dt, dt))
    fixture.put(os.path.join(hdd, 'System Volume Information', 'x.dat'))
    csv_path = os.path.join(base, 'PRM_N.csv')
    with open(csv_path, 'w', encoding='cp932', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    return {'hdd': hdd, 'nas': nas, 'csv': csv_path, 'season': S, 'times': times}


def test_datetime_from_file_time(tmp_path):
    t = build_browning_like(str(tmp_path / 'b'))
    S = t['season']
    store = make_store(tmp_path, t)
    assert tm_core.list_seasons(store.config())[0] == [S]       # System Volume Information は Season の候補にしない
    scan(store, S)
    p = tm_core.load_payload(store, S)
    items = by_name(p)
    assert len(items) == 32 and p['csv']['dtfmt'] == '%Y-%m-%dT%H:%M:%SZ'
    i31, i32 = items[('BSN1053', 'IMG_0031')][0], items[('BSN1053', 'IMG_0032')][0]
    c = tm_core.csv_change_counts(store, S)
    assert c['dt_source'] == 'embedded'
    assert c['dt_profile'] == {'minute_only': True, 'n_dt': 30,
                               'mtime': {'n': 30, 'match': 30, 'near': 0, 'differ': 0, 'disagree': 0}}
    # 既定（埋め込みのみ）: 埋め込みがないので NA。更新日時は表示だけされる
    d = tm_core.item_detail(store, S, i31)
    ts = t['times']['IMG_0031.AVI']
    assert d['embedded']['text'] == 'NA' and d['fill'] == {'text': 'NA', 'origin': 'na', 'minute_only': True,
                                                           'dt_source': 'embedded'}
    assert d['mtime'] == {'text': datetime.fromtimestamp(ts).strftime('%Y/%m/%d %H:%M:%S'),
                          'src': 'HDD: IMG_0031.AVI', 'differs': False}
    tm_core.set_decision(store, S, [i31, i32], 'keep')
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert (res['added'], res['added_na'], res['added_mtime'], res['added_embedded']) == (2, 2, 0, 0)
    # 埋め込みがなければ更新日時を使う設定
    cfg = store.config()
    cfg['settings']['dt_source'] = 'embedded_mtime'
    store.save(cfg)
    want31 = datetime.fromtimestamp(ts).replace(second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    d = tm_core.item_detail(store, S, i31)
    assert d['fill'] == {'text': want31, 'origin': 'mtime', 'minute_only': True, 'dt_source': 'embedded_mtime'}
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert (res['added'], res['added_na'], res['added_mtime'], res['minute_only']) == (2, 0, 2, True)
    raw = open(res['path'], 'rb').read()
    assert b'\r' not in raw                                   # 改行は LF のまま
    lines = raw.decode('cp932').split('\n')
    assert lines[0] == REAL_HEADER
    assert 'N180812,N180812,BSN1053,IMG_0031.AVI,%s,,,,%s' % (want31, REAL_DEFAULT_TAIL) in lines
    assert lines[1:31] == open(t['csv'], encoding='cp932', newline='').read().split('\n')[1:31]
    # 手で入れた DateTime は上書きしない
    tm_core.set_field(store, S, i32, 'csv_fields', {'DateTime': '2018-06-11T09:00:00Z', 'Species': 'Deer'})
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    text = open(res['path'], encoding='cp932', newline='').read()
    assert ('N180812,N180812,BSN1053,IMG_0032.AVI,2018-06-11T09:00:00Z,,,,%s'
            % REAL_DEFAULT_TAIL.replace(',Add', ',Deer')) in text.split('\n')
    assert res['added_mtime'] == 1


def test_file_time_differs_between_locations(tmp_path):
    t = build_browning_like(str(tmp_path / 'b'))
    S = t['season']
    # NAS 側の 0031 と、既存の 5 本は、あとからコピーし直して更新日時が新しくなっている
    later = 1700000000
    for name in ['IMG_0031.AVI'] + ['IMG_%04d.AVI' % i for i in range(1, 6)]:
        os.utime(os.path.join(t['nas'], S, 'BSN1053', name), (later, later))
    # 0032 は HDD と NAS の両方で更新日時が変わっている（元の日時はどこにも残っていない）
    for root in (t['hdd'], t['nas']):
        os.utime(os.path.join(root, S, 'BSN1053', 'IMG_0032.AVI'), (later, later))
    # 既存の 3 本は、HDD と NAS の両方で 1 時間ずれている
    for name in ['IMG_0010.AVI', 'IMG_0011.AVI', 'IMG_0012.AVI']:
        for root in (t['hdd'], t['nas']):
            p = os.path.join(root, S, 'BSN1053', name)
            os.utime(p, (t['times'][name] + 3600, t['times'][name] + 3600))
    store = make_store(tmp_path, t, dt_source='embedded_mtime')
    scan(store, S)
    items = by_name(tm_core.load_payload(store, S))
    c = tm_core.csv_change_counts(store, S)
    assert c['dt_profile']['mtime'] == {'n': 30, 'match': 27, 'near': 0, 'differ': 3, 'disagree': 5}
    d = tm_core.item_detail(store, S, items[('BSN1053', 'IMG_0031')][0])
    ts = t['times']['IMG_0031.AVI']
    assert d['mtime']['differs'] and d['mtime']['src'] == 'HDD: IMG_0031.AVI'       # 古いほう（HDD）を使う
    assert d['fill']['text'] == datetime.fromtimestamp(ts).replace(second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    d = tm_core.item_detail(store, S, items[('BSN1053', 'IMG_0032')][0])
    assert not d['mtime']['differs']
    assert d['fill']['text'] == datetime.fromtimestamp(later).replace(second=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_seconds_are_kept_when_csv_has_seconds(tree, tmp_path):
    store = make_store(tmp_path, tree, dt_source='embedded_mtime')
    scan(store)
    S = fixture.SEASON
    items = by_name(tm_core.load_payload(store, S))
    c = tm_core.csv_change_counts(store, S)
    assert c['dt_profile']['minute_only'] is False
    i9 = items[('BSN0001', 'IMG_0009')][0]                    # 埋め込みのない MP4
    d = tm_core.item_detail(store, S, i9)
    ts = os.stat(os.path.join(tree['hdd'], S, 'BSN0001', 'IMG_0009.MP4')).st_mtime
    ts2 = os.stat(os.path.join(tree['nas'], S, 'BSN0001', 'IMG_0009.MP4')).st_mtime
    want = datetime.fromtimestamp(int(min(ts, ts2))).strftime('%Y/%m/%d %H:%M:%S')
    assert d['fill']['origin'] == 'mtime' and d['fill']['text'] == want
    d = tm_core.item_detail(store, S, items[('BSN0001', 'IMG_0008')][0])      # 埋め込みがあればそちらを使う
    assert d['fill']['origin'] == 'embedded' and d['fill']['text'] == '2026/01/02 03:04:05'


def test_unreadable_video_does_not_fall_back_to_file_time(tree, tmp_path, monkeypatch):
    store = make_store(tmp_path, tree, dt_source='embedded_mtime')
    scan(store)
    S = fixture.SEASON
    i14 = by_name(tm_core.load_payload(store, S))[('BSN0001', 'IMG_0014')][0]

    def broken(path):
        raise OSError(5, 'network path not found')

    monkeypatch.setattr(tm_media, 'embedded_datetime', broken)
    d = tm_core.item_detail(store, S, i14)
    assert d['fill']['text'] == 'NA' and d['fill']['origin'] == 'unreadable'    # 埋め込みの有無がわからないあいだは決めない


def test_dt_profile_is_recomputed_after_rescan(tmp_path):
    t = build_browning_like(str(tmp_path / 'b'))
    S = t['season']
    store = make_store(tmp_path, t)
    scan(store, S)
    assert tm_core.csv_change_counts(store, S)['dt_profile']['mtime']['match'] == 30
    import time as _time
    _time.sleep(1.1)                                          # 走査の時刻（秒単位）が変わるように
    p = os.path.join(t['hdd'], S, 'BSN1053', 'IMG_0001.AVI')
    q = os.path.join(t['nas'], S, 'BSN1053', 'IMG_0001.AVI')
    os.utime(p, (1700000000, 1700000000))
    os.utime(q, (1700000000, 1700000000))
    scan(store, S)
    assert tm_core.csv_change_counts(store, S)['dt_profile']['mtime'] == {
        'n': 30, 'match': 29, 'near': 0, 'differ': 1, 'disagree': 0}


def test_dt_source_setting_is_validated(tmp_path):
    cfg = tm_core.Store.normalize({'settings': {'dt_source': 'embedded_mtime'}})
    assert cfg['settings']['dt_source'] == 'embedded_mtime'
    cfg = tm_core.Store.normalize({'settings': {'dt_source': 'whatever'}})
    assert cfg['settings']['dt_source'] == 'embedded'
    assert tm_core.skip_dir('System Volume Information') and tm_core.skip_dir('$RECYCLE.BIN')
    assert not tm_core.skip_dir('N180812')


def _export_lines(store, S):
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    return open(res['path'], encoding='cp932', newline='').read().split('\n')


def test_added_row_defaults(tmp_path):
    """CSV に足す行の、DateTime 以外の列の既定値。"""
    t = build_browning_like(str(tmp_path / 'b'))
    S = t['season']
    store = make_store(tmp_path, t)
    scan(store, S)
    p = tm_core.load_payload(store, S)
    hdr = REAL_HEADER.split(',')
    # 画面に渡す既定値は列番号ごと。Season、Station、File、DateTime と、空欄の指定の列は入らない
    want = {'Season_sub_folder': S, 'DeleteFlag': 'FALSE', 'Pass': 'TRUE', 'Star': 'FALSE', 'Remarks': 'NA',
            'PassCount': '0', 'Indep': 'TRUE', 'LeftCens': 'FALSE', 'RightCens': 'FALSE',
            'StartTime': '999.9', 'EndTime': '999.9', 'Species': 'Add'}
    assert {hdr[int(i)]: v for i, v in p['csv']['defaults'].items()} == want
    c = tm_core.csv_change_counts(store, S)
    assert c['add_defaults'] == [[h, want[h]] for h in hdr if h in want]      # 見出しの順
    i31 = by_name(p)[('BSN1053', 'IMG_0031')][0]
    i32 = by_name(p)[('BSN1053', 'IMG_0032')][0]
    tm_core.set_decision(store, S, [i31, i32], 'keep')
    # 行ごとの入力は既定値より優先する。入力のない列は既定値のまま
    tm_core.set_field(store, S, i32, 'csv_fields', {'species': 'Boar', 'Remarks': 'tail', 'DateTimeIsOk': 'TRUE'})
    lines = _export_lines(store, S)
    assert 'N180812,N180812,BSN1053,IMG_0031.AVI,NA,,,,%s' % REAL_DEFAULT_TAIL in lines
    assert 'N180812,N180812,BSN1053,IMG_0032.AVI,NA,,TRUE,,FALSE,TRUE,FALSE,tail,0,TRUE,FALSE,FALSE,999.9,999.9,Boar' in lines
    assert lines[1:31] == open(t['csv'], encoding='cp932', newline='').read().split('\n')[1:31]   # 既存の行はそのまま
    # 設定で変えた既定値。見出しとの照合は大文字小文字を区別しない。
    # Season、Station、File、DateTime への指定と、CSV にない列は無視する
    cfg = store.config()
    cfg['settings']['add_defaults'] = {'species': 'New', 'Remarks': '', 'DateTime': 'x', 'Station': 'y', 'File': 'z',
                                       'Season': 'w', 'NoSuchColumn': 'v', 'StartTime': 1.5, 'Star': True,
                                       'Season_sub_folder': 'sub_{season}'}
    saved = store.save(cfg)['settings']['add_defaults']
    assert saved == {'species': 'New', 'DateTime': 'x', 'Station': 'y', 'File': 'z', 'Season': 'w',
                     'NoSuchColumn': 'v', 'StartTime': '1.5', 'Star': 'TRUE', 'Season_sub_folder': 'sub_{season}'}
    assert tm_core.csv_change_counts(store, S)['add_defaults'] == [
        ['Season_sub_folder', 'sub_N180812'], ['Star', 'TRUE'], ['StartTime', '1.5'], ['Species', 'New']]
    lines = _export_lines(store, S)
    assert 'N180812,sub_N180812,BSN1053,IMG_0031.AVI,NA,,,,,,TRUE,,,,,,1.5,,New' in lines
    # 既定値をすべて外すと、ほかの列は空欄
    cfg['settings']['add_defaults'] = {}
    assert store.save(cfg)['settings']['add_defaults'] == {}
    assert tm_core.load_payload(store, S)['csv']['defaults'] == {}
    assert 'N180812,,BSN1053,IMG_0031.AVI,NA,,,,,,,,,,,,,,' in _export_lines(store, S)
    # 設定に項目がない（以前の版の設定）ときは、組み込みの既定値
    del cfg['settings']['add_defaults']
    assert store.save(cfg)['settings']['add_defaults'] == tm_core.ADD_ROW_DEFAULTS
    assert 'N180812,N180812,BSN1053,IMG_0031.AVI,NA,,,,%s' % REAL_DEFAULT_TAIL in _export_lines(store, S)


def test_add_defaults_setting_is_cleaned():
    norm = lambda v: tm_core.Store.normalize({'settings': {'add_defaults': v}})['settings']['add_defaults']   # noqa: E731
    assert tm_core.Store.normalize({})['settings']['add_defaults'] == tm_core.ADD_ROW_DEFAULTS
    assert norm(None) == tm_core.ADD_ROW_DEFAULTS and norm(['a']) == tm_core.ADD_ROW_DEFAULTS
    assert norm({}) == {}
    assert norm({' Pass ': ' TRUE ', 'pass': 'FALSE', '': 'x', 'A': None, 'B': 'two\nlines', 'C': 'x' * 501,
                 'D': 0, 'E': False}) == {'Pass': 'TRUE', 'D': '0', 'E': 'FALSE'}
    many = norm({'c%03d' % i: '1' for i in range(300)})
    assert len(many) == tm_core.MAX_ADD_DEFAULTS
    # 組み込みの既定値を、設定の変更で書き換えてしまわない
    a = tm_core.Store.normalize({})
    a['settings']['add_defaults']['Species'] = 'changed'
    assert tm_core.Store.normalize({})['settings']['add_defaults']['Species'] == 'Add'
    assert tm_core.ADD_ROW_DEFAULTS['Species'] == 'Add' and tm_core.DEFAULT_SETTINGS['add_defaults']['Species'] == 'Add'


def test_default_columns_follow_the_csv_header(tree, tmp_path):
    """既定値は CSV にある列にだけ入る（見出しが違う CSV では、ある列だけ）。"""
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    p = tm_core.load_payload(store, S)
    assert p['csv']['header'] == ['Season', 'Station', 'File', 'DateTime', 'Species', 'Note']
    assert p['csv']['defaults'] == {'4': 'Add'}


# ---------------------------------------------------------------- CSV にしかない動画を「残す」とした行の印
def test_add_mark_value():
    m = tm_csv.add_mark
    assert m('', 'csvのみ') == 'csvのみ' and m('NA', 'csvのみ') == 'csvのみ' and m(' na ', 'csvのみ') == 'csvのみ'
    assert m('cute', 'csvのみ') == 'cute; csvのみ' and m(' Boar, Empty ', 'csvのみ') == 'Boar, Empty; csvのみ'
    assert m('csvのみ', 'csvのみ') is None and m('cute; CSVのみ', 'csvのみ') is None      # すでに入っていれば変えない
    assert m('NAS', 'csvのみ') == 'NAS; csvのみ'                                          # NA という語そのものだけを空欄扱いにする


@pytest.mark.parametrize('enc', ['cp932', 'utf-8-sig'])
def test_write_updated_marks_rows(tmp_path, enc):
    src = str(tmp_path / 'in.csv')
    body = ('Season,Station,File,Remarks,Species\n'
            'S1,A,one.AVI,NA,Deer\n'
            'S1,A,two.AVI,"Boar, Empty",Boar\n'
            'S1,A,three.AVI,,Hare\n'
            'S1,A,four.AVI,csvのみ,Hare\n'
            'S1,A,five.AVI,NA,Deer\n'
            'S1,A,short.AVI\n'
            'S2,A,one.AVI,NA,Deer\n')
    open(src, 'w', encoding=enc, newline='').write(body)
    meta = tm_csv.read_season(src, 'S1')
    key = lambda st, f: (st + '/' + f.split('.')[0], f.split('.')[0], f.split('.')[-1])   # noqa: E731
    keys = {'A/one', 'A/two', 'A/three', 'A/four', 'A/short'}
    dst = str(tmp_path / 'out.csv')
    res = tm_csv.write_updated(src, dst, meta, 'S1', key, set(), {}, [], marks=(3, 'csvのみ', keys))
    assert (res['marked'], res['mark_kept']) == (4, 1)
    out = open(dst, encoding=enc, newline='').read()
    assert out == ('Season,Station,File,Remarks,Species\n'
                   'S1,A,one.AVI,csvのみ,Deer\n'
                   'S1,A,two.AVI,"Boar, Empty; csvのみ",Boar\n'       # 引用符で囲まれていた値は囲んだまま、後ろに足す
                   'S1,A,three.AVI,csvのみ,Hare\n'
                   'S1,A,four.AVI,csvのみ,Hare\n'                      # すでに入っている行はそのまま
                   'S1,A,five.AVI,NA,Deer\n'                           # 対象でない行は変えない
                   'S1,A,short.AVI,csvのみ\n'                          # 列が足りない行は列を補う
                   'S2,A,one.AVI,NA,Deer\n')                           # ほかの Season は同じ名前でも変えない
    # 印を付けない書き出しは、元と 1 バイトも違わない
    res = tm_csv.write_updated(src, dst, meta, 'S1', key, set(), {}, [])
    assert open(dst, 'rb').read() == open(src, 'rb').read() and res['marked'] == 0


def test_mark_keeps_quote_all_style(tmp_path):
    src = str(tmp_path / 'q.csv')
    open(src, 'w', encoding='utf-8', newline='').write(
        '"Season","Station","File","Remarks"\r\n"S1","A","one.AVI",NA\r\n"S1","A","two.AVI","x"\r\n')
    meta = tm_csv.read_season(src, 'S1')
    assert meta['quote_all']
    key = lambda st, f: (st + '/' + f.split('.')[0], f.split('.')[0], f.split('.')[-1])   # noqa: E731
    dst = str(tmp_path / 'o.csv')
    tm_csv.write_updated(src, dst, meta, 'S1', key, set(), {}, [], marks=(3, 'csvのみ', {'A/one', 'A/two'}))
    assert open(dst, encoding='utf-8', newline='').read() == (
        '"Season","Station","File","Remarks"\r\n"S1","A","one.AVI","csvのみ"\r\n"S1","A","two.AVI","x; csvのみ"\r\n')


def build_with_csv_only(base):
    """実データに似せた保存場所に、CSV にしかない Station（動画がどこにもない）を足す。"""
    t = build_browning_like(base)
    S = t['season']
    tail = 'TRUE,TRUE,FALSE,TRUE,FALSE,%s,0,TRUE,FALSE,FALSE,999.9,999.9,%s'
    extra = []
    for i in range(1, 7):
        rem = {2: '"Boar, Empty"', 3: 'cute'}.get(i, 'NA')
        extra.append('%s,%s,BSN0546,IMG_%04d.AVI,2019-05-30T10:45:00Z,2019-05-30T10:45:00Z,%s'
                     % (S, S, i, tail % (rem, 'Deer')))
    extra.append('%s,%s,BSN0546,IMG_0001.AVI,2019-05-30T10:45:00Z,2019-05-30T10:45:00Z,%s'
                 % (S, S, tail % ('NA', 'Boar')))                      # 同じ動画に 2 行（2 種）
    extra.append('N990101,N990101,BSN0546,IMG_0001.AVI,2019-05-30T10:45:00Z,2019-05-30T10:45:00Z,%s'
                 % (tail % ('NA', '動画なし')))                        # ほかの Season の同じ名前。日本語を含む（実データと同じく cp932 と判定される）
    with open(t['csv'], 'a', encoding='cp932', newline='') as f:
        f.write('\n'.join(extra) + '\n')
    t['extra'] = extra
    return t


def test_csv_only_kept_rows_are_marked(tmp_path):
    t = build_with_csv_only(str(tmp_path / 'b'))
    S = t['season']
    store = make_store(tmp_path, t)
    scan(store, S)
    p = tm_core.load_payload(store, S)
    items = by_name(p)
    assert p['csv']['mark'] == {'col': 'Remarks', 'text': 'csvのみ', 'cols': ['Note', 'Remarks']}   # Note 列がないので Remarks
    ident = lambda stem, st='BSN0546': items[(st, stem)][0]   # noqa: E731
    # 0001（2 行）、0002、0003 は残す。0004 は例外、0005 は削除、0006 は未判断
    tm_core.set_decision(store, S, [ident('IMG_0001'), ident('IMG_0002'), ident('IMG_0003')], 'keep')
    tm_core.set_decision(store, S, [ident('IMG_0004')], 'except')
    tm_core.set_decision(store, S, [ident('IMG_0005')], 'delete')
    # 動画がある（CSV のみではない）ものを残しても、印は付かない
    tm_core.set_decision(store, S, [ident('IMG_0001', 'BSN1053'), ident('IMG_0031', 'BSN1053')], 'keep')
    c = tm_core.csv_change_counts(store, S)
    assert (c['mark'], c['mark_rows'], c['add'], c['remove']) == (3, 4, 1, 1)
    assert c['mark_info'] == {'col': 'Remarks', 'text': 'csvのみ', 'cols': ['Note', 'Remarks']}
    orig = open(t['csv'], encoding='cp932', newline='').read().split('\n')
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert (res['marked'], res['mark_kept'], res['mark_col'], res['mark_text'], res['mark_items']) == (4, 0, 'Remarks', 'csvのみ', 3)
    out = open(res['path'], encoding='cp932', newline='').read().split('\n')
    e = t['extra']
    want = {
        e[0]: e[0].replace(',NA,0,', ',csvのみ,0,'),                         # NA は印に置き換える
        e[1]: e[1].replace('"Boar, Empty"', '"Boar, Empty; csvのみ"'),       # 文字が入っていれば後ろに足す
        e[2]: e[2].replace(',cute,', ',cute; csvのみ,'),
        e[6]: e[6].replace(',NA,0,', ',csvのみ,0,'),                         # 同じ動画の 2 行目にも付く
    }
    changed = [(a, b) for a, b in zip(orig, [ln for ln in out if 'IMG_0031' not in ln or 'BSN1053' not in ln])
               if a != b]
    removed = [ln for ln in orig if ln not in out and ln not in want]
    assert removed == [e[4]]                                                 # 「削除する」の行だけがなくなる
    marked = [ln for ln in out if 'csvのみ' in ln]
    assert sorted(marked) == sorted(want.values())
    for ln in (e[3], e[5], e[7]):                                            # 例外、未判断、ほかの Season の行はそのまま
        assert ln in out
    assert all(ln in out for ln in orig[:31])                                # 動画がある行は変わらない
    assert changed                                                           # （上の比較が空振りでないこと）
    # 印を付けない書き出し
    res = tm_core.export_csv(store, S, {'mark': False}, tm_core.Job('x', S))
    assert res['marked'] == 0 and res['mark_items'] == 0
    assert 'csvのみ' not in open(res['path'], encoding='cp932', newline='').read()
    # 実行計画では「手作業で確認」ではなく、印を付ける作業として出る
    plan = tm_core.export_plan(store, S)
    assert plan['summary']['csv_mark'] == 3 and plan['summary']['manual'] == 0
    rows = [r for r in csv.reader(open(plan['path'], encoding='utf-8-sig', newline='')) if r[1] == 'BSN0546']
    assert sorted(r[0] for r in rows) == ['CSV の行に印を付ける'] * 3 + ['CSV の行を削除']
    assert 'Remarks 列に「csvのみ」（2 行）' in [r for r in rows if r[2] == 'IMG_0001'][0][6]


def test_mark_is_idempotent_and_configurable(tmp_path):
    t = build_with_csv_only(str(tmp_path / 'b'))
    S = t['season']
    store = make_store(tmp_path, t)
    scan(store, S)
    items = by_name(tm_core.load_payload(store, S))
    ids = [items[('BSN0546', 'IMG_%04d' % i)][0] for i in (1, 2, 3)]
    tm_core.set_decision(store, S, ids, 'keep')
    first = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    # 書き出した CSV を元の CSV として使い直しても、印は二重に付かない
    data = open(first['path'], 'rb').read()
    open(t['csv'], 'wb').write(data)
    scan(store, S)
    assert tm_core.csv_change_counts(store, S)['mark'] == 3                  # 判断は再走査しても残る
    again = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert (again['marked'], again['mark_kept']) == (0, 4)
    assert open(again['path'], 'rb').read() == data
    # 書く列を変える: 先に見つかった列を使う。Season、Station、File、DateTime には書かない
    cfg = store.config()
    cfg['settings'].update(mark_cols='File; DateTime、NoSuch, species , Remarks', mark_text=' 動画なし ')
    st = store.save(cfg)['settings']
    assert st['mark_cols'] == ['File', 'DateTime', 'NoSuch', 'species', 'Remarks'] and st['mark_text'] == '動画なし'
    assert tm_core.load_payload(store, S)['csv']['mark']['col'] == 'Species'
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert res['mark_col'] == 'Species' and res['marked'] == 4
    assert sum(1 for ln in open(res['path'], encoding='cp932', newline='').read().split('\n')
               if ln.endswith(',Deer; 動画なし') or ln.endswith(',Boar; 動画なし')) == 4
    # 候補の列が CSV にない: 印は付かず、実行計画では手作業で確認に戻る
    cfg['settings'].update(mark_cols=['Note'])
    store.save(cfg)
    c = tm_core.csv_change_counts(store, S)
    assert c['mark'] == 3 and c['mark_info'] == {'col': '', 'text': '動画なし', 'cols': ['Note']}
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert res['marked'] == 0 and res['mark_col'] == '' and open(res['path'], 'rb').read() == data
    assert tm_core.export_plan(store, S)['summary']['manual'] == 3
    # 印を空にすると無効
    cfg['settings'].update(mark_cols=['Remarks'], mark_text='')
    store.save(cfg)
    assert tm_core.load_payload(store, S)['csv']['mark'] == {'col': '', 'text': '', 'cols': ['Remarks']}
    assert tm_core.export_csv(store, S, {}, tm_core.Job('x', S))['marked'] == 0


def test_mark_prefers_note_column(tree, tmp_path):
    """Note 列がある CSV では Note 列に書く。改行を含む値にも足せる。"""
    store = make_store(tmp_path, tree)
    scan(store)
    S = fixture.SEASON
    p = tm_core.load_payload(store, S)
    assert p['csv']['mark']['col'] == 'Note'
    tm_core.set_decision(store, S, [by_name(p)[('BSN0001', 'IMG_0010')][0]], 'keep')
    res = tm_core.export_csv(store, S, {'ext': False}, tm_core.Job('x', S))
    assert (res['marked'], res['mark_col']) == (1, 'Note')
    text = open(res['path'], encoding='utf-8-sig', newline='').read()
    assert 'N260101,BSN0001,IMG_0010.AVI,2026/01/05 06:12:00,empty,"line one\nline two; csvのみ"\r\n' in text
    assert text.count('csvのみ') == 1
    orig = open(tree['csv'], encoding='utf-8-sig', newline='').read()
    assert text.replace('line two; csvのみ', 'line two') == orig          # ほかは 1 文字も変わらない


def test_mark_settings_are_cleaned():
    norm = lambda **kw: tm_core.Store.normalize({'settings': kw})['settings']   # noqa: E731
    d = tm_core.Store.normalize({})['settings']
    assert d['mark_text'] == 'csvのみ' and d['mark_cols'] == ['Note', 'Remarks']
    assert norm(mark_text='a\nb')['mark_text'] == 'csvのみ' and norm(mark_text=5)['mark_text'] == 'csvのみ'
    assert norm(mark_text='')['mark_text'] == '' and norm(mark_cols=[])['mark_cols'] == []
    assert norm(mark_cols=['Remarks', 'remarks', '', ' Note '])['mark_cols'] == ['Remarks', 'Note']
    assert norm(mark_cols='Camera Note, Remarks')['mark_cols'] == ['Camera Note', 'Remarks']   # 空白を含む列名


# ---------------------------------------------------------------- 全 Season の書き出し
def _read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    return rows[0], [dict(zip(rows[0], r)) for r in rows[1:]]


def test_export_all_seasons(tree, tmp_path):
    store = make_store(tmp_path, tree)
    with pytest.raises(ValueError):
        tm_core.export_all(store, {}, None)                       # 走査済みの Season がない
    S = fixture.SEASON
    scan(store, S)
    # 2 つ目の Season は、保存場所の名前を変えてから走査する（HDD をつなぎ替えた場合など）
    cfg = store.config()
    cfg['sources'][0]['name'] = 'HDD2'
    store.save(cfg)
    scan(store, 'N250101')
    p = tm_core.load_payload(store, S)
    items = by_name(p)
    tm_core.set_decision(store, S, [items[('BSN0001', 'IMG_0007')][0]], 'keep')
    tm_core.set_decision(store, S, [items[('BSN0001', 'IMG_0010')][0]], 'delete')
    tm_core.set_field(store, S, items[('BSN0001', 'IMG_0001')][0], 'memo', '一致しているがメモあり')
    tm_core.set_decision(store, S, [items[('BSN0001', 'IMG_0002')][0]], 'except')       # 一致している動画への判断
    # 壊れたファイルが保存先にあっても、ほかの Season は書き出せる
    open(os.path.join(store.data_dir, 'seasons', 'broken.sqlite'), 'wb').write(b'not a database')

    summ = tm_core.all_seasons_summary(store)
    assert [c['season'] for c in summ['seasons']] == ['N250101', S]
    by = {c['season']: c for c in summ['seasons']}
    n_all = len(p['items'])
    n_mis = sum(1 for a in p['items'] if a[4] & tm_core.F_NEEDS)
    assert (by[S]['videos'], by[S]['mismatch'], by[S]['keep'], by[S]['delete'], by[S]['except']) == (n_all, n_mis, 1, 1, 1)
    assert by[S]['undecided'] == n_mis - 2 and by[S]['csv_only'] == 1
    assert summ['totals']['videos'] == n_all + by['N250101']['videos']

    job = tm_core.Job('export_all', '')
    res = tm_core.export_all(store, {}, job)
    assert res['scope'] == 'mismatch' and res['seasons'] == 2 and res['skipped'] == []
    assert os.path.basename(os.path.dirname(res['path'])) == 'all_seasons'
    assert not [n for n in os.listdir(os.path.dirname(res['path'])) if n.endswith('.part')]
    header, rows = _read_csv(res['path'])
    # 保存場所の列は名前ごと。その Season にない保存場所の列は空欄
    assert header[:3] == ['Season', 'Station', 'File']
    assert [h for h in header if h.endswith('_file')] == ['HDD2_file', 'NAS_file', 'CSV_file', 'HDD_file']
    assert header[-7:] == ['Pattern', 'Status', 'Decision', 'Memo', 'Pick', 'DecidedAt', 'ScannedAt']
    assert res['rows'] == len(rows)
    mine = [r for r in rows if r['Season'] == S]
    other = [r for r in rows if r['Season'] == 'N250101']
    assert len(mine) == n_mis + 2 and len(other) == by['N250101']['mismatch']     # 不一致 + 判断かメモのある一致 2 本
    assert all(r['HDD2_file'] == '' and r['ScannedAt'] for r in mine) and all(r['HDD_file'] == '' for r in other)
    row = {r['File']: r for r in mine if r['Station'] == 'BSN0001'}
    assert row['IMG_0007']['Decision'] == '残す' and row['IMG_0007']['Status'] == '欠け'
    assert row['IMG_0007']['HDD_file'] == 'IMG_0007.AVI' and row['IMG_0007']['NAS_file'] == ''
    assert row['IMG_0007']['Pattern'] == 'HDD:あり NAS:なし CSV:あり'
    assert row['IMG_0010']['Decision'] == '削除する' and row['IMG_0010']['Status'] == 'CSV のみ'
    assert row['IMG_0001']['Memo'] == '一致しているがメモあり' and row['IMG_0001']['Decision'] == '未判断'
    assert row['IMG_0002']['Decision'] == '例外として残す' and row['IMG_0002']['Status'] == '一致'
    assert 'IMG_0003' not in row                                                  # 一致していて判断もメモもない動画は出さない
    assert other and all(r['HDD2_file'] or r['CSV_file'] for r in other)
    # Season ごとの集計
    sh, srows = _read_csv(res['summary_path'])
    assert [r['Season'] for r in srows] == ['N250101', S]
    me = [r for r in srows if r['Season'] == S][0]
    assert (int(me['Videos']), int(me['Mismatch']), int(me['Undecided']), int(me['Keep']), int(me['Delete']),
            int(me['Except']), int(me['Exported'])) == (n_all, n_mis, n_mis - 2, 1, 1, 1, n_mis + 2)
    assert 'HDD=' in me['Sources'] and 'NAS=' in me['Sources']
    assert job.done == job.total == 2
    # すべての動画
    res = tm_core.export_all(store, {'scope': 'all'}, None)
    header2, rows2 = _read_csv(res['path'])
    assert res['scope'] == 'all' and len(rows2) == summ['totals']['videos'] and header2 == header
    # 1 つの Season の一覧と、同じ動画の内容が一致する
    one = tm_core.export_result(store, S)
    h1, r1 = _read_csv(one['path'])
    a = {(r['Station'], r['File']): r for r in r1}
    for r in [x for x in rows2 if x['Season'] == S]:
        for k in h1:
            assert r[k] == a[(r['Station'], r['File'])][k], k


def test_season_name_is_normalised_and_empty_season_is_not_created(tree, tmp_path):
    store = make_store(tmp_path, tree)
    # 全角で入力しても、同じ Season として走査する
    scan(store, 'Ｎ２６０１０１ ')
    assert [p['season'] for p in store.projects()] == [fixture.SEASON]
    assert len(tm_core.load_payload(store, fixture.SEASON)['items']) > 0
    # どこにもない Season 名（入力違い）では、空の作業状態を作らない
    with pytest.raises(ValueError) as e:
        scan(store, 'N260１０２')
    assert 'N260102' in str(e.value) and '見つかりません' in str(e.value)
    assert [p['season'] for p in store.projects()] == [fixture.SEASON]
    assert sorted(os.listdir(os.path.join(store.data_dir, 'seasons'))) == [fixture.SEASON + '.sqlite']
    # 以前の版で作られた空の Season があっても、全 Season の書き出しはできて、その名前を知らせる
    con = store.connect('N260１０２', create=True)
    tm_core.set_meta(con, season='N260１０２', scanned_at='2026-11-17 13:00:00',
                     sources=tm_core.get_meta(store.connect(fixture.SEASON))['sources'])
    con.commit()
    con.close()
    res = tm_core.export_all(store, {}, None)
    assert res['seasons'] == 2 and res['empty'] == ['N260１０２'] and res['rows'] > 0
    sh, srows = _read_csv(res['summary_path'])
    assert [int(r['Videos']) for r in srows if r['Season'] != fixture.SEASON] == [0]
