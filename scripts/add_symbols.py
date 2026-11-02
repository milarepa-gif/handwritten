#!/usr/bin/env python3
"""Add symbols (maths, Greek, accents, fractions, ticks ...) from photos of your
own handwriting to an existing handwriting set.

The photos can be any page where you wrote the symbols once or twice, ideally on
ruled paper next to a few capital letters (see "Calibration" below).

  1. Segment the photos (same tool as for notes):
        python harvest.py segment WORK photo1.jpg photo2.jpg
     Rotate sideways photos upright first.

  2. Look at WORK/sheets/*.png and write WORK/labels.json (format below).

  3. Add the symbols to a hand set, which is changed in place:
        python add_symbols.py WORK assets/default_hand      # or my_hand/
     Add --dry-run to check the labels without writing anything.

labels.json uses the same keys as harvest.py:

  "p0.g103": "+"            candidate 103 on page 0 is a plus
  "p0.g112+g113": "≥"        two pieces that make one symbol
  "p0.g147": "cal:H"        a capital H used only to measure your writing size
  "p0.g20": "cal:\u00e2"        ...or any character the set already has (here an a with circumflex)
  "_script": {              pairs (base, small) measuring your super/subscripts:
      "sup": [["p0.g115", "p0.g116"]],        x and the raised 2 in x^2
      "sub": [["p0.g147", "p0.g148"]]}        H and the lowered 2 in H_2O

Calibration: this tool works out how big your writing is on each photo by
comparing marks labelled "cal:X" with the same character X already in the hand
set: capitals (cal:H, cal:T ...) are compared with the set's capital height, any
other character with the set's own samples of it. Label at least three per
photo; use plain letters or symbols you wrote at normal size, not ones that are
resized on purpose (fractions, circled digits). The _script pairs are optional;
without them the renderer uses typical values.

Accented letters (e grave, o diaeresis, ...) are also split into the letter and
the accent mark, so the renderer can put the same marks on other letters.
"""
import argparse
import json
import os
import sys
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harvest as H  # noqa: E402

OUT_PPM, XH = H.OUT_PPM, H.XH_STD

# How each symbol is placed on the line. Heights are in x-heights (1.0 = the
# height of a small "a"); "fit" is the finished symbol height in capital
# heights. Anything not listed sits its ink bottom on the baseline.
#   sit    ink bottom is the baseline
#   line   baseline is taken from the sitting symbols on the same line of the photo
#   centre vertical centre at the given height above the baseline
#   top    ink top at the given height above the baseline
#   like   copy size and position from the same character already in the set
SPEC = {}
for _c in "èöüâàêñαδθλσωΔΣΩπ√€£åìòù":
    SPEC[_c] = ("sit",)
for _c in "βγμφ$₹ç":
    SPEC[_c] = ("line",)
SPEC.update({
    "+": ("centre", 0.5), "-": ("centre", 0.5), "=": ("centre", 0.5),
    "±": ("centre", 0.6), "×": ("centre", 0.55), "÷": ("centre", 0.55),
    "≠": ("centre", 0.55), "≤": ("centre", 0.55), "≥": ("centre", 0.55),
    "✓": ("centre", 0.65), "✗": ("centre", 0.65),
    "①": ("centre", 0.75, 1.25), "②": ("centre", 0.75, 1.25), "③": ("centre", 0.75, 1.25),
    "½": ("centre", 0.75, 1.4), "⅓": ("centre", 0.75, 1.4),
    "¼": ("centre", 0.75, 1.4), "¾": ("centre", 0.75, 1.4),
    "{": ("centre", 0.6, 1.5), "}": ("centre", 0.6, 1.5),
    "~": ("centre", 0.5), "^": ("top", 1.6), "\\": ("like", "/"),
    "(": ("like", "("), ")": ("like", ")"), "[": ("centre", 0.6, 1.5), "]": ("centre", 0.6, 1.5),
    "—": ("centre", 0.5), "°": ("top", 1.75, 0.4),
    "★": ("centre", 0.8, 1.0),
    "⇒": ("centre", 0.55), "←": ("centre", 0.5), "→": ("centre", 0.5),
    "↑": ("centre", 0.7), "↓": ("centre", 0.7),
    "∞": ("centre", 0.5), "≈": ("centre", 0.55), "∴": ("centre", 0.5),
    "©": ("centre", 0.75, 1.0), "®": ("centre", 0.75, 1.0),
})

