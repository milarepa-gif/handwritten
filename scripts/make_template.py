#!/usr/bin/env python3
"""Draw the printable handwriting template (A4 PDF).

Usage:
    python make_template.py [out.pdf]
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import layout as L  # noqa: E402

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.colors import Color, black

GREY = Color(0.6, 0.6, 0.6)
GUIDE = Color(0.80, 0.85, 0.95)
H = A4[1]


def Y(y_top_mm, h_mm=0.0):
    """Convert a top-left mm y (and height) into reportlab's bottom-left points."""
    return H - (y_top_mm + h_mm) * mm


def rect(c, x, y, w, h, fill=True, stroke=False):
    c.rect(x * mm, Y(y, h), w * mm, h * mm, stroke=int(stroke), fill=int(fill))


def furniture(c, page_no, total):
    c.setFillColor(black)
    for cx, cy in L.MARKER_CENTERS.values():
        rect(c, cx - L.MARKER / 2, cy - L.MARKER / 2, L.MARKER, L.MARKER)
    rect(c, *L.ORIENT_MARK)
    c.setLineWidth(0.6)
    c.setStrokeColor(black)
    for bit, r in zip(L.page_bits(page_no), L.page_id_rects()):
        rect(c, *r, fill=bool(bit), stroke=True)
    c.setFont("Helvetica", 7)
    c.setFillColor(GREY)
    c.drawCentredString(L.PAGE_W / 2 * mm, Y(287), f"page {page_no} of {total}  |  keep all four black squares in the photo  |  dark blue or black pen")


def grid_page(c, page_no, total, chars):
    furniture(c, page_no, total)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(L.MARGIN_X * mm, Y(21), "Handwriting sample")
    c.setFont("Helvetica", 8.5)
    c.drawString(L.MARGIN_X * mm, Y(27),
                 "Write the small grey character once in each box, in your normal writing. Sit on the blue line. Same pen throughout.")
    for i, ch in enumerate(chars):
        x, y, w, h = L.box_rect(i)
        c.setStrokeColor(GREY)
        c.setLineWidth(0.4)
        rect(c, x, y, w, h, fill=False, stroke=True)
        c.setStrokeColor(GUIDE)
        c.setLineWidth(0.5)
        by = y + h * L.BASELINE_FRAC
        c.line((x + 2) * mm, Y(by), (x + w - 2) * mm, Y(by))
        xy = y + h * L.XHEIGHT_FRAC
        c.setDash(1, 2)
        c.line((x + 2) * mm, Y(xy), (x + w - 2) * mm, Y(xy))
        c.setDash()
        c.setFillColor(GREY)
        c.setFont("Helvetica", 6.5 if ch != L.RUPEE else 5)
        c.drawString((x + 1.2) * mm, Y(y + 2.8), "Rupee" if ch == L.RUPEE else ch)
    c.showPage()


def natural_page(c, page_no, total):
    furniture(c, page_no, total)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(L.MARGIN_X * mm, Y(21), "Natural writing sample")
    c.setFont("Helvetica", 8.5)
    c.drawString(L.MARGIN_X * mm, Y(27),
                 "Copy each line onto the ruled lines below it at your normal speed and size. This sets spacing, size and slant.")
    for text, py, rules in L.natural_layout():
        c.setFillColor(GREY)
        c.setFont("Helvetica", 9)
        c.drawString(L.MARGIN_X * mm, Y(py), text)
        c.setStrokeColor(GUIDE)
        c.setLineWidth(0.5)
        for ry in rules:
            c.line(L.MARGIN_X * mm, Y(ry), (L.PAGE_W - L.MARGIN_X) * mm, Y(ry))
    c.showPage()


def main(out):
    pages = L.grid_pages()
    total = L.total_pages()
    c = canvas.Canvas(out, pagesize=A4)
    c.setTitle("Handwriting template")
    for n, chars in enumerate(pages, start=1):
        grid_page(c, n, total, chars)
    natural_page(c, total, total)
    c.save()
    print(f"{out}: {len(L.cell_list())} boxes on {len(pages)} pages + 1 natural-writing page")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "handwriting_template.pdf")
