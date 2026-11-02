#!/usr/bin/env python3
"""Make fake 'phone photos' of a filled-in template, for testing the pipeline
without writing anything by hand. Uses a handwriting-style TTF as the stand-in
writer, then skews, rotates, shades and adds noise like a real photo.

Usage:
    python tests/simulate_filled_template.py FONT.ttf OUT_DIR
"""
import os
import random
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import layout as L  # noqa: E402

DPI = 200
PX = DPI / 25.4
INK = (25, 35, 110, 255)


def write(img, text, x_mm, base_mm, font, rot=0.0):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((x_mm * PX, base_mm * PX), text, font=font, fill=INK, anchor="ls")
    if rot:
        layer = layer.rotate(rot, center=(x_mm * PX, base_mm * PX), resample=Image.BICUBIC)
    img.alpha_composite(layer)


def fill_pages(font_path):
    tmp = tempfile.mkdtemp()
    tpl = os.path.join(HERE, "..", "assets", "handwriting_template.pdf")
    subprocess.run(["pdftoppm", "-r", str(DPI), "-png", tpl, os.path.join(tmp, "p")], check=True)
    files = sorted(f for f in os.listdir(tmp) if f.endswith(".png"))
    big = ImageFont.truetype(font_path, int(9.5 * PX))
    small = ImageFont.truetype(font_path, int(6.0 * PX))
    out = []
    for n, f in enumerate(files, start=1):
        img = Image.open(os.path.join(tmp, f)).convert("RGBA")
        if n <= len(L.grid_pages()):
            for i, ch in enumerate(L.grid_pages()[n - 1]):
                x, y, w, h = L.box_rect(i)
                base = y + h * L.BASELINE_FRAC + random.uniform(-0.5, 0.4)
                write(img, ch, x + random.uniform(5, 9), base, big, rot=random.uniform(-4, 4))
        else:
            for text, _, rules in L.natural_layout():
                write(img, text, L.MARGIN_X + 2, rules[0] - 0.3, small, rot=random.uniform(-0.6, 0.6))
        out.append(img.convert("RGB"))
    return out


def photograph(page, rotate_k):
    p = cv2.cvtColor(np.array(page), cv2.COLOR_RGB2BGR)
    h, w = p.shape[:2]
    W, H = int(w * 1.35), int(h * 1.3)
    bg = np.full((H, W, 3), (60, 85, 110), np.uint8)
    bg = cv2.add(bg, np.random.randint(0, 25, bg.shape, dtype=np.uint8))
    ox, oy = (W - w) / 2, (H - h) / 2
    j = lambda: random.uniform(-0.06, 0.06) * w  # noqa: E731
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[ox + j(), oy + j()], [ox + w + j(), oy + j()], [ox + w + j(), oy + h + j()], [ox + j(), oy + h + j()]])
    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(p, M, (W, H), borderMode=cv2.BORDER_TRANSPARENT, dst=bg.copy())
    gx = np.linspace(0.72, 1.05, W)[None, :, None]
    gy = np.linspace(1.0, 0.85, H)[:, None, None]
    shaded = np.clip(warped.astype(np.float32) * gx * gy + np.random.normal(0, 4, warped.shape), 0, 255).astype(np.uint8)
    shaded = cv2.GaussianBlur(shaded, (3, 3), 0.8)
    return np.rot90(shaded, rotate_k).copy()


def main(font_path, out_dir):
    random.seed(7)
    np.random.seed(7)
    os.makedirs(out_dir, exist_ok=True)
    pages = fill_pages(font_path)
    names = ["IMG_4471.jpg", "IMG_4468.jpg", "IMG_4470.jpg", "IMG_4469.jpg"]  # deliberately out of order
    rots = [0, 1, 2, 3]  # upright, 90, 180, 270 degrees
    for page, name, k in zip(pages, names, rots):
        cv2.imwrite(os.path.join(out_dir, name), photograph(page, k), [cv2.IMWRITE_JPEG_QUALITY, 85])
        print(os.path.join(out_dir, name))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
