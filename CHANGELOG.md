# Changelog

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
