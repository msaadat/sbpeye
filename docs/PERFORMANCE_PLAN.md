# UI performance

The app is deployed for test users and the LLM paths are slow by nature — that is expected and
is not what this document is about. This is about everything *else* the user waits on: the
bytes before first paint, the search round trip, the detail pane, and the frames dropped while
an answer streams.

Fifteen items, measured rather than guessed, ordered by what a user actually feels per unit of
work. They are deliberately independent: each can land, ship and be verified on its own.

**Status:** P1, P2, P3, P4, P5 and P7 landed — circular search is **132 ms → 22 ms**, the
landing route ships 206 KB instead of 804 KB, and the laws list went from 273 queries to 7.
Next up is P6, the one that starts mattering when two testers use the app at once.

---

## 0. Tracker

| # | Item | Where | Measured effect | Effort | State |
|---|---|---|---|---|---|
| **P1** | Enable response compression | `main.py:480` | **804 KB → 206 KB on the wire** | XS | ☑ landed |
| **P2** | `best_window` is O(words × window × tokens) | `search.py:453` | **12–14× on the function** | S | ☑ landed |
| **P3** | Drop the Chroma pre-filter (circular arm) | `search.py:965` | **61.1 ms → 7.9 ms** | S | ☑ landed |
| **P3b** | Law arm still pays the pre-filter | `search.py:1091` | over-fetch starves — §14 | M | ☐ deferred |
| **P4** | SQLite WAL + `busy_timeout` | `database.py` | writers stop blocking readers | S | ☑ landed |
| **P5** | `/api/laws` loads 3.5 MB it never sends | `serializers.py:169` | **273 queries → 7, 4.88 MB → 0** | S | ☑ landed |
| **P6** | 25 `async def` routes block the event loop | `main.py` | removes a 5 s global stall | S | ☐ |
| **P7** | `Cache-Control` on hashed assets | `main.py:517` | **14 revalidations → 0** | XS | ☑ landed |
| **P8** | Landing route is a 4-deep request chain | `CircularsView.vue` | 4 serial RTTs → 2 | M | ☐ |
| **P9** | Chat re-renders the thread on every token | `ChatView.vue:943` | stream stutter | M | ☐ |
| **P10** | `/api/circulars/{id}` N+1 + blob reads | `main.py:1426` | 102 queries → 2 | S | ☐ |
| **P11** | Markdown re-parsed on every render | `SummarySection.vue:41` | −84 KB initial, less churn | S | ☐ |
| **P12** | Google Fonts blocks first render | `index.html` | one cross-origin RTT | XS | ☐ |
| **P13** | `_scan_documents` previews whole attachments | `search.py:1345` | the last of the 22 ms — §15 | S | ☐ |
| **P14** | Law search is 5× slower than circular search | `search.py:1261` | **121 ms → ~25 ms** — §16 | S | ☐ |

**Order.** Done so far: P1 + P7 (pure configuration, largest win for the least code), then
P2 + P3 (the search story, verified once rather than twice).

Remaining, in the order worth taking them: **P6** next — invisible to a single-user benchmark,
and the one that starts mattering the moment two testers overlap. Then **P10, P11, P12**, which
are small and independent. **P8 and P9** are real refactors; do them last and alone.
**P3b, P13 and P14** are follow-ups this work uncovered; none blocks anything. P13 and P14 are
the same defect in two places and are worth taking together.

---

## 1. How the numbers were taken

`benchmarks/perf_baseline.py` reproduces every server-side figure in this document:

```bash
.venv/bin/python benchmarks/perf_baseline.py
```

It calls the same serializers and search engine the routes call, in-process — no HTTP, no auth,
no LLM — so it reports the cost of a request *minus* framework overhead, which is the part
worth optimising. `--section assets|laws|search|chroma|status|detail` narrows the run. The
asset section reads the route's dependency closure out of the build's own `__vite__mapDeps`
table, so it stays correct when chunks move.

`--section wire` is the exception: it measures a **running server**, which is how P1 and P7 are
verified. `/spa/assets` is public, so it needs no auth and touches no corpus — start the
`sbpeye-perf` config in `.claude/launch.json` (port 8124, empty data root, scraper off) and it
runs beside the real server on 8000 rather than restarting it:

```bash
.venv/bin/python benchmarks/perf_baseline.py --section wire
```

**Two caveats, and they matter for reading everything below.**

1. Every figure is the *minimum* of three runs after a warm-up, on a fast development machine
   with the page cache hot. It is the optimistic case.
2. The deployment is a shared-vCPU container. Assume CPU-bound figures (P2, P3, P5) land
   **2–4× slower** there, and add the tester's round-trip latency to every request count.

So "search costs 132 ms" here plausibly means 300–500 ms on Railway, and the 804 KB of P1 is
gated by the tester's uplink, not by ours.

**Baseline before any of this landed** (this machine, 2026-08-22). Re-run the benchmark for
current numbers; the landed sections carry their own after-figures.

