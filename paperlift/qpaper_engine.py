"""
Paperlift exam-paper (qpaper) extraction engine.

Turns an IITM-style "flattened" question-paper PDF (text layer + embedded
images for math/figures, green/red coloured option ids for the answer key)
into a structured Paper object that the exporters render to MD/DOCX/PDF/JSON.

Pipeline:
  1. page_lines()  - collect text spans + images per page, cluster them into
                     visual lines (left->right), skipping the tick/cross icons.
  2. Parser        - a small state machine over those lines that recognises
                     question headers, options, accepted answers, comprehension
                     passages, section titles and drops all the console metadata.
  3. Images        - every referenced image is extracted once (by xref) as the
                     original PNG/JPEG, transparency flattened onto white.
"""
from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import pymupdf as fitz

# --------------------------------------------------------------------------
# Console metadata keys that are never question content.
# --------------------------------------------------------------------------
META_KEYS = [
    "Sub-Section Number", "Sub-Section Id", "Question Shuffling Allowed",
    "Sub Question Shuffling Allowed", "Response Type", "Evaluation Required For SA",
    "Show Word Count", "Answers Type", "Text Areas", "Section Id", "Section Number",
    "Section type", "Mandatory or Optional", "Number of Questions",
    "Number of Questions to be attempted", "Section Marks", "Display Number Panel",
    "Section Negative Marks", "Group All Questions", "Enable Mark as Answered",
    "Clear Response", "Section Maximum Duration", "Section Minimum Duration",
    "Section Time In", "Maximum Instruction Time", "Minimum Instruction Time",
    "Group Number", "Group Id", "Group Maximum Duration", "Group Minimum Duration",
    "Show Attended Group", "Edit Attended Group", "Break time", "Group Marks",
    "Is this Group for Examiner", "Examiner permission", "Show Progress Bar",
    "Revisit allowed for group Instructions", "Group Time In", "Revisit Section",
    "Action on Revisit Section", "Navigate To Group Summary From Last Question",
    "Disable Submit Button During Assessment", "Section Selection Time",
    "No of Optional sections to be attempted", "Question Paper Name", "Subject Name",
    "Creation Date", "Duration", "Total Marks", "Display Marks",
    "Share Answer Key With Delivery Engine", "Actual Answer Key", "Calculator",
    "Magnifying Glass Required", "Ruler Required", "Eraser Required",
    "Scratch Pad Required", "Rough Sketch/Notepad Required", "Protractor Required",
    "Show Watermark on Console", "Highlighter", "Auto Save on Console",
    "Change Font Color", "Change Background Color", "Change Theme", "Help Button",
    "Show Reports", "Group Comprehension Questions", "Question Pattern Type",
    "Correct Marks", "Question Label", "Question Numbers", "Is Section Default",
    "Section Duration", "Question Type",
]
META_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(k) for k in sorted(META_KEYS, key=len, reverse=True))
    + r")\b[^:]{0,40}:", re.I)
NOISE_RE = re.compile(
    r"^\s*(Notations\s*:|\d\.\s*Options shown in|Indian Institute of Technology|"
    r"Allowed\s*:|Questions\s*:\s*(Yes|No)|Group\s+[IVXLC\d]+\s*$)", re.I)

QNUM_RE = re.compile(r"Question\s+Number\s*:\s*(\d+)\s*Question\s+Id\s*:\s*(\d+)"
                     r"(?:\s*Question\s+Type\s*:\s*([A-Za-z_]+))?", re.I)
QGROUP_RE = re.compile(r"^\s*Question\s+Id\s*:\s*(\d+)", re.I)
QNUMS_RE = re.compile(r"Question\s+Numbers\s*:\s*\(\s*(\d+)\s*to\s*(\d+)\s*\)", re.I)
MARKS_RE = re.compile(r"Correct\s+Marks\s*:\s*([\d.]+)", re.I)
NEG_RE = re.compile(r"Negative\s+Marks\s*:\s*([\d.]+)", re.I)
MAXSEL_RE = re.compile(r"Max\.?\s*Selectable\s+Options\s*:\s*(\d+)", re.I)
LABEL_RE = re.compile(r"^\s*Question\s+Label\s*:\s*(.+)$", re.I)
OPTID_RE = re.compile(r"^\s*(\d{8,})\s*\.\s*")
SUBJECT_CHECK_RE = re.compile(r"THIS IS QUESTION PAPER FOR THE SUBJECT\s*\"?(.+?)\"", re.I | re.S)
HEADER_META = ["Question Paper Name", "Subject Name", "Creation Date", "Duration", "Total Marks"]

