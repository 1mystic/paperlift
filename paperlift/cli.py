"""Paperlift command line.

    paperlift                      start the web app
    paperlift serve [--port N]
    paperlift convert FILES|DIR [-o OUT] [--mode auto|qpaper|general] ...
    paperlift detect FILES         show which profile would be used
    paperlift audit FILES [--overlay DIR]   verify an extraction against the source PDF
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
    ap.add_argument("--no-audit", action="store_true", help="skip the fidelity audit")
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
                page_markers=a.page_markers, ocr=a.ocr, audit=not a.no_audit)
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
            au = s.get("audit") or {}
            fid = f", fidelity {au['score']:.1f} ({au['verdict']})" if au.get("verdict") and au["verdict"] != "unverifiable" else \
                (", not verifiable (scanned)" if au.get("verdict") == "unverifiable" else "")
            print(f"{what}{fid} in {time.time() - t:.1f}s -> {s['out_dir']}")
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


def cmd_audit(argv):
    import json
    import shutil
    import tempfile
    from .pipeline import convert
    ap = argparse.ArgumentParser(prog="paperlift audit",
                                 description="Extract and verify: every source word/image must be kept or explained.")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--mode", default="auto", choices=["auto", "qpaper", "general"])
    ap.add_argument("--overlay", metavar="DIR", help="also save the annotated source PDF into DIR")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    a = ap.parse_args(argv)
    worst = 0
    for f in _collect(a.inputs):
        tmp = tempfile.mkdtemp(prefix="paperlift-audit-")
        try:
            s = convert(f, tmp, {"mode": a.mode, "audit": True, "audit_overlay": bool(a.overlay), "ocr": "off"}, formats=("json",))
            rep = json.load(open(os.path.join(s["out_dir"], "_audit.json"), encoding="utf-8"))
            if a.json:
                print(json.dumps(rep, ensure_ascii=False, indent=1))
                continue
            m = rep["metrics"]
            print(f"\n{os.path.basename(f)}  [{rep['profile']}]  fidelity {rep['score']:.2f}  -> {rep['verdict'].upper()}")
            print(f"  text kept {m['token_recall']:.2%} of {m['kept_tokens']} words · images {m['image_recall']:.2%} of {m['kept_images']}"
                  f" · order {m['order']:.2%} of {m['order_pairs']} pairs · no-extra {m['precision']:.2%}")
            for c in rep["checks"]:
                print(f"  {'ok ' if c['ok'] else 'FAIL'} {c['detail']}")
            for i in rep["issues"][:30]:
                pg = f"p{i['page']}" if i.get("page") else "  "
                print(f"  [{i['severity']:6}] {pg:4} {i['detail']}" + (f"\n                 {i['excerpt'][:110]}" if i.get("excerpt") else ""))
            for n in rep.get("notes", []):
                print("  note: " + n)
            if a.overlay:
                os.makedirs(a.overlay, exist_ok=True)
                for fn in os.listdir(s["out_dir"]):
                    if fn.endswith("_audit.pdf"):
                        shutil.copy(os.path.join(s["out_dir"], fn), a.overlay)
                        print(f"  overlay: {os.path.join(a.overlay, fn)}")
            worst = max(worst, {"verified": 0, "unverifiable": 0, "minor": 1, "review": 2}[rep["verdict"]])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return worst


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
    if argv[0] == "audit":
        return cmd_audit(argv[1:])
    return cmd_convert(argv)    # `paperlift file.pdf` shorthand


if __name__ == "__main__":
    sys.exit(main())
