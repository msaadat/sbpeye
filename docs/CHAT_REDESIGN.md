# Chat: a clean-slate design

`docs/CHAT_CONTEXT_PLAN.md` is the incremental plan — eleven items that make the current
loop fit a context window. This document asks a different question: given what SBPEye is —
a regulatory assistant that must be grounded in actual circulars and laws — what would the
chat loop, the tool schema and the context assembly look like if they were designed for that
job rather than arrived at.

It is written to be **buildable on today's foundation**: the SQLite corpus schema, the
BM25 + Chroma search engine, `CitationHandles`, and the SSE contract the frontend consumes
all stay. What changes is the loop, what the tools are, what they return, and what happens
after the answer is written.

Two assumptions, chosen deliberately:

- **The model floor is 128k.** Small local models are out of scope; the design does not
  contort itself for an 8k window.
- **The loop is hybrid.** Deterministic retrieval handles the common case; the model
  escalates to tools only when the fixed pass is not enough.

**Status:** design only, nothing built. §9 is the migration path; it assumes
`CHAT_CONTEXT_PLAN.md` C11 has landed first.

---

## 0. Tracker

| # | Step | Where | Effect | Effort | State |
|---|---|---|---|---|---|
| **R1** | Evidence card replaces the result payload | `ai.py:3500` `_search_result_payload` | fixed cost per document | M | ☐ |
| **R2** | Chain collapse: one card per amendment chain | `consolidation.py:61` `resolve_chain` | removes duplicate lineage members | S | ☐ |
| **R3** | Stage 1 plan call replaces iteration 1 | new | −1 round trip, checkable output | M | ☐ |
| **R4** | Stage 2 intent dispatch | new | retrieval becomes deterministic | L | ☐ |
| **R5** | Collapse the tool schema to three verbs | `ai.py:134` `TOOLS` | 11,415 ch → ~1,200 ch | M | ☐ |
| **R6** | Stage 4 verification | new | citation grounding, supersession check | M | ☐ |

**Prerequisite.** Status-aware ranking and amender annotation is **C11** in
`CHAT_CONTEXT_PLAN.md`. It belongs in the current design, needs nothing from this one, and every
step below assumes it has landed — a card built over a candidate set that still contains
withdrawn text inherits the problem.

**Order.** R1 and R2 first: they change what a result *is* without changing the loop. Then R3
and R4, which change the loop. R5 follows R4 (the tools can only shrink once the deterministic
path carries the common case). R6 any time after R1, because verification needs the evidence
set to check against.

---

## 1. How the numbers were taken

Corpus figures are read directly from `sbpeye.db` (3,655 circulars, 135 reg documents).
Behavioural figures come from `benchmarks/chat_context_audit.py` over `sbpeye_debug.db` — 34
traced chat turns, 2026-08-15 to 2026-08-25, on OpenRouter with
`deepseek/deepseek-v4-flash-0731`. See `docs/CHAT_CONTEXT_PLAN.md` §1 for that script's
caveats; they apply here too.

The status distribution in §2 was taken by walking every `tool_result` event, parsing the
search payloads, and counting the `status` field the payload already carries per result.

Everything in §7 marked *target* is derived from the card sizing in §5, not measured.

---

## 2. The finding that reframes the problem

**`status` appears nowhere in `search.py`.** Not in ranking, not in filtering, not in scoring.
`_apply_circular_filters` (`search.py:1093`) filters on year, department and tag — and that is
the complete list.

Of 3,655 circulars, 273 are `superseded` or `cancelled` and 757 more are `amended`. Retrieval
sees none of it. Measured over 1,399 result entries in the traced turns, **13.2% of what
reaches the model is withdrawn or replaced text**, and of the `amended` entries, **64.8% arrive
with none of their amenders named** — the `circular_relationships` graph knows the edge, and
retrieval never looks.

That fix belongs in the current design and is specified there: **`CHAT_CONTEXT_PLAN.md` C11**.
This document assumes it rather than restating it.

What matters *here* is the shape of the finding, because it generalises. The asymmetry is stark
inside one file: the **law arm is version-aware** — `_law_arm` filters
`RegDocument.delisted_at.is_(None)` (`search.py:1347`) and reads `current_version`, and
`RegDocumentVersion.is_current` is maintained per sync. The corpus that has a currency concept
uses it. The corpus that also has one ignores it.

