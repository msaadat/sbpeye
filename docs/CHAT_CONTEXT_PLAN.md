# Chat context budget

A chat turn is not one request. It is up to six, each re-sending everything the previous
ones accumulated, and the model's context window applies to **each request separately**. A
turn totalling 221,200 prompt tokens is fine on a 250k model; a turn whose *largest single
request* is 285,007 is not, and that request has been sent.

This document is about the peak, not the total. Cost is secondary and is noted only where it
changes a decision — which it does once, in §4.

For the clean-slate alternative — what this loop would look like designed for the job rather
than arrived at — see `docs/CHAT_REDESIGN.md`. The two are not exclusive: most of the items
below survive into it, and its §9 maps which.

**Status.** The turn-share work is **landed in the working tree** (uncommitted at the time of
writing): `resolve_turn_share()`, `_search_payload_budgets()`, the window cache, and chat
being sized by the model instead of by a dataclass default. 51 tests pass across
`tests/test_chat_turn_budget.py` and `tests/test_chat_synthesis_budget.py`. That work bounds
*how much* each contributor may spend. It does nothing about *what* is spent on, and
measured, a fifth of it is documents the model has already been given and an eighth of it is
text that has been withdrawn.

**C1a has landed** — a turn now hands the model each document's text once, worth 19.8% of
search output, and it absorbed C4. Nine items remain (C2 blocked on evidence, C4 retired).
**C11 has landed too** — withdrawn circulars are demoted to pointers rather than competing
for the evidence budget, and every changed circular now names what changed it: a measured
−18.1% on top, and the first item here that improves the *answer* rather than the request.
**C1 is next**, the largest context win and the one the rest build on.

---

## 0. Tracker

| # | Item | Where | Effect | Effort | State |
|---|---|---|---|---|---|
| **C0** | Turn share: every contributor sized against `window / 6` | `ai.py:1349` `resolve_turn_share` | turn fits by arithmetic | — | ☑ landed |
| **C0b** | Search payload ceilings scale with the share | `ai.py:1367` `_search_payload_budgets` | 72k ch → 13k ch on a 32k model | — | ☑ landed |
| **C0c** | Chat sized by the model, window probed once | `ai.py:4691` `get_ai_client_for_user` | replaces the 4,000 default | — | ☑ landed |
| **C1a** | Suppress the second copy of a document — lossless core of C1, absorbs C4 | `ai.py:3569` `_dedupe_repeat_row` | **19.8% of search output, lossless** | XS | ☑ landed |
| **C1** | Per-turn document ledger — stub a document already sent | `ai.py:4278` `_apply_tool_calls` | **21.4% of tool output is a repeat** | M | ☐ |
| **C2** | ~~Tier `full_circular_text` by rank~~ — **measured 1:1 against recall, do not ship as written** | `ai.py:3405` `_inline_body_texts` | see §5.2 | S | ⚠ blocked |
| **C3** | Supersession pass before `_fair_shares` | `ai.py:1856` | the one request never cached | S | ☐ |
| **C4** | ~~One merged result list carrying both ranks~~ — **retired into C1a**, see §5.4 | — | — | — | ✗ retired |
| **C5** | Repeat-call guard keyed on what was resolved | `ai.py:4278` | 2.2%, far more when stuck | S | ☐ |
| **C6** | Stop when an iteration adds no new document | `ai.py:4470` `_stream_chat_impl` | removes 1–3 requests | S | ☐ |
| **C7** | Charge conversation history to the turn budget | `main.py:3351` | the remaining unbounded input | S | ☐ |
| **C8** | Express the synthesis budget as a share, and measure before sending | `ai.py:1776`, `ai.py:1573` | **the guarantee** | M | ☐ |
| **C9** | Pre-warm the first search, skip iteration 1 | `main.py:3351` | −1 request, ~4.5 s | M | ☐ |
| **C10** | Answer cache: question + selection + corpus version | new | −1 whole turn on a hit | M | ☐ |
| **C11** | Withdrawn demoted, amender **annotated** | `search.py:1702` `dual_arm_search` | **measured net −18.1%; nothing becomes unreachable** | S | ☑ landed |

**Order.** **C1a and C11 are landed.** C1a was the smallest diff here and the only item that
is *lossless* by construction (19.8% of search output); C11 followed because it improves the
answer rather than the request, and it cleaned the ranking that C2 turns out to depend on.
**C1 is next** — the largest remaining context win, and C3, C5 and C6 all reuse its ledger,
which C1a already wrote the write path for. C5 and C6 are small once C1 exists. C7 and C8
close the two holes C0 left. C9 and C10 are latency work and can land any time.

**C2 is blocked on evidence**, not on effort. Measured, tiering the letter budget by rank costs
recall roughly 1:1 — see §5.2. Re-measure after C11 and decide then.

**Lossless before lossy.** C1a, C1 and C5 all remove bytes the model has already been
given. C2 removes bytes it has not. Spend the first group completely before touching the
second.

---

## 1. How the numbers were taken

`benchmarks/chat_context_audit.py` reproduces every figure here:

```bash
.venv/bin/python benchmarks/chat_context_audit.py
```

It reads `sbpeye_debug.db` — the trace recorder's own file, written whenever
`llm_debug_enabled` is true — and touches neither the corpus nor a provider. `--section`
narrows to `peak`, `fields`, `waste`, `cache`, `simulate` or `turn`; `--session` and
`--trace` dump one turn in full.

The corpus: **34 traced chat turns, 2026-08-15 to 2026-08-25**, 128 provider requests with
reported usage, 8,628,844 characters of tool output, all on OpenRouter with
`deepseek/deepseek-v4-flash-0731`.

**Four caveats, and they matter for reading everything below.**

1. **Every trace predates C0.** The absolute sizes in §2 are what the code did with
   unscaled ceilings; they are the *problem statement*, not the current behaviour. The
   *proportions* in §3 — how much of a payload is a repeat, how many offered circulars get
   cited — survive the change, because C0 shrank the budgets without changing what fills
   them.
2. **Token counts are estimated from characters at 4.0 chars/token.** That is the median
   over the 128 measured requests and tracks well in aggregate. The densest request observed
   ran at **2.93**, where `chars/4` underestimates by 37%. Reporting uses 4.0; anything used
   as a *ceiling* must use 3.0 — see §7.
