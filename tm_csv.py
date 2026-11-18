# -*- coding: utf-8 -*-
"""CSV の読み書き。

変更しない行は元のテキストのまま（1 バイトも変えずに）書き戻すため、
レコードを「元のテキスト」と「分解した列」の両方で扱う。
"""
import csv
import io
import os


def candidate_encodings(path):
    with open(path, 'rb') as f:
        head = f.read(3)
    if head == b'\xef\xbb\xbf':
        return ['utf-8-sig']
    return ['utf-8', 'cp932']


MAX_RECORD_LINES = 20


def _valid_multiline(text, ctx):
    """複数行にまたがるレコードが、本当に 1 レコードとして読めるかを確かめる。"""
    ncols = ctx.get('ncols')
    if not ncols:
        return True
    try:
        rows = list(csv.reader([text.rstrip('\r\n')], delimiter=ctx.get('delim') or ','))
    except csv.Error:
        return False
    return len(rows) == 1 and len(rows[0]) == ncols


def iter_raw_records(f, ctx=None):
    """CSV の 1 レコードごとに、改行を含む元のテキストを返す。

    引用符の中の改行を 1 レコードとして扱う（引用符の数が偶数になるまで行をつなぐ）。

    引用符が 1 つだけ紛れ込んだ行（「5" long」など）があると、以降の行が 1 つのレコードに
    つながってしまう。そこで、つないだ結果が見出しと同じ列数の 1 レコードとして読めないとき、
    または MAX_RECORD_LINES 行つないでも閉じないときは、対応の取れない引用符とみなして、
    つないだ行を 1 行ずつのレコードに戻す。回数は ctx['malformed'] に数える。
    ctx には、見出しを読んだあとで 'delim' と 'ncols'（列数）を入れておく。
    """
    if ctx is None:
        ctx = {}
    buf = None
    quotes = 0

    def give_up():
        ctx['malformed'] = ctx.get('malformed', 0) + 1

    for line in f:
        if buf is None:
            c = line.count('"')
            if c % 2 == 0:
                yield line
            else:
                buf = [line]
                quotes = c
        else:
            buf.append(line)
            quotes += line.count('"')
            if quotes % 2 == 0:
                text = ''.join(buf)
                if _valid_multiline(text, ctx):
                    yield text
                else:
                    give_up()
                    for one in buf:
                        yield one
                buf = None
            elif len(buf) >= MAX_RECORD_LINES:
                give_up()
                for one in buf:
                    yield one
                buf = None
    if buf is not None:
        if len(buf) > 1:
            give_up()
        for one in buf:
            yield one


def eol_of(raw):
    return raw[len(raw.rstrip('\r\n')):]


def parse_record(raw, delim):
    body = raw.rstrip('\r\n')
    if '"' not in body:
        return body.split(delim)
    try:
        return next(csv.reader([body], delimiter=delim))
    except (csv.Error, StopIteration):
        return body.split(delim)


def serialize(fields, delim, quote_all=False):
    """列の値を 1 行の CSV にする（改行は付けない）。

    quote_all のときは、元の CSV が文字列を引用符で囲む書き方（R の write.csv など）
    なので、空欄と NA 以外を引用符で囲む。
    """
    if quote_all:
        return delim.join(v if v in ('', 'NA') else '"' + v.replace('"', '""') + '"' for v in fields)
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim, lineterminator='\n',
               quoting=csv.QUOTE_MINIMAL).writerow(fields)
    return buf.getvalue()[:-1]