And the graph that would drive it is already built and populated:

```
circular_relationships   3,172 edges
  adds_to    1,423        supersedes   362
  amends       838        cancels      114
  clarifies    435
reg_document_links         814   (circular ↔ law)
```

Nothing in the retrieval path reads any of it. Status is one instance; lineage, corpus routing
and currency are the others. A design that keeps asking the model to notice what the database
already knows will keep producing this class of bug, in a new place each time.

---

## 3. The design principle

The current loop asks the model to do four jobs at once:

1. **Formulate** what to look for
2. **Route** to the right corpus
3. **Select** which of ~30 candidates matter
4. **Compose** a grounded answer

Jobs 2 and 3 are the ones it does badly — 316 circulars offered across 30 turns, 45 cited,
**14.2%** — and they are the two that consume the window, because *selection requires
reading*. They are also the two the database can already do: routing from
`reg_document_links`, selection from status, relationships, dates and rank agreement.

Job 1 is a language task. Job 4 is the only thing an LLM is uniquely good at.

> **Separate language decisions from retrieval decisions.** The model plans and composes.
> Code retrieves, ranks, filters and verifies.

Every structural choice below follows from that one line.

---

## 4. The pipeline

```
  Stage 0  Frame            no LLM        ~5 ms
  Stage 1  Plan             1 small call  ~800 in / 200 out
  Stage 2  Retrieve         no LLM        ~50–200 ms
  Stage 3  Compose          1 streaming call, tools available
             ↳ escalate     ≤2 extra rounds
  Stage 4  Verify           no LLM (4 of 5 checks)  ~10 ms
```

Two provider calls for the common case, against four to six today.

### Stage 0 — Frame

Resolve the selection (workspace or pinned circulars), the session history, the corpus
version. Then run `SearchEngine._search_by_reference` (`search.py:1526`) over the question: if
it names an instrument, that document resolves **deterministically** and never enters a
similarity search. A question about "BPRD Circular No. 07 of 2019" is a lookup, not a
retrieval problem.

### Stage 1 — Plan

One cheap structured call producing a **retrieval plan**, not a tool call:

```json
{
  "intent": "definition | requirement | value | status | comparison | procedure | inventory",
  "queries": ["enhanced due diligence circumstances", "EDD high risk customers"],
  "corpora": ["circulars", "laws"],
  "named_instruments": ["AML/CFT/CPF Regulations"],
  "metric": {"name": "CAR", "subject": "MFB"},
  "as_of": "current",
  "exhaustive": false
}
```

This replaces iteration 1, which is entirely predictable: **30 of 30** traced turns called a
tool there, 28 of them a corpus search, and in **24 of 30** the query was at least half the
user's own words — at a median **4,539 ms** round trip (max 25,641 ms) to learn something the
server could have assumed.

Why a schema rather than a tool call:

- It is **checkable**. `corpora` and `intent` are enumerations; a malformed plan is rejected
  and re-asked, rather than discovered through a tool call that could not have succeeded.
- It is **cheap** — no documents in the input, ~1,000 tokens round trip.
- `intent` selects a retrieval strategy, and **the strategy is code**. This is the hinge on
  which "deterministic retrieval, agentic only when needed" turns.

The existing `focused_retrieval_query` (`chat_retrieval.py:88`) is the seed of the `queries`
field and can serve as the no-LLM fallback if the plan call fails.

### Stage 2 — Retrieve, rank, assemble

Intent-dispatched over the existing engine. No LLM.

| Intent | Strategy |
|---|---|
| `definition`, `requirement`, `procedure` | `dual_arm_search` → fuse → status-aware rank → chain collapse → document budget |
| `status` | `resolve_chain` + relationship graph. Pure SQL; no search at all |
| `comparison` | resolve both instruments, assemble both chains side by side |
| `value` | `CircularEntity` query — see the coverage caveat in §8 |
| `inventory` | inventory sweep, pointer rows only, table-shaped answer |

Three deterministic changes to ranking:

1. **Status-aware ordering — C11, already specified in `CHAT_CONTEXT_PLAN.md`.** Assumed here,
   not restated. What the card needs from it is the `amended_by` annotation, which §5 renders
   as a line on the card rather than a JSON field among thirty.
2. **Chain collapse (R2).** When several members of one amendment chain hit, return the
   *chain* — one card, current text, lineage attached — not the members competing with each
   other for rank. `consolidation.resolve_chain` (`consolidation.py:61`) already computes the
   closure.
3. **Corpus routing from the link graph (R4).** 814 `reg_document_links` edges make "which Act
   does this circular implement" a join rather than an inference. The prompt currently spends
   a paragraph warning that `get_circular_details` cannot fetch an Act; the graph makes the
   warning unnecessary.

Then the structural change that matters most: **budget by document, not by character.**

Character budgets produce the "twenty circulars at 2,000 characters each" failure. A document
budget — 6 to 8 cards — forces the selection decision into code, where it can be measured
against `benchmarks/pilot-v1-questions.md` instead of being delegated to a model that then has
to read everything to make it.

---

## 5. The evidence card

The single new data structure. One per document, fixed shape, generated entirely from data
that already exists.

```
[[c:BPRD-C-07-2019]]  BPRD Circular No. 07 of 2019
  "Enhanced Due Diligence Requirements for High Risk Customers"
  BPRD · 2019-05-14 · status: amended
  AMENDED BY  [[c:BPRD-C-03-2021]] (2021-02-11) — read this for current text
  IMPLEMENTS  [[l:AML-CFT-CPF-Regs]]
  ANNEXURES   A (14,200 ch) · B (3,100 ch) — NOT INCLUDED, use open_document
  RETRIEVED   lexical #2, semantic #1  (both arms agree)
  ── letter, para 4 ──────────────────────────────────
  <matched passage, whole — never a 25-word window>
  ── Annexure A, p.3 ─────────────────────────────────
  <matched passage>
```

What each line buys:

- **Fixed cost.** ~1,500–3,000 characters. Eight cards ≈ 20,000 chars ≈ 5,000 tokens, against
  72,000 characters for one `search_corpus` call today.
- **`AMENDED BY` is the card's reason for existing.** §2 measured that 64.8% of amended
  circulars reach the model with no amender anywhere in the result set. Today the only signal
  is a `status` field in a JSON blob among thirty, which says *that* something changed and
  never *what*. Here it is a machine-generated line naming the document that changed it, with
  the amender named so the model has a handle to open if the amendment matters. An
  `amended` circular is still the rule — the card is what stops it being read as the whole of
  the rule.
- **`ANNEXURES … NOT INCLUDED` makes the cover-letter trap explicit.** The current tool
  description spends 2,744 characters explaining that a letter announcing a change without
  stating its terms is a pointer rather than an answer. The card states the absence as a fact
  and names the tool that fixes it.
- **`RETRIEVED` lets a bad hit be discounted cheaply.** Rank agreement is the strongest signal
  the dual-arm design produces, and it currently has to be reconstructed by matching citations
  across two parallel lists.
- **Both ranks on one row** removes the 14.0% dual-arm duplication measured in
  `CHAT_CONTEXT_PLAN.md` §3.2 as a side effect, and preserves the agreement signal better than
  two independent lists do — agreement becomes a property of one row.
- **Passages are whole chunks, never windows.** The reasoning in `_inline_body_texts`
  (`ai.py:3405`) about term-density windows landing on the addressee block, and in
  `_passage_sets` (`ai.py:3441`) about windows lying on tables, is right and is preserved.
  What changes is that it applies to 8 documents rather than 20 under a shared budget.

The card replaces `_search_result_payload` (`ai.py:3500`) and `_law_search_payloads`
(`ai.py:3572`) with one serializer. A law card is the same shape with `IN FORCE` /
`full_text_chars` in place of the annexure line.

**Note on `summary`.** The current payload sends `circular.summary[:500]`. Six circulars of
3,655 have one (§8). The card should not carry a field that is null 99.8% of the time.

---

## 6. Stage 3 — compose, and the tool schema

System prompt + evidence cards + question, streamed. Tools are available but should rarely be
needed. The schema is **three verbs, ~1,200 characters**, against eight tools at 11,415:

```
open_document(handle, query?, section?)
    Read deeper into a document already on a card. Handle-addressed.

search_again(query, corpus, why)
    The plan was wrong. Retrieve differently, and say why.

insufficient(missing, searched_for)
    The corpus cannot answer this. A first-class outcome, recorded as one.
```

Three deliberate choices:

- **`open_document` unifies `get_circular_details` and `get_law_details`.** The handle already
  encodes the kind (`c` / `l` / `a`). Today's split is precisely why the prompt has to warn
  that one of them "CANNOT retrieve an Act — it searches circulars only, and asking it for one
  returns an unrelated circular that happens to mention the Act by name."
- **Handle-only addressing.** `get_circular_details` currently does reference-parse → exact
  match → title ILIKE → full search, with an ambiguity guard patching over the fuzziness. A
  handle taken from a card cannot resolve to the wrong document, so the guard, the fallback
  chain and the failure mode all disappear together.
- **`insufficient` makes "not in the corpus" a structured outcome.** `_ANSWER_CONTRACT`
  currently asks for this in prose — *"say plainly when you could not find something; that
  disclosure must survive"* — and hopes. A tool call is a record: it can be logged, counted,
  and used to drive corpus acquisition.

**Escalation budget: 2 rounds, not 5.** Iterations reached today is `{2: 4, 3: 6, 4: 11, 5: 9}`
— two thirds of turns run four or five rounds, and 15% of tool calls return only documents
already seen. With Stage 2 doing the retrieval, a third round means the plan was wrong twice,
which is a signal to stop and say so rather than to keep looking.

**Append-only stays.** Each escalation appends a new card set; nothing rewrites the prefix.
The prompt-cache measurement (94–97% hits on late iterations, `CHAT_CONTEXT_PLAN.md` §4) is
the reason, and it does not stop applying because the loop got shorter.

**The SSE contract is unchanged.** `meta` → `status` → `token`* → `done` / `error`, as
`main.py:3348`–`3387` emits today. Stage 1 and Stage 2 report through `status` events, which
is what the existing tool-activity UI already renders.

---

## 7. Stage 4 — verification

What a regulatory tool needs and what the system currently has almost none of. Five checks,
four of them pure SQL:

| # | Check | Mechanism | Today |
|---|---|---|---|
| 1 | **Resolution** — every handle resolves | `CitationHandles.expand` | partly, `_report_dropped_citations` (`ai.py:1166`) |
| 2 | **Grounding** — every cited handle was in the evidence set | set membership | **none** |
| 3 | **Quotation** — quoted strings appear in the evidence | `consolidation.value_supported` (`consolidation.py:84`) has this shape | none |
| 4 | **Supersession** — nothing cited is `cancelled`/`superseded`, or `amended_by` later than `as_of`, without saying so | one join | **none** |
| 5 | **Numeric** — stated figures agree with `CircularEntity` | query | none |

Check 2 is the important one and is currently a silent failure: a citation to a document that
was never retrieved expands into a working link and reaches the reader as a verified source.

Check 4 is the highest-value check in the system, and it is one join.

**Where it runs.** `StreamExpander` (`citation_handles.py:260`) already sits between the model
and the client, translating handles to real tokens mid-stream. Checks 1 and 2 belong there —
an unresolvable or ungrounded citation is withheld rather than rendered. Checks 3, 4 and 5 run
after the stream and annotate the `done` event, so the reader gets "this cites a circular
amended in 2021" as a footer rather than not at all. Failures annotate; only check 2 withholds.

---

## 8. What it costs, and what it depends on

### Targets

| | Today (measured) | This design (target) |
|---|---|---|
| Provider round trips per turn | 4–6 | **2**, 3 on escalation |
| Peak single request | 49,889 tok median, 243,584 max | **~8–10k tok** |
| Tool schema | 11,415 ch | ~1,200 ch |
| Documents shown per turn | median 29 | 6–8 |
| Withdrawn/replaced documents shown | **13.2%** | ~0% — *via C11, not this design* |
| Amended documents shown without their amender named | **64.8%** | ~0% — *via C11* |
| Turn latency | 37.8 s on the worked example | ~10–14 s |

