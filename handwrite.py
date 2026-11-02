#!/usr/bin/env python3
"""One command: typed document in, handwritten PDF out.

    python handwrite.py notes.docx                     # -> notes_handwritten.pdf
    python handwrite.py notes.pdf out.pdf --notebook   # exercise-book look
    python handwrite.py essay.md out.pdf --hand my_hand/ --pages 1-2

Any option render.py understands can be added (--ink black, --messiness 1.3,
--fit-pages 40, ...). Runs fully offline; no AI or internet needed.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NOTEBOOK = ["--paper", "notebook", "--line", "7.7", "--size", "2.45", "--indent", "0",
            "--para-gap", "1", "--margins", "34,12,10,30", "--ink", "#2c55a8"]


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("render.py options:\n")
        subprocess.run([sys.executable, os.path.join(HERE, "scripts", "render.py"), "-h"])
        return 0
    src = argv[0]
    rest = argv[1:]
    if not os.path.isfile(src):
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2
    if not src.lower().endswith((".md", ".markdown", ".txt", ".docx", ".pdf")):
        print("error: unsupported input type; use .docx, .pdf, .md or .txt", file=sys.stderr)
        return 2
    out = None
    if rest and not rest[0].startswith("-"):
        out, rest = rest[0], rest[1:]
    out = out or os.path.splitext(src)[0] + "_handwritten.pdf"
    hand = "default"
    if "--hand" in rest:
        i = rest.index("--hand")
        hand = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    if "--notebook" in rest:
        rest.remove("--notebook")
        rest = NOTEBOOK + rest            # explicit options after it still win

    if src.lower().endswith((".md", ".markdown", ".txt")):
        md = src
    else:
        md = os.path.join(tempfile.mkdtemp(), "doc.md")
        r = subprocess.run([sys.executable, os.path.join(HERE, "scripts", "doc_to_md.py"), src, md])
        if r.returncode:
            return r.returncode
    cmd = [sys.executable, os.path.join(HERE, "scripts", "render.py"), hand, md, out] + rest
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
