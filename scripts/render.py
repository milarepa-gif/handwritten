#!/usr/bin/env python3
"""Render Markdown as handwritten pages, using glyphs cut by extract_glyphs.py.

Usage:
    python render.py GLYPH_DIR input.md out.pdf [options]

GLYPH_DIR is a glyph set from extract_glyphs.py, harvest.py or
font_to_glyphs.py, or `default` for the handwriting bundled with the skill.
If the glyph set has whole-word samples, those words are drawn as written
(real joins) and everything else is built letter by letter.

Common options:
    --pages 1-3          render only these pages (for a quick sample)
    --paper ruled        ruled | plain | grid
    --ink blue           blue | black | #RRGGBB
    --size 2.6           x-height in mm (height of a, e, o)
    --line 8             line spacing in mm
    --messiness 1.0      0 = tidy, 1 = normal, 2 = rushed
    --fit-pages N        shrink or grow the writing to land on N pages
    --no-scan            skip the scanned-photo look

Markdown understood: # ## ### headings, - * and 1. lists, | tables,
blank line = new paragraph, --- = new page, **bold**/_italic_ markers are
dropped (handwriting has no bold).
"""
import argparse
import json
import math
import os
import random
import re
import sys
import tempfile
import unicodedata

import numpy as np
from PIL import Image, ImageFilter, ImageFont, ImageDraw

PAGE_W, PAGE_H = 210.0, 297.0

INKS = {"blue": (28, 45, 125), "black": (28, 28, 34), "royal": (35, 60, 170), "red": (170, 35, 40)}

