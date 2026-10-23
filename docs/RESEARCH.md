# Fidelity audit: reference-free verification of PDF extraction

*Paperlift 0.3 · research note*

**Summary.** Paperlift checks every extraction it produces, with no ground truth
needed. It treats the source PDF's text layer and embedded images as an exact
inventory of content, and gives every source unit a *fate*: kept, or dropped
for a stated reason. Then it proves that every kept unit is present in the
output exactly once and in an order the page layout requires. Exam papers add
a second, independent oracle: the redundancy the exam export carries about
itself (declared question counts, section marks, and ID sequences that form
arithmetic progressions).

On 9 documents, all 9 clean extractions verify with **zero false alarms**. On
**≈ 2,700 injected faults**, every fault that removes, duplicates or alters content
is detected **99–100 %** of the time. **99.1 %** of detectable block swaps are
caught. Faults that change only structure or answer marks are, as predicted,
not detected. The audit adds about 0.3–0.5 s per 12-page paper.

---

## 1. The problem

Converters such as Marker, Nougat, Docling, MinerU, GROBID and pymupdf4llm
turn PDFs into Markdown or structured text. Their quality is measured on
annotated benchmarks such as OmniDocBench, using text edit distance, TEDS for
tables and reading-order scores against ground truth[^omni]. This works for
comparing tools, but it says nothing about the file a user has just
converted, where no ground truth exists. A dropped line, a missing option or
a duplicated paragraph passes silently into the user's notes.

Reference-free quality estimation has mostly been studied for OCR. Earlier
methods used recognizer confidence or language models for historical
printings[^ocrqe], and models trained to predict error rates without
transcriptions for handwriting[^htr]. The most recent large study compares a
re-rendered output image with the page image ("render-and-compare"). Across
147 variants, its best composite method reaches a mean Spearman ρ of 0.494
with true error[^gtfree]. These methods have to treat the page as pixels,
because a scan has nothing better.

**Observation.** Much of what people convert is not scanned. Exported,
"flattened", born-digital PDFs still carry two things a pixel-based method
cannot use:

1. **An exact content oracle.** The text layer lists every glyph, and the image
   objects list every embedded picture. It is unreliable about *structure*
   (it has no headings, its order is the drawing order, and columns interleave),
   but it is exact about *what must survive*.
2. **Redundancy the document carries about itself.** For example, an
   online-exam export states how many questions a section has, what they are
   worth in total, and numbers questions and options with consecutive IDs.

The audit builds a verifier from these two oracles.

## 2. Method

### 2.1 Units and fates

The source is split into units *u*: text lines, as tokens, and embedded
images. A profile-specific **explanation function** assigns each unit a fate:

```
fate(u) ∈ { KEPT }  ∪  { DROPPED(r) : r ∈ Reasons }
```

The reasons are explicit and shown to the user. For exam papers they are
*console metadata*, *question header*, *comprehension header*, *subject
confirmation question*, *section not exported* and *section title used as
document title*. For general documents they are *running header / footer /
page number*, *inside a figure kept as image*, *rotated margin text*, *page
background* and *decorative image*. Option IDs are stripped from option lines
as an explained part of the line.

This turns the extractor's implicit decisions into an auditable ledger. A
policy that is not written down shows up as a finding, because no reason
covers it. That is how the audit exposed a silent "remove repeated heading"
rule in Paperlift's own document extractor (§4.3).

### 2.2 Witnessing: an injective map from kept tokens to output tokens

Text is normalised with NFKC and lower-casing, and tokenised into numbers and
letter runs. The output (question stems, options, accepted answers, passages,
document blocks and table cells) becomes one token stream *O*. Images become a
multiset of image names.

For each kept line with tokens *T = t₁…tₘ*, the audit matches its *k*-grams
(*k* = 3, or *k* = *m* for shorter lines) against an index of *O*. It then
builds a partial **injective map** φ from source tokens to output positions:

* **Consumption.** An output position can witness only one source token. This
  holds across lines, and also *within* a line: overlapping *k*-grams may share
  a position only if they assign it to the same source token. Without the
  within-line rule, the one surviving copy of a repeated phrase vouched for
  both copies, and fault injection found exactly that ("special methods table
  … special_methods_table").
* **Locality.** Among candidate occurrences, the nearest one after the
  previous match is taken. A per-word fallback is accepted only within a small
  window of the line's other matches.

From φ the audit computes:

| Quantity | Definition | Catches |
|---|---|---|
| Token recall *Rₜ* | \|dom φ\| / \|kept tokens\| | lost text |
| Image recall *Rᵢ* | matched kept images / kept images (multiset) | lost figures and inline math |
| Precision *P* | \|im φ\| / \|O\| | duplicated or invented text |
| Order *Ω* | 1 − inversions / \|Π\| | wrong reading order |

**Order constraints Π** are the pairs of units whose relative order the layout
fixes regardless of how the extractor reads the page:

* consecutive lines inside one PDF text block;
* the block directly below another in the same column;
* on single-column pages, a top-to-bottom chain of vertically separated blocks;
* on two-column pages, where columns are only recognised if their vertical
  extents overlap (so that right-aligned titles are not mistaken for a
  column), the bottom of the left column before the top of the right column;
* the end of page *n* before the start of page *n + 1*;
* for exam papers, the lines of one question (stem, options, answer) in order.

The position of a line in *O* is the median of its witnessed positions.

**Fidelity score:** F = 100 · Rₜ · Rᵢ · Ω · P.

**Conservation law.** For a correct extraction φ is a bijection between kept
source tokens and output tokens, so |kept tokens| = |O| and F = 100 exactly.
This held on every clean document tested. For example, 763 = 763 and
549 = 549 tokens on the two real exam papers, and 1236 = 1236 on the arXiv
paper. Any finding is therefore informative, and the verdict can be strict:
*verified* means zero findings.

### 2.3 Checksums declared by exam papers

The exam export repeats information about itself. The audit re-derives each
of the following from the extraction and compares:

| Check | Invariant |
|---|---|
| C1 declared count | `Number of Questions` = standalone questions + comprehension groups |
| C2 declared marks | `Section Marks` = Σ marks of the extracted questions |
| C3 question IDs | in paper order, question IDs form an arithmetic progression with step 1, and each comprehension group occupies one ID |
| C4 option IDs | option IDs are consecutive inside a question, and `first(qⱼ₊₁) = last(qⱼ) + 1 + #SA(qⱼ…qⱼ₊₁)`, because every short-answer question silently consumes one option ID |
| C5 answer cardinality | MCQ has exactly one correct option, MSQ at least one, SA at least one accepted answer |

C3 and C4 are empirical regularities. They were discovered by inspecting the
ID sequences (a gap of exactly 3 option IDs after three consecutive SA
questions, of 6 after six, and of 1 after one) and confirmed on both real
papers. C1 and C2 also hold on both papers: 11 questions = 7 standalone + 4
groups, and Σ = 40 marks. These checks are independent of the text oracle. A
question lost together with all its text, its marks and its IDs still breaks
C1–C3.

### 2.4 Visual ledger

Each unit is drawn on a copy of the source PDF:
* green: kept and found
* amber: partly found
* red: not found
* grey: removed on purpose
* dashed outline: out of order

The app renders these pages on demand. Clicking a finding jumps to its page.

![Overlay of an injected fault](audit-overlay-example.png)

*Injected fault on a synthetic paper: option C was deleted from the output (red), and two words were removed from a stem (amber). Checksum C4 also fires: "options inside the question are not consecutive".*

## 3. Evaluation

### 3.1 Setup

* **Documents (9).**
  * Exam-paper profile: one synthetic paper (`tests/fixtures.py`, in the
    repository) and two real exported IITM exam papers (12 pages each; not
    redistributed).
  * Document profile: one synthetic two-column article with a table, lists,
    code and a vector chart (in the repository), plus five PDFs from
    PyMuPDF's public test corpus: an arXiv astrophysics paper with dense
    inline math, a German magazine page with ligatures and hyphenation, an
    11-page Sphinx manual, a fund report, and Chinese financial tables.
* **False-positive check.** Audit each clean extraction.
* **Fault injection.** 13 operators mutate a copy of the extraction (40
  seeded trials per operator per document); then the audit is run again.
  * *Detected* means the score is below 100 or there is any finding.
  * *Alerted* means the verdict is not "verified".
  * *Located* means a finding points at the faulty question's page or the
    page after it (exam papers only, because general blocks carry no page).
* Two operators are marked **blind**: they change structure or answer marks
  but no content, so content conservation is not expected to see them. They
  are included to measure the method's limits.
* Reproduce with `python bench/fault_injection.py [extra.pdf …]`.

### 3.2 Clean extractions

All 9 documents scored F = 100 and were *verified*, with 0 findings.

### 3.3 Injected faults (all 9 documents)

| Profile | Fault | Trials | Detected | Alerted | Located |
|---|---|--:|--:|--:|--:|
| exam paper | drop option | 120 | 100 % | 100 % | 99 % |
| exam paper | drop question | 120 | 100 % | 100 % | 100 % |
| exam paper | delete 1–3 words (stem) | 118 | 100 % | 100 % | 100 % |
| exam paper | delete 1–3 words (option) | 80 | 100 % | 100 % | 100 % |
| exam paper | drop inline / figure image | 80 | 100 % | 100 % | 100 % |
| exam paper | swap adjacent options | 120 | 100 % | 100 % | 100 % |
| exam paper | duplicate a sentence | 120 | 100 % | 91 % | 91 % |
| exam paper | 1-character substitution | 120 | 100 % | 100 % | 100 % |
| exam paper | answer mark lost | 120 | 100 % | 100 % | 100 % |
| exam paper | *blind:* answer mark moved | 120 | 0 % | 0 % | – |
| document | drop block | 240 | 100 % | 100 % | – |
| document | delete 1–3 words | 198 | 100 % | 100 % | – |
| document | drop figure (raster or vector) | 80 | 100 % | 100 % | – |
| document | duplicate block | 240 | 100 % | 95 % | – |
| document | 1-character substitution | 202 | 99 % | 98 % | – |
| document | drop table row | 80 | 100 % | 100 % | – |
| document | swap adjacent blocks (sampled) | 240 | 83 % | 83 % | – |
| document | *blind:* heading → paragraph | 200 | 0 % | 0 % | – |

On the two synthetic documents alone, which anyone can reproduce from the
repository, every non-blind operator reaches 100 % detected, alerted and
located.

**Swaps, exhaustively.** Random sampling under-states swap detection on short
documents, because the same few undetectable swaps are drawn repeatedly. All
121 adjacent block swaps were therefore enumerated:

* 2 exchanged identical text ("Contents" / "Contents"), a no-op that nothing
  can detect;
* 3 involved text whose font has no Unicode mapping, which cannot be
  verified (and which the audit now reports as a note);
* of the remaining 116, **115 were detected (99.1 %)**. The single miss is a
  page footer next to a paragraph with no layout relation between them.

**Residual misses explained:**
* Substitutions that only change letter case (E → e) are invisible, because
  matching is case-insensitive.
* Duplicates of a single word lower the score (so they are *detected*), but
  fall below the 2-word threshold for listing a finding (hence 91–95 %
  *alerted*).

### 3.4 Cost

| Document | Extraction | With audit and overlay |
|---|--:|--:|
| 12-page exam paper (a) | 0.51 s | 1.00 s |
| 12-page exam paper (b) | 1.41 s | 1.71 s |
| 11-page manual | 0.23 s | 0.34 s |
| 1-page arXiv page | 0.46 s | 0.72 s |

Python heap peak stays under 16 MB. Matching itself takes about 0.2 s. The
rest is re-reading page geometry and writing the overlay PDF. Making the audit
fast also sped up extraction by about 20 %, because both share the same
line-building code.

## 4. What the audit found while it was being built

The audit was run on real documents during development, and every finding was
traced to its cause. The fixes fall into three groups.

### 4.1 Bugs in the extractors

* **Declared totals were never captured.** The exam-paper parser was meant to
  record a section's declared question count and marks, but its state machine
  had already moved on by the time those lines arrived. Fixed; C1 and C2
  depend on it.
* **Hyphen rejoining missed real cases.** A German page split
  "Gravitations-" / "wellen" across lines, and the hyphen sat in a different
  text fragment from the end of the line, so the extractor left the word
  broken. Fixed by joining on the line's concatenated tail.

### 4.2 Unwritten policies

* **A silent "remove repeated heading" rule.** The document extractor quietly
  deleted a "Contents" heading because it repeated the one before. Removed:
  the extractor may not drop content that no stated reason covers.

### 4.3 Audit false positives, each a lesson about the oracle

* A console label wrapping onto two lines without its colon had to be added
  to the metadata explanation.
* Style changes are not word breaks. "𝜇" in italic followed by "Jy" is one
  token in the text layer, so the output must be tokenised on the joined text
  of a block, not on each styled run.
* Right-aligned chapter titles above single-column text were taken for a
  second column. Columns are now recognised only when they run side by side.
* Matching had to become one-to-one at the token level, both across and
  within lines (§2.2), before deleted repeats became visible.

## 5. Limits and threats to validity

* **Content, not structure.** The oracle cannot say whether a line should be
  a heading, a list item or a table cell. A heading demoted to a paragraph,
  or two paragraphs merged, is invisible. A future direction is to add the
  PDF's structure tree as a structural oracle, when a tagged PDF has one.
* **Answer marks.** C5 catches a lost mark but not a *moved* one. The only
  independent evidence would be the green and red colours, and those are what
  the extractor already reads, so checking against them would be circular.
* **The oracle can be wrong.** If a font has no Unicode mapping, the text
  layer is garbage and the audit verifies against garbage. Such pages are
  reported as notes. Scanned pages have no text layer and are marked *not
  verifiable*. OCR output is excluded from scoring.
* **Explanation rules are policy.** They are written per profile and must
  change when an extraction policy changes. This is intended: policies stay
  explicit. But a too-generous rule would hide real losses.
* **Small corpus, synthetic faults.** Nine documents and thirteen operators
  chosen by the author are evidence, not proof. Real extractor bugs may
  cluster differently from injected ones. The development history in §4 is
  the more realistic signal: every real defect found was a content-conservation
  or ordering failure of exactly the kinds measured here.
* **ID regularities.** C3 and C4 are observed on one exam platform's exports.
  They are only checked when the exam-paper profile applies, and a failure is
  reported as "may be missing", not as a definite loss.

## 6. Reproducing

```bash
./paperlift.sh audit paper.pdf --overlay ./audit      # one file: report and annotated PDF
python bench/fault_injection.py                       # synthetic fixtures (bundled)
python bench/fault_injection.py a.pdf b.pdf …         # plus your own documents
pytest tests/test_audit.py                            # behaviour, including the documented blind spot
```

[^omni]: Ouyang et al., *OmniDocBench: Benchmarking Diverse PDF Document Parsing with Comprehensive Annotations*, [arXiv:2412.07626](https://arxiv.org/pdf/2412.07626).
[^ocrqe]: Springmann et al., *Automatic quality evaluation and (semi-)automatic improvement of OCR models for historical printings*, [arXiv:1606.05157](https://arxiv.org/pdf/1606.05157).
[^htr]: *Evaluation of HTR models without Ground Truth Material*, [arXiv:2201.06170](https://arxiv.org/pdf/2201.06170).
[^gtfree]: *GT-Free OCR Metrics: A Reference-Free Evaluation Framework for Document OCR Systems*, NeurIPS 2026, [poster page](https://neurips.cc/virtual/2026/poster/139231).
