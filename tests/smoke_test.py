#!/usr/bin/env python3
"""End-to-end smoke test: render a small document with the bundled handwriting
and check that a valid multi-page PDF comes out.

    python tests/smoke_test.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = """# Smoke test

The quick brown fox jumps over the lazy dog, and then it rests.

- a bullet
- another bullet

1. first
2. second

| Name | Qty |
|---|---|
| Apple | 3 |

---

# Page two

Second page text.
"""


def run(*args):
    return subprocess.run([sys.executable, os.path.join(ROOT, "handwrite.py"), *args],
                          capture_output=True, text=True)


def main():
    with tempfile.TemporaryDirectory() as d:
        md, pdf = os.path.join(d, "doc.md"), os.path.join(d, "out.pdf")
        with open(md, "w", encoding="utf-8") as f:
            f.write(DOC)
        r = run(md, pdf, "--notebook")
        assert r.returncode == 0, r.stdout + r.stderr
        data = open(pdf, "rb").read()
        assert data.startswith(b"%PDF"), "output is not a PDF"
        assert data.count(b"/Type /Page") + data.count(b"/Type/Page") >= 2, "expected 2+ pages"

        r = run(os.path.join(d, "missing.docx"))
        assert r.returncode != 0 and "not found" in r.stderr, "missing file should fail cleanly"
        assert "Traceback" not in r.stderr
    print("ok")


if __name__ == "__main__":
    main()