3. **`--section simulate` is a simulation.** It replays each turn's real tool results through
   the proposed transformations and re-derives the peak. Validated against the traces it
   replays: simulated baseline over actual largest request, median 0.98 (min 0.93, max 1.44).
4. **Thirty turns, one model.** Enough to see structure and rank the items. Not enough to
   promise a percentage on a different corpus or a different model's tool-calling habits.

---

## 2. The shape of the problem

### 2.1 Peak request per turn, before C0

```
turns: 34
peak request  median  49,889 tok   p90  114,171 tok   max  243,584 tok

turns whose peak breaches a window:
  >    32,768 tok :  22  ( 65%)
  >    65,536 tok :  14  ( 41%)
  >   131,072 tok :   4  ( 12%)
  >   250,000 tok :   0  (  0%)
```

Those are character estimates. The provider's own count on the worst request was **285,007
prompt tokens** (2026-08-19, `chat.final_synthesis`) — so a 250k window had already been
breached once in 34 turns, and the estimate did not see it coming. That single data point is
why §7 insists on the conservative divisor.

The peak lands at iteration 4, iteration 5 or the synthesis in every one of the ten largest
turns. It never lands early.

### 2.2 What C0 changed

`resolve_turn_share()` divides the input budget by `_TURN_CONTRIBUTORS` (the selected-circular
context plus one tool result per round), so every contributor at its ceiling still fits:

| Window | Input budget | Share | 6 × share | `max_context_tokens` | Search response |
|---|---|---|---|---|---|
| 8,192 | 4,915 | 819 | 4,914 | 1,638 | 3,276 ch |
| 32,768 | 19,660 | 3,276 | 19,656 | 6,552 | 13,104 ch |
| 131,072 | 78,643 | 13,107 | 78,642 | 26,214 | 52,427 ch |
| **250,000** | 150,000 | **18,000** | **108,000** | 36,000 | 72,000 ch |
| 1,310,720 | 786,432 | 18,000 | 108,000 | 36,000 | 72,000 ch |

On a 250k model the tool contributions are now capped at ~108,000 tokens — 43% of the window
— plus the system prompt (~640 tok) and the tool schema (~2,853 tok). The `_TURN_SHARE_MAX_TOKENS`
clamp is what stops a million-token window from turning one lookup into a corpus dump.

**Two things the arithmetic does not yet cover**, and they are C7 and C8:

- **Conversation history is not a contributor.** `_ordered_chat_messages` replays every prior
  message of the session in full (`main.py:3209`, `main.py:3351`) and nothing charges it to a
  share. Sessions are short today — 42 messages total, largest session 9,568 chars — so it
  does not bite yet. It is unbounded by construction and will.
- **`_synthesis_evidence_budget` is still `resolve_context_budget() * 4` characters**
  (`ai.py:1776`) — 3,145,728 characters on the deployment's current model. It is bounded
  *transitively*, because the tools can no longer produce that much, but it is not itself a
  ceiling and it does not know history exists.

### 2.3 A worked example — session `3fd797e0`, trace `4514b69c`

*"What is Enhanced Due Diligence, and in what circumstances must an SBP regulated entity
apply it?"* No circulars selected. 37.8 s. Pre-C0.

| Request | Msgs | Size | prompt_tok | cached | new |
|---|---|---|---|---|---|
| `chat.iteration.1` | 2 | 13,500 ch | 3,341 | 0 | 3,341 |
| `chat.iteration.2` | 5 | 174,970 ch | 40,963 | 0 | **40,963** |
| `chat.iteration.3` | 7 | 181,192 ch | 42,348 | 40,960 | 1,388 |
| `chat.iteration.4` | 9 | 187,384 ch | 43,724 | 42,240 | 1,484 |
| `chat.iteration.5` | 12 | 197,045 ch | 46,366 | 43,520 | 2,846 |
| `chat.final_synthesis` | 3 | 190,317 ch | 44,458 | **0** | **44,458** |
| | | | **221,200** | 126,720 (57%) | 94,480 |

```
it.1 → search_corpus("Enhanced Due Diligence EDD circumstances when required")   79,569 ch
       search_corpus("enhanced due diligence high risk customers PEPs")           76,079 ch
it.2 → get_law_details(AML/CFT/CPF Regulations, "definition of EDD and …")         5,605 ch
it.3 → get_law_details(AML/CFT/CPF Regulations, "definitions clause …")            5,605 ch
it.4 → get_law_details(AML/CFT/CPF Regulations, "… section 2 …", section="2")      4,071 ch
       get_latest_circulars(department="BPRD", limit=10)                           4,347 ch
it.5 → get_law_details(AML/CFT/CPF Regulations, "\"EDD\" means … commensurate")    5,867 ch
```

The final request is three messages: a 1,842-char system prompt, the 128-char question, and
one **188,339-char user message** holding every tool result of the turn.

The answer was **1,778 characters**. Thirty-three circulars were offered; **one** was cited.

Three defects, each with an item below. C0 makes the payloads smaller; none of these three go
away, because each is about *what* fills a payload rather than how big it is allowed to be.

- `search_corpus` #2 was **72% redundant** — 54,495 of its 76,079 chars were documents
  already returned by #1 (8 of 16 circulars, 6 of 10 laws overlapped). → **C1**
- `get_law_details` ran **four times on the same instrument**. Calls 1 and 2 returned
  byte-identical results: chunks `[45,46,47,48,49]` both times. Across all four, 6,647 of
  17,355 passage chars (38%) were exact repeats. → **C5**
- `get_latest_circulars(BPRD)` has nothing to do with the question. It consumed an iteration
  and returned no document that was ever cited. → **C6**

---

## 3. Where the context goes

### 3.1 Per tool, per field

`--section fields`, across all traced calls. Absolute sizes are pre-C0; shares are not.

**`search_corpus`** — 22 calls, avg 72,189 ch

```
full_circular_text        37.9%     matching_passage_excerpt   3.4%
matching_passages         27.1%     title / url / tags         5.8%
law passages              11.5%     everything else           14.3%
```

**`search_circulars`** — 50 calls, avg 49,262 ch. `full_circular_text` 47.4%, `matching_passages` 20.3%.