LABEL_TO_TYPE = {
    "multiple choice question": "MCQ",
    "multiple select question": "MSQ",
    "short answer question": "SA",
    "long answer question": "LA",
    "comprehension": "COMPREHENSION",
}


# --------------------------------------------------------------------------
# Data model.  Rich content = list of runs:
#   {"t": "text", "s": "sub"|"sup"}           text (style optional)
#   {"img": "x12.png", "w": pt, "h": pt}       inline image
#   {"block": "x13.png", "w": pt, "h": pt}     standalone image / figure
#   {"p": 1}                                   paragraph break
# --------------------------------------------------------------------------
@dataclass
class Option:
    oid: str
    content: List[dict] = field(default_factory=list)
    correct: bool = False


@dataclass
class Question:
    number: int
    qid: str
    qtype: str = ""
    label: str = ""
    marks: Optional[float] = None
    neg_marks: Optional[float] = None
    max_select: Optional[int] = None
    body: List[dict] = field(default_factory=list)
    options: List[Option] = field(default_factory=list)
    answers: List[str] = field(default_factory=list)
    group: Optional[str] = None          # comprehension group id
    subject_check: bool = False
    page: int = 0


@dataclass
class Group:
    gid: str
    first: Optional[int] = None
    last: Optional[int] = None
    passage: List[dict] = field(default_factory=list)


@dataclass
class Section:
    name: str
    meta: Dict[str, str] = field(default_factory=dict)
    questions: List[Question] = field(default_factory=list)
    groups: Dict[str, Group] = field(default_factory=dict)
    subject: Optional[str] = None        # from the "THIS IS QUESTION PAPER FOR" check
    partial: bool = False                # content before the first section header


@dataclass
class Paper:
    source: str
    meta: Dict[str, str] = field(default_factory=dict)
    sections: List[Section] = field(default_factory=list)
    images: Dict[str, dict] = field(default_factory=dict)   # name -> {w,h,px_w,px_h}
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# 1. Visual lines
# --------------------------------------------------------------------------
def _is_icon(r: fitz.Rect) -> bool:
    return r.width <= 14.5 and r.height <= 14.5 and abs(r.width - r.height) < 2


def _is_green(c: int) -> bool:
    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
    return g > 90 and g > r + 40 and g > b + 40


def _is_red(c: int) -> bool:
    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
    return r > 150 and r > g + 80 and r > b + 80


BLOCK_MIN_H = 30.0


@dataclass
class Line:
    page: int
    frags: list
    y0: float
    y1: float

    @property
    def plain(self) -> str:
        return "".join(f["text"] for f in self.frags if f["kind"] == "t")

    @property
    def texts(self):
        return [f for f in self.frags if f["kind"] == "t"]

    @property
    def images(self):
        return [f for f in self.frags if f["kind"] == "i"]

    @property
    def main_size(self) -> float:
        best: Dict[float, int] = {}
        for f in self.texts:
            n = len(f["text"].strip())
            if n:
                best[round(f["size"], 1)] = best.get(round(f["size"], 1), 0) + n
        return max(best, key=best.get) if best else 0.0

    @property
    def bold(self) -> bool:
        t = [f for f in self.texts if f["text"].strip()]
        return bool(t) and all(f["flags"] & 16 for f in t)


