"""
Fidelity audit — reference-free verification of an extraction.

Why this works
--------------
Document converters are normally evaluated against hand-made ground truth
(edit distance, TEDS, reading-order scores), which does not exist for the file
a user just converted. Reference-free methods so far compare a re-rendered
output image with the page image.

A *born-digital or flattened* PDF offers something stronger: its text layer
and embedded images are an exact inventory of the page's content. The layer
says nothing reliable about structure, but it does say what must be preserved.
So an extraction can be audited like an accounting ledger:

  every source unit u (a text line's word tokens, or an embedded image) gets a fate
      KEPT        -> must be *witnessed* in the output              (verified)
      DROPPED(r)  -> intentionally removed for an explicit reason r (listed, not penalised)

and three families of checks are computed:

  1. Conservation   token recall R_t, image recall R_i, output precision P
                    (output text that no source unit accounts for = duplication or garbage)
  2. Order          pairwise concordance O over pairs whose relative order is fixed by
                    layout invariants (same line block; same column stack; same question)
  3. Checksums      document-declared redundancy (exam papers): declared question count
                    and section marks, question-ID and option-ID arithmetic progressions,
                    answer cardinality per question type

Witnessing uses k-gram shingles (k = 3, or the whole line when shorter) matched
against the output token stream with *consumption*: every output occurrence can
witness at most one source occurrence, so a deleted repeat or a duplicated
block is detected rather than masked.

Fidelity score  F = 100 · R_t · R_i · O · P   (each in [0, 1])
"""
from __future__ import annotations

import bisect
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import pymupdf as fitz

TOKEN_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?|[^\W\d_]+", re.U)


def tokens(text: str) -> List[str]:
    return TOKEN_RE.findall(unicodedata.normalize("NFKC", text or "").lower())


# --------------------------------------------------------------------------
# Output side: one token stream + an image multiset
# --------------------------------------------------------------------------
class OutStream:
    def __init__(self):
        self.toks: List[str] = []
        self.imgs: Counter = Counter()
        self.page: Optional[int] = None       # 1-based source page of what is being added, when known
        self.pages: List[Optional[int]] = []

    def text(self, s: str):
        t = tokens(s)
        self.toks += t
        self.pages += [self.page] * len(t)

    def runs(self, runs: Iterable[dict]):
        # tokenise the concatenated text: style changes ("𝜇" italic + "Jy") are not word breaks,
        # exactly as in the source text layer; images are skipped on both sides
        buf = []
        for r in runs or []:
            if "t" in r:
                buf.append(r["t"])
            elif "p" in r:
                buf.append(" ")
            elif "img" in r or "block" in r:
                self.imgs[r.get("img") or r.get("block")] += 1
        self.text("".join(buf))


class Witness:
    """k-gram index over the output with position-level consumption.

    An output position can witness at most one source line (`claimed`), so a
    deleted repeat or a duplicated block shows up instead of being masked.
    Within one line, overlapping k-grams may share positions (`line_pos`).
    """
    MAX_SCAN = 400

    def __init__(self, toks: List[str]):
        self.toks = toks
        self.pos: Dict[int, Dict[tuple, List[int]]] = {1: defaultdict(list), 2: defaultdict(list), 3: defaultdict(list)}
        for k in (1, 2, 3):
            d = self.pos[k]
            for i in range(len(toks) - k + 1):
                d[tuple(toks[i:i + k])].append(i)
        self.claimed = bytearray(len(toks))     # owned by an earlier line
        self.covered = self.claimed               # alias used by the report
        self.line_map: Dict[int, int] = {}        # output position -> source token index (current line)
        self.src_map: Dict[int, int] = {}         # source token index -> output position (current line)

    def _free(self, p: int, k: int, src: int) -> bool:
        """Positions p..p+k-1 are unclaimed by earlier lines and, within this line, either
        unused or already mapped to the *same* source tokens (overlapping k-grams).
        This keeps the source-to-output token mapping one-to-one."""
        for t in range(k):
            q = p + t
            if self.claimed[q] or self.line_map.get(q, src + t) != src + t or self.src_map.get(src + t, q) != q:
                return False
        return True

    def take(self, gram: tuple, near: int, max_dist: Optional[int] = None, src: int = 0) -> Optional[int]:
        k = len(gram)
        lst = self.pos[k].get(gram)
        if not lst:
            return None
        j = bisect.bisect_left(lst, near)
        lo, hi = j - 1, j
        for _ in range(self.MAX_SCAN):        # nearest free occurrence, preferring forward
            cand = []
            if hi < len(lst):
                cand.append((lst[hi] - near, lst[hi], "hi"))
            if lo >= 0:
                cand.append((near - lst[lo] + 0.5, lst[lo], "lo"))
            if not cand:
                return None
            d, p, side = min(cand)
            if max_dist is not None and d > max_dist:
                return None
            if self._free(p, k, src):
                for t in range(k):
                    self.line_map[p + t] = src + t
                    self.src_map[src + t] = p + t
                return p
            if side == "hi":
                hi += 1
            else:
                lo -= 1
        return None

    def commit_line(self):
        for i in self.line_map:
            self.claimed[i] = 1
        self.line_map, self.src_map = {}, {}


