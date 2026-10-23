# Changelog

## 0.3.0 — 2026-10-23
- **Fidelity audit** (new): reference-free verification of every extraction.
  - Accounts for every source word and image: it is either kept and found in
    the output exactly once, in layout order, or dropped with a stated reason.
  - Checks exam papers against their own checksums: declared question count,
    declared section marks, question-ID and option-ID sequences, answer
    cardinality.
  - Produces a fidelity score and verdict, findings with page numbers, and an
    annotated source PDF.
  - Shown in the library, in a new "Fidelity audit" tab and in the CLI
    (`paperlift audit`). Can be turned off in Settings or with `--no-audit`.
  - Method, benchmark (≈2,700 injected faults, 0 false alarms) and limits are
    in `docs/RESEARCH.md`; reproduce with `bench/fault_injection.py`.
- Fixes found by the audit:
  - declared section totals were never captured by the exam-paper parser;
  - hyphenated line breaks were not rejoined when the hyphen sat in an earlier
    text fragment;
  - the document extractor silently dropped repeated headings.
- Faster line building (about 20 % quicker exam-paper extraction).

## 0.2.0 — 2026-10-20
- Renamed QPaperX → **Paperlift**.
- New **Document** profile for any PDF: headings, paragraphs, bullet/numbered lists,
  tables, code blocks, raster and vector figures, inline images, two-column reading
  order, running header/footer and page-number removal.
- Optional **OCR** add-on (`./paperlift.sh ocr`) for scanned pages, column aware.
- **Auto-detect** chooses Exam paper vs Document per file; can be forced in the UI/CLI.
- **Limits**: files per batch, MB per file, pages per file (default 10 / 50 / 50).
- Per-page progress, cancel queued files, remove files from the library.
- Installable with `pip install .` / `uvx` (`paperlift` command).
- New UI: SVG icon set, hover/focus states, favicon, reduced-motion support.
- Test suite with synthetic PDFs and GitHub Actions CI (Linux + macOS).

## 0.1.0
- First version (QPaperX): exam-paper extraction to Markdown, DOCX, PDF, JSON with an answer key.
