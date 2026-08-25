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
measured, a fifth of it is documents the model has already been given.

Nine items remain. C1 is the largest and the one the rest build on.

---

## 0. Tracker

| # | Item | Where | Effect | Effort | State |
|---|---|---|---|---|---|
| **C0** | Turn share: every contributor sized against `window / 6` | `ai.py:1349` `resolve_turn_share` | turn fits by arithmetic | — | ☑ landed |
| **C0b** | Search payload ceilings scale with the share | `ai.py:1367` `_search_payload_budgets` | 72k ch → 13k ch on a 32k model | — | ☑ landed |
| **C0c** | Chat sized by the model, window probed once | `ai.py:4691` `get_ai_client_for_user` | replaces the 4,000 default | — | ☑ landed |
| **C1** | Per-turn document ledger — stub a document already sent | `ai.py:4278` `_apply_tool_calls` | **21.4% of tool output is a repeat** | M | ☐ |
| **C2** | Tier `full_circular_text` by rank instead of all hits | `ai.py:3405` `_inline_body_texts` | **18.3% of output, 8% cited** | S | ☐ |
| **C3** | Supersession pass before `_fair_shares` | `ai.py:1856` | the one request never cached | S | ☐ |
| **C4** | One merged result list carrying both ranks | `ai.py:3441` `_passage_sets` | 14.0% of `search_corpus` | S | ☐ |
| **C5** | Repeat-call guard keyed on what was resolved | `ai.py:4278` | 2.2%, far more when stuck | S | ☐ |
| **C6** | Stop when an iteration adds no new document | `ai.py:4470` `_stream_chat_impl` | removes 1–3 requests | S | ☐ |
| **C7** | Charge conversation history to the turn budget | `main.py:3351` | the remaining unbounded input | S | ☐ |
| **C8** | Express the synthesis budget as a share, and measure before sending | `ai.py:1776`, `ai.py:1573` | **the guarantee** | M | ☐ |
| **C9** | Pre-warm the first search, skip iteration 1 | `main.py:3351` | −1 request, ~4.5 s | M | ☐ |
| **C10** | Answer cache: question + selection + corpus version | new | −1 whole turn on a hit | M | ☐ |

**Order.** C1 first — it is the largest single win and C3, C5 and C6 all reuse its ledger.
Then C2 and C4, independent local edits to the search payload. Then C5 and C6, both small
once C1 exists. C7 and C8 close the two holes C0 left. C9 and C10 are latency work and can
land any time.

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
  cache survives, the new message is smaller. → C1, C2, C4, C5
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

Tier by what the retrievers actually said:

- **rank ≤ 3 in either arm** → full letter, as today
- **rank 4–10** → matched passages only
- **below that** → title, reference, date, summary, citation

`attachment_text_chars` stays on every tier, so a low-ranked circular with annexures is still
recognisable as one and the model can spend a `get_circular_details` call on it — the intended
path, which C1 has just made cheap.

Under C0 this matters more, not less: on a 32k model the inline-body ceiling is 7,280
characters, which is one and a half letters. Choosing *which* one and a half is now the whole
of the decision.

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

### 5.4 — C4. One merged result list carrying both ranks

*`ai.py:3441` `_passage_sets`, `ai.py:3653`. 14.0% of `search_corpus` output. Effort S.*

`_passage_sets` charges a circular appearing in both arms twice, deliberately: *"the lists are
meant to be readable independently"*. Measured, that is 223,133 chars across 22 calls, with a
median of 2 circulars overlapping out of 10 + 10.

Emit one list per circular carrying both `lexical_rank` and `semantic_rank`, with `null`
meaning *this arm did not return it*. Every bit of the agreement signal the dual-arm design
exists to preserve is still there and arguably more legible — agreement becomes a property of
one row instead of something the reader reconstructs by matching citations across two lists.

The tool description at `ai.py:134` needs the corresponding edit, which also trims the largest
description in the schema.

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

**First:** C1. Largest single win, and C3, C5 and C6 all reuse its ledger.

**Then, independently:** C2 and C4 — local edits to the search payload, neither depending on
the other.

**Then the small ones:** C5 and C6, both cheap once C1 exists.

**Then close C0's two holes:** C7 (history is a contributor) and C8 (the synthesis budget is a
share, and the estimate is checked before sending).

**Any time:** C9 and C10, which are latency and independent of everything above.

Re-run `benchmarks/chat_context_audit.py --section simulate` after each landing. The
simulation replays real traces, so once an item ships its column should collapse toward the
baseline — and if it does not, the implementation and the model of it have diverged.

Traces recorded after C0 will show smaller absolute sizes than §2; the redundancy proportions
in §3.2 are the numbers to watch, because those are what these items move.
