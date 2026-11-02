#!/usr/bin/env python3
"""Build a handwriting glyph set from ordinary photos of someone's writing
(an alphabet sheet, old notes), instead of the printed template.

Two steps, with a labelling step in between:

  1. segment: clean each photo, find letter and word candidates, and draw
     numbered contact sheets.
        python harvest.py segment WORK_DIR page1.jpg page2.jpg ...

  2. Look at WORK_DIR/sheets/*.png and write WORK_DIR/labels.json saying what
     each candidate is (see LABELS below). Claude can do this by reading the
     sheets.

  3. build: cut the labelled letters and words into a glyph set that
     render.py can use.
        python harvest.py build WORK_DIR OUT_GLYPH_DIR

LABELS (labels.json): a flat object. Keys name a page (p0 = first image
given to segment) and either one candidate or a whole text line:

  "p0.g12": "A"            glyph candidate 12 on page 0 is the letter A
  "p0.g12+g13": "i"        candidates 12 and 13 together are one letter
  "p0.L3.g": "A a B b _ C" the glyph candidates of line 3, left to right,
                           one token each; "_" skips a candidate
  "p2.w40": "income"       word candidate 40 is the word "income"
  "p2.L7.w": "the income effect _ leads"   word candidates of line 7 in order
  "p5.L3.t": "the price of a good falls,"   the whole line as written; the
                           script finds the word boundaries itself ("?" = skip)

Unlabelled candidates are ignored, so only label what is clean and certain.
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pageclean as PC  # noqa: E402

BIN_T = 0.20
XH_STD = 4.0          # glyph-space x-height in mm (what meta.x_height_mm says)
OUT_PPM = 12.0        # stored glyph resolution, px per glyph-space mm
XHEIGHT_CHARS = set("acemnorsuvwxz")
SITTERS = set("abcdefhiklmnorstuvwxz→") | set("ABCDEFGHIKLMNOPRSTUVWXYZ") | set("0123456789") | set(".!?:%&#@$")
CAPS_DIGITS = set("ABCDEFGHIJKLMNOPRSTUVWXYZ0123456789")


def font(size):
    for f in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ====================================================================== segment
def text_lines(comps, rules, sp):
    """Assign each component to a text line. Returns {line_no: [comp idx]} and
    the baseline guess (rule y) per line."""
    lines, base = {}, {}
    if len(rules) >= 2:
        ys = np.array([r["y"] for r in rules])
        for i, c in enumerate(comps):
            # a point inside the letter body, above any descender
            cy = c["y0"] + min(0.62 * c["h"], 0.2 * sp)
            cx = int(min(max(c["x0"] + c["w"] // 2, 0), len(rules[0]["yc"]) - 1))
            ys_here = np.array([r["yc"][cx] for r in rules])   # rules can tilt or curve
            k = int(np.searchsorted(ys_here, cy))       # first rule below the body
            lines.setdefault(k, []).append(i)
        for k in lines:
            base[k] = float(ys[k]) if k < len(ys) else float(ys[-1] + sp * (k - len(ys) + 1))
    else:  # unruled paper: cluster by vertical position
        order = sorted(range(len(comps)), key=lambda i: comps[i]["y0"] + comps[i]["h"])
        k, last = 0, None
        for i in order:
            b = comps[i]["y0"] + comps[i]["h"]
            if last is not None and b - last > 0.45 * sp:
                k += 1
            lines.setdefault(k, []).append(i)
            last = b
        for k, idx in lines.items():
            base[k] = float(np.median([comps[i]["y0"] + comps[i]["h"] for i in idx]))
    return lines, base


def group_glyphs(comps, idx, sp):
    """Merge components that stack vertically (i + dot, :, =, %) into glyphs."""
    idx = sorted(idx, key=lambda i: comps[i]["x0"])
    groups = []
    for i in idx:
        c = comps[i]
        placed = False
        for g in groups[-3:]:
            gx0, gx1 = g["x0"], g["x1"]
            ov = min(gx1, c["x0"] + c["w"]) - max(gx0, c["x0"])
            narrow = min(gx1 - gx0, c["w"])
            small = c["area"] < 0.006 * sp * sp
            cx = c["x0"] + c["w"] / 2
            if (narrow > 0 and ov > 0.55 * narrow) or (small and gx0 - 0.08 * sp <= cx <= gx1 + 0.08 * sp):
                g["members"].append(i)
                g["x0"], g["x1"] = min(gx0, c["x0"]), max(gx1, c["x0"] + c["w"])
                placed = True
                break
        if not placed:
            groups.append({"members": [i], "x0": c["x0"], "x1": c["x0"] + c["w"]})
    return groups


def group_words(groups, sp):
    words = []
    for gi, g in enumerate(groups):
        if words and g["x0"] - words[-1]["x1"] < 0.24 * sp:
            words[-1]["glyphs"].append(gi)
            words[-1]["x1"] = max(words[-1]["x1"], g["x1"])
        else:
            words.append({"glyphs": [gi], "x0": g["x0"], "x1": g["x1"]})
    return words


def bbox(comps, members):
    x0 = min(comps[m]["x0"] for m in members)
    y0 = min(comps[m]["y0"] for m in members)
    x1 = max(comps[m]["x0"] + comps[m]["w"] for m in members)
    y1 = max(comps[m]["y0"] + comps[m]["h"] for m in members)
    return [int(x0), int(y0), int(x1), int(y1)]


def segment_page(path, pno, work):
    bgr = PC.load(path)
    r = PC.clean_page(bgr)
    ink = r["ink"]
    h, w = ink.shape
    binm = (ink > BIN_T).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(binm, 8)
    hs = st[1:, 3]
    sp = r["spacing"] or float(np.median(hs[hs > 5]) * 3.2 if (hs > 5).any() else 60)
    min_area = max(6, (0.045 * sp) ** 2)
    comps = []
    keep_ids = np.zeros(n, np.int32)
    for i in range(1, n):
        x, y, ww, hh, a = st[i]
        if a < min_area:
            continue
        big = hh > 1.7 * sp or ww > 4 * sp
        comps.append({"id": i, "x0": int(x), "y0": int(y), "w": int(ww), "h": int(hh), "area": int(a), "big": bool(big)})
        keep_ids[i] = len(comps)            # 1-based index into comps
    labmap = keep_ids[lab].astype(np.uint16)   # 0 = background, k = comps[k-1]

    small = [c for c in comps if not c["big"]]
    sidx = [i for i, c in enumerate(comps) if not c["big"]]
    lines, base = text_lines(small, r["rules"], sp)
    page = {"source": os.path.abspath(path), "pno": pno, "spacing": sp, "angle": r["angle"],
            "size": [w, h], "lines": []}
    gid = wid = 0
    for k in sorted(lines):
        idx = [sidx[i] for i in lines[k]]
        groups = group_glyphs(comps, idx, sp)
        words = group_words(groups, sp)
        L = {"line": int(k), "rule_y": base[k], "glyphs": [], "words": []}
        for g in groups:
            L["glyphs"].append({"id": f"g{gid}", "comps": [comps[m]["id"] for m in g["members"]],
                                "box": bbox(comps, g["members"])})
            g["gid"] = f"g{gid}"
            gid += 1
        for wd in words:
            members = [m for gi in wd["glyphs"] for m in groups[gi]["members"]]
            L["words"].append({"id": f"w{wid}", "glyphs": [groups[gi]["gid"] for gi in wd["glyphs"]],
                               "comps": [comps[m]["id"] for m in members], "box": bbox(comps, members)})
            wid += 1
        page["lines"].append(L)

    # remap component ids to our 1-based compact ids so the label image is small
    remap = {c["id"]: j + 1 for j, c in enumerate(comps)}
    for L in page["lines"]:
        for it in L["glyphs"] + L["words"]:
            it["comps"] = [remap[c] for c in it["comps"]]

    seg = os.path.join(work, "seg")
    os.makedirs(seg, exist_ok=True)
    cv2.imwrite(os.path.join(seg, f"p{pno}_ink.png"), (np.clip(ink, 0, 1) * 255).astype(np.uint8))
    cv2.imwrite(os.path.join(seg, f"p{pno}_lab.png"), labmap)
    with open(os.path.join(seg, f"p{pno}.json"), "w") as f:
        json.dump(page, f)
    return page, ink


def draw_sheets(pages_inks, work, strip_sp=70, max_w=1700, max_h=2000, prefix="sheet", boxes=True):
    out = os.path.join(work, "sheets")
    os.makedirs(out, exist_ok=True)
    f_id = font(13)
    strips = []
    for page, ink in pages_inks:
        sp = page["spacing"]
        s = strip_sp / sp
        vis = (255 - np.clip(ink * 1.8, 0, 1) * 255).astype(np.uint8)
        for L in page["lines"]:
            if not L["glyphs"]:
                continue
            boxes = [g["box"] for g in L["glyphs"]]
            x0 = max(min(b[0] for b in boxes) - int(0.2 * sp), 0)
            x1 = min(max(b[2] for b in boxes) + int(0.2 * sp), ink.shape[1])
            y0 = max(min(b[1] for b in boxes) - int(0.15 * sp), 0)
            y1 = min(max(b[3] for b in boxes) + int(0.15 * sp), ink.shape[0])
            crop = vis[y0:y1, x0:x1]
            sc = min(s, (max_w - 20) / max(crop.shape[1], 1))
            im = Image.fromarray(cv2.resize(crop, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)).convert("RGB")
            pad_t, pad_b = 18, 18
            canvas = Image.new("RGB", (im.width + 70, im.height + pad_t + pad_b + 16), "white")
            canvas.paste(im, (60, pad_t + 16))
            d = ImageDraw.Draw(canvas)
            d.text((4, 4), f"p{page['pno']}.L{L['line']}", fill=(0, 0, 0), font=f_id)
            for g in (L["glyphs"] if boxes else []):
                bx0, by0, bx1, by1 = [(v - o) * sc for v, o in zip(g["box"], (x0, y0, x0, y0))]
                d.rectangle([60 + bx0, pad_t + 16 + by0, 60 + bx1, pad_t + 16 + by1], outline=(225, 60, 60))
                d.text((60 + bx0, pad_t + 16 + by0 - 15), g["id"][1:], fill=(200, 30, 30), font=f_id)
            for wd in (L["words"] if boxes else []):
                if len(wd["glyphs"]) < 2:
                    continue
                bx0, by0, bx1, by1 = [(v - o) * sc for v, o in zip(wd["box"], (x0, y0, x0, y0))]
                d.line([60 + bx0, pad_t + 16 + by1 + 4, 60 + bx1, pad_t + 16 + by1 + 4], fill=(40, 80, 220), width=2)
                d.text((60 + bx0, pad_t + 16 + by1 + 5), "w" + wd["id"][1:], fill=(30, 60, 200), font=f_id)
            strips.append(canvas)
    sheets, cur, hh = [], [], 0
    for st in strips:
        if cur and hh + st.height > max_h:
            sheets.append(cur)
            cur, hh = [], 0
        cur.append(st)
        hh += st.height
    if cur:
        sheets.append(cur)
    paths = []
    for i, sh in enumerate(sheets):
        W = max(s.width for s in sh)
        H = sum(s.height for s in sh)
        img = Image.new("RGB", (W, H), "white")
        y = 0
        for s in sh:
            img.paste(s, (0, y))
            ImageDraw.Draw(img).line([0, y, W, y], fill=(200, 200, 200))
            y += s.height
        p = os.path.join(out, f"{prefix}_{i:02d}.png")
        img.save(p)
        paths.append(p)
    return paths


def cmd_segment(a):
    os.makedirs(a.work, exist_ok=True)
    pis = []
    for pno, path in enumerate(a.images, start=a.start):
        page, ink = segment_page(path, pno, a.work)
        ng = sum(len(L["glyphs"]) for L in page["lines"])
        nw = sum(len(L["words"]) for L in page["lines"])
        print(f"p{pno} {os.path.basename(path)}: {len(page['lines'])} lines, {ng} glyph / {nw} word candidates, "
              f"line spacing {page['spacing']:.0f}px, deskew {page['angle']:.2f} deg")
        pis.append((page, ink))
    for p in draw_sheets(pis, a.work, prefix=f"sheet_p{a.start}" if a.start else "sheet", boxes=not a.lines_only):
        print("sheet:", p)


# ====================================================================== build
def load_page(work, pno):
    seg = os.path.join(work, "seg")
    page = json.load(open(os.path.join(seg, f"p{pno}.json")))
    ink = cv2.imread(os.path.join(seg, f"p{pno}_ink.png"), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.0
    lab = cv2.imread(os.path.join(seg, f"p{pno}_lab.png"), cv2.IMREAD_UNCHANGED)
    return page, ink, lab


EDGE_PUNCT = ".,;:!?'\"()[]"


def align_transcript(L, text, sp):
    """Split a line's letter candidates into the words of its transcript.

    The transcript is the whole line as written, words separated by spaces;
    "?" stands for anything to ignore (a crossed-out word, a stray mark).
    Word boundaries are chosen by dynamic programming: boundaries prefer wide
    gaps, and each piece's width should fit its number of letters. Trailing
    punctuation is cut off the word when it is a separate small mark; words
    that can't be cleaned are dropped. Returns [(comps, box, word)] or None.
    """
    toks = text.split()
    G = sorted(L["glyphs"], key=lambda g: g["box"][0])
    n, m = len(G), len(toks)
    if m == 0 or n < m:
        return None
    x0 = [g["box"][0] for g in G]
    x1 = [g["box"][2] for g in G]
    chars = [max(len(t.strip(EDGE_PUNCT)), 1) if t != "?" else 2 for t in toks]
    total_w = max(x1) - min(x0)
    gaps = [x0[i + 1] - max(x1[: i + 1]) for i in range(n - 1)]
    cw = max(total_w - sum(max(g, 0) for g in gaps if g > 0.15 * sp), 1) / sum(chars)
    gap_unit = max(np.median([g for g in gaps if g > 0] or [1]), 1)
    NEG = -1e18
    best = [[NEG] * (n + 1) for _ in range(m + 1)]
    back = [[0] * (n + 1) for _ in range(m + 1)]
    best[0][0] = 0.0
    for j in range(1, m + 1):
        for i in range(j, n + 1):           # word j covers groups k..i-1
            for k in range(j - 1, i):
                if best[j - 1][k] == NEG:
                    continue
                w = max(x1[k:i]) - x0[k]
                ratio = w / (chars[j - 1] * cw)
                pen = 0.0 if toks[j - 1] == "?" else abs(np.log(max(ratio, 1e-3))) * 3.0
                gap_bonus = (gaps[k - 1] / gap_unit) if k > 0 else 0.0
                # a boundary on a tiny gap is very unlikely
                if k > 0 and gaps[k - 1] < 0.08 * sp:
                    gap_bonus -= 6
                v = best[j - 1][k] + min(gap_bonus, 8) - pen
                if v > best[j][i]:
                    best[j][i], back[j][i] = v, k
    if best[m][n] == NEG:
        return None
    cuts, i = [], n
    for j in range(m, 0, -1):
        k = back[j][i]
        cuts.append((k, i))
        i = k
    cuts.reverse()
    out = []
    for (k, i), tok in zip(cuts, toks):
        if tok == "?":
            continue
        grp = G[k:i]
        w = max(x1[k:i]) - x0[k]
        if not (0.45 < w / (chars[toks.index(tok)] * cw) < 2.4):
            continue
        core = tok
        # peel trailing punctuation off when it is a separate small mark
        ok = True
        while core and core[-1] in EDGE_PUNCT:
            if len(grp) > 1 and (grp[-1]["box"][3] - grp[-1]["box"][1]) < 0.32 * sp and (grp[-1]["box"][2] - grp[-1]["box"][0]) < 0.25 * sp:
                grp = grp[:-1]
                core = core[:-1]
            else:
                ok = False
                break
        if not ok or not core or core[0] in EDGE_PUNCT or "-" in core:
            continue
        comps = [c for g in grp for c in g["comps"]]
        bx = [min(g["box"][0] for g in grp), min(g["box"][1] for g in grp),
              max(g["box"][2] for g in grp), max(g["box"][3] for g in grp)]
        out.append((comps, bx, core))
    return out


def parse_labels(labels, pages):
    """-> list of (kind, pno, line_no, comps, text)"""
    out = []
    index = {}
    for pno, page in pages.items():
        for L in page["lines"]:
            for g in L["glyphs"]:
                index[(pno, g["id"])] = ("g", L, g)
            for w in L["words"]:
                index[(pno, w["id"])] = ("w", L, w)
    for key, text in labels.items():
        if key.startswith("_"):
            continue
        pkey, rest = key.split(".", 1)
        pno = int(pkey[1:])
        if pno not in pages:
            print("WARNING unknown page in", key)
            continue
        if rest.startswith("L"):
            lno_s, kind = rest.split(".")
            lno = int(lno_s[1:])
            L = next((x for x in pages[pno]["lines"] if x["line"] == lno), None)
            if L is None:
                print("WARNING unknown line", key)
                continue
            if kind == "t":
                got = align_transcript(L, text, pages[pno]["spacing"])
                if got is None:
                    print(f"WARNING {key}: transcript did not fit the line, skipped")
                    continue
                for comps, box, word in got:
                    out.append(("w" if len(word) > 1 else "g", pno, lno, comps, box, word))
                continue
            items = L["glyphs"] if kind == "g" else L["words"]
            toks = text.split()
            if len(toks) != len(items):
                print(f"WARNING {key}: {len(toks)} labels for {len(items)} candidates, skipped")
                continue
            for it, t in zip(items, toks):
                if t != "_":
                    out.append(("w" if len(t) > 1 else "g", pno, lno, it["comps"], it["box"], t))
        else:
            ids = rest.split("+")
            found = [index.get((pno, i)) for i in ids]
            if any(f is None for f in found):
                print("WARNING unknown id", key)
                continue
            kind = found[0][0]
            comps = [c for f in found for c in f[2]["comps"]]
            boxes = [f[2]["box"] for f in found]
            box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
            if text != "_":
                kind = "w" if len(text) > 1 else "g"
                out.append((kind, pno, found[0][1]["line"], comps, box, text))
    return out


def cut(ink, lab, comps, box, pad=4):
    x0, y0, x1, y1 = box
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, ink.shape[1]), min(y1 + pad, ink.shape[0])
    sub = ink[y0:y1, x0:x1]
    m = np.isin(lab[y0:y1, x0:x1], comps).astype(np.uint8)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
    a = sub * m
    core = a[m > 0]
    p = np.percentile(core[core > 0.15], 90) if (core > 0.15).sum() > 5 else 1.0
    a = np.clip(a / max(p, 0.2), 0, 1)
    a[a < 0.12] = 0
    # drop specks: leftover bits of ruled lines (thin horizontal dashes) and
    # tiny marks far from the body; keep round dots (i, j) and real strokes
    bm = (a > 0.3).astype(np.uint8)
    n, lab2, st, _ = cv2.connectedComponentsWithStats(bm, 8)
    if n > 2:
        total = st[1:, 4].sum()
        big = int(np.argmax(st[1:, 4])) + 1
        by0, by1 = st[big, 1], st[big, 1] + st[big, 3]
        for i in range(1, n):
            if i == big:
                continue
            x, y, w, h, ar = st[i]
            dash = w > 2.2 * h and ar < 0.06 * total
            far = (y + h < by0 - 0.6 * (by1 - by0) or y > by1 + 0.3 * (by1 - by0)) and ar < 0.05 * total
            if dash or far:
                a[lab2 == i] = 0
    return a, (x0, y0)


def ink_extent(a, t=0.3):
    ys, xs = np.nonzero(a > t)
    return xs.min(), xs.max() + 1, ys.min(), ys.max() + 1


def word_baseline(a, t=0.3):
    """Baseline row and x-height (px) of a word image.

    Baseline: median of the lowest ink pixel per column (descenders occupy
    few columns). x-height: the highest row above the baseline where the row
    still holds a good share of the ink, so ascenders and joining strokes
    don't skew it.
    """
    m = a > t
    cols = np.where(m.any(axis=0))[0]
    bottoms = np.array([np.nonzero(m[:, c])[0].max() for c in cols])
    b = float(np.percentile(bottoms, 55))
    proj = m[: int(b) + 1].sum(axis=1).astype(np.float32)
    if proj.max() <= 0:
        return b, None
    proj = np.convolve(proj, np.ones(3) / 3, mode="same")
    rows = np.where(proj >= 0.45 * proj.max())[0]
    xh = b - rows.min() if len(rows) else None
    return b, float(xh) if xh and xh > 2 else None


def warp(a, rng, strength=1.0):
    """Gentle elastic + shear + weight change: a believable second attempt."""
    h, w = a.shape
    pad = 6
    a = np.pad(a, pad)
    h, w = a.shape
    sigma = max(h, w) / 6
    dx = cv2.GaussianBlur((rng.random((h, w)) - 0.5).astype(np.float32), (0, 0), sigma)
    dy = cv2.GaussianBlur((rng.random((h, w)) - 0.5).astype(np.float32), (0, 0), sigma)
    amp = strength * max(h, w) * 0.035 / max(np.abs(dx).max(), 1e-6)
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    shear = np.tan(np.radians(rng.normal(0, 3.0 * strength)))
    sx = 1 + rng.normal(0, 0.035 * strength)
    sy = 1 + rng.normal(0, 0.035 * strength)
    cx, cy = w / 2, h / 2
    mapx = (gx - cx) / sx + cx + (gy - cy) * shear + dx * amp
    mapy = (gy - cy) / sy + cy + dy * amp
    out = cv2.remap(a.astype(np.float32), mapx.astype(np.float32), mapy.astype(np.float32), cv2.INTER_LINEAR, borderValue=0)
    k = rng.choice([-1, 0, 0, 1])          # stroke weight
    if k:
        ks = np.ones((2, 2), np.uint8)
        out = cv2.dilate(out, ks) * 0.5 + out * 0.5 if k > 0 else cv2.erode(out, ks) * 0.4 + out * 0.6
    return np.clip(out, 0, 1)


def stroke_glyph(ch, rng, xh_px=48.0, weight=None):
    """Simple punctuation drawn as pen strokes, for symbols not in the samples."""
    s = xh_px
    W = int(s * 1.6)
    H = int(s * 2.6)
    base = int(s * 1.9)
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    lw = max(2, int(round(weight or s * 0.11)))
    j = lambda v: v + rng.normal(0, s * 0.03)  # noqa: E731
    cx = W / 2
    top, mid = base - s * 1.45, base - s * 0.5
    def arc(sign):
        pts = []
        for t in np.linspace(-1, 1, 14):
            pts.append((cx - sign * s * 0.25 * (1 - t * t) + sign * s * 0.05, base - s * 0.55 + t * s * 1.0))
        d.line([(j(x), j(y)) for x, y in pts], fill=255, width=lw, joint="curve")
    if ch == "(":
        arc(1)
    elif ch == ")":
        arc(-1)
    elif ch in "/":
        d.line([(j(cx + s * 0.3), j(top)), (j(cx - s * 0.3), j(base + s * 0.1))], fill=255, width=lw)
    elif ch == "|":
        d.line([(j(cx), j(top)), (j(cx), j(base + s * 0.3))], fill=255, width=lw)
    elif ch == "!":
        d.line([(j(cx), j(top)), (j(cx - s * 0.03), j(base - s * 0.35))], fill=255, width=lw)
        d.ellipse([cx - lw * 0.75, base - lw * 1.5, cx + lw * 0.75, base], fill=255)
    elif ch == "+":
        d.line([(j(cx - s * 0.38), j(mid)), (j(cx + s * 0.38), j(mid))], fill=255, width=lw)
        d.line([(j(cx), j(mid - s * 0.38)), (j(cx), j(mid + s * 0.38))], fill=255, width=lw)
    elif ch == "=":
        for yy in (mid - s * 0.17, mid + s * 0.17):
            d.line([(j(cx - s * 0.38), j(yy)), (j(cx + s * 0.38), j(yy))], fill=255, width=lw)
    elif ch == "*":
        for ang in (90, 30, 150):
            r_ = s * 0.3
            dx_, dy_ = r_ * np.cos(np.radians(ang)), r_ * np.sin(np.radians(ang))
            d.line([(j(cx - dx_), j(top + s * 0.4 - dy_)), (j(cx + dx_), j(top + s * 0.4 + dy_))], fill=255, width=lw)
    elif ch in "[]":
        sgn = 1 if ch == "[" else -1
        xl, xr = cx - sgn * s * 0.15, cx + sgn * s * 0.15
        d.line([(j(xr), j(top)), (j(xl), j(top)), (j(xl), j(base + s * 0.3)), (j(xr), j(base + s * 0.3))], fill=255, width=lw, joint="curve")
    elif ch in "<>":
        sgn = 1 if ch == "<" else -1
        d.line([(j(cx + sgn * s * 0.3), j(mid - s * 0.32)), (j(cx - sgn * s * 0.3), j(mid)), (j(cx + sgn * s * 0.3), j(mid + s * 0.32))], fill=255, width=lw, joint="curve")
    elif ch == ".":
        d.ellipse([cx - lw * 0.75, base - lw * 1.5, cx + lw * 0.75, base], fill=255)
    elif ch == ",":
        d.line([(j(cx + lw * 0.3), j(base - lw * 0.8)), (j(cx - s * 0.12), j(base + s * 0.32))], fill=255, width=lw)
    elif ch == "?":
        pts = [(cx - s * 0.32, top + s * 0.3), (cx - s * 0.2, top + s * 0.05), (cx + s * 0.1, top), (cx + s * 0.32, top + s * 0.22),
               (cx + s * 0.22, top + s * 0.5), (cx, top + s * 0.68), (cx, base - s * 0.35)]
        d.line([(j(x), j(y)) for x, y in pts], fill=255, width=lw, joint="curve")
        d.ellipse([cx - lw * 0.75, base - lw * 1.5, cx + lw * 0.75, base], fill=255)
    elif ch == "_":
        d.line([(j(cx - s * 0.5), j(base + s * 0.15)), (j(cx + s * 0.5), j(base + s * 0.15))], fill=255, width=lw)
    else:
        return None, None
    a = np.asarray(img, np.float32) / 255.0
    a = cv2.GaussianBlur(a, (0, 0), 0.7)
    return a, base


def resample(a, ppm_src, baseline_row):
    f = OUT_PPM / ppm_src
    if abs(f - 1) > 0.02:
        pad = 4
        a = np.pad(a, pad)
        a = cv2.resize(a, None, fx=f, fy=f, interpolation=cv2.INTER_AREA if f < 1 else cv2.INTER_CUBIC)
        baseline_row = (baseline_row + pad) * f
        a = np.clip(a, 0, 1)
        if f > 1.3:  # upscaled from a low-res photo: restore a crisp pen edge
            a = cv2.GaussianBlur(a, (0, 0), 0.6 * f / 2)
            t = np.clip((a - 0.22) / 0.42, 0, 1)
        else:        # camera blur on high-res pages: firm the edge a little
            t = np.clip((a - 0.12) / 0.6, 0, 1)
        a = t * t * (3 - 2 * t)
    return np.clip(a, 0, 1), baseline_row


def stroke_width(a):
    """Average pen-stroke width in px: 2 * area / perimeter of the ink."""
    m = (a > 0.5).astype(np.uint8)
    area = m.sum()
    if area < 10:
        return None
    cs, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    per = sum(cv2.arcLength(c, True) for c in cs)
    return 2.0 * area / max(per, 1)


def set_weight(a, have, want):
    """Thicken or thin strokes so every source page has the same pen weight."""
    if not have:
        return a
    d = (want - have) / 2.0          # px to add on each side
    if abs(d) < 0.35:
        return a
    r = abs(d)
    k = int(np.ceil(r)) * 2 + 1
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    full = cv2.dilate(a, ker) if d > 0 else cv2.erode(a, ker)
    frac = min(r / np.ceil(r), 1.0)
    return np.clip(a + (full - a) * frac, 0, 1)


def save_variant(out_dir, key, k, a, ppm_src, baseline_row, ch_for_sit=None):
    """Resample to OUT_PPM (if needed), crop, save, and return the metrics dict."""
    if ppm_src != OUT_PPM:
        a, baseline_row = resample(a, ppm_src, baseline_row)
    if (a > 0.3).sum() < 3:
        return None
    x0, x1, y0, y1 = ink_extent(a)
    pad = 3
    X0, Y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    crop = a[Y0:y1 + pad, X0:x1 + pad]
    fname = f"{key}_{k}.png"
    Image.fromarray((np.clip(crop, 0, 1) * 255).astype(np.uint8)).save(os.path.join(out_dir, "g", fname))
    return {
        "file": f"g/{fname}",
        "w": round((x1 - x0) / OUT_PPM, 3),
        "h": round((y1 - y0) / OUT_PPM, 3),
        "desc": round((y1 - baseline_row) / OUT_PPM, 3),
        "top": round((baseline_row - y0) / OUT_PPM, 3),
        "pad_px": [int(x0 - X0), int(y0 - Y0)],
        "ppm": OUT_PPM,
    }


def safe_key(s):
    return "_".join(f"{ord(c):x}" for c in s) if len(s) <= 3 else "w_" + "".join(c if c.isalnum() else f"{ord(c):x}" for c in s)


def cmd_build(a):
    labels = json.load(open(os.path.join(a.work, "labels.json"), encoding="utf-8"))
    pnos = sorted({int(k.split(".")[0][1:]) for k in labels if not k.startswith("_")})
    pages, inks, labs = {}, {}, {}
    for p in pnos:
        pages[p], inks[p], labs[p] = load_page(a.work, p)
    items = parse_labels(labels, pages)
    drop = set(labels.get("_drop", []))     # e.g. "This@p8": bad sample of a word on a page
    items = [it for it in items if f"{it[5]}@p{it[1]}" not in drop and f"{it[5]}@p{it[1]}.L{it[2]}" not in drop
             and f"@p{it[1]}.L{it[2]}" not in drop]
    rng = np.random.default_rng(a.seed)

    # ---- pass 1: cut everything in page pixels, measure
    cuts = []
    for kind, pno, lno, comps, box, text in items:
        alpha, (ox, oy) = cut(inks[pno], labs[pno], comps, box)
        if (alpha > 0.3).sum() < 4:
            continue
        x0, x1, y0, y1 = ink_extent(alpha)
        cuts.append({"kind": kind, "pno": pno, "line": lno, "text": text, "alpha": alpha,
                     "top_px": oy + y0, "bot_px": oy + y1, "oy": oy, "h_px": y1 - y0})

    # ---- baselines
    by_line = {}
    for c in cuts:
        if c["kind"] == "g" and c["text"] in SITTERS:
            by_line.setdefault((c["pno"], c["line"]), []).append(c["bot_px"])
    for c in cuts:
        if c["kind"] == "w":
            b, xh = word_baseline(c["alpha"])
            c["base_px"] = c["oy"] + b
            c["xh_px"] = xh
        else:
            sitters = by_line.get((c["pno"], c["line"]))
            if c["text"] in SITTERS:
                c["base_px"] = c["bot_px"]
            elif sitters and len(sitters) >= 2:
                c["base_px"] = float(np.median(sitters))
            else:
                L = next(x for x in pages[c["pno"]]["lines"] if x["line"] == c["line"])
                c["base_px"] = L["rule_y"]

    # ---- per-page x-height in pixels
    xh, caph = {}, {}
    # Letter boxes and word profiles measure x-height differently. A page that
    # has both (e.g. an alphabet sheet with a pangram) gives the conversion.
    g_xh, w_xh = {}, {}
    for p in pnos:
        g_x = [c["h_px"] for c in cuts if c["pno"] == p and c["kind"] == "g" and c["text"] in XHEIGHT_CHARS]
        w_x = [c["xh_px"] for c in cuts if c["pno"] == p and c["kind"] == "w" and c["xh_px"]]
        caps = [c["h_px"] for c in cuts if c["pno"] == p and c["kind"] == "g" and c["text"] in CAPS_DIGITS]
        if len(g_x) >= 3:
            g_xh[p] = float(np.median(g_x))
        if len(w_x) >= 3:
            w_xh[p] = float(np.median(w_x))
        if len(caps) >= 3:
            caph[p] = float(np.median(caps))
    both = [g_xh[p] / w_xh[p] for p in pnos if p in g_xh and p in w_xh and len([1 for c in cuts if c["pno"] == p and c["kind"] == "g" and c["text"] in XHEIGHT_CHARS]) >= 8]
    word_to_letter = float(np.median(both)) if both else 1.0
    print(f"word/letter x-height calibration: x{word_to_letter:.2f}")
    for p in pnos:
        n_words = len([1 for c in cuts if c["pno"] == p and c["kind"] == "w"])
        n_xg = len([1 for c in cuts if c["pno"] == p and c["kind"] == "g" and c["text"] in XHEIGHT_CHARS])
        if p in w_xh and n_words >= max(3, n_xg):     # notes pages: trust the words
            xh[p] = w_xh[p] * word_to_letter
        elif p in g_xh:
            xh[p] = g_xh[p]
        elif p in w_xh:
            xh[p] = w_xh[p] * word_to_letter
    ratios = [caph[p] / xh[p] for p in pnos if p in xh and p in caph]
    cap_ratio = float(np.median(ratios)) if ratios else 1.5
    for p in pnos:
        if p not in xh and p in caph:
            xh[p] = caph[p] / cap_ratio
        if p not in xh:
            print(f"WARNING p{p}: cannot work out writing size, page skipped")
    print("x-height px per page:", {f"p{p}": round(v, 1) for p, v in xh.items()}, "cap/x ratio", round(cap_ratio, 2))

    # ---- pass 2: save
    os.makedirs(os.path.join(a.out, "g"), exist_ok=True)
    glyphs, words = {}, {}
    for c in cuts:
        if c["pno"] not in xh:
            continue
        c["img"], c["brow"] = resample(c["alpha"], xh[c["pno"]] / XH_STD, c["base_px"] - c["oy"])
        c["sw"] = stroke_width(c["img"])
    per_page = []
    for p in pnos:
        ws = [c["sw"] for c in cuts if c["pno"] == p and c.get("sw") and c["text"] not in ".,:;'\"-•"]
        if ws:
            per_page.append(float(np.median(ws)))
    target_w = float(np.median(per_page)) if per_page else OUT_PPM * 0.4
    print(f"pen weight: {target_w / OUT_PPM:.2f} mm at x-height {XH_STD} mm (all samples normalised to it)")
    for c in cuts:
        if "img" not in c:
            continue
        img = set_weight(c["img"], c["sw"], target_w)
        bank = glyphs if c["kind"] == "g" else words
        key = c["text"]
        k = len(bank.get(key, []))
        v = save_variant(a.out, safe_key(key), k, img, OUT_PPM, c["brow"])
        if v:
            v["src"] = f"p{c['pno']}.L{c['line']}"
            bank.setdefault(key, []).append(v)

    # reject samples far from the others of the same letter/word: they almost
    # always carry a neighbour's stroke or lost part of themselves
    rejected = []
    # words whose width doesn't fit their letter count picked up a neighbour or lost a part
    per = [v["w"] / len(k) for k, vs in words.items() for v in vs if len(k) >= 3]
    if per:
        mper = float(np.median(per))
        for k, vs in words.items():
            keep = []
            for v in vs:
                r = v["w"] / (len(k) * mper)
                if 0.5 < r < 1.9 or v.get("synthetic"):
                    keep.append(v)
                else:
                    rejected.append(f"{k}({v.get('src')})")
            words[k] = keep
        for k in [k for k, vs in words.items() if not vs]:
            del words[k]
    for bank in (glyphs, words):
        for key, vs in bank.items():
            if len(vs) < 3 or (bank is glyphs and not key.islower()):
                continue
            mh = float(np.median([v["h"] for v in vs]))
            keep = []
            for v in vs:
                if abs(v["h"] - mh) > 0.4 * mh:
                    rejected.append(f"{key}({v.get('src')})")
                else:
                    keep.append(v)
            bank[key] = keep
    if rejected:
        print("Rejected as outliers:", ", ".join(rejected))

    # symbols whose height on the page is set by convention, not by where the
    # sample happened to sit: centre them at a fixed height above the baseline
    CENTRE = {"+": 0.5, "=": 0.5, "-": 0.5, "\u2022": 0.5, "\u2192": 0.5, "*": 1.0, "~": 0.5,
              "(": 0.55, ")": 0.55, "[": 0.55, "]": 0.55, "<": 0.5, ">": 0.5}
    TOPS = {"'": 1.65, '"': 1.65}
    for ch, vs in glyphs.items():
        for v in vs:
            if ch in CENTRE:
                c = CENTRE[ch] * XH_STD
                v["desc"] = round(v["h"] / 2 - c, 3)
                v["top"] = round(c + v["h"] / 2, 3)
            elif ch in TOPS:
                t = TOPS[ch] * XH_STD
                v["top"] = round(t, 3)
                v["desc"] = round(v["h"] - t, 3)

    # sitters: force exactly onto the baseline
    for ch, vs in glyphs.items():
        if ch in SITTERS:
            for v in vs:
                v["top"] = round(v["top"] + v["desc"], 3)
                v["desc"] = 0.0

    # ---- synthetic variants so repeats never look stamped
    def add_synth(bank, key, target, strength):
        vs = bank[key]
        real = [v for v in vs if not v.get("synthetic")]
        i = 0
        while len(vs) < target and real:
            src = real[i % len(real)]
            img = np.asarray(Image.open(os.path.join(a.out, src["file"])), np.float32) / 255.0
            base_row = src["pad_px"][1] + src["top"] * OUT_PPM
            w_ = warp(img, rng, strength)
            base_row += 6  # warp pads by 6px
            v = save_variant(a.out, safe_key(key), len(vs), w_, OUT_PPM, base_row)
            if v:
                v["synthetic"] = True
                if key in SITTERS:
                    v["top"] = round(v["top"] + v["desc"], 3)
                    v["desc"] = 0.0
                vs.append(v)
            i += 1
    for ch in list(glyphs):
        add_synth(glyphs, ch, a.letter_variants, 1.0)
    for wd in list(words):
        add_synth(words, wd, a.word_variants, 0.6)

    # ---- simple punctuation drawn as strokes if missing
    weight = target_w
    drawn = []
    for ch in "()/|!+=*[]<>.,_?":
        if ch in glyphs:
            continue
        for k in range(2):
            img, base_row = stroke_glyph(ch, rng, xh_px=XH_STD * OUT_PPM, weight=weight)
            if img is None:
                break
            v = save_variant(a.out, safe_key(ch), k, img, OUT_PPM, base_row)
            if v:
                v["drawn"] = True
                glyphs.setdefault(ch, []).append(v)
        drawn.append(ch)

    meta = {"px_per_mm": OUT_PPM, "x_height_mm": XH_STD, "style": {"word_space": a.word_space, "letter_space": a.letter_space},
            "cap_height_mm": round(XH_STD * cap_ratio, 3), "source": "harvest", "words": len(words)}
    with open(os.path.join(a.out, "glyphs.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "glyphs": glyphs, "words": words}, f, indent=1, ensure_ascii=False)

    # review sheet
    review(a.out, glyphs, words)
    real = {ch: sum(1 for v in vs if not v.get("synthetic") and not v.get("drawn")) for ch, vs in glyphs.items()}
    want = list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,;:!?'\"()-/&@%+=#*")
    print(f"{len(glyphs)} characters ({sum(real.values())} real samples), {len(words)} whole words "
          f"({sum(len(v) for v in words.values())} images incl. variants) -> {a.out}")
    missing = [c for c in want if c not in glyphs]
    if missing:
        print("Still missing:", " ".join(missing))
    if drawn:
        print("Drawn as pen strokes (not in your samples):", " ".join(drawn))
    single = sorted(ch for ch, n in real.items() if n == 1)
    print("Only one real sample (variants are warped copies):", "".join(single))


def review(out, glyphs, words):
    cell = 80
    items = [(ch, v) for ch, vs in sorted(glyphs.items()) for v in vs]
    cols = 18
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), "white")
    d = ImageDraw.Draw(sheet)
    f = font(11)
    for i, (ch, v) in enumerate(items):
        r, c = divmod(i, cols)
        im = Image.open(os.path.join(out, v["file"]))
        im.thumbnail((cell - 12, cell - 22))
        ox, oy = c * cell + (cell - im.width) // 2, r * cell + 16 + (cell - 20 - im.height) // 2
        sheet.paste(Image.new("RGB", im.size, (20, 30, 100)), (ox, oy), im)
        tag = ch + (" s" if v.get("synthetic") else " d" if v.get("drawn") else "")
        d.text((c * cell + 3, r * cell + 2), tag, fill=(200, 40, 40), font=f)
        d.rectangle([c * cell, r * cell, (c + 1) * cell - 1, (r + 1) * cell - 1], outline=(225, 225, 225))
    sheet.save(os.path.join(out, "review_letters.png"))
    if words:
        witems = [(wd, v) for wd, vs in sorted(words.items()) for v in vs if not v.get("synthetic")]
        cw, ch_ = 220, 70
        cols = 7
        rows = (len(witems) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cw, max(rows, 1) * ch_), "white")
        d = ImageDraw.Draw(sheet)
        for i, (wd, v) in enumerate(witems):
            r, c = divmod(i, cols)
            im = Image.open(os.path.join(out, v["file"]))
            im.thumbnail((cw - 10, ch_ - 20))
            sheet.paste(Image.new("RGB", im.size, (20, 30, 100)), (c * cw + 5, r * ch_ + 16), im)
            d.text((c * cw + 3, r * ch_ + 2), wd, fill=(200, 40, 40), font=f)
            d.rectangle([c * cw, r * ch_, (c + 1) * cw - 1, (r + 1) * ch_ - 1], outline=(225, 225, 225))
        sheet.save(os.path.join(out, "review_words.png"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("segment")
    s1.add_argument("work")
    s1.add_argument("images", nargs="+")
    s1.add_argument("--start", type=int, default=0, help="page number of the first image (to add or redo pages)")
    s1.add_argument("--lines-only", action="store_true", help="sheets show just the numbered lines (for whole-line transcripts)")
    s2 = sub.add_parser("build")
    s2.add_argument("work")
    s2.add_argument("out")
    s2.add_argument("--letter-variants", type=int, default=3)
    s2.add_argument("--word-variants", type=int, default=2)
    s2.add_argument("--seed", type=int, default=5)
    s2.add_argument("--word-space", type=float, default=0.95, help="suggested gap between words (x-heights), stored for render.py")
    s2.add_argument("--letter-space", type=float, default=0.14, help="suggested gap between letters (x-heights)")
    a = ap.parse_args()
    if a.cmd == "segment":
        cmd_segment(a)
    else:
        cmd_build(a)


if __name__ == "__main__":
    main()