# --------------------------------------------------------------------------
# Source side: units with fates
# --------------------------------------------------------------------------
@dataclass
class Unit:
    page: int
    bbox: Tuple[float, float, float, float]
    kind: str                     # "text" | "image"
    toks: List[str] = field(default_factory=list)
    img: Optional[str] = None     # output image name (images)
    fate: str = "kept"            # "kept" | "dropped" | "scan"
    reason: str = ""
    flow: str = ""                # order group: units in one flow must appear in order
    excerpt: str = ""
    # results
    covered: int = 0
    pos: Optional[float] = None
    status: str = ""              # verified | partial | missing | dropped | scan


def verify_units(units: List[Unit], out: OutStream) -> Witness:
    w = Witness(out.toks)
    cursor = 0
    img_left = Counter(out.imgs)
    for u in units:
        if u.fate != "kept":
            u.status = "dropped" if u.fate == "dropped" else "scan"
            continue
        if u.kind == "image":
            if u.img and img_left[u.img] > 0:
                img_left[u.img] -= 1
                u.status, u.covered = "verified", 1
            else:
                u.status = "missing"
            continue
        T = u.toks
        m = len(T)
        if m == 0:
            u.status = "dropped"
            u.reason = u.reason or "no text"
            continue
        k = min(3, m)
        cov = [False] * m
        chosen: List[int] = []
        for i in range(m - k + 1):
            p = w.take(tuple(T[i:i + k]), chosen[-1] + 1 if chosen else cursor, src=i)
            if p is not None:
                for j in range(i, i + k):
                    cov[j] = True
                chosen.append(p)
        # second chance for single words not reached by any k-gram (an edit right next to them):
        # only accepted close to where the rest of this line was found
        if k > 1:
            for i in range(m):
                if not cov[i]:
                    p = w.take((T[i],), (chosen[0] + i) if chosen else cursor + i, max_dist=6 if chosen else 12, src=i)
                    if p is not None:
                        cov[i] = True
                        chosen.append(p)
        w.commit_line()
        u.covered = sum(cov)
        if chosen:
            chosen.sort()
            u.pos = chosen[len(chosen) // 2]
            cursor = chosen[-1] + k
        frac = u.covered / m
        u.status = "verified" if frac >= 0.9 else ("partial" if frac >= 0.5 else "missing")
    return w


# --------------------------------------------------------------------------
# Order: pairwise concordance on layout-determined pairs
# --------------------------------------------------------------------------
def order_pairs(units: List[Unit], extra_pairs: List[Tuple[int, int]] = ()) -> Tuple[int, List[Tuple[Unit, Unit]]]:
    idx = [i for i, u in enumerate(units) if u.kind == "text" and u.status in ("verified", "partial") and u.pos is not None]
    pairs, bad = 0, []
    last_in_flow: Dict[str, Unit] = {}
    for i in idx:
        u = units[i]
        if not u.flow:
            continue
        prev = last_in_flow.get(u.flow)
        if prev is not None:
            pairs += 1
            if u.pos < prev.pos:
                bad.append((prev, u))
        last_in_flow[u.flow] = u
    for a, b in extra_pairs:
        ua, ub = units[a], units[b]
        if ua.pos is None or ub.pos is None or ua.status not in ("verified", "partial") or ub.status not in ("verified", "partial"):
            continue
        pairs += 1
        if ub.pos < ua.pos:
            bad.append((ua, ub))
    return pairs, bad


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def _union(bbs):
    x0 = min(b[0] for b in bbs); y0 = min(b[1] for b in bbs)
    x1 = max(b[2] for b in bbs); y1 = max(b[3] for b in bbs)
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def build_report(units: List[Unit], w: Witness, extra_pairs, checks: List[dict], profile: str,
                 out_pages: Optional[List[Optional[int]]] = None) -> dict:
    kept_text = [u for u in units if u.kind == "text" and u.fate == "kept" and u.toks]
    kept_img = [u for u in units if u.kind == "image" and u.fate == "kept"]
    tok_total = sum(len(u.toks) for u in kept_text)
    tok_cov = sum(u.covered for u in kept_text)
    r_t = tok_cov / tok_total if tok_total else 1.0
    r_i = sum(u.covered for u in kept_img) / len(kept_img) if kept_img else 1.0
    n_out = len(w.toks)
    p = (sum(w.covered) / n_out) if n_out else 1.0
    pairs, bad = order_pairs(units, extra_pairs)
    o = 1 - len(bad) / pairs if pairs else 1.0
    score = 100.0 * r_t * r_i * o * p

    issues: List[dict] = []
    # missing / partial text, merged per page run
    run: List[Unit] = []

    def flush_run():
        if not run:
            return
        miss = sum(len(u.toks) - u.covered for u in run)
        sev = "high" if any(u.status == "missing" for u in run) else ("medium" if miss >= 2 else "low")
        issues.append({"kind": "missing-text" if sev == "high" else "partial-text", "severity": sev,
                       "page": run[0].page + 1, "bbox": _union([u.bbox for u in run]),
                       "detail": f"{miss} word(s) from the source not found in the output",
                       "excerpt": " / ".join(u.excerpt for u in run)[:220]})
        run.clear()
    for u in units:
        if u.kind == "text" and u.fate == "kept" and u.toks and u.covered < len(u.toks):
            if run and (run[-1].page != u.page):
                flush_run()
            run.append(u)
        else:
            if u.kind == "text" and u.fate == "kept":
                flush_run()
    flush_run()
    for u in kept_img:
        if u.status == "missing":
            issues.append({"kind": "missing-image", "severity": "high", "page": u.page + 1,
                           "bbox": [round(v, 1) for v in u.bbox], "detail": "embedded image not found in the output"})
    for a, b in bad:
        issues.append({"kind": "order", "severity": "medium", "page": b.page + 1, "bbox": [round(v, 1) for v in b.bbox],
                       "detail": "appears before text that precedes it in the source",
                       "excerpt": f"“{a.excerpt[:70]}” should come before “{b.excerpt[:70]}”"})
    # unwitnessed output (duplicated or invented text)
    i, toks = 0, w.toks
    while i < n_out:
        if not w.covered[i]:
            j = i
            while j < n_out and not w.covered[j]:
                j += 1
            if j - i >= 2:
                pg = out_pages[i] if out_pages and i < len(out_pages) else None
                issues.append({"kind": "extra-text", "severity": "medium", "page": pg, "bbox": None,
                               "detail": f"{j - i} output word(s) not accounted for by the source (duplicate or garbled)",
                               "excerpt": " ".join(toks[i:min(j, i + 25)])})
            i = j
        else:
            i += 1
    for c in checks:
        if not c["ok"]:
            issues.append({"kind": "checksum", "severity": c.get("severity", "high"), "page": c.get("page"),
                           "bbox": None, "detail": c["detail"]})

    dropped = Counter(u.reason for u in units if u.status == "dropped")
    pages: Dict[int, dict] = {}
    for u in units:
        pg = pages.setdefault(u.page + 1, {"kept": 0, "verified": 0, "dropped": 0, "scan": 0})
        if u.status == "dropped":
            pg["dropped"] += 1
        elif u.status == "scan":
            pg["scan"] += 1
        else:
            pg["kept"] += 1
            pg["verified"] += u.status == "verified"
    high = sum(1 for x in issues if x["severity"] == "high")
    med = sum(1 for x in issues if x["severity"] == "medium")
    # clean extractions produce exactly zero issues, so any finding moves the verdict
    verdict = "verified" if not issues else ("minor" if not high and not med else "review")
    if tok_total == 0 and not kept_img:
        verdict = "unverifiable"          # e.g. scanned pages: there is no text layer to check against
    order_rank = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: (order_rank[x["severity"]], x["page"] or 0))
    unwitnessed = [w.toks[i] for i in range(n_out) if not w.covered[i]]
    notes = [u.reason for u in units if u.kind == "note"]
    return {
        "notes": notes,
        "unwitnessed_sample": unwitnessed[:40],
        "profile": profile,
        "score": round(score, 2),
        "verdict": verdict,
        "metrics": {"token_recall": round(r_t, 4), "image_recall": round(r_i, 4), "order": round(o, 4),
                    "precision": round(p, 4), "kept_tokens": tok_total, "kept_images": len(kept_img),
                    "output_tokens": n_out, "order_pairs": pairs, "order_inversions": len(bad),
                    "units": len(units)},
        "checks": checks,
        "dropped": dict(dropped.most_common()),
        "pages": pages,
        "issues": issues,
        "scan_pages": sorted({u.page + 1 for u in units if u.status == "scan"}),
    }


