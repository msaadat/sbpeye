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
| `pilot-v1-questions.md` | The pilot questions (15 items). The single source of truth — `run_pilot.py` parses them from here. |
| `pilot-v1-answer-key.md` | **Grader only.** Verified facts, acceptable citations, and the traps each item sets. |
| `scoresheet-template.csv` | One pre-filled row per item for a rater to complete. |
| `run_pilot.py` | Puts the questions to a running SBPEye and captures the answers. |
| `make_grading_pack.py` | Assembles captured answers into one document a rater can work from. |
| `score.py` | Turns filled scoresheets into the reportable numbers. |
| `check_citations.py` | Engineering diagnostic for broken citation links. **Not a grading input.** |

**Round diagnostics — tracked, but not grading inputs:**

| File | Purpose |
|------|---------|
| `scan_defects.py` | Recurring answer-quality flaws, counted off a round's answers: attachment filenames used as the citable reference, `[[` that no well-formed citation token accounts for, retrieval vocabulary in user-facing prose, thinking-aloud preambles, closing offers to continue, runaway repetition, and answer length against tier. |
| `trace_readout.py` | Joins each answer to its `chat.turn` trace: own model time, tool calls, iteration-ceiling fallbacks, dropped citation handles, token counts. Needs `llm_debug_enabled`. |

Both have the same standing as `check_citations.py`, for the same reason: they read internals
a rater must not see, and a score that moved with them would not be portable to another
system. Report their numbers *beside* the score, never folded into it.

They exist because three numbers worth watching per round had no cheap way to be read:

- **Own model time.** The runner's wall clock reports batch totals whenever a round is not
  serial, so `llm_traces.duration_ms` is the only per-item latency that means anything.
- **Citation compliance.** `citation_drop` events give a per-round count of handles the
  renderer refused, which says whether a prompt change broke citations — without grading a
  single answer.
- **Handles that never reached the drop log at all.** A citation mangled badly enough to stop
  matching the handle pattern is never parsed, never dropped, never logged, and reaches the
  reader as literal text. Counting `[[` that no well-formed token explains is the only way to
  see those.

**Run them against any round directory.** Pointing them at an older round is how a comparison
gets its baseline — every before/after figure in `results/2026-08-26-sbpeye/assessment.md` came
from running both over the previous round as well.

**Validate before trusting a zero.** The probes for retrieval vocabulary, preamble and closing
offers match *phrases*, and those phrases were read off the 2026-08-23 answers and the prompt
strings that produced them. Rewording a prompt retires its phrases, so a probe can go quiet
whether or not the behaviour stopped — the module docstring has a worked example from the
2026-08-26 round. Pointing the scanner at `results/2026-08-23-sbpeye/reruns` is the cheap check
that it still fires: that directory holds a known degenerate answer and the worst vocabulary
leak of that round.

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

The pilot tests the **rubric**, not the systems. A set this size cannot rank anything — the
question to answer is whether two raters independently produce the same score. If they do,
scale to ~60 items and start reporting comparisons. If they do not, the anchors need repair
first.

The first round (2026-08-19) already showed the instrument hitting its ceiling: every
substantive item scored full marks, so the set needs harder items before it can discriminate.
See that round's `assessment.md`.

Five items were added on 2026-08-23 in response to that finding — P11 through P15. Four of
them (P12–P15) are answered from a standing regulation or an Act rather than from a circular,
which the first ten items barely tested.

**Those items have now been through two scored rounds, and the set is at its ceiling again.**
The 2026-08-26 round scored 99.0 % with thirteen of fifteen items at 100 and a 0 % hallucination
rate — the same saturation that prompted P11–P15, one tier harder. Only P12 and P13 still
discriminate, and both lose the same half-point of completeness. **The set needs harder items
before another round is worth running**; a round it cannot fail measures nothing. See
`results/2026-08-26-sbpeye/assessment.md`.

Note also what two rounds have shown about round size: the 2026-08-23 reruns swung P13 by 100
points on an identical question. One run per item reports one sample of a distribution — decide
whether items run n times, and whether they score on worst case or median, before the set is
enlarged.

## Grade the reference, not the plumbing

Grounding is scored on whether the answer names the instrument an auditor can act on —
"BPRD Circular No. 05 of 2020", "Regulation-8". Internal database IDs, URLs and link
mechanics are implementation details that differ between systems; scoring them would punish
a rendering bug rather than a wrong answer, and would make the benchmark non-portable.

## Keeping the keys honest

Answer keys are pinned to a live corpus that SBP keeps adding to. Items marked ⏱ in the
answer key are freshness-sensitive and must be re-verified before every round — P06 in
particular cites a circular issued one day before the keys were written.
