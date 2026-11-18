# -*- coding: utf-8 -*-
"""試験用の ffmpeg を用意する（GitHub Actions 用）。

pip で入れた imageio-ffmpeg に付いている ffmpeg を、指定のフォルダへ ffmpeg という名前で写し、
そのフォルダを PATH に足す。試験は、動画の見本を作るために ffmpeg を使う。
使い方:  python packaging/ci_ffmpeg.py <フォルダ>
"""
import os
import shutil
import sys

import imageio_ffmpeg


def main():
    src = imageio_ffmpeg.get_ffmpeg_exe()
    dst_dir = os.path.abspath(sys.argv[1])
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    shutil.copy2(src, dst)
    os.chmod(dst, 0o755)
    path_file = os.environ.get('GITHUB_PATH')
    if path_file:
        with open(path_file, 'a', encoding='utf-8') as f:
            f.write(dst_dir + '\n')
    print(dst)
    return 0


if __name__ == '__main__':
    sys.exit(main())