# --------------------------------------------------------------------------
# Profile: exam papers
# --------------------------------------------------------------------------
def qpaper_out_stream(models) -> Tuple[OutStream, set]:
    out, qids = OutStream(), set()
    for m in models:
        for kind, obj, extra in m.items:
            if kind == "passage":
                out.runs(obj.passage)
            elif kind == "q":
                qids.add(obj.qid)
                out.page = obj.page
                out.runs(obj.body)
                for o in obj.options:
                    out.runs(o.content)
                for a in obj.answers:
                    out.text(a)
    return out, qids


def qpaper_source_units(pdf_path: str, paper, exported_qids: set) -> List[Unit]:
    """Walk the source exactly as laid out and give every line and image a fate."""
    from .qpaper_engine import (page_lines, QNUM_RE, QGROUP_RE, LABEL_RE, META_RE, NOISE_RE, OPTID_RE, META_KEYS)
    # a console label can wrap onto two lines without its colon ("Enable Mark as Answered … and" / "Clear Response :")
    meta_prefix = re.compile(r"^\s*(?:" + "|".join(re.escape(k) for k in sorted(META_KEYS, key=len, reverse=True)) + r")\b", re.I)
    secs = paper.sections
    exported = [any(q.qid in exported_qids for q in s.questions) for s in secs]
    check_nums = {(si, q.number) for si, s in enumerate(secs) for q in s.questions if q.subject_check}
    units: List[Unit] = []
    doc = fitz.open(pdf_path)
    try:
        sidx = 0 if (secs and secs[0].partial) else -1
        cur_q, flow = None, "pre"
        for pno, page in enumerate(doc):
            for line in page_lines(page, pno):
                plain = line.plain.strip()
                xs = [f["bbox"] for f in line.frags]
                bbox = (min(r.x0 for r in xs), line.y0, max(r.x1 for r in xs), line.y1)
                reason = ""
                is_title = plain and line.main_size >= 15.5 and line.bold and not line.images \
                    and not re.match(r"^Group\b", plain, re.I) and not META_RE.match(plain)
                if is_title:
                    sidx += 1
                    cur_q, flow = None, f"s{sidx}"
                    reason = "section title (used as document title)"
                elif QNUM_RE.search(plain):
                    cur_q = int(QNUM_RE.search(plain).group(1))
                    flow = f"q{cur_q}"
                    reason = "question header"
                elif QGROUP_RE.match(plain):
                    cur_q, flow = None, f"g{pno}-{int(line.y0)}"
                    reason = "comprehension header"
                elif LABEL_RE.match(plain) or META_RE.match(plain) or meta_prefix.match(plain) or NOISE_RE.match(plain) \
                        or re.match(r"^\s*(Options|Possible Answers)\s*:\s*$", plain, re.I) \
                        or re.match(r"^(Based on the above data, answer the given sub-?questions\.?|Sub\s*questions)$", plain, re.I):
                    reason = "exam console metadata"
                elif 0 <= sidx < len(secs) and not exported[sidx]:
                    reason = "section not exported"
                elif sidx < 0:
                    reason = "paper header"
                elif (sidx, cur_q) in check_nums:
                    reason = "subject confirmation question"
                text = plain
                first = next((f for f in line.texts if f["text"].strip()), None)
                if not reason and first is not None and OPTID_RE.match(first["text"]):
                    text = OPTID_RE.sub("", plain, count=1)        # the option id itself is explained
                units.append(Unit(page=pno, bbox=bbox, kind="text", toks=[] if reason else tokens(text),
                                  fate="dropped" if reason else "kept", reason=reason, flow=flow, excerpt=text[:120]))
                for f in line.images:
                    units.append(Unit(page=pno, bbox=tuple(f["bbox"]), kind="image", img=paper.xrefs.get(f["xref"]),
                                      fate="dropped" if reason else "kept", reason=reason))
    finally:
        doc.close()
    return units


