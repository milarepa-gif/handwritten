"""Shared geometry for the handwriting template.

Every coordinate here is in millimetres with the origin at the TOP-LEFT of an
A4 page (x to the right, y downwards). make_template.py draws from these
numbers and extract_glyphs.py cuts from them, so the two always agree.
"""

PAGE_W, PAGE_H = 210.0, 297.0

# Four solid corner squares used to straighten photos.
MARKER = 7.0
MARKER_INSET = 8.0
MARKER_CENTERS = {
    "tl": (MARKER_INSET + MARKER / 2, MARKER_INSET + MARKER / 2),
    "tr": (PAGE_W - MARKER_INSET - MARKER / 2, MARKER_INSET + MARKER / 2),
    "br": (PAGE_W - MARKER_INSET - MARKER / 2, PAGE_H - MARKER_INSET - MARKER / 2),
    "bl": (MARKER_INSET + MARKER / 2, PAGE_H - MARKER_INSET - MARKER / 2),
}

# Small square next to the top-left marker only, so we can tell which way up a
# photo is. (x, y, w, h)
ORIENT_MARK = (19.0, 9.5, 4.0, 4.0)

# Page number written as 4 binary squares (1 = filled), top right.
PAGE_ID_X0, PAGE_ID_Y, PAGE_ID_SIZE, PAGE_ID_STEP = 168.0, 17.0, 3.2, 5.0
PAGE_ID_BITS = 4

# Character grid
MARGIN_X = 15.0
GRID_TOP = 32.0
COLS, ROWS = 8, 10
BOX_W = (PAGE_W - 2 * MARGIN_X) / COLS  # 22.5 mm
BOX_H = 22.0
BASELINE_FRAC = 0.70   # baseline sits 70% of the way down each box
XHEIGHT_FRAC = 0.42    # dotted x-height guide, measured from the top
LABEL_W, LABEL_H = 7.0, 3.6  # printed label area in the top-left of each box

RUPEE = "₹"

LOWER = list("abcdefghijklmnopqrstuvwxyz")
UPPER = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGITS = list("0123456789")
PUNCT = list(".,;:!?'\"()[]-/&@%+=#*<>$|") + [RUPEE]

VARIANTS = {"lower": 3, "upper": 2, "digit": 2, "punct": 2}


def cell_list():
    cells = []
    for ch in LOWER:
        cells += [ch] * VARIANTS["lower"]
    for ch in UPPER:
        cells += [ch] * VARIANTS["upper"]
    for ch in DIGITS:
        cells += [ch] * VARIANTS["digit"]
    for ch in PUNCT:
        cells += [ch] * VARIANTS["punct"]
    return cells


def grid_pages():
    """List of pages, each a list of characters in box order."""
    cells = cell_list()
    per = COLS * ROWS
    return [cells[i:i + per] for i in range(0, len(cells), per)]


def total_pages():
    return len(grid_pages()) + 1  # +1 for the natural-writing page


def box_rect(index):
    """(x, y, w, h) of box number `index` on its page, top-left origin, mm."""
    r, c = divmod(index, COLS)
    return (MARGIN_X + c * BOX_W, GRID_TOP + r * BOX_H, BOX_W, BOX_H)


def page_id_rects():
    return [(PAGE_ID_X0 + i * PAGE_ID_STEP, PAGE_ID_Y, PAGE_ID_SIZE, PAGE_ID_SIZE)
            for i in range(PAGE_ID_BITS)]


def page_bits(page_no):
    return [(page_no >> (PAGE_ID_BITS - 1 - i)) & 1 for i in range(PAGE_ID_BITS)]


# Natural writing page: ruled lines the user copies sentences onto.
NATURAL_LINES = [
    "The quick brown fox jumps over the lazy dog near 42 Park Road.",
    "Pack my box with five dozen liquor jugs, then call me at 9:30.",
    "Sphinx of black quartz, judge my vow! (Rs 1,250 paid on 12/08)",
    "Amazingly few discotheques provide jukeboxes; what a quiet week.",
    "Write a few lines of your own here, about anything at all.",
]
NATURAL_TOP = 36.0
NATURAL_RULE_GAP = 10.0
NATURAL_RULES_PER_PROMPT = 3


def natural_layout():
    """Yields (prompt_text, prompt_y, [rule_y, ...]) in mm."""
    y = NATURAL_TOP
    out = []
    for ln in NATURAL_LINES:
        prompt_y = y
        rules = []
        y += 6
        for _ in range(NATURAL_RULES_PER_PROMPT):
            y += NATURAL_RULE_GAP
            rules.append(y)
        y += 6
        out.append((ln, prompt_y, rules))
    return out
