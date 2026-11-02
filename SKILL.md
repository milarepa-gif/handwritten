---
name: handwritten-docs
description: Turn a typed document (docx, pdf, md, txt) into scanned-looking handwritten pages. Ships with a default handwriting, can learn a person's own hand from photos of their notes or from a printable template, and renders long documents to PDF in seconds. Use when someone wants a document, notes or pages to look handwritten.
---

# Handwritten docs

Renders text from real samples of handwriting, placed with small random
variation so no two letters or words look identical. Words that exist as whole
samples are placed as written, with their real joins; everything else is built
letter by letter. No image generation is involved.

`SKILL_DIR` below means the folder this file is in.

## Setup

```bash
pip install -r SKILL_DIR/requirements.txt   # add --break-system-packages if pip refuses
```
`pdftoppm` / `pdftotext` (poppler) are used when present for PDF input.

## Pick the handwriting

| Situation | Path | Person's effort |
|---|---|---|
| Just wants it to look handwritten | **A. Default hand** (`default`) | None |
| Wants their own writing, has old notes | **B. From their notes** (`harvest.py`) | Upload 3 to 8 photos |
| Wants their own writing, neat and complete | **C. Template** (`extract_glyphs.py`) | Fill a 4-page sheet, ~30 min |
| Wants a specific font look | **D. Font** (`font_to_glyphs.py`) | None |

Default to A and offer B or C in one line. Don't ask first when the request is clear.

## Step 1: Get the text

```bash
python SKILL_DIR/scripts/doc_to_md.py input.docx doc.md     # also .pdf .txt .md
```
For a Google Doc, read it through the connector and write the Markdown yourself.

Read `doc.md` and clean it before rendering. This decides how good it looks:
- Remove running headers, footers, page numbers and PDF leftovers
- Images and charts can't be handwritten: remove them and tell the person which
- Tables wider than 3 or 4 short columns: rewrite as lists or sentences
- Maths: powers and formulas can be written `x^2`, `x^{n+1}`, `10^-9`, `H_2O`, `a_{ij}` (or with
  Unicode `x²`, `H₂O`); they are drawn raised or lowered in the writer's own style. Symbols such as
  `± × ÷ ≠ ≈ ≤ ≥ ° √ ∞ ∴ ⇒ ← ↑ → ↓ ½ ¼ ¾ ✓ ✗ ① ② ③ ★ ₹ € £ © ® % { } ~ \ ^ —`, Greek letters (`α β γ δ θ λ μ π σ φ ω Δ Σ Ω`) and
  accented letters (`é è ñ ö ü ...`) are drawn directly. A symbol the handwriting set lacks is
  reported as "left as gaps"; write it another way or add it (section E). Bold, italics and
  colour are dropped
- Supported: `#`/`##`/`###` headings, `-` and `1.` lists, `|` tables, blank line between
  paragraphs, `---` for a new page

## Step 2: Build the handwriting (skip for path A)

### B. From photos of existing notes

Works best with clear photos (straight on, good light) of pages written in pen.
Ruled lines, margins, page curl and slight tilt are handled. Ask for an
alphabet sheet too if they can: a to z, A to Z and 0 to 9 written in their
normal note-taking style, plus a few sentences. Letters written in a neater
style than the notes will make letter-built words look different from whole words.

1. Segment. Cleans each photo and draws numbered contact sheets:
   ```bash
   python SKILL_DIR/scripts/harvest.py segment work/ alphabet.jpg notes1.jpg notes2.jpg ...
   ```
   Images become pages p0, p1, ... in the order given. To redo or add pages
   later without disturbing existing ids, use `--start N`. Pick dense, evenly
   lit pages; skip pages where ruled lines stay fused to the words.
2. Label. Look at every `work/sheets/*.png` and write `work/labels.json`.
   Each line strip is titled `pX.LY`. For notes, transcribe whole lines; the
   script finds the word boundaries (`?` = a blob to skip: crossed-out word,
   number, stray mark, margin speck):
   ```json
   {"p3.L4.t": "especially in Northern India. In the next",
    "p3.L5.t": "? ? there was a sharp rise in"}
   ```
   For an alphabet sheet, label the red numbered candidates instead:
   `"p0.g42": "A"`, `"p0.g61+g66": "j"`, `"p0.L10.g": "1 2 3 4 5 6 7 8 9 0"`.
   Never label names, ID or roll numbers, signatures, addresses or anything
   personal; leave those lines out entirely. Save the file after each sheet
   (`--lines-only` on segment gives cleaner sheets for transcription).
3. Build:
   ```bash
   python SKILL_DIR/scripts/harvest.py build work/ hand/ --word-space 1.6 --letter-space 0.12
   ```
   It normalises size and pen weight across pages, rejects outliers, adds
   warped variants so repeats don't look stamped, and draws simple punctuation
   that wasn't in the samples. Open `hand/review_letters.png` and
   `hand/review_words.png`. For each wrong sample (wrong word, cut off, stray
   marks) find its source in `hand/glyphs.json` (`"src": "p9.L17"`) and add it
   to `"_drop": ["far@p9.L17", "@p10.L28"]` in labels.json (`@pN.LX` drops a
   whole line), then rebuild. Expect to drop 5 to 10% on the first pass.

