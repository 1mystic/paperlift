"""
Fault-injection benchmark for the fidelity audit.

For each document: extract once, audit the clean result (false-positive check),
then apply a mutation operator to a copy of the extraction many times (seeded)
and audit each corrupted copy. A trial counts as

  detected  if the audit reports anything: score < 100, any issue, or a failed checksum
  alerted   if the verdict is not "verified" (what a user is shown by default)
  located   if a reported issue points at the page the corruption came from (exam papers)

Operators marked [blind] change *structure or answer marks* but keep every word and
image, so a content-conservation audit is not expected to see them: they are included
to measure the method's limits honestly.

    python bench/fault_injection.py                  # synthetic fixtures only (reproducible)
    python bench/fault_injection.py extra1.pdf ...   # add your own documents
"""
from __future__ import annotations

import copy
import json
import os
import random
import sys
import tempfile
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))

import pymupdf as fitz  # noqa: E402

from paperlift import audit as A  # noqa: E402
from paperlift.general import extract_general  # noqa: E402
from paperlift.pipeline import detect_profile  # noqa: E402
from paperlift.qpaper_engine import extract as q_extract  # noqa: E402
from paperlift.qpaper_render import build_models, DEFAULT_OPTIONS  # noqa: E402

TRIALS = int(os.environ.get("TRIALS", 30))
SWAPS = {"detected": 0, "missed": 0, "identical": 0, "unverifiable": 0}


# ---------------------------------------------------------------- helpers
def text_runs(runs):
    return [r for r in runs if "t" in r and len(A.tokens(r["t"])) >= 1]


def cut_words(runs, rng, n=None):
    """Delete 1-3 consecutive words from one text run; returns False if impossible."""
    cands = [r for r in runs if "t" in r and len(r["t"].split()) >= 2]
    if not cands:
        return False
    r = rng.choice(cands)
    words = r["t"].split(" ")
    idx = [i for i, w in enumerate(words) if A.tokens(w)]
    if len(idx) < 2:
        return False
    n = n or rng.randint(1, min(3, len(idx) - 1))
    s = rng.randrange(0, len(idx) - n + 1)
    kill = set(idx[s:s + n])
    r["t"] = " ".join(w for i, w in enumerate(words) if i not in kill)
    return True


def substitute_word(runs, rng):
    cands = [r for r in runs if "t" in r and [w for w in r["t"].split(" ") if len(A.tokens(w)) == 1 and len(w) > 3]]
    if not cands:
        return False
    r = rng.choice(cands)
    words = r["t"].split(" ")
    idx = [i for i, w in enumerate(words) if len(A.tokens(w)) == 1 and len(w) > 3]
    i = rng.choice(idx)
    w = words[i]
    j = rng.randrange(1, len(w) - 1)          # one-character OCR-style error
    c = "e" if w[j] != "e" else "a"
    words[i] = w[:j] + c + w[j + 1:]
    r["t"] = " ".join(words)
    return True


def drop_image(runs, rng):
    idx = [i for i, r in enumerate(runs) if "img" in r or "block" in r]
    if not idx:
        return False
    runs.pop(rng.choice(idx))
    return True


# ---------------------------------------------------------------- exam-paper operators
def q_questions(paper, models):
    qs = [obj for m in models for k, obj, _ in m.items if k == "q"]
    return qs


def op_q_drop_option(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if len(q.options) >= 2]
    if not qs:
        return None
    q = rng.choice(qs)
    q.options.pop(rng.randrange(len(q.options)))
    return q.page


def op_q_drop_question(paper, models, rng):
    qs = q_questions(paper, models)
    q = rng.choice(qs)
    for m in models:
        m.items = [it for it in m.items if not (it[0] == "q" and it[1] is q)]
    for s in paper.sections:
        if q in s.questions:
            s.questions.remove(q)
    return q.page


def op_q_cut_words(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if any(len(r.get("t", "").split()) >= 2 for r in q.body)]
    q = rng.choice(qs)
    return q.page if cut_words(q.body, rng) else None


def op_q_cut_option_text(paper, models, rng):
    opts = [(q, o) for q in q_questions(paper, models) for o in q.options if any(len(r.get("t", "").split()) >= 2 for r in o.content)]
    if not opts:
        return None
    q, o = rng.choice(opts)
    return q.page if cut_words(o.content, rng) else None


def op_q_drop_image(paper, models, rng):
    holders = [(q, q.body) for q in q_questions(paper, models)] + \
              [(q, o.content) for q in q_questions(paper, models) for o in q.options]
    holders = [(q, h) for q, h in holders if any("img" in r or "block" in r for r in h)]
    if not holders:
        return None
    q, h = rng.choice(holders)
    return q.page if drop_image(h, rng) else None