```
### initial payload for GET /circulars  (CircularsView)
  TOTAL (14 requests)      823509 bytes   gz 210718
  raw 804 KB   gzipped 205 KB (75% smaller)

### GET /api/laws?per_page=100  (LawsView fires this twice on mount)
as written                                   17.6 ms
with content_text deferred                    3.9 ms

### GET /api/circulars/search
q='capital adequacy'                        156.6 ms
q='foreign exchange remittance'             100.7 ms
q='AML CFT'                                 219.9 ms
q='minimum capital requirement banks'        82.7 ms
q='know your customer'                       75.2 ms
q='islamic banking mudarabah'               177.4 ms
q='cyber security incident reporting'       111.7 ms
MEAN                                        132.0 ms

### Chroma query strategies (n_results=50 unless noted)
  collection holds 44,395 chunks
$in ["circular","attachment"]  (current)     60.1 ms
$ne "law"                                    49.7 ms
no filter, n=50                               1.9 ms
no filter, n=150  (over-fetch + py filter)    5.0 ms
  top-150 unfiltered retains 148 circular/attachment chunks (50 needed)

### aggregate endpoints
GET /api/app/status  (3 aggregate scans)      1.7 ms
GET /api/circulars/departments                1.0 ms

### GET /api/circulars/{id}  (worst case: 51 outgoing edges)
as written                                    5.3 ms
```

---

## 2. P1 — Enable response compression ☑ landed

`main.py:480`, one line plus the reasoning around it. Verified over the wire:

```
### over the wire from http://localhost:8124
  file                                                        identity      gzip
  index-C1V1oRPN.js                                             485534    120454
  index-pH_cJlMR.css                                             82214     15720
  purify.es-B_voZNbR.js                                          67949     22603
  index-BM0mHMtT.js                                              68740     16951
  …10 more
  ---------------------------------------------------------------------------
  TOTAL (14 requests)                                           823509    211384
  raw 804 KB  ->  on the wire 206 KB (75% smaller)
```

Regression tests in `tests/test_response_headers.py`. Full suite: 660 passed.

**Two things worth knowing, both found during the change.**

*Middleware order is load-bearing, and the obvious order is wrong.* Registered outside the
authentication middleware — the natural reading of "compress everything" — `minimum_size`
silently never fires. `@app.middleware("http")` builds a `BaseHTTPMiddleware`, which re-emits
every response as a stream, and Starlette's GZip responder can only apply its size threshold to
a single-shot body. Measured: an 85-byte `/healthz` went out compressed, saving one byte, having
paid for a gzip stream. Registering it *inside* the auth middleware fixes it; the cost is that
the 401 and the login redirect go out uncompressed, and both are under 100 bytes.
`test_small_responses_are_not_compressed` is what keeps that from regressing.

*The SSE risk this plan flagged does not exist in this Starlette.* 1.3.1 carries
`DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",)` and skips compression for it outright,
so `/api/chat/stream` is unaffected. That is the library's decision rather than ours, so
`test_event_stream_is_never_compressed` asserts it — the existing stream test collects the whole
body before asserting and would not notice buffering.

*`compresslevel=6`, not the default 9.* On the 485 KB entry chunk:

| level | bytes | time |
|---|---|---|
| 1 | 144,776 | 3 ms |
| 4 | 127,203 | 5 ms |
| 6 | 120,581 | 9 ms |
| 9 | 120,016 | 11 ms |

9 buys 565 bytes (0.5%) for 22% more CPU, on a shared vCPU, on every cache miss.

**Still open.** Nothing compresses ahead of time, so each cache miss re-compresses the same
485 KB. P7 means a given browser pays that once, and at test-deployment traffic it does not
matter — but if the tester count grows, pre-compressed `.js.gz` on disk is the next move.
Brotli would beat gzip by a further ~15% here, but needs a dependency and Railway terminates
TLS in front of us. Not worth it at this size.

<details>
<summary>Original finding</summary>

**Symptom.** Every response goes out uncompressed. Against the running server:

```
$ curl -sD - -o /dev/null -H 'Accept-Encoding: gzip, br' \
    http://localhost:8000/spa/assets/index-C1V1oRPN.js
content-type: text/javascript; charset=utf-8
content-length: 485082
```

No `content-encoding`, because there is no compression middleware in `main.py`.

**Cost.** The first paint of `/circulars` is 14 files, **804 KB raw against 205 KB gzipped** —
75% of what we send is avoidable. On a 5 Mbps uplink that is roughly 1.3 s of transfer against
0.33 s, on every cold load, for every tester. JSON responses benefit too: `/api/laws?per_page=100`
and a 20-row search page are both compressible text.

**Change.** In `main.py`, alongside the other middleware:

```python
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
```

`minimum_size` keeps it off small JSON where the CPU is not repaid.

**Verify.** The same curl grows a `content-encoding: gzip` header and a `content-length` near
120 KB. Confirm `/api/circulars/search` is compressed too, and that the SSE stream at
`/api/chat/stream` still arrives incrementally.

</details>

---

## 3. P2 — `best_window` is 87% of search time ☑ landed

`search.py:453`. Each word is now tested against the query once and the window score rolls —
add the word entering on the right, subtract the one leaving on the left. O(N·tokens) instead
of O(N·window·tokens).

**Verified exactly, not approximately.** The rewrite was diffed against the previous
implementation over the whole attachment corpus plus 400 circular bodies, across seven token
sets including the degenerate ones (a token matching almost every word, a token matching none,
and the empty set) and hand-written edge cases around the window boundary:

```
12117 comparisons over 1728 documents, 0 mismatches
```

Tie-breaking is preserved: both take the *first* window of a maximal score, so identical input
gives a byte-identical snippet.

<details>
<summary>Original finding</summary>

**Symptom.** `cProfile` over three `search("AML CFT")` calls:

```
   ncalls  tottime  cumtime  filename:lineno(function)
        3    0.001    1.983  search.py:1447(search)
      111    0.024    1.733  search.py:453(best_window)
   106158    0.193    1.653  search.py:475(<genexpr>)
 17355345    0.734    0.734  search.py:478(<genexpr>)
       12    0.000    1.523  search.py:1320(_scan_documents)
```

**17.3 million** generator iterations for three searches. `best_window` scores every
25-word window by re-testing all 25 words, so each word is examined 25 times over.

Its own docstring says *"Only ever call this on a passage — one retrieved chunk, or a short
circular body"*. `_scan_documents` (`search.py:1345`) breaks that: it is the fallback for
circulars the vector arm did not cover, and it calls `make_preview` on **whole attachment
texts**, the largest of which is 99,321 words.

**Change.** Test each word once, then roll the score across the window — add the word entering,
subtract the word leaving. O(N·T) instead of O(N·W·T). Output is unchanged:

```
doc words     current     rolling   speedup   identical
    99321      568.9ms       48.2ms     11.8x     True
    76965      445.5ms       31.7ms     14.1x     True
    47985      273.7ms       19.8ms     13.8x     True
    31001      178.9ms       13.1ms     13.6x     True
      450        2.4ms        0.2ms     13.2x     True
```

**Verify.** The existing search tests cover snippet content; they must pass untouched. Beyond
that, assert equality against the current implementation over a sample of real attachment text
rather than trusting the reasoning — that is how the table above was produced.

**Worth considering alongside.** Even at 13× this is scanning a 100k-word document to pick 25
words, on a path the docstring calls a fallback. Capping the scan (first ~20k words, or a
window around the first hit) would make the remaining cost independent of document size. That
is a behaviour change, so it is a separate decision from the rewrite, which is not. **Still
open** — see P13.

</details>

---

## 4. P3 — The Chroma metadata pre-filter costs 35× ☑ landed (circular arm only)

`_query_chunks` at `search.py:965`. The circular arm now over-fetches `CANDIDATE_COUNT × 5`
neighbours with no `where=` and drops law chunks in Python.

**This plan assumed it would work for both arms. It does not.** The assumption was that the two
corpora were comparably sized; they are not. Of 44,395 chunks, 36,226 are circular or attachment
and only **8,169 are law**. So a circular query's neighbourhood is overwhelmingly circular
chunks and the over-fetch has room to spare, while a law query's neighbourhood is *also* mostly
circular chunks and the law arm starves — measured across five law queries, the top 150 held as
few as **4** law chunks against the 50 the arm needs.

Buying that margin back costs more than the filter it replaces:

| law arm | time | yield (worst of 5) | |
|---|---|---|---|
| `where={"kind": "law"}`, n=50 | 21.1 ms | 50 | **kept** |
| no filter, n=150 | 4.6 ms | 4 | starved |
| no filter, n=500 | 16.7 ms | 11 | starved |
| no filter, n=1000 | 33.4 ms | 56 | barely enough, and slower |

So the law arm keeps its pre-filter. It is also much cheaper than the circular arm's was
(21.1 ms against 61.1 ms) because `$eq` over a small subset is not the same query as `$in` over
a large one — which is why the original 35× headline overstated the law side. Tracked as **P3b**.

**The over-fetch is sized on adversarial queries, not friendly ones.** At the ×3 this plan
originally proposed, the worst case over law-flavoured phrasing, single stopwords and junk was
59 against 50 needed — a 1.2× margin, one unlucky query from starving. ×5 gives 2.6× for 2.7 ms:

| n_results | worst time | worst yield | margin |
|---|---|---|---|
| 150 (×3) | 5.2 ms | 59 | 1.2× |
| **250 (×5)** | **8.6 ms** | **128** | **2.6×** |
| 400 (×8) | 14.2 ms | 220 | 4.4× |

**And a margin is not a guarantee, so there is a fallback.** If the over-fetch does come up
short, the arm re-runs the old filtered query rather than handing back fewer candidates than
the rest of the fusion was sized for. The arm can therefore never be *weaker* than before this
change — only occasionally slower, on queries that do not arise in practice. Three tests cover
it, including that a store smaller than the over-fetch is not mistaken for a starved
neighbourhood (without that distinction a freshly indexed deployment would pay the slow query
on every search).

<details>
<summary>Original finding</summary>

**Symptom.** `_vector_ranks` (`search.py:968`) passes
`where={"doc_type": {"$in": ["circular", "attachment"]}}`. That pre-filter makes Chroma scan
all 44,395 chunks instead of using the HNSW index:

```
$in ["circular","attachment"]  (current)     60.1 ms
$ne "law"                                    49.7 ms
no filter, n=50                               1.9 ms
no filter, n=150  (over-fetch + py filter)    5.0 ms
```

The law arm (`search.py:1018`) carries the same shape with `where={"kind": "law"}`.

