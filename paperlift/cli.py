"""Paperlift command line.

    paperlift                      start the web app
    paperlift serve [--port N]
    paperlift convert FILES|DIR [-o OUT] [--mode auto|qpaper|general] ...
    paperlift detect FILES         show which profile would be used
"""
from __future__ import annotations

import argparse
import gc
import glob
import os
import sys
import time

from . import __version__


def _collect(inputs):
    files = []
    for x in inputs:
        x = os.path.expanduser(x)
        if os.path.isdir(x):
            files += sorted(glob.glob(os.path.join(x, "*.pdf")) + glob.glob(os.path.join(x, "*.PDF")))
        else:
            files += sorted(glob.glob(x)) or [x]
    return [f for f in dict.fromkeys(files) if os.path.isfile(f)]


def cmd_convert(argv):
    from .limits import DEFAULT_LIMITS, check_pdf
    from .pipeline import convert
    from .server import OUTPUT
    ap = argparse.ArgumentParser(prog="paperlift convert",
                                 description="Convert PDFs to Markdown/DOCX/PDF/JSON, one file at a time.")
    ap.add_argument("inputs", nargs="+", help="PDF files, folders or globs")
    ap.add_argument("-o", "--out", default=OUTPUT, help="output folder (default: %(default)s)")
    ap.add_argument("--formats", default="md,docx,pdf,json", help="comma list of md,docx,pdf,json")
    ap.add_argument("--mode", default="auto", choices=["auto", "qpaper", "general"],
                    help="auto-detect, force exam-paper profile, or force general document")
    g = ap.add_argument_group("exam papers")
    for k in ("code", "year", "term", "exam", "subject"):
        g.add_argument("--" + k)
    g.add_argument("--sections", default="auto", help="auto | all | indexes like 0,2")
    g.add_argument("--no-split", action="store_true", help="keep all chosen sections in one file")
    g.add_argument("--no-marks", action="store_true")
    g.add_argument("--keep-numbers", action="store_true", help="keep original question numbers")
    d = ap.add_argument_group("general documents")
    d.add_argument("--no-tables", action="store_true", help="skip table detection (faster)")
    d.add_argument("--no-vector-figures", action="store_true", help="do not render vector drawings as figures")
    d.add_argument("--page-markers", action="store_true", help="mark page boundaries in the output")
    d.add_argument("--ocr", default="auto", choices=["auto", "off"])
    ap.add_argument("--title", help="override the document title")
    lim = ap.add_argument_group("limits (0 = unlimited)")
    lim.add_argument("--max-files", type=int, default=DEFAULT_LIMITS["max_files"])
    lim.add_argument("--max-mb", type=int, default=DEFAULT_LIMITS["max_mb"])
    lim.add_argument("--max-pages", type=int, default=DEFAULT_LIMITS["max_pages"])
    lim.add_argument("--no-limits", action="store_true", help="disable all limits")
    a = ap.parse_args(argv)

    files = _collect(a.inputs)
    if not files:
        print("No PDF files found.")
        return 1
    limits = {"max_files": 0, "max_mb": 0, "max_pages": 0} if a.no_limits else \
        {"max_files": a.max_files, "max_mb": a.max_mb, "max_pages": a.max_pages}
    if limits["max_files"] and len(files) > limits["max_files"]:
        print(f"{len(files)} files given; the batch limit is {limits['max_files']}. "
              f"Processing the first {limits['max_files']} (use --max-files or --no-limits).")
        files = files[:limits["max_files"]]

    opts = {k: getattr(a, k) for k in ("code", "year", "term", "exam", "subject", "title") if getattr(a, k)}
    opts.update(mode=a.mode, sections=a.sections, split=not a.no_split, marks=not a.no_marks,
                renumber=not a.keep_numbers, tables=not a.no_tables, vector_figures=not a.no_vector_figures,
                page_markers=a.page_markers, ocr=a.ocr)
    fmts = tuple(f.strip() for f in a.formats.split(",") if f.strip())
    try:
        import pymupdf as fitz
    except Exception:
        fitz = None
    ok = 0
    for i, f in enumerate(files, 1):
        t = time.time()
        print(f"[{i}/{len(files)}] {os.path.basename(f)} … ", end="", flush=True)
        problem = check_pdf(f, limits)
        if problem:
            print("skipped: " + problem)
            continue
        try:
            s = convert(f, a.out, opts, formats=fmts)
            if s["profile"] == "qpaper":
                what = f"exam paper, {sum(o['questions'] for o in s['outputs'])} questions"
            else:
                st = s["outputs"][0].get("stats") or {}
                what = f"document, {sum(st.values())} blocks"
            print(f"{what} in {time.time() - t:.1f}s -> {s['out_dir']}")
            for w in s["warnings"]:
                print("    ! " + w)
            ok += 1
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {e}")
        finally:
            if fitz is not None:
                fitz.TOOLS.store_shrink(100)
            gc.collect()
    print(f"\nDone: {ok}/{len(files)} converted.")
    return 0 if ok == len(files) else 2


def cmd_detect(argv):
    from .pipeline import detect_profile
    for f in _collect(argv):
        d = detect_profile(f)
        print(f"{d['profile']:8} score={d['score']:<3} pages={d['pages']:<4} {f}")
    return 0


def cmd_ocr(argv):
    print("OCR add-on: run  ./paperlift.sh ocr  (installs rapidocr into the private environment).")
    return 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-V", "--version", "version"):
        print("paperlift", __version__)
        return 0
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not argv or argv[0] in ("serve", "ui", "web"):
        from .server import main as serve
        return serve(argv[1:])
    if argv[0] == "convert":
        return cmd_convert(argv[1:])
    if argv[0] == "detect":
        return cmd_detect(argv[1:])
    if argv[0] == "ocr":
        return cmd_ocr(argv[1:])
    return cmd_convert(argv)    # `paperlift file.pdf` shorthand


if __name__ == "__main__":
    sys.exit(main())
