#!/usr/bin/env python3
"""Turn a .docx, .pdf, .txt or .md file into the simple Markdown that
render.py understands (#/##/### headings, - and 1. lists, | tables, blank
lines between paragraphs, --- for a page break).

Usage:
    python doc_to_md.py input.docx out.md
"""
import os
import re
import shutil
import subprocess
import sys


def docx_to_md(path):
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    out = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if 'w:type="page"' in child.xml:
                out += ["", "---", ""]
            if not text:
                out.append("")
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            numbered = child.pPr is not None and child.pPr.numPr is not None
            if style.startswith("title"):
                out += [f"# {text}", ""]
            elif style.startswith("heading"):
                m = re.search(r"(\d)", style)
                lvl = min(int(m.group(1)) if m else 1, 3)
                out += ["#" * lvl + " " + text, ""]
            elif "list number" in style:
                out.append(f"1. {text}")
            elif "list" in style or numbered:
                out.append(f"- {text}")
            else:
                out += [text, ""]
        elif tag == "tbl":
            t = Table(child, d)
            rows = []
            for r in t.rows:
                cells = []
                for c in r.cells:
                    v = " ".join(c.text.split())
                    if not cells or cells[-1] != v:  # merged cells repeat
                        cells.append(v)
                rows.append(cells)
            if rows:
                out.append("")
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("|" + "---|" * len(rows[0]))
                for r in rows[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
    return "\n".join(out)


def pdf_to_md(path):
    if shutil.which("pdftotext"):
        raw = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], capture_output=True, text=True, check=True).stdout
    else:
        from pypdf import PdfReader
        raw = "\n\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
    raw = raw.replace("\f", "\n\n")
    paras = re.split(r"\n\s*\n", raw)
    out = []
    for p in paras:
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        if not lines:
            continue
        if len(lines) == 1 and re.fullmatch(r"(page\s*)?\d+(\s*of\s*\d+)?", lines[0], re.I):
            continue  # bare page number
        text = ""
        for ln in lines:
            if text.endswith("-") and ln[:1].islower():
                text = text[:-1] + ln
            else:
                text = (text + " " + ln).strip()
        if re.match(r"^[•●▪\-\*]\s+", text):
            text = "- " + re.sub(r"^[•●▪\-\*]\s+", "", text)
        out += [text, ""]
    return "\n".join(out)


def main(src, dst):
    ext = os.path.splitext(src)[1].lower()
    if ext == ".docx":
        md = docx_to_md(src)
    elif ext == ".pdf":
        md = pdf_to_md(src)
    elif ext in (".md", ".txt", ".markdown"):
        md = open(src, encoding="utf-8").read()
    else:
        sys.exit(f"Unsupported file type {ext}. Export to .docx, .pdf or .txt first.")
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(md)
    words = len(md.split())
    print(f"{dst}: {words} words")
    if words == 0:
        sys.exit("No text found. If this is a scanned PDF (pictures of pages), it has no text to read; "
                 "use the original document, or type/OCR the text into a .txt or .md file first.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
