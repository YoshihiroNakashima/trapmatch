# -*- coding: utf-8 -*-
"""TrapMatch のアイコン（packaging/trapmatch.ico）を作る。

図柄は、緑の角丸の四角に白い等号（2 つの保存場所が「一致」するという意味）。
作り直すとき:  python packaging/make_icon.py   （Pillow が必要）
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
MOSS = (46, 106, 77, 255)       # 画面の緑（--moss）
WHITE = (255, 255, 255, 255)


def draw(size):
    k = 8                               # 大きく描いてから縮めて、縁をなめらかにする
    n = size * k
    im = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.2), fill=MOSS)
    x0, x1 = n * 0.24, n * 0.76
    h = n * 0.13
    for cy in (n * 0.38, n * 0.62):
        d.rounded_rectangle([x0, cy - h / 2, x1, cy + h / 2], radius=h / 2, fill=WHITE)
    return im.resize((size, size), Image.LANCZOS)


def main():
    sizes = [256, 128, 64, 48, 32, 24, 16]
    big = draw(256)
    out = os.path.join(HERE, 'trapmatch.ico')
    big.save(out, format='ICO', sizes=[(s, s) for s in sizes])
    big.save(os.path.join(HERE, 'trapmatch.png'))
    print(out, os.path.getsize(out), 'bytes')


if __name__ == '__main__':
    main()