def reset_units(units: List[Unit]):
    for u in units:
        u.covered, u.pos, u.status = 0, None, ""
        if u.reason == "no text" and u.fate == "kept":
            u.reason = ""


def audit_qpaper(pdf_path: str, paper, models, units: Optional[List[Unit]] = None) -> Tuple[dict, List[Unit]]:
    out, qids = qpaper_out_stream(models)
    if units is None:
        units = qpaper_source_units(pdf_path, paper, qids)
    else:
        reset_units(units)
    exported = [any(q.qid in qids for q in s.questions) for s in paper.sections]
    w = verify_units(units, out)
    return build_report(units, w, [], qpaper_checksums(paper, exported), "qpaper", out.pages), units


def qpaper_checksums(paper, exported) -> List[dict]:
    """Redundancy the exam export itself carries, re-derived and compared."""
    checks = []

    def add(name, ok, detail, page=None, severity="high"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "page": page, "severity": severity})

    for si, s in enumerate(paper.sections):
        if not exported[si]:
            continue
        qs = s.questions
        n_groups = len({q.group for q in qs if q.group})
        n_items = sum(1 for q in qs if not q.group) + n_groups
        dq = s.meta.get("Number of Questions")
        if dq and dq.isdigit():
            add("declared-question-count", int(dq) == n_items,
                f"{s.name}: section declares {dq} questions, extracted {n_items} "
                f"({sum(1 for q in qs if not q.group)} standalone + {n_groups} comprehension groups)")
        dm = s.meta.get("Section Marks")
        if dm:
            try:
                total = sum(q.marks or 0 for q in qs)
                add("declared-section-marks", abs(float(dm) - total) < 1e-6,
                    f"{s.name}: section declares {dm} marks, extracted questions add up to {total:g}")
            except ValueError:
                pass
        # question ids: consecutive, comprehension groups take one id each
        ids, seen_g = [], set()
        for q in qs:
            if q.group and q.group not in seen_g:
                seen_g.add(q.group)
                ids.append((int(q.group), f"group before Q{q.number}", q.page))
            ids.append((int(q.qid), f"Q{q.number}", q.page))
        gaps = [(a, b) for a, b in zip(ids, ids[1:]) if b[0] - a[0] != 1]
        add("question-id-sequence", not gaps,
            f"{s.name}: question IDs are consecutive" if not gaps else
            f"{s.name}: question-ID jump between {gaps[0][0][1]} and {gaps[0][1][1]} "
            f"({gaps[0][1][0] - gaps[0][0][0] - 1:+d} ids) — a question may be missing",
            page=gaps[0][1][2] if gaps else None)
        # option ids: consecutive in paper order; every SA question consumes exactly one id
        last, pend_sa, bad = None, 0, []
        for q in qs:
            if not q.options:
                pend_sa += 1 if q.qtype in ("SA", "LA") else 0
                continue
            first = int(q.options[0].oid)
            inner = [int(o.oid) for o in q.options]
            if any(b - a != 1 for a, b in zip(inner, inner[1:])):
                bad.append((q, "options inside the question are not consecutive"))
            if last is not None and first != last + 1 + pend_sa:
                bad.append((q, f"expected first option id …{str(last + 1 + pend_sa)[-4:]}, found …{str(first)[-4:]}"))
            last, pend_sa = inner[-1], 0
        add("option-id-sequence", not bad,
            f"{s.name}: option IDs are consecutive" if not bad else
            f"{s.name}: Q{bad[0][0].number}: {bad[0][1]} — an option may be missing", page=bad[0][0].page if bad else None)
        # answer cardinality
        wrong, wpage = [], None
        for q in qs:
            if q.subject_check:
                continue
            nc = sum(o.correct for o in q.options)
            bad_q = None
            if q.qtype == "MCQ" and (len(q.options) < 2 or nc != 1):
                bad_q = f"Q{q.number} (MCQ, {len(q.options)} options, {nc} marked correct)"
            elif q.qtype == "MSQ" and (len(q.options) < 2 or nc < 1):
                bad_q = f"Q{q.number} (MSQ, {nc} marked correct)"
            elif q.qtype in ("SA",) and not q.answers:
                bad_q = f"Q{q.number} (SA without an accepted answer)"
            if bad_q:
                wrong.append(bad_q)
                wpage = wpage or q.page
        add("answer-cardinality", not wrong,
            f"{s.name}: every question has a well-formed answer" if not wrong else
            f"{s.name}: " + "; ".join(wrong[:4]) + (" …" if len(wrong) > 4 else ""), page=wpage)
    return checks


