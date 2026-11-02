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


SYMBOLS = """# Maths and science

Water is H_2O and CO₂. Also x^2 + y_{n+1} = 10^-9, E = mc², 25% of ½ or ¼ or ¾ or ⅓.

Compare: 1 ≤ x ≥ 0, x ≠ 1, 5 × 3 ÷ 2 ± 1, 90°, α β γ δ θ λ μ σ φ ω Δ Σ Ω.

Marks ✓ ✗ ① ② ③ ★, price ₹5 or $5, café résumé naïve señor Über crème, { } [ ] ~ \\ ^ and a — dash.
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

        # every symbol the bundled hand claims to support must render (no gaps)
        sm, sp = os.path.join(d, "sym.md"), os.path.join(d, "sym.pdf")
        with open(sm, "w", encoding="utf-8") as f:
            f.write(SYMBOLS)
        r = run(sm, sp)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "left as gaps" not in r.stdout + r.stderr, "symbols missing from the bundled handwriting:\n" + r.stdout + r.stderr
        assert open(sp, "rb").read(4) == b"%PDF"

        # x^2, H_2O, a_{n+1} and Unicode super/subscripts are understood; prose underscores are not
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        from render import script_runs
        assert script_runs("x^2") == [("x", None), ("2", "sup")]
        assert script_runs("H_2O") == [("H", None), ("2", "sub"), ("O", None)]
        assert script_runs("a_{n+1}") == [("a", None)] + [(c, "sub") for c in "n+1"]
        assert script_runs("m\u00b2") == [("m", None), ("2", "sup")]
        assert [m for _, m in script_runs("snake_case")] == [None] * 10

        r = run(os.path.join(d, "missing.docx"))
        assert r.returncode != 0 and "not found" in r.stderr, "missing file should fail cleanly"
        assert "Traceback" not in r.stderr
    print("ok")


if __name__ == "__main__":
    main()
