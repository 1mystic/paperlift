"""Profile detection + one entry point for every conversion."""
from __future__ import annotations

import re
from typing import Callable, Dict, Optional

import pymupdf as fitz

QPAPER_HINTS = [
    re.compile(r"Question\s+Number\s*:\s*\d+\s*Question\s+Id\s*:", re.I),
    re.compile(r"Question\s+Label\s*:", re.I),
    re.compile(r"Options\s*:\s*\n?\s*\d{8,}\.", re.I),
    re.compile(r"(Possible Answers|Sub-Section Id|Question Shuffling Allowed)\s*:", re.I),
]

PROFILES: Dict[str, dict] = {
    "qpaper": {"label": "Exam paper", "desc": "IITM-style exported question papers: questions, options, answer key"},
    "general": {"label": "Document", "desc": "Any PDF: headings, paragraphs, lists, tables, figures, OCR for scans"},
}


def detect_profile(pdf_path: str, max_pages: int = 4) -> dict:
    """Score the first pages; exam papers have very distinctive console labels."""
    doc = fitz.open(pdf_path)
    try:
        text = "".join(doc[i].get_text() for i in range(min(max_pages, doc.page_count)))
        pages = doc.page_count
    finally:
        doc.close()
    hits = {i: len(rx.findall(text)) for i, rx in enumerate(QPAPER_HINTS)}
    score = (min(hits[0], 5) * 2 + min(hits[1], 5) + min(hits[2], 5) + min(hits[3], 5))
    profile = "qpaper" if hits[0] >= 1 and score >= 5 else "general"
    return {"profile": profile, "score": score, "pages": pages}


def convert(pdf_path: str, out_root: str, opts: Optional[dict] = None, formats=("md", "docx", "pdf", "json"),
            progress: Optional[Callable[[int, int], None]] = None) -> dict:
    opts = dict(opts or {})
    mode = opts.pop("mode", "auto") or "auto"
    det = detect_profile(pdf_path)
    profile = det["profile"] if mode == "auto" else mode
    if profile == "qpaper":
        from .qpaper_render import convert as qconvert
        summary = qconvert(pdf_path, out_root, opts, formats=formats, progress=progress)
        no_questions = sum(o["questions"] for o in summary["outputs"]) == 0
        if mode == "auto" and no_questions:       # looked like a paper but wasn't: fall back
            profile = "general"
        else:
            summary.update(profile="qpaper", detected=det, mode=mode)
            return summary
    from .general import convert_general
    summary = convert_general(pdf_path, out_root, opts, formats=formats, progress=progress)
    summary.update(profile="general", detected=det, mode=mode)
    return summary
