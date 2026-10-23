"""Generate synthetic PDFs for tests (no copyrighted material in the repo).

    python tests/fixtures.py OUT_DIR
"""
from __future__ import annotations

import os
import sys

import pymupdf as fitz

GREEN, RED, BLACK = (0, 0.5, 0), (1, 0, 0), (0, 0, 0)


def _story_pdf(path: str, html: str, css: str = "", columns: int = 1, header: str = "", footer: bool = True):
    tmp = path + ".tmp.pdf"
    story = fitz.Story(html=html, user_css=css)
    w = fitz.DocumentWriter(tmp)
    mb = fitz.paper_rect("a4")
    if columns == 2:
        gap = 24
        cw = (mb.width - 108 - gap) / 2
        rects = [fitz.Rect(54, 72, 54 + cw, mb.height - 72), fitz.Rect(54 + cw + gap, 72, mb.width - 54, mb.height - 72)]
    else:
        rects = [fitz.Rect(54, 72, mb.width - 54, mb.height - 72)]
    more, n = True, 0
    while more:
        dev = w.begin_page(mb)
        for r in rects:
            if more:
                more, _ = story.place(r)
                story.draw(dev)
        w.end_page()
        n += 1
    w.close()
    doc = fitz.open(tmp)
    for i, page in enumerate(doc):
        if header:
            page.insert_text((54, 40), header, fontsize=8, color=(0.4, 0.4, 0.4))
        if footer:
            page.insert_text((page.rect.width / 2 - 10, page.rect.height - 30), f"Page {i + 1} of {doc.page_count}", fontsize=8)
    doc.save(path)
    doc.close()
    os.remove(tmp)


ARTICLE = """
<h1>Gradient Descent in Practice</h1>
<p>Training a neural network means minimising a loss function over millions of parameters.
This note collects practical observations about <b>learning rates</b>, <i>momentum</i> and batch sizes,
written for students who have already seen the basic update rule.</p>
<h2>1. The update rule</h2>
<p>At every step the parameters move a small distance against the gradient. The size of that step is
controlled by the learning rate, and choosing it well matters more than almost any other setting.
A rate that is too small converges slowly; a rate that is too large makes the loss oscillate or diverge.</p>
<ul>
<li>Start with a rate around 0.001 for Adam and 0.1 for plain SGD.</li>
<li>Reduce the rate when the validation loss plateaus.</li>
<li>Warm the rate up during the first few hundred steps for large models.</li>
</ul>
<h2>2. Momentum</h2>
<p>Momentum keeps an exponentially decaying average of past gradients. It smooths noisy directions
and accelerates progress along consistent ones, which is why nearly every modern optimiser uses it in
some form. The coefficient beta is commonly set to 0.9.</p>
<ol>
<li>Compute the gradient on the current mini-batch.</li>
<li>Blend it into the velocity vector.</li>
<li>Step the parameters along the velocity.</li>
</ol>
<h2>3. Comparing optimisers</h2>
<p>The table below summarises typical defaults that work for a wide range of problems.</p>
<table border="1">
<tr><th>Optimiser</th><th>Learning rate</th><th>Momentum</th></tr>
<tr><td>SGD</td><td>0.1</td><td>0.9</td></tr>
<tr><td>Adam</td><td>0.001</td><td>0.9 / 0.999</td></tr>
<tr><td>RMSProp</td><td>0.001</td><td>0.0</td></tr>
</table>
<h2>4. Batch size</h2>
<p>Larger batches give more accurate gradient estimates but fewer updates per epoch. In practice the
learning rate is often scaled linearly with the batch size, a rule of thumb that holds surprisingly
well until batches become very large. Smaller batches add noise that can help generalisation.</p>
<p>When memory is limited, gradient accumulation simulates a larger batch by summing gradients over
several forward and backward passes before applying a single update.</p>
<h3>4.1 A worked example</h3>
<p>Suppose the loss is quadratic with curvature two. Gradient descent with a learning rate of one half
reaches the minimum in a single step, while a rate of one oscillates forever between two points.
Any rate above one diverges. This tiny example already shows every regime seen in large networks.</p>
<pre>for step in range(1000):
    grad = compute_grad(w)
    w = w - lr * grad</pre>
<p>That is all the machinery needed; everything else is choosing good hyperparameters and monitoring
the training curves carefully for signs of trouble.</p>
"""