**`get_circular_details`** — 50 calls, 97.1% `document_context`.

**`search_regulatory_inventory`** — 8 calls, avg 130,658 ch, max 574,624. `passage` 36.9% —
and note `_INVENTORY_PASSAGE_CHARS` is already 240 (`ai.py:109`), so that share is *row
count*, not passage length. Its budget now derives from the share (`ai.py:4142`), which is
the right fix; `_INVENTORY_MAX_ROWS` at 1,000 is the remaining loose end.

**`get_law_details`** — 15 calls, avg 5,099 ch, 83.5% passage text. The best-behaved tool in
the set. Its problem is entirely repetition.

**Tool schema** — 11,415 ch (~2,853 tok), re-sent every iteration; `search_corpus`'s
description alone is 2,744 ch. It sits inside the cached prefix from iteration 3 onward, so
it is nearly free there — but it is **80% of iteration 1's request**, and iteration 1 is
never cached.

### 3.2 Redundancy — what the budgets are being spent on

`--section waste`, across 8,628,844 chars of tool output:

| | Chars | Share |
|---|---|---|
| Same document re-sent in a later call | 1,843,571 | **21.4%** |
| Full letters for circulars never cited in the answer | 1,574,863 | **18.3%** |
| Search payload superseded by a later `get_*_details` | 383,815 | 4.4% |
| Identical repeat results | 190,217 | 2.2% |

Circulars offered by tools: **316**. Cited in an answer: **45**. **14.2%.**

Of the 1,105 search entries carrying a `full_circular_text`, the circular was cited in **88**
— **8%**.

**This is why C0 raises the value of the items below rather than lowering it.** Before the
share existed, a re-sent document was waste in a budget nobody was enforcing. Now the budget
is a hard division: on a 250k model a search response gets 72,000 characters, and roughly a
fifth of them go to documents the model already has. That fifth no longer merely inflates the
request — it *displaces evidence that would otherwise have fit*. Tighter budgets make
deduplication a retrieval-quality item, not just a size item.

### 3.3 What the budgets are being spent on that is no longer the rule

The same argument, taken one step further. Some of what fills a share is not merely a repeat —
it is text that has been withdrawn.

`status` appears **nowhere in `search.py`**. Not in ranking, not in filtering, not in scoring:
`_apply_circular_filters` (`search.py:1093`) filters on year, department and tag, and that is
the complete list. Measured across 1,399 search result entries handed to the model:

| Status | All positions | Top-3 only | Given a full letter |
|---|---|---|---|
| active | 50.8% | 51.0% | 53.3% |
| amended | 36.1% | 37.1% | 35.0% |
| **superseded** | **12.2%** | **11.0%** | **11.4%** |
| **cancelled** | **0.9%** | **0.9%** | **0.3%** |

**13.2% of every result set is superseded or cancelled text**, and 11.7% of the full covering
letters — the most expensive item in the payload — are for documents that no longer say
anything. The corpus itself is only 7.5% non-operative, so retrieval over-represents withdrawn
circulars by about 1.8×.

**`amended` is not in that category and must not be dropped.** It means something later
modified part of the circular; the circular is still the rule, and the base text is where the
bulk of the requirements live. `_recompute_statuses` (`circular_ai.py:63`) maps `supersedes` →
superseded and `cancels` → cancelled, and *every other edge type* to `amended` — including
`adds_to` (1,423 edges) and `clarifies` (435), where nothing was changed at all.

The failure with `amended` is showing it **alone**. Of 505 amended entries whose citation
resolves to the corpus, **327 — 64.8% — arrived with none of their amenders in the same result
set**: the model reads a figure that is still on the page of a circular still in force, and the
document that changed it is not in front of it.

That is a correctness item, not a context item, which is why C11 sits at the front of the order
rather than among the compaction work. It happens to also return 13.2% of every share.

---

## 4. The constraint that decides the design

The obvious fix — walk back through `full_messages` and drop or shrink earlier tool results
once they are superseded — is the wrong one, and the traces say so.

`--section cache`:

```
stage                      n      prompt      cached    hit    uncached
chat.final_synthesis       5     668,599           0   0.0%     668,599
chat.iteration.1          30      81,786      24,576  30.0%      57,210
chat.iteration.2          30     849,014      22,528   2.7%     826,486
chat.iteration.3          26   1,397,863     497,712  35.6%     900,151
chat.iteration.4          18   1,468,748     816,688  55.6%     652,060
chat.iteration.5           9   1,050,505     569,920  54.3%     480,585
```

On the worked example, iterations 3, 4 and 5 ran at **96.7%, 96.6% and 93.9%** cache hits.
`full_messages` is append-only, so the prefix is byte-stable and the provider caches it.
Growth is nearly free *on re-send*; it is expensive exactly twice:

1. **The iteration where a tool result is first appended** — iteration 2 in the example,
   40,963 tokens at 0% cached.
2. **The final synthesis**, which rebuilds the message list from scratch and forfeits the
   entire cache — 44,458 tokens at 0%.

Rewriting message *k* invalidates every cached token from *k* onward. A retro-compaction at
iteration 4 that saved 20k tokens of context would cost ~40k tokens of cache re-write — and,
more to the point here, **it would not lower the peak**, because the peak is the request
being assembled, not the one before it.

So every item below compacts in one of exactly two places:

- **At insertion time**, when a tool result is first appended. The prefix is untouched, the
  cache survives, the new message is smaller. → C1a, C1, C5
- **At synthesis time**, where the cache is already forfeit and compaction is free. → C3

---

## 5. The items

### 5.1 — C1. Per-turn document ledger

*`ai.py:4278` `_apply_tool_calls`. 21.4% of tool output is a document already sent. Effort M.*

`_apply_tool_calls` is the single choke point through which every tool result enters the
conversation, and `CitationHandles` already gives every document a stable per-turn key. Add a
ledger beside them:

```python
sent: dict[str, Fidelity]   # STUB < EXCERPT < PASSAGES < FULL_LETTER < DOCUMENT_CONTEXT
```

Before appending a result, walk its document entries. If a document is already in the ledger
at equal or higher fidelity, replace that entry with a stub:

```json
{"citation": "[[c:BPRD-C-07-2019]]", "lexical_rank": 3,
 "already_provided": "full text, in search_corpus #1 above"}
```