ACCENT_MARKS = {"è": "̀", "à": "̀", "ö": "̈", "ü": "̈",
                "â": "̂", "ê": "̂", "ñ": "̃",
                "å": "̊", "ò": "̀", "ù": "̀"}


def median(xs):
    return float(np.median(xs))


def cap_mm_reference(glyphs, chars):
    """Median height (mm) of each of these capitals in the hand set, averaged."""
    hs = []
    for c in chars:
        vs = [v["h"] for v in glyphs.get(c, [])]
        if vs:
            hs.append(median(vs))
    return float(np.mean(hs)) if hs else None


def target_weight(out_dir, glyphs):
    ws = []
    for c in "aeonst":
        for v in glyphs.get(c, [])[:3]:
            a = np.asarray(Image.open(os.path.join(out_dir, v["file"])), np.float32) / 255.0
            w = H.stroke_width(a)
            if w:
                ws.append(w)
    return median(ws) if ws else OUT_PPM * 0.4


def split_marks(lab, comps):
    """(body_comps, mark_comps) of an accented letter's pieces on the label image."""
    info = []
    for c in comps:
        ys, xs = np.nonzero(lab == c)
        if len(ys):
            info.append((c, ys.min(), ys.max() + 1, len(ys)))
    if len(info) < 2:
        return comps, []
    body = max(info, key=lambda t: t[3])
    bh = body[2] - body[1]
    marks = [t[0] for t in info if t[0] != body[0] and t[2] <= body[1] + 0.4 * bh]
    rest = [t[0] for t in info if t[0] not in marks]
    return rest, marks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("hand", help="handwriting set to add to (changed in place)")
    ap.add_argument("--labels", help="labels file (default: WORK/labels.json)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--variants", type=int, default=3, help="make at least this many variants of each new character")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    labels = json.load(open(a.labels or os.path.join(a.work, "labels.json"), encoding="utf-8"))
    script = labels.get("_script", {})
    gj = os.path.join(a.hand, "glyphs.json")
    data = json.load(open(gj, encoding="utf-8"))
    glyphs = data["glyphs"]
    rng = np.random.default_rng(a.seed)

    pnos = sorted({int(k.split(".")[0][1:]) for k in labels if not k.startswith("_")})
    pages, inks, labs = {}, {}, {}
    for p in pnos:
        pages[p], inks[p], labs[p] = H.load_page(a.work, p)
    items = H.parse_labels(labels, pages)

    # ---- cut every labelled piece
    cuts = []
    for kind, pno, lno, comps, box, text in items:
        alpha, (ox, oy) = H.cut(inks[pno], labs[pno], comps, box)
        if (alpha > 0.3).sum() < 4:
            continue
        x0, x1, y0, y1 = H.ink_extent(alpha)
        cuts.append({"pno": pno, "line": lno, "text": text, "comps": comps, "alpha": alpha, "ox": ox, "oy": oy,
                     "top": oy + y0, "bot": oy + y1, "h": y1 - y0})

    # ---- how big is the writing on each photo? Marks labelled cal:X are compared with the same
    # character already in the set (capitals: with the set's median capital height, because single
    # capitals in a hand set vary too much to compare one by one).
    cap_mm = data["meta"].get("cap_height_mm") or cap_mm_reference(glyphs, "HCO")
    cal = {}
    for c in cuts:
        if c["text"].startswith("cal:"):
            cal.setdefault(c["pno"], []).append((c["h"], c["text"][4:]))
    ppm = {}
    for p in pnos:
        ests = []
        for h_px, X in cal.get(p, []):
            if len(X) == 1 and X.isascii() and X.isupper():
                ests.append(h_px / cap_mm)
            else:
                refs = [v["h"] for v in glyphs.get(X, []) if not v.get("synthetic")]
                if refs:
                    ests.append(h_px / median(refs))
        if len(ests) < 3:
            sys.exit(f"page p{p}: label at least three marks as \"cal:H\" (a capital) or \"cal:\u00e2\" (any character "
                     f"the set already has) so the writing size can be worked out (usable: {len(ests)})")
        ppm[p] = median(ests)
        spread = (max(ests) - min(ests)) / ppm[p]
        print(f"p{p}: {len(ests)} calibration marks -> {ppm[p]:.2f} px per mm of the hand set"
              + (f" (spread {spread:.0%}, check the labels)" if spread > 0.35 else ""))

    # ---- super/subscript measurements from the (base, small) pairs
    boxes = {}
    for p, pg in pages.items():
        for L in pg["lines"]:
            for g in L["glyphs"]:
                boxes[(p, g["id"])] = g["box"]

    def pair_stats(pairs, sign):
        sc, off = [], []
        for base, small in pairs:
            pb, gb = base.split(".")
            ps, gs = small.split(".")
            b, s = boxes[(int(pb[1:]), gb)], boxes[(int(ps[1:]), gs)]
            capx = ppm[int(pb[1:])] * cap_mm
            sc.append((s[3] - s[1]) / capx)
            off.append(sign * (b[3] - s[3]) / capx)
        return median(sc), median(off)

    if script.get("sup") and script.get("sub"):
        ss, so = pair_stats(script["sup"], 1)
        bs, bo = pair_stats(script["sub"], -1)
        data["meta"]["script"] = {"sup_scale": round(ss, 3), "sup_raise": round(so, 3),
                                  "sub_scale": round(bs, 3), "sub_drop": round(bo, 3),
                                  "cap_mm": round(cap_mm, 3)}
        print("superscript: %.2f of cap height, raised %.2f caps;  subscript: %.2f, lowered %.2f caps"
              % (ss, so, bs, bo))

    # ---- baselines
    sitters = {}
    for c in cuts:
        if SPEC.get(c["text"], ("sit",))[0] == "sit":
            sitters.setdefault((c["pno"], c["line"]), []).append(c["bot"])
    wt = target_weight(a.hand, glyphs)
    new, mark_src = {}, []
    for c in cuts:
        ch = c["text"]
        if ch.startswith("cal:"):
            continue
        spec = SPEC.get(ch, ("sit",))
        mode = spec[0]
        L = next(x for x in pages[c["pno"]]["lines"] if x["line"] == c["line"])
        group = sitters.get((c["pno"], c["line"]), [])
        if mode == "sit":
            base = c["bot"]
        elif mode == "line":
            base = median(group) if len(group) >= 2 else L["rule_y"]
        else:
            base = c["bot"]            # fixed afterwards from conventions
        eff = ppm[c["pno"]]
        fit = None
        if mode == "like":
            like = glyphs.get(spec[1])
            if like:
                fit = median([v["h"] for v in like]) / cap_mm
        elif len(spec) >= 3:
            fit = spec[2]
        if fit:
            eff = c["h"] / (fit * cap_mm)      # px per mm that makes the symbol `fit` capitals tall
        img, brow = H.resample(c["alpha"], eff, base - c["oy"])
        sw = H.stroke_width(img)
        img = H.set_weight(img, sw, wt)
        new.setdefault(ch, []).append({"img": img, "brow": brow, "spec": spec, "c": c})
        if ch in ACCENT_MARKS:
            mark_src.append((ch, c, base))

    # ---- save glyphs
    os.makedirs(os.path.join(a.hand, "g"), exist_ok=True)
    added, dropped = {}, []
    for ch, vs in new.items():
        existing = glyphs.get(ch, [])
        start = len(existing)
        kept = []
        for i, it in enumerate(vs):
            k = start + i
            while os.path.exists(os.path.join(a.hand, "g", f"{H.safe_key(ch)}_{k}.png")):
                k += 1
            v = None if a.dry_run else H.save_variant(a.hand, H.safe_key(ch), k, it["img"], OUT_PPM, it["brow"])
            if a.dry_run:
                v = {"file": "dry", "w": 0, "h": 0, "desc": 0, "top": 0, "pad_px": [0, 0], "ppm": OUT_PPM}
            if not v:
                continue
            spec = it["spec"]
            if spec[0] == "centre":
                cen = spec[1] * XH
                v["desc"], v["top"] = round(v["h"] / 2 - cen, 3), round(cen + v["h"] / 2, 3)
            elif spec[0] == "top":
                t = spec[1] * XH
                v["top"], v["desc"] = round(t, 3), round(v["h"] - t, 3)
            elif spec[0] == "like":
                lk = glyphs.get(spec[1])
                if lk:
                    v["top"] = round(median([x["top"] for x in lk]), 3)
                    v["desc"] = round(median([x["desc"] for x in lk]), 3)
            elif spec[0] == "sit":
                v["top"], v["desc"] = round(v["top"] + v["desc"], 3), 0.0
            kept.append(v)
        if kept:
            ref = median([v["h"] for v in kept])
            old_ok = []
            for v in glyphs.get(ch, []):
                if ref and abs(v["h"] - ref) > 0.5 * ref and not a.dry_run:      # wildly off-size: a bad old sample
                    try:
                        os.remove(os.path.join(a.hand, v["file"]))
                    except OSError:
                        pass
                    dropped.append(ch)
                else:
                    old_ok.append(v)
            glyphs[ch] = old_ok
            glyphs[ch].extend(kept)
            added[ch] = len(kept)
        # a few warped copies so repeats never look stamped
        have = glyphs.get(ch, [])
        real = [v for v in have if not v.get("synthetic") and v["file"] != "dry"]
        i = 0
        while len(glyphs.get(ch, [])) < a.variants and real and not a.dry_run and ch in added:
            src = real[i % len(real)]
            img = np.asarray(Image.open(os.path.join(a.hand, src["file"])), np.float32) / 255.0
            row = src["pad_px"][1] + src["top"] * OUT_PPM
            w_ = H.warp(img, rng, 1.0)
            kk = len(glyphs[ch])
            while os.path.exists(os.path.join(a.hand, "g", f"{H.safe_key(ch)}_{kk}.png")):
                kk += 1
            sv = H.save_variant(a.hand, H.safe_key(ch), kk, w_, OUT_PPM, row + 6)
            if sv:
                sv["synthetic"] = True
                if SPEC.get(ch, ("sit",))[0] == "sit":
                    sv["top"], sv["desc"] = round(sv["top"] + sv["desc"], 3), 0.0
                elif SPEC[ch][0] in ("centre", "top"):
                    sv["top"], sv["desc"] = src["top"], round(src["desc"] + (sv["h"] - src["h"]) * 0, 3)
                glyphs[ch].append(sv)
            i += 1
            if i > 8:
                break

    # ---- accent marks, cut off the accented letters, so any letter can take them
    marks = {}
    for ch, c, base in mark_src:
        lab = labs[c["pno"]]
        body, mk = split_marks(lab, c["comps"])
        if not mk:
            print(f"note: could not separate the accent from {ch}; it can be used only as the whole letter")
            continue
        box = [int(1e9), int(1e9), 0, 0]
        ys, xs = np.nonzero(np.isin(lab, mk))
        box = [xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]
        alpha, (ox, oy) = H.cut(inks[c["pno"]], lab, mk, box)
        if (alpha > 0.3).sum() < 3:
            continue
        img, brow = H.resample(alpha, ppm[c["pno"]], base - oy)
        img = H.set_weight(img, H.stroke_width(img), wt)
        marks.setdefault(ACCENT_MARKS[ch], []).append((img, brow))
    for mch, vs in marks.items():
        out = []
        for i, (img, brow) in enumerate(vs):
            if not a.dry_run:
                v = H.save_variant(a.hand, H.safe_key(mch), len(glyphs.get(mch, [])) + i, img, OUT_PPM, brow)
                if v:
                    out.append(v)
        if out:
            glyphs.setdefault(mch, []).extend(out)
            added[f"mark {unicodedata.name(mch, mch)}"] = len(out)
    if "̀" in glyphs and "́" not in glyphs and not a.dry_run:
        # acute is the grave accent mirrored
        outv = []
        for i, v in enumerate(glyphs["̀"]):
            im = ImageOps.mirror(Image.open(os.path.join(a.hand, v["file"])))
            fn = f"{H.safe_key(chr(0x301))}_{i}.png"
            im.save(os.path.join(a.hand, "g", fn))
            nv = dict(v)
            nv["file"] = f"g/{fn}"
            right_pad = im.width - v["pad_px"][0] - round(v["w"] * OUT_PPM)
            nv["pad_px"] = [max(right_pad, 0), v["pad_px"][1]]
            outv.append(nv)
        glyphs["́"] = outv
        added["mark ACUTE (mirrored grave)"] = len(outv)

    data["meta"]["symbols"] = sorted(c for c in glyphs if not (c.isalnum() and ord(c) < 128))
    print("added:", ", ".join(f"{k} x{n}" for k, n in added.items()))
    if dropped:
        print("dropped old samples that were far off-size:", " ".join(sorted(set(dropped))), f"({len(dropped)} images)")
    if a.dry_run:
        print("dry run: nothing written")
        return
    with open(gj, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(glyphs)} characters now in {a.hand}")


if __name__ == "__main__":
    main()
