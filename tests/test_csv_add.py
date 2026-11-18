# -*- coding: utf-8 -*-
"""「CSV に行を追加する」: 残すと判断した動画のうち、NAS にあって CSV にないものの行を、元の CSV に書き足す。"""
import json
import os
import shutil
import sys
from datetime import datetime

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixture  # noqa: E402
import tm_core  # noqa: E402
import tm_csv  # noqa: E402
import tm_media  # noqa: E402
from test_core import REAL_DEFAULT_TAIL, REAL_HEADER, build_browning_like, by_name, make_store, scan  # noqa: E402

S = 'N180812'
ST = 'BSN1053'


def build(base):
    """実データに似せた保存場所に、CSV にない動画を足す。

    IMG_0031, IMG_0032: HDD と NAS にある      IMG_0033: HDD にだけある
    IMG_0034, IMG_0035: NAS にだけある
    """
    t = build_browning_like(base)
    # 実際の CSV と同じく、日本語を含む Shift_JIS の CSV にする（英数字だけだと UTF-8 とみなされる）
    text = open(t['csv'], encoding='cp932', newline='').read().replace(',Empty\n', ',シカ\n', 1)
    with open(t['csv'], 'w', encoding='cp932', newline='') as f:
        f.write(text)
    data = open(os.path.join(t['nas'], S, ST, 'IMG_0001.AVI'), 'rb').read()
    ts = t['times']['IMG_0032.AVI']
    for k, (root, name) in enumerate(((t['hdd'], 'IMG_0033.AVI'), (t['nas'], 'IMG_0034.AVI'), (t['nas'], 'IMG_0035.AVI'))):
        p = os.path.join(root, S, ST, name)
        fixture.put(p, data)
        t['times'][name] = ts + 200 * (k + 1)
        os.utime(p, (t['times'][name], t['times'][name]))
    return t


def keep(store, p, *stems):
    tm_core.set_decision(store, S, [by_name(p)[(ST, x)][0] for x in stems], 'keep')


def run(store):
    return tm_core.csv_add_rows(store, S, {}, tm_core.Job('csv_add', S))


