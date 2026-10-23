"""The fidelity audit: clean extractions verify; injected faults are caught."""
import copy
import os

from paperlift import audit as A
from paperlift.general import extract_general
from paperlift.pipeline import convert
from paperlift.qpaper_engine import extract as q_extract
from paperlift.qpaper_render import DEFAULT_OPTIONS, build_models


def _q(pdf, tmp):
    paper = q_extract(pdf, str(tmp / "img"))
    return paper, build_models(paper, DEFAULT_OPTIONS)


def _questions(models):
    return [o for m in models for k, o, _ in m.items if k == "q"]


def test_clean_exam_paper_is_verified_with_checksums(pdfs, tmp_path):
    paper, models = _q(pdfs["qpaper"], tmp_path)
    rep, _ = A.audit_qpaper(pdfs["qpaper"], paper, models)
    assert rep["score"] == 100 and rep["verdict"] == "verified" and rep["issues"] == []
    names = {c["name"] for c in rep["checks"]}
    assert {"declared-question-count", "declared-section-marks", "question-id-sequence",
            "option-id-sequence", "answer-cardinality"} <= names
    assert all(c["ok"] for c in rep["checks"])
    # conservation: every kept source word maps to exactly one output word
    assert rep["metrics"]["kept_tokens"] == rep["metrics"]["output_tokens"]
    assert rep["dropped"]["exam console metadata"] > 0


def test_clean_document_is_verified(pdfs, tmp_path):
    g = extract_general(pdfs["article"], str(tmp_path / "img"), {})
    rep, _ = A.audit_general(pdfs["article"], g)
    assert rep["verdict"] == "verified" and rep["score"] == 100
    assert rep["metrics"]["order_pairs"] > 20
    assert rep["dropped"].get("running header / footer / page number", 0) >= 2


def test_dropped_option_breaks_checksum(pdfs, tmp_path):
    paper, models = _q(pdfs["qpaper"], tmp_path)
    q = next(q for q in _questions(models) if len(q.options) == 4)
    q.options.pop(1)
    rep, _ = A.audit_qpaper(pdfs["qpaper"], paper, models)
    assert rep["verdict"] == "review"
    assert not next(c for c in rep["checks"] if c["name"] == "option-id-sequence")["ok"]
    assert any(i["kind"] == "missing-text" for i in rep["issues"])


def test_dropped_question_breaks_declared_count(pdfs, tmp_path):
    paper, models = _q(pdfs["qpaper"], tmp_path)
    q = _questions(models)[-1]
    for m in models:
        m.items = [it for it in m.items if it[1] is not q]
    paper.sections[0].questions.remove(q)
    rep, _ = A.audit_qpaper(pdfs["qpaper"], paper, models)
    failed = {c["name"] for c in rep["checks"] if not c["ok"]}
    assert {"declared-question-count", "declared-section-marks"} <= failed


def test_lost_answer_mark(pdfs, tmp_path):
    paper, models = _q(pdfs["qpaper"], tmp_path)
    q = next(q for q in _questions(models) if q.qtype == "MCQ")
    for o in q.options:
        o.correct = False
    rep, _ = A.audit_qpaper(pdfs["qpaper"], paper, models)
    assert not next(c for c in rep["checks"] if c["name"] == "answer-cardinality")["ok"]


def _doc(pdfs, tmp_path):
    g = extract_general(pdfs["article"], str(tmp_path / "img"), {})
    return g, A.general_source_units(pdfs["article"], g)


def test_document_faults(pdfs, tmp_path):
    g, cache = _doc(pdfs, tmp_path)
    paras = [i for i, b in enumerate(g.blocks) if b["type"] == "para"]

    g1 = copy.deepcopy(g)                                   # words deleted
    r = g1.blocks[paras[1]]["runs"][0]
    r["t"] = r["t"].replace("the parameters move a small distance", "")
    rep, _ = A.audit_general(pdfs["article"], g1, cache=cache)
    assert any(i["kind"] in ("missing-text", "partial-text") for i in rep["issues"])

    g2 = copy.deepcopy(g)                                   # adjacent paragraphs swapped
    i = next(i for i in paras if i + 1 in paras)
    g2.blocks[i], g2.blocks[i + 1] = g2.blocks[i + 1], g2.blocks[i]
    rep, _ = A.audit_general(pdfs["article"], g2, cache=cache)
    assert any(x["kind"] == "order" for x in rep["issues"])

    g3 = copy.deepcopy(g)                                   # paragraph duplicated
    g3.blocks.insert(paras[0] + 1, copy.deepcopy(g3.blocks[paras[0]]))
    rep, _ = A.audit_general(pdfs["article"], g3, cache=cache)
    assert any(x["kind"] == "extra-text" for x in rep["issues"])

    g4 = copy.deepcopy(g)                                   # rendered vector figure dropped
    g4.blocks = [b for b in g4.blocks if b["type"] != "figure"]
    rep, _ = A.audit_general(pdfs["article"], g4, cache=cache)
    assert any(x["kind"] == "missing-image" for x in rep["issues"])


def test_structure_only_change_is_out_of_scope(pdfs, tmp_path):
    """Documented limit: content conservation cannot see a heading demoted to a paragraph."""
    g, cache = _doc(pdfs, tmp_path)
    next(b for b in g.blocks if b["type"] == "heading")["type"] = "para"
    rep, _ = A.audit_general(pdfs["article"], g, cache=cache)
    assert rep["verdict"] == "verified"


def test_convert_writes_audit_and_overlay(pdfs, out):
    s = convert(pdfs["qpaper"], out, {})
    assert s["audit"]["verdict"] == "verified"
    assert os.path.exists(os.path.join(s["out_dir"], "_audit.json"))
    assert os.path.getsize(s["outputs"][0]["files"]["audit_pdf"]) > 0


def test_scanned_is_unverifiable(pdfs, out):
    s = convert(pdfs["scanned"], out, {"ocr": "off"})
    assert s["audit"]["verdict"] == "unverifiable"
