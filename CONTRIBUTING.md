# Contributing to Paperlift

Thanks for helping! Bug reports with a sample PDF are the most valuable contribution.

## Set up

```bash
git clone https://github.com/1mystic/paperlift && cd paperlift
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
| `paperlift/audit.py` | fidelity audit: unit fates, one-to-one k-gram witnessing, order pairs, checksums, overlay |
| `paperlift/limits.py` | file/page/size limits shared by UI and CLI |
| `paperlift/server.py` | local web server, one-at-a-time job queue |
| `paperlift/static/` | the web UI (single HTML file, no build step) |
| `tests/fixtures.py` | generates the synthetic PDFs used by the tests |
| `bench/fault_injection.py` | injects faults into extractions and measures what the audit catches |

## Adding a profile

A profile is a function `convert_x(pdf, out_root, opts, formats, progress) -> summary`.
Register it in `pipeline.PROFILES`, add a detection rule in `detect_profile`, and add a
fixture + test. Keep memory flat: process page by page and write images straight to disk.

## Changing an extraction policy

If an extractor starts dropping something on purpose, give the audit a matching
reason in `audit.py` (`qpaper_source_units` or `general_source_units`). Otherwise
the audit reports it as lost, which is the point. Then run
`python bench/fault_injection.py` and check that clean documents still show
0 findings.

## Ground rules

- No copyrighted PDFs in the repo — build fixtures with `tests/fixtures.py`.
- Keep runtime dependencies small (currently PyMuPDF, python-docx, Pillow).
- The UI stays a single dependency-free HTML file that works offline.
