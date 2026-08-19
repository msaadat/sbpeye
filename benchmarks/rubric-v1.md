# SBPEye Chat Benchmark — Scoring Rubric v1

**Purpose.** Score the free-text answer a system gives to a regulatory question, so that
two independent raters reach the same number and different systems can be compared on one
scale.

**System-agnostic by design.** The rater sees only the question and the answer text. No
retrieval logs, no internal IDs. Any system that accepts a question and returns prose can
be scored — SBPEye, a competing product, or a general-purpose assistant.

---

## 1. How to score one answer

Score the five dimensions below, each **0, 1, or 2**, then apply the fabrication gate.

| # | Dimension | Weight | What it asks |
|---|-----------|--------|--------------|
| A | Accuracy | 35 | Is the regulatory position correct? |
| B | Grounding | 20 | Can I verify it from the citation given? |
| C | Concreteness | 15 | Does it give the operative figure/date/scope? |
| D | Currency | 15 | Is it the position in force *today*? |
| E | Completeness | 15 | Does it cover every element the key requires? |

**Item score** = (35·A + 20·B + 15·C + 15·D + 15·E) / 2, giving 0–100.

---

## 2. Dimension anchors

Use the answer key's "Required facts" list as the reference for every dimension.

### A — Accuracy (weight 35)
| Score | Anchor |
|-------|--------|
| 0 | The stated position is wrong, or contradicts the key on a material point |
| 1 | Directionally right but a material element is wrong, misstated, or missing |
| 2 | Matches the key on every material point |

### B — Grounding (weight 20)
| Score | Anchor |
|-------|--------|
| 0 | No citation at all, or the citation does not exist / does not say this |
| 1 | Names the instrument only loosely — "a BPRD circular", "the AML regulations" — so the rater cannot go straight to the source |
| 2 | Precise and checkable: circular number + year (and date or department where the key requires it), or the named regulation and its regulation/clause number |

> A correct answer with **no** citation caps at B=0. This is deliberate: an auditor cannot
> use an uncitable answer.

> **Score the reference an auditor can act on — "BPRD Circular No. 05 of 2020",
> "Regulation-8", "Definition-25" — never an internal identifier.** Database keys, URLs,
> and link mechanics are implementation details the user never sees, and they differ from
> one system to the next; grading them would make the benchmark non-portable and would
> punish a system for a rendering bug rather than for a wrong answer. A broken link on a
> correctly named circular is an engineering defect, reported separately from the score.

### C — Concreteness (weight 15)
| Score | Anchor |
|-------|--------|
| 0 | Generic advice — "banks must ensure compliance with applicable requirements" |
| 1 | Some specifics, but the key operative value (amount, %, date, deadline) is absent or vague |
| 2 | States the actual figure, threshold, deadline, and who it applies to |

### D — Currency (weight 15)
| Score | Anchor |
|-------|--------|
| 0 | Presents a superseded/withdrawn position as if current, or ignores a stated future effective date |
| 1 | Cites the right current instrument but is silent on what it amended/superseded, or on transition dates, where the key requires it |
| 2 | Gives the in-force position and correctly handles supersession and effective dates |

> Where the key marks a question **"currency-neutral"**, score D = 2 by default and note it.

### E — Completeness (weight 15)
| Score | Anchor |
|-------|--------|
| 0 | Answers a narrower or different question than the one asked |
| 1 | Main point covered; one or more required elements from the key omitted |
| 2 | Every required element present |

---

## 3. The fabrication gate

Tick **F** if the answer contains any of:

- a circular / regulation number, or a date, that does not exist
- a figure, threshold, or deadline not supported by the key or the cited source
- a quotation presented as verbatim that is not in the source
- a citation attached to a proposition the cited instrument does not make

**If F is ticked, the item score is 0**, regardless of the five dimensions. Still record
the dimension scores — they are needed to show *what kind* of failure it was.

Rationale for management: an answer containing an invented circular number is worth zero
to a compliance officer no matter how well written. Averaging would let a fluent system
buy back marks it should not have.

**Hallucination rate** = items with F ÷ total items. Report it beside the headline score,
never folded into it.

### 3a. Silent substitution — tick S, not F

A distinct failure, found in the first pilot round: the system is asked about instrument X,
silently answers about a *different* instrument Y, and every fact it states about Y is
correct. Nothing is fabricated, so F would misreport it — but the auditor walks away citing
the wrong instrument, which is worse than an unanswered question.

Tick **S** where the answer:

- substitutes a different instrument for the one asked about without saying it has done so
  (e.g. answers on a *Circular Letter* when asked about a *Circular* of the same number), or
- answers a materially different question while presenting it as the one asked.

**S caps the item score at 25**, and is reported as its own rate beside the hallucination
rate. Naming the substitution — "no Circular No. 07 of 2026 exists; you may mean Circular
Letter No. 07 of 2026, which says…" — is not a substitution at all and is scored normally.

---

## 4. Abstention items

Items marked **ABSTAIN** in the key (unanswerable, false premise, out of scope) use a
single scale instead of the five dimensions:

| Score | Item score | Anchor |
|-------|-----------|--------|
| 2 | 100 | States plainly that the instrument does not exist / the matter is outside the SBP corpus, and invents nothing |
| 1 | 50 | Does not fabricate, but hedges without clearly denying — leaves the auditor unsure |
| 0 | 0 | Answers as though the premise were true. Tick **F** if it invented content; tick **S** instead if it silently answered about a real but different instrument (section 3a) |

These items are the cheapest and sharpest test of whether a system is grounded or merely
confident. Do not drop them from the set.

---

## 5. Rater protocol

1. **Blind.** Systems are labelled A / B / C. The rater must not know which is which.
2. **Shuffled.** Randomise answer order per question, so no system holds a fixed position.
3. **Two raters per item.** Any dimension differing by more than 1 point goes to a third
   rater for adjudication.
4. **No outside research.** Score against the answer key only. If the key is silent on a
   point the answer raises, ignore that point and note it — then fix the key before the
   next round.
5. **Report agreement.** Publish the % of items where both raters' item scores fell within
   10 points. Below ~80%, the rubric is at fault, not the systems — repair it before
   reporting any comparison.

---

## 6. Reading the results

Report, per system:

- **Overall score** — mean item score across all items
- **Hallucination rate** — % of items with F
- **Score by difficulty tier** — Easy / Medium / Hard reported separately; the Hard tier is
  where systems actually separate
- **Per-dimension means** — shows *how* a system wins or loses

**On comparing two systems.** With only 10 pilot items, no difference between systems is
statistically meaningful — the pilot exists to test the rubric, not to rank systems. Even
at the full ~60-item set, a single system's score carries roughly a ±7pp interval. Because
every system answers the identical set, always compare **per-question** — "System A beat B
on 34 items, lost on 12, tied 14" — which cancels item difficulty and is far more
sensitive than two headline means.

---

## 7. Versioning

| Field | Value |
|-------|-------|
| Rubric version | v1 |
| Question set | pilot-v1 (10 items) |
| Corpus snapshot | record the SBPEye DB date at run time |
| Run date | record per round |

Answer keys are tied to a corpus that changes as SBP issues new circulars. **Re-verify
every key marked *freshness-sensitive* before each round.**
