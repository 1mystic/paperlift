"""Resource limits shared by the web app and the CLI (keeps low-end machines happy)."""
from __future__ import annotations

import os
from typing import Optional

DEFAULT_LIMITS = {"max_files": 10, "max_mb": 50, "max_pages": 50}


def clean_limits(d: Optional[dict]) -> dict:
    out = dict(DEFAULT_LIMITS)
    for k, v in (d or {}).items():
        if k in out:
            try:
                out[k] = max(0, int(v))          # 0 = no limit
            except (TypeError, ValueError):
                pass
    return out


def check_pdf(path: str, limits: dict) -> Optional[str]:
    """None if the file is acceptable, otherwise a short human-readable reason."""
    name = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            if f.read(5) != b"%PDF-":
                return f"{name} is not a PDF"
    except OSError as e:
        return f"{name}: {e.strerror}"
    mb = os.path.getsize(path) / 1_048_576
    if limits.get("max_mb") and mb > limits["max_mb"]:
        return f"{name} is {mb:.0f} MB (limit {limits['max_mb']} MB)"
    import pymupdf as fitz
    try:
        doc = fitz.open(path)
    except Exception:
        return f"{name} could not be opened (damaged PDF?)"
    try:
        if doc.needs_pass:
            return f"{name} is password protected"
        n = doc.page_count
    finally:
        doc.close()
    if limits.get("max_pages") and n > limits["max_pages"]:
        return f"{name} has {n} pages (limit {limits['max_pages']})"
    return None
