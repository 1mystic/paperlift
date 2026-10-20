"""
General-purpose PDF extraction profile.

Works on any PDF with a text layer (reports, notes, handouts, articles,
"flattened" exports) and, when the optional OCR add-on is installed, on
scanned pages too.  Produces a list of blocks:

  {"type": "heading", "level": 1-4, "runs": [...]}
  {"type": "para",    "runs": [...]}
  {"type": "item",    "runs": [...], "ordered": bool, "marker": "1.", "level": 0-3}
  {"type": "code",    "text": "..."}
  {"type": "table",   "rows": [[cell, ...], ...]}
  {"type": "figure",  "img": "img_001.png", "w": pt, "h": pt}
  {"type": "page",    "n": 3}            # page boundary marker (optional output)

Pipeline per document:
  pass 1  font statistics + repeated header/footer lines (cheap, discarded)
  pass 2  per page: tables -> figures (raster + vector clusters) -> text blocks
          with inline images -> reading order (column aware) -> blocks
Pages are processed one by one; images go straight to disk.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pymupdf as fitz

if hasattr(fitz, "no_recommend_layout"):
    fitz.no_recommend_layout()

from .qpaper_engine import ImageStore, normalize_runs, runs_plain
from .qpaper_render import md_runs, html_runs, _md_escape, PX

BULLET_RE = re.compile(r"^\s*([•◦▪●○■□–—\-\*·‣⁃➢→])\s+")
ORDERED_RE = re.compile(r"^\s*(\(?(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,5})[.)])\s+")
PAGENUM_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$|^\s*[-–]\s*\d{1,4}\s*[-–]\s*$", re.I)
MONO_RE = re.compile(r"mono|courier|consol|menlo|code", re.I)


def _norm_margin(t: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", t.strip().lower()))


@dataclass
class GDoc:
    source: str
    title: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    blocks: List[dict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pages: int = 0
    ocr_pages: List[int] = field(default_factory=list)


# --------------------------------------------------------------------------
# OCR (optional add-on: `./paperlift ocr` installs rapidocr into the private env)
# --------------------------------------------------------------------------
_OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            _OCR = RapidOCR()
        except Exception:
            _OCR = False
    return _OCR or None


def ocr_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except Exception:
        return False


def ocr_page_blocks(page: fitz.Page, dpi: int = 200) -> List[dict]:
    """OCR one page; boxes are grouped column-aware into headings / paragraphs / items."""
    eng = ocr_engine()
    if not eng:
        return []
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    import numpy as np  # installed with rapidocr
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    result, _ = eng(img)
    pix = img = None
    k = 72.0 / dpi
    boxes = []
    for box, text, score in result or []:
        if not text.strip() or float(score) < 0.5:
            continue
        xs = [q[0] * k for q in box]; ys = [q[1] * k for q in box]
        boxes.append({"r": fitz.Rect(min(xs), min(ys), max(xs), max(ys)), "t": text.strip()})
    if not boxes:
        return []
    W, H = page.rect.width, page.rect.height
    heights = sorted(b["r"].height for b in boxes)
    med = heights[len(heights) // 2]
    # drop repeated-looking margin lines (running header / page number)
    boxes = [b for b in boxes if not ((b["r"].y1 < H * 0.07 or b["r"].y0 > H * 0.93) and len(b["t"]) < 60)]
    mid = W / 2
    left = [b for b in boxes if b["r"].x1 <= mid + 6]
    right = [b for b in boxes if b["r"].x0 >= mid - 6]
    two = len(left) > 4 and len(right) > 4
    def col(b):
        if not two:
            return 0
        if b["r"].x0 < mid - 6 and b["r"].x1 > mid + 6:
            return -1                       # spans both columns
        return 0 if b["r"].x1 <= mid + 6 else 1
    # bands separated by full-width boxes, then left column, then right
    ordered, band = [], []
    def flush():
        for c in (0, 1):
            ordered.extend(sorted((x for x in band if col(x) == c), key=lambda x: (x["r"].y0, x["r"].x0)))
    for b in sorted(boxes, key=lambda x: x["r"].y0):
        if col(b) == -1:
            flush(); band = []; ordered.append(b)
        else:
            band.append(b)
    flush()
    # rows -> blocks
    blocks, cur, prev = [], None, None
    for b in ordered:
        same_row = prev is not None and col(b) == col(prev) and abs(b["r"].y0 - prev["r"].y0) < 0.5 * med
        if same_row and cur is not None:
            cur["runs"][-1]["t"] += " " + b["t"]
            prev = b
            continue
        gap = (b["r"].y0 - prev["r"].y1) if prev is not None and col(b) == col(prev) else 99
        text = b["t"]
        bm = BULLET_RE.match(text) or re.match(r"^[·•]\s*", text)
        is_head = b["r"].height > med * 1.35 and len(text) < 90
        if is_head and cur is not None and cur["type"] == "heading" and gap < 0.7 * med:
            cur["runs"][-1]["t"] += " " + text
        elif is_head:
            cur = {"type": "heading", "level": 1 if b["r"].height > med * 1.8 else 2, "runs": [{"t": text}]}
            blocks.append(cur)
        elif bm:
            cur = {"type": "item", "ordered": False, "marker": "", "level": 0, "runs": [{"t": text[bm.end():]}]}
            blocks.append(cur)
        elif cur is not None and cur["type"] in ("para", "item") and gap < 0.7 * med:
            t = cur["runs"][-1]["t"]
            cur["runs"][-1]["t"] = (t[:-1] if t.endswith("-") and text[:1].islower() else t + " ") + text
        else:
            cur = {"type": "para", "runs": [{"t": text}]}
            blocks.append(cur)
        prev = b
    return blocks


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
class GeneralExtractor:
    def __init__(self, doc: fitz.Document, source: str, image_dir: Optional[str], opts: dict):
        self.doc, self.source, self.opts = doc, source, opts
        self.images = ImageStore(doc, image_dir)
        self.g = GDoc(source=source, pages=doc.page_count)
        self.body_size = 11.0
        self.size_level: Dict[float, int] = {}
        self.repeated: set = set()

    # ---- pass 1 ----------------------------------------------------------
    def stats(self):
        sizes: Counter = Counter()
        margin: Counter = Counter()
        n = self.doc.page_count
        for page in self.doc:
            H = page.rect.height
            d = page.get_text("dict", flags=0)
            seen = set()
            for b in d["blocks"]:
                for ln in b.get("lines", []):
                    t = "".join(s["text"] for s in ln["spans"])
                    for s in ln["spans"]:
                        k = len(s["text"].strip())
                        if k:
                            sizes[round(s["size"] * 2) / 2] += k
                    y0, y1 = ln["bbox"][1], ln["bbox"][3]
                    if t.strip() and (y1 < H * 0.09 or y0 > H * 0.91):
                        key = _norm_margin(t)
                        if key not in seen:
                            seen.add(key); margin[key] += 1
            d = None
        if sizes:
            self.body_size = sizes.most_common(1)[0][0]
        total = sum(sizes.values()) or 1
        heads = sorted([s for s, c in sizes.items() if s >= self.body_size * 1.12 and c / total < 0.3], reverse=True)
        for i, s in enumerate(heads[:4]):
            self.size_level[s] = i + 1
        if n >= 3:
            self.repeated = {k for k, c in margin.items() if c >= max(2, 0.4 * n)}
        elif n == 2:
            self.repeated = {k for k, c in margin.items() if c == 2 and len(k) > 3}

    def level_for(self, size: float) -> int:
        s = round(size * 2) / 2
        if s in self.size_level:
            return self.size_level[s]
        bigger = [lvl for sz, lvl in self.size_level.items() if sz <= s]
        return min(bigger) if bigger and s >= self.body_size * 1.12 else 0

    # ---- pass 2 ----------------------------------------------------------
    def run(self, progress=None) -> GDoc:
        self.stats()
        total = self.doc.page_count
        for pno, page in enumerate(self.doc):
            if self.opts.get("page_markers") and pno:
                self.g.blocks.append({"type": "page", "n": pno + 1})
            try:
                self.page(page, pno)
            except Exception as e:  # one bad page never kills the document
                self.g.warnings.append(f"page {pno + 1}: {type(e).__name__}: {e}")
            if progress:
                progress(pno + 1, total)
        self.merge_across_pages()
        self.g.title = self.pick_title()
        md = self.doc.metadata or {}
        self.g.meta = {k: v for k, v in md.items() if v and k in ("title", "author", "subject", "creator", "producer", "creationDate")}
        self.g.meta["pages"] = str(total)
        return self.g

    def page(self, page: fitz.Page, pno: int):
        prect = page.rect
        W, H = prect.width, prect.height
        raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP | fitz.TEXT_DEHYPHENATE * 0)
        text_chars = sum(len(ch["c"].strip()) for b in raw["blocks"] if b.get("type") == 0
                         for ln in b["lines"] for s in ln["spans"] for ch in s["chars"])
        imgs = [i for i in page.get_image_info(xrefs=True) if i.get("xref")]

        # ---- scanned page? -------------------------------------------------
        cover = max(((fitz.Rect(i["bbox"]) & prect).get_area() for i in imgs), default=0) / max(1, prect.get_area())
        if text_chars < 25 and cover > 0.45:
            self.scanned_page(page, pno)
            return

        items: List[dict] = []      # {bbox, kind, data}
        blocked: List[fitz.Rect] = []

        # ---- tables --------------------------------------------------------
        if self.opts.get("tables", True):
            try:
                tabs = page.find_tables()
                for t in tabs.tables:
                    rows = t.extract()
                    if not rows or len(rows) < 2 or max(len(r) for r in rows) < 2:
                        continue
                    rows = [[re.sub(r"\s+", " ", c or "").strip() for c in r] for r in rows]
                    if sum(1 for r in rows for c in r if c) < 3:
                        continue
                    r = fitz.Rect(t.bbox)
                    items.append({"bbox": r, "kind": "table", "rows": rows})
                    blocked.append(r)
            except Exception:
                pass

        # ---- figures: raster images ---------------------------------------
        inline_candidates = []
        for info in imgs:
            r = fitz.Rect(info["bbox"]) & prect
            if r.is_empty or r.width < 6 or r.height < 6:
                continue
            if r.get_area() > 0.85 * prect.get_area() and text_chars > 50:
                continue            # page background / invisible-OCR scan underlay
            if any((r & b).get_area() > 0.6 * r.get_area() for b in blocked):
                continue
            if r.height <= 30 and r.width <= 220:
                inline_candidates.append((r, info["xref"]))
                continue
            items.append({"bbox": r, "kind": "image", "xref": info["xref"]})

        # ---- figures: vector drawings --------------------------------------
        vec_regions = []
        if self.opts.get("vector_figures", True):
            try:
                paths = page.get_drawings()
                if paths and len(paths) < 6000:
                    clusters = page.cluster_drawings(drawings=paths, x_tolerance=4, y_tolerance=4)
                    for c in clusters:
                        c = fitz.Rect(c) & prect
                        if c.width < 50 or c.height < 35 or c.get_area() > 0.92 * prect.get_area():
                            continue
                        if any((c & b).get_area() > 0.5 * c.get_area() for b in blocked):
                            continue
                        inside = [p for p in paths if fitz.Rect(p["rect"]).intersects(c)]
                        kinds = Counter(it[0] for p in inside for it in p["items"])
                        curvy = kinds.get("c", 0) + kinds.get("l", 0)
                        if curvy < 4:      # just boxes / shading behind text -> not a figure
                            continue
                        vec_regions.append(c)
            except Exception:
                pass
        # merge overlapping vector regions with each other and with raster images
        merged: List[fitz.Rect] = []
        for r in vec_regions:
            for m in merged:
                if (m + (-6, -6, 6, 6)).intersects(r):
                    m |= r
                    break
            else:
                merged.append(fitz.Rect(r))
        for r in merged:
            for it in list(items):
                if it["kind"] == "image" and (it["bbox"] & r).get_area() > 0.3 * it["bbox"].get_area():
                    r |= it["bbox"]
                    items.remove(it)
            items.append({"bbox": r, "kind": "vector"})
            blocked.append(r)
        for it in items:
            if it["kind"] == "image":
                blocked.append(it["bbox"])

        # ---- text blocks ---------------------------------------------------
        for b in raw["blocks"]:
            if b.get("type") != 0:
                continue
            br = fitz.Rect(b["bbox"])
            lines = []
            for ln in b["lines"]:
                lr = fitz.Rect(ln["bbox"])
                c = (lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2
                if any(r.contains(fitz.Point(*c)) for r in blocked):
                    continue
                spans = [s for s in ln["spans"] if s["chars"]]
                text = "".join(ch["c"] for s in spans for ch in s["chars"])
                if not text.strip():
                    continue
                if (lr.y1 < H * 0.09 or lr.y0 > H * 0.91):
                    if _norm_margin(text) in self.repeated or PAGENUM_RE.match(text):
                        continue
                if abs(ln.get("dir", (1, 0))[1]) > 0.5:    # vertical/rotated margin text
                    continue
                lines.append({"bbox": lr, "spans": spans, "text": text})
            if lines:
                bb = fitz.Rect(lines[0]["bbox"])
                for l in lines[1:]:
                    bb |= l["bbox"]
                items.append({"bbox": bb, "kind": "text", "lines": lines})

        # inline images -> attach to the text line they sit on, else figure
        for r, xref in inline_candidates:
            best, best_ov = None, 0.0
            for it in items:
                if it["kind"] != "text" or not (it["bbox"] + (-30, -4, 30, 4)).intersects(r):
                    continue
                for l in it["lines"]:
                    ov = min(l["bbox"].y1, r.y1) - max(l["bbox"].y0, r.y0)
                    if ov > best_ov:
                        best, best_ov = l, ov
            if best is not None and best_ov > 0.3 * min(r.height, best["bbox"].height):
                best.setdefault("imgs", []).append((r, xref))
            elif r.width >= 14 and r.height >= 14:
                items.append({"bbox": r, "kind": "image", "xref": xref})

        for it in self.reading_order(items, W):
            self.emit(it, page, pno)

    def scanned_page(self, page, pno):
        mode = self.opts.get("ocr", "auto")
        if mode != "off" and ocr_available():
            blocks = ocr_page_blocks(page)
            if blocks:
                self.g.blocks.extend(blocks)
                self.g.ocr_pages.append(pno + 1)
                return
        name = self.images.add_render(page, page.rect, dpi=110)
        self.g.blocks.append({"type": "figure", "img": name, "w": page.rect.width, "h": page.rect.height, "scan": True})
        self.g.warnings.append(f"page {pno + 1} is a scanned image" + (" (install OCR: ./paperlift.sh ocr)" if not ocr_available() else " (OCR found no text)"))

    # ---- reading order: column aware ------------------------------------
    @staticmethod
    def reading_order(items: List[dict], W: float) -> List[dict]:
        if not items:
            return []
        mid = W / 2
        left = [i for i in items if i["bbox"].x1 <= mid + 8]
        right = [i for i in items if i["bbox"].x0 >= mid - 8]
        two_col = len(left) >= 2 and len(right) >= 2 and \
            sum(i["bbox"].height for i in right if i["kind"] == "text") > 60
        if not two_col:
            return sorted(items, key=lambda i: (round(i["bbox"].y0 / 3), i["bbox"].x0))
        out, band = [], []
        for it in sorted(items, key=lambda i: i["bbox"].y0):
            spans_both = it["bbox"].x0 < mid - 8 and it["bbox"].x1 > mid + 8
            if spans_both:
                out += sorted([b for b in band if b["bbox"].x0 < mid - 8], key=lambda i: i["bbox"].y0)
                out += sorted([b for b in band if b["bbox"].x0 >= mid - 8], key=lambda i: i["bbox"].y0)
                band = []
                out.append(it)
            else:
                band.append(it)
        out += sorted([b for b in band if b["bbox"].x0 < mid - 8], key=lambda i: i["bbox"].y0)
        out += sorted([b for b in band if b["bbox"].x0 >= mid - 8], key=lambda i: i["bbox"].y0)
        return out

    # ---- block emission --------------------------------------------------
    def emit(self, it: dict, page: fitz.Page, pno: int):
        k = it["kind"]
        if k == "table":
            self.g.blocks.append({"type": "table", "rows": it["rows"]})
        elif k == "image":
            name = self.images.name_for(it["xref"])
            m = self.images.meta.get(name, {})
            r = it["bbox"]
            self.g.blocks.append({"type": "figure", "img": name, "w": round(r.width * m.get("fx", 1), 1),
                                  "h": round(r.height * m.get("fy", 1), 1)})
        elif k == "vector":
            r = it["bbox"] + (-3, -3, 3, 3)
            name = self.images.add_render(page, r & page.rect, dpi=170)
            self.g.blocks.append({"type": "figure", "img": name, "w": round(r.width, 1), "h": round(r.height, 1)})
        else:
            self.text_block(it)

    def line_runs(self, line: dict) -> List[dict]:
        main = Counter()
        for s in line["spans"]:
            main[round(s["size"], 1)] += len(s["text"].strip()) if "text" in s else len(s["chars"])
        msize = main.most_common(1)[0][0] if main else self.body_size
        moy = next((s["origin"][1] for s in line["spans"] if abs(s["size"] - msize) < 0.6), None)
        imgs = sorted(line.get("imgs", []), key=lambda t: t[0].x0)
        runs = []
        for s in line["spans"]:
            chars = s["chars"]
            style = {}
            f = s["flags"]
            font = s.get("font", "")
            if f & 16 or re.search(r"bold|black|heavy|semibold", font, re.I):
                style["b"] = True
            if f & 2 or re.search(r"italic|oblique", font, re.I):
                style["i"] = True
            if f & 8 or MONO_RE.search(font):
                style["code"] = True
            if s["size"] < msize * 0.86 and moy is not None and "".join(c["c"] for c in chars).strip():
                style["s"] = "sub" if s["origin"][1] > moy + 0.5 else "sup"
            # split span around inline images
            buf = ""
            for ch in chars:
                while imgs and ch["bbox"][0] >= imgs[0][0].x0 - 0.5:
                    if buf:
                        runs.append(dict(style, t=buf)); buf = ""
                    r, xref = imgs.pop(0)
                    name = self.images.name_for(xref)
                    m = self.images.meta.get(name, {})
                    runs.append({"img": name, "w": round(r.width * m.get("fx", 1), 1), "h": round(r.height * m.get("fy", 1), 1)})
                buf += ch["c"]
            if buf:
                runs.append(dict(style, t=buf))
        for r, xref in imgs:
            name = self.images.name_for(xref)
            m = self.images.meta.get(name, {})
            runs.append({"img": name, "w": round(r.width * m.get("fx", 1), 1), "h": round(r.height * m.get("fy", 1), 1)})
        return runs

    def text_block(self, it: dict):
        lines = it["lines"]
        x_left = min(l["bbox"].x0 for l in lines)
        # monospace block -> code
        mono = all(all(MONO_RE.search(s.get("font", "")) or not "".join(c["c"] for c in s["chars"]).strip()
                       for s in l["spans"]) for l in lines)
        if mono and len(lines) >= 2:
            self.g.blocks.append({"type": "code", "text": "\n".join(l["text"].rstrip() for l in lines)})
            return

        cur: Optional[dict] = None
        prev = None
        for l in lines:
            text = l["text"]
            sizes = [s["size"] for s in l["spans"] if "".join(c["c"] for c in s["chars"]).strip()]
            size = max(sizes) if sizes else self.body_size
            lvl = self.level_for(size)
            runs = self.line_runs(l)
            bold_line = all(r.get("b") for r in runs if "t" in r and r["t"].strip())
            bm = BULLET_RE.match(text)
            om = ORDERED_RE.match(text) if not bm else None
            gap = (l["bbox"].y0 - prev["bbox"].y1) if prev else 0
            lh = l["bbox"].height
            new_par = prev is None or gap > 0.55 * lh

            if lvl:
                if cur and cur["type"] == "heading" and cur["level"] == lvl and not new_par:
                    cur["runs"] += [{"t": " "}] + runs
                else:
                    cur = self._push({"type": "heading", "level": lvl, "runs": runs})
            elif bm or (om and (new_par or (cur and cur["type"] == "item") or l["bbox"].x0 > x_left + 2 or prev is None)):
                m = bm or om
                runs = self._strip_prefix(runs, m.end())
                level = max(0, min(3, int(round((l["bbox"].x0 - x_left) / 18))))
                cur = self._push({"type": "item", "ordered": bool(om), "marker": (om.group(1) if om else ""),
                                  "level": level, "runs": runs})
            elif cur and cur["type"] in ("para", "item") and not new_par and cur.get("_open", True):
                self._join(cur, runs)
            else:
                indent_new = prev is not None and l["bbox"].x0 > x_left + 12 and cur and cur["type"] == "para"
                cur = self._push({"type": "para", "runs": runs})
                if indent_new:
                    pass
            # a short bold line on its own reads as a heading
            if cur["type"] == "para" and bold_line and len(lines) <= 2 and len(text.strip()) <= 90 \
                    and not text.strip().endswith((".", ":", ",")) and len(lines) == 1:
                cur["type"] = "heading"
                cur["level"] = min(4, (max(self.size_level.values()) if self.size_level else 1) + 1)
                for r in cur["runs"]:
                    r.pop("b", None)
            prev = l

    @staticmethod
    def _strip_prefix(runs, n):
        out = []
        for r in runs:
            if n > 0 and "t" in r:
                t = r["t"]
                if len(t) <= n:
                    n -= len(t); continue
                r = dict(r, t=t[n:]); n = 0
            out.append(r)
        return out

    def _push(self, blk):
        self.g.blocks.append(blk)
        return blk

    @staticmethod
    def _join(cur, runs):
        last = next((r for r in reversed(cur["runs"]) if "t" in r), None)
        first = next((r for r in runs if "t" in r), None)
        if last and first and re.search(r"[A-Za-z]-\s*$", last["t"]) and re.match(r"\s*[a-z]", first["t"]):
            last["t"] = re.sub(r"-\s*$", "", last["t"])
            first["t"] = first["t"].lstrip()
        else:
            cur["runs"].append({"t": " "})
        cur["runs"] += runs

    def merge_across_pages(self):
        """Join a paragraph split by a page break; normalise runs; drop empties."""
        out = []
        for b in self.g.blocks:
            if "runs" in b:
                b["runs"] = normalize_runs(b["runs"])
                if b["type"] == "heading":
                    for r in b["runs"]:
                        r.pop("b", None)
                if not b["runs"]:
                    continue
            prev = out[-1] if out else None
            if prev and prev["type"] == "para" and b["type"] == "para":
                pt = runs_plain(prev["runs"]).rstrip()
                nt = runs_plain(b["runs"]).lstrip()
                if pt and nt and pt[-1] not in ".:!?”\")" and nt[:1].islower() and b.get("_page_start"):
                    self._join(prev, b["runs"])
                    continue
            b.pop("_open", None)
            out.append(b)
        # mark list continuation spacing and dedupe consecutive identical headings
        clean = []
        for b in out:
            if clean and b["type"] == "heading" and clean[-1]["type"] == "heading" and \
                    runs_plain(b["runs"]) == runs_plain(clean[-1]["runs"]):
                continue
            clean.append(b)
        self.g.blocks = clean

    def pick_title(self) -> str:
        t = (self.doc.metadata or {}).get("title", "") or ""
        t = t.strip()
        bad = (not t) or t.lower() in ("untitled", "document", "pdf") or re.search(r"\.(docx?|pptx?|pdf|tex)$", t, re.I) \
            or re.match(r"^microsoft (word|powerpoint)", t, re.I)
        if not bad:
            return t
        for b in self.g.blocks[:30]:
            if b["type"] == "heading" and b["level"] == 1:
                s = runs_plain(b["runs"]).strip()
                if 3 <= len(s) <= 140:
                    return s
        for b in self.g.blocks[:3]:
            if b["type"] == "heading" and 3 <= len(runs_plain(b["runs"]).strip()) <= 140:
                return runs_plain(b["runs"]).strip()
        stem = os.path.splitext(os.path.basename(self.source))[0]
        return re.sub(r"[_\-]+", " ", stem).strip()


def extract_general(pdf_path: str, image_dir: Optional[str], opts: Optional[dict] = None, progress=None) -> GDoc:
    doc = fitz.open(pdf_path)
    try:
        if doc.needs_pass:
            raise ValueError("PDF is password protected")
        return GeneralExtractor(doc, pdf_path, image_dir, opts or {}).run(progress)
    finally:
        doc.close()


# --------------------------------------------------------------------------
# Exporters
# --------------------------------------------------------------------------
def _cell_md(c: str) -> str:
    return _md_escape(c).replace("|", "\\|") or " "


def _layout(g: GDoc, title: str):
    """Index of a heading that just repeats the title, and the level shift so
    the top remaining heading becomes H2 under the H1 title."""
    skip = None
    for i, b in enumerate(g.blocks[:15]):
        if b["type"] == "heading" and runs_plain(b["runs"]).strip().lower() == title.strip().lower():
            skip = i
            break
    levels = [b["level"] for i, b in enumerate(g.blocks) if b["type"] == "heading" and i != skip]
    base = min(levels) if levels else 1
    return skip, base


def _hlevel(b, base) -> int:
    return max(2, min(6, b["level"] - base + 2))


def g_markdown(g: GDoc, title: str, opts: dict) -> str:
    L = [f"# {_md_escape(title)}", ""]
    prev_type = None
    skip, base = _layout(g, title)
    for bi, b in enumerate(g.blocks):
        t = b["type"]
        if t == "heading":
            if bi == skip:
                continue
            text = re.sub(r"^(\d+)\\\.", r"\1.", md_runs(b["runs"]))
            L += ["", "#" * _hlevel(b, base) + " " + text, ""]
        elif t == "para":
            if prev_type == "item":
                L.append("")
            L += [md_runs(b["runs"]), ""]
        elif t == "item":
            ind = "  " * b.get("level", 0)
            marker = b["marker"] if b.get("ordered") and re.match(r"^\d+[.)]$", b.get("marker", "")) else \
                ("- " + _md_escape(b["marker"]) if b.get("ordered") else "-")
            body = md_runs(b["runs"]).replace("\n\n", "<br>")
            L.append(f"{ind}{marker} {body}")
        elif t == "code":
            L += ["```", b["text"], "```", ""]
        elif t == "table":
            rows = b["rows"]
            ncol = max(len(r) for r in rows)
            rows = [r + [""] * (ncol - len(r)) for r in rows]
            if prev_type == "item":
                L.append("")
            L.append("| " + " | ".join(_cell_md(c) for c in rows[0]) + " |")
            L.append("|" + "---|" * ncol)
            for r in rows[1:]:
                L.append("| " + " | ".join(_cell_md(c) for c in r) + " |")
            L.append("")
        elif t == "figure":
            if prev_type == "item":
                L.append("")
            L += [f'<img src="images/{b["img"]}" width="{round(min(b["w"], 520) * PX)}" alt="figure">', ""]
        elif t == "page":
            L += [f"<!-- page {b['n']} -->", ""]
        prev_type = t
    s = "\n".join(L)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip() + "\n"


G_CSS = """
:root{color-scheme:light}
body{font:15px/1.6 "Charter","Georgia",serif;max-width:820px;margin:28px auto;padding:0 20px;color:#1d1d1f;background:#fff}
h1,h2,h3,h4,h5{font-family:-apple-system,"Segoe UI",sans-serif;line-height:1.3}
h1{font-size:24px;border-bottom:2px solid #222;padding-bottom:8px}
h2{font-size:19px;margin-top:28px}h3{font-size:16px}h4,h5{font-size:15px}
table{border-collapse:collapse;margin:12px 0;font:13.5px -apple-system,sans-serif}
td,th{border:1px solid #ccc;padding:4px 10px;vertical-align:top}
th{background:#f3f3f3;text-align:left}
pre{background:#f5f5f4;padding:10px 12px;border-radius:6px;overflow:auto;font-size:13px}
code{font-size:.92em}
img.inl{vertical-align:middle}
.fig{margin:12px 0}.fig img{max-width:100%}
.pg{color:#aaa;font:11px sans-serif;text-align:center;border-top:1px dashed #ddd;margin:18px 0;padding-top:4px}
li p{display:inline;margin:0}
"""

G_PDF_CSS = """
body{font-family:serif;font-size:11pt;line-height:1.4}
h1{font-family:sans-serif;font-size:17pt;font-weight:bold}
h2{font-family:sans-serif;font-size:14pt;font-weight:bold;margin-top:10pt}
h3{font-family:sans-serif;font-size:12pt;font-weight:bold;margin-top:8pt}
h4,h5{font-family:sans-serif;font-size:11pt;font-weight:bold}
p{margin-top:3pt;margin-bottom:3pt}
pre{font-family:monospace;font-size:9pt;background-color:#f2f2f2}
table{border-collapse:collapse}
td,th{border:1px solid #999999;padding:2pt 5pt;font-size:9.5pt}
.li{margin-left:14pt;margin-top:1pt;margin-bottom:1pt}
"""


def g_html(g: GDoc, title: str, opts: dict, for_pdf=False) -> str:
    H = [f"<h1>{html.escape(title)}</h1>"]
    skip, base = _layout(g, title)
    i, blocks = 0, g.blocks
    while i < len(blocks):
        b = blocks[i]
        t = b["type"]
        if t == "heading":
            if i == skip:
                i += 1; continue
            lv = _hlevel(b, base)
            H.append(f"<h{lv}>{html_runs(b['runs'], for_pdf=for_pdf)[3:-4] if html_runs(b['runs'], for_pdf=for_pdf).startswith('<p>') else html_runs(b['runs'], for_pdf=for_pdf)}</h{lv}>")
        elif t == "para":
            H.append(html_runs(b["runs"], for_pdf=for_pdf))
        elif t == "item":
            if for_pdf:
                mk = b["marker"] if b.get("ordered") else "•"
                inner = re.sub(r"</?p>", " ", html_runs(b["runs"], for_pdf=True))
                H.append(f'<p class="li" style="margin-left:{14 + 14 * b.get("level", 0)}pt">{html.escape(mk)} {inner}</p>')
            else:
                group = []
                while i < len(blocks) and blocks[i]["type"] == "item":
                    group.append(blocks[i]); i += 1
                i -= 1
                lis = "".join(
                    f'<li style="margin-left:{18 * x.get("level", 0)}px">'
                    + (f"<b>{html.escape(x['marker'])}</b> " if x.get("ordered") else "")
                    + html_runs(x["runs"]) + "</li>" for x in group)
                ordered_all = all(x.get("ordered") for x in group)
                H.append(f'<ul style="{"list-style:none;padding-left:8px" if ordered_all else ""}">{lis}</ul>')
        elif t == "code":
            H.append(f"<pre>{html.escape(b['text'])}</pre>")
        elif t == "table":
            rows = b["rows"]
            head = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
            body = "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
            H.append(f"<table><tr>{head}</tr>{body}</table>")
        elif t == "figure":
            if for_pdf:
                w = min(b["w"], 480.0); h = b["h"] * w / max(1, b["w"])
                if h > 640:
                    w, h = w * 640 / h, 640
                H.append(f'<p><img src="{b["img"]}" width="{w:.1f}" height="{h:.1f}"/></p>')
            else:
                H.append(f'<p class="fig"><img src="images/{b["img"]}" style="width:{min(b["w"], 600) * PX:.0f}px" alt="figure"></p>')
        elif t == "page":
            H.append(f'<div class="pg">page {b["n"]}</div>' if not for_pdf else "")
        i += 1
    body = "\n".join(H)
    if for_pdf:
        return body
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{G_CSS}</style></head><body>{body}</body></html>")


def html_to_pdf(body: str, css: str, img_dir: str, out_path: str):
    archive = fitz.Archive(img_dir) if os.path.isdir(img_dir) else None
    story = fitz.Story(html=body, user_css=css, archive=archive)
    writer = fitz.DocumentWriter(out_path)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (54, 54, -54, -54)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    doc = fitz.open(out_path)
    for i, page in enumerate(doc):
        page.insert_text((mediabox.width / 2 - 12, mediabox.height - 28), f"{i + 1} / {doc.page_count}",
                         fontsize=8, color=(0.45, 0.45, 0.45))
    doc.saveIncr()
    doc.close()


def g_docx(g: GDoc, title: str, opts: dict, img_dir: str, out_path: str):
    from docx import Document
    from docx.shared import Pt, Cm
    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Cm(2))
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    max_w = 17.0 * 28.35
    styles = {s.name for s in doc.styles}

    def add_runs(p, runs):
        for r in runs:
            if "p" in r:
                p = doc.add_paragraph()
            elif "t" in r:
                run = p.add_run(r["t"])
                run.bold = bool(r.get("b")) or None
                run.italic = bool(r.get("i")) or None
                if r.get("s") == "sub":
                    run.font.subscript = True
                elif r.get("s") == "sup":
                    run.font.superscript = True
                if r.get("code"):
                    run.font.name = "Consolas"
            elif "img" in r or "block" in r:
                path = os.path.join(img_dir, r.get("img") or r.get("block"))
                if os.path.exists(path):
                    w = min(r["w"], max_w)
                    p.add_run().add_picture(path, height=Pt(r["h"] * w / max(1, r["w"])))
        return p

    doc.add_heading(title, level=0)
    skip, base = _layout(g, title)
    for bi, b in enumerate(g.blocks):
        t = b["type"]
        if t == "heading":
            if bi == skip:
                continue
            h = doc.add_heading(level=min(4, _hlevel(b, base) - 1))
            add_runs(h, b["runs"])
        elif t == "para":
            add_runs(doc.add_paragraph(), b["runs"])
        elif t == "item":
            style = "List Number" if b.get("ordered") else "List Bullet"
            lvl = b.get("level", 0)
            if lvl and f"{style} {lvl + 1}" in styles:
                style = f"{style} {lvl + 1}"
            if b.get("ordered"):
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.6 + 0.6 * lvl)
                p.add_run(b["marker"] + " ").bold = True
            else:
                p = doc.add_paragraph(style=style if style in styles else None)
            add_runs(p, b["runs"])
        elif t == "code":
            p = doc.add_paragraph()
            r = p.add_run(b["text"])
            r.font.name = "Consolas"; r.font.size = Pt(9)
        elif t == "table":
            rows = b["rows"]
            ncol = max(len(r) for r in rows)
            tb = doc.add_table(rows=0, cols=ncol)
            tb.style = "Table Grid" if "Table Grid" in styles else None
            for ri, row in enumerate(rows):
                cells = tb.add_row().cells
                for ci in range(ncol):
                    cells[ci].text = row[ci] if ci < len(row) else ""
                    if ri == 0:
                        for run in cells[ci].paragraphs[0].runs:
                            run.bold = True
            doc.add_paragraph()
        elif t == "figure":
            path = os.path.join(img_dir, b["img"])
            if os.path.exists(path):
                w = min(b["w"], max_w)
                h = b["h"] * w / max(1, b["w"])
                if h > 680:
                    w, h = w * 680 / h, 680
                doc.add_paragraph().add_run().add_picture(path, width=Pt(w))
        elif t == "page" and opts.get("page_markers"):
            doc.add_page_break()
    doc.save(out_path)


def g_json(g: GDoc, title: str) -> dict:
    blocks = []
    for b in g.blocks:
        d = dict(b)
        if "runs" in d:
            d["text"] = runs_plain(d["runs"]).strip()
        blocks.append(d)
    return {"title": title, "source": os.path.basename(g.source), "profile": "general", "meta": g.meta,
            "pages": g.pages, "ocr_pages": g.ocr_pages, "warnings": g.warnings, "blocks": blocks}


def convert_general(pdf_path: str, out_root: str, opts: Optional[dict] = None,
                    formats=("md", "html", "docx", "pdf", "json"), progress=None) -> dict:
    opts = dict(opts or {})
    formats = tuple(formats) + ("html",)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_dir = os.path.join(out_root, stem)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(out_dir, exist_ok=True)
    g = extract_general(pdf_path, img_dir, opts, progress)
    title = opts.get("title") or g.title
    files = {}
    if "md" in formats:
        p = os.path.join(out_dir, stem + ".md")
        open(p, "w", encoding="utf-8").write(g_markdown(g, title, opts))
        files["md"] = p
    p = os.path.join(out_dir, stem + ".html")
    open(p, "w", encoding="utf-8").write(g_html(g, title, opts))
    files["html"] = p
    if "json" in formats:
        p = os.path.join(out_dir, stem + ".json")
        json.dump(g_json(g, title), open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        files["json"] = p
    if "docx" in formats:
        p = os.path.join(out_dir, stem + ".docx")
        g_docx(g, title, opts, img_dir, p)
        files["docx"] = p
    if "pdf" in formats:
        p = os.path.join(out_dir, stem + ".pdf")
        html_to_pdf(g_html(g, title, opts, for_pdf=True), G_PDF_CSS, img_dir, p)
        files["pdf"] = p
    if "md" in formats:
        zp = os.path.join(out_dir, stem + "_markdown.zip")
        used = sorted(set(re.findall(r"images/(img_\d+\.png)", open(files["md"], encoding="utf-8").read())))
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(files["md"], f"{stem}/{stem}.md")
            for u in used:
                z.write(os.path.join(img_dir, u), f"{stem}/images/{u}")
        files["md_zip"] = zp
    counts = Counter(b["type"] for b in g.blocks)
    summary = {"source": os.path.basename(pdf_path), "out_dir": out_dir, "profile": "general",
               "outputs": [{"title": title, "slug": stem, "questions": 0, "files": files,
                            "stats": dict(counts)}],
               "sections": [], "warnings": g.warnings, "meta": g.meta,
               "guess": {"title": g.title}, "options": opts, "ocr_pages": g.ocr_pages}
    json.dump(summary, open(os.path.join(out_dir, "_summary.json"), "w"), indent=1, default=str)
    return summary