def line(t, name, tail=REAL_DEFAULT_TAIL):
    """書き足されるはずの行（DateTime はファイルの更新日時を分に切り捨てた値）。"""
    dt = datetime.fromtimestamp(t['times'][name]).replace(second=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    return '%s,%s,%s,%s,%s,,,,%s' % (S, S, ST, name, dt, tail)


def snapshot(root):
    out = {}
    for d, _dirs, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def ready(tmp_path, **settings):
    t = build(str(tmp_path / 'b'))
    st = {'dt_source': 'embedded_mtime'}
    st.update(settings)
    store = make_store(tmp_path, t, **st)
    scan(store, S)
    return t, store, tm_core.load_payload(store, S)


# ---------------------------------------------------------------- 末尾への書き足し（tm_csv）

@pytest.mark.parametrize('enc,eol,last_eol,quote_all', [
    ('cp932', '\n', True, False), ('cp932', '\n', False, False), ('utf-8-sig', '\r\n', True, False),
    ('utf-8', '\r\n', False, True)])
def test_append_rows_keeps_existing_bytes(tmp_path, enc, eol, last_eol, quote_all):
    p = str(tmp_path / 'a.csv')
    body = eol.join(['Season,Station,File,Note', 'N1,BSN1,IMG_1.AVI,シカ', 'N1,BSN1,IMG_2.AVI,"a,b"']) + (eol if last_eol else '')
    with open(p, 'w', encoding=enc, newline='') as f:
        f.write(body)
    before = open(p, 'rb').read()
    meta = {'encoding': enc, 'delimiter': ',', 'eol': eol, 'quote_all': quote_all}
    st = os.stat(p)
    res = tm_csv.append_rows(p, meta, [['N1', 'BSN1', 'IMG_3.AVI', 'イノシシ, 2 頭'], ['N1', 'BSN2', 'IMG_1.AVI', '']],
                             (st.st_size, int(st.st_mtime)))
    after = open(p, 'rb').read()
    assert after.startswith(before) and res['old_size'] == len(before) and res['new_size'] == len(after) and res['added'] == 2
    want = ('' if last_eol else eol)
    if quote_all:
        want += '"N1","BSN1","IMG_3.AVI","イノシシ, 2 頭"' + eol + '"N1","BSN2","IMG_1.AVI",' + eol
    else:
        want += 'N1,BSN1,IMG_3.AVI,"イノシシ, 2 頭"' + eol + 'N1,BSN2,IMG_1.AVI,' + eol
    assert after[len(before):] == want.encode('utf-8' if enc == 'utf-8-sig' else enc) and res['text'] == want
    assert after.count(b'\xef\xbb\xbf') == (1 if enc == 'utf-8-sig' else 0)          # BOM は先頭の 1 つだけ
    # 読み直すと、足した行がそのまま読める
    data = tm_csv.read_season(p, 'N1')
    assert [r[2] for r in data['rows']] == ['IMG_1.AVI', 'IMG_2.AVI', 'IMG_3.AVI', 'IMG_1.AVI']
    assert data['rows'][2][3] == 'イノシシ, 2 頭'


def test_append_rows_refuses_changed_file_and_restores_on_failure(tmp_path, monkeypatch):
    p = str(tmp_path / 'a.csv')
    open(p, 'wb').write(b'Season,Station,File\nN1,BSN1,IMG_1.AVI\n')
    before = open(p, 'rb').read()
    meta = {'encoding': 'utf-8', 'delimiter': ',', 'eol': '\n', 'quote_all': False}
    st = os.stat(p)
    # 確かめたときと大きさが違う: 書かない
    with pytest.raises(tm_csv.CsvChanged):
        tm_csv.append_rows(p, meta, [['N1', 'BSN1', 'IMG_2.AVI']], (st.st_size + 1, int(st.st_mtime)))
    assert open(p, 'rb').read() == before
    # 足す行がなければ、何もしない
    assert tm_csv.append_rows(p, meta, [])['added'] == 0 and open(p, 'rb').read() == before
    # 書いている途中で失敗した: 書きかけの分を取り除いて、元の長さに戻す
    real = os.fsync

    def broken(fd):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(os, 'fsync', broken)
    with pytest.raises(tm_csv.AppendFailed) as e:
        tm_csv.append_rows(p, meta, [['N1', 'BSN1', 'IMG_2.AVI']])
    assert e.value.restored and open(p, 'rb').read() == before
    monkeypatch.setattr(os, 'fsync', real)
    # 書き足しのあとに、ほかのプログラムが書いた分があるときは、切り詰めない
    assert tm_csv._drop_partial(p, len(before) - 5, b'XXXXXXXXXX') is False and open(p, 'rb').read() == before


# ---------------------------------------------------------------- 実行

def test_rows_are_appended_to_the_original_csv(tmp_path):
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031', 'IMG_0032', 'IMG_0033', 'IMG_0034')
    c = tm_core.csv_add_counts(store, S)
    assert (c['target'], c['elsewhere']) == (3, 1) and [b['name'] for b in c['basis']] == ['NAS']
    assert c['choice'] == 'auto' and [f['name'] for f in c['folders']] == ['HDD', 'NAS']
    assert c['exists'] and not c['changed'] and not c['sources_changed'] and c['path'] == t['csv']
    before = open(t['csv'], 'rb').read()
    folders = (snapshot(t['hdd']), snapshot(t['nas']))
    meta0 = tm_core.get_meta(store.connect(S))
    ids0 = {k: v[0] for k, v in by_name(p).items()}

    res = run(store)
    assert (res['added'], res['targets'], res['elsewhere'], res['changed'], res['unreadable']) == (3, 3, 1, 0, 0)
    assert (res['added_mtime'], res['added_embedded'], res['added_na']) == (3, 0, 0)
    assert res['verified'] == 3 and res['unverified'] == [] and res['refresh_error'] == '' and not res['refreshed']
    assert res['basis'] == ['NAS'] and res['offline'] == [] and res['path'] == t['csv']
    # 元の CSV: すでにあった内容は 1 バイトも変わらず、末尾に 3 行が足されている
    after = open(t['csv'], 'rb').read()
    assert after.startswith(before) and res['old_size'] == len(before) and res['new_size'] == len(after)
    want = ''.join(line(t, n) + '\n' for n in ('IMG_0031.AVI', 'IMG_0032.AVI', 'IMG_0034.AVI'))
    assert after[len(before):].decode('cp932') == want
    # 控え: 書き込む前の CSV と同じ内容。作業用フォルダの backups に作る
    assert res['backup_new'] and os.path.dirname(res['backup']) == os.path.join(store.data_dir, 'backups')
    assert open(res['backup'], 'rb').read() == before
    # 足した行の記録（見出しと、足した行）
    assert open(res['record'], encoding='cp932', newline='').read() == REAL_HEADER + '\n' + want
    # 操作の記録
    ops = store.operations()
    assert len(ops) == 1 and ops[0]['op'] == 'csv_add' and ops[0]['season'] == S and ops[0]['rows'] == 3
    assert (ops[0]['old_size'], ops[0]['new_size'], ops[0]['csv']) == (len(before), len(after), t['csv'])
    assert ops[0]['backup'] == res['backup'] and ops[0]['record'] == res['record'] and ops[0]['version'] == tm_core.VERSION
    # 動画のファイルには触れない
    assert (snapshot(t['hdd']), snapshot(t['nas'])) == folders

    # 作業状態: 足した動画に CSV の行が付き、判断と動画の番号はそのまま
    p2 = tm_core.load_payload(store, S)
    items = by_name(p2)
    assert {k: v[0] for k, v in items.items()} == ids0
    assert items[(ST, 'IMG_0031')][3] == 7 and items[(ST, 'IMG_0031')][4] == 0 and items[(ST, 'IMG_0031')][5] == 'k'
    assert items[(ST, 'IMG_0034')][3] == 6 and items[(ST, 'IMG_0034')][7][2] == ['AVI', 1]
    assert items[(ST, 'IMG_0033')][3] == 1 and items[(ST, 'IMG_0033')][5] == 'k'          # HDD にしかない: 足さない
    assert items[(ST, 'IMG_0035')][3] == 2 and items[(ST, 'IMG_0035')][5] == ''           # 判断していない: 足さない
    meta1 = tm_core.get_meta(store.connect(S))
    assert meta1['scanned_at'] == meta0['scanned_at'] and meta1['csv_read_at'] and meta1['season'] == S
    assert meta1['csv']['size'] == len(after) and meta1['csv']['rows'] == meta0['csv']['rows'] + 3
    assert meta1['sources'] == meta0['sources'] and meta1['info']['files']['csv'] == meta0['csv']['rows'] + 3
    c = tm_core.csv_add_counts(store, S)
    assert (c['target'], c['elsewhere'], c['changed']) == (0, 1, False)

    # もう一度実行しても、同じ行は足さない。CSV も控えも増えない
    res2 = run(store)
    assert res2['added'] == 0 and res2['targets'] == 0 and res2['backup'] == '' and res2['record'] == ''
    assert open(t['csv'], 'rb').read() == after and len(store.operations()) == 1
    assert len(os.listdir(os.path.join(store.data_dir, 'backups'))) == 1

    # CSV だけを読み直した結果は、走査をやり直した結果と同じ
    scan(store, S)
    p3 = tm_core.load_payload(store, S)
    assert p3['items'] == p2['items'] and p3['stations'] == p2['stations']
    m3 = tm_core.get_meta(store.connect(S))
    assert m3['csv'] == meta1['csv'] and m3['info']['files'] == meta1['info']['files']
    # 更新後の CSV の書き出しも、そのまま続けて使える（行を足す対象は、HDD にしかない 1 本だけになる）
    out = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert out['added'] == 1 and not out['refreshed']


def test_export_rereads_a_csv_changed_since_the_scan(tmp_path):
    """ほかの Season の行が書き足されたあとでも、走査をやり直さずに更新後の CSV を書き出せる。"""
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031')
    other = 'N999999,N999999,BSN0001,IMG_0001.AVI,2020-01-01T00:00:00Z,,,,%s\n' % REAL_DEFAULT_TAIL
    with open(t['csv'], 'ab') as f:
        f.write((other + line(t, 'IMG_0032.AVI')).encode('cp932'))             # 最後の行に改行がない
    before = open(t['csv'], 'rb').read()
    out = tm_core.export_csv(store, S, {}, tm_core.Job('x', S))
    assert out['refreshed'] and out['added'] == 1
    text = open(out['path'], encoding='cp932', newline='').read()
    assert text.endswith(other + line(t, 'IMG_0032.AVI') + '\n' + line(t, 'IMG_0031.AVI') + '\n')
    assert open(t['csv'], 'rb').read() == before                                 # 別名での書き出しは、元の CSV を変えない
    # 元の CSV への書き足しでも、改行のない最後の行につながらない
    res = run(store)
    assert res['added'] == 1 and not res['refreshed']
    assert open(t['csv'], 'rb').read() == before + ('\n' + line(t, 'IMG_0031.AVI') + '\n').encode('cp932')


def test_backup_is_made_once_a_day_unless_someone_else_changed_the_csv(tmp_path):
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031')
    r1 = run(store)
    assert r1['added'] == 1 and r1['backup_new']
    # 同じ日の 2 回目: CSV を変えたのは前回の書き足しだけなので、控えは前回のものを使う
    keep(store, p, 'IMG_0034')
    r2 = run(store)
    assert r2['added'] == 1 and not r2['backup_new'] and r2['backup'] == r1['backup'] and not r2['refreshed']
    assert len(os.listdir(os.path.join(store.data_dir, 'backups'))) == 1
    # ほかのプログラムが CSV を変えたあとは、読み直してから対象を決め、新しい控えを作る
    with open(t['csv'], 'ab') as f:
        f.write(('N999999,N999999,BSN0001,IMG_0001.AVI,2020-01-01T00:00:00Z,,,,%s\n' % REAL_DEFAULT_TAIL).encode('cp932'))
    os.utime(t['csv'], (os.stat(t['csv']).st_atime, os.stat(t['csv']).st_mtime + 5))
    mid = open(t['csv'], 'rb').read()
    assert tm_core.csv_add_counts(store, S)['changed']
    keep(store, p, 'IMG_0035')
    r3 = run(store)
    assert r3['added'] == 1 and r3['refreshed'] and r3['backup_new'] and r3['backup'] != r1['backup']
    assert open(r3['backup'], 'rb').read() == mid and open(t['csv'], 'rb').read().startswith(mid)
    assert [o['rows'] for o in store.operations()] == [1, 1, 1]
    # 日付が変わっていれば、TrapMatch の書き足しだけでも新しい控えを作る
    ops = store.operations()
    ops[-1]['backup_day'] = '2000-01-01'
    with open(store.operations_path(), 'w', encoding='utf-8') as f:
        f.write(''.join(json.dumps(o, ensure_ascii=False) + '\n' for o in ops))
    keep(store, p, 'IMG_0032')
    r4 = run(store)
    assert r4['added'] == 1 and r4['backup_new'] and len(os.listdir(os.path.join(store.data_dir, 'backups'))) == 3


def test_rows_already_in_the_csv_are_not_added_twice(tmp_path):
    """走査のあとに、同じ動画の行がほかの方法で CSV に足されていたら、その動画は対象から外れる。"""
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031', 'IMG_0032')
    with open(t['csv'], 'ab') as f:
        f.write((line(t, 'IMG_0031.AVI', REAL_DEFAULT_TAIL.replace('Add', 'Deer')) + '\n').encode('cp932'))
    res = run(store)
    assert res['refreshed'] and (res['targets'], res['added'], res['verified']) == (1, 1, 1)
    text = open(t['csv'], encoding='cp932', newline='').read()
    assert text.count(',IMG_0031.AVI,') == 1 and text.count(',IMG_0032.AVI,') == 1
    assert text.endswith(line(t, 'IMG_0032.AVI') + '\n')


def test_videos_that_changed_or_cannot_be_read_are_skipped(tmp_path, monkeypatch):
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031', 'IMG_0032', 'IMG_0034', 'IMG_0035')
    before = open(t['csv'], 'rb').read()
    os.remove(os.path.join(t['nas'], S, ST, 'IMG_0031.AVI'))                    # 走査のあとに NAS からなくなった
    with open(os.path.join(t['nas'], S, ST, 'IMG_0032.AVI'), 'ab') as f:        # 走査のあとにサイズが変わった
        f.write(b'x')
    real = tm_media.embedded_datetime

    def flaky(path):
        if path.endswith('IMG_0034.AVI'):
            raise OSError(5, 'network path not found')
        return real(path)

    monkeypatch.setattr(tm_media, 'embedded_datetime', flaky)
    res = run(store)
    assert (res['targets'], res['added'], res['changed'], res['unreadable'], res['verified']) == (4, 1, 2, 1, 1)
    assert open(t['csv'], 'rb').read() == before + (line(t, 'IMG_0035.AVI') + '\n').encode('cp932')
    # 読めるようになれば、次の実行で足される（サイズが変わった動画は、走査をやり直すまで足さない）
    monkeypatch.setattr(tm_media, 'embedded_datetime', real)
    res = run(store)
    assert (res['targets'], res['added'], res['changed'], res['unreadable']) == (3, 1, 2, 0)
    assert open(t['csv'], encoding='cp932', newline='').read().endswith(line(t, 'IMG_0034.AVI') + '\n')
    # 行ごとに DateTime を入力してあれば、動画を読めなくても足せる
    scan(store, S)
    p = tm_core.load_payload(store, S)
    i32 = by_name(p)[(ST, 'IMG_0032')][0]
    tm_core.set_field(store, S, i32, 'csv_fields', {'DateTime': '2018-06-11T07:00:00Z', 'Species': 'Deer'})
    monkeypatch.setattr(tm_media, 'embedded_datetime', lambda path: (_ for _ in ()).throw(OSError(5, 'x')))
    res = run(store)
    assert (res['targets'], res['added'], res['unreadable']) == (1, 1, 0)
    assert open(t['csv'], encoding='cp932', newline='').read().endswith(
        '%s,%s,%s,IMG_0032.AVI,2018-06-11T07:00:00Z,,,,%s\n' % (S, S, ST, REAL_DEFAULT_TAIL.replace('Add', 'Deer')))


def test_works_with_the_hdd_disconnected(tmp_path):
    """HDD を外していても、NAS に同じ動画（同じ拡張子、同じサイズ）があれば足せる。"""
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031', 'IMG_0033')
    shutil.move(t['hdd'], t['hdd'] + '_unplugged')
    res = run(store)
    assert res['offline'] == ['HDD'] and (res['targets'], res['added'], res['unreadable'], res['elsewhere']) == (1, 1, 0, 1)
    assert open(t['csv'], encoding='cp932', newline='').read().endswith(line(t, 'IMG_0031.AVI') + '\n')


def test_embedded_datetime_with_an_offline_copy(tmp_path):
    t, store, p = ready(tmp_path)
    con = store.connect(S)
    meta = tm_core.get_meta(con)
    it = tm_core._row_dict(con, by_name(p)[(ST, 'IMG_0031')][0])
    # 同じ内容の控えを NAS で読めた: 「日時なし」と決める
    tm_core.ensure_embedded(con, meta, it, offline={'hdd'})
    assert it['dt_emb'] == 'NA' and not it['dt_unreadable']
    # NAS の動画が HDD のものとサイズ違い: HDD を読めないあいだは決めない
    it = tm_core._row_dict(con, by_name(p)[(ST, 'IMG_0032')][0])
    it['files']['nas'][0][1] += 1
    tm_core.ensure_embedded(con, meta, it, offline={'hdd'})
    assert it['dt_emb'] is None and it['dt_unreadable']
    con.close()


def test_refusals_leave_the_csv_untouched(tmp_path):
    t, store, p = ready(tmp_path)
    keep(store, p, 'IMG_0031')
    before = open(t['csv'], 'rb').read()
    i31 = by_name(p)[(ST, 'IMG_0031')][0]

    def refused(text):
        with pytest.raises(ValueError) as e:
            run(store)
        assert text in str(e.value), str(e.value)
        assert open(t['csv'], 'rb').read() == before and not os.path.isdir(os.path.join(store.data_dir, 'backups'))
        assert store.operations() == []

    # CSV の文字コードで表せない文字
    tm_core.set_field(store, S, i31, 'csv_fields', {'Remarks': '\U0001F98C'})
    refused('表せない文字')
    tm_core.set_field(store, S, i31, 'csv_fields', {})
    # NAS に接続できない
    shutil.move(t['nas'], t['nas'] + '_off')
    refused('接続できません')
    shutil.move(t['nas'] + '_off', t['nas'])
    # 保存場所の登録が走査のあとに変わった
    cfg = store.config()
    saved = json.loads(json.dumps(cfg))
    cfg['sources'][0]['enabled'] = False
    store.save(cfg)
    assert tm_core.csv_add_counts(store, S)['sources_changed']
    refused('走査')
    store.save(saved)
    # CSV が見つからない
    shutil.move(t['csv'], t['csv'] + '.away')
    assert not tm_core.csv_add_counts(store, S)['exists']
    with pytest.raises(ValueError) as e:
        run(store)
    assert '見つかりません' in str(e.value)
    shutil.move(t['csv'] + '.away', t['csv'])
    assert open(t['csv'], 'rb').read() == before
    assert run(store)['added'] == 1


def test_basis_source_setting(tmp_path):
    t = build(str(tmp_path / 'b'))
    store = tm_core.Store(str(tmp_path / 'data'))
    # 名前に NAS を含む保存場所がない
    cfg = store.save({'sources': [
        {'id': 'a', 'name': '外付け', 'type': 'folder', 'path': t['hdd']},
        {'id': 'b', 'name': 'サーバー', 'type': 'folder', 'path': t['nas']},
        {'id': 'c', 'name': 'CSV', 'type': 'csv', 'path': t['csv']},
    ], 'settings': {'exts': ['avi'], 'dt_source': 'embedded_mtime', 'csv_add_source': 'nowhere'}})
    assert cfg['settings']['csv_add_source'] == 'auto'            # 登録にない保存場所の指定は無視する
    scan(store, S)
    p = tm_core.load_payload(store, S)
    keep(store, p, 'IMG_0031', 'IMG_0033', 'IMG_0034')
    c = tm_core.csv_add_counts(store, S)
    assert c['basis'] == [] and c['target'] == 0
    with pytest.raises(ValueError) as e:
        run(store)
    assert '選んでください' in str(e.value)
    # 保存場所を選ぶ
    cfg['settings']['csv_add_source'] = 'b'
    assert store.save(cfg)['settings']['csv_add_source'] == 'b'
    c = tm_core.csv_add_counts(store, S)
    assert [x['name'] for x in c['basis']] == ['サーバー'] and (c['target'], c['elsewhere'], c['choice']) == (2, 1, 'b')
    cfg['settings']['csv_add_source'] = 'c'                       # CSV は選べない
    assert store.save(cfg)['settings']['csv_add_source'] == 'auto'
    cfg['settings']['csv_add_source'] = 'a'
    store.save(cfg)
    c = tm_core.csv_add_counts(store, S)
    assert [x['name'] for x in c['basis']] == ['外付け'] and (c['target'], c['elsewhere']) == (2, 1)
    res = run(store)
    assert res['added'] == 2 and res['basis'] == ['外付け']
    text = open(t['csv'], encoding='cp932', newline='').read()
    assert text.endswith(line(t, 'IMG_0031.AVI') + '\n' + line(t, 'IMG_0033.AVI') + '\n')


def test_csv_only_reread_matches_a_full_scan(tmp_path):
    """CSV だけの読み直しは、フォルダを読まずに、走査をやり直したときと同じ結果になる。"""
    base = str(tmp_path / 't')
    t = fixture.build(base, real_videos=False)
    SE = fixture.SEASON
    store = make_store(tmp_path, t)
    scan(store, SE)
    p0 = tm_core.load_payload(store, SE)
    ids = {k: v[0] for k, v in by_name(p0).items()}
    tm_core.set_decision(store, SE, [ids[('BSN0001', 'IMG_0010')]], 'keep')         # CSV にしかない動画
    tm_core.set_field(store, SE, ids[('BSN0001', 'IMG_0007')], 'memo', 'メモ')
    p0 = tm_core.load_payload(store, SE)
    m0 = tm_core.get_meta(store.connect(SE))
    # 何も変わっていなければ、読み直しても同じ
    tm_core.run_scan(store, SE, tm_core.Job('scan', SE), csv_only=True)
    p1 = tm_core.load_payload(store, SE)
    assert p1['items'] == p0['items'] and p1['scanned_at'] == p0['scanned_at'] and p1['info'] == p0['info']
    # CSV を変える: 行を足す（CSV になかった動画）、行を除く（CSV にしかなかった動画）
    text = open(t['csv'], encoding='utf-8-sig', newline='').read()
    text = text.replace('%s,BSN0001,IMG_0010.AVI,2026/01/05 06:12:00,empty,"line one\nline two"\r\n' % SE, '')
    text += '%s,BSN0001,IMG_0009.MP4,2026/01/05 06:20:00,deer,\r\n' % SE
    with open(t['csv'], 'w', encoding='utf-8-sig', newline='') as f:
        f.write(text)
    shutil.move(t['hdd'], t['hdd'] + '_off')                                         # フォルダは読まない
    tm_core.run_scan(store, SE, tm_core.Job('scan', SE), csv_only=True)
    shutil.move(t['hdd'] + '_off', t['hdd'])
    p2 = tm_core.load_payload(store, SE)
    a = by_name(p2)
    assert ('BSN0001', 'IMG_0010') not in a and p2['gone'] == 1                      # 判断のあった動画は、記録に残る
    assert a[('BSN0001', 'IMG_0009')][3] == 7 and a[('BSN0001', 'IMG_0009')][0] == ids[('BSN0001', 'IMG_0009')]
    assert a[('BSN0001', 'IMG_0007')][6] == 'メモ'
    m2 = tm_core.get_meta(store.connect(SE))
    assert m2['scanned_at'] == m0['scanned_at'] and m2['csv_read_at'] and m2['version'] == m0['version']
    assert m2['season_prefix'] is True and m2['sources'] == m0['sources']
    # 走査をやり直した結果と同じ
    scan(store, SE)
    p3 = tm_core.load_payload(store, SE)
    assert p3['items'] == p2['items'] and p3['info'] == p2['info'] and p3['csv'] == p2['csv']
    assert tm_core.get_meta(store.connect(SE))['csv_read_at'] == ''


def test_split_season_folders_and_results_of_older_versions(tmp_path):
    """Season フォルダが分かれている Season と、0.2.0 以前の走査結果（Station ごとの記録がない）での書き足し。"""
    from test_season_prefix import build_split
    t = build_split(str(tmp_path / 't'))
    SE = t['season']
    store = make_store(tmp_path, t)
    scan(store, SE)
    p = tm_core.load_payload(store, SE)
    items = by_name(p)
    tm_core.set_decision(store, SE, [items[k][0] for k in (('BSN0003', 'IMG_0002'), ('BSN0004', 'IMG_0001'),
                                                           ('BSN0005', 'IMG_0001'))], 'keep')
    # 0.2.0 以前の走査結果の形にする（CSV から読んだ、Station ごとの Season_sub_folder などの記録がない）
    con = store.connect(SE)
    meta = tm_core.get_meta(con)
    tm_core.set_meta(con, csv={k: v for k, v in meta['csv'].items()
                               if k not in ('season_name', 'i_sub', 'station_sub', 'station_season')})
    con.commit()
    con.close()
    before = open(t['csv'], 'rb').read()
    res = tm_core.csv_add_rows(store, SE, {}, tm_core.Job('csv_add', SE))
    assert res['refreshed'] and (res['targets'], res['added'], res['elsewhere'], res['verified']) == (2, 2, 1, 2)
    after = open(t['csv'], 'rb').read()
    assert after.startswith(before)
    tail = after[len(before):].decode('cp932').split('\n')
    # Season_sub_folder は、同じ Station の行が使っている値。行のない Station は、動画が入っているフォルダの名前
    assert [x.split(',')[:5] for x in tail[:-1]] == [
        ['N250102', 'N250102_P02', 'BSN0003', 'IMG_0002.AVI', 'NA'],
        ['N250102', 'N250102', 'BSN0005', 'IMG_0001.AVI', 'NA']]
    assert all(x.endswith(REAL_DEFAULT_TAIL) for x in tail[:-1]) and tail[-1] == ''
