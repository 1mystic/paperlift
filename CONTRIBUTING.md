# Contributing to Paperlift

Thanks for helping! Bug reports with a sample PDF are the most valuable contribution.

## Set up

```bash
git clone https://github.com/YOUR-USERNAME/paperlift && cd paperlift
./paperlift.sh --version          # builds .runtime/ (uv + Python + deps) inside the repo
.runtime/bin/uv pip install --python .runtime/venv/bin/python pytest
.runtime/venv/bin/python -m pytest -q
```

## Layout

| File | Role |
|---|---|
| `paperlift/pipeline.py` | profile detection and the single `convert()` entry point |
| `paperlift/qpaper_engine.py` | exam-paper parser (visual lines → questions, options, answers) |
| `paperlift/qpaper_render.py` | exam-paper exporters (Markdown, HTML, PDF, DOCX, JSON) |
| `paperlift/general.py` | general document extractor + exporters, OCR hook |
| `paperlift/limits.py` | file/page/size limits shared by UI and CLI |
| `paperlift/server.py` | local web server, one-at-a-time job queue |
| `paperlift/static/` | the web UI (single HTML file, no build step) |
| `tests/fixtures.py` | generates the synthetic PDFs used by the tests |

## Adding a profile

A profile is a function `convert_x(pdf, out_root, opts, formats, progress) -> summary`.
Register it in `pipeline.PROFILES`, add a detection rule in `detect_profile`, and add a
fixture + test. Keep memory flat: process page by page and write images straight to disk.

## Ground rules

- No copyrighted PDFs in the repo — build fixtures with `tests/fixtures.py`.
- Keep runtime dependencies small (currently PyMuPDF, python-docx, Pillow).
- The UI stays a single dependency-free HTML file that works offline.
