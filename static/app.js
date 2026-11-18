'use strict';
(function () {
  // ------------------------------------------------------------ 共通
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC[c]);
  const nf = (n) => Number(n || 0).toLocaleString('ja-JP');

  const F = { MISSING: 1, CSVONLY: 2, SIZE: 4, DUP: 8, EXTDIFF: 16, CSVEXT: 32, ZERO: 64 };
  const NEEDS = F.MISSING | F.CSVONLY | F.SIZE | F.DUP | F.ZERO;   // 判断が必要な状態
  const OKBIT = 0x1000;
  // [ビット, 名前, 色の種類, 一覧での短い名前]
  const STATE_DEFS = [
    [F.MISSING, '欠け', 'miss', '欠け'], [F.CSVONLY, 'CSV のみ', 'miss', 'CSV のみ'], [F.SIZE, 'サイズ違い', 'bad', 'サイズ違い'],
    [F.DUP, '場所内の重複', 'bad', '重複'], [F.EXTDIFF, '拡張子違い', 'info', '拡張子'], [F.CSVEXT, 'CSV の拡張子違い', 'info', 'CSV 拡張子'],
    [F.ZERO, 'サイズ 0', 'bad', 'サイズ 0'],
  ];
  const DEC = { '': '未判断', k: '残す', d: '削除する', e: '例外として残す' };
  const DEC_SHORT = { '': '', k: '残す', d: '削除', e: '例外' };
  const DEC_API = { '': '', k: 'keep', d: 'delete', e: 'except' };
  const DEC_CODE = { '': '', keep: 'k', delete: 'd', except: 'e' };
  const ROW_H = 28;
  const UI_VERSION = '0.2.0';      // この画面のプログラムの版（tm_core.py の VERSION と同じにする）

  const S = {
    cfg: null, ffmpeg: false, dataDir: '', season: '', data: null, busy: false,
    rows: [], byId: new Map(), view: [], cur: -1, sel: new Set(), anchor: -1,
    filt: { mismatch: true, undecided: false, pair: null, states: 0, mask: null, station: null, q: '' },
    stCount: null, stLack: [], undo: null, detail: null, detailReq: 0, speed: 1,
    nsrc: 0, full: 0, loadReq: 0, deciding: false, scanning: false,
    // 前後の動画: 絞り込んだ一覧に足した「一つ前」「一つ後」の動画（id -> 1: 一つ前、2: 一つ後、3: 両方）
    ctx: new Map(), nreal: 0, showCtx: true, folderMask: 0,
  };
  const CTX_LABEL = { 1: ['一つ前'], 2: ['一つ後'], 3: ['一つ後', '一つ前'] };
  try { S.showCtx = localStorage.getItem('tm.ctx') !== '0'; } catch (e) { /* 保存できない環境では既定のまま */ }

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    };
    const r = await fetch(path, opt);
    let j = null;
    try { j = await r.json(); } catch (e) { /* 本文なし */ }
    if (!r.ok) throw new Error((j && j.error) || ('エラー ' + r.status));
    return j;
  }

  let toastTimer = 0;
  function toast(text, isErr) {
    const t = $('toast');
    t.textContent = text;
    t.className = isErr ? 'err' : '';
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 2600);
  }

  function fmtSize(b) {
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(0) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  }
  function fmtTime(t) {
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function tagsHtml(flags, short) {
    let h = '';
    for (const [bit, label, cls, brief] of STATE_DEFS) if (flags & bit) h += `<span class="tag ${cls}">${short ? brief : label}</span>`;
    return h || '<span class="tag ok">一致</span>';
  }
  function ctxTags(cx) {
    return CTX_LABEL[cx].map((t) => `<span class="tag ctx">${t}</span>`).join('');
  }
  function stateTitle(flags) {
    return STATE_DEFS.filter((s) => flags & s[0]).map((s) => s[1]).join('、') || '一致';
  }

  // ------------------------------------------------------------ 窓
  let modalClose = null;
  function openModal(html, narrow, onClose) {
    const box = $('modalBox');
    box.onclick = null;
    box.oninput = null;
    box.onchange = null;
    box.className = narrow ? 'narrow' : '';
    box.innerHTML = html;
    $('modal').hidden = false;
    modalClose = onClose || null;
    const first = box.querySelector('[autofocus]') || box.querySelector('button.primary');
    if (first) first.focus({ preventScroll: true });
    $('modal').scrollTop = 0;
    return box;
  }
  function closeModal() {
    $('modal').hidden = true;
    $('modalBox').innerHTML = '';
    const cb = modalClose;
    modalClose = null;
    if (cb) cb();
    $('tbody').focus({ preventScroll: true });
  }
  $('modal').addEventListener('mousedown', (e) => { if (e.target === $('modal')) closeModal(); });

  function confirmBox(message, okLabel, danger) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const box = openModal(
        `<p>${esc(message)}</p><div class="mfoot"><button data-x="0">やめる</button>` +
        `<button data-x="1" class="${danger ? 'danger' : 'primary'}" autofocus>${esc(okLabel || '実行する')}</button></div>`,
        true, () => finish(false));
      box.onclick = (e) => {
        const x = e.target.getAttribute && e.target.getAttribute('data-x');
        if (x === null || x === undefined) return;
        finish(x === '1');
        closeModal();
      };
    });
  }

  // ------------------------------------------------------------ ジョブ
  function setBusy(b) {
    S.busy = b;
    $('btnScan').disabled = b;
    $('btnExport').disabled = b;
    if (!b) setScanning(false);
  }
  // 走査中は判断やメモを受け付けない（走査の最後に作業状態を書き直すので、その間の変更が失われるため）
  function setScanning(on) {
    S.scanning = on;
    document.body.classList.toggle('scanning', on);
  }
  function blockedByScan() {
    if (!S.scanning) return false;
    toast('走査が終わるまで、判断とメモは変更できません');
    return true;
  }
  function watchJob(onDone, onTick) {
    setBusy(true);
    $('jobbar').hidden = false;
    const tick = async () => {
      let j = null;
      try { j = (await api('/api/job')).job; } catch (e) { setTimeout(tick, 1000); return; }
      if (!j) { $('jobbar').hidden = true; setBusy(false); return; }
      const text = j.phase + (j.total ? `  ${nf(j.done)} / ${nf(j.total)}` : '');
      $('jobText').textContent = text;
      if (onTick && j.state === 'running') onTick(text);
      $('jobFill').style.width = j.total ? (100 * j.done / j.total).toFixed(1) + '%' : '0%';
      if (j.state === 'running') { setTimeout(tick, 350); return; }
      $('jobbar').hidden = true;
      setBusy(false);
      if (onDone) onDone(j);
    };
    tick();
  }

  // ------------------------------------------------------------ 起動
  async function init() {
    let st;
    try { st = await api('/api/state'); } catch (e) { toast('サーバーに接続できません: ' + e.message, true); return; }
    S.cfg = st.config;
    S.ffmpeg = st.ffmpeg;
    S.dataDir = st.data_dir;
    $('ver').textContent = st.version;
    if (st.version !== UI_VERSION) {
      // プログラムのファイルは更新されたのに、古い版のサーバーが動いたまま（起動し直していない）
      const el = $('verNotice');
      el.textContent = `プログラムは ${UI_VERSION} に更新されていますが、古い版（${st.version}）が動いたままです。` +
        '新しい機能はまだ使えません。黒い窓（タイトルが TrapMatch の窓）を閉じてから、TrapMatch を起動し直してください。';
      el.hidden = false;
    }
    fillSeasons([], st.projects);
    refreshSeasons();
    const last = localStorage.getItem('tm.season') || (st.projects[0] && st.projects[0].season) || '';
    if (st.job && st.job.state === 'running') {
      $('season').value = st.job.season;
      if (st.job.kind === 'scan') { setScanning(true); showEmpty('走査中です。終わると一覧が出ます。'); }
      else loadSeason(st.job.season);
      watchJob((j) => afterJob(j));
    } else if (!S.cfg.sources.length) {
      showEmpty('はじめに「保存場所と設定」で、照合する保存場所（HDD、NAS、CSV）を登録してください。');
      openSettings();
    } else if (last) {
      $('season').value = last;
      loadSeason(last);
    } else {
      showEmpty('Season を選んで「走査」を押してください。');
    }
    updateStatus();
  }

  function fillSeasons(seasons, projects) {
    const done = new Map((projects || []).map((p) => [p.season.toLowerCase(), p]));
    const names = seasons.slice();
    for (const p of projects || []) if (!names.some((n) => n.toLowerCase() === p.season.toLowerCase())) names.push(p.season);
    $('seasonList').innerHTML = names.map((n) => {
      const p = done.get(n.toLowerCase());
      return `<option value="${esc(n)}">${p ? '走査済み ' + esc(p.scanned_at) : ''}</option>`;
    }).join('');
  }
  async function refreshSeasons() {
    try {
      const r = await api('/api/seasons');
      fillSeasons(r.seasons, r.projects);
    } catch (e) { /* 一覧が取れなくても入力はできる */ }
  }

  function showEmpty(text) {
    const e = $('empty');
    e.textContent = text;
    e.hidden = !text;
  }

  async function loadSeason(season) {
    season = season.trim();
    if (!season) return;
    const req = ++S.loadReq;
    let d;
    try { d = await api('/api/items?season=' + enc(season)); } catch (e) {
      if (req === S.loadReq) toast(e.message, true);
      return;
    }
    if (req !== S.loadReq) return;      // あとから別の Season を選び直した: この応答は使わない
    flushSaves();
    S.season = d.season || season;      // 以後の操作は、いま画面に出ている Season に対して行う
    localStorage.setItem('tm.season', S.season);
    if (!d.scanned) {
      clearData();
      showEmpty(`「${season}」はまだ走査していません。「走査」を押すと、保存場所を読んで照合します（ファイルは変更しません）。`);
      return;
    }
    $('season').value = S.season;
    setData(d);
  }

  function clearData() {
    stopPlayer();
    S.data = null; S.rows = []; S.byId = new Map(); S.view = []; S.cur = -1; S.sel.clear(); S.detail = null;
    S.ctx = new Map(); S.nreal = 0;
    $('detail').classList.remove('stale');
    $('rows').innerHTML = ''; $('spacer').style.height = '0px'; $('thead').innerHTML = '';
    $('detail').innerHTML = ''; $('srcBtns').innerHTML = '';
    $('pairs').innerHTML = ''; $('states').innerHTML = ''; $('patterns').innerHTML = ''; $('stationList').innerHTML = '';
    $('scanInfo').textContent = ''; $('notice').hidden = true;
    stageMsg('一覧の動画を選ぶと、ここで再生します');
    updateToolbar(); updateStatus();
  }

  function setData(d) {
    stopPlayer();
    S.data = d;
    S.nsrc = d.sources.length;
    S.full = (1 << S.nsrc) - 1;
    S.folderMask = 0;
    d.sources.forEach((x, b) => { if (x.type === 'folder') S.folderMask |= 1 << b; });
    S.ctx = new Map(); S.nreal = 0;
    S.rows = d.items.map((a) => ({
      id: a[0], st: a[1], stem: a[2], stemL: a[2].toLowerCase(), mask: a[3], flags: a[4], dec: a[5], memo: a[6], cells: a[7],
    }));
    S.byId = new Map(S.rows.map((r) => [r.id, r]));
    S.view = []; S.cur = -1; S.sel.clear(); S.anchor = -1; S.detail = null; S.undo = null;
    $('btnUndo').disabled = true;
    $('detail').innerHTML = ''; $('srcBtns').innerHTML = '';
    $('detail').classList.remove('stale');
    stageMsg('一覧の動画を選ぶと、ここで再生します');
    $('scanInfo').textContent = '走査 ' + d.scanned_at;
    // Station ごとに、動画が 1 本もない保存場所を調べる（Station 全体の欠け）
    const per = d.stations.map(() => new Int32Array(S.nsrc));
    for (const r of S.rows) for (let b = 0; b < S.nsrc; b++) if (r.mask >> b & 1) per[r.st][b]++;
    S.stLack = per.map((c) => {
      const lack = [];
      for (let b = 0; b < S.nsrc; b++) if (!c[b]) lack.push(d.sources[b].name);
      return lack.length && lack.length < S.nsrc ? lack : [];
    });
    S.filt = { mismatch: true, undecided: false, pair: null, states: 0, mask: null, station: null, q: '' };
    $('fSearch').value = '';
    buildHead();
    buildFilterUI();
    applyFilters();
    showNotice();
    if (S.view.length) select(firstReal(), { quiet: true });
    $('tbody').focus({ preventScroll: true });
  }

  function showNotice() {
    const d = S.data;
    const lines = [];
    if (!d) { $('notice').hidden = true; return; }
    const info = d.info || {};
    for (const sid of info.missing_season || []) {
      const s = d.sources.find((x) => x.id === sid);
      if (s) lines.push(`「${s.name}」に Season フォルダ「${d.season}」がありません。この保存場所の動画はすべて「なし」として扱っています。`);
    }
    if (d.csv && d.csv.malformed) lines.push(`CSV に、引用符（"）の対応が取れない行が ${nf(d.csv.malformed)} か所あります。その付近の行は正しく読めていない可能性があります。`);
    const ex = (info.exceptions || []).length;
    if (ex) lines.push(`想定の階層にない動画が ${nf(ex)} か所あります（「集計」で確認できます。照合の対象にはしていません）。`);
    const cur = S.cfg;
    const used = cur.sources.filter((s) => s.enabled && s.path);
    const folders = used.filter((s) => s.type === 'folder');
    const csvs = used.filter((s) => s.type === 'csv').slice(0, 1);
    const now = folders.concat(csvs).map((s) => s.id + '|' + (s.type === 'csv' ? s.path.replace(/\{season\}/gi, d.season) : s.path)).join('\n');
    const then = d.sources.map((s) => s.id + '|' + s.path).join('\n');
    const extsNow = cur.settings.exts.slice().sort().join(',');
    if (now !== then || cur.settings.ignore_ext !== d.ignore_ext || extsNow !== d.exts.slice().sort().join(',')) {
      lines.push('保存場所か照合の設定が、走査のあとに変わっています。「走査」をやり直すと反映されます（判断とメモは保たれます）。');
    }
    $('notice').innerHTML = lines.map((l) => `<div>${esc(l)}</div>`).join('');
    $('notice').hidden = !lines.length;
  }

  // ------------------------------------------------------------ 一覧の見出しと絞り込み
  function buildHead() {
    const d = S.data;
    const cols = `92px minmax(140px, 1.3fr) repeat(${S.nsrc}, 118px) minmax(104px, 170px) 48px minmax(70px, 1fr)`;
    const minw = 92 + 140 + S.nsrc * 118 + 104 + 48 + 70;
    document.documentElement.style.setProperty('--cols', cols);
    document.documentElement.style.setProperty('--minw', minw + 'px');
    let h = '<div class="hrow"><div>Station</div><div>ファイル名</div>';
    for (const s of d.sources) h += `<div class="src" title="${esc(s.path)}">${esc(s.name)}</div>`;
    h += '<div>状態</div><div>判断</div><div>メモ</div></div>';
    $('thead').innerHTML = h;
  }

  function pairDiff(r, i, j) {
    const a = r.cells[i], b = r.cells[j];
    if (!a !== !b) return true;
    if (!a || !b) return false;
    const ta = S.data.sources[i].type, tb = S.data.sources[j].type;
    if (ta === 'folder' && tb === 'folder') {
      return a[0].toLowerCase() === b[0].toLowerCase() && a[1] !== b[1];
    }
    return false;
  }

  function patternLabel(mask) {
    if (mask === S.full) return 'すべてにあり';
    const lack = [];
    for (let b = 0; b < S.nsrc; b++) if (!(mask >> b & 1)) lack.push(S.data.sources[b].name);
    return lack.join(' と ') + ' になし';
  }

  function buildFilterUI() {
    const d = S.data;
    // 組
    const pairs = [];
    for (let i = 0; i < S.nsrc; i++) for (let j = i + 1; j < S.nsrc; j++) pairs.push([i, j, 0]);
    const stateCount = new Map(STATE_DEFS.map((s) => [s[0], 0]));
    let okCount = 0, needs = 0;
    const pat = new Map();
    for (const r of S.rows) {
      for (const p of pairs) if (pairDiff(r, p[0], p[1])) p[2]++;
      for (const s of STATE_DEFS) if (r.flags & s[0]) stateCount.set(s[0], stateCount.get(s[0]) + 1);
      if (r.flags & NEEDS) needs++; else okCount++;
      pat.set(r.mask, (pat.get(r.mask) || 0) + 1);
    }
    $('cMismatch').textContent = nf(needs);
    $('pairs').innerHTML = pairs.map((p) =>
      `<button class="chip${p[2] ? '' : ' zero'}" data-pair="${p[0]},${p[1]}">${esc(d.sources[p[0]].name)} と ${esc(d.sources[p[1]].name)}<i>${nf(p[2])}</i></button>`).join('');
    $('states').innerHTML = STATE_DEFS.map((s) =>
      `<button class="chip${stateCount.get(s[0]) ? '' : ' zero'}" data-state="${s[0]}">${s[1]}<i>${nf(stateCount.get(s[0]))}</i></button>`).join('') +
      `<button class="chip${okCount ? '' : ' zero'}" data-state="${OKBIT}">一致<i>${nf(okCount)}</i></button>`;
    const pats = Array.from(pat.entries()).sort((a, b) => b[1] - a[1]).slice(0, 16);
    $('patterns').innerHTML = pats.map(([mask, n]) => {
      let dots = '';
      for (let b = 0; b < S.nsrc; b++) dots += `<span class="dot${mask >> b & 1 ? '' : ' off'}"></span>`;
      return `<button class="pat" data-mask="${mask}" title="${esc(d.sources.map((s, b) => s.name + ': ' + (mask >> b & 1 ? 'あり' : 'なし')).join('、'))}">` +
        `<span class="dots">${dots}</span><span class="lab">${esc(patternLabel(mask))}</span><b>${nf(n)}</b></button>`;
    }).join('');
    syncFilterUI();
  }

  function syncFilterUI() {
    const f = S.filt;
    $('fMismatch').checked = f.mismatch;
    $('fUndecided').checked = f.undecided;
    $('fContext').checked = S.showCtx;
    for (const el of $('pairs').children) el.classList.toggle('on', !!f.pair && el.dataset.pair === f.pair.join(','));
    for (const el of $('states').children) el.classList.toggle('on', !!(f.states & Number(el.dataset.state)));
    for (const el of $('patterns').children) el.classList.toggle('on', f.mask !== null && Number(el.dataset.mask) === f.mask);
  }

  function curRow() {
    return S.cur >= 0 && S.cur < S.view.length ? S.rows[S.view[S.cur]] : null;
  }

  function applyFilters() {
    if (!S.data) return;
    const f = S.filt;
    const q = f.q.trim().toLowerCase();
    const keep = curRow();
    let view = [];
    const stCount = new Int32Array(S.data.stations.length);
    const rows = S.rows;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (f.mismatch && !(r.flags & NEEDS)) continue;
      if (f.undecided && r.dec) continue;
      if (f.mask !== null && r.mask !== f.mask) continue;
      if (f.pair && !pairDiff(r, f.pair[0], f.pair[1])) continue;
      if (f.states && !(r.flags & f.states) && !((f.states & OKBIT) && !(r.flags & NEEDS))) continue;
      if (q && r.stemL.indexOf(q) < 0) continue;
      stCount[r.st]++;
      if (f.station !== null && r.st !== f.station) continue;
      view.push(i);
    }
    S.nreal = view.length;
    S.ctx = new Map();
    if (S.showCtx && view.length && view.length < rows.length) view = withContext(view);
    S.view = view;
    S.stCount = stCount;
    let cur = -1;
    if (keep) for (let v = 0; v < view.length; v++) if (rows[view[v]] === keep) { cur = v; break; }
    $('spacer').style.height = (view.length * ROW_H) + 'px';
    renderStations();
    syncFilterUI();
    if (cur >= 0) {
      S.cur = cur; S.anchor = cur;
      S.sel.clear(); S.sel.add(keep.id);
      ensureVisible(cur);
      renderRows(); updateToolbar();
      syncLock();
    } else if (view.length) {
      $('tbody').scrollTop = 0;
      select(firstReal(), { quiet: true });
    } else {
      S.cur = -1; S.sel.clear(); S.detail = null;
      stopPlayer();
      $('detail').innerHTML = ''; $('srcBtns').innerHTML = '';
      $('detail').classList.remove('stale');
      stageMsg('一覧の動画を選ぶと、ここで再生します');
      renderRows(); updateToolbar();
    }
    const total = rows.length;
    if (!view.length) {
      showEmpty(total ? (f.mismatch && !rows.some((r) => r.flags & NEEDS)
        ? 'この Season に不一致はありません。すべての保存場所が同じ動画を持っています。'
        : 'この絞り込みに当てはまる動画はありません。') : 'この Season には動画がありません。');
    } else showEmpty('');
    updateStatus();
  }

  // 前後の動画を足した並びを返す。表示する動画の連続したまとまりごとに、同じ Station で
  // その一つ前と一つ後にある動画（保存場所のどこかに動画があるもの）を足す。なければ足さない。
  function withContext(view) {
    const rows = S.rows, n = rows.length;
    const inView = new Uint8Array(n), mark = new Uint8Array(n);
    for (const i of view) inView[i] = 1;
    const near = (i, step) => {
      const st = rows[i].st;
      for (let j = i + step; j >= 0 && j < n && rows[j].st === st; j += step) {
        if (inView[j]) return -1;                       // 隣は表示中の動画: 足すものはない
        if (rows[j].mask & S.folderMask) return j;      // 動画がある
      }                                                 // 動画のない行（CSV のみ）は飛ばして、その先を見る
      return -1;
    };
    for (const i of view) {
      const a = near(i, -1), b = near(i, 1);
      if (a >= 0) mark[a] |= 1;
      if (b >= 0) mark[b] |= 2;
    }
    const out = [];
    for (let i = 0; i < n; i++) {
      if (inView[i]) out.push(i);
      else if (mark[i]) { out.push(i); S.ctx.set(rows[i].id, mark[i]); }
    }
    return out;
  }
  function isCtx(r) { return !!r && S.ctx.has(r.id); }
  function firstReal() {
    for (let v = 0; v < S.view.length; v++) if (!S.ctx.has(S.rows[S.view[v]].id)) return v;
    return 0;
  }

  function renderStations() {
    const d = S.data;
    const f = S.filt;
    let total = 0;
    for (let i = 0; i < S.stCount.length; i++) total += S.stCount[i];
    let h = `<button class="st${f.station === null ? ' on' : ''}" data-st="-1"><span class="nm">すべての Station</span><b>${nf(total)}</b></button>`;
    for (let i = 0; i < d.stations.length; i++) {
      if (!S.stCount[i] && f.station !== i) continue;
      const lack = S.stLack[i];
      h += `<button class="st${f.station === i ? ' on' : ''}" data-st="${i}"><span class="nm">${esc(d.stations[i])}</span>` +
        (lack.length ? `<span class="lack" title="この Station の動画が 1 本もない保存場所">${esc(lack.join('、'))} なし</span>` : '') +
        `<b>${nf(S.stCount[i])}</b></button>`;
    }
    $('stationList').innerHTML = h;
  }

  // ------------------------------------------------------------ 一覧の描画
  function cellHtml(r, b) {
    const c = r.cells[b];
    if (!c) return '<div class="cell miss"><span class="dot off"></span>なし</div>';
    const ext = c[0] ? c[0].toUpperCase() : '';
    if (S.data.sources[b].type === 'csv') {
      const cls = (r.flags & (F.CSVEXT | F.EXTDIFF)) ? ' diff' : '';
      return `<div class="cell"><span class="dot"></span><span class="ext${cls}">${esc(ext || '拡張子なし')}</span>` +
        (c[1] > 1 ? `<span class="sz">${c[1]} 行</span>` : '') + '</div>';
    }
    return `<div class="cell"><span class="dot"></span><span class="ext${r.flags & F.EXTDIFF ? ' diff' : ''}">${esc(ext)}</span>` +
      (c[2] > 1 ? `<span class="mult">×${c[2]}</span>` : '') +
      `<span class="sz${r.flags & (F.SIZE | F.ZERO) ? ' warn' : ''}">${fmtSize(c[1])}</span></div>`;
  }
  function rowHtml(v) {
    const r = S.rows[S.view[v]];
    const cx = S.ctx.get(r.id);
    const cls = 'row' + (cx ? ' ctx' : '') + (v === S.cur ? ' cur' : (S.sel.has(r.id) ? ' sel' : ''));
    let h = `<div class="${cls}" data-v="${v}"><div class="stn">${esc(S.data.stations[r.st])}</div>` +
      `<div class="fn" title="クリックで再生">${esc(r.stem)}</div>`;
    for (let b = 0; b < S.nsrc; b++) h += cellHtml(r, b);
    h += (cx ? `<div title="前後の確認用に表示しています（判断は変えられません）。この動画の状態: ${stateTitle(r.flags)}">${ctxTags(cx)}</div>`
      : `<div title="${stateTitle(r.flags)}">${tagsHtml(r.flags, true)}</div>`) + `<div class="dec ${r.dec}">${DEC_SHORT[r.dec]}</div>` +
      `<div class="memo" title="${esc(r.memo)}">${esc(r.memo)}</div></div>`;
    return h;
  }
  function renderRows() {
    const tb = $('tbody');
    if (!S.data) { $('rows').innerHTML = ''; return; }
    const top = tb.scrollTop, h = tb.clientHeight || 600;
    const first = Math.max(0, Math.floor(top / ROW_H) - 8);
    const last = Math.min(S.view.length, Math.ceil((top + h) / ROW_H) + 8);
    let html = '';
    for (let v = first; v < last; v++) html += rowHtml(v);
    const el = $('rows');
    el.style.transform = `translateY(${first * ROW_H}px)`;
    el.innerHTML = html;
    $('thead').style.paddingRight = (tb.offsetWidth - tb.clientWidth) + 'px';
  }
  function ensureVisible(v) {
    const tb = $('tbody');
    const y = v * ROW_H, h = tb.clientHeight || 600;
    if (y < tb.scrollTop) tb.scrollTop = y;
    else if (y + ROW_H > tb.scrollTop + h) tb.scrollTop = y + ROW_H - h;
  }
  let scrollRaf = 0;
  $('tbody').addEventListener('scroll', () => {
    $('thead').scrollLeft = $('tbody').scrollLeft;
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; renderRows(); });
  });
  window.addEventListener('resize', renderRows);

  function updateToolbar() {
    const n = S.nreal;
    const total = S.rows.length;
    $('viewCount').textContent = S.data ? `${nf(n)} 本を表示` + (n !== total ? `（全 ${nf(total)} 本）` : '') +
      (S.ctx.size ? `　＋前後の動画 ${nf(S.ctx.size)} 本` : '') : '';
    $('bulkLabel').textContent = S.sel.size > 1 ? `選択した ${nf(S.sel.size)} 本にまとめて:` : '表示中の動画にまとめて:';
    for (const b of document.querySelectorAll('#toolbar [data-bulk]')) b.disabled = !n;
  }
  function updateStatus() {
    const rows = S.rows;
    if (!S.data) { $('status').innerHTML = `<span>設定と作業状態の保存先 ${esc(S.dataDir)}</span>`; $('cUndecided').textContent = ''; return; }
    let needs = 0, und = 0, k = 0, d = 0, e = 0;
    for (const r of rows) {
      if (r.flags & NEEDS) { needs++; if (!r.dec) und++; }
      if (r.dec === 'k') k++; else if (r.dec === 'd') d++; else if (r.dec === 'e') e++;
    }
    $('cUndecided').textContent = nf(und);
    $('status').innerHTML =
      `<span>全 <b>${nf(rows.length)}</b> 本</span><span>判断が必要 <b>${nf(needs)}</b> 本</span>` +
      `<span>未判断 <b>${nf(und)}</b> 本</span><span>残す <b>${nf(k)}</b></span><span>削除する <b>${nf(d)}</b></span>` +
      `<span>例外 <b>${nf(e)}</b></span><span class="right">この版は保存場所のファイルを変更しません</span>`;
  }

  // ------------------------------------------------------------ 選択
  function select(v, o) {
    o = o || {};
    if (v < 0 || v >= S.view.length) return;
    const r = S.rows[S.view[v]];
    if (o.shift && S.anchor >= 0) {
      S.sel.clear();
      const a = Math.min(S.anchor, v), b = Math.max(S.anchor, v);
      for (let i = a; i <= b; i++) S.sel.add(S.rows[S.view[i]].id);
    } else if (o.ctrl) {
      if (S.sel.has(r.id) && S.sel.size > 1) {
        S.sel.delete(r.id);
        // 外した行にカーソルを残さない（残すと、キーでの判断が外した行に付いてしまう）
        let keep = -1;
        for (let i = 0; i < S.view.length; i++) if (S.sel.has(S.rows[S.view[i]].id)) { keep = i; if (i >= v) break; }
        v = keep >= 0 ? keep : v;
      } else S.sel.add(r.id);
      S.anchor = v;
    } else {
      S.sel.clear(); S.sel.add(r.id); S.anchor = v;
    }
    const row = S.rows[S.view[v]];
    const changed = S.cur !== v || !S.detail || S.detail.id !== row.id;
    S.cur = v;
    if (!o.ctrl) ensureVisible(v);
    renderRows();
    updateToolbar();
    if (changed) markStale();
    if (changed || o.play) scheduleDetail(o.play);
  }
  // 行を移った直後は、右側にまだ前の動画が出ている。読み込みが終わるまで、再生を止めて判断を受け付けない。
  function markStale() {
    stopPlayer();
    stageMsg('読み込み中');
    $('detail').classList.add('stale');
    $('srcBtns').innerHTML = '';
  }
  function detailReady() {
    const r = curRow();
    return !!(r && S.detail && S.detail.id === r.id && !$('detail').classList.contains('stale'));
  }
  function moveCur(delta, shift) {
    if (!S.view.length) return;
    const v = Math.max(0, Math.min(S.view.length - 1, (S.cur < 0 ? 0 : S.cur) + delta));
    select(v, { shift });
  }
  $('rows').addEventListener('click', (e) => {
    const row = e.target.closest('.row');
    if (!row) return;
    const v = Number(row.dataset.v);
    select(v, { shift: e.shiftKey, ctrl: e.ctrlKey || e.metaKey, play: e.target.classList.contains('fn') && !e.shiftKey && !e.ctrlKey });
    $('tbody').focus({ preventScroll: true });
  });

  // ------------------------------------------------------------ 判断
  const CTX_LOCKED = 'この動画は前後の確認用に表示しています。判断は変えられません';
  async function applyDecision(targets, code) {
    const prev = targets.map((r) => [r.id, DEC_API[r.dec]]);
    for (const r of targets) r.dec = code;
    renderRows(); updateStatus(); syncDecideButtons();
    S.deciding = true;
    try {
      await api('/api/decide', { season: S.season, ids: targets.map((r) => r.id), decision: DEC_API[code] });
      S.undo = prev;
      $('btnUndo').disabled = false;
    } catch (e) {
      toast('判断を保存できませんでした: ' + e.message, true);
      loadSeason(S.season);
    } finally {
      S.deciding = false;
    }
  }
  async function decide(code) {
    if (!S.data || S.deciding || blockedByScan()) return;
    if (S.sel.size > 1) {
      const targets = S.rows.filter((r) => S.sel.has(r.id) && !S.ctx.has(r.id));    // 前後の動画には判断を付けない
      if (!targets.length) { toast(CTX_LOCKED); return; }
      const ok = await confirmBox(`選択した ${nf(targets.length)} 本を「${DEC[code]}」にします。`, DEC[code] + 'にする', code === 'd');
      if (!ok) return;
      await applyDecision(targets, code);
      return;
    }
    const r = curRow();
    if (!r) return;
    if (isCtx(r)) { toast(CTX_LOCKED); return; }
    if (!detailReady()) return;        // まだ前の動画が表示されている: 見ていない動画には判断を付けない
    await applyDecision([r], code);
    if (code && S.cfg.settings.auto_next) gotoNextUndecided();
  }
  function gotoNextUndecided() {
    for (let v = S.cur + 1; v < S.view.length; v++) {
      const r = S.rows[S.view[v]];
      if (!r.dec && (r.flags & NEEDS) && !S.ctx.has(r.id)) { select(v); return; }
    }
    toast('この先に未判断の動画はありません');
  }
  async function bulkDecide(code) {
    if (!S.data || !S.view.length || S.deciding || blockedByScan()) return;
    const bySel = S.sel.size > 1;
    const targets = (bySel ? S.rows.filter((r) => S.sel.has(r.id)) : S.view.map((i) => S.rows[i]))
      .filter((r) => !S.ctx.has(r.id));                 // 前後の動画には判断を付けない
    if (!targets.length) { toast(CTX_LOCKED); return; }
    const what = bySel ? `選択した ${nf(targets.length)} 本` : `表示中の ${nf(targets.length)} 本すべて`;
    const ok = await confirmBox(`${what}を「${DEC[code]}」にします。` + (code === 'd' ? 'この版ではファイルは変更されず、判断だけが記録されます。' : ''),
      DEC[code] + 'にする', code === 'd');
    if (!ok) return;
    await applyDecision(targets, code);
    toast(`${nf(targets.length)} 本を「${DEC[code]}」にしました`);
  }
  async function undo() {
    if (!S.undo || blockedByScan()) return;
    const pairs = S.undo;
    S.undo = null;
    $('btnUndo').disabled = true;
    try {
      await api('/api/decide', { season: S.season, pairs });
      for (const [id, d] of pairs) { const r = S.byId.get(id); if (r) r.dec = DEC_CODE[d] || ''; }
      renderRows(); updateStatus(); syncDecideButtons();
      toast(`${nf(pairs.length)} 本の判断を元に戻しました`);
    } catch (e) { toast('元に戻せませんでした: ' + e.message, true); }
  }
  for (const b of document.querySelectorAll('#toolbar [data-bulk]')) b.addEventListener('click', () => bulkDecide(b.dataset.bulk));
  $('btnUndo').addEventListener('click', undo);

  // ------------------------------------------------------------ 詳細
  let detailTimer = 0;
  function scheduleDetail(forcePlay) {
    clearTimeout(detailTimer);
    detailTimer = setTimeout(() => loadDetail(forcePlay), 70);
  }
  async function loadDetail(forcePlay) {
    const r = curRow();
    if (!r) return;
    flushSaves();
    const req = ++S.detailReq;
    let d;
    try { d = await api(`/api/item?season=${enc(S.season)}&id=${r.id}`); } catch (e) {
      if (req === S.detailReq) toast(e.message, true);
      return;
    }
    if (req !== S.detailReq) return;
    S.detail = d;
    renderDetail(d);
    let first = null;
    for (const s of d.sources) if (s.type === 'folder' && s.files.length) { first = { src: s.id, n: 0 }; break; }
    renderSrcButtons(d);
    if (first) playFile(first.src, first.n, S.cfg.settings.autoplay || !!forcePlay);
    else { stopPlayer(); stageMsg('この動画はどのフォルダにもありません（CSV にだけ行があります）'); }
  }

  function renderSrcButtons(d) {
    let h = '';
    for (const s of d.sources) {
      if (s.type !== 'folder') continue;
      for (const f of s.files) {
        h += `<button data-src="${esc(s.id)}" data-n="${f.n}" title="${esc(f.path)}">${esc(s.name)}${s.files.length > 1 ? ' ' + esc(f.ext.toUpperCase()) : ''}</button>`;
      }
    }
    $('srcBtns').innerHTML = h;
  }
  function markSrcButtons() {
    for (const b of $('srcBtns').children) b.classList.toggle('on', b.dataset.src === P.src && Number(b.dataset.n) === P.n);
  }
  $('srcBtns').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) playFile(b.dataset.src, Number(b.dataset.n), true);
  });

  // CSV に足す行の DateTime に何が入るかの説明
  function fillHint(d) {
    const f = d.fill;
    const minute = f.minute_only ? 'この CSV の DateTime は分単位なので、秒は 00 にそろえます。' : '';
    if (f.origin === 'embedded') {
      return `<p class="hint">空欄の DateTime には、埋め込まれた撮影日時から <b>${esc(f.text)}</b> が入ります。${minute}</p>`;
    }
    if (f.origin === 'mtime') {
      return `<p class="hint">空欄の DateTime には、ファイルの更新日時から <b>${esc(f.text)}</b> が入ります（埋め込まれた撮影日時がないため）。${minute}</p>`;
    }
    if (f.origin === 'unreadable') {
      return '<p class="hint">動画を読めなかったため、空欄の DateTime は NA になります。保存場所の接続を確かめてください。</p>';
    }
    let h = '<p class="hint">空欄の DateTime には <b>NA</b> が入ります（埋め込まれた撮影日時がないため）。</p>';
    if (f.dt_source === 'embedded' && d.mtime) {
      h += '<p class="hint"><button class="link" data-dtsrc="embedded_mtime">埋め込みがないときは、ファイルの更新日時を使う設定にする</button></p>';
    }
    return h;
  }
  // DateTime の取得元を切り替えて保存する
  async function setDtSource(value) {
    const cfg = JSON.parse(JSON.stringify(S.cfg));
    cfg.settings.dt_source = value;
    try {
      const r = await api('/api/config', cfg);
      S.cfg = r.config;
    } catch (e) { toast('設定を保存できませんでした: ' + e.message, true); return false; }
    refreshDetailOnly();
    return true;
  }
  // 再生を止めずに、右側の詳細だけを読み直す
  async function refreshDetailOnly() {
    const r = curRow();
    if (!r || !S.detail || S.detail.id !== r.id) return;
    const req = S.detailReq;
    let d;
    try { d = await api(`/api/item?season=${enc(S.season)}&id=${r.id}`); } catch (e) { return; }
    if (req !== S.detailReq) return;
    const memoEl = $('memo');
    if (memoEl && document.activeElement === memoEl) return;   // 入力中は書き換えない
    S.detail = d;
    renderDetail(d);
  }

  function syncDecideButtons() {
    const r = curRow();
    for (const b of document.querySelectorAll('#detail .decide button')) {
      b.classList.toggle('on', !!r && b.dataset.dec === r.dec);
    }
  }
  // 前後の確認用に出している動画では、判断、メモ、入力欄を変えられないようにする
  function syncLock() {
    const r = curRow();
    const cx = r && S.detail && S.detail.id === r.id ? S.ctx.get(r.id) : 0;
    const note = document.getElementById('ctxNote');
    if (note) {
      note.hidden = !cx;
      if (cx) {
        note.innerHTML = `${ctxTags(cx)}表示中の動画の${cx === 3 ? '前後' : cx === 1 ? '一つ前' : '一つ後'}にある動画です。確認用に表示しているので、判断とメモは変えられません（絞り込みを外すと変えられます）。`;
      }
    }
    for (const el of document.querySelectorAll('#detail .decide button, #detail #memo, #detail input[name="pick"], #addrow input')) {
      el.disabled = !!cx;
    }
  }

  function renderDetail(d) {
    const data = S.data;
    const hasCsvSource = data.sources.some((s) => s.type === 'csv');
    const needPick = !!(d.flags & (F.SIZE | F.DUP));
    let h = `<div class="dname"><small>${esc(d.station)}</small>${esc(d.stem)}</div><div class="dtags">${tagsHtml(d.flags)}</div>`;
    h += '<p class="ctxnote" id="ctxNote" hidden></p>';
    h += '<div class="decide">' +
      '<button data-dec="k" class="k">残す<kbd>K</kbd></button><button data-dec="d" class="d">削除する<kbd>D</kbd></button>' +
      '<button data-dec="e" class="e">例外<kbd>E</kbd></button><button data-dec="" class="u">未判断<kbd>U</kbd></button></div>';
    h += `<textarea id="memo" placeholder="メモ（判断の理由など）">${esc(d.memo)}</textarea>`;
    h += '<div class="dsec"><h4>保存場所ごとの状況</h4>';
    let csvInfo = null;
    for (const s of d.sources) {
      if (s.type === 'folder') {
        if (!s.files.length) {
          h += `<div class="dfile none"><div class="l1"><span class="sn">${esc(s.name)}</span><span>なし</span></div></div>`;
          continue;
        }
        for (const f of s.files) {
          const pv = s.id + ':' + f.name;
          h += `<div class="dfile"><div class="l1"><span class="sn">${esc(s.name)}</span><span class="fnm">${esc(f.name)}</span></div>` +
            `<div class="l2"><span>${fmtSize(f.size)}（${nf(f.size)} バイト）</span><span>更新 ${fmtTime(f.mtime)}</span></div>` +
            `<div class="l2"><button data-play="${esc(s.id)}" data-n="${f.n}">再生</button>` +
            `<button data-open="file" data-src="${esc(s.id)}" data-n="${f.n}">既定のプレーヤーで開く</button>` +
            `<button data-open="folder" data-src="${esc(s.id)}" data-n="${f.n}">フォルダを開く</button>` +
            (needPick ? `<label><input type="radio" name="pick" value="${esc(pv)}"${d.pick === pv ? ' checked' : ''}>これを正とする</label>` : '') +
            '</div></div>';
        }
      } else {
        csvInfo = s.csv;
        if (!s.csv) {
          h += `<div class="dfile none"><div class="l1"><span class="sn">${esc(s.name)}</span><span>行なし</span></div></div>`;
        } else {
          h += `<div class="dfile"><div class="l1"><span class="sn">${esc(s.name)}</span><span class="fnm">${esc(s.csv.file)}</span></div>` +
            `<div class="l2"><span>${nf(s.csv.n)} 行</span><span>DateTime ${esc(s.csv.dt || '（空欄）')}</span></div>`;
          const row = s.csv.rows && s.csv.rows[0];
          if (row && d.header.length) {
            h += '<details><summary class="muted">行の内容</summary><dl class="kv">' +
              d.header.map((name, i) => `<dt>${esc(name)}</dt><dd>${esc(row[i] == null ? '' : row[i])}</dd>`).join('') + '</dl></details>';
          }
          h += '</div>';
        }
      }
    }
    h += '</div>';
    // CSV にしかない動画: 「残す」としたときに付く印の説明
    const hasFile = d.sources.some((x) => x.type === 'folder' && x.files.length);
    const mk = data.csv && data.csv.mark;
    if (csvInfo && !hasFile && mk && mk.text) {
      h += '<div class="dsec"><h4>CSV にしかない動画</h4><p class="hint">' +
        (mk.col ? `「残す」と判断すると、更新後の CSV でこの行の ${esc(mk.col)} 列に「<b>${esc(mk.text)}</b>」と書きます（行は残ります）。`
          : `印を書く列（${esc(mk.cols.join('、') || '指定なし')}）が CSV にないため、「残す」と判断しても「${esc(mk.text)}」の印は付きません。`) +
        '「削除する」なら行を除き、「例外」と「未判断」では行をそのまま残します。</p></div>';
    }
    if (d.embedded && d.embedded.text) {
      h += '<div class="dsec"><h4>動画の日時</h4><dl class="kv"><dt>埋め込まれた撮影日時</dt><dd>' +
        (d.embedded.unreadable ? '動画を読めませんでした（保存場所の接続を確かめてください）'
          : d.embedded.text === 'NA' ? 'なし（この動画には記録されていません）'
            : `${esc(d.embedded.text)} <span class="muted">${esc(d.embedded.src)}</span>`) + '</dd>';
      if (d.mtime) {
        h += `<dt>ファイルの更新日時</dt><dd>${esc(d.mtime.text)} <span class="muted">${esc(d.mtime.src)}</span>` +
          (d.mtime.differs ? '<div class="muted">保存場所によって更新日時が違います（最も古いものを表示）</div>' : '') + '</dd>';
      }
      h += '</dl></div>';
    }
    if (hasCsvSource && !csvInfo && d.ref && data.csv) {
      const fixed = new Set(data.csv.fixed_cols);
      h += '<div class="dsec"><h4>CSV に足す行</h4>' +
        `<p class="hint">「残す」と判断すると、Season、Station、File（${esc(d.ref.name)}）を埋めた行が更新後の CSV に入ります。</p>`;
      if (data.csv.has_dt && d.fill) h += fillHint(d);
      const defs = data.csv.defaults || {};
      if (Object.keys(defs).length) {
        h += '<p class="hint">ほかの列は、空欄のままにすると薄い字の既定値が入ります（既定値は「保存場所と設定」で変えられます）。</p>';
      }
      h += '<div class="addrow" id="addrow">';
      data.csv.header.forEach((name, i) => {
        if (fixed.has(i)) return;
        const isDt = name.trim().toLowerCase() === 'datetime';
        const val = d.csv_fields[name] || '';
        const ph = isDt ? ((d.fill && d.fill.text) || 'NA') : (defs[i] || '');
        h += `<label>${esc(name)}</label><input type="text" data-col="${esc(name)}" value="${esc(val)}"` +
          (ph ? ` placeholder="${esc(ph)}"` : '') + '>';
      });
      h += '</div></div>';
    }
    $('detail').innerHTML = h;
    $('detail').classList.remove('stale');
    syncDecideButtons();
    syncLock();
  }

  $('detail').addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b || !S.detail) return;
    if (b.dataset.dec !== undefined) { decide(b.dataset.dec); return; }
    if (b.dataset.dtsrc) {
      if (await setDtSource(b.dataset.dtsrc)) toast('埋め込みがないときは、ファイルの更新日時を使う設定にしました');
      return;
    }
    if (b.dataset.play !== undefined) { playFile(b.dataset.play, Number(b.dataset.n), true); return; }
    if (b.dataset.open) {
      try {
        await api('/api/open', { season: S.season, id: S.detail.id, src: b.dataset.src, n: Number(b.dataset.n), mode: b.dataset.open });
      } catch (err) { toast(err.message, true); }
    }
  });
  // ボタンを押したあとも、キー操作が一覧に届くようにする
  for (const id of ['detail', 'toolbar', 'pbar', 'mjbar']) {
    $(id).addEventListener('click', (e) => { if (e.target.closest('button')) $('tbody').focus({ preventScroll: true }); });
  }
  function saveField(field, value, id) {
    return api('/api/field', { season: S.season, id, field, value }).catch((e) => toast('保存できませんでした: ' + e.message, true));
  }
  // 入力の保存は少し待ってから行う。別の動画へ移る前に、待っている保存は必ず済ませる。
  const pending = new Map();   // 'memo:12' -> { timer, run }
  function saveLater(field, value, id) {
    const key = field + ':' + id;
    const old = pending.get(key);
    if (old) clearTimeout(old.timer);
    const run = () => { pending.delete(key); if (field === 'memo') renderRows(); return saveField(field, value, id); };
    pending.set(key, { timer: setTimeout(run, 500), run });
  }
  function flushSaves() {
    const jobs = [];
    for (const p of Array.from(pending.values())) { clearTimeout(p.timer); jobs.push(p.run()); }
    return Promise.all(jobs);
  }
  window.addEventListener('beforeunload', flushSaves);
  $('detail').addEventListener('input', (e) => {
    if (!S.detail || S.ctx.has(S.detail.id)) return;
    const id = S.detail.id;
    if (e.target.id === 'memo') {
      const v = e.target.value;
      const r = S.byId.get(id);
      if (r) r.memo = v;
      saveLater('memo', v, id);
    } else if (e.target.dataset.col !== undefined) {
      const vals = {};
      for (const inp of document.querySelectorAll('#addrow input')) if (inp.value.trim()) vals[inp.dataset.col] = inp.value.trim();
      S.detail.csv_fields = vals;
      saveLater('csv_fields', vals, id);
    }
  });
  $('detail').addEventListener('focusout', flushSaves);
  $('detail').addEventListener('change', (e) => {
    if (!S.detail) return;
    if (e.target.name === 'pick' && !S.ctx.has(S.detail.id)) {
      S.detail.pick = e.target.value;
      saveField('pick', e.target.value, S.detail.id);
    }
  });

  // ------------------------------------------------------------ 再生
  const P = { kind: '', src: '', n: -1, req: 0 };
  const MJ = { info: null, base: 0, t0: 0, playing: false, raf: 0, busy: false, want: null, shown: -1 };

  function stageMsg(text) {
    const m = $('stageMsg');
    m.textContent = text || '';
    m.hidden = !text;
  }
  function stopPlayer() {
    P.req++;
    P.kind = '';
    MJ.playing = false;
    cancelAnimationFrame(MJ.raf);
    MJ.info = null; MJ.busy = false; MJ.want = null;
    const vid = $('vid');
    vid.onerror = null;
    try { vid.pause(); } catch (e) { /* 何もしない */ }
    if (vid.getAttribute('src')) { vid.removeAttribute('src'); vid.load(); }
    vid.hidden = true;
    $('cv').hidden = true;
    $('mjbar').hidden = true;
  }

  async function playFile(srcId, n, autoplay) {
    const d = S.detail;
    if (!d) return;
    const src = d.sources.find((s) => s.id === srcId);
    const f = src && src.files && src.files[n];
    if (!f) return;
    stopPlayer();
    const req = P.req;
    P.src = srcId; P.n = n;
    markSrcButtons();
    const qs = `season=${enc(S.season)}&id=${d.id}&src=${enc(srcId)}&n=${n}`;
    if (f.ext.toLowerCase() === 'avi') {
      stageMsg('読み込み中');
      let info;
      try { info = await api('/api/avinfo?' + qs); } catch (e) {
        if (req === P.req) stageMsg('再生できません: ' + e.message);
        return;
      }
      if (req !== P.req) return;
      if (info.mjpeg) { startMJ(info, autoplay); return; }
      if (info.ffmpeg) { startVideo('/transcode?' + qs, autoplay, qs, true); return; }
      stageMsg(`この AVI（${info.codec || '形式不明'}）はアプリ内で再生できません。「既定のプレーヤーで開く」を使うか、設定で ffmpeg の場所を指定してください。`);
      return;
    }
    startVideo('/media?' + qs, autoplay, qs, false);
  }

  function startVideo(url, autoplay, qs, isTranscode) {
    const vid = $('vid');
    const req = P.req;
    P.kind = 'video';
    stageMsg('');
    vid.hidden = false;
    vid.defaultPlaybackRate = S.speed;
    vid.src = url;
    vid.playbackRate = S.speed;
    vid.onerror = () => {
      if (req !== P.req) return;
      if (!isTranscode && S.ffmpeg) { startVideo('/transcode?' + qs, autoplay, qs, true); return; }
      vid.hidden = true;
      stageMsg('このブラウザでは再生できない形式です。「既定のプレーヤーで開く」を使ってください。');
    };
    if (autoplay) {
      const p = vid.play();
      if (p && p.catch) p.catch(() => { /* 自動再生が止められた場合は、再生ボタンで始める */ });
    }
  }
  $('vid').tabIndex = -1;
  $('vid').addEventListener('focus', () => $('tbody').focus({ preventScroll: true }));

  function mjPos() {
    if (!MJ.info) return 0;
    if (!MJ.playing) return MJ.base;
    const p = MJ.base + (performance.now() - MJ.t0) / 1000 * MJ.info.fps * S.speed;
    return Math.min(MJ.info.n - 1, Math.floor(p));
  }
  function mjDraw(i) {
    const info = MJ.info;
    if (!info) return;
    if (MJ.busy) { MJ.want = i; return; }
    MJ.busy = true;
    const img = new Image();
    img.onload = () => {
      if (MJ.info !== info) return;
      MJ.busy = false;
      const cv = $('cv');
      if (cv.dataset.auto === '1') {
        const sc = Math.min(1, 1280 / img.naturalWidth);
        cv.width = Math.round(img.naturalWidth * sc); cv.height = Math.round(img.naturalHeight * sc);
        cv.dataset.auto = '0';
      }
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      MJ.shown = i;
      MJ.errors = 0;
      if (MJ.want !== null && MJ.want !== i) { const w = MJ.want; MJ.want = null; mjDraw(w); } else MJ.want = null;
    };
    img.onerror = () => {
      if (MJ.info !== info) return;
      MJ.busy = false; MJ.want = null;
      MJ.errors = (MJ.errors || 0) + 1;
      if (MJ.errors >= 5) {        // 続けて読めない: 接続が切れた、ファイルが動かされた、など
        const at = mjPos();
        MJ.playing = false; cancelAnimationFrame(MJ.raf); MJ.base = at; mjUI(at);
        stageMsg('動画を読めなくなりました。保存場所の接続を確かめて、もう一度選び直してください。');
        $('cv').hidden = true;
      }
    };
    img.src = `/frame?t=${info.token}&i=${i}`;
  }
  function mjUI(p) {
    const info = MJ.info;
    if (!info) return;
    $('mjSeek').value = String(p);
    $('mjTime').textContent = `${(p / info.fps).toFixed(1)} / ${(info.n / info.fps).toFixed(1)} 秒`;
    $('mjPlay').textContent = MJ.playing ? '停止' : '再生';
  }
  function mjLoop() {
    if (!MJ.playing || !MJ.info) return;
    const p = mjPos();
    if (p !== MJ.shown) mjDraw(p);
    if (p >= MJ.info.n - 1) { MJ.base = MJ.info.n - 1; MJ.playing = false; mjUI(MJ.base); return; }
    mjUI(p);
    MJ.raf = requestAnimationFrame(mjLoop);
  }
  function mjPlay() {
    if (!MJ.info) return;
    if (MJ.base >= MJ.info.n - 1) MJ.base = 0;
    MJ.t0 = performance.now();
    MJ.playing = true;
    mjLoop();
  }
  function mjPause() {
    if (!MJ.info) return;
    MJ.base = mjPos();
    MJ.playing = false;
    cancelAnimationFrame(MJ.raf);
    mjDraw(MJ.base);
    mjUI(MJ.base);
  }
  function mjSeekTo(i) {
    if (!MJ.info) return;
    MJ.base = Math.max(0, Math.min(MJ.info.n - 1, Math.round(i)));
    MJ.t0 = performance.now();
    mjDraw(MJ.base);
    mjUI(MJ.base);
  }
  function startMJ(info, autoplay) {
    P.kind = 'mj';
    MJ.info = info; MJ.base = 0; MJ.shown = -1; MJ.busy = false; MJ.want = null; MJ.playing = false; MJ.errors = 0;
    const cv = $('cv');
    if (info.width && info.height) {
      const sc = Math.min(1, 1280 / info.width);
      cv.width = Math.max(2, Math.round(info.width * sc)); cv.height = Math.max(2, Math.round(info.height * sc));
      cv.dataset.auto = '0';
    } else cv.dataset.auto = '1';
    cv.hidden = false;
    stageMsg('');
    $('mjSeek').max = String(Math.max(0, info.n - 1));
    $('mjbar').hidden = false;
    mjDraw(0);
    if (autoplay) mjPlay(); else mjUI(0);
  }
  $('mjPlay').addEventListener('click', () => { if (MJ.playing) mjPause(); else mjPlay(); });
  $('mjSeek').addEventListener('input', (e) => mjSeekTo(Number(e.target.value)));
  $('cv').addEventListener('click', () => { if (MJ.playing) mjPause(); else mjPlay(); });

  function togglePlay() {
    if (P.kind === 'mj') { if (MJ.playing) mjPause(); else mjPlay(); } else if (P.kind === 'video') {
      const vid = $('vid');
      if (vid.paused) { const p = vid.play(); if (p && p.catch) p.catch(() => {}); } else vid.pause();
    }
  }
  function seekBy(sec) {
    if (P.kind === 'mj' && MJ.info) mjSeekTo(mjPos() + sec * MJ.info.fps);
    else if (P.kind === 'video') { const vid = $('vid'); if (isFinite(vid.duration)) vid.currentTime = Math.max(0, Math.min(vid.duration, vid.currentTime + sec)); }
  }
  function setSpeed(sp) {
    if (P.kind === 'mj' && MJ.playing) { MJ.base = mjPos(); MJ.t0 = performance.now(); }
    S.speed = sp;
    const vid = $('vid');
    vid.defaultPlaybackRate = sp;
    vid.playbackRate = sp;
    for (const b of $('speedBtns').children) b.classList.toggle('on', Number(b.dataset.speed) === sp);
  }
  $('speedBtns').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setSpeed(Number(b.dataset.speed)); });

  // ------------------------------------------------------------ 絞り込みの操作
  $('fMismatch').addEventListener('change', (e) => {
    S.filt.mismatch = e.target.checked;
    if (e.target.checked) { S.filt.states = 0; if (S.filt.mask === S.full) S.filt.mask = null; }
    applyFilters();
  });
  $('fUndecided').addEventListener('change', (e) => { S.filt.undecided = e.target.checked; applyFilters(); });
  $('fContext').addEventListener('change', (e) => {
    S.showCtx = e.target.checked;
    try { localStorage.setItem('tm.ctx', S.showCtx ? '1' : '0'); } catch (err) { /* 保存できなくても動作は変えない */ }
    applyFilters();
  });
  $('pairs').addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (!b) return;
    const p = b.dataset.pair.split(',').map(Number);
    S.filt.pair = (S.filt.pair && S.filt.pair[0] === p[0] && S.filt.pair[1] === p[1]) ? null : p;
    applyFilters();
  });
  $('states').addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (!b) return;
    S.filt.states ^= Number(b.dataset.state);
    if (S.filt.states) S.filt.mismatch = false;   // 状態を選んだら「不一致のみ」は外す（一致や拡張子違いも見られるように）
    applyFilters();
  });
  $('patterns').addEventListener('click', (e) => {
    const b = e.target.closest('.pat');
    if (!b) return;
    const m = Number(b.dataset.mask);
    S.filt.mask = S.filt.mask === m ? null : m;
    if (S.filt.mask === S.full) S.filt.mismatch = false;
    applyFilters();
  });
  $('stationList').addEventListener('click', (e) => {
    const b = e.target.closest('.st');
    if (!b) return;
    const i = Number(b.dataset.st);
    S.filt.station = i < 0 || S.filt.station === i ? null : i;
    applyFilters();
  });
  let searchTimer = 0;
  $('fSearch').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    const v = e.target.value;
    searchTimer = setTimeout(() => { S.filt.q = v; applyFilters(); }, 120);
  });
  $('btnClear').addEventListener('click', () => {
    S.filt = { mismatch: false, undecided: false, pair: null, states: 0, mask: null, station: null, q: '' };
    $('fSearch').value = '';
    applyFilters();
  });

  // ------------------------------------------------------------ 走査
  function afterJob(j) {
    if (j.state === 'error') {
      openModal(`<h2>処理を完了できませんでした</h2><div class="errbox">${esc(j.error || '')}</div><div class="mfoot"><button class="primary" data-close>閉じる</button></div>`, true)
        .querySelector('[data-close]').addEventListener('click', closeModal);
      return;
    }
    if (j.kind === 'scan') { refreshSeasons(); loadSeason((j.result && j.result.season) || j.season); }
  }
  // 入力された Season 名（全角の英数字は半角にそろえる）
  function seasonText() {
    const v = $('season').value.normalize('NFKC').trim();
    if (v !== $('season').value) $('season').value = v;
    return v;
  }
  async function startScan() {
    const season = seasonText();
    if (!season) { toast('Season を選ぶか入力してください', true); $('season').focus(); return; }
    if (!S.cfg.sources.length) { openSettings(); return; }
    await flushSaves();      // 入力中のメモなどを先に保存する
    try {
      await api('/api/scan', { season });
    } catch (e) {
      openModal(`<h2>走査を始められません</h2><div class="errbox">${esc(e.message)}</div><div class="mfoot"><button class="primary" data-close>閉じる</button></div>`, true)
        .querySelector('[data-close]').addEventListener('click', closeModal);
      return;
    }
    // 別の Season を走査するときは、いまの一覧を消す（古い一覧に付けた判断が、別の Season に届かないように）
    if (S.data && S.data.season.toLowerCase() !== season.toLowerCase()) {
      clearData();
      showEmpty('走査中です。終わると一覧が出ます。');
    } else if (!S.data) showEmpty('走査中です。終わると一覧が出ます。');
    setScanning(true);
    watchJob(afterJob);
  }
  $('btnScan').addEventListener('click', startScan);
  $('season').addEventListener('change', () => { const v = seasonText(); if (v && v !== S.season) loadSeason(v); });
  $('season').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); $('season').blur(); const v = seasonText(); if (v) loadSeason(v); }
  });

  // ------------------------------------------------------------ 設定
  // CSV に足す行の既定値を、設定の窓で編集する文字列と行き来させる
  function defsToText(defs) {
    return Object.entries(defs || {}).map(([k, v]) => `${k}=${v}`).join('\n');
  }
  function textToDefs(text) {
    const defs = {}, bad = [];
    for (const raw of String(text || '').split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const at = line.search(/[=＝]/);
      const name = at < 0 ? '' : line.slice(0, at).trim();
      if (!name) { bad.push(line); continue; }
      const val = line.slice(at + 1).trim();
      if (val) defs[name] = val;   // 値が空の行は「既定値なし（空欄）」
    }
    return { defs, bad };
  }
  function openSettings() {
    const draft = JSON.parse(JSON.stringify(S.cfg));
    draft.settings.add_defaults_text = defsToText(draft.settings.add_defaults);
    let browsing = null; // {index, mode}

    function render() {
      const st = draft.settings;
      const folderOpts = draft.sources.filter((s) => s.type === 'folder')
        .map((s) => `<option value="${esc(s.id)}"${st.ext_ref === s.id ? ' selected' : ''}>${esc(s.name)}</option>`).join('');
      let rows = '';
      draft.sources.forEach((s, i) => {
        rows += `<tr data-i="${i}"><td><input type="checkbox" data-k="enabled"${s.enabled ? ' checked' : ''} title="照合に使う"></td>` +
          `<td style="width:110px"><input type="text" data-k="name" value="${esc(s.name)}"></td>` +
          `<td>${s.type === 'csv' ? 'CSV' : 'フォルダ'}</td>` +
          `<td><div class="pathline"><input type="text" data-k="path" value="${esc(s.path)}" spellcheck="false" placeholder="${s.type === 'csv' ? 'CSV ファイルの場所（{Season} と書くと Season 名に置き換わります）' : 'PRM_N に当たる親フォルダ（例: E:\\PRM_N）'}"><button data-act="browse">選ぶ</button></div></td>` +
          `<td class="act"><button data-act="up"${i ? '' : ' disabled'}>上へ</button><button data-act="down"${i < draft.sources.length - 1 ? '' : ' disabled'}>下へ</button><button data-act="del">外す</button></td></tr>`;
      });
      const box = openModal(
        '<h2>保存場所と設定</h2>' +
        '<p class="muted">照合する保存場所を登録します。フォルダは「親フォルダ / Season / Station / 動画」の階層の、親フォルダを指定してください。上にあるものほど、再生とコピー元の優先順が高くなります。</p>' +
        `<table class="grid"><tr><th>使う</th><th>名前</th><th>種類</th><th>場所</th><th></th></tr>${rows || '<tr><td colspan="5" class="muted">まだ登録されていません</td></tr>'}</table>` +
        '<div class="setrow"><button data-act="addfolder">フォルダを追加</button><button data-act="addcsv">CSV を追加</button></div>' +
        '<h3>照合</h3>' +
        `<label class="setrow"><input type="checkbox" id="setIgnore"${st.ignore_ext ? ' checked' : ''}>拡張子を無視して照合する（AVI と MP4 を同じ動画とみなす）</label>` +
        `<label class="setrow">対象とする拡張子<input type="text" id="setExts" value="${esc(st.exts.join(', '))}" style="width:200px"></label>` +
        '<h3>CSV</h3>' +
        `<label class="setrow">拡張子をそろえる基準にする保存場所<select id="setExtRef"><option value="auto">自動（上にある保存場所を優先）</option>${folderOpts}</select></label>` +
        `<label class="setrow">埋め込まれた撮影日時の解釈<select id="setTz"><option value="asis"${st.embedded_tz === 'asis' ? ' selected' : ''}>記録されたままの時刻を使う</option><option value="utc"${st.embedded_tz === 'utc' ? ' selected' : ''}>UTC とみなして PC の時刻帯に直す（MP4 のみ）</option></select></label>` +
        `<label class="setrow">CSV にしかない動画を「残す」とした行に書く印<input type="text" id="setMarkText" value="${esc(st.mark_text || '')}" style="width:140px">書く列<input type="text" id="setMarkCols" value="${esc((st.mark_cols || []).join(', '))}" style="width:200px" spellcheck="false"></label>` +
        '<p class="hint">書く列は、左から順に CSV の見出しと照らし、最初に見つかった列を使います。空欄と NA は印に置き換え、ほかの文字が入っている行では後ろに足します。印を空にすると、付けません。</p>' +
        '<label class="setcol">CSV に足す行の既定値（1 行に 1 列、「列名=値」の形。ここにない列は空欄になります）' +
        `<textarea id="setAddDefs" rows="6" spellcheck="false">${esc(st.add_defaults_text)}</textarea></label>` +
        '<p class="hint">値に {Season} と書くと Season 名に置き換わります。Season、Station、File、DateTime は自動で入るため、ここでは指定しません。CSV にない列名は無視されます。</p>' +
        '<h3>再生と操作</h3>' +
        `<label class="setrow"><input type="checkbox" id="setAutoplay"${st.autoplay ? ' checked' : ''}>動画を選んだら自動で再生する</label>` +
        `<label class="setrow"><input type="checkbox" id="setAutoNext"${st.auto_next ? ' checked' : ''}>判断を付けたら次の未判断の動画へ進む</label>` +
        `<label class="setrow">ffmpeg の場所（任意）<input type="text" id="setFfmpeg" value="${esc(st.ffmpeg)}" style="flex:1;min-width:220px" spellcheck="false"><span class="muted">${S.ffmpeg ? '見つかっています' : '見つかっていません（MJPEG の AVI と MP4 は ffmpeg なしで再生できます）'}</span></label>` +
        `<p class="hint">設定と作業状態の保存先: ${esc(S.dataDir)}</p>` +
        '<div class="mfoot"><button data-act="cancel">キャンセル</button><button class="primary" data-act="save">保存する</button></div>');
      box.onclick = onClick;
      box.oninput = onInput;
    }

    function collect() {
      const st = draft.settings;
      const g = (id) => document.getElementById(id);
      if (!g('setIgnore')) return;
      st.ignore_ext = g('setIgnore').checked;
      st.exts = g('setExts').value.split(/[\s,;、]+/).filter(Boolean);
      st.ext_ref = g('setExtRef').value;
      st.embedded_tz = g('setTz').value;
      st.add_defaults_text = g('setAddDefs').value;
      st.mark_text = g('setMarkText').value.trim();
      st.mark_cols = g('setMarkCols').value.split(/[,;、]+/).map((x) => x.trim()).filter(Boolean);
      st.autoplay = g('setAutoplay').checked;
      st.auto_next = g('setAutoNext').checked;
      st.ffmpeg = g('setFfmpeg').value;
    }
    function onInput(e) {
      const tr = e.target.closest('tr[data-i]');
      const k = e.target.dataset.k;
      if (!tr || !k) return;
      const s = draft.sources[Number(tr.dataset.i)];
      s[k] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    }
    async function onClick(e) {
      const b = e.target.closest('button');
      if (!b) return;
      const act = b.dataset.act;
      if (!act) return;
      if (browsing) { onBrowseClick(b); return; }
      const tr = b.closest('tr[data-i]');
      const i = tr ? Number(tr.dataset.i) : -1;
      collect();
      if (act === 'addfolder' || act === 'addcsv') {
        const isCsv = act === 'addcsv';
        const n = draft.sources.filter((s) => s.type === (isCsv ? 'csv' : 'folder')).length;
        draft.sources.push({ id: 's' + Math.random().toString(16).slice(2, 10), name: isCsv ? 'CSV' : (['HDD', 'NAS', 'NAS2', 'HDD2'][n] || 'フォルダ' + (n + 1)), type: isCsv ? 'csv' : 'folder', path: '', enabled: true, readonly: false });
        render();
      } else if (act === 'del') { draft.sources.splice(i, 1); render(); }
      else if (act === 'up' && i > 0) { draft.sources.splice(i - 1, 0, draft.sources.splice(i, 1)[0]); render(); }
      else if (act === 'down' && i < draft.sources.length - 1) { draft.sources.splice(i + 1, 0, draft.sources.splice(i, 1)[0]); render(); }
      else if (act === 'browse') { browsing = { index: i, mode: draft.sources[i].type === 'csv' ? 'csv' : 'folder' }; showBrowse(draft.sources[i].path || ''); }
      else if (act === 'cancel') closeModal();
      else if (act === 'save') {
        const parsed = textToDefs(draft.settings.add_defaults_text);
        if (parsed.bad.length) {
          toast('既定値の行を「列名=値」の形にしてください: ' + parsed.bad[0], true);
          return;
        }
        draft.settings.add_defaults = parsed.defs;
        delete draft.settings.add_defaults_text;
        try {
          const r = await api('/api/config', draft);
          S.cfg = r.config; S.ffmpeg = r.ffmpeg;
          closeModal();
          toast('設定を保存しました');
          refreshSeasons();
          if (S.data) loadSeason(S.season);
          else showEmpty('Season を選んで「走査」を押してください。');
        } catch (err) { toast('保存できませんでした: ' + err.message, true); }
      }
    }

    async function showBrowse(path) {
      let r;
      try { r = await api(`/api/browse?mode=${browsing.mode}&path=${enc(path)}`); } catch (e) {
        try { r = await api(`/api/browse?mode=${browsing.mode}&path=`); } catch (e2) { toast(e2.message, true); browsing = null; render(); return; }
      }
      const isCsv = browsing.mode === 'csv';
      let list = '';
      if (r.parent !== null) list += `<button data-act="go" data-path="${esc(r.parent)}">上のフォルダへ</button>`;
      for (const d of r.dirs) list += `<button data-act="go" data-path="${esc(d.path)}">${esc(d.name)}</button>`;
      for (const f of r.files) list += `<button class="file" data-act="pick" data-path="${esc(f.path)}">${esc(f.name)}</button>`;
      const box = openModal(
        `<h2>${isCsv ? 'CSV ファイルを選ぶ' : 'フォルダを選ぶ'}</h2>` +
        `<div class="pathline"><input type="text" id="brPath" value="${esc(r.path)}" spellcheck="false" placeholder="場所を貼り付けて Enter でも移動できます"><button data-act="jump">移動</button></div>` +
        `<div class="blist">${list || '<div class="muted" style="padding:10px">ここには何もありません</div>'}</div>` +
        '<div class="mfoot"><button data-act="back">戻る</button>' +
        (isCsv ? '' : `<button class="primary" data-act="pick" data-path="${esc(r.path)}"${r.path ? '' : ' disabled'}>このフォルダを選ぶ</button>`) + '</div>');
      box.onclick = onClick;
      box.oninput = null;
      const inp = document.getElementById('brPath');
      inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); showBrowse(inp.value.trim()); } });
    }
    function onBrowseClick(b) {
      const act = b.dataset.act;
      if (act === 'go') showBrowse(b.dataset.path);
      else if (act === 'jump') showBrowse(document.getElementById('brPath').value.trim());
      else if (act === 'pick') { draft.sources[browsing.index].path = b.dataset.path; browsing = null; render(); }
      else if (act === 'back') { browsing = null; render(); }
    }

    render();
  }
  $('btnSettings').addEventListener('click', openSettings);

  // ------------------------------------------------------------ 集計
  function openSummary() {
    const d = S.data;
    if (!d) { toast('先に Season を走査してください'); return; }
    const info = d.info || {};
    const c = { ok: 0, okExt: 0, needs: 0, und: 0, k: 0, dd: 0, e: 0 };
    const sc = new Map(STATE_DEFS.map((s) => [s[0], 0]));
    for (const r of S.rows) {
      if (r.flags & NEEDS) { c.needs++; if (!r.dec) c.und++; } else { c.ok++; if (r.flags & F.EXTDIFF) c.okExt++; }
      if (r.dec === 'k') c.k++; else if (r.dec === 'd') c.dd++; else if (r.dec === 'e') c.e++;
      for (const s of STATE_DEFS) if (r.flags & s[0]) sc.set(s[0], sc.get(s[0]) + 1);
    }
    let h = `<h2>集計 ${esc(d.season)}</h2><p class="muted">走査 ${esc(d.scanned_at)}</p>`;
    h += '<h3>保存場所</h3><table class="grid"><tr><th>名前</th><th>場所</th><th class="n">動画（CSV は行）</th><th class="n">Station</th><th class="n">対象外のファイル</th></tr>';
    for (const s of d.sources) {
      const miss = (info.missing_season || []).includes(s.id);
      h += `<tr><td>${esc(s.name)}</td><td style="word-break:break-all">${esc(s.path)}${miss ? ' <span class="tag miss">Season フォルダなし</span>' : ''}</td>` +
        `<td class="n">${nf((info.files || {})[s.id])}</td><td class="n">${s.type === 'folder' ? nf((info.stations || {})[s.id]) : ''}</td>` +
        `<td class="n">${s.type === 'folder' ? nf((info.nontarget || {})[s.id]) : nf(d.csv ? d.csv.nontarget : 0)}</td></tr>`;
    }
    h += '</table>';
    h += '<h3>照合の結果</h3><table class="grid">' +
      `<tr><td>動画（照合キー）の総数</td><td class="n">${nf(S.rows.length)}</td></tr>` +
      `<tr><td>一致（判断不要）</td><td class="n">${nf(c.ok)}</td></tr>` +
      `<tr><td>　うち拡張子だけが違うもの</td><td class="n">${nf(c.okExt)}</td></tr>` +
      `<tr><td>判断が必要</td><td class="n">${nf(c.needs)}</td></tr>`;
    for (const s of STATE_DEFS) h += `<tr><td>　${s[1]}</td><td class="n">${nf(sc.get(s[0]))}</td></tr>`;
    h += '</table><p class="hint">1 本の動画が複数の状態に当てはまることがあります（欠けとサイズ違いなど）。</p>';
    h += '<h3>判断の状況</h3><table class="grid">' +
      `<tr><td>未判断（判断が必要なもののうち）</td><td class="n">${nf(c.und)}</td></tr>` +
      `<tr><td>残す</td><td class="n">${nf(c.k)}</td></tr><tr><td>削除する</td><td class="n">${nf(c.dd)}</td></tr>` +
      `<tr><td>例外として残す</td><td class="n">${nf(c.e)}</td></tr></table>`;
    if (d.csv) {
      h += '<h3>CSV</h3><table class="grid">' +
        `<tr><td>CSV 全体の行数</td><td class="n">${nf(d.csv.records)}</td></tr>` +
        `<tr><td>この Season の行数（照合の対象）</td><td class="n">${nf(d.csv.rows)}</td></tr>` +
        `<tr><td>Station か File が空欄の行（対象外）</td><td class="n">${nf(d.csv.blank)}</td></tr>` +
        `<tr><td>動画でない拡張子の行（対象外）</td><td class="n">${nf(d.csv.nontarget)}</td></tr>` +
        `<tr><td>DateTime 列</td><td class="n">${d.csv.has_dt ? 'あり' : 'なし'}</td></tr></table>`;
    }
    const ex = info.exceptions || [];
    if (ex.length) {
      const kind = { season: 'Season の直下にある動画', subdir: 'Station の中のフォルダにある動画', error: '読めなかったフォルダ' };
      h += `<h3>想定の階層にない動画（${nf(ex.length)} か所。照合の対象外）</h3><table class="grid"><tr><th>保存場所</th><th>種類</th><th>場所</th><th class="n">本数</th></tr>`;
      for (const x of ex.slice(0, 60)) {
        const s = d.sources.find((y) => y.id === x.src);
        h += `<tr><td>${esc(s ? s.name : '')}</td><td>${kind[x.kind] || ''}</td><td style="word-break:break-all">${esc(x.path)}${x.note ? ' ' + esc(x.note) : ''}</td><td class="n">${nf(x.count)}</td></tr>`;
      }
      h += '</table>';
    }
    if (d.gone) h += `<p class="hint">以前の走査にあって今回見つからなかった動画の判断が ${nf(d.gone)} 件、記録として残っています。</p>`;
    h += '<div class="mfoot"><button class="primary" data-close>閉じる</button></div>';
    openModal(h).querySelector('[data-close]').addEventListener('click', closeModal);
  }
  $('btnSummary').addEventListener('click', openSummary);

  // ------------------------------------------------------------ 書き出し
  function resultHtml(title, path, extra) {
    return `<div class="result"><b>${esc(title)}</b>${extra || ''}<div class="path">${esc(path)}</div>` +
      `<button class="link" data-openpath="${esc(path)}">保存先のフォルダを開く</button></div>`;
  }
  // 書き出しの窓: 追加する行の DateTime の取得元と、その根拠
  function dtSourceHtml(counts) {
    const src = counts.dt_source || 'embedded';
    const p = counts.dt_profile;
    let h = '<label class="setrow">追加する行の DateTime<select id="exDtSrc">' +
      `<option value="embedded"${src === 'embedded' ? ' selected' : ''}>動画に埋め込まれた撮影日時のみ（なければ NA）</option>` +
      `<option value="embedded_mtime"${src === 'embedded_mtime' ? ' selected' : ''}>埋め込まれた撮影日時、なければファイルの更新日時</option>` +
      '</select></label>';
    h += '<p class="hint">書式は CSV の既存の行に合わせます。' +
      (p && p.minute_only ? 'この Season の既存の DateTime は分単位（秒が 00）なので、追加する値も秒を 00 に切り捨てます。' : '') + '</p>';
    if (p && p.mtime && p.mtime.n) {
      const m = p.mtime;
      const pct = (100 * m.match / m.n).toFixed(m.match === m.n ? 0 : 1);
      h += `<p class="hint">ファイルの更新日時と既存の DateTime の比較（CSV に行がある ${nf(m.n)} 本）: ` +
        `一致 ${nf(m.match)} 本（${pct}%）、2 分以内の差 ${nf(m.near)} 本、それ以上の差 ${nf(m.differ)} 本。` +
        (m.disagree ? `保存場所のあいだで更新日時が違う動画が ${nf(m.disagree)} 本あります（最も古いものを使います）。` : '') + '</p>';
      if (m.match < 0.95 * m.n) {
        h += '<div class="warnbox">この Season では、ファイルの更新日時が既存の DateTime と合わない動画が少なくありません。' +
          'コピーや変換で更新日時が変わっている可能性があるため、更新日時を使う場合は書き出した値を確かめてください。</div>';
      }
    }
    return h;
  }

  // 書き出しの窓: CSV にしかない動画を「残す」とした行に付ける印
  function markRowHtml(counts) {
    const m = counts.mark_info;
    if (!m || !m.text) return '';      // 印は設定で無効
    if (!m.col) {
      return `<label class="setrow"><input type="checkbox" id="exMark" disabled>CSV にしかない動画を「残す」とした行に「${esc(m.text)}」と書く<b>${nf(counts.mark)} 本</b></label>` +
        `<p class="hint">印を書く列（${esc(m.cols.join('、') || '指定なし')}）が CSV にないため、印は付きません。書く列は「保存場所と設定」で変えられます。</p>`;
    }
    return `<label class="setrow"><input type="checkbox" id="exMark" checked>CSV にしかない動画を「残す」とした行の ${esc(m.col)} 列に「${esc(m.text)}」と書く<b>${nf(counts.mark)} 本、${nf(counts.mark_rows)} 行</b></label>`;
  }
  // 書き出しの窓: 追加する行の、DateTime 以外の列に入る値
  function addDefaultsHtml(counts) {
    const list = counts.add_defaults || [];
    if (!list.length) return '<p class="hint">追加する行のほかの列は空欄になります（既定値は「保存場所と設定」で指定できます）。</p>';
    return '<p class="hint">追加する行のほかの列に入る既定値: ' +
      list.map(([k, v]) => `${esc(k)} <b>${esc(v)}</b>`).join('、') +
      '。ここにない列は空欄です。行ごとに入力した値があれば、そちらを使います。</p>';
  }

  async function openExport() {
    const d = S.data;
    if (!d) { toast('先に Season を走査してください'); return; }
    let counts = null, cerr = '', all = null;
    if (d.csv) {
      try { counts = await api('/api/export', { season: S.season, kind: 'csv_counts' }); } catch (e) { cerr = e.message; }
    }
    try { all = await api('/api/export', { season: S.season, kind: 'all_counts' }); } catch (e) { all = null; }
    let h = `<h2>書き出し ${esc(d.season)}</h2><p class="muted">どの書き出しも、保存場所のファイルと元の CSV を変更しません。ファイルは作業用の保存先に作られます。</p>`;
    h += '<h3>照合結果の一覧</h3><p>全動画の所在、状態、判断、メモを CSV にします。</p><div class="setrow"><button data-ex="result">一覧を書き出す</button></div><div id="exResult"></div>';
    h += '<h3>更新後の CSV</h3>';
    if (!d.csv) h += '<p class="muted">CSV が保存場所に登録されていないため、使えません。</p>';
    else if (!counts) h += `<div class="errbox">${esc(cerr)}</div>`;
    else {
      h += '<p>判断と照合の結果を反映した CSV を、別の名前で書き出します。変更しない行は元のまま書き写します。</p>' +
        `<label class="setrow"><input type="checkbox" id="exAdd" checked>行を追加する（「残す」と判断した、CSV にない動画）<b>${nf(counts.add)} 本</b></label>` +
        `<label class="setrow"><input type="checkbox" id="exRemove" checked>行を削除する（「削除する」と判断した動画）<b>${nf(counts.remove)} 本、${nf(counts.remove_rows)} 行</b></label>` +
        `<label class="setrow"><input type="checkbox" id="exExt" checked>File の拡張子を実ファイルにそろえる<b>${nf(counts.ext)} 本、${nf(counts.ext_rows)} 行</b></label>` +
        markRowHtml(counts) +
        (counts.has_dt ? dtSourceHtml(counts)
          : '<p class="hint">この CSV には DateTime 列がないため、撮影日時は入りません。</p>') +
        addDefaultsHtml(counts) +
        '<div class="setrow"><button data-ex="csv">更新後の CSV を書き出す</button>' +
        (counts.has_dt ? '<button data-ex="dtcheck">埋め込みの日時を既存の DateTime と突き合わせる</button>' : '') + '</div><div id="exCsv"></div>';
    }
    h += '<h3>実行計画（確認用）</h3><p>判断どおりにそろえるために必要なコピーと退避の一覧です。この版は実行しません。</p>' +
      '<div class="setrow"><button data-ex="plan">実行計画を書き出す</button></div><div id="exPlan"></div>';
    if (all && all.seasons.length) {
      const t = all.totals;
      h += '<h3>全 Season をまとめて書き出す</h3>' +
        `<p>走査済みの ${nf(all.seasons.length)} Season（全 ${nf(t.videos)} 本）について、不一致の動画と判断を 1 つの CSV にまとめます。` +
        `いま不一致は ${nf(t.mismatch)} 本で、うち未判断は ${nf(t.undecided)} 本です。Season ごとの集計も一緒に作ります。</p>` +
        '<label class="setrow"><input type="checkbox" id="exAllEvery">一致している動画も含める（すべての動画を書き出す）</label>' +
        '<div class="setrow"><button data-ex="all">全 Season をまとめて書き出す</button></div><div id="exAll"></div>';
    }
    h += '<div class="mfoot"><button class="primary" data-close>閉じる</button></div>';
    const box = openModal(h);
    box.onchange = (e) => { if (e.target.id === 'exDtSrc') setDtSource(e.target.value); };
    box.onclick = async (e) => {
      const t = e.target;
      if (t.dataset.close !== undefined) { closeModal(); return; }
      if (t.dataset.openpath) { api('/api/openpath', { path: t.dataset.openpath }).catch((err) => toast(err.message, true)); return; }
      const ex = t.dataset.ex;
      if (!ex) return;
      try {
        if (ex === 'result') {
          const r = await api('/api/export', { season: S.season, kind: 'result' });
          document.getElementById('exResult').innerHTML = resultHtml(`${nf(r.rows)} 本を書き出しました`, r.path);
        } else if (ex === 'plan') {
          const r = await api('/api/export', { season: S.season, kind: 'plan' });
          const sm = r.summary;
          let x = '<table class="grid" style="margin:6px 0">';
          for (const [name, v] of Object.entries(sm.copy)) x += `<tr><td>${esc(name)} へコピー</td><td class="n">${nf(v[0])} 本、${fmtSize(v[1])}</td></tr>`;
          for (const [name, v] of Object.entries(sm.quarantine)) x += `<tr><td>${esc(name)} から退避</td><td class="n">${nf(v[0])} 本、${fmtSize(v[1])}</td></tr>`;
          x += `<tr><td>CSV に行を追加</td><td class="n">${nf(sm.csv_add)} 本</td></tr><tr><td>CSV の行を削除</td><td class="n">${nf(sm.csv_remove)} 本</td></tr>` +
            `<tr><td>CSV の拡張子を更新</td><td class="n">${nf(sm.csv_ext)} 本</td></tr>` +
            (sm.csv_mark ? `<tr><td>CSV の行に印を付ける（CSV にしかない動画）</td><td class="n">${nf(sm.csv_mark)} 本</td></tr>` : '') +
            `<tr><td>手作業で確認</td><td class="n">${nf(sm.manual)} 本</td></tr>` +
            `<tr><td>未判断のまま</td><td class="n">${nf(sm.undecided)} 本</td></tr></table>`;
          document.getElementById('exPlan').innerHTML = resultHtml(`${nf(r.rows)} 件の作業を書き出しました`, r.path, x);
        } else if (ex === 'all') {
          const scope = document.getElementById('exAllEvery').checked ? 'all' : 'mismatch';
          await api('/api/export', { season: S.season, kind: 'all', options: { scope } });
          for (const b of box.querySelectorAll('[data-ex]')) b.disabled = true;
          document.getElementById('exAll').innerHTML = '<p class="hint" id="exAllProg">処理中</p>';
          watchJob((j) => {
            for (const b of box.querySelectorAll('[data-ex]')) b.disabled = false;
            const out = document.getElementById('exAll');
            if (!out) return;
            if (j.state === 'error') { out.innerHTML = `<div class="errbox">${esc(j.error || '')}</div>`; return; }
            const r = j.result;
            out.innerHTML = resultHtml(`${nf(r.seasons)} Season、${nf(r.rows)} 本を書き出しました`, r.path,
              `<div>${r.scope === 'all' ? 'すべての動画' : '不一致の動画と、判断またはメモのある動画'}を書き出しました。</div>`) +
              resultHtml('Season ごとの集計', r.summary_path) +
              (r.empty && r.empty.length ? `<p class="hint">動画が 1 本もない Season があります（${esc(r.empty.join('、'))}）。Season 名の入力違いで走査した可能性があります。</p>` : '') +
              (r.skipped.length ? `<div class="warnbox">読めなかった Season: ${esc(r.skipped.map((x) => x.season).join('、'))}</div>` : '');
          }, (text) => { const el = document.getElementById('exAllProg'); if (el) el.textContent = text; });
        } else if (ex === 'csv' || ex === 'dtcheck') {
          const body = { season: S.season, kind: ex };
          if (ex === 'csv') { const mk = document.getElementById('exMark'); body.options = { add: document.getElementById('exAdd').checked, remove: document.getElementById('exRemove').checked, ext: document.getElementById('exExt').checked, mark: !!(mk && mk.checked) }; }
          await api('/api/export', body);
          for (const b of box.querySelectorAll('[data-ex]')) b.disabled = true;
          const prog = document.getElementById('exCsv');
          if (prog) prog.innerHTML = '<p class="hint" id="exProg">処理中</p>';
          watchJob((j) => {
            for (const b of box.querySelectorAll('[data-ex]')) b.disabled = false;
            const out = document.getElementById('exCsv');
            if (!out) return;
            if (j.state === 'error') { out.innerHTML = `<div class="errbox">${esc(j.error || '')}</div>`; return; }
            const r = j.result;
            if (j.kind === 'export_csv') {
              out.innerHTML = resultHtml('更新後の CSV を書き出しました', r.path,
                `<div>追加 ${nf(r.added)} 行、削除 ${nf(r.removed)} 行、拡張子の更新 ${nf(r.ext_updated)} 行</div>` +
                (r.mark_items ? `<div>${esc(r.mark_col)} 列に「${esc(r.mark_text)}」と書いた行 ${nf(r.marked)} 行` +
                  (r.mark_kept ? `（すでに入っていた ${nf(r.mark_kept)} 行はそのまま）` : '') + '</div>' : '') +
                (r.added ? `<div>追加した行の DateTime: 埋め込まれた撮影日時から ${nf(r.added_embedded)} 行、ファイルの更新日時から ${nf(r.added_mtime)} 行、NA ${nf(r.added_na)} 行</div>` : '')) +
                (r.added_unreadable ? `<div class="warnbox">追加した行のうち ${nf(r.added_unreadable)} 本は、動画を読めなかったため DateTime が NA になっています。保存場所の接続を確かめて、もう一度書き出してください。</div>` : '') +
                (r.removed_path ? resultHtml('削除した行の控え', r.removed_path) : '') +
                `<p class="hint">内容を確かめてから、元の CSV（${esc(r.source)}）と置き換えてください。置き換えたあとは再走査すると反映されます。</p>`;
            } else {
              let x = `<div>CSV に DateTime がある動画 ${nf(r.candidates)} 本のうち ${nf(r.sampled)} 本を調べました。</div><table class="grid" style="margin:6px 0">` +
                `<tr><td>埋め込みの撮影日時がない（追加時は NA）</td><td class="n">${nf(r.no_embedded)} 本</td></tr>` +
                (r.unreadable ? `<tr><td>動画を読めなかった</td><td class="n">${nf(r.unreadable)} 本</td></tr>` : '') +
                `<tr><td>CSV の DateTime と一致</td><td class="n">${nf(r.match)} 本</td></tr>` +
                `<tr><td>1 分未満の差</td><td class="n">${nf(r.within_minute)} 本</td></tr>` +
                `<tr><td>1 分以上の差</td><td class="n">${nf(r.differ)} 本</td></tr>` +
                (r.unparsed ? `<tr><td>CSV の DateTime を読めなかった</td><td class="n">${nf(r.unparsed)} 本</td></tr>` : '') + '</table>';
              if (r.offsets.length) x += '<div class="hint">差（埋め込み − CSV、分）の多いもの: ' + r.offsets.map((o) => `${o[0]} 分が ${nf(o[1])} 本`).join('、') + '</div>';
              if (r.differ && r.offsets.length && Math.abs(r.offsets[0][0]) === 540) x += '<div class="warnbox">9 時間の差が多く見られます。設定の「埋め込まれた撮影日時の解釈」を切り替えると合う可能性があります。</div>';
              out.innerHTML = `<div class="result"><b>突き合わせの結果</b>${x}</div>`;
            }
          }, (text) => { const el = document.getElementById('exProg'); if (el) el.textContent = text; });
        }
      } catch (err) { toast(err.message, true); }
    };
  }
  $('btnExport').addEventListener('click', openExport);

  // ------------------------------------------------------------ キー操作
  document.addEventListener('keydown', (e) => {
    if (!$('modal').hidden) { if (e.key === 'Escape') closeModal(); return; }
    const t = e.target;
    const tag = t && t.tagName;
    const typing = tag === 'TEXTAREA' || tag === 'SELECT' ||
      (tag === 'INPUT' && !['checkbox', 'radio', 'range', 'button'].includes(t.type));
    if (typing) {
      if (e.key === 'Escape') { t.blur(); $('tbody').focus({ preventScroll: true }); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (tag === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;
    const page = Math.max(1, Math.floor(($('tbody').clientHeight || 600) / ROW_H) - 1);
    let key = e.key;
    if (key === 'Process' || key === 'Unidentified') {   // 日本語入力が有効なときは、キーの位置から判断する
      if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3).toLowerCase();
      else if (/^Digit[0-9]$/.test(e.code)) key = e.code.slice(5);
      else if (e.code === 'Space') key = ' ';
    }
    switch (key) {
      case 'ArrowDown': moveCur(1, e.shiftKey); break;
      case 'ArrowUp': moveCur(-1, e.shiftKey); break;
      case 'PageDown': moveCur(page, e.shiftKey); break;
      case 'PageUp': moveCur(-page, e.shiftKey); break;
      case 'Home': moveCur(-1e9, e.shiftKey); break;
      case 'End': moveCur(1e9, e.shiftKey); break;
      case ' ': togglePlay(); break;
      case 'ArrowRight': seekBy(2); break;
      case 'ArrowLeft': seekBy(-2); break;
      // 判断のキーは、押しっぱなしでは繰り返さない（次々に別の動画へ判断が付くのを防ぐ）
      case 'k': case 'K': if (!e.repeat) decide('k'); break;
      case 'd': case 'D': if (!e.repeat) decide('d'); break;
      case 'e': case 'E': if (!e.repeat) decide('e'); break;
      case 'u': case 'U': if (!e.repeat) decide(''); break;
      case '1': setSpeed(1); break;
      case '2': setSpeed(2); break;
      case '4': setSpeed(4); break;
      default: return;
    }
    e.preventDefault();
  });

  // チェックや選択、つまみを操作したあとは、キー操作が一覧に届くように戻す
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (!$('modal').hidden || !t || t.tagName !== 'INPUT') return;
    if (['checkbox', 'radio', 'range'].includes(t.type)) $('tbody').focus({ preventScroll: true });
  });

  window.TM = { S, MJ, P };   // 試験と調査用
  init();
})();
