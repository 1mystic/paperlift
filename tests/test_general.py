import json

from paperlift.pipeline import convert, detect_profile


def _blocks(s):
    return json.load(open(s["outputs"][0]["files"]["json"], encoding="utf-8"))["blocks"]


def test_detects_general(pdfs):
    assert detect_profile(pdfs["article"])["profile"] == "general"


def test_structure(pdfs, out):
    s = convert(pdfs["article"], out, {})
    assert s["profile"] == "general"
    b = _blocks(s)
    kinds = [x["type"] for x in b]
    assert kinds.count("heading") >= 5
    assert "code" in kinds and "figure" in kinds
    table = next(x for x in b if x["type"] == "table")
    assert table["rows"][0] == ["Optimiser", "Learning rate", "Momentum"] and len(table["rows"]) == 4
    items = [x for x in b if x["type"] == "item"]
    assert len(items) == 6 and sum(1 for x in items if x["ordered"]) == 3


def test_reading_order_two_columns_and_margins(pdfs, out):
    s = convert(pdfs["article"], out, {})
    md = open(s["outputs"][0]["files"]["md"], encoding="utf-8").read()
    assert md.index("The update rule") < md.index("Momentum") < md.index("Batch size") < md.index("A worked example")
    assert "ML Notes" not in md                     # running header removed
    assert "Page 1 of" not in md                    # page numbers removed
    assert md.startswith("# Gradient Descent in Practice")


def test_scanned_page_is_kept(pdfs, out):
    s = convert(pdfs["scanned"], out, {"ocr": "off"})
    b = _blocks(s)
    assert b and b[0]["type"] == "figure" and b[0].get("scan")
    assert any("scanned" in w for w in s["warnings"])


def test_forced_mode(pdfs, out):
    s = convert(pdfs["qpaper"], out, {"mode": "general"})
    assert s["profile"] == "general"
