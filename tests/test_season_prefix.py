# -*- coding: utf-8 -*-
"""Season 名を先頭の英字 1 文字と数字 6 桁だけで照合する（N250102_P01 と N250102_P02 を N250102 として扱う）。"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402
import tm_core  # noqa: E402
import tm_csv  # noqa: E402
from test_core import REAL_DEFAULT_TAIL, REAL_HEADER, _read_csv, by_name, make_store, scan  # noqa: E402

S = 'N250102'
A = b'a' * 300          # ふつうの動画
B = b'b' * 500          # 同じ名前で中身の違う動画


def build_split(base, csv_season=None):
    """HDD では N250102_P01 と N250102_P02 に分かれ、NAS では N250102 の 1 つにまとまっている Season。

    csv_season は、CSV の Season 列に書く綴りを Station ごとに変えるとき（{Station: 綴り}）。
    """
    hdd = os.path.join(base, 'hdd')
    nas = os.path.join(base, 'nas', 'PRM_N')
    put = fixture.put
    H = lambda d, st, name: os.path.join(hdd, d, st, name)   # noqa: E731
    N = lambda st, name: os.path.join(nas, S, st, name)       # noqa: E731
    # BSN0001: P01 にあり、同じ名前の IMG_0001 が P02 にもある（中身が違う）
    put(H('N250102_P01', 'BSN0001', 'IMG_0001.AVI'), A)
    put(H('N250102_P01', 'BSN0001', 'IMG_0002.AVI'), A)
    put(H('N250102_P02', 'BSN0001', 'IMG_0001.AVI'), B)
    put(N('BSN0001', 'IMG_0001.AVI'), A)
    put(N('BSN0001', 'IMG_0002.AVI'), A)
    # BSN0002: P01 だけ
    put(H('N250102_P01', 'BSN0002', 'IMG_0001.AVI'), A)
    put(N('BSN0002', 'IMG_0001.AVI'), A)
    # BSN0003: P02 だけ。IMG_0002 は NAS にしかなく、CSV にもない
    put(H('N250102_P02', 'BSN0003', 'IMG_0001.AVI'), A)
    put(N('BSN0003', 'IMG_0001.AVI'), A)
    put(N('BSN0003', 'IMG_0002.AVI'), A)
    # BSN0004: HDD の P02 にしかない Station（CSV に行がない）
    put(H('N250102_P02', 'BSN0004', 'IMG_0001.AVI'), A)
    # BSN0005: NAS にしかない Station（CSV に行がない）
    put(N('BSN0005', 'IMG_0001.AVI'), A)
    # ほかの Season と、Season の形でないフォルダ
    put(H('N250103_P01', 'BSN0001', 'IMG_0001.AVI'), A)
    put(os.path.join(hdd, 'misc', 'BSN0001', 'IMG_0001.AVI'), A)
    put(os.path.join(hdd, 'N250102_P01', 'stray.AVI'), A)

    cs = csv_season or {}
    sub = {'BSN0001': 'N250102_P01', 'BSN0002': 'N250102_P01', 'BSN0003': 'N250102_P02'}
    lines = [REAL_HEADER]
    for st, name in (('BSN0001', 'IMG_0001.AVI'), ('BSN0001', 'IMG_0002.AVI'), ('BSN0002', 'IMG_0001.AVI'),
                     ('BSN0003', 'IMG_0001.AVI')):
        lines.append('%s,%s,%s,%s,2025-01-05 06:07,,,,FALSE,TRUE,FALSE,メモ,1,TRUE,FALSE,FALSE,0.5,3.2,シカ'
                     % (cs.get(st, S), sub[st], st, name))
    lines.append('N250103,N250103_P01,BSN0001,IMG_0001.AVI,2025-01-06 06:07,,,,FALSE,TRUE,FALSE,NA,1,TRUE,FALSE,FALSE,0.5,3.2,シカ')
    csv_path = os.path.join(base, 'list.csv')
    with open(csv_path, 'w', encoding='cp932', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    return {'hdd': hdd, 'nas': nas, 'csv': csv_path, 'season': S}


def _snapshot(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def _detail(store, p, station, stem):
    return tm_core.item_detail(store, p['season'], by_name(p)[(station, stem)][0])


def _files(d, sid):
    return [x for x in d['sources'] if x['id'] == sid][0]['files']


def _export_lines(store):
    res = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    return open(res['path'], encoding='cp932', newline='').read().split('\n')


def _plan(store):
    res = tm_core.export_plan(store, S)
    return res, _read_csv(res['path'])[1]


def test_season_key():
    k = tm_core.season_key
    assert k('N250102_P01') == 'N250102' and k('N250102') == 'N250102' and k('Ｎ２５０１０２＿Ｐ０２ ') == 'N250102'
    assert k('n250102-b') == 'n250102' and k('N2501020') == 'N250102'
    assert k('N25010') == 'N25010' and k('misc') == 'misc' and k('2025_N250102') == '2025_N250102'
    assert k('N250102_P01', False) == 'N250102_P01' and k('', True) == ''


def test_split_folders_are_one_season(tmp_path):
    t = build_split(str(tmp_path / 't'))
    before = _snapshot(str(tmp_path / 't'))
    store = make_store(tmp_path, t)
    # Season の候補は、まとめた名前で出る
    names, errors = tm_core.list_seasons(store.config())
    assert names == ['N250103', 'N250102', 'misc'] and errors == []
    # 後ろに文字が付いた名前で走査しても、まとめた名前の Season になる
    res = scan(store, 'N250102_P01')
    assert res['season'] == S and [p['season'] for p in store.projects()] == [S]
    p = tm_core.load_payload(store, S)
    assert p['season'] == S and p['season_prefix'] is True and p['inherited_from'] == []
    assert {s['id']: s['dirs'] for s in p['sources']} == {
        'hdd': ['N250102_P01', 'N250102_P02'], 'nas': ['N250102'], 'csv': []}
    info = p['info']
    assert info['missing_season'] == [] and info['station_multi'] == {'hdd': 1, 'nas': 0}
    assert info['stations'] == {'hdd': 4, 'nas': 4} and info['files'] == {'hdd': 6, 'nas': 6, 'csv': 4}
    assert [e['kind'] for e in info['exceptions']] == ['season']             # stray.AVI
    items = by_name(p)
    assert sorted(items) == [('BSN0001', 'IMG_0001'), ('BSN0001', 'IMG_0002'), ('BSN0002', 'IMG_0001'),
                             ('BSN0003', 'IMG_0001'), ('BSN0003', 'IMG_0002'), ('BSN0004', 'IMG_0001'),
                             ('BSN0005', 'IMG_0001')]
    F = tm_core
    flags = {k: v[4] for k, v in items.items()}
    assert flags[('BSN0001', 'IMG_0002')] == 0 and flags[('BSN0002', 'IMG_0001')] == 0
    assert flags[('BSN0003', 'IMG_0001')] == 0                                 # P02 のフォルダの動画も一致になる
    assert flags[('BSN0001', 'IMG_0001')] == F.F_DUP | F.F_SIZE               # 2 つのフォルダに同じ名前
    assert flags[('BSN0003', 'IMG_0002')] == F.F_MISSING
    assert flags[('BSN0004', 'IMG_0001')] == F.F_MISSING and flags[('BSN0005', 'IMG_0001')] == F.F_MISSING

    # ファイルごとに、入っている Season フォルダが分かり、その場所のパスになる
    d = _detail(store, p, 'BSN0001', 'IMG_0001')
    hf = _files(d, 'hdd')
    assert [f['folder'] for f in hf] == ['N250102_P01', 'N250102_P02']
    assert [f['path'] for f in hf] == [os.path.join(t['hdd'], x, 'BSN0001', 'IMG_0001.AVI')
                                       for x in ('N250102_P01', 'N250102_P02')]
    assert [f['pick'] for f in hf] == ['hdd:N250102_P01/BSN0001/IMG_0001.AVI', 'hdd:N250102_P02/BSN0001/IMG_0001.AVI']
    assert [f['pick'] for f in _files(d, 'nas')] == ['nas:IMG_0001.AVI'] and _files(d, 'nas')[0]['folder'] == S
    i1 = d['id']
    for n in (0, 1):
        path, src = tm_core.resolve_file(store, S, i1, 'hdd', n)
        assert path == hf[n]['path'] and open(path, 'rb').read() == (A, B)[n]
    one = _detail(store, p, 'BSN0003', 'IMG_0001')
    assert [f['pick'] for f in _files(one, 'hdd')] == ['hdd:IMG_0001.AVI']
    assert _files(one, 'hdd')[0]['path'] == os.path.join(t['hdd'], 'N250102_P02', 'BSN0003', 'IMG_0001.AVI')

    # CSV に足す行の Season_sub_folder
    hdr = REAL_HEADER.split(',')
    sub = lambda st, stem: {hdr[int(i)]: v for i, v in _detail(store, p, st, stem)['defaults'].items()}['Season_sub_folder']  # noqa: E731
    assert sub('BSN0003', 'IMG_0002') == 'N250102_P02'      # 同じ Station の行が CSV で使っている値
    assert sub('BSN0004', 'IMG_0001') == 'N250102_P02'      # 行のない Station は、動画が入っているフォルダ
    assert sub('BSN0005', 'IMG_0001') == 'N250102'
    assert p['csv']['defaults'][str(hdr.index('Season_sub_folder'))] == tm_core.FOLDER_LABEL
    ids = [items[k][0] for k in (('BSN0003', 'IMG_0002'), ('BSN0004', 'IMG_0001'), ('BSN0005', 'IMG_0001'))]
    tm_core.set_decision(store, S, ids, 'keep')
    lines = _export_lines(store)
    orig = open(t['csv'], encoding='cp932', newline='').read().split('\n')
    assert lines[:6] == orig[:6]                              # 既存の行はそのまま
    added = [x for x in lines if x.endswith(REAL_DEFAULT_TAIL)]
    assert sorted(x.split(',')[:4] for x in added) == [
        ['N250102', 'N250102', 'BSN0005', 'IMG_0001.AVI'],
        ['N250102', 'N250102_P02', 'BSN0003', 'IMG_0002.AVI'],
        ['N250102', 'N250102_P02', 'BSN0004', 'IMG_0001.AVI']]
    assert len(lines) == len(orig) + 3

    # 実行計画: コピー先と退避先は、Season フォルダの名前を含む
    res, rows = _plan(store)
    copies = {(r[list(r)[1]], r[list(r)[2]]): r for r in rows if r[list(r)[0]] == 'コピー'}
    cols = list(rows[0])
    c = copies[('BSN0003', 'IMG_0002')]
    assert c[cols[3]] == os.path.join(t['nas'], S, 'BSN0003', 'IMG_0002.AVI')
    assert c[cols[4]] == os.path.join(t['hdd'], 'N250102_P02', 'BSN0003', 'IMG_0002.AVI')     # Station があるフォルダへ
    assert c[cols[6]] == ''
    c = copies[('BSN0004', 'IMG_0001')]
    assert c[cols[4]] == os.path.join(t['nas'], S, 'BSN0004', 'IMG_0001.AVI') and c[cols[6]] == ''
    c = copies[('BSN0005', 'IMG_0001')]
    # コピー先に Station がなく、Season フォルダが複数あって決められない: 最初のフォルダにして、確かめるよう書く
    assert c[cols[4]] == os.path.join(t['hdd'], 'N250102_P01', 'BSN0005', 'IMG_0001.AVI')
    assert '複数' in c[cols[6]] and 'N250102_P01、N250102_P02' in c[cols[6]]

    # 同じ名前のファイルのうち、P02 のほうを正とする
    tm_core.set_field(store, S, i1, 'pick', hf[1]['pick'])
    tm_core.set_decision(store, S, [i1], 'keep')
    d = tm_core.item_detail(store, S, i1)
    assert d['pick'] == hf[1]['pick']
    res, rows = _plan(store)
    mine = [r for r in rows if r[cols[1]] == 'BSN0001' and r[cols[2]] == 'IMG_0001']
    q = tm_core.quarantine_root
    hsrc = {'path': t['hdd']}
    nsrc = {'path': t['nas']}
    got = sorted((r[cols[0]], r[cols[3]], r[cols[4]]) for r in mine)
    assert got == sorted([
        ('退避', hf[0]['path'], os.path.join(q(hsrc), 'N250102_P01', 'BSN0001', 'IMG_0001.AVI')),
        ('退避', os.path.join(t['nas'], S, 'BSN0001', 'IMG_0001.AVI'), os.path.join(q(nsrc), S, 'BSN0001', 'IMG_0001.AVI')),
        ('コピー', hf[1]['path'], os.path.join(t['nas'], S, 'BSN0001', 'IMG_0001.AVI')),
    ])
    # 以前の形の pick（保存場所:ファイル名）は、同じ名前の最初のファイルを指す
    tm_core.set_field(store, S, i1, 'pick', 'hdd:IMG_0001.AVI')
    assert tm_core.item_detail(store, S, i1)['pick'] == hf[0]['pick']

    # 照合結果の一覧に、Season フォルダの列がある
    h, rr = _read_csv(tm_core.export_result(store, S)['path'])
    row = {(r['Station'], r['File']): r for r in rr}
    assert 'HDD_folder' in h and 'NAS_folder' in h and 'CSV_folder' not in h
    assert row[('BSN0001', 'IMG_0001')]['HDD_folder'] == 'N250102_P01 | N250102_P02'
    assert row[('BSN0003', 'IMG_0001')]['HDD_folder'] == 'N250102_P02'
    assert row[('BSN0003', 'IMG_0001')]['NAS_folder'] == 'N250102' and row[('BSN0005', 'IMG_0001')]['HDD_folder'] == ''
    # 再走査しても判断は保たれる。保存場所と元の CSV は変わらない
    scan(store, 'N250102_P02')
    p2 = tm_core.load_payload(store, S)
    assert by_name(p2)[('BSN0003', 'IMG_0002')][5] == 'k' and [x['season'] for x in store.projects()] == [S]
    assert _snapshot(str(tmp_path / 't')) == before


def test_prefix_off_matches_whole_names(tmp_path):
    t = build_split(str(tmp_path / 't'))
    store = make_store(tmp_path, t, season_prefix=False)
    assert store.config()['settings']['season_prefix'] is False
    names, _ = tm_core.list_seasons(store.config())
    assert names == ['N250103_P01', 'N250102_P02', 'N250102_P01', 'N250102', 'misc']
    res = scan(store, 'N250102_P01')
    assert res['season'] == 'N250102_P01'
    p = tm_core.load_payload(store, 'N250102_P01')
    assert p['season_prefix'] is False
    assert {s['id']: s['dirs'] for s in p['sources']} == {'hdd': ['N250102_P01'], 'nas': [], 'csv': []}
    assert p['info']['missing_season'] == ['nas'] and p['info']['files'] == {'hdd': 3, 'nas': 0, 'csv': 0}
    # 名前の全体が同じ Season は、これまでどおり
    scan(store, S)
    p = tm_core.load_payload(store, S)
    assert p['info']['missing_season'] == ['hdd'] and p['info']['files'] == {'hdd': 0, 'nas': 6, 'csv': 4}


def test_decisions_are_inherited_from_folder_named_projects(tmp_path):
    """先頭だけで照合するようになる前に、フォルダ名のまま走査した Season の判断とメモを引き継ぐ。"""
    t = build_split(str(tmp_path / 't'))
    store = make_store(tmp_path, t, season_prefix=False)
    scan(store, 'N250102_P02')
    old = tm_core.load_payload(store, 'N250102_P02')
    i4 = by_name(old)[('BSN0004', 'IMG_0001')][0]
    i3 = by_name(old)[('BSN0003', 'IMG_0001')][0]
    tm_core.set_decision(store, 'N250102_P02', [i4], 'except')
    tm_core.set_field(store, 'N250102_P02', i3, 'memo', '確認済み')
    cfg = store.config()
    cfg['settings']['season_prefix'] = True
    store.save(cfg)
    scan(store, 'N250102_P02')
    assert sorted(x['season'] for x in store.projects()) == ['N250102', 'N250102_P02']     # 元の作業状態は残す
    p = tm_core.load_payload(store, S)
    assert p['inherited_from'] == ['N250102_P02']
    items = by_name(p)
    assert items[('BSN0004', 'IMG_0001')][5] == 'e' and items[('BSN0003', 'IMG_0001')][6] == '確認済み'
    assert items[('BSN0001', 'IMG_0002')][5] == '' and items[('BSN0001', 'IMG_0002')][6] == ''
    # 引き継ぐのは初めての走査のときだけ。あとで元の作業状態を変えても、こちらは変わらない
    tm_core.set_decision(store, S, [items[('BSN0004', 'IMG_0001')][0]], 'keep')
    tm_core.set_decision(store, 'N250102_P02', [i4], 'delete')
    scan(store, S)
    p = tm_core.load_payload(store, S)
    assert by_name(p)[('BSN0004', 'IMG_0001')][5] == 'k' and p['inherited_from'] == []
    # 元の作業状態は、その名前のまま開ける
    assert tm_core.load_payload(store, 'N250102_P02')['season'] == 'N250102_P02'


def test_csv_season_column_with_suffix(tmp_path):
    """CSV の Season 列に N250102_P01 のように書かれていても、同じ Season の行として読む。"""
    t = build_split(str(tmp_path / 't'), csv_season={'BSN0001': 'N250102_P01', 'BSN0002': 'N250102_P01',
                                                     'BSN0003': 'N250102_P02'})
    store = make_store(tmp_path, t)
    scan(store, S)
    p = tm_core.load_payload(store, S)
    assert p['info']['files']['csv'] == 4 and p['csv']['rows'] == 4
    items = by_name(p)
    assert items[('BSN0003', 'IMG_0001')][4] == 0
    ids = [items[k][0] for k in (('BSN0003', 'IMG_0002'), ('BSN0004', 'IMG_0001'))]
    tm_core.set_decision(store, S, ids, 'keep')
    tm_core.set_decision(store, S, [items[('BSN0002', 'IMG_0001')][0]], 'delete')
    lines = _export_lines(store)
    added = sorted(x.split(',')[:4] for x in lines if x.endswith(REAL_DEFAULT_TAIL))
    # Season 列は、同じ Station の行の綴り。行のない Station は Season 名
    assert added == [['N250102', 'N250102_P02', 'BSN0004', 'IMG_0001.AVI'],
                     ['N250102_P02', 'N250102_P02', 'BSN0003', 'IMG_0002.AVI']]
    assert not [x for x in lines if ',BSN0002,' in x]                    # 削除の判断は、綴りが違っても行を除く
    assert [x for x in lines if x.startswith('N250103,')]                # ほかの Season の行は残る
    # 先頭だけで照合しない設定では、綴りの違う行はこの Season の行にしない
    store2 = make_store(tmp_path / 'off', t, season_prefix=False)
    scan(store2, S)
    assert tm_core.load_payload(store2, S)['csv']['rows'] == 0


def test_results_scanned_before_prefix_matching_still_work(tmp_path):
    """0.2.0 以前の走査結果（Season フォルダの記録がない）でも、パスの組み立てと書き出しができる。"""
    base = str(tmp_path / 't')
    t = fixture.build(base, real_videos=False)
    SE = fixture.SEASON
    store = make_store(tmp_path, t)
    scan(store, SE)
    p = tm_core.load_payload(store, SE)
    ids = {k: v[0] for k, v in by_name(p).items()}
    tm_core.set_decision(store, SE, [ids[('BSN0001', 'IMG_0007')], ids[('BSN0001', 'IMG_0009')]], 'keep')
    tm_core.set_decision(store, SE, [ids[('BSN0001', 'IMG_0010')]], 'delete')
    p = tm_core.load_payload(store, SE)
    want_detail = tm_core.item_detail(store, SE, ids[('BSN0001', 'IMG_0012')])
    want_plan = _read_csv(tm_core.export_plan(store, SE)['path'])
    want_csv = open(tm_core.export_csv(store, SE, {}, tm_core.Job('x', SE))['path'], 'rb').read()
    # 0.2.0 の形に戻す: ファイルの情報は 4 項目、Season フォルダと照合の方法の記録はない
    con = store.connect(SE)
    for item_id, files in con.execute('SELECT id, files FROM items').fetchall():
        fl = {k: [f[:4] for f in v] for k, v in json.loads(files).items()}
        con.execute('UPDATE items SET files=? WHERE id=?', (json.dumps(fl, ensure_ascii=False, separators=(',', ':')), item_id))
    meta = tm_core.get_meta(con)
    info = {k: v for k, v in meta['info'].items() if k not in ('season_dirs', 'station_dirs', 'station_multi')}
    csv_meta = {k: v for k, v in meta['csv'].items() if k not in ('season_name', 'i_sub', 'station_sub', 'station_season')}
    tm_core.set_meta(con, info=info, csv=csv_meta)
    con.execute("DELETE FROM meta WHERE key IN ('season_prefix', 'inherited_from')")
    con.commit()
    con.close()
    p2 = tm_core.load_payload(store, SE)
    assert p2['season_prefix'] is None and [s['dirs'] for s in p2['sources']] == [[], [], []]
    assert p2['items'] == p['items']
    got = tm_core.item_detail(store, SE, ids[('BSN0001', 'IMG_0012')])
    assert [[(f['path'], f['folder'], f['pick']) for f in s.get('files', [])] for s in got['sources']] == \
           [[(f['path'], f['folder'], f['pick']) for f in s.get('files', [])] for s in want_detail['sources']]
    assert _read_csv(tm_core.export_plan(store, SE)['path']) == want_plan
    assert open(tm_core.export_csv(store, SE, {}, tm_core.Job('x', SE))['path'], 'rb').read() == want_csv
    # 0.2.0 以前の走査結果では、CSV の行は名前の全体で照合する（走査したときと同じ行を対象にする）
    m = tm_core.season_matcher({'season': 'N250102'})
    assert m('N250102') and m('n250102') and not m('N250102_P01')
    m = tm_core.season_matcher({'season': 'N250102', 'season_prefix': True})
    assert m('N250102') and m('N250102_P01') and not m('N250103')


def test_settings_for_season_prefix(tmp_path):
    n = tm_core.Store(str(tmp_path / 'd'))
    assert n.config()['settings']['season_prefix'] is True
    cfg = n.config()
    cfg['settings']['season_prefix'] = 'no'                     # 真偽値でない指定は無視する
    assert n.save(cfg)['settings']['season_prefix'] is True
    cfg['settings']['season_prefix'] = False
    assert n.save(cfg)['settings']['season_prefix'] is False
    # 0.2.0 以前に保存された設定（season_prefix の項目がない）の Season_sub_folder={Season} は、{Folder} に読み替える
    old = {'sources': [], 'settings': {'add_defaults': {'Season_sub_folder': '{Season}', 'Species': 'Add'}}}
    assert tm_core.Store.normalize(old)['settings']['add_defaults'] == {'Season_sub_folder': '{Folder}', 'Species': 'Add'}
    # 0.3.0 で保存した設定では、書かれたとおりに使う
    new = {'sources': [], 'settings': {'season_prefix': True, 'add_defaults': {'Season_sub_folder': '{Season}'}}}
    assert tm_core.Store.normalize(new)['settings']['add_defaults'] == {'Season_sub_folder': '{Season}'}
    assert tm_core.ADD_ROW_DEFAULTS['Season_sub_folder'] == '{Folder}'


def test_read_and_write_with_match(tmp_path):
    src = str(tmp_path / 'a.csv')
    with open(src, 'w', encoding='cp932', newline='') as f:
        f.write('Season,Station,File,Note\nN250102_P01,BSN1,IMG_1.AVI,あ\nN250102_P02,BSN2,IMG_1.AVI,い\n'
                'N250103,BSN1,IMG_1.AVI,う\nn250102,BSN3,IMG_1.AVI,え\n')
    is_s = lambda v: tm_core.season_key(v).casefold() == 'n250102'      # noqa: E731
    data = tm_csv.read_season(src, 'N250102', match=is_s)
    assert [r[1] for r in data['rows']] == ['BSN1', 'BSN2', 'BSN3']
    assert [r[1] for r in tm_csv.read_season(src, 'N250102')['rows']] == ['BSN3']       # 既定は名前の全体