**Change — the one that needs no migration.** Ask for `CANDIDATE_COUNT * 3` with no `where`,
then drop the wrong `doc_type` in Python before `_collect_evidence`. Over-fetching 150 retains
148 circular/attachment chunks against the 50 needed, so the candidate set is not starved.

**Change — the one that is actually right.** Separate collections per corpus. That is the 1.9 ms
column, with no over-fetch heuristic and no risk of a query whose neighbourhood is all law
chunks coming back short. It costs a re-index of a 330 MB store and a migration path for the
deployed volume, which is why it is not the first move.

**Combined effect of P2 + P3**, measured end to end through `search()`:

```
query                                     current   both fixes   speedup
capital adequacy                           160.0ms        22.7ms      7.1x
foreign exchange remittance                101.7ms        15.2ms      6.7x
AML CFT                                    221.1ms        27.8ms      8.0x
minimum capital requirement banks           85.2ms        12.6ms      6.8x
know your customer                          74.8ms        11.3ms      6.6x
islamic banking mudarabah                  177.1ms        25.0ms      7.1x
cyber security incident reporting          113.0ms        18.3ms      6.2x
------------------------------------------------------------------------
MEAN                                       133.3ms        19.0ms      7.0x
```

**Verify.** Diff the top-20 for a fixed query set before and after; a change at rank 20 is
acceptable, a change at rank 1–5 is not.

</details>

### As landed: 132 ms → 22 ms

```
query                                        before      after   speedup
capital adequacy                            159.6ms      26.5ms      6.0x
foreign exchange remittance                 101.3ms      18.5ms      5.5x
AML CFT                                     219.4ms      30.9ms      7.1x
minimum capital requirement banks            84.1ms      15.6ms      5.4x
know your customer                           76.5ms      15.2ms      5.0x
islamic banking mudarabah                   180.2ms      28.7ms      6.3x
cyber security incident reporting           113.8ms      21.4ms      5.3x
microfinance institutions ordinance         102.2ms      17.6ms      5.8x
prudential regulations                       74.4ms      12.6ms      5.9x
deposit protection                           85.2ms      16.6ms      5.1x
------------------------------------------------------------------------
MEAN                                        119.7ms      20.4ms      5.9x
```

Law search is byte-identical before and after, as it must be — that arm did not change.

**Circular results moved on 2 of 10 queries, and the movement is worth understanding rather
than waving through.** Both are *insertions*, not reorderings:

```
islamic banking mudarabah   rank 4  gained IBD Circular No. 04 of 2008
                            rank 20 dropped BPD Circular Letter No. 40 of 2005
deposit protection          rank 12 gained BPRD Circular No. 07 of 2011
                            rank 20 dropped BPRD Circular No. 10 of 2019
```

Everything above the insertion point is unchanged; everything below shifts down one and the
old rank 20 falls off. The cause is not the Python filter — it is that HNSW is *approximate*,
and `ef` scales with `n_results`. Searching for 250 neighbours explores more of the graph than
searching for 50, so the new path finds near neighbours the old one missed. The inserted result
for an Islamic-banking query is an IBD (Islamic Banking Department) circular joining three
other IBD circulars at the top, which reads as better recall rather than drift.

**Both were then checked against the documents themselves**, rather than left as inference.

