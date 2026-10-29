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
LIBRARY = os.path.join(ROOT, "library.json")

DEFAULT_SETTINGS = {
    "formats": ["md", "docx", "pdf", "json"],
    "mode": "auto",                       # auto | qpaper | general
    "split": True, "renumber": True, "marks": True, "qtype_hint": True, "sections": "auto",
    "tables": True, "vector_figures": True, "page_markers": False, "ocr": "auto",
    "audit": True, "audit_overlay": True,
    "limits": clean_limits(None),
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
        self.hook = None                                   # optional callback(name, cur, total) for live progress
        if not os.environ.get("PAPERLIFT_NO_THREAD"):      # the browser build has no threads and pumps run_next()
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
            if self.hook:
                self.hook(name, cur, total)
        return cb

    def _worker(self):
        while True:
            self._run(self.q.get())

    def run_next(self):
        """Run one queued file synchronously. Returns its name, or None when the queue is empty."""
        while True:
            try:
                name = self.q.get_nowait()
            except queue.Empty:
                return None
            if self.state.get(name, {}).get("status") == "queued":
                self._run(name)
                return name

    def _run(self, name):
        from .pipeline import convert
        try:
            import pymupdf as fitz
        except Exception:
            fitz = None
        with self.lock:
            if self.state.get(name, {}).get("status") != "queued":
                return
            self.state[name].update(status="processing", started=time.time(), progress=[0, 0])
            opts = dict(self.opts.get(name, {}))
        if self.hook:
            self.hook(name, 0, 0)
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

# --------------------------------------------------------------------------
# File manager: virtual folders (stored in library.json; files never move on disk)
# --------------------------------------------------------------------------
LIB_LOCK = threading.Lock()
EXPORT_KINDS = {"md": "md", "docx": "docx", "pdf": "pdf", "json": "json", "html": "html",
                "md_zip": "md_zip", "audit_pdf": "audit_pdf"}


def load_library() -> dict:
    try:
        d = json.load(open(LIBRARY))
    except Exception:
        d = {}
    folders = sorted({str(f) for f in d.get("folders", []) if f})
    assign = {k: v for k, v in (d.get("assign") or {}).items() if v in folders}
    return {"folders": folders, "assign": assign}


def save_library(lib: dict):
    tmp = LIBRARY + ".tmp"
    json.dump(lib, open(tmp, "w"), indent=1)
    os.replace(tmp, LIBRARY)


def clean_folder(name) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", str(name or "")).strip(" .")[:60]


def rename_pdf(old: str, new: str):
    """Rename a source PDF, its output folder and the paths recorded in its summary."""
    os_, ns = stem_of(old), stem_of(new)
    os.replace(os.path.join(INBOX, old), os.path.join(INBOX, new))
    od, nd = os.path.join(OUTPUT, os_), os.path.join(OUTPUT, ns)
    if os.path.isdir(od):
        os.replace(od, nd)
        for base, _, files in os.walk(nd):
            for f in files:
                if f.startswith(os_):
                    os.replace(os.path.join(base, f), os.path.join(base, ns + f[len(os_):]))
        for meta in ("_summary.json", "_audit.json"):
            mp = os.path.join(nd, meta)
            if os.path.exists(mp):
                txt = open(mp).read().replace(os_, ns)
                open(mp, "w").write(txt)


def build_export(names, kinds, layout, include_source):
    """Zip the chosen formats for the chosen files into a temp file; returns its path."""
    lib = load_library()
    zp = os.path.join(OUTPUT, f".export_{int(time.time() * 1000)}.zip")
    used = set()
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for nm in names:
            nm = safe_name(nm)
            stem = stem_of(nm)
            folder = lib["assign"].get(nm, "") if layout == "folders" else ""
            sp = os.path.join(OUTPUT, stem, "_summary.json")
            pairs = []
            if include_source and os.path.exists(os.path.join(INBOX, nm)):
                pairs.append(os.path.join(INBOX, nm))
            if os.path.exists(sp):
                try:
                    outs = json.load(open(sp)).get("outputs", [])
                except Exception:
                    outs = []
                for o in outs:
                    for k in kinds:
                        fp = o.get("files", {}).get(k)
                        if fp and os.path.isfile(fp) and self_under_output(fp):
                            pairs.append(fp)
            for fp in pairs:
                arc = "/".join(x for x in (folder, os.path.basename(fp)) if x)
                base, ext = os.path.splitext(arc)
                i = 2
                while arc in used:
                    arc, i = f"{base} ({i}){ext}", i + 1
                used.add(arc)
                z.write(fp, arc)
    return zp, len(used)


def self_under_output(fp):
    return os.path.realpath(fp).startswith(os.path.realpath(OUTPUT) + os.sep)



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
                audit=summary.get("audit"),
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

    def _export(self, names, formats, layout, source):
        kinds = [k for k in formats if k in EXPORT_KINDS]
        if not names or not (kinds or source):
            return self._json({"error": "nothing selected"}, 400)
        zp, n = build_export(names, kinds, "folders" if layout == "folders" else "flat", source)
        try:
            if not n:
                return self._json({"error": "None of the selected files have those formats yet. Extract them first."}, 404)
            self._file(zp, "paperlift_export.zip")
        finally:
            os.remove(zp)

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
                               "version": VERSION, "root": ROOT, "ocr": ocr_status(),
                               "library": load_library()})
        if path.startswith("/out/"):
            p = self._under(OUTPUT, path[5:])
            return self._file(p, os.path.basename(p) if p and qs.get("dl") else None)
        if path == "/api/audit-page":
            # one annotated source page as PNG, rendered on demand (no browser PDF viewer needed)
            stem = qs.get("stem", [""])[0]
            d = self._under(OUTPUT, stem) if stem else None
            try:
                n = max(1, int(qs.get("n", ["1"])[0]))
                dpi = min(150, max(40, int(qs.get("dpi", ["90"])[0])))
            except ValueError:
                return self._json({"error": "bad page"}, 400)
            src = next((os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith("_audit.pdf")), None) \
                if d and os.path.isdir(d) else None
            if not src:
                return self._json({"error": "not found"}, 404)
            import pymupdf as fitz
            doc = fitz.open(src)
            try:
                if n > doc.page_count:
                    return self._json({"error": "no such page"}, 404)
                data = doc[n - 1].get_pixmap(dpi=dpi, alpha=False).tobytes("png")
            finally:
                doc.close()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/export":
            return self._export(
                [n for n in qs.get("names", [""])[0].split("\n") if n],
                qs.get("formats", [""])[0].split(","), qs.get("layout", ["flat"])[0],
                qs.get("source", ["0"])[0] == "1")
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
        if u.path == "/api/export":
            return self._export([str(n) for n in body.get("names") or []], body.get("formats") or [],
                                body.get("layout", "flat"), bool(body.get("source")))
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
            with LIB_LOCK:
                lib = load_library()
                if lib["assign"].pop(nm, None) is not None:
                    save_library(lib)
            return self._json({"ok": True})
        if u.path == "/api/folder":
            act, name = body.get("action"), clean_folder(body.get("name"))
            with LIB_LOCK:
                lib = load_library()
                if act == "create" and name:
                    if name.lower() not in [f.lower() for f in lib["folders"]]:
                        lib["folders"].append(name)
                elif act == "rename" and name and body.get("to") and name in lib["folders"]:
                    to = clean_folder(body["to"])
                    if to and to != name:
                        lib["folders"] = [to if f == name else f for f in lib["folders"]]
                        lib["assign"] = {k: (to if v == name else v) for k, v in lib["assign"].items()}
                elif act == "delete" and name in lib["folders"]:
                    lib["folders"].remove(name)
                    lib["assign"] = {k: v for k, v in lib["assign"].items() if v != name}
                else:
                    return self._json({"error": "bad folder request"}, 400)
                save_library(lib)
            return self._json(load_library())
        if u.path == "/api/move":
            folder = clean_folder(body.get("folder"))
            with LIB_LOCK:
                lib = load_library()
                if folder and folder not in lib["folders"]:
                    lib["folders"].append(folder)
                for nm in body.get("names") or []:
                    nm = safe_name(nm)
                    if not os.path.exists(os.path.join(INBOX, nm)):
                        continue
                    if folder:
                        lib["assign"][nm] = folder
                    else:
                        lib["assign"].pop(nm, None)
                save_library(lib)
            return self._json(load_library())
        if u.path == "/api/rename":
            old, new = safe_name(body.get("name", "")), safe_name(body.get("to", ""))
            if not os.path.exists(os.path.join(INBOX, old)):
                return self._json({"error": "not found"}, 404)
            if old == new:
                return self._json({"ok": True, "name": new})
            case_only = old.lower() == new.lower()
            if not case_only and (os.path.exists(os.path.join(INBOX, new)) or os.path.exists(os.path.join(OUTPUT, stem_of(new)))):
                return self._json({"error": f"{new} already exists"}, 409)
            if JOBS.get(old).get("status") in ("queued", "processing"):
                return self._json({"error": "file is queued or being processed"}, 409)
            rename_pdf(old, new)
            with LIB_LOCK, JOBS.lock:
                JOBS.state.pop(old, None)
                lib = load_library()
                if old in lib["assign"]:
                    lib["assign"][new] = lib["assign"].pop(old)
                    save_library(lib)
            return self._json({"ok": True, "name": new})
        if u.path == "/api/delete-many":
            deleted, busy = [], []
            for nm in body.get("names") or []:
                nm = safe_name(nm)
                with JOBS.lock:
                    if JOBS.state.get(nm, {}).get("status") == "processing":
                        busy.append(nm)
                        continue
                    JOBS.state.pop(nm, None)
                p = os.path.join(INBOX, nm)
                if os.path.exists(p):
                    os.remove(p)
                d = os.path.join(OUTPUT, stem_of(nm))
                if os.path.isdir(d):
                    shutil.rmtree(d)
                deleted.append(nm)
            with LIB_LOCK:
                lib = load_library()
                lib["assign"] = {k: v for k, v in lib["assign"].items() if k not in deleted}
                save_library(lib)
            return self._json({"ok": True, "deleted": deleted, "busy": busy})
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
