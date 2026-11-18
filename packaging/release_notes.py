# -*- coding: utf-8 -*-
"""Releases に載せる説明文を作る（CHANGELOG.md のその版の節 + 入れ方）。

使い方:  python packaging/release_notes.py 0.2.0 > notes.md
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOWTO = u"""## 入れ方（Windows）

1. 下の Assets から `TrapMatch.exe` を取ってきます。
2. 好きな場所に置いて、ダブルクリックします。Python を入れる必要はありません。
   「Windows によって PC が保護されました」と出たら、「詳細情報」→「実行」を押します。

前の版から入れ替えるときは、`TrapMatch.exe` を置き換えて起動するだけです。設定と作業状態（ホームフォルダの `TrapMatchData`）はそのまま使えます。
"""


def section(version):
    with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as f:
        text = f.read()
    m = re.search(r'^## %s\b[^\n]*\n(.*?)(?=^## |\Z)' % re.escape(version), text, re.S | re.M)
    return m.group(1).strip() if m else ''


def main():
    version = sys.argv[1].lstrip('v')
    body = section(version)
    out = (u'## 変更点\n\n%s\n\n' % body if body else u'') + HOWTO
    sys.stdout.buffer.write(out.encode('utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
