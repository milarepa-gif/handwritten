#!/usr/bin/env python3
"""Build a glyph set from a handwriting-style font instead of a filled
template. Useful for a quick preview before anyone writes the template, or
when a font is good enough.

Each character gets several slightly different variants (tilt, size, a small
wobble in the strokes) so repeated letters don't look stamped.

Usage:
    python font_to_glyphs.py FONT.ttf OUT_DIR [--variants 3]
"""
import argparse
import json
import os
import random
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout as L  # noqa: E402
from extract_glyphs import SITTERS, XHEIGHT_CHARS, safe_name, contact_sheet  # noqa: E402

S = 12.0  # px per mm, same as extract_glyphs


def wobble(a, rng, amount=1.2):
    """Small smooth random displacement so strokes aren't machine-perfect."""
    h, w = a.shape
    dx = cv2.GaussianBlur((rng.random((h, w)) - 0.5).astype(np.float32), (0, 0), 6) * amount * 25
    dy = cv2.GaussianBlur((rng.random((h, w)) - 0.5).astype(np.float32), (0, 0), 6) * amount * 25
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(a, gx + dx, gy + dy, cv2.INTER_LINEAR, borderValue=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("font")
    ap.add_argument("out_dir")
    ap.add_argument("--variants", type=int, default=3)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    nrng = np.random.default_rng(a.seed)
    os.makedirs(os.path.join(a.out_dir, "g"), exist_ok=True)

    # size the font so its x-height is about 4.8 mm, like a filled template
    probe = ImageFont.truetype(a.font, 200)
    xb = probe.getbbox("x", anchor="ls")
    px_size = int(200 * (4.8 * S) / max(xb[3] - xb[1], 1))
    font = ImageFont.truetype(a.font, px_size)

    chars = list(dict.fromkeys(L.cell_list()))
    glyphs, skipped = {}, []
    for ch in chars:
        if font.getmask(ch).getbbox() is None:
            skipped.append(ch)
            continue
        for k in range(a.variants):
            canvas_w, canvas_h = int(px_size * 2.2), int(px_size * 2.2)
            bx, by = canvas_w // 4, int(canvas_h * 0.62)
            img = Image.new("L", (canvas_w, canvas_h), 0)
            ImageDraw.Draw(img).text((bx, by), ch, font=font, fill=255, anchor="ls")
            if k:
                sc = 1 + rng.uniform(-0.05, 0.05)
                img = img.resize((int(canvas_w * sc), int(canvas_h * sc)), Image.LANCZOS)
                img = img.rotate(rng.uniform(-3.5, 3.5), center=(bx * sc, by * sc), resample=Image.BICUBIC)
                by = int(by * sc)
            arr = np.asarray(img, dtype=np.float32)
            if k:
                arr = wobble(arr, nrng)
            ys, xs = np.nonzero(arr > 60)
            if not len(xs):
                continue
            pad = 3
            x0, x1 = max(xs.min() - pad, 0), xs.max() + pad + 1
            y0, y1 = max(ys.min() - pad, 0), ys.max() + pad + 1
            crop = arr[y0:y1, x0:x1].clip(0, 255).astype(np.uint8)
            fname = f"{safe_name(ch)}_{k}.png"
            Image.fromarray(crop).save(os.path.join(a.out_dir, "g", fname))
            glyphs.setdefault(ch, []).append({
                "file": f"g/{fname}",
                "path": os.path.join(a.out_dir, "g", fname),
                "w": round((xs.max() - xs.min() + 1) / S, 3),
                "h": round((ys.max() - ys.min() + 1) / S, 3),
                "desc": 0.0 if ch in SITTERS else round((ys.max() + 1 - by) / S, 3),
                "top": round((by - ys.min()) / S, 3),
                "pad_px": [int(xs.min() - x0), int(ys.min() - y0)],
            })

    def med(cs):
        hs = [v["h"] for c in cs if c in glyphs for v in glyphs[c]]
        return round(float(np.median(hs)), 3) if hs else 4.8

    meta = {"px_per_mm": S, "x_height_mm": med(XHEIGHT_CHARS), "cap_height_mm": med(set("ABDEFHIKLMNPRTUVWXYZ")),
            "has_natural_page": False, "source_font": os.path.basename(a.font)}
    contact_sheet(glyphs, os.path.join(a.out_dir, "contact_sheet.png"))
    for vs in glyphs.values():
        for v in vs:
            v.pop("path", None)
    with open(os.path.join(a.out_dir, "glyphs.json"), "w") as f:
        json.dump({"meta": meta, "glyphs": glyphs}, f, indent=1, ensure_ascii=False)
    print(f"{sum(len(v) for v in glyphs.values())} glyphs for {len(glyphs)} characters -> {a.out_dir}")
    if skipped:
        print("Font has no shape for:", " ".join(skipped))


if __name__ == "__main__":
    main()