def page_lines(page: fitz.Page, pno: int) -> List[Line]:
    els = []
    d = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for s in ln["spans"]:
                chars = s.get("chars") or []
                if not chars:
                    continue
                # Split a span wherever a wide gap exists: inline math images are
                # drawn into those gaps, so they must be able to sit between pieces.
                pieces, cur, last_x1 = [], [], None
                for ch in chars:
                    c = ch["c"]
                    if c.strip() and last_x1 is not None and ch["bbox"][0] - last_x1 > 0.8 * s["size"] and cur:
                        pieces.append(cur)
                        cur = []
                    cur.append(ch)
                    if c.strip():
                        last_x1 = ch["bbox"][2]
                if cur:
                    pieces.append(cur)
                for pc in pieces:
                    text = "".join(ch["c"] for ch in pc)
                    r = fitz.Rect(pc[0]["bbox"])
                    for ch in pc[1:]:
                        r |= fitz.Rect(ch["bbox"])
                    r.y0, r.y1 = s["bbox"][1], s["bbox"][3]
                    els.append(dict(kind="t", text=text, bbox=r, color=s["color"], size=s["size"],
                                    flags=s["flags"], oy=s["origin"][1]))
    for info in page.get_image_info(xrefs=True):
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.width < 1 or r.height < 1 or _is_icon(r):
            continue
        els.append(dict(kind="i", xref=info["xref"], bbox=r))

    els.sort(key=lambda e: (round(e["bbox"].y0, 1), e["bbox"].x0))
    lines: List[dict] = []
    for e in els:
        r = e["bbox"]
        big = e["kind"] == "i" and r.height >= BLOCK_MIN_H
        placed = False
        if not big:
            for L in lines[-3:][::-1]:
                if L["big"]:
                    continue
                a0, a1 = L["anchor"]
                ov = min(a1, r.y1) - max(a0, r.y0)
                if ov >= 0.5 * min(a1 - a0, r.height):
                    L["frags"].append(e)
                    L["y0"], L["y1"] = min(L["y0"], r.y0), max(L["y1"], r.y1)
                    placed = True
                    break
        if not placed:
            lines.append(dict(frags=[e], anchor=(r.y0, r.y1), y0=r.y0, y1=r.y1, big=big))
    # A tall option image (e.g. a matrix) sits beside its option id, which is
    # vertically centred / bottom-aligned on it: pull the id into the image's line.
    for i, L in enumerate(lines):
        if not L["big"]:
            continue
        img = L["frags"][0]["bbox"]
        for M in lines[i + 1:i + 4]:
            if M["big"] or M.get("merged"):
                continue
            cy = (M["anchor"][0] + M["anchor"][1]) / 2
            texts = [f for f in M["frags"] if f["kind"] == "t" and f["text"].strip()]
            if img.y0 <= cy <= img.y1 + 2 and texts and OPTID_RE.match(texts[0]["text"]) \
                    and texts[0]["bbox"].x1 <= img.x0 + 2:
                L["frags"].extend(M["frags"])
                L["y0"], L["y1"] = min(L["y0"], M["y0"]), max(L["y1"], M["y1"])
                L["big"] = False
                M["merged"] = True
                break
    lines = [L for L in lines if not L.get("merged")]

    out = []
    for L in lines:
        fr = sorted(L["frags"], key=lambda f: f["bbox"].x0)
        if not any((f["kind"] == "i") or f["text"].strip() for f in fr):
            continue
        out.append(Line(pno, fr, L["y0"], L["y1"]))
    out.sort(key=lambda l: l.y0)
    return out


