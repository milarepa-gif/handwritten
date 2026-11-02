"""Clean a photo of handwriting on (ruled) paper.

Produces an ink map (0..1, how strongly each pixel is pen ink) with ruled
lines, margin lines and paper shading removed, plus the positions of the ruled
lines, which are used as baselines and as a size reference.

Used by harvest.py. Nothing here is specific to one person's handwriting.
"""
import cv2
import numpy as np
from PIL import Image, ImageOps

try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except Exception:
    pass


def load(path):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)


def darkness(bgr):
    """0..1 darkness of thin dark marks relative to the paper right around them.

    A morphological closing wider than any pen stroke gives the local paper
    brightness, so shadows, page curl and the desk around the page drop out
    and only stroke-sized marks (ink, ruled lines) remain.
    """
    v = bgr.mean(axis=2).astype(np.float32)
    k = int(max(9, round(min(v.shape) / 90))) | 1
    paper = cv2.morphologyEx(v, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    paper = cv2.GaussianBlur(paper, (0, 0), k / 4)
    d = np.clip((paper - v) / np.maximum(paper, 30.0), 0, 1)
    d[paper < 70] = 0          # very dark surroundings (desk, binding shadow)
    return d


def reddish(bgr):
    """Mask of red/pink pixels (notebook margin lines, red pen)."""
    b, g, r = [bgr[..., i].astype(np.int16) for i in range(3)]
    return (r - np.maximum(b, g)) > 22


def estimate_skew(mask):
    """Angle in degrees that makes horizontal structure most horizontal."""
    h, w = mask.shape
    k = 900.0 / max(h, w)
    small = cv2.resize(mask.astype(np.uint8) * 255, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    best, best_a = -1, 0.0
    for a in np.arange(-8, 8.01, 0.25):
        M = cv2.getRotationMatrix2D((small.shape[1] / 2, small.shape[0] / 2), a, 1)
        rot = cv2.warpAffine(small, M, (small.shape[1], small.shape[0]))
        s = np.var(rot.sum(axis=1).astype(np.float64))
        if s > best:
            best, best_a = s, a
    # refine
    for a in np.arange(best_a - 0.25, best_a + 0.26, 0.05):
        M = cv2.getRotationMatrix2D((small.shape[1] / 2, small.shape[0] / 2), a, 1)
        rot = cv2.warpAffine(small, M, (small.shape[1], small.shape[0]))
        s = np.var(rot.sum(axis=1).astype(np.float64))
        if s > best:
            best, best_a = s, a
    return float(best_a)


def rotate(img, angle, border):
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=border)


def _fit_curve(xs, ys, w, seg=120):
    """Centre line y(x) over the full width from a line's pixels.

    Piecewise medians over short segments follow page curl; outlier segments
    (underlines, letter bottoms) are dropped against their neighbours.
    """
    x0, x1 = int(xs.min()), int(xs.max())
    cx, cy = [], []
    for a in range(x0, x1 + 1, seg):
        m = (xs >= a) & (xs < a + seg)
        if m.sum() >= 12:
            cx.append(a + seg / 2)
            cy.append(float(np.median(ys[m])))
    cx, cy = np.array(cx), np.array(cy)
    if len(cx) >= 5:
        keep = np.ones(len(cx), bool)
        for i in range(len(cx)):
            nb = cy[max(i - 3, 0):i + 4]
            if abs(cy[i] - np.median(nb)) > 5:
                keep[i] = False
        if keep.sum() >= 3:
            cx, cy = cx[keep], cy[keep]
    if len(cx) == 1:
        return np.full(w, cy[0], np.float32)
    X = np.arange(w, dtype=np.float32)
    y = np.interp(X, cx, cy)
    # extend past the ends along the end slope
    k = min(3, len(cx) - 1)
    sl = (cy[k] - cy[0]) / (cx[k] - cx[0])
    sr = (cy[-1] - cy[-1 - k]) / (cx[-1] - cx[-1 - k])
    y = np.where(X < cx[0], cy[0] + (X - cx[0]) * sl, y)
    y = np.where(X > cx[-1], cy[-1] + (X - cx[-1]) * sr, y)
    y = cv2.GaussianBlur(y.astype(np.float32)[None, :], (0, 0), seg / 3)[0]
    return y


def find_rules(d, min_frac=0.25):
    """Ruled lines in a darkness map, traced as curves (pages bend and tilt).

    Returns (rule_mask, rules); each rule has a per-column centre line `yc`,
    its median `y` and stroke `thick`.
    """
    h, w = d.shape
    lo = (d > 0.07).astype(np.uint8)
    klen = max(25, w // 45)
    ker = cv2.getStructuringElement(cv2.MORPH_RECT, (klen, 1))
    horiz = cv2.morphologyEx(lo, cv2.MORPH_OPEN, ker)
    # 1-px steps of a tilted line: open a vertically thickened copy too
    horiz |= cv2.morphologyEx(cv2.dilate(lo, np.ones((3, 1), np.uint8)), cv2.MORPH_OPEN, ker) & lo
    bridge = cv2.dilate(horiz, cv2.getStructuringElement(cv2.MORPH_RECT, (81, 3)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(bridge, 8)
    rules = []
    for i in range(1, n):
        x, y, ww, hh, _a = st[i]
        if ww < min_frac * w or hh > h / 10:
            continue
        sub = horiz[y:y + hh, x:x + ww] & (lab[y:y + hh, x:x + ww] == i)
        ys_, xs_ = np.nonzero(sub)
        if len(xs_) < 0.15 * w:
            continue
        # per-column centre
        cols = np.bincount(xs_, minlength=ww)
        sums = np.bincount(xs_, weights=ys_, minlength=ww)
        have = cols > 0
        _ = sums
        yc = _fit_curve(xs_.astype(np.float64) + x, ys_.astype(np.float64) + y, w)
        thick = float(np.median(cols[have]))
        rules.append({"y": float(np.median(yc)), "yc": yc, "thick": max(min(thick, 8.0), 1.0)})
    rules.sort(key=lambda r: r["y"])
    out = []
    for r in rules:   # pieces of the same line found twice
        if out and np.median(np.abs(r["yc"] - out[-1]["yc"])) < 6:
            continue
        out.append(r)
    return horiz, out


def rule_spacing(rules):
    if len(rules) < 3:
        return None
    gaps = np.diff([r["y"] for r in rules])
    med = np.median(gaps)
    good = gaps[(gaps > 0.6 * med) & (gaps < 1.4 * med)]
    return float(np.median(good)) if len(good) else float(med)


def remove_rules(d, rules, horiz):
    """Erase ruled lines from the darkness map.

    Ink and the printed line combine multiplicatively (1-d = (1-line)(1-ink)),
    so dividing out a per-block model of the line's darkness profile recovers
    the ink even where a letter sits on or crosses the line.
    """
    out = d.copy()
    h, w = d.shape
    block = 40
    for r in rules:
        half = r["thick"] / 2 + 1.5
        R = int(np.ceil(half)) + 1
        offs = np.arange(-R, R + 1)
        yc = np.round(r["yc"]).astype(int)
        for x0 in range(0, w, block):
            x1 = min(x0 + block, w)
            cols = np.arange(x0, x1)
            ys = np.clip(yc[cols][None, :] + offs[:, None], 0, h - 1)
            vals = d[ys, cols[None, :]]                    # (rows, cols)
            prof = np.percentile(vals, 45, axis=1)          # line darkness per row offset
            prof = np.clip(prof, 0, 0.95)
            ink = 1 - (1 - vals) / (1 - prof[:, None])
            ink = np.clip(ink * 1.08 - 0.03, 0, 1)
            out[ys, cols[None, :]] = ink
    for r in rules:
        half = r["thick"] / 2 + 1.5
        # leftovers that live entirely inside the line band are bits of the line
        y0 = int(max(np.floor(r["yc"].min() - half) - 1, 0))
        y1 = int(min(np.ceil(r["yc"].max() + half) + 2, h))
        sub = (out[y0:y1] > 0.12).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(sub, 8)
        drop = []
        for i in range(1, n):
            x, y, ww, hh, _a = st[i]
            cx = min(x + ww // 2, w - 1)
            yt = y0 + y
            yb = y0 + y + hh - 1
            inside = yt >= np.floor(r["yc"][cx] - half) - 1 and yb <= np.ceil(r["yc"][cx] + half) + 1
            if not inside:
                continue
            ext = out[max(yt - 2, 0):yt, x:x + ww].max(initial=0) > 0.2 or out[yb + 1:yb + 3, x:x + ww].max(initial=0) > 0.2
            if not ext:
                drop.append(i)
        if drop:
            region = out[y0:y1]
            region[np.isin(lab, drop)] = 0
    return out


def colour_split(bgr, d, ink, rules):
    """When the pen is bluer than the printed lines (common: blue/violet pen on
    grey or black rules), use colour to remove what's left of the lines under
    words, then bridge strokes that crossed a line. No-op when they match."""
    h, w = d.shape
    b, g, r = [bgr[..., i].astype(np.float32) for i in range(3)]
    chroma = cv2.blur(b - (r + g) / 2, (3, 3))
    band = np.zeros((h, w), np.uint8)
    cols = np.arange(w)
    for ru in rules:
        yc = np.round(ru["yc"]).astype(int)
        half = int(np.ceil(ru["thick"] / 2)) + 1
        for off in range(-half, half + 1):
            band[np.clip(yc + off, 0, h - 1), cols] = 1
    bandb = band.astype(bool)
    near = cv2.dilate(band, np.ones((11, 1), np.uint8)).astype(bool)
    strong_ink = (d > 0.35) & ~near
    on_line = bandb & (d > 0.12)
    if strong_ink.sum() < 500 or on_line.sum() < 500:
        return ink
    ink_c = float(np.median(chroma[strong_ink]))
    line_c = float(np.median(chroma[on_line]))
    if ink_c - line_c < 8:
        return ink
    thr = (ink_c + line_c) / 2
    out = ink.copy()
    # only weak leftovers of the line go; ink that clearly survived the
    # line-division step (letter bottoms sitting on the line) stays
    # line pixels form long horizontal runs of the line's colour; the bottoms
    # of letters resting on the line only make short ones
    sp = np.median(np.diff([ru["y"] for ru in rules])) if len(rules) > 2 else 60
    runs = (bandb & (chroma < thr)).astype(np.uint8)
    klen = max(9, int(0.28 * sp))
    kill = cv2.morphologyEx(runs, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (klen, 1))).astype(bool)
    out[kill] = 0
    # bridge: a stroke continuing straight through the band
    for ru in rules:
        yc = np.round(ru["yc"]).astype(int)
        half = int(np.ceil(ru["thick"] / 2)) + 1
        top = np.clip(yc - half - 1, 0, h - 1)
        bot = np.clip(yc + half + 1, 0, h - 1)
        a_ = out[top, cols]
        b_ = out[bot, cols]
        cross = (a_ > 0.3) & (b_ > 0.3)
        for x in np.nonzero(cross)[0]:
            seg = slice(top[x], bot[x] + 1)
            out[seg, x] = np.maximum(out[seg, x], min(a_[x], b_[x]))
    return out


def clean_page(bgr, deskew=True):
    """Returns dict with deskewed bgr, ink map, rules, spacing, angle."""
    red = reddish(bgr)
    d = darkness(bgr)
    d[red] = 0
    angle = 0.0
    if deskew:
        angle = estimate_skew(d > 0.1)
        if abs(angle) > 0.05:
            bgr = rotate(bgr, angle, (255, 255, 255))
            d = rotate(d, angle, 0)
    horiz, rules = find_rules(d)
    ink = remove_rules(d, rules, horiz) if rules else d.copy()
    if rules:
        ink = colour_split(bgr, d, ink, rules)
    # vertical margin lines: long thin vertical runs
    lo = (ink > 0.07).astype(np.uint8)
    vk = max(40, ink.shape[0] // 10)
    lo_w = cv2.dilate(lo, np.ones((1, 3), np.uint8))
    vert = cv2.morphologyEx(lo_w, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk)))
    vert = cv2.dilate(vert, np.ones((1, 3), np.uint8))
    n, lab, st, _ = cv2.connectedComponentsWithStats(vert, 8)
    for i in range(1, n):
        if st[i, 3] > vk and st[i, 2] < max(12, ink.shape[1] // 150):  # long and thin
            ink[lab == i] = 0
    # margin lines: columns that have ink down most of the page
    colfrac = cv2.dilate((ink > 0.08).astype(np.uint8), np.ones((1, 3), np.uint8)).mean(axis=0)
    for x in np.where(colfrac > 0.33)[0]:
        ink[:, max(x - 2, 0):x + 3] = 0
    return {"bgr": bgr, "ink": ink, "rules": rules, "spacing": rule_spacing(rules), "angle": angle}