CHAR_MAP = {
    "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "_": "-",
    "…": "...", "×": "x", "{": "(", "}": ")", "\\": "/", "~": "-", "^": "'", "`": "'", " ": " ", "•": "-", "·": ".",
    "→": "->", "₹": "Rs", "←": "<-", "≥": ">=", "≤": "<=", "°": "o",
    # used only when the handwriting set has no real sample of the symbol
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅛": "1/8",
    "±": "+-", "÷": "/", "≠": "!=", "≈": "~~", "⇒": "=>", "⇐": "<=", "↔": "<->", "∴": ":.",
    "✓": "/", "✔": "/", "✗": "x", "✘": "x", "★": "*", "☆": "*", "©": "(c)", "®": "(R)", "™": "TM",
    "∞": "inf", "√": "root", "€": "EUR", "£": "GBP", "¢": "c",
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon", "θ": "theta", "λ": "lambda",
    "μ": "mu", "π": "pi", "ρ": "rho", "σ": "sigma", "τ": "tau", "φ": "phi", "ω": "omega",
    "Δ": "Delta", "Σ": "Sigma", "Ω": "Omega", "Π": "Pi", "Φ": "Phi",
    **{c: f"({i})" for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩", start=1)},
}

# Superscripts and subscripts are the normal digit/letter glyphs, made smaller
# and moved up or down. Sizes come from the handwriting set (meta.script, measured
# from the writer's own x^2 and H_2O) or these defaults, in capital heights.
SUP_CHARS = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", "0123456789+-=()ni"))
SUB_CHARS = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓ", "0123456789+-=()aeox"))
SUP_KEY, SUB_KEY = "\x01sup:", "\x01sub:"
SCRIPT_DEFAULTS = {"sup_scale": 0.65, "sup_raise": 1.0, "sub_scale": 0.58, "sub_drop": 0.35}
_SUP_RE = re.compile(r"\^(?:\{([^{}]*)\}|([+\-−]?\d+)|([+\-−]?[A-Za-z]))")
_SUB_RE = re.compile(r"_(?:\{([^{}]*)\}|(\d+)|([A-Za-z])(?![A-Za-z]))")
NO_ACCENT_BASES = set("ijIJ")          # the accent would collide with the dot


def script_runs(word):
    """Split a word into (char, mode) pairs; mode is None, 'sup' or 'sub'.

    x^2  x^{n+1}  10^-9  H_2O  a_{ij}  and the Unicode forms x² H₂O are all understood.
    """
    out, i, n = [], 0, len(word)
    while i < n:
        c = word[i]
        if c in SUP_CHARS:
            out.append((SUP_CHARS[c], "sup"))
        elif c in SUB_CHARS:
            out.append((SUB_CHARS[c], "sub"))
        elif c == "^" and i > 0 and _SUP_RE.match(word, i):
            m = _SUP_RE.match(word, i)
            out += [(ch.replace("−", "-"), "sup") for ch in "".join(g for g in m.groups() if g)]
            i = m.end()
            continue
        elif c == "_" and i > 0 and (word[i - 1].isalnum() or word[i - 1] in ")]}") and _SUB_RE.match(word, i):
            m = _SUB_RE.match(word, i)
            out += [(ch.replace("−", "-"), "sub") for ch in "".join(g for g in m.groups() if g)]
            i = m.end()
            continue
        else:
            out.append((c, None))
        i += 1
    return out


# ------------------------------------------------------------------ glyphs
WORD = "\x00w:"   # key prefix for whole-word images in GlyphSet.g
PUNCT_EDGE = ".,;:!?\"'()[]{}\u201c\u201d\u2018\u2019"
HERE = os.path.dirname(os.path.abspath(__file__))


def resolve_glyph_dir(gdir):
    """'default' (or nothing) means the handwriting bundled with the skill."""
    if gdir in ("default", "", None):
        return os.path.join(HERE, "..", "assets", "default_hand")
    return gdir


class GlyphSet:
    def __init__(self, gdir, use_words=True):
        gdir = resolve_glyph_dir(gdir)
        meta = json.load(open(os.path.join(gdir, "glyphs.json"), encoding="utf-8"))
        self.meta = meta["meta"]
        self.src_px = self.meta["px_per_mm"]
        self.g = {}
        for ch, vs in meta["glyphs"].items():
            for v in vs:
                v["img"] = Image.open(os.path.join(gdir, v["file"])).convert("L")
            self.g[ch] = vs
        self.words = set()
        if use_words:
            for wd, vs in meta.get("words", {}).items():
                for v in vs:
                    v["img"] = Image.open(os.path.join(gdir, v["file"])).convert("L")
                self.g[WORD + wd] = vs
                self.words.add(wd)
        self.last = {}
        self.missing = {}
        self.word_hits = 0
        self.word_total = 0

    def has(self, ch):
        return ch in self.g

    # derived glyphs ---------------------------------------------------
    @staticmethod
    def _thicken(im, amount):
        """Shrunk strokes look thin next to full-size ones (the pen is the same);
        thicken them a little."""
        return Image.blend(im, im.filter(ImageFilter.MaxFilter(3)), amount)

    def _script_params(self):
        p = dict(SCRIPT_DEFAULTS)
        p.update({k: v for k, v in self.meta.get("script", {}).items() if k in SCRIPT_DEFAULTS})
        cap = self.meta.get("script", {}).get("cap_mm") or self.meta.get("cap_height_mm") or 1.5 * self.meta["x_height_mm"]
        hs = [v["h"] for d in "0123456789" for v in self.g.get(d, [])]
        digit_h = sorted(hs)[len(hs) // 2] if hs else cap
        return p, cap, digit_h

    def derive(self, mode, ch):
        """Key of a smaller, raised ('sup') or lowered ('sub') copy of glyph `ch`, or None."""
        key = (SUP_KEY if mode == "sup" else SUB_KEY) + ch
        if key in self.g:
            return key
        if ch not in self.g:
            return None
        p, cap, digit_h = self._script_params()
        # never bigger than 80% / 72% of the normal glyph, so they always read as smaller
        if mode == "sup":
            r, shift = min(p["sup_scale"] * cap / digit_h, 0.8), p["sup_raise"] * cap
        else:
            r, shift = min(p["sub_scale"] * cap / digit_h, 0.72), -p["sub_drop"] * cap
        vs = []
        for v in self.g[ch]:
            src = v["img"]
            nw, nh = max(1, round(src.width * r)), max(1, round(src.height * r))
            d = dict(v)
            d["img"] = self._thicken(src.resize((nw, nh), Image.LANCZOS), 0.55)
            d["w"], d["h"] = v["w"] * r, v["h"] * r
            d["desc"], d["top"] = v["desc"] * r - shift, v["top"] * r + shift
            d["pad_px"] = [v["pad_px"][0] * nw / src.width, v["pad_px"][1] * nh / src.height]
            vs.append(d)
        self.g[key] = vs
        return key

    def _composite(self, parts):
        """Join glyph pieces into one new variant. parts: [(variant, scale, x_mm, up_mm)],
        x_mm = left of the piece's ink from the left of the whole, up_mm = how far its baseline is raised."""
        boxes = []
        for v, r, x, up in parts:
            top = v["top"] * r + up
            boxes.append((x, x + v["w"] * r, top, top - v["h"] * r))
        x0, x1 = min(b[0] for b in boxes), max(b[1] for b in boxes)
        ytop, ybot = max(b[2] for b in boxes), min(b[3] for b in boxes)
        ppm = parts[0][0].get("ppm", self.src_px)
        pad = 3
        W, Hh = int(math.ceil((x1 - x0) * ppm)) + 2 * pad + 1, int(math.ceil((ytop - ybot) * ppm)) + 2 * pad + 1
        acc = np.zeros((Hh, W), np.float32)
        for (v, r, x, up), b in zip(parts, boxes):
            im = v["img"]
            nw, nh = max(1, round(im.width * r)), max(1, round(im.height * r))
            if r != 1:
                im = self._thicken(im.resize((nw, nh), Image.LANCZOS), 0.55)
            arr = np.asarray(im, np.float32) / 255.0
            ox = int(round(pad + (b[0] - x0) * ppm - v["pad_px"][0] * nw / v["img"].width))
            oy = int(round(pad + (ytop - b[2]) * ppm - v["pad_px"][1] * nh / v["img"].height))
            ys0, xs0 = max(oy, 0), max(ox, 0)
            ys1, xs1 = min(oy + nh, Hh), min(ox + nw, W)
            if ys1 > ys0 and xs1 > xs0:
                sub = arr[ys0 - oy:ys1 - oy, xs0 - ox:xs1 - ox]
                acc[ys0:ys1, xs0:xs1] = 1 - (1 - acc[ys0:ys1, xs0:xs1]) * (1 - sub)
        return {"img": Image.fromarray((np.clip(acc, 0, 1) * 255).astype(np.uint8)),
                "w": x1 - x0, "h": ytop - ybot, "desc": -ybot, "top": ytop, "pad_px": [pad, pad], "ppm": ppm}

    def _compose(self, ch):
        """Build `ch` from parts we do have: an accented letter from the plain letter and a
        separately stored accent mark; % from a slash and two small circles. True if built."""
        if ch == "%":
            slash, circle = self.g.get("/"), self.g.get("o")
            if not slash or not circle:
                return False
            vs = []
            for i, s in enumerate(slash):
                c1, c2 = circle[i % len(circle)], circle[(i + 1) % len(circle)]
                r = 0.55
                cw1, cw2 = c1["w"] * r, c2["w"] * r
                sx = cw1 * 0.75
                parts = [(c1, r, 0.0, s["top"] - c1["top"] * r),
                         (s, 1.0, sx, 0.0),
                         (c2, r, sx + s["w"] - cw2 * 0.35, -s["desc"] + (c2["h"] - c2["top"]) * r)]
                vs.append(self._composite(parts))
            self.g["%"] = vs
            return True
        nfd = unicodedata.normalize("NFD", ch)
        if len(nfd) != 2:
            return False
        base, mark = nfd
        if base not in self.g or mark not in self.g:
            return False
        lift = 0.0
        if base.isupper():
            lift = max((self.meta.get("cap_height_mm") or 0) - self.meta["x_height_mm"], 0.0)
        vs = []
        for i, b in enumerate(self.g[base]):
            m = self.g[mark][i % len(self.g[mark])]
            up = lift if m["top"] - m["h"] > 0 else 0.0      # only accents above the letter go higher on capitals
            if base in NO_ACCENT_BASES:          # i and j have a dot: the accent goes above it
                up = max(b["top"] + 0.35 - (m["top"] - m["h"]), up)
            parts = [(b, 1.0, 0.0, 0.0), (m, 1.0, (b["w"] - m["w"]) / 2, up)]
            vs.append(self._composite(parts))
        self.g[ch] = vs
        return True

    def resolve(self, ch):
        """Map a character to something we can draw. Returns a string (may be
        several glyphs, e.g. '...') or None to use the fallback font."""
        if ch in self.g:
            return ch
        if self._compose(ch):
            return ch
        if ch in CHAR_MAP:
            mapped = CHAR_MAP[ch]
            if all(c in self.g or c == " " for c in mapped):
                return mapped
        base = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in base if not unicodedata.combining(c))
        if base and all(c in self.g for c in base):
            return base
        for alt in (ch.lower(), ch.upper()):
            if alt in self.g:
                return alt
        self.missing[ch] = self.missing.get(ch, 0) + 1
        return None

    def pick(self, ch, rng):
        vs = self.g[ch]
        if len(vs) == 1:
            return 0
        prev = self.last.get(ch)
        choices = [i for i in range(len(vs)) if i != prev]
        i = rng.choice(choices)
        self.last[ch] = i
        return i


# ------------------------------------------------------------------ markdown
def clean_inline(t):
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)              # images
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)          # links
    t = re.sub(r"<[^>]+>", "", t)                           # html
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", t)
    return " ".join(t.split())