# --------------------------------------------------------------------------
# 2. Rich-content helpers
# --------------------------------------------------------------------------
class ImageStore:
    def __init__(self, doc: fitz.Document, outdir: Optional[str]):
        self.doc, self.outdir = doc, outdir
        self.meta: Dict[str, dict] = {}
        self.done: Dict[int, str] = {}
        self.renders: List[str] = []

    def name_for(self, xref: int) -> str:
        if xref in self.done:
            return self.done[xref]
        name = f"img_{len(self.done) + len(self.renders) + 1:03d}.png"
        self.done[xref] = name
        if self.outdir:
            os.makedirs(self.outdir, exist_ok=True)
            px = self._extract(xref, os.path.join(self.outdir, name))
            self.meta[name] = px
        return name

    def add_render(self, page: "fitz.Page", clip: "fitz.Rect", dpi: int = 150) -> str:
        """Render a page region (vector figure, scanned page) to PNG."""
        name = f"img_{len(self.done) + len(self.renders) + 1:03d}.png"
        self.renders.append(name)
        if self.outdir:
            os.makedirs(self.outdir, exist_ok=True)
            pix = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
            pix.save(os.path.join(self.outdir, name))
            self.meta[name] = {"px_w": pix.width, "px_h": pix.height, "fx": 1.0, "fy": 1.0}
            pix = None
        return name

    def _extract(self, xref: int, path: str) -> dict:
        """Original pixels; transparency flattened on white (readable in dark mode)."""
        try:
            from PIL import Image
            info = self.doc.extract_image(xref)
            im = Image.open(io.BytesIO(info["image"]))
            im.load()
            if info.get("smask"):
                m = Image.open(io.BytesIO(self.doc.extract_image(info["smask"])["image"])).convert("L")
                if m.size != im.size:
                    m = m.resize(im.size)
                im = im.convert("RGBA")
                im.putalpha(m)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                im = Image.alpha_composite(bg, im)
            im = im.convert("RGB")
            ow, oh = im.size
            # trim the white padding MathJax-style renders carry (tighter inline math)
            from PIL import ImageChops, ImageOps
            diff = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255)))
            box = ImageOps.grayscale(diff).point(lambda v: 255 if v > 24 else 0).getbbox()
            fx = fy = ox = oy = 0.0
            if box:
                l, t, r, b = max(0, box[0] - 1), max(0, box[1] - 1), min(ow, box[2] + 1), min(oh, box[3] + 1)
                if (r - l) * (b - t) < ow * oh:
                    im = im.crop((l, t, r, b))
                ox, oy = l / ow, t / oh
            im.save(path, "PNG", optimize=True)
            return {"px_w": im.width, "px_h": im.height, "fx": im.width / ow, "fy": im.height / oh}
        except Exception:
            pix = fitz.Pixmap(self.doc, xref)
            if pix.n - pix.alpha > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if pix.alpha:
                pix = fitz.Pixmap(pix, 0)
            pix.save(path)
            return {"px_w": pix.width, "px_h": pix.height}


def line_runs(line: Line, images: ImageStore, skip_first_text: Optional[str] = None) -> List[dict]:
    """Convert one visual line to runs (text with sub/sup, inline/block images)."""
    runs: List[dict] = []
    main = line.main_size or 12.0
    main_oy = None
    for f in line.texts:
        if f["text"].strip() and abs(f["size"] - main) < 0.6:
            main_oy = f["oy"]
            break
    frags = list(line.frags)
    if skip_first_text is not None:
        for i, f in enumerate(frags):
            if f["kind"] == "t" and f["text"].strip():
                rest = f["text"][len(skip_first_text):] if f["text"].startswith(skip_first_text) else \
                    OPTID_RE.sub("", f["text"], count=1)
                frags[i] = dict(f, text=rest)
                break
    only_image = len([f for f in frags if f["kind"] == "t" and f["text"].strip()]) == 0
    for f in frags:
        if f["kind"] == "i":
            r = f["bbox"]
            name = images.name_for(f["xref"])
            key = "block" if (only_image and r.height >= BLOCK_MIN_H) else "img"
            m = images.meta.get(name, {})
            runs.append({key: name, "w": round(r.width * m.get("fx", 1), 1), "h": round(r.height * m.get("fy", 1), 1)})
            continue
        t = f["text"]
        if not t:
            continue
        style = None
        if t.strip() and f["size"] < main * 0.86 and main_oy is not None:
            style = "sub" if f["oy"] > main_oy + 0.5 else "sup"
        elif t.strip() and f["flags"] & 1:
            style = "sup"
        run = {"t": t}
        if style:
            run["s"] = style
        runs.append(run)
    # keep the word gap after an inline image (pdf puts the space before it)
    for i in range(1, len(runs) - 1):
        if "img" in runs[i] and "t" in runs[i - 1] and "t" in runs[i + 1]:
            prev, nxt = runs[i - 1]["t"], runs[i + 1]["t"]
            if prev[-1:].isspace() and nxt[:1].isalnum():
                runs[i + 1]["t"] = " " + nxt
    return runs


