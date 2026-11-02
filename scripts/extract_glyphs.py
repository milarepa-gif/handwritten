#!/usr/bin/env python3
"""Cut a person's letters out of photos or scans of the filled template.

Usage:
    python extract_glyphs.py OUT_DIR page1.jpg page2.jpg ... [--debug]

Pages can be given in any order and at any rotation; the page number is read
from the squares printed on each sheet. Scanned PDFs work too.

Writes to OUT_DIR:
    glyphs.json        character -> list of variants with size and baseline info
    g/*.png            one greyscale alpha image per variant (white = ink)
    contact_sheet.png  every extracted glyph, for a quick visual check
    natural.png        the straightened natural-writing page, if supplied
"""
import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout as L  # noqa: E402

try:  # iPhone photos
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass

S = 12.0             # px per mm in the straightened page (~305 dpi)
INK_T = 0.30         # darkness threshold for "this is ink"
DESCENDERS = set("gjpqy,;") | {"Q", "J"}
SITTERS = set("abcdefhiklmnorstuvwxz") | set("ABCDEFGHIKLMNOPRSTUVWXYZ") | set("0123456789") | set(".!?:")
XHEIGHT_CHARS = set("acemnorsuvwxz")
FLOATERS = set("'\"-=+*")


# ---------------------------------------------------------------- loading
def load_images(paths):
    """Yield (label, BGR image). PDFs are rasterised page by page."""
    for p in paths:
        if p.lower().endswith(".pdf"):
            tmp = tempfile.mkdtemp()
            try:
                subprocess.run(["pdftoppm", "-r", "250", "-png", p, os.path.join(tmp, "pg")], check=True)
            except (FileNotFoundError, subprocess.CalledProcessError):
                sys.exit(f"Could not rasterise {p}. Install poppler (pdftoppm) or supply images instead.")
            for f in sorted(os.listdir(tmp)):
                img = cv2.imread(os.path.join(tmp, f))
                yield f"{os.path.basename(p)}:{f}", img
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            yield os.path.basename(p), cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)


# ---------------------------------------------------------------- straightening
def find_marker_centres(gray):
    h, w = gray.shape
    k = 1600.0 / max(h, w) if max(h, w) > 1600 else 1.0
    small = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k != 1 else gray
    small = cv2.GaussianBlur(small, (3, 3), 0)
    sh, sw = small.shape
    block = int(max(sh, sw) / 8) | 1
    th = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 15)
    # RETR_LIST so markers are found even when a dark tabletop wraps the page.
    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    img_area = sh * sw
    cands = []
    for c in contours:
        a = cv2.contourArea(c)
        if a < 1.5e-4 * img_area or a > 0.02 * img_area:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(c)
        if rw == 0 or rh == 0:
            continue
        if not (0.65 < rw / rh < 1.55) or a / (rw * rh) < 0.82:
            continue
        r = max(int(min(rw, rh) * 0.25), 1)
        core = th[max(int(cy) - r, 0):int(cy) + r + 1, max(int(cx) - r, 0):int(cx) + r + 1]
        if core.size == 0 or core.mean() < 200:
            continue  # a white hole (e.g. inside a box outline), not a solid square
        if any(abs(cx / k - p[1]) < 3 and abs(cy / k - p[2]) < 3 for p in cands):
            continue  # same square traced twice
        cands.append((a, cx / k, cy / k))
    cands.sort(reverse=True)
    cands = cands[:8]
    if len(cands) < 4:
        return None
    best, best_score = None, 0
    for combo in itertools.combinations(cands, 4):
        areas = [c[0] for c in combo]
        if min(areas) / max(areas) < 0.35:
            continue
        pts = np.array([[c[1], c[2]] for c in combo], dtype=np.float32)
        hull = cv2.convexHull(pts)
        if len(hull) != 4:
            continue
        score = cv2.contourArea(hull)
        if score > best_score:
            best, best_score = pts, score
    return best


def clockwise(pts):
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    return pts[np.argsort(ang)]  # image coords: increasing angle = clockwise on screen


def darkness(gray, rect_mm, scale, inset=0.5):
    x, y, w, h = rect_mm
    x0, y0 = int((x + inset) * scale), int((y + inset) * scale)
    x1, y1 = int((x + w - inset) * scale), int((y + h - inset) * scale)
    region = gray[y0:y1, x0:x1]
    return 1.0 - region.mean() / 255.0 if region.size else 0.0