Only *upgrades* are written in full — a circular that arrived as a ranked excerpt and comes
back as a full letter is written, because the second copy carries something the first did not.

The fidelity ladder is what makes this safe. A naive "seen this citation, skip it" would
suppress the `get_circular_details` reading of a circular a search had already mentioned in
one line, which is the opposite of the intent.

**On the worked example:** `search_corpus` #2 drops from 76,079 to roughly 22,000 chars.
Under C0's share, that is not a smaller request so much as **54,000 characters returned to
the budget for documents the model has not seen** — the reframing in §3.2.

This also settles the cache-safe half of the supersession question: once `get_circular_details`
has delivered a circular at `DOCUMENT_CONTEXT` fidelity, any *later* search returning it emits
a stub rather than re-inlining the letter.

### 5.2 — C2. Tier `full_circular_text` by rank

*`ai.py:3405` `_inline_body_texts`. 18.3% of tool output; 8% of it gets cited. Effort S.*

The existing docstring is right: a 25-word term-density window on a two-page SBP letter
reliably picks the addressee block over the operative clause, and for a short letter the fix
is not a better window but no window. Nothing here disputes that.

What is wrong is the scope. The rule applies to every hit under one shared budget, so twenty
circulars each get a full letter and the answer cites one in twelve. 1,574,863 characters of
complete covering letters went to circulars no answer referenced.

The obvious fix is to tier by what the retrievers said — full letter for the top ranks,
passages below. **Measured, that trade is close to 1:1 and this item is not the free win it
looks like.** Cutoff `k` in either arm, over the 1,106 traced entries that carried a letter:

| Cutoff | Body chars saved | Cited letters that keep their body |
|---|---|---|
| ≤ 1 | 86.6% | 28/88 = 32% |
| ≤ 2 | 75.0% | 43/88 = 49% |
| ≤ 3 | 64.6% | 52/88 = 59% |
| ≤ 5 | 43.4% | 63/88 = 72% |
| ≤ 8 | 17.2% | 81/88 = 92% |

There is no knee. Saving tracks loss almost proportionally, which says **rank is barely
predictive of which letter the answer ends up using** — 41% of the letters actually cited were
ranked below 3 in both arms. That is a finding about the retrieval ranking, not about the
budget, and it is the same weakness `docs/CHAT_REDESIGN.md` §3 is built around.

So C2 should not ship as written. Two ways forward, in order of confidence:

1. **Take the lossless part first (C1a, §5.12).** Roughly 20% of search output is the identical
   bytes sent twice. Spend that before spending anything that costs recall.
2. **Then make rank worth tiering on**, via C11's status filter, which removes noise from the
   ranking. Re-run this curve afterwards. If a knee appears, tier at
   it; if it stays linear, the honest conclusion is that the letter budget should be spent on
   *fewer, better-chosen documents* — which is the evidence card in `CHAT_REDESIGN.md` §5, not a
   cutoff.

Dropping the body still leaves `matching_passages` and `attachment_text_chars`, so a demoted
circular remains citable and remains recognisable as a cover letter. The 41% above are letters
the answer would have had to cite from passages instead — not documents it would have lost. That
softens the risk; it does not make the trade free.

### 5.3 — C3. Supersession pass before `_fair_shares`

*`ai.py:1856` `_tool_result_synthesis_messages`. Effort S.*

`_tool_result_sections` hands every tool result to `_fair_shares`, which splits the budget
max-min fairly and clips what does not fit. The fairness argument is sound and well tested —
but it clips *blindly*. A search entry for a circular later read in full through
`get_circular_details` is clipped at the same rate as a unique source.

Dedupe before splitting: where a document appears in both a search payload and a
`get_*_details` payload, keep only the details version and reduce the search entry to its rank
line. Then fair-share the remainder.

This is the only item that may safely rewrite history, because the synthesis step rebuilds the
message list anyway and its cache hit rate is 0.0% across all five traced occurrences. It also
lands on the largest single request in the sample: the 285,007-token request was a
`chat.final_synthesis`.

### 5.4 — C4. ~~One merged result list carrying both ranks~~ — retired into C1a

*Do not merge the arms. Dedup them in place instead — §5.12.*

C4 originally proposed collapsing `lexical_results` and `semantic_results` into one list, each
row carrying both ranks. That was wrong on the design, and barely better on the numbers.

**On the design.** `dual_arm_search` (`search.py:1677`) exists *because* fusing was wrong for
chat. Its docstring: the title and recency bonuses are "an order of magnitude larger than the
entire RRF range", so "a circular that names the topic only in its body or an annexure
therefore cannot outrank one that names it in the title, however much better the retrieval
judged it to be. Handing the model both ranked lists keeps that signal intact and lets it
decide."

A single merged list has to be ordered by *something*. Whatever that something is — min rank,
RRF, lexical-first — it is a fusion decision, which is the decision this design deliberately
declined to make. Two lists carry two orderings for free.

The merge's claimed benefit was that agreement becomes a property of one row. It already is:
`lexical_rank` and `semantic_rank` are on every row of both lists, and they cost 1 ch/row each.

**On the numbers.** The duplicated *rows* are not the cost; the duplicated *evidence* is.
Across 237 duplicated rows in the traced turns:

| | Chars | Share of search output |
|---|---|---|
| Full second copy — what a merge removes | 643,268 | 12.62% |
| ├ `full_circular_text` | 268,091 | 5.26% |
| ├ `matching_passages` | 197,831 | 3.88% |
| └ everything else (506 ch/row) | 145,804 | 2.86% |

And that residual is mostly droppable too — 96 ch/row of `matching_passage`, 30 of
`matching_passage_excerpt` (both duplicated evidence), 64 of `url` (the citation handle is what
the model cites with; the URL is never used), 56 of `tags`.

Keep the row, drop the evidence, and a repeat lands at **~260 ch against 2,714** — about **90%
of what a merge would remove**, with both lists still readable top to bottom. That is C1a's
mechanism, not a separate item, so C4 is retired into §5.12 rather than kept as an alternative.

### 5.5 — C5. Repeat-call guard keyed on what was resolved

*`ai.py:4278`. 2.2% of tool output; 38% on the worked example. Effort S.*