def field_spans(body, delim):
    """1 レコードの元のテキストを、列ごとの (開始, 終了, 引用符つきか) に分ける。"""
    spans = []
    i = 0
    n = len(body)
    while True:
        if i < n and body[i] == '"':
            j = i + 1
            while j < n:
                if body[j] == '"':
                    if j + 1 < n and body[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            end = min(n, j + 1)
            k = body.find(delim, end)
            if k < 0:
                k = n
            if k == end:
                spans.append((i, end, True))
            else:                      # 引用符のあとに余分な文字がある（規格外）
                spans.append((i, k, False))
            i = k
        else:
            k = body.find(delim, i)
            if k < 0:
                k = n
            spans.append((i, k, False))
            i = k
        if i >= n:
            break
        i += len(delim)
        if i >= n:
            spans.append((n, n, False))
            break
    return spans


def replace_field(raw, delim, index, new_value, force_quote=False):
    """元のテキストの index 番目の列だけを new_value に置き換える。ほかは 1 文字も変えない。

    元の値が引用符で囲まれていれば、新しい値も囲む。force_quote のときは必ず囲む
    （文字列をすべて引用符で囲む書き方の CSV で、空欄や NA を文字列に置き換えるとき）。
    """
    eol = eol_of(raw)
    body = raw[:len(raw) - len(eol)] if eol else raw
    spans = field_spans(body, delim)
    if index >= len(spans):
        return None
    a, b, quoted = spans[index]
    need = (quoted or force_quote or delim in new_value or '"' in new_value
            or '\n' in new_value or '\r' in new_value)
    text = '"' + new_value.replace('"', '""') + '"' if need else new_value
    return body[:a] + text + body[b:] + eol


def _columns(header):
    cols = {}
    for i, h in enumerate(header):
        cols.setdefault(h.strip().casefold(), i)
    return cols


def read_season(path, season, progress=None, match=None):
    """CSV から対象の Season の行だけを読む。

    match を渡すと、Season 列の値がその Season のものかどうかを match(値) で決める
    （Season 名を先頭の文字だけで照合するときなど）。season は、その行に必ず含まれる文字列にする。
    渡さなければ、Season 列の値が season と同じ（大文字と小文字は区別しない）行を読む。
    """
    last = None
    for enc in candidate_encodings(path):
        try:
            return _read_season(path, season, enc, progress, match)
        except UnicodeDecodeError as e:
            last = e
    raise ValueError('CSV の文字コードを判定できません（UTF-8 と Shift_JIS を試しました）: %s' % last)


def _read_season(path, season, enc, progress, match=None):
    size = os.path.getsize(path)
    season_cf = season.strip().casefold()
    if match is None:
        match = lambda value: value.casefold() == season_cf    # noqa: E731
    stats = {}
    with open(path, 'rb') as fb:
        f = io.TextIOWrapper(fb, encoding=enc, newline='')
        it = iter_raw_records(f, stats)
        try:
            header_raw = next(it)
        except StopIteration:
            raise ValueError('CSV が空です')
        eol = eol_of(header_raw) or '\r\n'
        delim = '\t' if header_raw.count('\t') > header_raw.count(',') else ','
        header = [h.strip() for h in parse_record(header_raw, delim)]
        stats['delim'], stats['ncols'] = delim, len(header)
        quote_all = header_raw.lstrip().startswith('"')
        cols = _columns(header)
        missing = [n for n in ('season', 'station', 'file') if n not in cols]
        if missing:
            raise ValueError('CSV に必要な列がありません: %s（見つかった列: %s）' % (
                ', '.join(m.capitalize() for m in missing), ', '.join(header)))
        i_season, i_station, i_file = cols['season'], cols['station'], cols['file']
        i_dt = cols.get('datetime', -1)
        need = max(i_season, i_station, i_file)
        rows = []
        nrec = 0
        for raw in it:
            nrec += 1
            if progress is not None and nrec % 20000 == 0:
                progress(fb.tell(), size)
            if season_cf not in raw.casefold():
                continue
            fields = parse_record(raw, delim)
            if len(fields) <= need:
                continue
            if not match(fields[i_season].strip()):
                continue
            rows.append(fields)
    return {
        'encoding': enc, 'eol': eol, 'delimiter': delim, 'header': header, 'quote_all': quote_all,
        'i_season': i_season, 'i_station': i_station, 'i_file': i_file, 'i_dt': i_dt,
        'rows': rows, 'records': nrec, 'malformed': stats.get('malformed', 0),
    }


def replace_ext(file_field, new_ext):
    """File 列の値の拡張子だけを置き換える（前後の空白やフォルダ部分は保つ）。"""
    stripped = file_field.strip()
    lead = file_field[:len(file_field) - len(file_field.lstrip())]
    trail = file_field[len(file_field.rstrip()):]
    base, _old = os.path.splitext(stripped)
    return lead + base + '.' + new_ext + trail


def add_mark(old, text):
    """列の値に印（text）を付けた値を返す。すでに印が入っていれば None（変更なし）。

    空欄と NA は印に置き換える。ほかの文字が入っているときは、消さずに「; 」でつないで後ろに足す。
    """
    cur = old.strip()
    if text.casefold() in cur.casefold():
        return None
    if not cur or cur.upper() == 'NA':
        return text
    return cur + '; ' + text


def write_updated(src, dst, meta, season, keyfunc, remove_keys, ext_new, add_rows,
                  removed_path=None, progress=None, marks=None, match=None):
    """src を読みながら、変更を反映した CSV を dst に書く。

    remove_keys: 行を除く照合キーの集合
    ext_new:     照合キー -> 新しい拡張子（File 列の拡張子を書き換える）
    add_rows:    末尾に足す行（列の値のリスト）のリスト
    marks:       (列番号, 印の文字列, 照合キーの集合)。その行の列に印を付ける。None なら付けない
    match:       Season 列の値がこの Season のものかを決める関数（read_season と同じ）
    変更のないレコードは元のテキストのまま書く。戻り値は件数の辞書。
    """
    season_cf = season.strip().casefold()
    if match is None:
        match = lambda value: value.casefold() == season_cf    # noqa: E731
    size = os.path.getsize(src)
    # いったん別の名前（.part）に書き、最後まで書けたときだけ本来の名前にする。
    # 途中で失敗しても、不完全な CSV が「更新後の CSV」として残らないようにするため。
    final_dst, final_removed = dst, removed_path
    dst = dst + '.part'
    if removed_path:
        removed_path = removed_path + '.part'
    try:
        res = _write_updated(src, dst, meta, season_cf, keyfunc, remove_keys, ext_new, add_rows,
                             removed_path, progress, size, marks, match)
    except BaseException:
        for p in (dst, removed_path):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        raise
    os.replace(dst, final_dst)
    if removed_path and os.path.exists(removed_path):
        os.replace(removed_path, final_removed)
    return res


def _write_updated(src, dst, meta, season_cf, keyfunc, remove_keys, ext_new, add_rows,
                   removed_path, progress, size, marks=None, match=None):
    enc, delim, eol = meta['encoding'], meta['delimiter'], meta['eol']
    i_season, i_station, i_file = meta['i_season'], meta['i_station'], meta['i_file']
    need = max(i_season, i_station, i_file)
    n_removed = n_ext = nrec = n_marked = n_mark_kept = 0
    i_mark, mark_text, mark_keys = marks if marks else (-1, '', ())
    rem = None
    ctx = {}
    with open(src, 'rb') as fb, open(dst, 'w', encoding=enc, newline='') as out:
        f = io.TextIOWrapper(fb, encoding=enc, newline='')
        it = iter_raw_records(f, ctx)
        try:
            header_raw = next(it)
        except StopIteration:
            raise ValueError('CSV が空です')
        # 読み込みのときと同じ規則でレコードを区切る
        ctx['delim'], ctx['ncols'] = delim, len(parse_record(header_raw, delim))
        out.write(header_raw)
        last = header_raw
        try:
            for raw in it:
                nrec += 1
                if progress is not None and nrec % 20000 == 0:
                    progress(fb.tell(), size)
                if season_cf in raw.casefold():
                    fields = parse_record(raw, delim)
                    if len(fields) > need and match(fields[i_season].strip()):
                        key, _stem, ext = keyfunc(fields[i_station], fields[i_file])
                        if key is not None and key in remove_keys:
                            n_removed += 1
                            if removed_path:
                                if rem is None:
                                    rem = open(removed_path, 'w', encoding=enc, newline='')
                                    rem.write(header_raw if eol_of(header_raw) else header_raw + eol)
                                rem.write(raw if eol_of(raw) else raw + eol)
                            continue
                        new_ext = ext_new.get(key) if key is not None else None
                        if new_ext and ext and ext.lower() != new_ext.lower():
                            new_file = replace_ext(fields[i_file], new_ext)
                            changed = replace_field(raw, delim, i_file, new_file)
                            if changed is None:
                                fields[i_file] = new_file
                                changed = serialize(fields, delim) + eol_of(raw)
                            raw = changed
                            n_ext += 1
                        if key is not None and i_mark >= 0 and key in mark_keys:
                            if len(fields) <= i_mark:
                                fields += [''] * (i_mark + 1 - len(fields))     # 列が足りない行は、列を補って書く
                            marked = add_mark(fields[i_mark], mark_text)
                            if marked is None:
                                n_mark_kept += 1
                            else:
                                qa = meta.get('quote_all', False)
                                changed = replace_field(raw, delim, i_mark, marked, force_quote=qa)
                                if changed is None:
                                    fields[i_mark] = marked
                                    changed = serialize(fields, delim, qa) + eol_of(raw)
                                raw = changed
                                n_marked += 1
                out.write(raw)
                last = raw
        finally:
            if rem is not None:
                rem.close()
        if add_rows:
            if not eol_of(last):
                out.write(eol)
            for fields in add_rows:
                out.write(serialize(fields, delim, meta.get('quote_all', False)) + eol)
    return {'removed': n_removed, 'ext_updated': n_ext, 'added': len(add_rows), 'records': nrec,
            'marked': n_marked, 'mark_kept': n_mark_kept}
