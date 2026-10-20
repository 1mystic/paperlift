"""
Paperlift local server — Python standard library only (no web framework).

One background worker extracts files strictly one at a time, so memory use
stays flat no matter how many files are queued. Limits (files per batch,
MB per file, pages per file) are enforced at upload and again before work.
"""
from __future__ import annotations

import gc
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__ as VERSION
from .limits import DEFAULT_LIMITS, check_pdf, clean_limits

PKG = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(PKG, "static")


def data_home() -> str:
    """Repo checkout -> the repo folder. pip install -> ~/Paperlift. Override: PAPERLIFT_HOME."""
    env = os.environ.get("PAPERLIFT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    repo = os.path.dirname(PKG)
    if os.path.exists(os.path.join(repo, "paperlift.sh")) or os.path.exists(os.path.join(repo, "pyproject.toml")):
        return repo
    return os.path.join(os.path.expanduser("~"), "Paperlift")


ROOT = data_home()
INBOX = os.path.join(ROOT, "inbox")
OUTPUT = os.path.join(ROOT, "output")
SETTINGS = os.path.join(ROOT, "settings.json")

DEFAULT_SETTINGS = {
    "formats": ["md", "docx", "pdf", "json"],
    "mode": "auto",                       # auto | qpaper | general
    "split": True, "renumber": True, "marks": True, "qtype_hint": True, "sections": "auto",
    "tables": True, "vector_figures": True, "page_markers": False, "ocr": "auto",
    "limits": dict(DEFAULT_LIMITS),
}


def load_settings() -> dict:
    try:
        s = dict(DEFAULT_SETTINGS, **json.load(open(SETTINGS)))
    except Exception:
        s = dict(DEFAULT_SETTINGS)
    s["limits"] = clean_limits(s.get("limits"))
    return s


def safe_name(name: str) -> str:
    name = os.path.basename(urllib.parse.unquote(name or ""))
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .") or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:150]


def stem_of(name: str) -> str:
    return os.path.splitext(name)[0]


# --------------------------------------------------------------------------
# Job queue — one worker thread, one file at a time
# --------------------------------------------------------------------------
class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.q: "queue.Queue[str]" = queue.Queue()
        self.state = {}
        self.opts = {}
        threading.Thread(target=self._worker, daemon=True, name="paperlift-worker").start()

    def enqueue(self, name: str, opts: dict) -> bool:
        with self.lock:
            if self.state.get(name, {}).get("status") in ("queued", "processing"):
                return False
            self.opts[name] = opts
            self.state[name] = {"status": "queued", "queued_at": time.time()}
        self.q.put(name)
        return True

    def get(self, name):
        with self.lock:
            return dict(self.state.get(name, {}))

    def pending(self) -> int:
        with self.lock:
            return sum(1 for s in self.state.values() if s.get("status") in ("queued", "processing"))

    def cancel(self, name):
        with self.lock:
            if self.state.get(name, {}).get("status") == "queued":
                self.state[name] = {"status": "cancelled"}

    def _progress(self, name):
        def cb(cur, total):
            with self.lock:
                if name in self.state:
                    self.state[name]["progress"] = [cur, total]
        return cb

    def _worker(self):
        from .pipeline import convert
        try:
            import pymupdf as fitz
        except Exception:
            fitz = None
        while True:
            name = self.q.get()
            with self.lock:
                if self.state.get(name, {}).get("status") != "queued":
                    continue
                self.state[name].update(status="processing", started=time.time(), progress=[0, 0])
                opts = dict(self.opts.get(name, {}))
            try:
                src = os.path.join(INBOX, name)
                limits = clean_limits(opts.pop("limits", None))
                problem = check_pdf(src, limits)
                if problem:
                    raise ValueError(problem)
                fmts = tuple(opts.pop("formats", DEFAULT_SETTINGS["formats"]))
                summary = convert(src, OUTPUT, opts, formats=fmts, progress=self._progress(name))
                json.dump(dict(opts, formats=list(fmts), mode=summary.get("mode", "auto")),
                          open(os.path.join(OUTPUT, stem_of(name), "_options.json"), "w"))
                with self.lock:
                    self.state[name].update(status="done", finished=time.time())
            except Exception as e:  # the queue survives any single bad file
                if not isinstance(e, ValueError):
                    traceback.print_exc()
                with self.lock:
                    self.state[name].update(status="error", error=str(e) if isinstance(e, ValueError)
                                            else f"{type(e).__name__}: {e}", finished=time.time())
            finally:
                if fitz is not None:
                    try:
                        fitz.TOOLS.store_shrink(100)       # drop MuPDF caches between files
                    except Exception:
                        pass
                gc.collect()
                time.sleep(0.05)


JOBS = Jobs()


def file_list():
    os.makedirs(INBOX, exist_ok=True)
    items = []
    names = [n for n in os.listdir(INBOX) if n.lower().endswith(".pdf") and not n.startswith(".")]
    for name in sorted(names, key=lambda n: -os.path.getmtime(os.path.join(INBOX, n))):
        stem = stem_of(name)
        summary = None
        sp = os.path.join(OUTPUT, stem, "_summary.json")
        if os.path.exists(sp):
            try:
                summary = json.load(open(sp))
            except Exception:
                summary = None
        st = JOBS.get(name)
        status = st.get("status") or ("done" if summary else "new")
        if status == "cancelled":
            status = "done" if summary else "new"
        rec = {"name": name, "stem": stem, "status": status, "error": st.get("error"),
               "progress": st.get("progress"), "size": os.path.getsize(os.path.join(INBOX, name))}
        if summary and status != "error":
            rel = lambda p: "/out/" + urllib.parse.quote(os.path.relpath(p, OUTPUT))
            rec.update(
                profile=summary.get("profile", "qpaper"),
                outputs=[{"title": o["title"], "slug": o["slug"], "questions": o.get("questions", 0),
                          "stats": o.get("stats"), "files": {k: rel(v) for k, v in o["files"].items()}}
                         for o in summary["outputs"]],
                sections=summary.get("sections"), warnings=summary.get("warnings"),
                guess=summary.get("guess"), meta=summary.get("meta"), ocr_pages=summary.get("ocr_pages"))
            op = os.path.join(OUTPUT, stem, "_options.json")
            if os.path.exists(op):
                try:
                    rec["options"] = json.load(open(op))
                except Exception:
                    pass
        items.append(rec)
    return items


def open_folder(path):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def ocr_status() -> bool:
    from .general import ocr_available
    return ocr_available()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Paperlift/" + VERSION

    def log_message(self, fmt, *args):
        if os.environ.get("PAPERLIFT_DEBUG"):
            super().log_message(fmt, *args)

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _drain(self, n):
        while n > 0:
            chunk = self.rfile.read(min(256 * 1024, n))
            if not chunk:
                break
            n -= len(chunk)

    def _file(self, path, download_name=None):
        if not path or not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".md"):
            ctype = "text/markdown; charset=utf-8"
        elif path.endswith(".svg"):
            ctype = "image/svg+xml"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 64 * 1024)

    @staticmethod
    def _under(base, rel):
        p = os.path.realpath(os.path.join(base, rel))
        return p if p.startswith(os.path.realpath(base) + os.sep) else None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = urllib.parse.unquote(u.path)
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if path in ("/favicon.ico", "/favicon.svg"):
            return self._file(os.path.join(STATIC, "favicon.svg"))
        if path.startswith("/static/"):
            return self._file(self._under(STATIC, path[len("/static/"):]))
        if path == "/api/state":
            return self._json({"files": file_list(), "pending": JOBS.pending(), "settings": load_settings(),
                               "version": VERSION, "root": ROOT, "ocr": ocr_status()})
        if path.startswith("/out/"):
            p = self._under(OUTPUT, path[5:])
            return self._file(p, os.path.basename(p) if p and qs.get("dl") else None)
        if path == "/api/zip":
            stem = qs.get("stem", [""])[0]
            d = self._under(OUTPUT, stem) if stem else None
            if not d or not os.path.isdir(d):
                return self._json({"error": "not found"}, 404)
            zp = os.path.join(OUTPUT, f".{stem}_all.zip")
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                for base, _, files in os.walk(d):
                    for f in files:
                        if f.startswith("_") or f.endswith(("_markdown.zip", ".html")):
                            continue
                        fp = os.path.join(base, f)
                        z.write(fp, os.path.join(stem, os.path.relpath(fp, d)))
            self._file(zp, f"{stem}.zip")
            os.remove(zp)
            return
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/api/upload":
            limits = load_settings()["limits"]
            name = safe_name(qs.get("name", ["document.pdf"])[0])
            n = int(self.headers.get("Content-Length") or 0)
            if limits["max_mb"] and n > limits["max_mb"] * 1_048_576:
                self._drain(n)
                return self._json({"error": f"{name} is {n / 1_048_576:.0f} MB (limit {limits['max_mb']} MB)"}, 413)
            os.makedirs(INBOX, exist_ok=True)
            tmp = os.path.join(INBOX, "." + name + ".part")
            with open(tmp, "wb") as f:              # streamed to disk in chunks, never held in RAM
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            problem = check_pdf(tmp, limits)
            if problem:
                os.remove(tmp)
                return self._json({"error": problem.replace("." + name + ".part", name)}, 400)
            os.replace(tmp, os.path.join(INBOX, name))
            return self._json({"ok": True, "name": name})

        body = self._body_json()
        if u.path == "/api/convert":
            s = load_settings()
            opts = dict(s, **(body.get("options") or {}))
            names = body.get("names") or [f["name"] for f in file_list() if f["status"] in ("new", "error")]
            max_files = s["limits"]["max_files"]
            skipped = []
            if max_files and len(names) > max_files:
                names, skipped = names[:max_files], names[max_files:]
            queued = []
            for nm in names:
                nm = safe_name(nm)
                if os.path.exists(os.path.join(INBOX, nm)):
                    per = dict(opts)
                    per.update((body.get("per_file") or {}).get(nm) or {})
                    if JOBS.enqueue(nm, per):
                        queued.append(nm)
            msg = f"Batch limit is {max_files} files: {len(skipped)} left for the next run." if skipped else ""
            return self._json({"ok": True, "queued": queued, "skipped": skipped, "message": msg})
        if u.path == "/api/cancel":
            JOBS.cancel(safe_name(body.get("name", "")))
            return self._json({"ok": True})
        if u.path == "/api/delete":
            nm = safe_name(body.get("name", ""))
            with JOBS.lock:
                if JOBS.state.get(nm, {}).get("status") == "processing":
                    return self._json({"error": "file is being processed"}, 409)
                JOBS.state.pop(nm, None)
            p = os.path.join(INBOX, nm)
            if os.path.exists(p):
                os.remove(p)
            d = os.path.join(OUTPUT, stem_of(nm))
            if os.path.isdir(d):
                shutil.rmtree(d)
            return self._json({"ok": True})
        if u.path == "/api/settings":
            s = load_settings()
            for k, v in body.items():
                if k == "limits":
                    s["limits"] = clean_limits(dict(s["limits"], **(v or {})))
                elif k in DEFAULT_SETTINGS:
                    s[k] = v
            json.dump(s, open(SETTINGS, "w"), indent=1)
            return self._json(s)
        if u.path == "/api/open":
            target = INBOX if body.get("which") == "inbox" else OUTPUT
            if body.get("stem"):
                target = self._under(OUTPUT, body["stem"]) or OUTPUT
            os.makedirs(target, exist_ok=True)
            return self._json({"ok": open_folder(target), "path": target})
        return self._json({"error": "not found"}, 404)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="paperlift serve")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PAPERLIFT_PORT", 8765)))
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only on a trusted network")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(OUTPUT, exist_ok=True)
    try:
        os.nice(5)          # stay out of the way of the rest of the machine
    except Exception:
        pass
    port = a.port
    for _ in range(20):
        try:
            srv = ThreadingHTTPServer((a.host, port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("No free port found.")
        return 1
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{port}/"
    print(f"\n  Paperlift {VERSION} running at {url}\n  Inbox:  {INBOX}\n  Output: {OUTPUT}\n  Press Ctrl+C to stop.\n", flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