Keying on the argument string does not work: the four `get_law_details` calls in §2.3 had four
different `query` values and two returned byte-identical results. Key on what the tool
*resolved to* — `(tool, resolved_document_id, returned_chunk_indices)` — which is known after
it runs and before its result is appended.

On a repeat, append a pointer:

```json
{"already_provided": "chunks 45-49 of [[l:AML-CFT-CPF-Regs]], see get_law_details #1",
 "hint": "ask for a different section, or a query aimed at a different provision"}
```

~150 chars instead of 5,605. The `hint` matters: a bare "duplicate" tells the model nothing
about how to make progress, and a model that cannot make progress spends the rest of the loop
discovering that.

### 5.6 — C6. Stop when an iteration adds no new document

*`ai.py:4470` `_stream_chat_impl` and the same loop in `_chat_impl` at `ai.py:4360`. Effort S.*

`_MAX_TOOL_ITERATIONS` is 5, unconditionally, with `tool_choice="auto"` every round.
Measured: **26 of 170 tool calls (15%) returned only documents already seen**, and iterations
reached is `{2: 4, 3: 6, 4: 11, 5: 9}` — two thirds of turns run four or five rounds. On the
worked example, iterations 3, 4 and 5 produced zero new documents; the turn should have ended
at iteration 3.

C1's ledger already computes the predicate: if a round contributed no document at a fidelity
above what the ledger held, go to synthesis instead of issuing another iteration. That removes
one to three requests per turn *and* — unlike everything else here — removes the requests that
would have carried the peak.

Twenty percent of turns currently exhaust the loop and pay for a `chat.final_synthesis` on
top. C6 makes most of those unnecessary.

### 5.7 — C7. Charge conversation history to the turn budget

*`main.py:3209`, `main.py:3351`. Effort S.*

`_ordered_chat_messages` returns every message of the session and both chat routes replay all
of them. Nothing bounds this and `_TURN_CONTRIBUTORS` does not count it, so C0's arithmetic —
"six shares is the whole of it, by construction" — is true of the tool results and silent
about the transcript in front of them.

Today that is harmless: 42 messages across all sessions, largest session 9,568 chars (~2,400
tokens). It stays harmless right up until someone has a twenty-turn conversation, at which
point the guarantee C0 provides quietly stops holding.

Make history the seventh contributor: keep the most recent turns that fit one share, and
replace what falls off with a one-line note that earlier turns were dropped. Oldest-first is
the right eviction order for a regulatory Q&A, where the current question is nearly always
self-contained — the traced sessions bear this out, with a median of two messages.

### 5.8 — C8. Make the ceiling a measurement, not an inference

*`ai.py:1776` `_synthesis_evidence_budget`, `ai.py:1573` `_create_traced_completion`. Effort M.
**This is the guarantee.***

C0 gives a bound by construction: six contributors, each capped at a share. That is a good
bound and it is not a measurement. Two things sit outside it — history (C7) and
`_synthesis_evidence_budget`, which is still `resolve_context_budget() * 4` characters
(3,145,728 on the current model) rather than a share — and neither the estimate nor the
arithmetic has ever been checked against what the provider actually counted.

Two changes:

1. **Express the synthesis budget as a share of the window**, the way C0 expressed everything
   else. It is transitively bounded today only because the tools can no longer produce enough
   to exceed it; that is a coincidence of two numbers, not an invariant.
2. **Measure at the provider boundary.** In `_create_traced_completion`, before the request
   goes out: estimate at `SAFE_CHARS_PER_TOKEN` (§7); compare against `resolve_context_budget()`
   with room reserved for the reply; if it fits, send. If not, shed in a fixed order — oldest
   stub-eligible tool results, then oldest history, then fair-share clipping with the "your
   record is partial" instruction `_tool_result_synthesis_messages` already writes — and emit
   a `context_shed` trace event so it is visible rather than silent.

Shedding invalidates the cached prefix, which is exactly why it must be a backstop rather than
a strategy: it should fire on the tail, not the median. That is what C1–C6 are for.

`_is_context_size_error` (`ai.py:1222`) already exists and already works — it drives
shrink-and-retry in checklist and entity extraction (`ai.py:2439`, `2819`, `2835`) and is
unused in chat. Wire chat's 413 into the same gate as a second line of defence, so a window we
mis-estimated costs one retry instead of the turn.

### 5.9 — C9. Pre-warm the first search

*`main.py:3351` and `main.py:3209`. −1 request, ~4.5 s. Effort M.*

Measured: **30 of 30** traced turns issued a tool call at iteration 1. Twenty-eight called a
corpus search first (`search_circulars` 20, `search_corpus` 8), and in **24 of 30** the query
was at least half the user's own words. Iteration 1's median round trip is **4,539 ms** (max
25,641 ms) to learn something the server could have assumed.

Run the search server-side before the first provider call, inject the result as a pre-supplied
block, and start the loop where iteration 2 is today. `focused_retrieval_query` in
`chat_retrieval.py` already exists for shaping the query, and `_chat_turn_circular_ids` already
does a smaller version of this by inferring referenced circulars. Keep the tools available so
the model can still search again with a better query — the pre-warm is a hint, not a
replacement.

**This does not lower the peak.** The first request grows from ~3,300 to ~40,000 tokens and the
tail is unchanged. It is a latency item, listed here because it is often mistaken for a context
item.

### 5.10 — C10. Answer cache

*New. −1 whole turn on a hit. Effort M.* See §6.

### 5.11 — C11. Status-aware ranking and amender annotation

*`search.py:1702` `dual_arm_search`; `_relationship_annotation` (`search.py:973`) and
`_withdrawn_pointer` (`search.py:1023`); `WITHDRAWN_QUERY_PATTERN` (`chat_retrieval.py:55`);
`_withdrawn_section` (`ai.py:1108`). **Measured net −18.1% of search output**, plus the
correctness win in §3.3. Effort S. **☑ Landed**, pinned by `tests/test_search_currency.py`
(29 tests).*

Two clauses, and they are not the same clause.

**Demote what has been withdrawn — do not hide it.** `superseded` and `cancelled` circulars
stop competing for the evidence budget, but they must remain *findable*. A regulatory tool that
cannot answer "what did the old rule say?" has traded one failure for another, and answering
"that is not in the corpus" about a document the corpus holds is the worse of the two.