def normalize_runs(runs: List[dict]) -> List[dict]:
    """Merge adjacent text runs, collapse whitespace, trim paragraph edges."""
    out: List[dict] = []
    for r in runs:
        if "t" in r:
            t = re.sub(r"\s+", " ", r["t"])
            if out and "t" in out[-1] and all(out[-1].get(k) == r.get(k) for k in ("s", "b", "i", "code")):
                out[-1]["t"] += t
                out[-1]["t"] = re.sub(r" {2,}", " ", out[-1]["t"])
            else:
                nr = dict(r, t=t)
                out.append(nr)
        elif "p" in r:
            if out and "p" not in out[-1]:
                out.append(r)
        else:
            out.append(r)
    # trim spaces next to paragraph boundaries / block images
    def is_break(x):
        return x is None or "p" in x or "block" in x
    for i, r in enumerate(out):
        if "t" in r and r.get("s") is None:
            if is_break(out[i - 1] if i else None):
                r["t"] = r["t"].lstrip()
            if is_break(out[i + 1] if i + 1 < len(out) else None):
                r["t"] = r["t"].rstrip()
    out = [r for r in out if not ("t" in r and r["t"] == "")]
    while out and "p" in out[0]:
        out.pop(0)
    while out and "p" in out[-1]:
        out.pop()
    return out


def runs_plain(runs: List[dict]) -> str:
    return "".join(r.get("t", "") for r in runs)