def parse_md(text):
    blocks, para = [], []

    def flush():
        if para:
            blocks.append(("p", clean_inline(" ".join(para))))
            para.clear()

    table = []

    def flush_table():
        if table:
            blocks.append(("table", list(table)))
            table.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("|"):
            flush()
            cells = [clean_inline(c) for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c):
                continue
            table.append(cells)
            continue
        flush_table()
        if not s:
            flush()
            continue
        if s in ("---", "***", "\f") or s.lower() == "<!-- pagebreak -->":
            flush()
            blocks.append(("pagebreak", ""))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            flush()
            blocks.append((f"h{min(len(m.group(1)), 3)}", clean_inline(m.group(2))))
            continue
        m = re.match(r"^([-*+])\s+(.*)", s)
        if m:
            flush()
            indent = (len(line) - len(line.lstrip())) // 2
            blocks.append(("li", clean_inline(m.group(2)), "-", indent))
            continue
        m = re.match(r"^(\d+|[a-z])[.)]\s+(.*)", s)
        if m:
            flush()
            indent = (len(line) - len(line.lstrip())) // 2
            blocks.append(("li", clean_inline(m.group(2)), m.group(1) + ".", indent))
            continue
        para.append(s)
    flush()
    flush_table()
    return blocks


# ------------------------------------------------------------------ layout
class Layout:
    """Places glyphs in millimetres. No pixels until render time."""

    def __init__(self, gs, a, rng):
        self.gs, self.a, self.rng = gs, a, rng
        self.k = a.size / gs.meta["x_height_mm"]      # glyph-mm -> page-mm
        self.top, self.right, self.bottom, self.left = a.margins
        self.line_h = a.line
        self.lines_per_page = int((PAGE_H - self.top - self.bottom) / self.line_h) + 1
        self.pages = [[]]
        self.line_no = 0
        self.mess = a.messiness
        self.fb_font = ImageFont.truetype(a.fallback_font, 100) if a.fallback_font else None
        self._new_line_params()

    # page/line cursor -------------------------------------------------
    def _new_line_params(self):
        # writing gets a little looser further down a page, like a tired hand
        self.mess = self.a.messiness * (1 + getattr(self.a, "fatigue", 0.25) * self.line_no / max(self.lines_per_page, 1))
        m = self.mess
        self.slope = math.tan(math.radians(self.rng.gauss(0, 0.35 * m)))
        self.drift = 0.0
        self.line_x0 = self.left + self.rng.gauss(0, 0.6 * m)
        # a slow wave along the line: hands don't move in straight lines
        self.wave_amp = abs(self.rng.gauss(0, 0.18 * m))
        self.wave_len = self.rng.uniform(60, 140)
        self.wave_ph = self.rng.uniform(0, 2 * math.pi)

    def baseline(self):
        return self.top + self.line_no * self.line_h

    def newline(self, n=1):
        for _ in range(n):
            self.line_no += 1
            if self.line_no >= self.lines_per_page:
                self.newpage()
                break      # a fresh page starts at its first line, no blank lines on top
        self._new_line_params()

    def newpage(self):
        self.pages.append([])
        self.line_no = 0
        self._new_line_params()

    def at_page_top(self):
        return self.line_no == 0

    # words ------------------------------------------------------------
    def split_word(self, word):
        """-> list of (text, is_whole_word). A word from the word bank becomes
        one piece; leading/trailing punctuation stays as separate letters."""
        if not self.gs.words:
            return [(word, False)]
        lead = len(word) - len(word.lstrip(PUNCT_EDGE))
        trail = len(word) - len(word.rstrip(PUNCT_EDGE))
        core = word[lead:len(word) - trail] if trail else word[lead:]
        if core and any(c.isalpha() for c in core):
            self.gs.word_total += 1
        if core in self.gs.words:
            self.gs.word_hits += 1
            parts = []
            if lead:
                parts.append((word[:lead], False))
            parts.append((core, True))
            if trail:
                parts.append((word[len(word) - trail:], False))
            return parts
        return [(word, False)]

    def build_word(self, word, scale):
        """List of (char, variant, width_mm, s) plus total width."""
        out, total = [], 0.0
        gap_base = self.a.size * self.a.letter_space
        for text, whole in self.split_word(word):
            if whole:
                key = WORD + text
                v = self.gs.pick(key, self.rng)
                s = scale * (1 + self.rng.gauss(0, 0.025 * self.mess))
                w = self.gs.g[key][v]["w"] * self.k * s
                out.append((key, v, w, s))
                total += w
                continue
            total += self._letters(text, scale, out)
        gaps = [gap_base * scale * min(max(1 + self.rng.gauss(0, 0.3 * self.mess), 0.4), 1.7) for _ in range(max(len(out) - 1, 0))]
        return out, gaps, total + sum(gaps)

    def _letters(self, word, scale, out):
        total = 0.0
        for raw, mode in script_runs(word):
            if mode:
                mapped = self.gs.resolve(raw)
                seq = [self.gs.derive(mode, c) for c in mapped] if mapped is not None else [None]
                if any(k is None for k in seq):          # no glyph to shrink: draw it full size
                    seq = [None] if mapped is None else list(mapped)
            else:
                mapped = self.gs.resolve(raw)
                seq = mapped if mapped is not None else [None]
            for ch in seq:
                if ch is None:
                    if self.fb_font is not None:
                        w = self.fb_font.getlength(raw) / 100.0 * self.a.size * 1.9 * scale
                    else:
                        w = self.a.size * 0.9 * scale
                    out.append((raw, -1, w, scale))
                else:
                    v = self.gs.pick(ch, self.rng)
                    s = scale * (1 + self.rng.gauss(0, 0.03 * self.mess))
                    w = self.gs.g[ch][v]["w"] * self.k * s
                    out.append((ch, v, w, s))
                total += w
        return total

    def avg_char_w(self):
        ws = [v["w"] for ch in "etaoinsrhldcu" if ch in self.gs.g for v in self.gs.g[ch]]
        mean = sum(ws) / len(ws) if ws else self.gs.meta["x_height_mm"] * 0.8
        return mean * self.k + self.a.size * self.a.letter_space

    def place_word(self, glyphs, gaps, x):
        page = self.pages[-1]
        y0 = self.baseline()
        self.drift += self.rng.gauss(0, 0.10 * self.mess)
        self.drift = max(min(self.drift, 0.6), -0.6)
        slant = self.a.slant + self.rng.gauss(0, 0.6 * self.mess)       # each word leans a bit differently
        word_ink = min(1.0, max(0.78, self.rng.gauss(0.94, 0.05)))      # pressure changes word to word
        for i, (ch, v, w, s) in enumerate(glyphs):
            wave = self.wave_amp * math.sin(2 * math.pi * (x - self.left) / self.wave_len + self.wave_ph)
            base = y0 + self.slope * (x - self.left) + self.drift + wave + self.rng.gauss(0, 0.07 * self.mess)
            if v < 0:
                page.append(("font", ch, x, base, s))
            else:
                g = self.gs.g[ch][v]
                bottom = base + g["desc"] * self.k * s
                top = bottom - g["h"] * self.k * s
                spread = 0.5 if ch.startswith(WORD) else 1.6
                rot = self.rng.gauss(0, spread * self.mess) - slant
                op = min(1.0, max(0.72, word_ink * self.rng.gauss(1.0, 0.025)))
                page.append(("g", ch, v, x, top, s, rot, op))
            x += w + (gaps[i] if i < len(gaps) else 0)
        return x

    def write_text(self, text, scale=1.0, first_indent=0.0, hang=0.0, underline=False, right_edge=None):
        # spaces inside x^{n + 1} or H_{2 3} must not split the word
        text = re.sub(r"([\^_])\{([^{}]*)\}", lambda m: m.group(1) + "{" + m.group(2).replace(" ", "") + "}", text)
        words = text.split(" ")
        right_edge = right_edge or PAGE_W - self.right
        x = self.line_x0 + first_indent
        line_start_x = x
        started = False
        seg_start = x
        for word in words:
            if not word:
                continue
            if word == "\t":  # table cell gap
                if started:
                    x += self.a.size * 2.2 * scale
                continue
            glyphs, gaps, width = self.build_word(word, scale)
            space = self.a.size * self.a.word_space * scale * min(max(1 + self.rng.gauss(0, 0.15 * self.mess), 0.8), 1.35)
            avail = right_edge - self.line_x0 - hang
            if started and x + space + width > right_edge:
                if underline:
                    self.underline(seg_start, x, scale)
                self.newline()
                x = self.line_x0 + hang
                seg_start = x
                started = False
            if width > avail:  # one enormous word: break it across lines
                while glyphs:
                    take, w_sum = 0, 0.0
                    while take < len(glyphs) and x + w_sum + glyphs[take][2] <= right_edge:
                        w_sum += glyphs[take][2] + (gaps[take] if take < len(gaps) else 0)
                        take += 1
                    take = max(take, 1)
                    x = self.place_word(glyphs[:take], gaps[:take], x)
                    glyphs, gaps = glyphs[take:], gaps[take:]
                    if glyphs:
                        self.newline()
                        x = self.line_x0 + hang
                started = True
                continue
            if started:
                x += space
            if getattr(self.a, "corrections", 0) and len(word) >= 5 and word.isalpha() and self.rng.random() < self.a.corrections:
                wrong = self.misspell(word)
                wg, wgaps, ww = self.build_word(wrong, scale)
                if x + ww + space + width <= right_edge:
                    x0w = x
                    x = self.place_word(wg, wgaps, x)
                    self.strike(x0w, x, scale)
                    x += space
            x = self.place_word(glyphs, gaps, x)
            started = True
        if underline and started:
            self.underline(seg_start, x, scale)
        return line_start_x

    def misspell(self, word):
        """A believable slip: a doubled, dropped or swapped letter near the end."""
        i = self.rng.randrange(2, len(word) - 1)
        kind = self.rng.random()
        if kind < 0.4:
            return word[:i] + word[i + 1] + word[i] + word[i + 2:]
        if kind < 0.7:
            return word[:i] + word[i + 1:]
        return word[:i + 1]          # stopped part-way, crossed it out

    def strike(self, x0, x1, scale):
        y = self.baseline() + self.slope * (x0 - self.left) - 0.45 * self.a.size * scale
        n = max(int((x1 - x0) / 3), 2)
        pts = [(x0 - 0.4 + t * (x1 - x0 + 0.8) / n, y + (t / n) * self.rng.gauss(0, 0.4) + self.rng.gauss(0, 0.15)) for t in range(n + 1)]
        self.pages[-1].append(("line", pts, 0.3 * scale))

    def underline(self, x0, x1, scale):
        y = self.baseline() + self.slope * (x0 - self.left) + 1.1 * scale
        pts = []
        n = max(int((x1 - x0) / 4), 2)
        for i in range(n + 1):
            t = i / n
            pts.append((x0 - 0.5 + t * (x1 - x0 + 1.0), y + self.slope * t * (x1 - x0) + self.rng.gauss(0, 0.12 * self.mess)))
        self.pages[-1].append(("line", pts, 0.28 * scale))

    # blocks -----------------------------------------------------------
    def block(self, b):
        kind = b[0]
        a = self.a
        if kind == "pagebreak":
            if not self.at_page_top():
                self.newpage()
            return
        if kind in ("h1", "h2", "h3"):
            scale = {"h1": a.heading_scale, "h2": 1 + (a.heading_scale - 1) * 0.6, "h3": 1.0}[kind]
            if self.pages[-1]:  # something already written on this page
                self.newline(2 if (a.para_gap or kind != "h3") and self.prev not in ("h1", "h2", "h3") else 1)
            if self.line_no >= self.lines_per_page - 2:
                self.newpage()
            self.write_text(b[1], scale=scale, underline=kind in ("h1", "h2"))
            return
        if kind == "p":
            if self.started_any:
                self.newline(1 + max(a.para_gap, 1 if self.prev == "table" else 0))
            self.write_text(b[1], first_indent=a.indent)
            return
        if kind == "li":
            _, text, marker, level = b
            if self.started_any:
                self.newline()
            ind = 2.0 + level * 6.0
            hang = ind + (4.6 if marker == "-" else 2.6 + 1.6 * len(marker))
            mglyphs, mgaps, mw = self.build_word(marker, 1.0)
            self.place_word(mglyphs, mgaps, self.line_x0 + ind)
            self.write_text(text, first_indent=hang, hang=hang)
            return
        if kind == "table":
            if self.started_any:
                self.newline(2)
            rows = b[1]
            ncol = max(len(r) for r in rows)
            per_char = self.avg_char_w()
            widths = [max(len(r[c]) if c < len(r) else 0 for r in rows) * per_char * 1.3 for c in range(ncol)]
            gap = a.size * 2.0
            usable = PAGE_W - self.right - self.left
            aligned = sum(widths) + gap * (ncol - 1) <= usable
            for r, cells in enumerate(rows):
                if r:
                    self.newline()
                if not aligned:  # too wide for columns: write the row as one line of text
                    self.write_text(" \t ".join(c for c in cells if c), underline=(r == 0))
                    continue
                col_x = 0.0
                for c, text in enumerate(cells):
                    if text:
                        self.write_text(text, first_indent=col_x, right_edge=1e9)
                    col_x += widths[c] + gap
                if r == 0:
                    self.underline(self.line_x0, self.line_x0 + col_x - gap, 1.0)
            return

    def run(self, blocks):
        self.started_any = False
        self.prev = None
        for b in blocks:
            self.block(b)
            self.prev = b[0]
            if b[0] != "pagebreak":
                self.started_any = True
        if not self.pages[-1]:
            self.pages.pop()
        return self.pages


