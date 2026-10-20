import json
import os

from paperlift.pipeline import convert, detect_profile


def test_detects_exam_paper(pdfs):
    assert detect_profile(pdfs["qpaper"])["profile"] == "qpaper"


def test_questions_and_answer_key(pdfs, out):
    s = convert(pdfs["qpaper"], out, {})
    assert s["profile"] == "qpaper"
    o = s["outputs"][0]
    assert o["questions"] == 3                      # subject-check question dropped
    md = open(o["files"]["md"], encoding="utf-8").read()
    assert "THIS IS QUESTION PAPER" not in md
    assert "## Answer Key" in md
    data = json.load(open(o["files"]["json"], encoding="utf-8"))
    answers = [q["answer"] for q in data["questions"]]
    assert answers == ["B", "A, B", "4"]
    assert [q["type"] for q in data["questions"]] == ["MCQ", "MSQ", "SA"]


def test_all_formats_written(pdfs, out):
    s = convert(pdfs["qpaper"], out, {})
    for k in ("md", "docx", "pdf", "json", "html", "md_zip"):
        assert os.path.getsize(s["outputs"][0]["files"][k]) > 0