# --------------------------------------------------------------------------
# 3. Parser
# --------------------------------------------------------------------------
class Parser:
    def __init__(self, doc: fitz.Document, source: str, image_dir: Optional[str]):
        self.doc = doc
        self.paper = Paper(source=source)
        self.images = ImageStore(doc, image_dir)
        self.state = "skip"
        self.section: Optional[Section] = None
        self.q: Optional[Question] = None
        self.group: Optional[Group] = None
        self.target: Optional[List[dict]] = None
        self.prev: Optional[Line] = None

    # ---- helpers ---------------------------------------------------------
    def _sec(self) -> Section:
        if self.section is None:
            self.section = Section(name="(continued from previous section)", partial=True)
            self.paper.sections.append(self.section)
        return self.section

    def _append(self, line: Line, skip_first_text=None, new_para_ok=True):
        if self.target is None:
            return
        runs = line_runs(line, self.images, skip_first_text)
        if not runs:
            return
        if self.target and self.prev is not None and new_para_ok:
            gap = line.y0 - self.prev.y1
            h = max(8.0, (self.prev.y1 - self.prev.y0))
            blockish = any("block" in r for r in runs) or any("block" in r for r in self.target[-1:])
            if self.prev.page == line.page and (gap > 0.6 * min(h, 16) or blockish):
                self.target.append({"p": 1})
            elif blockish:
                self.target.append({"p": 1})
            else:
                self.target.append({"t": " "})
        self.target.extend(runs)
        self.prev = line

    def _finish_question(self):
        q = self.q
        if q is None:
            return
        q.body = normalize_runs(q.body)
        for o in q.options:
            o.content = normalize_runs(o.content)
        if not q.qtype:
            q.qtype = LABEL_TO_TYPE.get(q.label.lower().strip(), q.label or "Q")
        q.qtype = q.qtype.upper()
        m = SUBJECT_CHECK_RE.search(runs_plain(q.body))
        if m:
            q.subject_check = True
            sec = self._sec()
            sec.subject = sec.subject or re.sub(r"\s+", " ", m.group(1)).strip()
        self.q = None

    def _finish_group(self):
        if self.group is not None:
            self.group.passage = normalize_runs(self.group.passage)
        self.group = None

    # ---- main loop -------------------------------------------------------
    def run(self, progress=None) -> Paper:
        total = self.doc.page_count
        for pno, page in enumerate(self.doc):
            for line in page_lines(page, pno):
                self.feed(line)
            if progress:
                progress(pno + 1, total)
        self._finish_question()
        self._finish_group()
        # close any group passage normalisation done above; build warnings
        for s in self.paper.sections:
            for q in s.questions:
                if q.qtype in ("MCQ", "MSQ") and not q.options:
                    self.paper.warnings.append(f"Q{q.number} ({q.qtype}) has no options detected")
                if q.qtype in ("MCQ", "MSQ") and q.options and not any(o.correct for o in q.options):
                    self.paper.warnings.append(f"Q{q.number}: no correct option detected")
                if q.qtype == "SA" and not q.answers:
                    self.paper.warnings.append(f"Q{q.number}: no accepted answer detected")
        self.paper.meta.update({"pages": str(self.doc.page_count)})
        self.paper.images = self.images.meta
        return self.paper

    def feed(self, line: Line):
        plain = line.plain.strip()

        # header meta (first occurrence wins)
        for k in HEADER_META:
            m = re.match(r"^\s*" + re.escape(k) + r"\s*:\s*(.+)$", plain, re.I)
            if m and k not in self.paper.meta:
                self.paper.meta[k] = m.group(1).strip()

        # section title: big bold line ("Deep Learning"), not "Group I"
        if plain and line.main_size >= 15.5 and line.bold and not line.images \
                and not re.match(r"^Group\b", plain, re.I) and not META_RE.match(plain):
            self._finish_question()
            self._finish_group()
            self.section = Section(name=plain)
            self.paper.sections.append(self.section)
            self.state, self.target = "section", None
            return

        if self.state == "section":
            m = re.match(r"^\s*(Number of Questions(?: to be attempted)?|Section Marks|Section Negative Marks)\s*:\s*(.+)$", plain, re.I)
            if m:
                self.section.meta[m.group(1)] = m.group(2).strip()

        # question header
        m = QNUM_RE.search(plain)
        if m:
            self._finish_question()
            num = int(m.group(1))
            if self.group and not (self.group.first is None or self.group.first <= num <= (self.group.last or num)):
                self._finish_group()
            q = Question(number=num, qid=m.group(2), qtype=(m.group(3) or "").upper(), page=line.page + 1)
            if self.group:
                q.group = self.group.gid
            self._sec().questions.append(q)
            self.q = q
            self._header_bits(plain)
            self.state, self.target = "qhead", None
            return

        # comprehension group header ("Question Id : ... Question Type : COMPREHENSION")
        m = QGROUP_RE.match(plain)
        if m:
            self._finish_question()
            self._finish_group()
            self.group = Group(gid=m.group(1))
            self._sec().groups[self.group.gid] = self.group
            self._group_bits(plain)
            self.state, self.target = "ghead", None
            return

        if self.state in ("qhead", "ghead"):
            self._header_bits(plain)
            if self.state == "ghead":
                self._group_bits(plain)
            lm = LABEL_RE.match(plain)
            if lm:
                label = lm.group(1).strip()
                if self.state == "ghead" or label.lower() == "comprehension":
                    self.state = "passage"
                    if self.group is None:
                        self.group = Group(gid="g%d" % len(self._sec().groups))
                        self._sec().groups[self.group.gid] = self.group
                    self.target, self.prev = self.group.passage, None
                else:
                    self.q.label = label
                    self.state = "body"
                    self.target, self.prev = self.q.body, None
            return

        if not plain and not line.images:
            return
        if NOISE_RE.match(plain):
            return

        # options block
        if re.match(r"^\s*Options\s*:\s*$", plain, re.I) and self.q:
            self.state, self.target = "options", None
            return
        if re.match(r"^\s*Possible Answers\s*:", plain, re.I) and self.q:
            self.state, self.target = "answers", None
            rest = re.sub(r"^\s*Possible Answers\s*:\s*", "", plain, flags=re.I)
            if rest:
                self.q.answers.append(rest)
            return

        first_text = next((f for f in line.texts if f["text"].strip()), None)
        om = OPTID_RE.match(first_text["text"]) if first_text else None
        if om and self.q and self.state in ("options", "option", "body"):
            opt = Option(oid=om.group(1), correct=_is_green(first_text["color"]))
            self.q.options.append(opt)
            self.state = "option"
            self.target, self.prev = opt.content, None
            self._append(line, skip_first_text=first_text["text"][:om.end()])
            return

        if META_RE.match(plain):
            self._header_bits(plain)
            self.state, self.target = "skip", None
            return

        if self.state == "answers":
            if plain:
                self.q.answers.append(plain)
            return

        if self.state == "passage":
            if re.match(r"^(Based on the above data, answer the given sub-?questions\.?|Sub\s*questions)$", plain, re.I):
                return
            self._append(line)
            return

        if self.state in ("body", "option"):
            self._append(line)
            return
        # anything else (state skip) is console metadata -> ignored

    def _header_bits(self, plain: str):
        if not self.q:
            return
        m = MARKS_RE.search(plain)
        if m:
            self.q.marks = float(m.group(1))
        m = NEG_RE.search(plain)
        if m:
            self.q.neg_marks = float(m.group(1))
        m = MAXSEL_RE.search(plain)
        if m:
            self.q.max_select = int(m.group(1))
        m = re.search(r"Question\s+Type\s*:\s*([A-Za-z_]+)", plain)
        if m and not self.q.qtype:
            self.q.qtype = m.group(1).upper()

    def _group_bits(self, plain: str):
        m = QNUMS_RE.search(plain)
        if m and self.group:
            self.group.first, self.group.last = int(m.group(1)), int(m.group(2))