*`islamic banking mudarabah` — the change is an improvement.* The gained circular is
*Instructions and Guidelines for Shariah Compliance in Islamic Banking Institutions* (vector
neighbour #49, distance 0.5776; body: "islamic" ×6, "shariah" ×2). The dropped one is *R-6(1B)
20% Limit on Investment in Shares* — which had **no chunk in the top 250 at all** and sat at
lexical rank 50, the last position the lexical arm emits. It was in the results because
`mudarabah` expands to `investment, profit, sharing` and the circular is about *investment in
shares*: the same word in an unrelated sense. A false friend at rank 20, replaced by a document
that is squarely on topic at rank 4.

*`deposit protection` — the change is marginal, and the query has no right answer here.* The
gained circular is *Service Charges on PLS Deposit Accounts*, whose matched passage is about
small depositors declining as service charges rose — depositor-protection-adjacent, and new to
the vector arm. The dropped one is *Branchless Banking Regulations*, whose matched passage is a
**glossary definition of the word "Deposit"**. So the substantive document displaced the
boilerplate one. But the corpus holds **no Deposit Protection Corporation or deposit-insurance
circulars** — the seven title matches are depositor grievances, sponsor shares and Islamic
returns — so both results are weak and the swap is low-stakes churn.

One correction worth recording, because the obvious reading of the diff is wrong: the dropped
document here was **not evicted from the vector arm**. It is still in it, at rank 10; it fell
below the top 20 because the newcomer pushed everything down one. The aggregate check that the
wider search is not returning worse neighbours is the distance of the last chunk kept, which
improved slightly — 0.6581 before, 0.6551 after.

---

## 5. P4 — SQLite WAL ☑ landed

Already done in `4666e74` (the admin-console change): `database.py` now applies
`journal_mode=WAL`, `busy_timeout=30000` and `synchronous=NORMAL` per engine on connect, with a
`wal_checkpoint(TRUNCATE)` on shutdown so the corpus copies as a whole file.

Confirmed applied — all three databases report `wal`.

This was independently on the list here for the reason the commit gives: under the old rollback
journal a writer held an exclusive lock on the whole file and every reader queued behind it, and
the app writes on ordinary user actions (a workspace PATCH on every search, a chat turn per
message), not only during sync. Recorded so it is not re-investigated.

---

## 6. P5 — `/api/laws` materialises 3.5 MB it never sends ☑ landed

`law_summary_load_options()` at `serializers.py:169`, applied at the two queries that feed
`_law_summary` in bulk — the laws listing (`main.py:2046`) and the circular detail pane's
regulations block (`main.py:1478`).

**Measured in queries and bytes, not just milliseconds**, because wall time understates this
one: the benchmark machine has a hot page cache and an NVMe under it, so reads that cost
almost nothing here are real I/O on a container.

```
GET /api/laws  (LawsView.loadCorpus — the whole corpus, 100 per page)
                                  time  SQL stmts    law text read
  as written                     24.2ms        273          4.88 MB
  with load options               6.3ms          7          0.00 MB
  payload identical across all 2 pages: True

GET /api/circulars/{id}  (regulations block, worst case: 5 linked laws)
  as written                      1.7ms         17         75.3 KB
  preloaded                       1.4ms          4          0.0 KB
```

**273 queries to 7, and 4.88 MB of law text to nothing.** The query count is the more honest
headline: it was four relationship walks per document, so the old cost scaled with page size
while the new one does not.

**The option is per-query, not on the mapper.** Making `content_text` `deferred()` would fix
every path at once, and that is the wrong trade here: search previews the text, the AI pipeline
summarises it, the scraper writes it, and a mapper-level default would turn each of those into
its own lazy load — silently, with no error to notice and a *worse* profile than today on the
paths that matter most. Opting out at the queries that genuinely do not need it keeps the cost
where it can be seen.

**Also landed:** `LawsView.loadCorpus` fetched its pages in a sequential loop. The first page
reports `total`, so the rest are known up front and now go out together — the tree cannot render
until the last one lands, so serializing them put a whole round trip in front of every visit for
nothing.

Regression tests in `tests/test_law_list_loading.py` — two, deliberately. One asserts
`content_text` is never materialised, which is the property and which fails if the loader
options are dropped. The other asserts the payload is unchanged, which guards a different
mistake: options *edited* into something that multiplies rows (a `joinedload` against a
collection duplicates parents) rather than options removed.

A third, asserting the query count does not grow with the page, was written and then cut: it
overlapped the first, and its "the lazy path issues more queries" assertion pinned the old slow
behaviour, so it would have failed if `_law_summary` ever legitimately stopped walking
relationships. `benchmarks/perf_baseline.py --section laws` reports query counts in more detail
anyway. Full suite: 662 passed.

<details>
<summary>Original finding</summary>

**Symptom.** `_law_summary` (`api/serializers.py`) touches, per document:

- `document.current_version` — a Python property that iterates `self.versions`, so it lazy-loads
  every version row **including `content_text`**
- `document.parent` — a query
- `len(document.versions)` — the rows above
- `_analysis_row(document)` — `document.children`, then `current_version` again; called twice,
  once for `summary` and once for `tags`

For one 100-document page that is **3.54 MB of extracted PDF text** read out of SQLite and built
into Python objects, none of which reaches the response.

```sql
-- 100 docs by (doc_type, title): 81 version rows, 3,713,800 bytes of content_text
```

`LawsView.loadCorpus` (`frontend/src/views/LawsView.vue:831`) calls this **twice on mount**,
sequentially, and again on every search.

**Change.** Eager-load with the blob deferred:

```python
query.options(
    selectinload(RegDocument.versions).defer(RegDocumentVersion.content_text),
    selectinload(RegDocument.children),
)
```

```
as written                                   17.6 ms
with content_text deferred                    3.9 ms      4.5x
```

While in here: `loadCorpus` learns `response.total` from page 1, so pages 2..N can be fetched
in parallel instead of in sequence.

**Verify.** `benchmarks/perf_baseline.py --section laws`, and confirm the payload is
byte-identical — this changes only *how* the rows are fetched.

### Which user actions actually pay this

Traced rather than assumed, because `_law_summary` is reached from four places and only one of
them is worth changing. The endpoint is `GET /api/laws`; the screen is **Laws & Regulations**
(`/laws`, and `/laws/:id` as a deep link).

| User action | Path | Cost | P5's share |
|---|---|---|---|
| **Open `/laws`** — `onMounted` → `loadCorpus` | `GET /api/laws` ×2, sequential | 17.2 + 6.2 = **23.4 ms** | **~18 ms — this is the item** |
| **Type in the search box** — 250 ms debounce | `GET /api/laws?q=` | 14–121 ms | 0–7 ms — negligible |
| **Change the doc-type filter** | `GET /api/laws?q=` | same | same |
| **Click a document in the tree** | `GET /api/laws/{id}` | 0.7 ms | negligible |
| **Open a circular** — the regulations block | `GET /api/circulars/{id}` | 1.6 ms | negligible |

The corpus is 135 documents, so `loadCorpus` makes exactly two requests — a full page of 100 and
a second of 35 — one after the other, and the library tree renders only when both land.

**The search path is not a P5 problem, contrary to what this section originally implied.** By
the time `_law_summary` runs there, the search engine has already loaded those `RegDocument`
rows into the session, so the lazy loads it would otherwise trigger are already paid. Measured
split for `q=foreign exchange`: 121.0 ms of search, **−0.4 ms** of serialization.

That leaves the two detail paths (`/api/laws/{id}`, and the circular pane's regulations block)
touching one document and five respectively — real N+1s, far too small to matter. So P5 is worth
doing for the `/laws` mount and nothing else, which also caps its value at roughly 18 ms rather
than the "twice on mount plus every search" the original framing suggested.

**What the search path is really waiting on is P14**, below.

</details>

---

## 7. P6 — 25 `async def` routes block the event loop

**Symptom.** 25 route handlers in `main.py` are declared `async def` but do synchronous,
blocking work — SQLAlchemy queries, `requests` calls, file reads — with no `await` in the body.
An `async def` handler runs *on* the event loop, so for its whole duration nothing else in the
process progresses: not another tester's request, not a static asset, not the next chunk of an
in-flight chat stream.

The worst is `/api/llm/status` (`main.py:878`). It is `async def`, and it calls
`client.check_availability()` — a **synchronous HTTP request to the user's LLM vendor with a
5-second timeout** (`ai.py:4029`). It fires on every page load, and credentials are per-user, so
on the deployment it is a remote call. One tester with a slow provider freezes the app for
everyone, for up to five seconds.

**Change.** Delete the word `async`. FastAPI then runs the handler in its threadpool, which is
what the blocking code needs. The full list:

`get_app_status`, `get_llm_status`, `get_tags`, `get_ecodata`, `get_departments`, `get_years`,
`browse_circulars`, `browse_recent_circulars`, `get_circular_by_url`, `get_circular_detail`,
`export_circular_checklist`, `query_circular_entities`, `get_ai_generation_job`,
`get_circular_relationships`, `get_circular_consolidation`, `resolve_document`,
`document_content`, `list_research_workspaces`, `get_research_workspace`,
`delete_research_workspace`, `unpin_workspace_circular`, `list_chat_sessions`,
`get_chat_session`, `delete_chat_session`, `truncate_chat_session`.

Take `get_llm_status` first and alone — it is the one with a five-second worst case, and it is a
one-word diff.

**Verify.** This is invisible to a single-user benchmark, which is why it survived. Reproduce it:
point the LLM base URL at an address that blackholes (not one that refuses — refusal is instant),
then load any page and time a concurrent `GET /healthz`. Before the change that health check
waits behind the probe; after it, it returns immediately.

**Note.** `main.py` is otherwise careful about this — `_remote_circular_check_status` already
does its SBP fetch on a background thread behind a 30-minute TTL, and `healthz`, `list_laws`
and `search_circulars` are correctly plain `def`. The 25 above look like drift rather than
intent.

---

## 8. P7 — `Cache-Control` on hashed assets ☑ landed

`ImmutableStaticFiles` at `main.py:517`, mounted at `/spa/assets` only. Served headers:

```
cache-control: public, max-age=31536000, immutable
conditional GET: 304, Cache-Control: 'public, max-age=31536000, immutable'
```

**One detail that is easy to get wrong.** `StaticFiles.file_response` decides on the 304
*before* returning, so a subclass that only decorates the 200 leaves the `NotModifiedResponse`
with no freshness information — and a 304 that says nothing about freshness asks the browser to
re-validate again next time, which is the exact round trip this item removes. The override sets
the header on whatever comes back, 200 or 304. `test_not_modified_still_carries_cache_control`
pins it.

`index.html` is deliberately excluded — it is the file that names the new hashes after a
deploy, and a year of caching would strand testers on the old build.
`test_index_html_is_not_cached_forever` pins that.

<details>
<summary>Original finding</summary>

**Symptom.** `app.mount("/spa/assets", StaticFiles(...))` (`main.py:492`). Starlette's
`StaticFiles` sends `etag` and `last-modified` but no `Cache-Control`, so a browser revalidates
every asset on every load. That is 14 conditional requests over HTTP/1.1 — uvicorn serves no
HTTP/2 — before the landing route can paint, each one a full round trip even though each
returns 304.

The filenames are content-hashed. They are immutable by construction.

**Change.** A `StaticFiles` subclass overriding `file_response` to add
`Cache-Control: public, max-age=31536000, immutable` for `/spa/assets` only. `index.html` must
*not* get it — it is the file that names the new hashes after a deploy.

**Verify.** Reload twice with the network panel open: the second load shows the assets served
from disk cache with no request, and only `index.html` and the API calls on the wire.

</details>

---

## 9. P8 — The landing route is a 4-deep request chain

**Symptom.** `/circulars` is the redirect target of `/`, so this is the app's front door. Its
mount fires nine API calls, four of which are strictly serialized:

```
getResearchWorkspaces()          ─┐  CircularsView.loadWorkspaces
  → getResearchWorkspace(id)      │  activateWorkspace — refetches a workspace
                                  │  the list response already contained
    → getCircularSearch()         │  loadCirculars — the actual content
      → updateResearchWorkspace() ┘  saveActiveWorkspaceState, inside the try,
                                     so the spinner waits for it
```

alongside `getAppStatus`, `getLlmStatus`, `getCurrentUser`, `getCircularDepartments`,
`getCircularTags` in parallel. On a 200 ms link the serial chain alone is 800 ms before the
result list can render, and the last hop is a *write* the user is not waiting for.

Three separable defects:

1. `onMounted` (`CircularsView.vue:469`) `await`s `loadWorkspaces()` before `loadCirculars()`.
   They are independent — the search does not need the workspace list.
2. `activateWorkspace` (`:215`) calls `getResearchWorkspace(workspaceId)` for a workspace that
   is already in the list payload just fetched.
3. `loadCirculars` (`:284`) `await`s `saveActiveWorkspaceState()` inside its `try`, so
   `loading` stays true through a PATCH nobody is waiting on. Fire and forget it.

**Change.** Fix 3 first — it is two characters and takes a round trip off *every* search, not
just the first. Then 2. Then 1, which is the one that needs care: the workspace's saved
`search_state` feeds the search, so parallelising means either accepting one search with default
filters that a restore may supersede, or having the server return workspace + first page
together.

**Verify.** Network panel on a cold load of `/circulars`: count the requests on the critical path
before the list paints. Related, same pattern, smaller: `CircularDetailPane.loadCircular`
(`:296`) serializes `getCircularDetail` then `getCircularSource` — `Promise.all` them.

---

## 10. P9 — Chat re-renders the whole thread on every token

**Symptom.** `ChatView.vue:943`, in `onToken`:

```js
messages.value = messages.value.map((message) =>
  message.id === assistantId ? { ...message, content: message.content + content } : message,
)
```

Every token allocates a new object for *every* message in the thread and replaces the array. Vue
therefore re-renders every bubble, so `renderMarkdown` is called once per message per token —
and its cache key is the message's entire content string, so a 40-message thread hashes several
hundred KB of strings per token. `scrollToBottom()` also runs per token, each call awaiting
`nextTick` plus two `requestAnimationFrame`s and then reading `scrollHeight`.

The markdown cache at `:363` already fixed the *parsing* half of this — completed messages are
parsed once. The churn it cannot fix is the array replacement upstream of it.

**Change.** Hold the streaming message's text in its own `ref` and mutate it in place, so the
other messages keep object identity and Vue leaves them alone. Coalesce token appends to one
`requestAnimationFrame` rather than one per token, and scroll from the same frame.

**Verify.** Performance panel, record a long streamed answer in a thread of 30+ messages, compare
scripting time and dropped frames. The subjective test — does a long answer stream smoothly on a
laptop — is the one that matters.

**Note.** P6 compounds here: SSE chunks are handed out by Starlette from the threadpool, but each
one still has to cross the event loop to reach the socket. A blocking `async` route stalls the
stream even when the model is producing tokens fine.

---

## 11. P10 — `/api/circulars/{id}` N+1 and blob reads

**Symptom.** Two defects in one handler (`main.py:1414`):

1. `rel_dict` (`:1426`) runs a point query for `source_id` and another for `target_id` on every
   relationship. The worst circular in the corpus has 51 outgoing edges — 102 extra queries for
   a payload of at most 102 distinct circulars, most of them repeats.
2. `"has_text": bool(attachment.content_text)` reads the entire extracted text of every
   attachment to decide whether it is empty. One circular carries 723 KB of it.

Measured at 5.3 ms warm on the worst case — small here because SQLite is fast and the cache is
hot, and the reason this is P10 and not P2.

**Change.** Collect the referenced ids and fetch them in one `IN` query into a dict. Replace the
`has_text` blob read with a length check pushed into SQL, or a `deferred` column with an
explicit `func.length(...) > 0` in the query.

**Verify.** `benchmarks/perf_baseline.py --section detail`, and check the response is unchanged
for a circular with many relationships and a large attachment.

**Related.** `search.py:1544` eager-loads `joinedload(Circular.attachments)` across the whole
candidate set, pulling ~1.5 MB of attachment text per search. Measured at 1.4 ms, and deferring
`content_text` did not improve it — the cost there is `_scan_documents` tokenizing that text
(P2), not fetching it. Noted so it is not "fixed" twice.

---

## 12. P11 — Markdown re-parsed on every render

**Symptom.** `SummarySection.vue:41` calls the parser *from the template*:

```html
<div v-show="expanded" v-html="render(props.summary)" />
```

A function call in a template re-runs on every re-render of the component, not when its input
changes. And `v-show` renders the element and hides it with CSS, so `marked.parse` + `DOMPurify`
run even though the section is collapsed by default and invisible. Same shape at
`EcoDataView.vue:628`.

**Change.** `computed(() => render(props.summary))`, and `v-if="expanded"` so a collapsed
section costs nothing.

**Second, larger win in the same file.** `SummarySection` is imported eagerly by
`CircularDetailPane`, which puts it and its dependencies in the landing route's payload:

```
SummarySection.vue_...js    15879     purify.es-...js    67949
```

**84 KB of the 804 KB** for a block that starts collapsed. `defineAsyncComponent` — which this
codebase already uses correctly for `CircularGraph` (164 KB), `ConsolidatedView` and
`PdfPreviewDialog` — moves it out of the critical path.

**Verify.** Re-run `--section assets` and confirm the total drops by ~84 KB raw / ~28 KB gzipped.

---

## 13. P12 — Google Fonts blocks first render

**Symptom.** `frontend/index.html` loads Inter from `fonts.googleapis.com` with a plain
`<link rel="stylesheet">` in `<head>`. A stylesheet in the head is render-blocking, so first
paint waits on a DNS lookup, TLS handshake and round trip to a third party — before any of our
own bytes matter. `preconnect` is already present, which shortens the handshake but does not
stop the block. Testers are in Pakistan, where that origin is neither fast nor reliably
reachable.

**Change.** Self-host the two or three weights actually used as woff2 next to the other assets,
with `font-display: swap`. It removes a cross-origin dependency, a round trip and a privacy
footnote, and the files then inherit P7's cache headers.

**Verify.** Network panel shows no request to any `fonts.g*` origin, and the page still renders
in Inter.

---

## 14. P3b — One Chroma collection per corpus

**Deferred, not rejected.** It is the fix that deletes a heuristic rather than tuning one.

Circulars and laws share a single collection, so each arm has to exclude the other's chunks —
and every way of doing that is a compromise. The circular arm over-fetches and filters in
Python, which works only because it is the majority corpus and which needs a fallback for when
that fails (§4). The law arm cannot do even that and pays 21.1 ms of pre-filter per search.
Separate collections make both arms a plain ANN query at **1.9 ms**, with no over-fetch, no
margin to size, no fallback path, and no approximate-recall difference to reason about.

The cost is a re-index of a 330 MB store plus a migration for the deployed volume — which is
exactly why it was not the first move, and why it is worth doing once the corpus grows or the
law search path gets real use. Note the numbers scale against us: the over-fetch margin is a
function of how lopsided the two corpora are, so it narrows as the law corpus grows.

---

## 15. P13 — `_scan_documents` previews whole attachments

**Left open by P2 deliberately.** The rewrite made `best_window` 13× faster; it did not stop it
being handed a 99,321-word document to pick 25 words out of.

`_scan_documents` (`search.py:1345`) is the fallback for circulars the vector arm did not cover.
It calls `make_preview` on entire attachment texts — which `best_window`'s own docstring says
never to do, because term density over a whole document prefers prose *about* a subject to the
table that states it. So this is a correctness smell and the remaining bulk of the 22 ms, in the
same place.

Capping the scan — the first ~20k words, or a window around the first token hit — makes the cost
independent of document size. Unlike P2 that is a **behaviour change**: it can change which
passage is previewed for a long attachment. It therefore needs a judgement about snippet quality
rather than an equality check, which is why it is its own item.

---

## 16. P14 — Law search is now 5× slower than circular search

**Found while tracing P5's user paths.** Circular search is 22 ms after P2 + P3. Law search —
the `/laws` search box — is **111–121 ms**, and the profile is the one P2 already fixed once:

```
   ncalls  tottime  cumtime  filename:lineno(function)
        3    0.000    0.905  search.py:1546(search)
      150    0.000    0.837  search.py:1261(_law_result)
      198    0.197    0.792  search.py:453(best_window)     <- 87% of the total
       96    0.003    0.785  search.py:522(make_preview)
```

`_law_result` calls `make_preview` on the full `content_text` of a law version, which averages
**44 KB** — so `best_window` is scanning whole documents to choose a 25-word snippet, exactly
what its docstring says never to do. P2's rewrite already made this 13× cheaper than it was;
what remains is that it should not be scanning whole documents at all.

This is P13's defect on the law arm, and worse there: for circulars `_scan_documents` is a
*fallback* for results the vector arm did not cover, while `_law_result` runs this on **every**
law result. Fixing it should bring law search into the same range as circular search.

Do it with P13 — one decision about how to bound a preview scan, applied in both places.

---

## 17. Checked and not worth doing

Recorded so the same ground is not covered twice.

| Looked at | Finding |
|---|---|
| Missing indexes on `circulars.date`, `.department`, `.indexed_at`, `circular_relationships.source_id/target_id` | Every hot query is a full scan, but the table is 3,653 rows / 8 MB and each scan is 1–2 ms. Real, far too cheap to matter at this corpus size. Revisit past ~50k circulars. |
| `/api/app/status` polling every 5 s | Gated on `syncRunning \|\| remoteStatus === 'checking'` (`App.vue:168`), so it only polls during a sync. Not a background cost. |
| `_remote_circular_check_status` fetching sbp.org.pk | Already on a background thread behind a 30-minute TTL cache. Correct as written. |
| Query embedding on the search path | 7.6 ms cold, ~0 ms cached. The 60 ms in `_vector_ranks` is Chroma, not FastEmbed — see P3. |
| Heavy components in the initial bundle | `CircularGraph` (164 KB, Vue Flow), `ConsolidatedView`, `PdfPreviewDialog` are all already `defineAsyncComponent`. Only `SummarySection` is not — P11. |
| `AdminSyncTab` polling every 4 s | New in the admin-console change. Gated on `running`, cleared on unmount. Fine. Could pause on `document.hidden` the way `DebugView` does, but it is an admin tab open on purpose. |
| Chat markdown cache | `ChatView.vue:363` already caches parses and evicts at 400 entries. The remaining cost is upstream array churn — P9. |