Three rules, and the first is the one that matters most:

1. **Never filter a document the user named.** `reference_matches` (from
   `_search_by_reference`, `search.py:1526`) and `get_circular_details` bypass the status filter
   entirely. They are separate paths from the two ranked arms, so this is a matter of *where*
   the clause goes, not an exception to it. If someone asks about BPRD Circular 12 of 2015, they
   get BPRD Circular 12 of 2015 — with its withdrawal stated, never withheld.
2. **Ranked hits are demoted to a pointer list, not deleted.** Withdrawn matches leave the two
   arms and land in `withdrawn_matches`: citation, title, date, status, and what replaced it —
   no body, no passages, no excerpt.

   ```json
   {"citation": "[[c:BSD-C-08-2006]]", "title": "…", "date": "2006-04-11",
    "status": "superseded", "superseded_by": "[[c:BPRD-C-07-2019]]",
    "note": "matched this query but is no longer in force; open it if the question is historical"}
   ```

   Measured: the 184 withdrawn entries in the traced turns cost **517,902 ch as full entries
   (2,814 ch each)**; as pointers they cost a twentieth of that, leaving every withdrawn hit
   one `get_circular_details` call away.

   **The slice must come before the split.** Filtering the arm and *then* taking the top
   `limit` pulls the next-ranked circular up into the vacated slot, and because that
   replacement carries a body and passages of its own the response ends up the same size —
   a hit lost for nothing. Measured on the live corpus, filter-then-slice moved the payload
   only **−2.5%**; partitioning the top `limit` moved it **−20.5%**. This is the single
   detail on which the whole clause turns, and it is pinned by
   `test_the_arm_does_not_refill_the_vacated_slot`.
3. **An explicitly historical question turns the demotion off.** A query naming a past year, or
   using "previously / earlier / used to / superseded / replaced / withdrawn", keeps withdrawn
   circulars in the arms. `FRESHNESS_QUERY_PATTERN` (`chat_retrieval.py:37`) is the existing
   precedent for a query-pattern switch of exactly this kind.

   One piece of care in the year clause: every SBP reference *ends* in a year — "BPRD Circular
   No. 07 of 2019" — so matching years indiscriminately would switch the demotion off for
   almost every question that names a circular. The pattern excludes the reference form, where
   the year is part of a name rather than a period being asked about.

**Why demotion rather than exclusion.** Measured over 143 result arms in the traced turns:
filtering empties **0%** of them and leaves **0%** with fewer than three results — so the
"nothing left to answer from" fear is unfounded. But it removes the **#1 ranked hit in 12.6%**
of arms. In those, a hard filter would make the best-matching document silently invisible; a
pointer row makes it visible at 5% of the cost, and the model can open it when the question
turns out to be about the old rule.

**The worked case, and the test this item must pass.** *"BC & CPD Circular No. 08 of 2021"* is
`superseded` — withdrawn by `BPRD Circular No. 04 of 2025` on 2025-10-17. Run today, the three
lists come back:

```
reference_matches   BC & CPD Circular No. 08 of 2021   (superseded)   ← the only one that has it
lexical_results     BC & CPD Circular Letter No. 01 of 2022, BC&CPD Circular No. 03 of 2015,
                    BC & CPD Circular Letter No. 02 of 2018, …        (all active, all irrelevant)
semantic_results    BPRD Circular No. 04 of 2025, EPD Circular Letter No. 09 of 2025, …
```

**Neither ranked arm returns it at all.** `reference_matches` is the *only* path by which a
directly-named circular reaches the model, which makes rule 1 load-bearing rather than a
courtesy: a filter applied uniformly across all three lists would answer this question with
five irrelevant active circulars and no mention of the one that was asked about.

What the user should get instead is the document, with its withdrawal stated:

```json
{"citation": "[[c:BC-CPD-C-08-2021]]", "reference": "BC & CPD Circular No. 08 of 2021",
 "date": "2021-08-16", "status": "superseded",
 "superseded_by": {"citation": "[[c:BPRD-C-04-2025]]", "date": "2025-10-17"},
 "note": "withdrawn; quote it only as the position at the time"}
```

Note the semantic arm *did* surface `BPRD Circular No. 04 of 2025` — the circular that replaced
it — at rank 1, with nothing to say it was related. The `supersedes` edge is in the database.
Rule 2's `superseded_by` field is what turns that coincidence into a statement.

Pin all three behaviours: a named withdrawn circular is returned; a withdrawn circular that only
*matched* appears in `withdrawn_matches` and not in an arm; and `get_circular_details` on a
withdrawn circular returns it with the withdrawal named.

**Name the amender on the row.** Every changed result carries the circulars that changed it as
citations — `amended_by` for one still in force, `replaced_by` for one that is not, with
replacement winning when both edges exist. The document is not fetched:

```json
{"citation": "[[c:BPRD-C-07-2019]]", "status": "amended",
 "amended_by": [{"citation": "[[c:BPRD-C-03-2021]]", "date": "2021-02-11", "type": "amends"}],
 "note": "read the amending circular before quoting a figure from this one"}
```

**Cap the fan-out.** Median fan-out is 1 and p90 is 3, but `BSD Circular No.18 of 2001` has
**266 amenders**. Uncapped, annotating that one row costs ~16,000 ch. `MAX_NAMED_AMENDERS`
names the 3 most recent and `older_changes_not_shown` counts the rest.

**What it actually cost, measured.** Ten representative queries against the live corpus,
dual-arm, `limit=10`, counted after handle rewriting — which is what reaches the model:

| | Chars | Share |
|---|---|---|
| baseline (withdrawn in the arms, no annotation) | 685,689 | — |
| demotion: 36 full entries become pointers | −140,708 | **−20.5%** |
| amender annotation on 70 rows, 237 ch each | +16,558 | +2.4% |
| **net** | **561,539** | **−18.1%** |

Two deviations from the sketch above, both worth recording. The pointers cost **~300 ch, not
~140**: `title` is kept because it is the whole of what tells the model whether a withdrawn hit
is worth opening, and `reference` because `get_circular_details` takes a reference string rather
than a handle — dropping it would leave the model unable to act on the pointer it was given.
And the instruction that goes with the list is emitted **once per response**
(`withdrawn_matches_note`) rather than once per pointer, for the reason C1a exists.

