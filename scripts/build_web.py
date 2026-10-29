#!/usr/bin/env python3
"""Assemble the static website into ./web (landing page + in-browser app). Output is git-ignored.

    python3 scripts/build_web.py            # then:  python3 -m http.server -d web 8000
"""
import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
APP = os.path.join(WEB, "app")
PKG = os.path.join(ROOT, "paperlift")

# files the browser build needs (no CLI, no desktop server entry point)
PY_FILES = ["__init__.py", "audit.py", "general.py", "limits.py", "pipeline.py",
            "qpaper_engine.py", "qpaper_render.py", "server.py", "webhost.py"]


def main():
    shutil.rmtree(os.path.join(APP, "py"), ignore_errors=True)
    dst = os.path.join(APP, "py", "paperlift")
    os.makedirs(dst)
    for f in PY_FILES:
        shutil.copy(os.path.join(PKG, f), os.path.join(dst, f))
    with open(os.path.join(APP, "py", "manifest.json"), "w") as fh:
        json.dump({"files": ["paperlift/" + f for f in PY_FILES]}, fh)

    html = open(os.path.join(PKG, "static", "index.html"), encoding="utf-8").read()
    html = html.replace('href="/favicon.svg"', 'href="../favicon.svg"')
    html = html.replace("<script>\nconst PL_BASE", '<script src="engine.js"></script>\n<script>\nconst PL_BASE', 1)
    assert "engine.js" in html, "index.html layout changed: engine.js was not injected"
    with open(os.path.join(APP, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    shutil.copy(os.path.join(PKG, "static", "favicon.svg"), os.path.join(WEB, "favicon.svg"))
    open(os.path.join(WEB, ".nojekyll"), "w").close()
    print(f"Built {WEB}")


if __name__ == "__main__":
    main()