def op_q_swap_options(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if len(q.options) >= 2]
    q = rng.choice(qs)
    i = rng.randrange(len(q.options) - 1)
    q.options[i], q.options[i + 1] = q.options[i + 1], q.options[i]
    return q.page


def op_q_duplicate_text(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if text_runs(q.body)]
    q = rng.choice(qs)
    r = rng.choice(text_runs(q.body))
    q.body.append({"t": " " + r["t"]})
    return q.page


def op_q_substitute(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if text_runs(q.body)]
    q = rng.choice(qs)
    return q.page if substitute_word(q.body, rng) else None


def op_q_unmark_answer(paper, models, rng):
    qs = [q for q in q_questions(paper, models) if q.qtype == "MCQ" and any(o.correct for o in q.options)]
    if not qs:
        return None
    q = rng.choice(qs)
    for o in q.options:
        o.correct = False
    return q.page


def op_q_move_answer(paper, models, rng):  # [blind]
    qs = [q for q in q_questions(paper, models) if q.qtype == "MCQ" and len(q.options) >= 2 and any(o.correct for o in q.options)]
    if not qs:
        return None
    q = rng.choice(qs)
    i = next(i for i, o in enumerate(q.options) if o.correct)
    j = rng.choice([k for k in range(len(q.options)) if k != i])
    q.options[i].correct, q.options[j].correct = False, True
    return q.page


Q_OPS = {
    "drop option": op_q_drop_option,
    "drop question": op_q_drop_question,
    "delete 1-3 words (stem)": op_q_cut_words,
    "delete 1-3 words (option)": op_q_cut_option_text,
    "drop inline/figure image": op_q_drop_image,
    "swap adjacent options": op_q_swap_options,
    "duplicate a sentence": op_q_duplicate_text,
    "1-character substitution": op_q_substitute,
    "answer mark lost": op_q_unmark_answer,
    "[blind] answer mark moved": op_q_move_answer,
}


# ---------------------------------------------------------------- document operators
def g_text_blocks(g):
    return [b for b in g.blocks if "runs" in b and A.tokens("".join(r.get("t", "") for r in b["runs"])) and not b.get("ocr")]


def op_g_drop_block(g, rng):
    b = rng.choice(g_text_blocks(g))
    g.blocks.remove(b)
    return True


def op_g_cut_words(g, rng):
    return cut_words(rng.choice(g_text_blocks(g))["runs"], rng)


def op_g_swap_blocks(g, rng):
    idx = [i for i in range(len(g.blocks) - 1) if "runs" in g.blocks[i] and "runs" in g.blocks[i + 1]]
    if not idx:
        return False
    i = rng.choice(idx)
    g.blocks[i], g.blocks[i + 1] = g.blocks[i + 1], g.blocks[i]
    return True


def op_g_drop_figure(g, rng):
    idx = [i for i, b in enumerate(g.blocks) if b["type"] == "figure" and not b.get("scan")]
    if not idx:
        return False
    g.blocks.pop(rng.choice(idx))
    return True


def op_g_duplicate(g, rng):
    b = rng.choice(g_text_blocks(g))
    g.blocks.insert(g.blocks.index(b) + 1, copy.deepcopy(b))
    return True


def op_g_substitute(g, rng):
    return substitute_word(rng.choice(g_text_blocks(g))["runs"], rng)


def op_g_drop_table_row(g, rng):
    t = [b for b in g.blocks if b["type"] == "table" and len(b["rows"]) >= 2]
    if not t:
        return False
    b = rng.choice(t)
    b["rows"].pop(rng.randrange(len(b["rows"])))
    return True


def op_g_heading_to_para(g, rng):  # [blind]
    h = [b for b in g.blocks if b["type"] == "heading"]
    if not h:
        return False
    rng.choice(h)["type"] = "para"
    return True


G_OPS = {
    "drop block": op_g_drop_block,
    "delete 1-3 words": op_g_cut_words,
    "swap adjacent blocks": op_g_swap_blocks,
    "drop figure": op_g_drop_figure,
    "duplicate block": op_g_duplicate,
    "1-character substitution": op_g_substitute,
    "drop table row": op_g_drop_table_row,
    "[blind] heading -> paragraph": op_g_heading_to_para,
}


# ---------------------------------------------------------------- runner
def judged(rep):
    detected = rep["score"] < 100 or bool(rep["issues"])
    alerted = rep["verdict"] != "verified"
    return detected, alerted