def straighten(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    pts = find_marker_centres(gray)
    if pts is None:
        return None, "could not find the four corner squares"
    pts = clockwise(pts)
    order = ["tl", "tr", "br", "bl"]
    best = None
    for k in range(4):
        src = np.array([pts[(i + k) % 4] for i in range(4)], dtype=np.float32)
        dst4 = np.array([[L.MARKER_CENTERS[o][0] * 4, L.MARKER_CENTERS[o][1] * 4] for o in order], dtype=np.float32)
        M = cv2.getPerspectiveTransform(src, dst4)
        low = cv2.warpPerspective(gray, M, (int(L.PAGE_W * 4), int(L.PAGE_H * 4)), borderValue=255)
        score = darkness(low, L.ORIENT_MARK, 4)
        if best is None or score > best[0]:
            best = (score, src)
    if best[0] < 0.35:
        return None, "could not tell which way up the page is (small square by the top-left corner not found)"
    dst = np.array([[L.MARKER_CENTERS[o][0] * S, L.MARKER_CENTERS[o][1] * S] for o in order], dtype=np.float32)
    M = cv2.getPerspectiveTransform(best[1], dst)
    warped = cv2.warpPerspective(bgr, M, (int(L.PAGE_W * S), int(L.PAGE_H * S)),
                                 flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
    return warped, None


def read_page_number(warped):
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    bits = [1 if darkness(gray, r, S, inset=0.8) > 0.45 else 0 for r in L.page_id_rects()]
    n = 0
    for b in bits:
        n = (n << 1) | b
    return n


# ---------------------------------------------------------------- ink
def ink_darkness(warped):
    """0..1 map of how ink-dark each pixel is relative to the local paper."""
    m = warped.min(axis=2).astype(np.float32)
    small = cv2.resize(m, None, fx=0.125, fy=0.125, interpolation=cv2.INTER_AREA)
    small = cv2.morphologyEx(small, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    small = cv2.medianBlur(small.astype(np.uint8), 5).astype(np.float32)
    bg = cv2.resize(small, (m.shape[1], m.shape[0]), interpolation=cv2.INTER_LINEAR)
    return np.clip((bg - m) / np.maximum(bg, 1.0), 0, 1)


def cut_box(d, index, ch):
    x, y, w, h = L.box_rect(index)
    inset = 1.3
    x0, y0 = int((x + inset) * S), int((y + inset) * S)
    x1, y1 = int((x + w - inset) * S), int((y + h - inset) * S)
    crop = d[y0:y1, x0:x1].copy()
    # blank out the printed label
    crop[: int((L.LABEL_H - inset + 0.3) * S), : int((L.LABEL_W - inset) * S)] = 0
    mask = (crop > INK_T).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    keep = np.zeros_like(mask)
    ch_, cw_ = mask.shape
    min_area = 0.15 * S * S
    for i in range(1, n):
        bx, by, bw, bh, area = stats[i]
        if area < min_area:
            continue
        touches = bx == 0 or by == 0 or bx + bw >= cw_ or by + bh >= ch_
        line_like = min(bw, bh) < 0.9 * S and max(bw, bh) > 0.6 * max(cw_, ch_)
        if touches and line_like:
            continue  # a box edge that crept in
        keep[lab == i] = 1
    if keep.sum() < 0.3 * S * S:
        return None
    ys, xs = np.nonzero(keep)
    pad = 3
    bx0, bx1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, cw_)
    by0, by1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, ch_)
    soft = np.clip((crop - 0.10) / (0.50 - 0.10), 0, 1)
    soft *= cv2.dilate(keep, np.ones((5, 5), np.uint8)).astype(np.float32)
    alpha = (soft[by0:by1, bx0:bx1] * 255).astype(np.uint8)
    baseline_px = (y + h * L.BASELINE_FRAC) * S
    bottom_px = y0 + ys.max() + 1
    top_px = y0 + ys.min()
    return {
        "alpha": alpha,
        "w": (xs.max() - xs.min() + 1) / S,
        "h": (ys.max() - ys.min() + 1) / S,
        "desc": (bottom_px - baseline_px) / S,   # + means below the baseline
        "top": (baseline_px - top_px) / S,
        "pad_px": [int(xs.min() - bx0), int(ys.min() - by0)],
    }


# ---------------------------------------------------------------- output
def safe_name(ch):
    return f"u{ord(ch):04x}"


def contact_sheet(glyphs, out_path):
    items = [(ch, v) for ch, vs in glyphs.items() for v in vs]
    cols, cell = 16, 90
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
    for i, (ch, v) in enumerate(items):
        r, c = divmod(i, cols)
        a = Image.open(v["path"])
        a.thumbnail((cell - 20, cell - 26))
        ox, oy = c * cell + (cell - a.width) // 2, r * cell + 18 + (cell - 26 - a.height) // 2
        ink = Image.new("RGB", a.size, (20, 30, 90))
        sheet.paste(ink, (ox, oy), a)
        draw.text((c * cell + 3, r * cell + 2), ch, fill=(200, 60, 60), font=font)
        draw.rectangle([c * cell, r * cell, (c + 1) * cell - 1, (r + 1) * cell - 1], outline=(220, 220, 220))
    sheet.save(out_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("images", nargs="+")
    ap.add_argument("--debug", action="store_true", help="also save each straightened page")
    a = ap.parse_args()

    os.makedirs(os.path.join(a.out_dir, "g"), exist_ok=True)
    pages = L.grid_pages()
    natural_no = L.total_pages()
    found, problems = {}, []

    for label, img in load_images(a.images):
        if img is None:
            problems.append(f"{label}: could not open")
            continue
        warped, err = straighten(img)
        if err:
            problems.append(f"{label}: {err}")
            continue
        n = read_page_number(warped)
        if not 1 <= n <= natural_no:
            problems.append(f"{label}: page number squares unreadable (got {n})")
            continue
        if n in found:
            problems.append(f"{label}: duplicate of page {n}, ignored")
            continue
        found[n] = warped
        if a.debug:
            cv2.imwrite(os.path.join(a.out_dir, f"page{n}_straight.jpg"), warped)
        print(f"{label}: page {n}")

    glyphs, empty = {}, []
    for n, chars in enumerate(pages, start=1):
        if n not in found:
            continue
        d = ink_darkness(found[n])
        for i, ch in enumerate(chars):
            g = cut_box(d, i, ch)
            if g is None:
                empty.append(ch)
                continue
            k = len(glyphs.get(ch, []))
            fname = f"{safe_name(ch)}_{k}.png"
            path = os.path.join(a.out_dir, "g", fname)
            Image.fromarray(g.pop("alpha")).save(path)
            g["file"] = f"g/{fname}"
            g["path"] = path
            glyphs.setdefault(ch, []).append(g)

    if natural_no in found:
        nat = cv2.resize(found[natural_no], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(a.out_dir, "natural.png"), nat)

    if not glyphs:
        print("\n".join(problems) or "no glyphs found")
        sys.exit(1)

    # Baseline normalisation: people rarely sit exactly on the guide line.
    sit = [v["desc"] for ch, vs in glyphs.items() if ch in SITTERS for v in vs]
    shift = float(np.median(sit)) if sit else 0.0
    for ch, vs in glyphs.items():
        for v in vs:
            v["desc"] = 0.0 if ch in SITTERS else round(v["desc"] - shift, 3)
            v["top"] = round(v["top"] + shift, 3)
            v["w"], v["h"] = round(v["w"], 3), round(v["h"], 3)

    def med(chars):
        hs = [v["h"] for ch in chars if ch in glyphs for v in glyphs[ch]]
        return round(float(np.median(hs)), 3) if hs else None

    meta = {
        "px_per_mm": S,
        "x_height_mm": med(XHEIGHT_CHARS) or 4.0,
        "cap_height_mm": med(set("ABDEFHIKLMNPRTUVWXYZ")) or 6.0,
        "has_natural_page": natural_no in found,
        "pages_found": sorted(found),
    }
    contact_sheet(glyphs, os.path.join(a.out_dir, "contact_sheet.png"))
    for vs in glyphs.values():
        for v in vs:
            v.pop("path", None)
    with open(os.path.join(a.out_dir, "glyphs.json"), "w") as f:
        json.dump({"meta": meta, "glyphs": glyphs}, f, indent=1, ensure_ascii=False)

    wanted = set(L.cell_list())
    missing = sorted(wanted - set(glyphs))
    missing_pages = [n for n in range(1, natural_no + 1) if n not in found]
    print(f"\n{sum(len(v) for v in glyphs.values())} glyphs for {len(glyphs)} characters -> {a.out_dir}")
    print(f"x-height {meta['x_height_mm']} mm, cap height {meta['cap_height_mm']} mm (as written in the boxes)")
    if missing_pages:
        print("Pages not supplied or unreadable:", missing_pages)
    if missing:
        print("No usable sample for:", " ".join(missing))
    if empty:
        print(f"{len(empty)} empty boxes (fine if a character still has another variant)")
    for p in problems:
        print("WARNING", p)


if __name__ == "__main__":
    main()