def make_article(path: str):
    _story_pdf(path, ARTICLE * 1, css="table{border-collapse:collapse} td,th{border:1px solid black;padding:3px}",
               columns=2, header="ML Notes — Week 3")
    # add a vector chart on the last page
    doc = fitz.open(path)
    page = doc[-1]
    r = fitz.Rect(320, 560, 540, 720)
    sh = page.new_shape()
    sh.draw_rect(r); sh.finish(color=BLACK, width=0.6)
    pts = [fitz.Point(r.x0 + i * 10, r.y1 - 10 - 120 * (0.8 ** i)) for i in range(22)]
    sh.draw_polyline(pts); sh.finish(color=(0.1, 0.4, 0.9), width=1.2)
    for i in range(5):
        sh.draw_line((r.x0, r.y0 + i * 32), (r.x0 + 5, r.y0 + i * 32)); sh.finish(color=BLACK, width=0.5)
    sh.commit()
    page.insert_text((r.x0 + 40, r.y0 + 14), "Training loss", fontsize=8)
    doc.saveIncr()
    doc.close()


def make_scanned(src: str, path: str):
    """Image-only copy of page 1 of `src` (simulates a scan)."""
    s = fitz.open(src)
    pix = s[0].get_pixmap(dpi=150)
    d = fitz.open()
    p = d.new_page(width=s[0].rect.width, height=s[0].rect.height)
    p.insert_image(p.rect, pixmap=pix)
    d.save(path)
    d.close(); s.close()


def make_qpaper(path: str):
    """A tiny paper in the IITM export layout: labels, option ids coloured green/red."""
    doc = fitz.open()
    page = doc.new_page()
    y = 40

    def line(text, x=23, color=BLACK, size=12, bold=False):
        nonlocal y, page
        if y > 790:
            page = doc.new_page(); y = 30
        page.insert_text((x, y), text, fontsize=size, color=color, fontname="hebo" if bold else "helv")
        y += 19

    line("Question Paper Name :", x=21); y -= 19; line("DEMO DEGREE EXAM 12 Jan 2026", x=330)
    y += 20
    line("Sample Subject", x=230, size=18, bold=True)
    line("Section Id :", x=21); y -= 19; line("1234567890", x=330)
    line("Number of Questions :", x=21); y -= 19; line("4", x=330)
    line("Section Marks :", x=21); y -= 19; line("7", x=330)
    line("Sub-Section Number :", x=21); y -= 19; line("1", x=330)
    qs = [
        ("MCQ", "Multiple Choice Question", 'THIS IS QUESTION PAPER FOR THE SUBJECT "DEGREE LEVEL : SAMPLE SUBJECT"', 0, [("Yes", True), ("No", False)], None),
        ("MCQ", "Multiple Choice Question", "Which activation is linear?", 2, [("Sigmoid", False), ("Identity", True), ("Tanh", False), ("ReLU", False)], None),
        ("MSQ", "Multiple Select Question", "Which are optimisers?", 3, [("Adam", True), ("SGD", True), ("Softmax", False), ("Dropout", False)], None),
        ("SA", "Short Answer Question", "How many parameters does a 3-input, 1-output linear unit with bias have?", 2, [], "4"),
    ]
    oid = 6406530000000
    for n, (qt, label, text, marks, opts, ans) in enumerate(qs, 1):
        line(f"Question Number : {n} Question Id : 64065310000{n:02d} Question Type : {qt}")
        line(f"Correct Marks : {marks}")
        line(f"Question Label : {label}")
        line(text)
        if opts:
            line("Options :")
            for t, ok in opts:
                oid += 1
                page.insert_text((24, y), f"{oid}. ", fontsize=12, color=GREEN if ok else RED)
                page.insert_text((131, y), t, fontsize=12)
                y += 21
        else:
            line("Response Type : Numeric")
            line("Possible Answers :")
            line(ans, color=GREEN, size=13.5)
        line("Sub-Section Number :", x=21); y -= 19; line(str(n + 1), x=330)
    doc.save(path)
    doc.close()


def make_all(out: str) -> dict:
    os.makedirs(out, exist_ok=True)
    p = {k: os.path.join(out, f"{k}.pdf") for k in ("article", "scanned", "qpaper")}
    make_article(p["article"])
    make_scanned(p["article"], p["scanned"])
    make_qpaper(p["qpaper"])
    return p


if __name__ == "__main__":
    print(make_all(sys.argv[1] if len(sys.argv) > 1 else "fixtures_out"))