Two things this must *not* do:

- **Do not drop `amended` results.** They are still the rule. Dropping the base text where the
  requirements live, to avoid a figure that may have moved, is a worse failure than the one
  being fixed.
- **Do not demote them either.** The `amended` bucket includes every target of an `adds_to` or
  `clarifies` edge — 1,858 of the 3,172 relationships — where nothing was changed. Ranking a
  circular lower because someone once clarified it is noise, not signal.

**What makes the annotation enough.** It converts a silent failure into a visible one: the
model can no longer read an amended circular without being told, imperatively, that something
changed it — and it is handed the handle. Fetching the amender is an escalation the loop
already supports, and after C1a and C5 it is cheap. What makes it *safe* is not pre-fetching
but checking afterwards: an answer that cites an `amended` circular and never mentions the
amendment is one join away from being flagged. That check is `CHAT_REDESIGN.md` §7 check 4, and
it costs nothing.

**Interaction with the rest of the plan.** C11 lands *before* C1 on purpose: C1 decides what to
keep out of a fixed budget, and it is worth having it make that decision over a candidate set
that no longer contains withdrawn text.

**How to verify.** This is the one item here whose effect is on answer quality rather than
request size, so `--section simulate` will not show it. Measure it with
`benchmarks/run_pilot.py` and `benchmarks/check_citations.py` against
`benchmarks/pilot-v1-questions.md`, and re-run `chat_context_audit.py --section waste` to
confirm the payload reduction separately.

### 5.12 — C1a. Suppress the second copy of a document (the lossless core of C1)

*`ai.py:3569` `_dedupe_repeat_row`, spent by `_search_result_payload` and
`_law_search_payloads`; ledger on `AIClient`, reset in both chat loops.
**19.8% of search output**, lossless. Effort XS. **☑ Landed**, pinned by
`tests/test_turn_text_ledger.py` (15 tests).*

C1's fidelity ladder is effort M because deciding whether a second copy is an *upgrade* needs
the ladder. But a large part of the duplication needs no such judgement, because the second
copy is byte-identical to the first:

| | Full letters | Passages | Total | Share of search output |
|---|---|---|---|---|
| Same circular serialized in **both arms of one call** | 260,545 ch | 197,831 ch | 458,376 ch | **9.0%** |
| Same circular re-sent in a **later call of the same turn** | 380,112 ch | 172,597 ch | 552,709 ch | **10.8%** |
| Combined | 640,657 ch | 370,428 ch | **1,011,085 ch** | **19.8%** |

`_inline_body_texts` already dedupes its *budget* by `circular.id` — the docstring says so, and
`test_budget_is_charged_once_for_a_circular_in_both_arms` pins it. What is not deduped is the
*serialization*: `_search_result_payload` is called once per arm and looks the same body up
both times, so the letter goes on the wire twice. `_passage_sets` charges twice on purpose
(`test_passage_budget_charges_a_circular_served_in_both_arms_twice`), on the argument that the
two lists must be independently readable.

**The fix keeps both lists, both rank fields, and every row in place.** Only the evidence moves.
A repeat row keeps what identifies it and drops what repeats:

```json
{"citation": "[[c:BPRD-C-07-2019]]", "reference": "BPRD Circular No. 07 of 2019",
 "title": "Enhanced Due Diligence Requirements", "date": "2019-05-14", "status": "amended",
 "lexical_rank": 7, "semantic_rank": 2, "evidence_provided_above": true}
```

Dropped on a repeat: `full_circular_text`, `matching_passages`, `matching_passage`,
`matching_passage_excerpt` — all four are duplicated evidence — plus `url` and `tags`, which
carry nothing the first copy did not. Measured, that takes a repeat from **2,714 ch to ~260 ch**:

| | Chars | Share of search output |
|---|---|---|
| `full_circular_text` on a repeat | 268,091 | 5.26% |
| `matching_passages` on a repeat | 197,831 | 3.88% |
| window text, `url`, `tags` on a repeat | ~90,000 | ~1.8% |
| **removed** | **~556,000** | **~10.9%** *(intra-call)* |
| kept, so both lists stay readable | ~62,000 | ~1.2% |

That is **~90% of what merging the two arms would remove**, without merging them — see §5.4 for
why merging is the wrong trade. Add the cross-call half and the item reaches the 19.8% above.

Scope is the **turn**, not the call, so the same mechanism collects both rows of the table.
`AIClient` is constructed per request and serves exactly one turn, so
`self._sent_text_keys: dict[str, list[str]]`, reset at the top of `_chat_impl` and
`_stream_chat_impl`, is enough — the same shape as the existing `self._context_budget`.

**What shipped, against the sketch above.** Two differences, both deliberate:

- The marker is **two keys, not one**: `duplicate_of_earlier_entry: true` says the row is a
  repeat, and `text_provided_earlier: ["full_circular_text", ...]` names what the first row
  carried. One boolean was not enough. A stripped row with no pointer reads as a document
  whose text is *unavailable*, and the model answers that — or spends a
  `get_circular_details` round recovering what is already in its own context, which costs
  more than the duplicate did. `matching_passage_excerpt` is dropped but not named: it is a
  window on text the pointer already names.
- The ledger stores **which text keys went out**, not just that the document was seen, which
  is what lets the pointer be specific and is the shape C1's fidelity ladder needs anyway.

**The allocators had to change too, and this is the part that is easy to get wrong.**
Withholding at serialization alone would leave `_inline_body_texts`, `_passage_sets` and the
law loop *charging* their ceilings for bytes that never leave — so a second search call would
have spent its whole letter budget on documents it then stripped, and the genuinely new
circulars behind them would arrive with no text at all. That trades a context saving for a
worse answer, which is the one outcome this item must not have. All three now skip the charge
for a document already sent, and `_passage_sets` charges **one** copy rather than one per arm,
because one is now what goes on the wire. `test_a_withheld_letter_does_not_consume_the_inline_budget`
and its two siblings pin it.

**Known limit.** A later row cannot *upgrade* an earlier one: a circular first seen with only
an excerpt keeps the excerpt even if a later search would have matched real passages. Deciding
when a second copy is an upgrade is exactly the judgement C1's fidelity ladder exists to make,
and this item is deliberately the part that needs none.

