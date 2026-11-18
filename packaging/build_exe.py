# -*- coding: utf-8 -*-
"""TrapMatch を 1 つの実行ファイルにまとめる（PyInstaller）。

使い方:  python packaging/build_exe.py
出力:    dist/TrapMatch.exe（Windows）、dist/TrapMatch（macOS、Linux）

実行ファイルは、作った OS でだけ動く。配布用の TrapMatch.exe は、GitHub Actions の
Windows の環境で作る（.github/workflows/build.yml）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tm_core  # noqa: E402

VERSION_INFO = u"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=%(tuple)s,
    prodvers=%(tuple)s,
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(u'041104B0', [
        StringStruct(u'CompanyName', u'Yoshihiro Nakashima'),
        StringStruct(u'FileDescription', u'TrapMatch'),
        StringStruct(u'FileVersion', u'%(version)s'),
        StringStruct(u'InternalName', u'TrapMatch'),
        StringStruct(u'LegalCopyright', u'Copyright (c) 2026 Yoshihiro Nakashima. MIT License.'),
        StringStruct(u'OriginalFilename', u'TrapMatch.exe'),
        StringStruct(u'ProductName', u'TrapMatch'),
        StringStruct(u'ProductVersion', u'%(version)s')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [0x0411, 1200])])
  ]
)
"""


def version_tuple(version):
    nums = [int(x) for x in version.split('.')[:4] if x.isdigit()]
    return tuple((nums + [0, 0, 0, 0])[:4])


def main():
    build = os.path.join(ROOT, 'build')
    dist = os.path.join(ROOT, 'dist')
    os.makedirs(build, exist_ok=True)
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--console',
            '--name', 'TrapMatch',
            '--add-data', os.path.join(ROOT, 'static') + os.pathsep + 'static',
            # ffmpeg を探すときだけ使う、あってもなくてもよい部品。作る環境に入っていても同梱しない
            '--exclude-module', 'imageio_ffmpeg',
            '--distpath', dist, '--workpath', build, '--specpath', build]
    if os.name == 'nt':
        vf = os.path.join(build, 'version_info.txt')
        with open(vf, 'w', encoding='utf-8') as f:
            f.write(VERSION_INFO % {'tuple': repr(version_tuple(tm_core.VERSION)), 'version': tm_core.VERSION})
        args += ['--version-file', vf, '--icon', os.path.join(ROOT, 'packaging', 'trapmatch.ico')]
    args.append(os.path.join(ROOT, 'app.py'))
    print('TrapMatch %s を実行ファイルにまとめます' % tm_core.VERSION)
    sys.stdout.flush()
    subprocess.check_call(args, cwd=ROOT)
    out = os.path.join(dist, 'TrapMatch.exe' if os.name == 'nt' else 'TrapMatch')
    print('%s (%.1f MB)' % (out, os.path.getsize(out) / 1e6))
    return 0


if __name__ == '__main__':
    sys.exit(main())
