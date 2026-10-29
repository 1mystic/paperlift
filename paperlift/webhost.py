"""
Browser host: runs the real Paperlift request handler without a socket.

The web build loads this inside Pyodide. A request is turned into raw HTTP bytes and fed to the
same Handler the desktop server uses, so the browser and desktop apps share every route and rule.
"""
from __future__ import annotations

import io
import os

os.environ.setdefault("PAPERLIFT_NO_THREAD", "1")

from . import server  # noqa: E402


class _Conn:
    def __init__(self, raw: bytes):
        self._in = io.BytesIO(raw)
        self.out = bytearray()

    def makefile(self, mode="rb", *a, **k):
        return self._in if "r" in mode else io.BytesIO()

    def sendall(self, data):
        self.out += data

    def setsockopt(self, *a):
        pass

    def close(self):
        pass


class _Srv:
    server_name = "paperlift"
    server_port = 0


def request(method: str, target: str, headers: dict | None = None, body: bytes = b""):
    """Returns (status, {header: value}, body bytes)."""
    head = [f"{method} {target} HTTP/1.0"]
    h = {k: v for k, v in (headers or {}).items() if k.lower() != "content-length"}
    h["Content-Length"] = str(len(body))
    head += [f"{k}: {v}" for k, v in h.items()]
    conn = _Conn(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + bytes(body))
    server.Handler(conn, ("127.0.0.1", 0), _Srv())
    raw = bytes(conn.out)
    sep = raw.find(b"\r\n\r\n")
    lines = raw[:sep].decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    out = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        out[k.strip()] = v.strip()
    return status, out, raw[sep + 4:]


def js_request(method, target, headers, body):
    """JS-friendly wrapper: returns (status, headers_json, body_bytes)."""
    import json
    if hasattr(body, "to_py"):
        body = body.to_py().tobytes()
    if hasattr(headers, "to_py"):
        headers = headers.to_py()
    status, h, out = request(method, target, dict(headers or {}), bytes(body or b""))
    return status, json.dumps(h), out


def set_progress_hook(fn):
    server.JOBS.hook = fn


def run_next():
    return server.JOBS.run_next()


def pending() -> int:
    return server.JOBS.pending()
