import importlib
import json
import threading
import urllib.request

from paperlift.limits import check_pdf, clean_limits


def test_page_limit(pdfs):
    assert check_pdf(pdfs["article"], {"max_pages": 1, "max_mb": 50}) is not None
    assert check_pdf(pdfs["article"], {"max_pages": 50, "max_mb": 50}) is None


def test_not_a_pdf(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_text("hello")
    assert "not a PDF" in check_pdf(str(p), clean_limits({}))


def test_clean_limits():
    assert clean_limits({"max_files": "3", "bogus": 1, "max_mb": -5}) == {"max_files": 3, "max_mb": 0, "max_pages": 50}


def test_server_smoke(tmp_path, monkeypatch, pdfs):
    monkeypatch.setenv("PAPERLIFT_HOME", str(tmp_path))
    import paperlift.server as server
    server = importlib.reload(server)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["files"] == [] and st["settings"]["limits"]["max_files"] == 10
        data = open(pdfs["qpaper"], "rb").read()
        req = urllib.request.Request(base + "/api/upload?name=demo.pdf", data=data, method="POST")
        assert json.load(urllib.request.urlopen(req))["ok"]
        assert urllib.request.urlopen(base + "/").status == 200
        assert urllib.request.urlopen(base + "/favicon.svg").status == 200
    finally:
        srv.shutdown()