# --------------------------------------------------------------------------
# 4. Title metadata from filename + header
# --------------------------------------------------------------------------
TERM_NAMES = {"T1": "Term 1 (Jan)", "T2": "Term 2 (May)", "T3": "Term 3 (Sep)"}
EXAM_NAMES = {"Q1": "Quiz 1", "Q2": "Quiz 2", "ET": "End Term", "OPPE": "OPPE", "F": "Final"}


def guess_title_fields(source: str, paper: Paper) -> dict:
    stem = os.path.splitext(os.path.basename(source))[0]
    f = {"code": "", "year": "", "term": "", "exam": "", "subject": "", "date": ""}
    m = re.search(r"([A-Za-z]{2,4}\d{3,4}[A-Za-z]?)", stem)
    if m:
        f["code"] = m.group(1).upper()
    m = re.search(r"(20\d{2})[_ -]?(T[123])", stem, re.I)
    if m:
        f["year"], f["term"] = m.group(1), m.group(2).upper()
    else:
        m = re.search(r"(20\d{2})", stem)
        if m:
            f["year"] = m.group(1)
    m = re.search(r"(?:^|_)(Q[12]|ET|OPPE\d?|F)(?:_|$)", stem, re.I)
    if m:
        f["exam"] = EXAM_NAMES.get(m.group(1).upper(), m.group(1).upper())
    qpn = paper.meta.get("Question Paper Name", "")
    m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2})", qpn)
    if m:
        f["date"] = m.group(1)
        if not f["year"]:
            f["year"] = m.group(1)[-4:]
    return f


def subject_display(sec: Section) -> str:
    """Prefer the section heading ('Deep Learning'); fall back to subject check text."""
    if not sec.partial and sec.name:
        return sec.name
    if sec.subject:
        s = re.sub(r"^\s*(DEGREE|DIPLOMA|FOUNDATION)\s+LEVEL\s*:\s*", "", sec.subject, flags=re.I)
        s = re.sub(r"\(COMPUTER BASED EXAM\)", "", s, flags=re.I).strip()
        return s.title()
    return sec.name


def extract(pdf_path: str, image_dir: Optional[str] = None, progress=None) -> Paper:
    doc = fitz.open(pdf_path)
    try:
        return Parser(doc, pdf_path, image_dir).run(progress)
    finally:
        doc.close()