# --------------------------------------------------------------------------
# Profile: general documents
# --------------------------------------------------------------------------
def general_out_stream(g) -> OutStream:
    out = OutStream()
    for b in g.blocks:
        if b.get("ocr") or b.get("scan"):
            continue
        t = b["type"]
        if t == "item" and b.get("ordered"):
            out.text(b.get("marker", ""))
        if "runs" in b:
            out.runs(b["runs"])
        elif t == "table":
            for row in b["rows"]:
                for c in row:
                    out.text(c)
        elif t == "code":
            out.text(b["text"])
        elif t == "figure":
            out.imgs[b["img"]] += 1
    return out


def general_source_units(pdf_path: str, g) -> Tuple[List[Unit], List[Tuple[int, int]]]:
    from .general import PAGENUM_RE, _norm_margin
    margin = set(g.margin_keys)
    regions = {int(k): [fitz.Rect(r[:4]) for r in v] for k, v in g.regions.items()}
    region_imgs = {int(k): [(fitz.Rect(r[:4]), r[4] if len(r) > 4 else None) for r in v] for k, v in g.regions.items()}
    scans = set(g.scan_pages)

    units: List[Unit] = []
    extra_pairs: List[Tuple[int, int]] = []
    prev_page_last: Optional[int] = None
    unmapped: set = set()
    doc = fitz.open(pdf_path)
    try:
        for pno, page in enumerate(doc):
            W, H = page.rect.width, page.rect.height
            if pno + 1 in scans:
                units.append(Unit(page=pno, bbox=tuple(page.rect), kind="text", fate="scan", reason="scanned page"))
                continue
            regs = regions.get(pno, [])
            d = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
            block_spans = []          # (first_unit_index, last_unit_index, rect) for column-stack pairs
            text_chars = 0
            for bi, b in enumerate(d["blocks"]):
                if b.get("type") != 0:
                    continue
                first_i = None
                lines = []
                for ln in b["lines"]:
                    text = "".join(s["text"] for s in ln["spans"])
                    if not text.strip():
                        continue
                    text_chars += len(text.strip())
                    lines.append((ln, text))
                # mirror the extractor's de-hyphenation so "separa-" + "ble" is one token
                toks_list = [tokens(t) for _, t in lines]
                for i in range(len(lines) - 1):
                    if re.search(r"[A-Za-z]-\s*$", lines[i][1]) and re.match(r"\s*[a-z]", lines[i + 1][1]) \
                            and toks_list[i] and toks_list[i + 1]:
                        toks_list[i][-1] += toks_list[i + 1].pop(0)
                for (ln, text), tk in zip(lines, toks_list):
                    if text.count("\ufffd") >= 3:
                        unmapped.add(pno + 1)
                    r = fitz.Rect(ln["bbox"])
                    c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                    reason = ""
                    if (r.y1 < H * 0.09 or r.y0 > H * 0.91) and (_norm_margin(text) in margin or PAGENUM_RE.match(text)):
                        reason = "running header / footer / page number"
                    elif abs(ln.get("dir", (1, 0))[1]) > 0.5:
                        reason = "rotated margin text"
                    elif any(rg.contains(c) for rg in regs):
                        reason = "inside a figure (kept as image)"
                    u = Unit(page=pno, bbox=tuple(r), kind="text", toks=[] if reason else tk,
                             fate="dropped" if reason else "kept", reason=reason,
                             flow=f"p{pno}b{bi}", excerpt=text.strip()[:120])
                    if not reason:
                        if first_i is None:
                            first_i = len(units)
                        last_i = len(units)
                    units.append(u)
                if first_i is not None:
                    block_spans.append((first_i, last_i, fitz.Rect(b["bbox"])))
            # column-stack invariant: a block directly below another in the same column is read after it
            for a in range(len(block_spans)):
                ra = block_spans[a][2]
                best, best_gap = None, None
                for bidx in range(len(block_spans)):
                    if bidx == a:
                        continue
                    rb = block_spans[bidx][2]
                    ov = min(ra.x1, rb.x1) - max(ra.x0, rb.x0)
                    if ov < 0.5 * min(ra.width, rb.width) or rb.y0 < ra.y1 - 2:
                        continue
                    gap = rb.y0 - ra.y1
                    if gap > 60:
                        continue
                    if best_gap is None or gap < best_gap:
                        best, best_gap = bidx, gap
                if best is not None:
                    extra_pairs.append((block_spans[a][1], block_spans[best][0]))
            # column and page invariants: left column before right column; page n before page n+1
            if block_spans:
                mid = W / 2
                full = [bs for bs in block_spans if bs[2].x0 < mid - 8 and bs[2].x1 > mid + 8]
                left = [bs for bs in block_spans if bs[2].x1 <= mid + 8]
                right = [bs for bs in block_spans if bs[2].x0 >= mid - 8]
                key = lambda bs: bs[2].y0
                two_col = False
                if len(left) >= 2 and len(right) >= 2:
                    ly0, ly1 = min(b[2].y0 for b in left), max(b[2].y1 for b in left)
                    ry0, ry1 = min(b[2].y0 for b in right), max(b[2].y1 for b in right)
                    # real columns run side by side: their vertical extents must largely overlap
                    two_col = min(ly1, ry1) - max(ly0, ry0) >= 0.5 * min(ly1 - ly0, ry1 - ry0) and not any(
                        f[2].y0 > ly0 and f[2].y1 < ly1 for f in full)
                if two_col:
                    extra_pairs.append((max(left, key=key)[1], min(right, key=key)[0]))
                else:
                    # single-column page: blocks that do not overlap vertically are read top to bottom
                    chain = sorted(block_spans, key=key)
                    for a_bs, b_bs in zip(chain, chain[1:]):
                        if b_bs[2].y0 >= a_bs[2].y1 - 2:
                            extra_pairs.append((a_bs[1], b_bs[0]))
                order_key = lambda bs: ((1 if (two_col and bs in right) else 0), bs[2].y0)
                first_bs, last_bs = min(block_spans, key=order_key), max(block_spans, key=order_key)
                if prev_page_last is not None:
                    extra_pairs.append((prev_page_last, first_bs[0]))
                prev_page_last = last_bs[1]
            # images
            for info in page.get_image_info(xrefs=True):
                r = fitz.Rect(info["bbox"]) & page.rect
                if r.is_empty or r.width < 6 or r.height < 6:
                    continue
                xref = info.get("xref") or 0
                c = fitz.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
                name = g.xrefs.get(xref)
                reason = ""
                if name is None:
                    if any(rg.contains(c) for rg in regs):
                        reason = "inside a figure (kept as image)"
                    elif r.get_area() > 0.85 * page.rect.get_area() and text_chars > 50:
                        reason = "page background"
                    elif r.width < 14 and r.height < 14:
                        reason = "decorative (tiny image)"
                units.append(Unit(page=pno, bbox=tuple(r), kind="image", img=name,
                                  fate="dropped" if reason else "kept", reason=reason))
            for r, name in region_imgs.get(pno, []):
                units.append(Unit(page=pno, bbox=tuple(r), kind="image", img=name,
                                  fate="kept" if name else "dropped", reason="" if name else "figure region"))
    finally:
        doc.close()
    if unmapped:
        units.append(Unit(page=min(unmapped) - 1, bbox=(0, 0, 0, 0), kind="note", fate="dropped",
                          reason=f"text without Unicode mapping on page(s) {', '.join(map(str, sorted(unmapped)))}"))
    return units, extra_pairs


