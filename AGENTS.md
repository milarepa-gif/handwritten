# Instructions for AI agents

This repo turns a typed document into a handwritten-looking PDF. It runs locally
in Python; you only run commands and tidy text. Any agent that can run Python
can follow this (ChatGPT, Gemini, Cursor, Codex, Claude, Aider, ...).

## Setup

```bash
pip install -r requirements.txt   # add --break-system-packages if pip refuses
```
If installs are blocked: numpy, opencv, Pillow, reportlab and pypdf are usually
present already. `python-docx` is only needed for .docx input and `img2pdf`
is optional.

## Do this

1. **Convert the document to Markdown:**
   `python scripts/doc_to_md.py INPUT doc.md` (.docx, .pdf, .txt, .md)
2. **Read doc.md and tidy it.** This matters most for quality:
   - remove page numbers, running headers/footers, PDF leftovers
   - remove images and charts and tell the user which ones
   - rewrite tables wider than 3 or 4 short columns as lists
   - write maths as plain words; bold and italics are dropped anyway
   - supported: `#`/`##`/`###` headings, `-` and `1.` lists, `|` tables,
     blank line between paragraphs, `---` for a new page
3. **Render a sample and show it to the user:**
   `python handwrite.py doc.md sample.pdf --notebook --pages 1 --png-dir sample_png`
   (drop `--notebook` for plain ruled paper). Look at the PNG yourself first.
4. **Adjust if asked:** `--ink black`, `--paper plain`, `--messiness 1.3`,
   `--size 2.8`, `--fit-pages N`, `--corrections 0.01`. Run
   `python scripts/render.py -h` for everything.
5. **Render everything:** `python handwrite.py doc.md handwritten.pdf [same options]`
   and give the user the PDF with the page count and anything left out
   (images, characters reported missing).

## Their own handwriting

Prefer the template (no labelling): send `assets/handwriting_template.pdf`,
then `python scripts/extract_glyphs.py my_hand/ photo1.jpg ...` and render
with `--hand my_hand/`. To learn from photos of existing notes instead, follow
"Option A" in README.md: segment, then read each `work/sheets/*.png` and write
`work/labels.json` with what each line says, then build. Never label names,
ID numbers or signatures.

## Don't

- Don't try to draw handwriting yourself or call an image model; the scripts do it.
- Don't skip the sample page on long documents.
- Don't use this to make work look handwritten where handwriting is required.