def run_qpaper(pdf, img_dir, rng, results, baseline):
    paper = q_extract(pdf, img_dir)
    models = build_models(paper, DEFAULT_OPTIONS)
    rep, units = A.audit_qpaper(pdf, paper, models)
    baseline.append((os.path.basename(pdf), "exam paper", rep["score"], rep["verdict"], len(rep["issues"])))
    for name, op in Q_OPS.items():
        for _ in range(TRIALS):
            p2, m2 = copy.deepcopy((paper, models))
            page = op(p2, m2, rng)
            if page is None:
                continue
            r2, _ = A.audit_qpaper(pdf, p2, m2, units=units)
            d, a = judged(r2)
            loc = any(i.get("page") in (page, page + 1) for i in r2["issues"])  # a question may continue on the next page
            st = results[("exam paper", name)]
            st["n"] += 1; st["det"] += d; st["alert"] += a; st["loc"] += loc and d


def run_general(pdf, img_dir, rng, results, baseline):
    g = extract_general(pdf, img_dir, {"ocr": "off"})
    cache = A.general_source_units(pdf, g)
    rep, _ = A.audit_general(pdf, g, cache=cache)
    baseline.append((os.path.basename(pdf), "document", rep["score"], rep["verdict"], len(rep["issues"])))
    # exhaustive adjacent swaps, classified (no sampling noise)
    from paperlift.qpaper_engine import runs_plain
    for i in range(len(g.blocks) - 1):
        a, b = g.blocks[i], g.blocks[i + 1]
        if "runs" not in a or "runs" not in b:
            continue
        ta, tb = A.tokens(runs_plain(a["runs"])), A.tokens(runs_plain(b["runs"]))
        g2 = copy.deepcopy(g)
        g2.blocks[i], g2.blocks[i + 1] = b, a
        r2, _ = A.audit_general(pdf, g2, cache=cache)
        key = "detected" if r2["issues"] else ("identical" if ta == tb else ("unverifiable" if not ta or not tb else "missed"))
        SWAPS[key] += 1
    for name, op in G_OPS.items():
        for _ in range(TRIALS):
            g2 = copy.deepcopy(g)
            if not op(g2, rng):
                continue
            r2, _ = A.audit_general(pdf, g2, cache=cache)
            d, a = judged(r2)
            st = results[("document", name)]
            st["n"] += 1; st["det"] += d; st["alert"] += a


def main(extra):
    import fixtures
    tmp = tempfile.mkdtemp(prefix="pl-bench-")
    fx = fixtures.make_all(os.path.join(tmp, "fx"))
    docs = [fx["qpaper"], fx["article"]] + list(extra)
    rng = random.Random(1234)
    results = defaultdict(lambda: {"n": 0, "det": 0, "alert": 0, "loc": 0})
    baseline = []
    t0 = time.time()
    for i, pdf in enumerate(docs):
        prof = detect_profile(pdf)["profile"]
        img = os.path.join(tmp, f"img{i}")
        (run_qpaper if prof == "qpaper" else run_general)(pdf, img, rng, results, baseline)
        print(f"  {os.path.basename(pdf)} ({prof}) done", file=sys.stderr)
    print(f"\nFidelity-audit fault injection — {len(docs)} documents, {TRIALS} trials per operator per document, "
          f"{time.time() - t0:.0f}s\n")
    print("Clean extractions (false-positive check)\n")
    print("| Document | Profile | Score | Verdict | Issues |\n|---|---|--:|---|--:|")
    for b in baseline:
        print(f"| {b[0]} | {b[1]} | {b[2]:.1f} | {b[3]} | {b[4]} |")
    print("\nInjected faults\n")
    print("| Profile | Fault | Trials | Detected | Alerted | Located on page |\n|---|---|--:|--:|--:|--:|")
    for (prof, name), st in results.items():
        n = st["n"] or 1
        loc = f"{st['loc'] / max(1, st['det']):.0%}" if prof == "exam paper" else "–"
        print(f"| {prof} | {name} | {st['n']} | {st['det'] / n:.0%} | {st['alert'] / n:.0%} | {loc} |")
    det = SWAPS["detected"]
    eligible = det + SWAPS["missed"]
    print(f"\nExhaustive adjacent-block swaps (documents): {sum(SWAPS.values())} swaps — "
          f"{det}/{eligible} detectable swaps detected ({det / max(1, eligible):.1%}); "
          f"{SWAPS['identical']} swapped identical text, {SWAPS['unverifiable']} involved text without Unicode mapping")
    json.dump({"swaps": SWAPS, "baseline": baseline, "results": {f"{k[0]}|{k[1]}": v for k, v in results.items()}},
              open(os.path.join(HERE, "last_run.json"), "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