def audit_general(pdf_path: str, g, cache: Optional[tuple] = None) -> Tuple[dict, List[Unit]]:
    out = general_out_stream(g)
    if cache is None:
        units, extra_pairs = general_source_units(pdf_path, g)
    else:
        units, extra_pairs = cache
        reset_units(units)
    w = verify_units(units, out)
    return build_report(units, w, extra_pairs, [], "general"), units


# --------------------------------------------------------------------------
# Visual overlay: the source PDF with every unit's fate drawn on it
# --------------------------------------------------------------------------
COLORS = {
    "verified": ((0.16, 0.62, 0.36), 0.10),
    "partial": ((0.93, 0.62, 0.10), 0.22),
    "missing": ((0.90, 0.18, 0.15), 0.28),
    "dropped": ((0.45, 0.45, 0.50), 0.13),
}


def write_overlay(pdf_path: str, units: List[Unit], report: dict, out_path: str):
    doc = fitz.open(pdf_path)
    try:
        for u in units:
            if u.status not in COLORS:
                continue
            page = doc[u.page]
            r = fitz.Rect(u.bbox) & page.rect
            if r.is_empty:
                continue
            col, op = COLORS[u.status]
            page.draw_rect(r + (-1, -0.5, 1, 0.5), color=col, fill=col, width=0.6 if u.status == "verified" else 1.2,
                           fill_opacity=op, stroke_opacity=0.6 if u.status == "verified" else 0.95, overlay=True)
        for it in report["issues"]:
            if it.get("page") and it.get("bbox") and it["kind"] == "order":
                page = doc[it["page"] - 1]
                r = fitz.Rect(it["bbox"])
                page.draw_rect(r + (-2, -1, 2, 1), color=(0.35, 0.3, 0.9), width=1.6, dashes="[3 2] 0", overlay=True)
        # legend on the first page
        page = doc[0]
        y = 6
        x = 8
        legend = [("verified", "kept and found"), ("partial", "partly found"), ("missing", "not found"),
                  ("dropped", "removed on purpose")]
        page.draw_rect(fitz.Rect(4, 3, 470, 17), color=None, fill=(1, 1, 1), fill_opacity=0.9, overlay=True)
        page.insert_text((x, y + 8.5), f"Paperlift fidelity audit  {report['score']:.1f}  ·", fontsize=7.5, color=(0.1, 0.1, 0.1))
        x += 150
        for st, label in legend:
            col, _ = COLORS[st]
            page.draw_rect(fitz.Rect(x, y + 1, x + 9, y + 9), color=col, fill=col, fill_opacity=0.5, overlay=True)
            page.insert_text((x + 12, y + 8.5), label, fontsize=7.5, color=(0.1, 0.1, 0.1))
            x += 18 + fitz.get_text_length(label, fontsize=7.5) + 8
        doc.save(out_path, garbage=3, deflate=True)
    finally:
        doc.close()


def save_audit(report: dict, units: List[Unit], pdf_path: str, out_dir: str, stem: str, overlay: bool = True) -> dict:
    path = os.path.join(out_dir, "_audit.json")
    json.dump(report, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    files = {"audit_json": path}
    if overlay:
        op = os.path.join(out_dir, stem + "_audit.pdf")
        try:
            write_overlay(pdf_path, units, report, op)
            files["audit_pdf"] = op
        except Exception as e:  # overlay is a nicety; never fail the conversion
            report.setdefault("notes", []).append(f"overlay not written: {e}")
    return files


def summary_of(report: dict) -> dict:
    return {"score": report["score"], "verdict": report["verdict"], "metrics": report["metrics"],
            "issues": len(report["issues"]),
            "high": sum(1 for i in report["issues"] if i["severity"] == "high"),
            "checks_failed": sum(1 for c in report["checks"] if not c["ok"]),
            "checks": len(report["checks"])}