# ------------------------------------------------------------------ paper & ink
def lowfreq_noise(h, w, rng, cells=8):
    small = rng.random((cells, int(cells * w / h) + 1)).astype(np.float32)
    img = Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
    return np.asarray(img, dtype=np.float32) / 255.0


def make_paper(a, layout, P, nrng):
    h, w = int(PAGE_H * P), int(PAGE_W * P)
    base = np.array([246, 247, 250] if a.paper == "notebook" else [250, 248, 241], np.float32)
    paper = np.ones((h, w, 3), np.float32) * base
    paper *= (0.985 + 0.025 * lowfreq_noise(h, w, nrng, 6))[..., None]
    paper += nrng.normal(0, 1.5, (h, w, 1)).astype(np.float32)
    canvas = Image.fromarray(np.clip(paper, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(canvas)
    lw = max(1, round(0.22 * P))
    if a.paper in ("ruled", "notebook"):
        notebook = a.paper == "notebook"
        blue = (150, 172, 205) if notebook else (160, 186, 220)
        y = layout.top
        while y < PAGE_H - 4:
            d.line([(0, y * P + 0.35 * P), (w, y * P + 0.35 * P)], fill=blue, width=lw)
            y += layout.line_h
        y = layout.top - layout.line_h
        stop = layout.top - 1.5 * layout.line_h if notebook else 4
        while y > stop:
            d.line([(0, y * P + 0.35 * P), (w, y * P + 0.35 * P)], fill=blue, width=lw)
            y -= layout.line_h
        mx = (layout.left - 4) * P
        red = (220, 120, 135) if notebook else (222, 130, 130)
        d.line([(mx, 0), (mx, h)], fill=red, width=lw)
        if notebook:  # double header rule and a second margin line, like an exercise book
            hy = (layout.top - 2.2 * layout.line_h) * P
            for off in (0, 0.9 * P):
                d.line([(0, hy + off), (w, hy + off)], fill=red, width=lw)
            d.line([(mx + 0.9 * P, 0), (mx + 0.9 * P, h)], fill=red, width=lw)
    elif a.paper == "grid":
        c = (195, 210, 228)
        step = 5.0
        x = 0.0
        while x < PAGE_W:
            d.line([(x * P, 0), (x * P, h)], fill=c, width=1)
            x += step
        y = 0.0
        while y < PAGE_H:
            d.line([(0, y * P), (w, y * P)], fill=c, width=1)
            y += step
    return np.asarray(canvas, dtype=np.float32)


def parse_ink(s):
    if s in INKS:
        return INKS[s]
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


class Rasteriser:
    def __init__(self, gs, a):
        self.gs, self.a = gs, a
        self.P = a.dpi / 25.4
        self.cache = {}
        self.font = None
        if a.fallback_font:
            self.font = ImageFont.truetype(a.fallback_font, int(a.size * 1.9 * self.P))

    def glyph_img(self, ch, v, page_scale):
        f = page_scale * self.P / self.gs.g[ch][v].get("ppm", self.gs.src_px)
        key = (ch, v, round(f * 200))
        im = self.cache.get(key)
        if im is None:
            src = self.gs.g[ch][v]["img"]
            nw, nh = max(1, round(src.width * f)), max(1, round(src.height * f))
            im = src.resize((nw, nh), Image.LANCZOS)
            self.cache[key] = im
        return im, f

    def page(self, ops, layout, page_seed):
        a, P = self.a, self.P
        nrng = np.random.default_rng(page_seed)
        paper = make_paper(a, layout, P, nrng)
        h, w = paper.shape[:2]
        acc = np.zeros((h, w), np.float32)
        k = layout.k

        def blend(arr, x0, y0):
            x0, y0 = int(round(x0)), int(round(y0))
            gh, gw = arr.shape
            xs0, ys0 = max(x0, 0), max(y0, 0)
            xs1, ys1 = min(x0 + gw, w), min(y0 + gh, h)
            if xs1 <= xs0 or ys1 <= ys0:
                return
            sub = arr[ys0 - y0:ys1 - y0, xs0 - x0:xs1 - x0]
            reg = acc[ys0:ys1, xs0:xs1]
            acc[ys0:ys1, xs0:xs1] = 1 - (1 - reg) * (1 - sub)

        for op in ops:
            if op[0] == "g":
                _, ch, v, x, top, s, rot, opac = op
                g = self.gs.g[ch][v]
                im, f = self.glyph_img(ch, v, k * s)
                px, py = g["pad_px"]
                ox, oy = x * P - px * f, top * P - py * f
                cx, cy = ox + im.width / 2, oy + im.height / 2
                if abs(rot) > 0.2:
                    im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
                arr = np.asarray(im, dtype=np.float32) * (opac / 255.0)
                blend(arr, cx - im.width / 2, cy - im.height / 2)
            elif op[0] == "line":
                _, pts, width = op
                layer = Image.new("L", (w, h), 0)
                ImageDraw.Draw(layer).line([(px_ * P, py_ * P) for px_, py_ in pts], fill=235, width=max(1, round(width * P)), joint="curve")
                bbox = layer.getbbox()
                if bbox:
                    crop = np.asarray(layer.crop(bbox), dtype=np.float32) / 255.0
                    blend(crop, bbox[0], bbox[1])
            elif op[0] == "font" and self.font is not None:
                _, ch, x, base, s = op
                layer = Image.new("L", (int(self.a.size * 6 * P), int(self.a.size * 6 * P)), 0)
                ImageDraw.Draw(layer).text((0, layer.height * 0.7), ch, font=self.font, fill=235, anchor="ls")
                arr = np.asarray(layer, dtype=np.float32) / 255.0
                blend(arr, x * P, base * P - layer.height * 0.7)

        # pen pressure: ink is a little uneven across the page
        acc *= 0.86 + 0.14 * lowfreq_noise(h, w, nrng, 14)
        acc = np.asarray(Image.fromarray((acc * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.35)), np.float32) / 255.0
        ink = np.array(parse_ink(a.ink), np.float32) / 255.0
        ink = np.clip(ink * (1 + nrng.normal(0, 0.03)), 0, 1)
        out = paper * (1 - acc[..., None] * (1 - ink))
        img = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
        if not a.no_scan:
            img = scan_look(img, nrng, P)
        return img


def scan_look(img, nrng, P):
    w, h = img.size
    angle = float(nrng.normal(0, 0.35))
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=(236, 234, 228))
    arr = np.asarray(img, dtype=np.float32)
    # soft shadow from one side, like a page that didn't lie quite flat
    side = nrng.integers(0, 2)
    ramp = np.ones(w, np.float32)
    n = int(18 * P)
    ramp[:n] = np.linspace(0.90, 1.0, n)
    if side:
        ramp = ramp[::-1]
    arr *= ramp[None, :, None]
    arr *= (0.97 + 0.03 * np.linspace(1, 0, h))[:, None, None]
    arr += nrng.normal(0, 1.6, arr.shape[:2])[..., None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.3))
    return img


