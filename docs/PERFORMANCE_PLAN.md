# UI performance

The app is deployed for test users and the LLM paths are slow by nature — that is expected and
is not what this document is about. This is about everything *else* the user waits on: the
bytes before first paint, the search round trip, the detail pane, and the frames dropped while
an answer streams.

Twelve items, measured rather than guessed, ordered by what a user actually feels per unit of
work. They are deliberately independent: each can land, ship and be verified on its own.

**Status:** P4 landed (uncommitted, §5). Nothing else started.

---

## 0. Tracker

| # | Item | Where | Measured effect | Effort | State |
|---|---|---|---|---|---|
| **P1** | Enable response compression | `main.py` | 804 KB → 205 KB first paint | XS | ☐ |
| **P2** | `best_window` is O(words × window × tokens) | `search.py:453` | 12–14× on the function | S | ☐ |
| **P3** | Drop the Chroma `doc_type` pre-filter | `search.py:968`, `:1018` | 60.1 ms → 5.0 ms | S | ☐ |
| **P4** | SQLite WAL + `busy_timeout` | `database.py` | writers stop blocking readers | S | ☑ landed |
| **P5** | `/api/laws` loads 3.5 MB it never sends | `serializers.py` | 17.6 ms → 3.9 ms | S | ☐ |
| **P6** | 25 `async def` routes block the event loop | `main.py` | removes a 5 s global stall | S | ☐ |
| **P7** | `Cache-Control` on hashed assets | `main.py:492` | 14 revalidations → 0 | XS | ☐ |
| **P8** | Landing route is a 4-deep request chain | `CircularsView.vue` | 4 serial RTTs → 2 | M | ☐ |
| **P9** | Chat re-renders the thread on every token | `ChatView.vue:943` | stream stutter | M | ☐ |
| **P10** | `/api/circulars/{id}` N+1 + blob reads | `main.py:1426` | 102 queries → 2 | S | ☐ |
| **P11** | Markdown re-parsed on every render | `SummarySection.vue:41` | −84 KB initial, less churn | S | ☐ |
| **P12** | Google Fonts blocks first render | `index.html` | one cross-origin RTT | XS | ☐ |

**Suggested order.** P1 and P7 first — they are the largest user-visible win for the least
code, and they are pure configuration. Then P2 + P3 together, which is the search story and
wants one round of verification, not two. Then P6, which is the one that stops mattering only
until two testers use the app at once. P5, P10, P11, P12 are small and independent — take them
whenever. P8 and P9 are real refactors; do them last and alone.

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

**Two caveats, and they matter for reading everything below.**

1. Every figure is the *minimum* of three runs after a warm-up, on a fast development machine
   with the page cache hot. It is the optimistic case.
2. The deployment is a shared-vCPU container. Assume CPU-bound figures (P2, P3, P5) land
   **2–4× slower** there, and add the tester's round-trip latency to every request count.

So "search costs 132 ms" here plausibly means 300–500 ms on Railway, and the 804 KB of P1 is
gated by the tester's uplink, not by ours.

**Baseline, this machine, 2026-08-22:**

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

## 2. P1 — Enable response compression

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
`/api/chat/stream` still arrives incrementally — Starlette's `GZipMiddleware` passes streaming
responses through, but this is exactly the pairing worth eyeballing once rather than assuming.

**Note.** Brotli would beat gzip by a further ~15% on this payload, but needs a dependency and
Railway terminates TLS in front of us anyway. Not worth it at this size.

---

## 3. P2 — `best_window` is 87% of search time

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
is a behaviour change, so it is a separate decision from the rewrite, which is not.

---

## 4. P3 — The Chroma metadata pre-filter costs 35×

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

**Verify.** Top-20 result *order* was identical on six of the seven queries above, with 19/20
overlap on `islamic banking mudarabah` — a law chunk displacing a marginal circular at the
tail of the over-fetch. That single-result drift is the price of the no-migration option and
is the reason to prefer separate collections eventually. Diff the top-20 for a fixed query set
before and after; a change at rank 20 is acceptable, a change at rank 1–5 is not.

---

## 5. P4 — SQLite WAL ☑ landed

Already done in the working tree, in the admin-console change: `database.py` now applies
`journal_mode=WAL`, `busy_timeout=30000` and `synchronous=NORMAL` per engine on connect, with a
`wal_checkpoint(TRUNCATE)` on shutdown so the corpus copies as a whole file.

Confirmed applied — all three databases report `wal`.

This was independently on the list here for the reason the commit gives: under the old rollback
journal a writer held an exclusive lock on the whole file and every reader queued behind it, and
the app writes on ordinary user actions (a workspace PATCH on every search, a chat turn per
message), not only during sync. Recorded so it is not re-investigated.

---

## 6. P5 — `/api/laws` materialises 3.5 MB it never sends

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

## 8. P7 — No `Cache-Control` on hashed assets

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

## 14. Checked and not worth doing

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