The right-hand column is derived from the card sizing in §5. It is a design target, not a
measurement.

### The honest dependency list

The design leans on stored structure. Four of those tables are well populated and four are
effectively empty:

| Table | Rows | Verdict |
|---|---|---|
| `circular_relationships` | 3,172 | **Strong** — C11 and R2 work today |
| `reg_document_links` | 814 | **Strong** — corpus routing works today |
| `tags` | 3,009 of 3,655 | Strong |
| `attachments` | 1,471 | Strong |
| `circular_entities` | **57 rows, 7 circulars** | **Unusable** — the `value` intent is aspirational |
| `compliance_checklist` | **4 circulars** | Unusable |
| `summary` | **6 circulars** | Unusable — and the payload sends it anyway |
| `circular_consolidations` | 7 | Sparse — `resolve_chain` works, generated consolidation does not |

So: build Stages 0–4 on the relationship and link graphs, which are real. **Gate the `value`
intent behind an entity-coverage check** and fall back to `requirement` until extraction has
been run over the corpus. Do not put `summary` on the card until it exists.

That gate is worth stating as a rule rather than a special case: *an intent whose backing table
is below a coverage threshold degrades to the nearest general intent, and says so in the trace.*
Otherwise the first quantitative question after a partial extraction run gets a confident answer
from seven circulars' worth of data.

### Risks

- **Stage 1 misclassification.** A wrong `intent` picks a wrong strategy. Mitigated by the
  escalation path and by allowing a plan to name more than one intent; measurable against the
  pilot set.
- **Deterministic ranking is now on the hook for precision.** Today the model compensates for
  mediocre ranking by reading everything. Removing that crutch is the point, and it is also the
  main way this design could be worse than what it replaces. C11 should be measured against
  `benchmarks/score.py` and `benchmarks/check_citations.py` before R4 depends on it.
- **The card is a new serialization to tune.** Expect two or three rounds on what belongs on it.

---

## 9. Migration path

Each step is independently shippable and independently measurable.

1. **R1 — the evidence card**, behind the existing tools. Same loop, better payload. This is
   where `CHAT_CONTEXT_PLAN.md` C1a (suppress the second copy) lands, since one card per document
   makes duplication structurally impossible.
2. **R2 — chain collapse.** Small, and it depends only on R1 being in place to have somewhere
   to put the lineage.
3. **R3 — the plan call**, replacing iteration 1.
4. **R4 — intent dispatch.** Start with `definition` and `requirement`; add intents as they earn
   their place. This is the largest single piece of work here.
5. **R5 — collapse the tool schema** once R4 carries the common case.
6. **R6 — verification.** Checks 1, 2 and 4 first; they need no LLM and no new data.

C11 comes before all of them and is tracked in `CHAT_CONTEXT_PLAN.md`, not here.

### Relationship to `CHAT_CONTEXT_PLAN.md`

The incremental plan and this design are not alternatives — most of the plan survives into it:

| Plan item | Fate |
|---|---|
| C0 turn share | Stays, as the backstop for the escalation path. Matters less once the peak is 10k tokens |
| C1a suppress the second copy | **Survives** — becomes the card registry's write path |
| C1 document ledger | **Survives unchanged** — becomes the card registry |
| C2 tiered full text | Subsumed by the card |
| C3 supersession in synthesis | Subsumed — Stage 2 dedupes before the model ever sees it |
| C4 merged arms | **Retired** — merging refuses the dual-arm design's own argument; C1a dedups in place instead |
| C5 repeat-call guard | Survives — applies to `open_document` |
| C6 stop on no new document | **Survives** — becomes the escalation budget |
| C7 history as a contributor | Survives unchanged |
| C8 measure before sending | Survives as the backstop |
| C9 pre-warmed first search | Superseded by Stage 1 + Stage 2, which do the same thing better |
| C10 answer cache | Orthogonal — still worth building, keyed the same way |
| C11 status-aware ranking | **Prerequisite.** Do it in the current design; every step here assumes it |

The sensible reading is that C1a, C1, C6 and C7 are worth doing now under either plan, and C11
is worth doing now regardless of both.