### C. Printable template

Send `SKILL_DIR/assets/handwriting_template.pdf`. Print at 100% (not "Fit to
page"), one character per box sitting on the blue line, same dark pen
throughout, photograph each page straight on with all four black corner
squares in frame. Order and rotation don't matter.
```bash
python SKILL_DIR/scripts/extract_glyphs.py hand/ photo1.jpg photo2.jpg photo3.jpg photo4.jpg
```
Check `hand/contact_sheet.png`; ask for a re-shoot of any page it couldn't read.

### D. From a font

```bash
python SKILL_DIR/scripts/font_to_glyphs.py SomeHandwritingFont.ttf hand/
```

### E. Teach it more symbols (maths, Greek, accents)

Any hand set, including the bundled one, can be extended from a photo of the
person writing the symbols they need once or twice, on ruled paper, with a few
capital letters (H, C, O, T) and, if wanted, `x^2` and `H_2O` written as they
normally would so the raised and lowered sizes match. Rotate the photo upright, then:
```bash
python SKILL_DIR/scripts/harvest.py segment work/ symbols.jpg
python SKILL_DIR/scripts/add_symbols.py work/ hand/        # hand/ is changed in place
```
Between the two, read `work/sheets/*.png` and write `work/labels.json`: `"p0.g12": "±"`,
`"p0.g30+g31": "\u00bd"` (pieces of one symbol), `"p0.g40": "cal:H"` (three or more capitals per
photo, used to measure the writing size), and optional `"_script"` pairs (see the top of
`add_symbols.py`). The tool matches size and pen weight to the set and adds accent marks
that the renderer reuses on other letters. Never label anything personal.

## Step 3: Sample, tune, confirm

```bash
python SKILL_DIR/handwrite.py doc.md sample.pdf --notebook --pages 1-2 --png-dir sample_png
```
(Add `--hand hand/` for paths B to D; drop `--notebook` for plain ruled paper.
`handwrite.py` also accepts .docx/.pdf directly, but tidy the Markdown first.)
Look at the PNGs and adjust:

| If | Change |
|---|---|
| Should look like an exercise book | `--notebook` (handwrite.py preset) |
| Should look even more human | `--corrections 0.01` (occasional crossed-out slip), `--fatigue 0.4` |
| Writing too big or small | `--size` (x-height in mm, default 2.6) |
| Lines too close or far apart | `--line` mm (default 8) |
| Letters cramped or loose | `--letter-space` (default from the glyph set) |
| Words run together or float apart | `--word-space` (default from the glyph set) |
| Too neat or too wobbly | `--messiness` 0.5 to 1.6 (default 1.0) |
| Must land on an exact page count | `--fit-pages N` |
| Paper and ink | `--paper ruled\|notebook\|plain\|grid`, `--ink blue\|black\|royal\|#RRGGBB` |
| Should not look scanned | `--no-scan` |
| Characters reported missing | `--fallback-font SomeFont.ttf` |

Send the sample pages and ask about paper, ink and messiness before the full
run. Mention the page count the full run will have (the script prints it).

## Step 4: Full render and delivery

```bash
python SKILL_DIR/handwrite.py doc.md handwritten.pdf [same options]
```
About 0.5 s and 250 KB per page at 150 dpi; `--dpi 200` for print. Open a few
pages from the middle and end to check, then send the PDF with the page count
and anything left out (images, missing characters).

## Limits to state up front

- Letters are placed side by side; only whole-word samples carry real joins
- Images, charts and complex tables are not drawn
- Handwriting fits about 150 to 250 words a page, so page counts grow
- Symbols the handwriting set doesn't contain (the bundled one has no non-Latin scripts and none of `∑ ∫ ∂ ∈ ∪ ∩ ⊂ ≡`) show as gaps or spelled-out names

## Files

- `handwrite.py` one command: document to handwritten PDF
- `AGENTS.md` the same workflow for non-Claude agents
- `assets/default_hand/` bundled handwriting (letters + ~230 common whole words)
- `assets/handwriting_template.pdf` printable template
- `scripts/render.py` Markdown to handwritten PDF
- `scripts/doc_to_md.py` document to Markdown
- `scripts/harvest.py` photos of notes to glyph set (segment, then build)
- `scripts/add_symbols.py` add maths, Greek and accent symbols to a glyph set from a photo
- `scripts/pageclean.py` photo cleaning used by harvest.py
- `scripts/extract_glyphs.py` filled template to glyph set
- `scripts/font_to_glyphs.py` font to glyph set
- `scripts/make_template.py`, `scripts/layout.py` template generation
- `tests/simulate_filled_template.py` fake template photos for testing