# ------------------------------------------------------------------ driver
def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    out = set()
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return sorted(p for p in out if 1 <= p <= n)


def do_layout(gs, a, blocks):
    rng = random.Random(a.seed)
    gs.last = {}
    gs.missing = {}
    return Layout(gs, a, rng)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("glyph_dir", help="glyph set folder, or 'default' for the bundled handwriting")
    ap.add_argument("input")
    ap.add_argument("out")
    ap.add_argument("--paper", default="ruled", choices=["ruled", "notebook", "plain", "grid"])
    ap.add_argument("--ink", default="blue")
    ap.add_argument("--size", type=float, default=2.6, help="x-height in mm")
    ap.add_argument("--line", type=float, default=8.0, help="line spacing in mm")
    ap.add_argument("--messiness", type=float, default=1.0)
    ap.add_argument("--fatigue", type=float, default=0.25, help="how much looser the writing gets toward the bottom of a page")
    ap.add_argument("--corrections", type=float, default=0.0, help="chance per long word of a crossed-out slip before it, e.g. 0.01")
    ap.add_argument("--letter-space", type=float, default=None, help="gap between letters, x-heights (default: from the glyph set, else 0.14)")
    ap.add_argument("--word-space", type=float, default=None, help="gap between words, x-heights (default: from the glyph set, else 0.95)")
    ap.add_argument("--slant", type=float, default=0.0, help="extra slant in degrees (+ leans right)")
    ap.add_argument("--margins", default="22,12,14,24", help="top,right,bottom,left in mm")
    ap.add_argument("--indent", type=float, default=6.0, help="first-line paragraph indent, mm")
    ap.add_argument("--para-gap", type=int, default=0, help="blank lines between paragraphs")
    ap.add_argument("--heading-scale", type=float, default=1.35)
    ap.add_argument("--fit-pages", type=int, default=0)
    ap.add_argument("--pages", default="", help="e.g. 1-3 or 1,5,9")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--quality", type=int, default=72, help="JPEG quality of each page (file size vs sharpness)")
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--fallback-font", default="", help="TTF used for characters you did not write")
    ap.add_argument("--png-dir", default="", help="also save page images here")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-words", action="store_true", help="build every word from letters (ignore whole-word samples)")
    a = ap.parse_args()
    a.margins = [float(v) for v in a.margins.split(",")]

    gs = GlyphSet(a.glyph_dir, use_words=not a.no_words)
    style = gs.meta.get("style", {})
    if a.word_space is None:
        a.word_space = style.get("word_space", 0.95)
    if a.letter_space is None:
        a.letter_space = style.get("letter_space", 0.14)
    blocks = parse_md(unicodedata.normalize("NFC", open(a.input, encoding="utf-8").read()))
    if not blocks:
        sys.exit("Nothing to render.")

    def lay(factor=1.0):
        b = argparse.Namespace(**vars(a))
        b.size, b.line = a.size * factor, a.line * factor
        lo = do_layout(gs, b, blocks)
        pages = lo.run(blocks)
        return lo, pages, b

    lo, pages, eff = lay()
    if a.fit_pages:
        lo_f, hi_f = 0.45, 1.8
        best = None
        for _ in range(14):
            mid = (lo_f + hi_f) / 2
            l2, p2, b2 = lay(mid)
            if len(p2) <= a.fit_pages:
                best, lo_f = (l2, p2, b2), mid
            else:
                hi_f = mid
        if best is None:
            sys.exit(f"Cannot fit into {a.fit_pages} pages even at the smallest size.")
        lo, pages, eff = best
        print(f"fit-pages: x-height {eff.size:.2f} mm, line spacing {eff.line:.2f} mm")

    want = parse_pages(a.pages, len(pages))
    ras = Rasteriser(gs, eff)
    tmp = tempfile.mkdtemp()
    jpgs = []
    if a.png_dir:
        os.makedirs(a.png_dir, exist_ok=True)
    for n in want:
        img = ras.page(pages[n - 1], lo, page_seed=a.seed * 10007 + n)
        jp = os.path.join(tmp, f"p{n:04d}.jpg")
        img.save(jp, quality=a.quality, dpi=(a.dpi, a.dpi), optimize=True)
        jpgs.append(jp)
        if a.png_dir:
            img.save(os.path.join(a.png_dir, f"page_{n:03d}.png"))
        if len(want) > 10 and n % 10 == 0:
            print(f"  page {n}/{len(pages)}", flush=True)

    try:
        import img2pdf
        layout_fun = img2pdf.get_layout_fun((img2pdf.mm_to_pt(PAGE_W), img2pdf.mm_to_pt(PAGE_H)))
        with open(a.out, "wb") as f:
            f.write(img2pdf.convert(jpgs, layout_fun=layout_fun))
    except ImportError:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        c = canvas.Canvas(a.out, pagesize=A4)
        for jp in jpgs:
            c.drawImage(jp, 0, 0, *A4)
            c.showPage()
        c.save()

    chars = sum(1 for p in pages for op in p if op[0] == "g")
    print(f"{a.out}: {len(want)} of {len(pages)} pages rendered ({chars} letters in the full document)")
    if gs.words and gs.word_total:
        print(f"Whole-word samples used for {gs.word_hits} of {gs.word_total} words ({100 * gs.word_hits / gs.word_total:.0f}%)")
    if gs.missing:
        miss = ", ".join(f"{repr(c)} x{n}" for c, n in sorted(gs.missing.items(), key=lambda kv: -kv[1]))
        how = "drawn with the fallback font" if a.fallback_font else "left as gaps (pass --fallback-font to fill them)"
        print(f"Characters not in your handwriting set, {how}: {miss}")


if __name__ == "__main__":
    main()
