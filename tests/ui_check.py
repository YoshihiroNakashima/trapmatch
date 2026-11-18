# -*- coding: utf-8 -*-
"""画面の動作確認（Playwright が必要）。python tests/ui_check.py [出力フォルダ]

試験用の保存場所を作ってサーバーを起動し、ブラウザで一通りの操作を行う。
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fixture  # noqa: E402
from test_http import Client, free_port  # noqa: E402


def extend(tree, stations=40, per=30):
    """一覧のスクロールを試すため、ダミーの動画を足す。"""
    rnd = random.Random(1)
    lines = []
    for s in range(10, 10 + stations):
        st = 'BSN%04d' % s
        for i in range(1, per + 1):
            name = 'IMG_%04d.AVI' % i
            r = rnd.random()
            in_h = r > 0.04
            in_n = r < 0.93
            in_c = rnd.random() > 0.05
            if in_h:
                fixture.put(os.path.join(tree['hdd'], tree['season'], st, name), b'a' * 2000)
            if in_n:
                fixture.put(os.path.join(tree['nas'], tree['season'], st, name), b'a' * 2000)
            if in_c:
                lines.append('%s,%s,%s,2026/01/08 10:00:%02d,deer,' % (tree['season'], st, name, i))
    with open(tree['csv'], 'a', encoding='utf-8', newline='') as f:
        f.write('\r\n'.join(lines) + '\r\n')


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), 'tm_ui')
    os.makedirs(out, exist_ok=True)
    base = tempfile.mkdtemp(prefix='tm_ui_')
    tree = fixture.build(os.path.join(base, 'tree'))
    extend(tree)
    port = free_port()
    env = dict(os.environ, TRAPMATCH_NO_OPEN='1', PYTHONIOENCODING='utf-8')
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py'), '--port', str(port),
                             '--data-dir', os.path.join(base, 'data'), '--no-browser', '--allow-quit'], env=env)
    c = Client(port)
    for _ in range(100):
        try:
            c.get('/api/ping')
            break
        except OSError:
            time.sleep(0.05)
    url = 'http://127.0.0.1:%d/' % port
    problems = []
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={'width': 1500, 'height': 900})
            logs = []
            page.on('console', lambda m: logs.append('%s: %s' % (m.type, m.text)) if m.type in ('error', 'warning') else None)
            page.on('pageerror', lambda e: logs.append('pageerror: %s' % e))

            def check(cond, label):
                print(('ok   ' if cond else 'FAIL ') + label)
                if not cond:
                    problems.append(label)

            # 1. はじめての起動: 設定の窓が開く
            page.goto(url)
            page.wait_for_selector('#modalBox h2')
            check('保存場所と設定' in page.inner_text('#modalBox h2'), '初回は設定の窓が開く')
            page.click('#modalBox [data-act="addfolder"]')
            page.click('#modalBox [data-act="addfolder"]')
            page.click('#modalBox [data-act="addcsv"]')
            rows = page.query_selector_all('#modalBox tr[data-i]')
            check(len(rows) == 3, '保存場所を 3 つ追加できる')
            # フォルダを選ぶ窓
            page.click('#modalBox tr[data-i="0"] [data-act="browse"]')
            page.wait_for_selector('#brPath')
            page.fill('#brPath', tree['hdd'])
            page.press('#brPath', 'Enter')
            page.wait_for_function("document.querySelector('#brPath') && document.querySelector('#brPath').value.endsWith('PRM_N')")
            page.screenshot(path=os.path.join(out, '01_browse.png'))
            page.click('#modalBox button.primary[data-act="pick"]')
            page.wait_for_selector('#modalBox tr[data-i="0"]')
            check(page.input_value('#modalBox tr[data-i="0"] [data-k="path"]') == tree['hdd'], 'フォルダを選ぶと場所が入る')
            page.fill('#modalBox tr[data-i="1"] [data-k="path"]', tree['nas'])
            page.click('#modalBox tr[data-i="2"] [data-act="browse"]')
            page.wait_for_selector('#brPath')
            page.fill('#brPath', os.path.dirname(tree['csv']))
            page.press('#brPath', 'Enter')
            page.wait_for_selector('.blist button.file')
            page.click('.blist button.file')
            page.wait_for_selector('#modalBox tr[data-i="2"]')
            check(page.input_value('#modalBox tr[data-i="2"] [data-k="path"]') == tree['csv'], 'CSV を選ぶと場所が入る')
            page.fill('#setExts', 'avi, mp4, mov, webm')
            page.screenshot(path=os.path.join(out, '02_settings.png'))
            page.click('#modalBox [data-act="save"]')
            page.wait_for_selector('#modal', state='hidden')
            st, s = c.get('/api/state')
            check([x['name'] for x in s['config']['sources']] == ['HDD', 'NAS', 'CSV'], '設定が保存される')

            # 2. 走査
            page.fill('#season', tree['season'])
            page.click('#btnScan')
            page.wait_for_selector('.row', timeout=20000)
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(out, '03_main.png'))
            n_all = page.evaluate('TM.S.rows.length')
            n_view = page.evaluate('TM.S.nreal')
            check(n_all == 19 + 40 * 30 - page.evaluate("TM.S.rows.length") + n_all, '走査後に一覧が出る（%d 本）' % n_all)
            check(page.is_checked('#fMismatch'), '走査の直後は「不一致のみ」')
            needs = page.evaluate('TM.S.rows.filter(r => r.flags & 15).length')
            check(n_view == needs and 0 < n_view < n_all, '不一致のみが表示される（%d / %d）' % (n_view, n_all))
            check(page.evaluate("document.querySelectorAll('#rows .row').length") < 80, '一覧は見える分だけ描画する')

            # 2a. 前後の動画（一つ前、一つ後）
            ctx_of = ("TM.S.rows.filter(r => TM.S.ctx.has(r.id) && TM.S.data.stations[r.st] === 'BSN0001')"
                      ".map(r => r.stem + ':' + TM.S.ctx.get(r.id))")
            check(page.is_checked('#fContext'), '「前後の動画も表示」は既定で入っている')
            got = page.evaluate(ctx_of)
            check(got == ['IMG_0006:1', 'IMG_0013:3', 'IMG_0015:2'],
                  '連続する不一致の一つ前と一つ後だけが足される（%s）' % got)
            n_ctx = page.evaluate('TM.S.ctx.size')
            check(page.evaluate('TM.S.view.length') == n_view + n_ctx and n_ctx > 0, '前後の動画は本数に数えない（＋%d 本）' % n_ctx)
            check('＋前後の動画 %d 本' % n_ctx in page.inner_text('#viewCount'), '一覧の上に、足した前後の動画の本数が出る')
            first = page.evaluate("TM.S.rows[TM.S.view[TM.S.cur]].stem + ':' + (TM.S.ctx.has(TM.S.rows[TM.S.view[TM.S.cur]].id) ? 'ctx' : 'real')")
            check(first.endswith(':real'), '最初に選ばれるのは前後の動画ではない（%s）' % first)
            page.click('#stationList .st >> text=BSN0001')
            page.wait_for_timeout(200)
            labels = page.evaluate("Array.from(document.querySelectorAll('#rows .row.ctx')).map(e => e.querySelector('.fn').textContent + ':' + Array.from(e.querySelectorAll('.tag.ctx')).map(t => t.textContent).join('+'))")
            check(labels == ['IMG_0006:一つ前', 'IMG_0013:一つ後+一つ前', 'IMG_0015:一つ後'], '状態の欄に「一つ前」「一つ後」と出る（%s）' % labels)
            page.screenshot(path=os.path.join(out, '03a_context.png'))
            # 前後の動画は再生できるが、判断は変えられない
            page.click('.row.ctx >> text=IMG_0006')
            page.wait_for_function("TM.S.detail && TM.S.detail.stem === 'IMG_0006' && !document.getElementById('detail').classList.contains('stale')")
            page.wait_for_timeout(600)
            check(page.evaluate("!document.getElementById('ctxNote').hidden") and '一つ前にある動画' in page.inner_text('#ctxNote'),
                  '前後の動画を選ぶと、確認用であることが出る')
            check(page.evaluate("Array.from(document.querySelectorAll('#detail .decide button')).every(b => b.disabled) && document.getElementById('memo').disabled"),
                  '前後の動画では、判断のボタンとメモが押せない')
            check(page.evaluate("!!TM.S.detail.sources.find(x => x.type === 'folder' && x.files.length)") and
                  page.evaluate("!document.getElementById('cv').hidden || !document.getElementById('vid').hidden"),
                  '前後の動画も再生の画面に出る')
            page.keyboard.press('k')
            page.wait_for_timeout(400)
            check(page.evaluate("TM.S.rows.filter(r => r.dec).length") == 0 and
                  not [a for a in c.get('/api/items?season=N260101')[1]['items'] if a[5]], '前後の動画には、キーでも判断が付かない')
            page.screenshot(path=os.path.join(out, '03b_context_locked.png'))
            # まとめての判断は、前後の動画を除いた本数になる
            n_st = page.evaluate('TM.S.nreal')
            page.click('#toolbar [data-bulk="e"]')
            page.wait_for_selector('#modalBox')
            msg = page.inner_text('#modalBox')
            check('表示中の %d 本すべて' % n_st in msg and n_st == 7, 'まとめての判断は前後の動画を除く（%s）' % msg.split('\n')[0])
            page.keyboard.press('Escape')
            page.wait_for_selector('#modal', state='hidden')
            # 動画を選び直すと、判断できる状態に戻る
            page.click('.row >> text=IMG_0007')
            page.wait_for_function("TM.S.detail && TM.S.detail.stem === 'IMG_0007' && !document.getElementById('detail').classList.contains('stale')")
            check(page.evaluate("document.getElementById('ctxNote').hidden && Array.from(document.querySelectorAll('#detail .decide button')).every(b => !b.disabled)"),
                  '不一致の動画では、判断のボタンが押せる')
            # 検索した動画の前後も出る
            page.fill('#fSearch', 'IMG_0010')
            page.wait_for_timeout(300)
            got = page.evaluate(ctx_of)
            check(got == ['IMG_0009:1', 'IMG_0011:2'], '検索で絞っても前後の動画が出る（%s）' % got)
            page.fill('#fSearch', '')
            page.wait_for_timeout(300)
            page.click('#stationList .st >> text=すべての Station')
            # 以降の確認は、前後の動画を出さない状態で行う
            page.click('#fContext')
            page.wait_for_timeout(200)
            check(page.evaluate('TM.S.ctx.size') == 0 and page.evaluate('TM.S.view.length') == n_view,
                  '「前後の動画も表示」を外すと、絞り込んだ動画だけになる')
            check(page.evaluate("localStorage.getItem('tm.ctx')") == '0', '「前後の動画も表示」の設定を覚える')

            # 3. 絞り込み
            t0 = time.time()
            page.click('#pairs .chip >> nth=0')
            page.wait_for_timeout(50)
            pair = page.evaluate('TM.S.view.length')
            exp = page.evaluate("TM.S.rows.filter(r => (!r.cells[0] !== !r.cells[1]) || (r.cells[0] && r.cells[1] && r.cells[0][0].toLowerCase() === r.cells[1][0].toLowerCase() && r.cells[0][1] !== r.cells[1][1])).length")
            check(pair == exp and pair > 0, '「HDD と NAS」の組で絞れる（%d 本）' % pair)
            page.screenshot(path=os.path.join(out, '04_pair.png'))
            page.click('#pairs .chip >> nth=0')
            ms = page.evaluate("(() => { const t = performance.now(); for (let i = 0; i < 20; i++) { TM.S.filt.mismatch = !TM.S.filt.mismatch; document.getElementById('fMismatch').click(); } return (performance.now() - t) / 20; })()")
            check(ms < 200, '絞り込みの切り替えが速い（1 回 %.1f ms）' % ms)
            if not page.is_checked('#fMismatch'):
                page.click('#fMismatch')
            page.fill('#fSearch', 'IMG_0011')
            page.wait_for_timeout(250)
            check(page.evaluate('TM.S.view.length') >= 1, 'ファイル名で検索できる')
            page.fill('#fSearch', '')
            page.wait_for_timeout(250)

            # 4. AVI の再生（コマ表示）と判断
            page.click('#stationList .st >> text=BSN0001')
            page.wait_for_timeout(200)
            page.click('.row >> text=IMG_0007')
            page.wait_for_selector('#cv:not([hidden])', timeout=5000)
            page.wait_for_timeout(900)
            pos = page.evaluate('TM.MJ.shown')
            lit = page.evaluate("(() => { const cv = document.getElementById('cv'); const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; let s = 0; for (let i = 0; i < d.length; i += 97) s += d[i]; return s; })()")
            check(pos > 3 and lit > 0, 'AVI がアプリ内で再生される（%d コマ目まで進んだ）' % pos)
            page.screenshot(path=os.path.join(out, '05_play_avi.png'))
            page.keyboard.press('Space')
            page.wait_for_timeout(150)
            a = page.evaluate('TM.MJ.playing')
            check(a is False, 'スペースで停止する')
            page.keyboard.press('k')
            page.wait_for_timeout(400)
            st, items = c.get('/api/items?season=' + tree['season'])
            dec = {(items['stations'][x[1]], x[2]): x[5] for x in items['items']}
            check(dec[('BSN0001', 'IMG_0007')] == 'k', 'K で「残す」が保存される')
            cur = page.evaluate("TM.S.rows[TM.S.view[TM.S.cur]].stem")
            check(cur == 'IMG_0008', '判断のあと次の未判断へ進む（%s）' % cur)
            page.wait_for_selector('#vid:not([hidden])', timeout=5000)
            page.keyboard.press('d')
            page.wait_for_timeout(300)
            page.click('#btnUndo')
            page.wait_for_timeout(300)
            st, items = c.get('/api/items?season=' + tree['season'])
            dec = {(items['stations'][x[1]], x[2]): x[5] for x in items['items']}
            check(dec[('BSN0001', 'IMG_0008')] == '', '「元に戻す」で判断が戻る')
            # メモ
            page.fill('#memo', '空撃ち。削除でよい')
            page.wait_for_timeout(800)
            st, items = c.get('/api/items?season=' + tree['season'])
            memos = [x[6] for x in items['items'] if x[6]]
            check(memos == ['空撃ち。削除でよい'], 'メモが保存される')

            # 5. <video> の再生（webm）
            page.click('#btnClear')
            page.wait_for_timeout(200)
            page.fill('#fSearch', 'IMG_0015')
            page.wait_for_timeout(300)
            page.click('.row >> text=IMG_0015')
            page.wait_for_selector('#vid:not([hidden])', timeout=5000)
            page.wait_for_function("document.getElementById('vid').readyState >= 2 && document.getElementById('vid').currentTime > 0.2", timeout=8000)
            check(True, '<video> で再生される（webm）')
            page.keyboard.press('2')
            check(page.evaluate("document.getElementById('vid').playbackRate") == 2, '2 キーで 2 倍速になる')
            page.keyboard.press('1')
            page.screenshot(path=os.path.join(out, '06_play_video.png'))
            page.fill('#fSearch', '')
            page.wait_for_timeout(250)

            # 6. CSV のみの行、重複の行
            page.click('#fMismatch')
            page.click('#states .chip >> text=CSV のみ')
            page.wait_for_timeout(300)
            check(page.evaluate('TM.S.view.length') == 1, '「CSV のみ」で絞れる')
            page.wait_for_function("document.getElementById('stageMsg').textContent.includes('どのフォルダにもありません')")
            # CSV にしかない動画を「残す」とすると、書き出しで印が付く
            page.wait_for_function("document.getElementById('detail').textContent.includes('CSV にしかない動画')")
            check('Note 列に「csvのみ」と書きます' in page.inner_text('#detail'), 'CSV にしかない動画に、付く印の説明が出る')
            page.keyboard.press('k')
            page.wait_for_timeout(500)
            pl = c.get('/api/items?season=N260101')[1]
            dec10 = [a[5] for a in pl['items'] if a[2] == 'IMG_0010' and pl['stations'][a[1]] == 'BSN0001']
            check(dec10 == ['k'], 'CSV にしかない動画を「残す」にできる（%s）' % dec10)
            page.screenshot(path=os.path.join(out, '06a_csv_only.png'))
            page.click('#states .chip >> text=CSV のみ')
            page.click('#states .chip >> text=場所内の重複')
            page.wait_for_timeout(400)
            check(page.locator('#detail input[name="pick"]').count() == 3, '重複では「これを正とする」を選べる')
            page.screenshot(path=os.path.join(out, '07_dup.png'))
            page.click('#states .chip >> text=場所内の重複')
            page.click('#fMismatch')

            # 7. CSV に足す行の入力
            page.fill('#fSearch', 'IMG_0014')
            page.wait_for_timeout(300)
            page.wait_for_selector('#addrow input')
            ph = page.get_attribute('#addrow input[data-col="DateTime"]', 'placeholder')
            check(ph == '2026/01/05 06:07:08', 'CSV に足す行の DateTime に埋め込みの日時が出る（%s）' % ph)
            ph = page.get_attribute('#addrow input[data-col="Species"]', 'placeholder')
            check(ph == 'Add', 'CSV に足す行の Species に、既定値が薄い字で出る（%s）' % ph)
            # 既定値を設定で変える
            page.click('#btnSettings')
            page.wait_for_selector('#setAddDefs')
            check('Species=Add' in page.input_value('#setAddDefs').split('\n'), '設定の窓に、追加する行の既定値が出る')
            check(page.input_value('#setMarkText') == 'csvのみ' and page.input_value('#setMarkCols') == 'Note, Remarks',
                  '設定の窓に、CSV にしかない動画の印と書く列が出る')
            page.fill('#setAddDefs', 'Species=New\nNote＝from {Season}\nこの行は形が違う')
            page.click('#modalBox [data-act="save"]')
            page.wait_for_timeout(400)
            check(page.locator('#setAddDefs').count() == 1, '「列名=値」の形でない行があると、保存せずに知らせる')
            page.fill('#setAddDefs', 'Species=New\nNote＝from {Season}\nRemarks=')
            page.screenshot(path=os.path.join(out, '08a_settings_defaults.png'))
            page.click('#modalBox [data-act="save"]')
            page.wait_for_selector('#setAddDefs', state='detached')
            saved_defs = c.get('/api/state')[1]['config']['settings']['add_defaults']
            check(saved_defs == {'Species': 'New', 'Note': 'from {Season}'}, '既定値の変更が保存される（%s）' % saved_defs)
            page.wait_for_timeout(500)
            page.fill('#fSearch', 'IMG_0014')
            page.wait_for_timeout(300)
            page.wait_for_selector('#addrow input')
            ph2 = [page.get_attribute('#addrow input[data-col="%s"]' % col, 'placeholder') for col in ('Species', 'Note')]
            check(ph2 == ['New', 'from N260101'], '変えた既定値が、CSV に足す行に出る（%s）' % ph2)
            check('既定値' in page.inner_text('#detail'), 'CSV に足す行に、既定値の説明が出る')
            page.fill('#addrow input[data-col="Species"]', 'deer')
            page.wait_for_timeout(700)
            page.screenshot(path=os.path.join(out, '08_addrow.png'))
            page.fill('#fSearch', '')
            page.wait_for_timeout(250)

            # 8. 一括判断
            page.click('#patterns .pat >> text=CSV になし')
            page.wait_for_timeout(200)
            nv = page.evaluate('TM.S.view.length')
            page.click('#toolbar [data-bulk="k"]')
            page.wait_for_selector('#modalBox [data-x="1"]')
            page.screenshot(path=os.path.join(out, '09_bulk_confirm.png'))
            page.click('#modalBox [data-x="1"]')
            page.wait_for_timeout(500)
            st, items = c.get('/api/items?season=' + tree['season'])
            kept = sum(1 for x in items['items'] if x[5] == 'k')
            check(kept >= nv, '表示中の動画をまとめて「残す」にできる（%d 本）' % nv)
            page.click('#patterns .pat >> text=CSV になし')

            # 9. 複数選択（Shift）
            page.click('.row >> nth=1')
            page.click('.row >> nth=4', modifiers=['Shift'])
            check(page.evaluate('TM.S.sel.size') == 4, 'Shift で範囲を選べる')
            check('選択した 4 本' in page.inner_text('#bulkLabel'), '選択中は選択した動画が一括判断の対象になる')
            page.click('.row >> nth=0')

            # 9b. 誤操作を防ぐ仕組み
            ready = "TM.S.detail && !document.getElementById('detail').classList.contains('stale') && TM.S.detail.id === TM.S.rows[TM.S.view[TM.S.cur]].id"
            count_dec = "TM.S.rows.filter(r => r.dec).length"
            page.click('#fUndecided')
            page.wait_for_timeout(300)
            page.keyboard.press('Space')
            check(page.is_checked('#fUndecided'), 'チェックを入れた直後のスペースで、チェックが外れない')
            page.wait_for_function(ready)
            n0 = page.evaluate(count_dec)
            page.keyboard.down('k')
            for _ in range(6):
                page.keyboard.down('k')          # 押しっぱなし（キーの自動繰り返し）
            page.keyboard.up('k')
            page.wait_for_timeout(700)
            check(page.evaluate(count_dec) == n0 + 1, 'K を押しっぱなしにしても、判断が付くのは 1 本だけ')
            page.wait_for_function(ready)
            page.keyboard.press('ArrowDown')
            page.keyboard.press('d')             # 右側がまだ前の動画のうちに押す
            page.wait_for_function(ready)
            check(page.evaluate("TM.S.rows[TM.S.view[TM.S.cur]].dec") == '' and page.evaluate(count_dec) == n0 + 1,
                  '動画の表示が切り替わる前の判断は受け付けない')
            page.keyboard.press('d')
            page.wait_for_timeout(400)
            check(page.evaluate(count_dec) == n0 + 2, '表示が切り替わったあとの判断は付く')
            page.click('#btnUndo')
            page.wait_for_timeout(300)
            page.click('#fUndecided')
            page.click('#states .chip >> text=場所内の重複')
            page.wait_for_selector('#detail input[name="pick"]')
            page.wait_for_function(ready)
            page.click('#detail input[name="pick"] >> nth=0')
            page.wait_for_timeout(200)
            page.keyboard.press('ArrowDown')
            page.wait_for_timeout(400)
            st, d12 = c.get('/api/item?season=%s&id=%d' % (tree['season'], page.evaluate('TM.S.detail.id')))
            check(d12['pick'].endswith(':IMG_0012.AVI'), '「これを正とする」を選んだあとの矢印キーで、選択が変わらない（%s）' % d12['pick'])
            page.click('#states .chip >> text=場所内の重複')
            page.click('#fMismatch')
            page.wait_for_timeout(200)
            page.click('.row >> nth=0')
            page.click('.row >> nth=2', modifiers=['Control'])
            page.click('.row >> nth=2', modifiers=['Control'])
            check(page.evaluate('TM.S.sel.size') == 1 and page.evaluate('TM.S.cur') == 0, 'Ctrl で選択を外した行にカーソルが残らない')
            page.click('.row >> nth=0')

            # 10. 集計と書き出し
            page.click('#btnSummary')
            page.wait_for_selector('#modalBox h2')
            page.screenshot(path=os.path.join(out, '10_summary.png'), full_page=False)
            check('想定の階層にない動画' in page.inner_text('#modalBox'), '集計に想定外の階層が出る')
            page.keyboard.press('Escape')
            page.click('#btnExport')
            page.wait_for_selector('#modalBox [data-ex="csv"]')
            extxt = page.inner_text('#modalBox')
            check('Note 列に「csvのみ」と書く' in extxt and page.is_checked('#exMark'),
                  '書き出しの窓に、CSV にしかない動画の印の項目が出る')
            check('追加する行のほかの列に入る既定値' in extxt and 'Species New' in extxt and 'Note from N260101' in extxt,
                  '書き出しの窓に、追加する行の既定値が出る')
            page.click('#modalBox [data-ex="result"]')
            page.wait_for_selector('#exResult .result')
            page.click('#modalBox [data-ex="dtcheck"]')
            page.wait_for_selector('#exCsv .result', timeout=15000)
            check('突き合わせの結果' in page.inner_text('#exCsv'), '撮影日時の突き合わせができる')
            page.screenshot(path=os.path.join(out, '11_dtcheck.png'))
            page.click('#modalBox [data-ex="csv"]')
            page.wait_for_function("document.getElementById('exCsv') && document.getElementById('exCsv').textContent.includes('更新後の CSV を書き出しました')", timeout=15000)
            page.click('#modalBox [data-ex="plan"]')
            page.wait_for_selector('#exPlan .result')
            page.screenshot(path=os.path.join(out, '12_export.png'))
            txt = page.inner_text('#exCsv')
            check('追加' in txt and 'NA' in txt, '更新後の CSV を書き出せる')
            path = page.inner_text('#exCsv .result .path').strip()
            check(os.path.isfile(path), '書き出した CSV が保存先にある')
            body = open(path, encoding='utf-8-sig', newline='').read()
            check('empty,"line one\nline two; csvのみ"\r\n' in body and body.count('csvのみ') == 1,
                  'CSV にしかない動画を「残す」とした行にだけ、印が書き出される')
            check('列に「csvのみ」と書いた行 1 行' in txt, '書き出しの結果に、印を付けた行数が出る')
            added = [ln for ln in open(path, encoding='utf-8-sig', newline='').read().split('\r\n')
                     if ln.endswith(',from N260101')]
            check(any(',IMG_0014.AVI,' in ln and ln.endswith(',deer,from N260101') for ln in added),
                  '行ごとに入力した値は、既定値より優先して書き出される')
            check(any(ln.endswith(',New,from N260101') for ln in added), '入力のない列には、既定値が書き出される（%d 行）' % len(added))
            # 10a. 全 Season をまとめて書き出す
            check('全 Season をまとめて書き出す' in page.inner_text('#modalBox') and '走査済みの 1 Season' in page.inner_text('#modalBox'),
                  '書き出しの窓に、全 Season の書き出しが出る')
            page.click('#modalBox [data-ex="all"]')
            page.wait_for_selector('#exAll .result', timeout=20000)
            txt = page.inner_text('#exAll')
            check('1 Season' in txt and '本を書き出しました' in txt and 'Season ごとの集計' in txt, '全 Season の書き出しの結果が出る')
            paths = [e.strip() for e in page.locator('#exAll .result .path').all_inner_texts()]
            check(len(paths) == 2 and all(os.path.isfile(x) for x in paths), '全 Season の一覧と集計が保存先にある')
            allrows = open(paths[0], encoding='utf-8-sig', newline='').read()
            check(allrows.startswith('Season,Station,File,HDD_file') and 'N260101,BSN0001,IMG_0010,' in allrows
                  and 'N260101,BSN0001,IMG_0003,' not in allrows, '全 Season の一覧は、不一致と判断のある動画だけ')
            page.screenshot(path=os.path.join(out, '12a_export_all.png'), full_page=True)
            page.keyboard.press('Escape')

            # 11. 再読み込みしても判断が残る
            page.reload()
            page.wait_for_selector('.row', timeout=10000)
            check(page.evaluate("TM.S.rows.filter(r => r.dec).length") >= kept, '再読み込みしても判断が残る')
            check(not page.is_checked('#fContext') and page.evaluate('TM.S.ctx.size') == 0, '再読み込みしても「前後の動画も表示」の設定が残る')
            page.set_viewport_size({'width': 1180, 'height': 760})
            page.wait_for_timeout(300)
            page.screenshot(path=os.path.join(out, '13_small.png'))
            # 12. プログラムを更新したのに、古い版が動いたままのときの案内
            check(page.evaluate("document.getElementById('verNotice').hidden"), '版が合っていれば、起動し直しの案内は出ない')

            def fake_state(route):
                r = route.fetch()
                body = r.json()
                body['version'] = '0.1.3'
                route.fulfill(response=r, json=body)
            page.route('**/api/state', fake_state)
            page.reload()
            page.wait_for_selector('.row', timeout=10000)
            check(page.evaluate("!document.getElementById('verNotice').hidden") and
                  '古い版（0.1.3）が動いたまま' in page.inner_text('#verNotice') and 'TrapMatch を起動し直して' in page.inner_text('#verNotice'),
                  '古い版が動いたままだと、起動し直しの案内が出る')
            page.screenshot(path=os.path.join(out, '14_old_version.png'))
            page.unroute('**/api/state')
            for ln in logs:
                print('console', ln)
            check(not [ln for ln in logs if 'pageerror' in ln or ln.startswith('error')], 'ブラウザのエラーがない')
            b.close()
    finally:
        try:
            c.post('/api/quit', {})
        except OSError:
            pass
        proc.wait(timeout=10)
        shutil.rmtree(base, ignore_errors=True)
    print('\n%d problem(s)' % len(problems))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