Why it went first:

- **It is lossless by construction.** The model is not being asked to work from less; it is
  being asked not to read the same letter twice. Nothing else in this plan can say that.
- **It is the smallest diff here.** One new function, three allocator guards, one instance
  attribute, one paragraph in the `search_corpus` description. No existing test changed.
- **It is C1's mechanism at reduced scope.** When the fidelity ladder lands, `_sent_text_keys`
  becomes the ledger and `_dedupe_repeat_row` is its write path, not something thrown away.
- **It absorbs C4.** Retiring the arm merge into this item (§5.4) means the dual-arm
  duplication is fixed here, in place, without a fusion decision `dual_arm_search` deliberately
  declined to make.

Two care points: the date-sorted branch of `search_corpus` (`ai.py:3673`) passes a single
`results` list and must share the same set; and `reference_matches` is a third list that can
carry the same circular as either arm, so it has to participate too.

---

## 6. In-app caching — what actually saves a request

Four things get called "a cache" here. Only two save a provider round trip, and the cheapest
to build saves the least.

**(a) Tool-result cache**, keyed on tool + normalised arguments.
Saves *tool execution*, not a request — the model still has to see the result, so the round
trip happens either way. Since the `PERFORMANCE_PLAN` work, circular search is ~19 ms and law
search ~28 ms, so the saving is milliseconds against a 4.5 s round trip. **Not worth building
on its own.** C5 delivers the part that matters — not re-sending the *payload* — at the point
where it also returns budget.

**(b) The per-turn document ledger (C1).**
A cache in substance — "what has this turn already told the model?" — but its payoff is
context, not requests. The most valuable item in this document, and it saves zero round trips.

**(c) Pre-warmed first search (C9).**
Saves one request per turn, every turn, because iteration 1 is 100% predictable in the sample.
Latency, not context.

**(d) Answer cache (C10) — the only one that saves a whole turn.**

Key on everything that can change the answer:

```
sha256(normalised_question, sorted(selected_circular_ids), corpus_version, model_id, prompt_version)
```

- **`corpus_version` is non-negotiable.** This is a regulatory tool; an answer cached before a
  circular was superseded is worse than no answer. A monotonic counter bumped by the scraper
  and by any supersession or re-index run is enough, and it makes invalidation a comparison
  rather than a policy.
- **`prompt_version` likewise.** `_CITATION_RULES` and `_ANSWER_CONTRACT` change, and a cached
  answer from the previous contract should not survive the change.
- **Store in the application database** beside `chat_messages`, not in memory: the deployment
  restarts, and a cache that empties on every deploy will never be measured doing anything.
- **Serve a hit as a normal assistant message, flagged** in the response payload so the UI can
  say so. A silently cached regulatory answer is a support ticket waiting to happen.

**Expected hit rate, honestly:** 2 of 19 distinct questions in the traced history repeat
verbatim — 4 of 21 user messages. That is inflated by testing against
`benchmarks/pilot-v1-questions.md`, and that is also where the value is concentrated: a pilot
re-run, a rubric scoring pass or a demo replays the same question set repeatedly, and each
replay currently costs a full multi-request turn. In production the hit rate will be lower. A
semantic key — embedding the question, hitting above a similarity threshold — would raise it
and is the obvious follow-up, but exact-match should land first because it cannot be wrong.

**One cache is already working and must not be broken.** The provider's own prompt cache runs
at 35.0% across the sample and 94–97% on late iterations. §4 is the whole story: compact at
insertion or at synthesis, never in between.

---

## 7. Estimating tokens safely

Every ceiling in C8 depends on turning characters into a token count that is never too low.

Measured over 128 chat requests where both the character count and the provider's
`prompt_tokens` are known:

```
chars per prompt token:  min 2.93   p10 3.43   median 3.99   max 4.46
```

`chars // 4` is an excellent *estimator* — median 0.997× the true count — and an unsafe
*ceiling*. On the densest request in the sample, 282,847 characters were 96,649 tokens;
`chars // 4` predicts 70,711, a 37% underestimate. Density that high comes from tables,
reference strings and citation handles, which is what a large tool result is made of.

So:

- **Report** with 4.0. `_estimate_tokens` (`ai.py:1334`) stays as it is for batch sizing.
- **Budget** with 3.0 — `SAFE_CHARS_PER_TOKEN` in the audit script.
- **Better: calibrate per turn.** Every provider response carries `usage.prompt_tokens` for a
  request whose exact character count is known. After iteration 1 the true ratio for *this*
  conversation's text is measured rather than assumed. Use `min(3.0, measured × 0.9)` from
  iteration 2 onward. It costs nothing and is strictly more accurate than any constant.

Worth noting against C0: `resolve_turn_share()` divides a token budget and
`_search_payload_budgets()` multiplies it by 4 to reach characters. That 4 is the median
ratio, so on dense content a share can overshoot its token intent by up to a third. Six shares
overshooting together is how a bound by construction becomes a breach — which is the argument
for C8 in one sentence.

---

## 8. What lands when

**First:** C1a. Smallest diff in this document, lossless by construction, 19.8% of search
output. One serializer, two call sites, one instance attribute, one existing test inverted.

**Then:** C11. The only item that improves the answer rather than the request, and it shrinks
the candidate set every later item has to budget. Measure it with the pilot set, not with
`--section simulate`.

**Then:** C1. Largest context win, and C3, C5 and C6 all reuse its ledger — C1a is already its
write path.

**Not C2, and not C4** — C4 is retired into C1a (§5.4), and C2's curve should be re-measured
after C11 before anyone decides on it.

**Then the small ones:** C5 and C6, both cheap once C1 exists.

**Then close C0's two holes:** C7 (history is a contributor) and C8 (the synthesis budget is a
share, and the estimate is checked before sending).

**Any time:** C9 and C10, which are latency and independent of everything above.

Re-run `benchmarks/chat_context_audit.py --section simulate` after each landing. The
simulation replays real traces, so once an item ships its column should collapse toward the
baseline — and if it does not, the implementation and the model of it have diverged.

Traces recorded after C0 will show smaller absolute sizes than §2; the redundancy proportions
in §3.2 are the numbers to watch, because those are what these items move.
