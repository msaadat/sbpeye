# SBPEye Chat Benchmark

A human-scored, system-agnostic benchmark for regulatory answer quality. It treats every
system as a black box — question in, prose out — so SBPEye, a competing product, and a
general-purpose assistant can all be put on the same scale and reported to management as one
number plus a hallucination rate.

## Layout

**Methodology — tracked in git:**

| File | Purpose |
|------|---------|
| `rubric-v1.md` | The scoring rubric. Five weighted dimensions, a fabrication gate, a silent-substitution tick, and the rater protocol. |
| `pilot-v1-questions.md` | The 10 pilot questions. The single source of truth — `run_pilot.py` parses them from here. |
| `pilot-v1-answer-key.md` | **Grader only.** Verified facts, acceptable citations, and the traps each item sets. |
| `scoresheet-template.csv` | One pre-filled row per item for a rater to complete. |
| `run_pilot.py` | Puts the questions to a running SBPEye and captures the answers. |
| `make_grading_pack.py` | Assembles captured answers into one document a rater can work from. |
| `score.py` | Turns filled scoresheets into the reportable numbers. |
| `check_citations.py` | Engineering diagnostic for broken citation links. **Not a grading input.** |

**Results — gitignored (`/benchmarks/results/`):** one directory per round, e.g.
`results/2026-08-19-sbpeye/` containing `answers/`, `grading-pack.md`, `scoresheet.csv`,
`summary.txt` and `assessment.md`. Results are per-run, per-model evidence and stay local;
only the methodology is committed.

## Running a round

```bash
python benchmarks/run_pilot.py --out benchmarks/results/2026-08-19-sbpeye/answers
```

```bash
python benchmarks/make_grading_pack.py benchmarks/results/2026-08-19-sbpeye/answers --system SBPEye --out benchmarks/results/2026-08-19-sbpeye/grading-pack.md
```

Then: copy `scoresheet-template.csv` into the round directory, fill `run_date`/`system`/
`rater`; label systems A/B/C and shuffle answer order before handing anything to raters; two
raters score each item against the answer key; concatenate their sheets and run:

```bash
python benchmarks/score.py benchmarks/results/2026-08-19-sbpeye/scoresheet.csv
```

Other systems under test are prompted by hand; save their answers in the same JSON shape
(`item`, `question`, `answer`, `elapsed_s`) and the rest of the pipeline is identical.

## What the pilot is for

The pilot tests the **rubric**, not the systems. Ten items cannot rank anything — the
question to answer is whether two raters independently produce the same score. If they do,
scale to ~60 items and start reporting comparisons. If they do not, the anchors need repair
first.

The first round (2026-08-19) already showed the instrument hitting its ceiling: every
substantive item scored full marks, so the set needs harder items before it can discriminate.
See that round's `assessment.md`.

## Grade the reference, not the plumbing

Grounding is scored on whether the answer names the instrument an auditor can act on —
"BPRD Circular No. 05 of 2020", "Regulation-8". Internal database IDs, URLs and link
mechanics are implementation details that differ between systems; scoring them would punish
a rendering bug rather than a wrong answer, and would make the benchmark non-portable.

## Keeping the keys honest

Answer keys are pinned to a live corpus that SBP keeps adding to. Items marked ⏱ in the
answer key are freshness-sensitive and must be re-verified before every round — P06 in
particular cites a circular issued one day before the keys were written.
