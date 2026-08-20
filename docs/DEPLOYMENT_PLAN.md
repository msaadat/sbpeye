# Plan: Multi-User Web Deployment

Implementation plan for deploying SBPEye as a shared, authenticated web application on
Railway, starting from a codebase that assumes a single trusted operator on a local
machine with a writable source tree.

The plan is deliberately scoped to a **test deploy** — a small set of known testers, not
a production service. Decisions that would be wrong for production are taken anyway where
they buy a materially faster path to feedback; each one is marked and carries the reason,
so the trade is visible rather than inherited silently.

---

## 0. Status

### 0.1 Complete

**Section 2 — the database split.** `research_workspaces`, `workspace_circulars`,
`chat_sessions`, `chat_messages` and `settings` live on `AppBase` in `sbpeye_app.db`,
moved out of `sbpeye.db` by `database._relocate_app_tables()`.

Migrated against the live files with every row preserved — 4 workspaces, 12 pins, 11 chat
sessions, 30 messages, 9 settings — and the served API verified to report the same pin
counts as the raw tables, with all 12 pinned circular ids resolving across the database
boundary. Test suite sits at the 6-failure environment baseline with 569 passing (up from
567: three new tests, listed in 2.1) — a Windows measurement; see 0.4.

`sync_status` and `ai_generation_jobs` were deliberately **not** moved; see 2.2 and 10.1.

**Section 4 — the data directory.** `DATA_ROOT` is defined in `env.py` and honours
`SBPEYE_DATA_DIR`, defaulting to the source root so local checkouts and the test suite are
unaffected. All seven mutable paths follow it; `main.STATIC_DIR` correctly does not.

The change was six functional lines, not the ~40 call sites this section originally
estimated — see 4.4 for why, and for the four inline re-derivations that were the actual
trap. Test suite unchanged against the true baseline (0.4).

**Section 12 — the file tree.** `attachments/` and `cache/` are one `files/` tree split by
cost to lose: `laws/` (archive, never deleted), `circulars/` (re-fetchable), `cache/`
(disposable). Model weights moved out to `models/`. Migrated with `sbpeye cache
migrate-layout`; reconciliation clean, and `--prune` can no longer reach the archive.

**Section 3 — the cache write paths.** A cache miss on an ordinary document view no longer
runs a re-ingest: it fetches the bytes and writes `local_path` alone, leaving `content_text`
and the `is_vectorized` ledger intact. Law versions gained the opposite change — they refetch
on miss instead of 404ing, accepted only when the bytes hash to the version's own hash, so a
replaced edition can never be served as the archived one.

The `DocumentCache` table was not built and is not needed (10.3, option C). Suite: 1 failed /
578 passed, baseline failure only.

**Section 9 (partial) — the container.** `Dockerfile`, `.dockerignore`, `GET /healthz` and
the `PORT` binding are done, and the image is built and smoke-tested locally: it boots in
~6 s, serves the SPA, and **returns correct vector search results from the store built on
this machine** — retiring the chromadb format risk that 5.3 called the most likely thing
here to fail. `docling` became an optional extra to get the image from 19.3 GB to 1.51 GB
(9.1.3). Still to do in 9: the Railway service itself, the volume, and the environment.

### 0.2 Not started

Sections 6, 7 and 8, and the Railway half of section 9 (9.2, 9.3). Section 5 needs no code
(5.1).

### 0.3 Sequencing

Sections 2-4 are prerequisites for everything else and are independent of the auth design;
both are now in and were verified locally. Section 7 (auth) is the largest single piece.
Sections 6-8 are small and can be done in any order.

```
2. DB split ✔ ──→ 4. data directory ✔ ──→ 3. cache paths ✔ ──→ 9. Railway
                                                  │              │
                                  5. corpus upload (no code) ────┤
                                  6. ecodata refresh ────────────┤
                                  7. auth + admin gating ────────┤
                                  8. corpus content prep ────────┤
                                 12. tree consolidation ✔ ───────┘
```

Sections 2, 3 and 4 are in. Section 7 (auth) is now the largest remaining piece, and section
12 is unblocked — it was waiting on 10.3, which is closed.

Section 5 turned out to need no code at all — the corpus and vector store are uploaded to the
volume with the Railway CLI (5.1). That closed what had been a blocker (5.5) and removed the
seed-copy machinery this plan originally specified (5.2).

### 0.4 Test baseline

The **6 failed / 569 passed** figure quoted below was measured on Windows
(`.venv/Scripts/python.exe`). On Linux the baseline on clean `main` is **1 failed / 575
passed** — the single failure being `test_attachments::test_fetch_page_cached_uses_uuid_filename`.

Diff against the baseline on the machine you are actually running, measured by stashing the
change rather than assumed from this document.

---

## 1. Decisions taken

Recorded so they are not silently re-litigated. The analysis behind each is in the session
that produced this document; the short form is here.

### 1.1 Web deployment on Railway, not desktop

Desktop packaging was evaluated and rejected **for now**, not on technical grounds — it
dissolves the auth requirement, the single-writer constraints and the secrets problem
entirely — but on iteration speed. A fix reaches every tester in two minutes on Railway
and requires re-downloading a ~700 MB bundle on desktop.

The measurements that would matter if this is revisited:

| Fact | Value |
|---|---|
| `.venv` total | 1.7 GB |
| `torch` / `onnxruntime` / `cv2` | 500 MB / 291 MB / 113 MB |
| `docling` imports | **All function-local** (`checklist.py` only) — droppable, costs one feature |
| `fastembed` | Lazily imported but on the hot path (`embed_queries` per search and per chat turn) |
| Shippable corpus | `sbpeye.db` 69 MB + `chroma_db/` 486 MB (was recorded as 395 MB; re-measured) |
| Not shippable | `files/` 963 MB (see 12.6), `models/` 209 MB |

Desktop remains viable later; the work in sections 3 and 4 is a prerequisite for it too —
and section 4 is now done, so that prerequisite is half paid.

### 1.2 Two databases, both SQLite

`sbpeye.db` (corpus, shipped in git) and `sbpeye_app.db` (runtime state, never shipped).
LLM traces already have a third file, `sbpeye_debug.db`, and are unaffected.

`settings` lives with runtime state rather than with the corpus despite being operator
configuration, because the Settings UI writes it at runtime: shipped with the corpus, every
saved provider change would be reverted the next time that file was replaced.

Postgres is not needed. With corpus writes restricted to one admin (1.3), the corpus has a
single writer, and the app database's write volume is a handful of testers' chat messages.

### 1.3 Corpus writes are admin-only

Sync, AI generation, refresh and link discovery become admin-gated. This makes the corpus
**single-writer by policy**, which is what actually matters for correctness, and it caps
LLM spend from the corpus side.

It does **not** make the corpus read-only, so the seeding work in section 5 is still
required. Three write paths do not fit the rule and are handled individually in sections
3, 6 and 7.3.

### 1.4 The corpus is a seed, not a live file

The `sbpeye.db` committed to git becomes a read-only seed shipped inside the image. The
running app opens a copy on the Railway volume. This is what stops the tracked file showing
permanently dirty in git, which is the symptom that prompted the split.

---

## 2. Database split — complete

### 2.1 Landed

| Area | Where |
|---|---|
| `AppBase`, `app_engine`, `AppSessionLocal`, `get_app_db` | `database.py` |
| Generalised relocation (`_relocate_tables`) shared with the trace split | `database.py` |
| `_relocate_app_tables()`, `_ensure_app_columns()` | `database.py` |
| Five models moved to `AppBase`; cross-DB `ForeignKey`s dropped | `models.py` |
| `WorkspaceCircular.circular` relationship replaced by `_load_workspace_circulars` | `api/serializers.py` |
| Workspace, settings and chat endpoints taking both sessions | `main.py` |
| `get_ai_client` reading settings from its own app session | `ai.py` |
| `debug_setting_enabled` reading from the app database | `llm_debug.py` |
| Trace-console routes reading settings from the app session | `api/debug.py` |
| `sbpeye_app.db` gitignored | `.gitignore` |

Three tests were added (`tests/test_llm_debug.py`), alongside the existing trace-split ones:

| Test | Asserts |
|---|---|
| `test_runtime_tables_stay_out_of_the_corpus_metadata` | The five tables are on `AppBase`, not `Base` |
| `test_corpus_session_cannot_see_runtime_tables` | A corpus session raises `no such table` for each of the five |
| `test_relocate_moves_legacy_runtime_state_and_drops_it` | Rows move with content intact, originals are dropped, second run is a no-op |

Verified against the live files: `sbpeye.db` no longer contains any of the five tables, and
`sbpeye_app.db` contains exactly those five.

### 2.2 Closed items

1. **Full-suite verification.** Done — 6 failed / 569 passed, matching the environment
   baseline on clean `main` exactly (measured on Windows; the Linux baseline is different,
   see 0.4). The 6 are the known set (`test_attachments`,
   `test_llm_debug::test_only_gateway_calls_chat_completions_create`, four in
   `test_routes_smoke` around document redownload).

2. **`upsert_settings` call sites.** Confirmed. Every writer (`ai.py:796`,
   `embeddings.py:106`, `main.py:1919/1926/1928`) is reached from the `POST /api/settings`
   route, which now declares `Depends(get_app_db)`.

3. **Regression guard.** Added — `test_corpus_session_cannot_see_runtime_tables`, plus the
   metadata-level guard. This was not hypothetical: the bug it guards against occurred
   during implementation (see 2.4).

### 2.3 Deliberately deferred

**`sync_status` and `ai_generation_jobs` are still on `Base`.** They are operational runtime
state by nature and would normally belong in the app database. Left in the corpus for now
because their writers — `circular_ai._run_generation_job`, `laws_ai.run_law_generation_job`,
`main._run_circular_sync` — interleave job-row commits with corpus writes inside a single
session. `update_progress` (`circular_ai.py:203`) commits the job row and the circular
together. Splitting them is not a `Depends` change; it is a rework of transaction boundaries
in three of the most failure-sensitive functions in the codebase, with no shared transaction
available across two SQLite files.

The cost of leaving them is that job history from the build machine ships inside the seed.
**Decision needed** (10.1); low consequence, should not block.

### 2.4 Bug found during implementation

`api/debug.py` declared `Depends(get_db)` and passed that corpus session into
`debug_setting_enabled()`, which queries `settings`. Every `/api/debug/status` call logged
`no such table: settings` and silently reported tracing as disabled.

The root cause was a naming ambiguity, not a logic error: helpers took a parameter called
`db` that had quietly become an app session. Those were renamed to `app_db`
(`_ordered_chat_messages`, `_truncate_chat_messages`, `_ensure_default_workspace`,
`_get_workspace_for_chat_session`, `_settings_payload`) so the mismatch is visible at the
call site. Worth remembering when sections 3 and 7 add more two-session routes: the
convention is now `db` = corpus, `app_db` = runtime state, and any helper that breaks it is
a latent 500.

### 2.5 Verification

Every app-table query must run through an app session. This finds violations:

```bash
grep -rn "[^_a-z]db\.query(\(ChatSession\|ChatMessage\|ResearchWorkspace\|WorkspaceCircular\|Settings\)\b" src/sbpeye --include=*.py
```

Hits inside helpers whose session is a *parameter* (`upsert_settings`,
`_ensure_default_workspace`, `_get_workspace_for_chat_session`, `_ordered_chat_messages`,
`AIConfig.from_db`, `EmbeddingConfig.from_db`) are fine — check their callers instead.
Hits in a route body that also declares `Depends(get_db)` are bugs.

Direct check that the boundary holds:

```bash
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); from sbpeye.database import SessionLocal; from sbpeye.models import ChatSession; SessionLocal().query(ChatSession).first()"
```

This **must** raise `no such table: chat_sessions`. If it succeeds, the relocation has not
run against that file and the two databases are out of sync. Confirmed passing against the
live `sbpeye.db`, and now also asserted by `test_corpus_session_cannot_see_runtime_tables`.

---

## 3. Attachment and law cache paths — resolved without moving the column

### 3.1 The problem

`Attachment` mixes two kinds of data in one table. `content_text`, `extraction_status` and
`filename` are corpus — derived once, identical for every deployment. `local_path` is not:
it records where *this machine* cached the file, and it is written by
`_ensure_document_cached` (`main.py`) on any cache miss, from an ordinary user-initiated
document view.

Under 1.3 that write has no admin to attribute it to, and gating it would break PDF preview
for everyone.

This is not an edge case. Measured on the current corpus:

```
attachments total          1466
  with local_path          1368  (93%)
  extracted                1150  (78%)
```

All 1,368 of those paths point inside `attachments/`, which is gitignored and will not exist
in the image. Every one is a guaranteed cache miss on first access after deploy, so the
first tester to open any PDF triggers a corpus write.

Search is unaffected — `content_text` is in the database, so attachment text is searchable
without the files. Only preview and download need them.

### 3.2 The change

Add a `DocumentCache` table on `AppBase`:

| Column | Notes |
|---|---|
| `document_id` | PK. `Attachment.id` or `CachedDocument.id`; no FK, cross-database |
| `document_kind` | `attachment` \| `standalone`, so the two id spaces cannot collide |
| `local_path` | Relative to the data directory (section 4) |
| `fetched_at` | For future eviction; nothing reads it yet |
| `error` | Last download failure, replacing `Attachment.extraction_error` for the download arm only |

Then:

1. Rewrite `_cached_document_path` and `_ensure_document_cached` (`main.py`) to read and
   write `DocumentCache` through an app session instead of mutating the `Attachment` row.
2. Update the other readers of `local_path`: `api/serializers.py` (`_document_payload` and
   the attachment path guard), `main.py` law-version download routes, `scraper/circulars.py`
   (`download_attachment` / `process_attachment`), `cli/commands.py`.
3. Keep `Attachment.local_path` as a column for now. Dropping it means a table rebuild in
   SQLite, and the CLI's offline paths still populate it usefully when scraping locally.
   Treat it as deprecated for the served application; the served path reads `DocumentCache`.

### 3.3 Watch for

`RegDocumentVersion.local_path` has the same shape and the same problem — law PDFs are
archived under `attachments/laws/`. It is admin-written during laws sync, so it fits 1.3 and
does not strictly need to move.

> **Correction (3.5).** This section originally claimed "the *download-on-miss* path for law
> versions (`main.py` law document routes) is user-reachable." That was **false when
> written**: `download_law_file` had exactly one caller, `scraper/laws.py:662`, inside sync.
> The served routes returned 404 and never refetched. A download-on-miss path was
> subsequently added deliberately, with hash verification — see 3.5.3.

**See 3.4 — for law versions, "download-on-miss" is not a safe assumption without the hash.**

### 3.4 Counter-analysis: findings from reading the `local_path` call sites

Recorded for evaluation before section 3 is implemented. These do not contradict 3.1's
measurements, which hold; they qualify what "it is a cache" means per table, and surface two
costs the change carries.

#### 3.4.1 There are three `local_path` columns and they are not the same kind of data

| Column | Rows with a path | Reproducible? |
|---|---|---|
| `Attachment.local_path` | 1368 / 1466 | **Yes** — cache |
| `CachedDocument.local_path` | 0 / 0 | **Yes** — cache |
| `RegDocumentVersion.local_path` | 108 / 116 | **No** — archive |

For the first two, the path is a pure function of data already in the row —
`attachment_id(circular_id, url)` plus the file type (`scraper/circulars.py:259-263`) — and
the bytes re-fetch from `original_url`. 3.1's framing is exactly right for these.

`RegDocumentVersion` is different in kind. SBP replaces law PDFs in place and keeps no
history; `download_law_file` is explicit that an existing archive file is never overwritten
because "that copy is the historical record, and SBP does not keep another one"
(`scraper/laws.py:571-576`), and the module docstring calls the archive the reason SBPEye
"is the historical record the site itself does not keep" (`scraper/laws.py:5-9`).

The corpus already holds **2 non-current versions** whose bytes exist nowhere else. For
those, re-downloading `file_url` fetches the *current* edition — different bytes by
definition, which is the entire reason `content_hash` exists. A "download on miss" path for
a superseded law version does not restore it; it silently replaces a historical record with
today's text.

**Implication:** whatever section 3 does, `RegDocumentVersion.local_path` should not be
treated as a cache entry, and its files deserve *stronger* durability than the corpus
database itself. This is the least reproducible data in the system.

#### 3.4.2 Moving the column does not move the bytes

`local_path` is a pointer; the 638 MB of `attachments/` is what has to survive a deploy.
Sections 4 and 5 already handle that by putting the tree on the volume — which is the change
that actually fixes staleness.

Note the interaction: if `attachments/` were ever *not* on the volume, moving the pointer to
a persistent `sbpeye_app.db` makes things worse rather than better. Today pointer and corpus
travel together and go stale together; split, a persistent app database would hold confident
pointers into a filesystem that gets wiped on every redeploy. Section 3 is therefore only
safe **after** section 4, not before — the sequencing diagram in 0.3 already has this right,
but the dependency is load-bearing rather than incidental.

#### 3.4.3 It splits a write that is currently one transaction

`local_path` is written in the same commit as `content_text` and `extraction_status` —
`scraper/circulars.py:466-482` and `scraper/laws.py:695-706`. Across two SQLite files there
is no shared transaction, so a sync can commit extracted text to the corpus and fail to
commit the path to the app database, leaving a row that claims `extraction_status =
"extracted"` with no file behind it.

That drift class already exists and is already known: `sbpeye cache check-stale`
(`cli/commands.py:1692`) exists specifically to reconcile all three tables' paths against the
filesystem, and carries a comment about a `--prune` run that nearly deleted the laws archive.
Splitting the pointer from the content it describes adds a third participant to that
reconciliation.

If section 3 proceeds, `check-stale` needs updating in the same change, not afterwards.

#### 3.4.4 How strong is the motivation, really?

3.1's argument is policy consistency with 1.3: a user-triggered PDF open writes the corpus,
and there is no admin to attribute that write to.

Worth separating two things 1.3 bundles:

- **Single-writer correctness.** Not actually at risk. Section 9.2 establishes one replica,
  and both the Chroma client and the `threading.Lock` sync guards already require a single
  process. One process writing its own SQLite file on a cache miss is not a correctness
  problem.
- **Cost control.** Not applicable — downloading a PDF from sbp.org.pk costs nothing and
  calls no LLM.

What remains is tidiness plus the section 5.1 reseed rule: corpus rows mutated on the
deployment are rows an admin would lose if `SBPEYE_RESEED=1` were ever used. That is real,
but it applies equally to every other corpus write the deployment makes, and 5.1 already
handles it by refusing to reseed silently.

#### 3.4.5 Options

| Option | Cost | Notes |
|---|---|---|
| **A. Implement 3.2 as written** | ~6 files, plus `check-stale` | Cleanest end state. Do it after section 4 |
| **B. Overlay with fallback** | Same files, simpler migration | `DocumentCache` is consulted first; the corpus column stays as the shipped default and is never written by the served app. Keeps the corpus self-consistent, so 3.4.3 stops being a correctness issue and becomes a staleness one |
| **C. Defer entirely** | Zero | Sections 4 and 5 make the corpus writable-but-not-shipped, which removes the persistence problem. Leaves 1.3 with a documented exception |

**Suggested:** B, and in all three options exclude `RegDocumentVersion.local_path` from the
move per 3.4.1. Superseded by 3.5.

### 3.5 Resolution — the column did not need to move

Decided and implemented. 10.3 is closed. A/B/C turned out to be the wrong axis: it asks
*which table the pointer lives in*, and the defect was that **a read path was calling a
re-ingest**.

#### 3.5.1 What the read path was actually doing

3.1 describes a user PDF view as writing `local_path` — one column. It was not.
`_ensure_document_cached` called `process_attachment(force_download=True)`, which commits
**seven** columns: `local_path`, `filename`, `original_url`, `file_type`,
`extraction_error`, `content_text` (re-extracted) and **`is_vectorized = 0`**.

That last one is a ledger with four consumers:

| Consumer | Effect of the reset |
|---|---|
| `scraper/circulars.py:1125` | `if attachment.is_vectorized: continue` — the row becomes re-index work |
| `cli/commands.py:241` | `filter(is_vectorized == 0)` selects it for embedding |
| `main.py` document payload | Reported as unindexed through the API |
| `chat_retrieval.py:186` | The model is told `indexed=no` for a retrievable source |

On a deployment `attachments/` starts empty, so all 1,368 rows with a path are guaranteed
misses and any tester opening any PDF flips that row's flag.

**What this is not.** It does not invalidate or corrupt the vector store. Chunk ids are
positional (`f"{doc_id}__chunk_{i}"`, `scraper/circulars.py:1030`) and
`_replace_document_chunks` deletes then re-adds, so re-indexing identical text reproduces an
identical store — the same bytes re-downloaded give the same `content_text`, the same chunks
and the same embeddings. The harm is a **false ledger entry** that the API and the chat
context both repeat, plus redundant re-embedding work. Worth fixing, not an emergency.

Two things checked and cleared: extraction uses `pdfplumber` (`scraper/circulars.py:348`),
not docling, so re-extraction is reproducible in the container; and no attachment of a type
the extractor reports as unsupported currently holds text, so the rewrite cannot silently
blank one.

#### 3.5.2 The fix

`_ensure_document_cached` now splits on intent:

| Call | Behaviour |
|---|---|
| `refresh=True` | Unchanged — `process_attachment`, a full re-ingest. This is what a refresh means |
| plain cache miss | `download_attachment` only; writes `local_path` and nothing else |
| failed download | **No commit at all.** The error is set on the in-memory row for the route to build its 502 from, and is discarded with the session |

The `CachedDocument` arm eight lines below was already exactly this; the `Attachment` arm was
the outlier, and now matches it.

With the write reduced to one pointer column, 3.4.4's own reasoning holds without
qualification — no correctness risk (one replica, one process, one writer), no cost risk (a
PDF fetch calls no LLM) — and its last substantive argument, the reseed rule, disappeared when
section 5 became a manual upload. **Option C: the table move is deferred, and `check-stale`
needs no change**, which drops 3.4.3's coupling entirely.

#### 3.5.3 Law versions now refetch, verified by hash

The opposite change, for the opposite reason. `GET /api/laws/{document_id}/file` used to 404
whenever the archived file was absent, which on a fresh volume is every law PDF. It now
refetches through `_ensure_law_version_cached`.

This is only safe because of the hash. SBP replaces law PDFs in place, so `file_url` always
serves whichever edition is current; for a superseded version those are different bytes by
definition (3.4.1). Bytes are therefore accepted for a version **only if they hash to the hash
that version is identified by**:

| Fetched hash | Outcome |
|---|---|
| Matches the requested version | Archived, `local_path` recorded, file served |
| Matches a sibling version | That sibling's `local_path` is recorded; the request still fails, naming the reason |
| Matches no version | Nothing is written. The bytes stay in the archive under their own name, where the next sync finds them |

The archive cannot be damaged even in the mismatch case: `download_law_file` names its
destination `_archive_name(content_hash, url)` from the hash of what it actually fetched
(`scraper/laws.py:561`), so a replaced edition lands under a *different* filename and
physically cannot overwrite the historical record. Creating a version row and deciding
`is_current` across siblings stays with sync, where 1.3 puts it.

#### 3.5.4 Tests

`tests/test_routes_smoke.py`. The three existing redownload tests asserted the old contract
(they monkeypatched `process_attachment` and required `force_download=True` on a plain miss)
and were rewritten; a shared `_unexpected_reingest` guard now fails any test in which a plain
miss reaches the re-ingest path.

| Test | Asserts |
|---|---|
| `test_document_content_redownloads_missing_attachment_file` | Serves the refetched bytes; `content_text` and `is_vectorized` survive untouched |
| `test_document_content_reports_failed_redownload` | 502 carries the reason; the row is left exactly as it shipped |
| `test_ensure_document_cached_redownloads_missing_attachment` | Unit-level equivalent |
| `test_ensure_document_cached_refresh_reingests` | `refresh=True` still calls `process_attachment` |
| `test_law_file_redownloads_when_the_hash_matches` | Refetched, accepted, `local_path` recorded |
| `test_law_file_refuses_when_the_live_file_is_a_different_edition` | 404 naming the reason; no row re-pointed at the wrong bytes |

Suite: 1 failed / 578 passed, the baseline failure only (0.4).

---

## 4. Separate code location from data location — complete

### 4.1 The problem

`PROJECT_ROOT = Path(__file__).resolve().parents[2]` (`database.py`, `env.py`) derives every
mutable path from where the source file happens to sit. There are roughly 40 call sites
across `database.py`, `env.py`, `main.py`, `api/serializers.py`, `documents.py`,
`embeddings.py`, `checklist.py`, `scraper/circulars.py`, `scraper/laws.py` and
`cli/commands.py`.

Everything mutable is affected: both databases, `chroma_db/`, `cache/`, `attachments/`,
`.env.local`, the FastEmbed model cache (`embeddings.py`) and the docling parse cache
(`checklist.py`).

In a container the source lives in the image and the writable volume is mounted elsewhere.
Every one of those writes lands on the ephemeral layer and is lost on redeploy.

### 4.2 The change

Introduce a data root, separate from the package root:

```python
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # code; keep for package assets
DATA_ROOT = Path(os.getenv("SBPEYE_DATA_DIR") or PROJECT_ROOT)
```

Defaulting to `PROJECT_ROOT` keeps every existing local checkout and the whole test suite
working unchanged, which is what makes this a mechanical refactor rather than a risky one.

Then repoint, in this order:

| Path | Currently | Becomes |
|---|---|---|
| `sbpeye.db` | `PROJECT_ROOT` | `DATA_ROOT` (see section 5 — seeded, not the tracked file) |
| `sbpeye_app.db` | `PROJECT_ROOT` | `DATA_ROOT` |
| `sbpeye_debug.db` | `PROJECT_ROOT` | `DATA_ROOT` (already env-overridable) |
| `chroma_db/` | `PROJECT_ROOT` | `DATA_ROOT` |
| `attachments/`, `cache/` | `PROJECT_ROOT` | `DATA_ROOT` |
| `.env.local` | `PROJECT_ROOT` | `DATA_ROOT` — but see 4.3 |
| FastEmbed model cache | `cache/models` under package root | `DATA_ROOT/cache/models` |
| docling parse cache | `cache/parses` under package root | `DATA_ROOT/cache/parses` |

The path-containment guards that resolve a candidate against an `attachments` root
(`main.py`, `api/serializers.py`) must be repointed too. They are a security control, not
bookkeeping — if they keep comparing against `PROJECT_ROOT` while files are written under
`DATA_ROOT`, they fail open or closed depending on layout, and neither is acceptable.

### 4.3 `.env.local` in a container

`env.set_managed_env_value` writes `.env.local` from the Settings UI. On Railway the
container filesystem is ephemeral, so a rewritten `.env.local` is lost on redeploy unless it
sits on the volume.

Two options, both acceptable for a test deploy:

- **Point it at `DATA_ROOT`** — the Settings UI keeps working and changes persist. Simplest,
  and consistent with everything else in this section.
- **Make it read-only in production** — provider config comes from Railway environment
  variables, and the Settings UI's secret-writing paths return an error. More correct for a
  real deployment; more work, and it removes a knob that is genuinely useful while testing.

**Recommendation: the first**, with the second noted as production hardening. Either way,
`load_app_env` already gives process environment variables precedence over file values
(`env.py`), so Railway variables win over anything written to the file — which is the
behaviour you want. **Taken:** `MANAGED_ENV_FILE` is `DATA_ROOT / ".env.local"`. 10.4 closed.

### 4.4 What landed, and why it was six lines

This section estimated "roughly 40 call sites across 10 files." That is the right count of
*usages*, but nearly all of them reach the root through `from ..database import
PROJECT_ROOT`, and — the fact that collapses the work — **no use of `PROJECT_ROOT` was ever
a code path.** Every one is a database, a cache, or the attachment tree. Package assets are
resolved separately, from `Path(__file__).resolve().parent` (`main.STATIC_DIR`).

So the root is defined once, in `env.py`, which imports only stdlib and `dotenv` and is
therefore safe for every other module to import from:

```python
CODE_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.getenv("SBPEYE_DATA_DIR") or CODE_ROOT).resolve()
PROJECT_ROOT = DATA_ROOT   # alias; see below
```

| File | Change |
|---|---|
| `env.py` | Defines `CODE_ROOT` / `DATA_ROOT` / `PROJECT_ROOT`; `.env` and `.env.local` repointed |
| `database.py` | Imports both from `env`; corpus, app and debug databases and `chroma_db/` repointed |
| `documents.py` | Two inline `parents[2]` → `DATA_ROOT` |
| `checklist.py` | `PARSE_CACHE_DIR` → `DATA_ROOT` |
| `embeddings.py` | `_project_root()` deleted (one caller) → `DATA_ROOT`; unused `Path` import dropped |

**`PROJECT_ROOT` was kept as the exported name** rather than renamed at ~40 sites. Two
reasons beyond diff size, both of which 4.2 half-anticipated:

- The attachment path-containment guards (`main.py:187`, `1744`, `1795`,
  `api/serializers.py:419`) compare a candidate against this root. 4.2 warns they "fail open
  or closed" if they drift from the tree they guard. One name makes drift impossible; two
  names make it a live risk on every future edit.
- Roughly 20 test sites monkeypatch `PROJECT_ROOT` on `laws`, `scraper`, `main`,
  `database` and `cli_commands`. Renaming breaks all of them for no behavioural gain.

**The four inline re-derivations were the actual trap.** `documents.py` (twice),
`checklist.py` and `embeddings.py` each computed `Path(__file__).resolve().parents[2]`
locally instead of importing it. An env override of `PROJECT_ROOT` alone would have left all
four silently writing to the ephemeral image layer — the exact failure this section exists to
prevent, and invisible until a redeploy ate the data. This grep must return exactly one hit,
the `CODE_ROOT` definition:

```bash
grep -rn "parents\[2\]" src/
```

**Verified.** With `SBPEYE_DATA_DIR` unset every path resolves exactly where it did before.
With it set, all seven mutable paths follow — both databases, the debug database,
`chroma_db/`, `.env.local`, `cache/parses`, `cache/models` — plus `ATTACHMENTS_DIR` and
`HTML_CACHE_DIR`, while `main.STATIC_DIR` correctly stays in the source tree. Suite: 1 failed
/ 575 passed, identical to the stashed baseline, same single failure (0.4).

**One constraint now documented in the code:** `SBPEYE_DATA_DIR` must be a real process
environment variable. It is read before any env file loads, because it is what says where
those files are; setting it in `.env.local` cannot work.

---

## 5. Get the corpus onto the volume

### 5.1 The route: upload it directly — decided

The corpus and the vector store are uploaded to the Railway volume with the CLI, once, by
hand. Nothing is copied out of the image and **no seeding code is written**.

```bash
railway volume -v <volume> files upload ./sbpeye.db /sbpeye.db
railway volume -v <volume> files upload ./chroma_db /chroma_db
```

`files upload` accepts a directory as well as a file (`--concurrency` defaults to 32), so the
store goes up as a directory rather than a tarball — a tarball would have to be extracted
inside the container, and Railway documents no shell for that. `/` is the volume root, which
is the `/data` mount; confirm placement with `railway volume files list /`.

This works only because of section 4: `SBPEYE_DATA_DIR=/data` is what makes the application
look on the volume instead of beside its own source. With that set, uploaded files are simply
found. There is no code change at all in this section.

### 5.2 What this removes

Three things this plan previously specified are now unnecessary and should not be built:

| Was | Why it is gone |
|---|---|
| Seed copied from the image on first boot | Nothing is in the image to copy |
| `.corpus-seed` version marker | Nothing auto-copies, so there is no version to compare |
| `SBPEYE_RESEED=1` opt-in | The rule it protected — "never silently overwrite admin work" — is enforced by the absence of any automatic overwrite |

Updating the corpus later is the same command with `--overwrite`, run deliberately, with the
service stopped. The protection is that it is manual, which is stronger than a marker file and
is zero code.

### 5.3 What goes on the volume

| On the volume | Not on the volume |
|---|---|
| `sbpeye.db` (69 MB) — uploaded | `files/circulars/` (570 MB) — fetched on demand |
| `chroma_db/` (486 MB) — uploaded | `files/cache/` (318 MB) — regenerated |
| | `models/` (209 MB) — belongs in the image, see 5.4 |
| `sbpeye_app.db` — created empty on first boot | |
| `sbpeye_debug.db` — created on demand | |
| `.env.local` — written by the Settings UI (4.3) | |

**Volume sizing.** Railway gates volume size by plan: Free/Trial 0.5 GB, Hobby 5 GB, Pro
50 GB. 0.5 GB does not fit the corpus alone. **Hobby is the floor, and is the plan being
taken.** Budget roughly 555 MB at rest, ~1.2 GB once attachments cache, ~1.9 GB if the HTML
cache is also allowed to fill — comfortably inside 5 GB.

### 5.4 Rules that still apply

1. **Do not upload into a live store.** Chroma's `PersistentClient` is single-process, and
   writing into `chroma_db/` while the application holds the HNSW segment open is exactly
   what `_require_exclusive_vector_store` (`cli/commands.py:891`) exists to prevent. Stop the
   service, upload, start it. This is the one way to corrupt the store on this route.
2. **Volumes are not mounted at pre-deploy time**, so this cannot be scripted as a deploy
   step even if that later looks attractive. CLI upload is the mechanism.
3. **The chromadb format coupling is still live.** The store was built by the local
   `chromadb`; the container's pinned `chromadb>=1.5.9` has to open it. 5.3 originally called
   this the single most likely thing in this plan to fail in a way that is annoying to
   diagnose, and choosing upload over rebuild keeps that risk rather than removing it. Test
   one boot before relying on it. If it fails, 5.6 is the fallback.
4. **Bake the FastEmbed model into the image.** `EmbeddingConfig.cache_dir()` resolves to
   `DATA_ROOT/models` (12.6), overridable with `FASTEMBED_CACHE_PATH`. Left on the volume it
   is 209 MB of third-party weights being treated as application data; unset, first boot
   downloads them before it can embed anything. Point `FASTEMBED_CACHE_PATH` at a baked-in
   image path.
5. **FTS on first boot.** `_warm_up_search_index` (`main.py`) runs `backfill_fts` in a
   background thread on every boot and writes to the corpus if the FTS tables are empty. The
   uploaded `sbpeye.db` already contains populated `circulars_fts` and `laws_fts`, so this is
   a no-op in practice — but a corpus uploaded without them would make first boot perform a
   large corpus write with no admin involved. Worth checking before uploading.

### 5.5 The vector store cannot ship in git

Recorded because it is what forced the upload route, and because it will come up again the
first time someone assumes the repo is self-contained.

`chroma_db/` is **gitignored** (`.gitignore`, `/chroma_db/`) and has never been tracked, so a
build sourced from the repo — which is what a Railway GitHub deploy is — does not have it.
Nor can it be added:

| File | Size | Against GitHub's 100 MB hard per-file limit |
|---|---|---|
| `chroma_db/chroma.sqlite3` | 311 MB | ✗ |
| `…/data_level0.bin` | 152 MB | ✗ |
| remaining 9 files | < 5 MB each | ✓ |

Git LFS was rejected: 486 MB per corpus version against a 1 GB free quota, plus bandwidth
billing. Baking it into the image was rejected because it requires building where `chroma_db/`
exists — locally, then pushing a multi-GB image to a registry — which kills the two-minute
push-to-deploy that 1.1 chose Railway for.

### 5.6 Fallback: the store is reproducible from `sbpeye.db`

Not the chosen route, but the reason the upload route is safe to take: if the uploaded store
turns out to be unopenable by the container's chromadb, or is ever lost, it can be rebuilt
without any of the file trees.

`sbpeye reindex` (`cli/commands.py:937`) rebuilds all 44,106 chunks from the corpus database
alone. The PDFs are vectored, but **not from the files**:

| Source | Chunks |
|---|---|
| attachment | 27,740 |
| circular | 8,486 |
| law | 8,169 |

`attachment_document()` (`scraper/circulars.py:925`) feeds the chunker
`attachment.content_text` and nothing else — no `local_path`, no file read — and
`prepare_index_chunks` routes to `prepare_reference_chunks` (`checklist.py:934`), whose
docstring is explicit that it chunks "without invoking the PDF pipeline". Docling runs on the
checklist and entity paths, never on indexing.

So the input is 16 MB of `content_text` across 1,328 extracted attachments, already inside the
69 MB corpus. The 645 MB of `attachments/` is irrelevant to a rebuild. Cost is time: roughly
18 chunks/s once warm on a desktop CPU puts 44,106 chunks near 40 minutes, and a container
will be slower. That is one rough measurement, not a benchmark.

If this is ever wired in as automatic recovery rather than run by hand, note the hazard:
`reset_collection()` (`database.py:100`) drops the collection and rebinds the module-level
handle that `search`, `chat_retrieval` and `scraper.circulars` bound at import. The CLI can
assume it is alone; a rebuild inside the serving process has readers arriving mid-swap.

---

## 6. Ecodata: scheduled refresh, not request-triggered

### 6.1 The problem

`_get_ecodata_entries` (`main.py`) refreshes on a 1-hour TTL, and the refresh fires from
whichever `GET /api/ecodata/entries` request happens to find the data stale. That request
then blocks on a live scrape of SBP. There is no user identity involved, so no admin gate
can catch it, and it writes to the corpus.

### 6.2 The change

Move the refresh to a background scheduler owned by the application:

1. Remove the TTL check from the request path. `GET /api/ecodata/entries` becomes a pure read
   of `ecodata_entries` and never scrapes.
2. Add a background thread started from `app_lifespan` (`main.py`) that calls
   `scrape_ecodata_index` on an interval, defaulting to 1 hour and configurable via
   `SBPEYE_ECODATA_REFRESH_SECONDS`. `0` disables it.
3. Run it once shortly after startup — not synchronously in the lifespan, which would delay
   the container passing its health check.
4. Keep `force_refresh` reachable as an **admin-only** endpoint, for triggering a refresh
   without waiting.

The existing `SyncStatus.ecodata_index_time` column continues to record the last successful
refresh, so the UI needs no change.

### 6.3 Note

This is the one corpus writer that remains un-gated by design, since it is the application
refreshing its own scraped index on a schedule rather than anyone acting. It is a single
scraper writing one small table, and it is serialised by being the only thread that runs it.

---

## 7. Authentication and admin gating

### 7.1 Scope

Test deploy: email plus password, no verification email, no password reset, no OAuth. The
goal is a boundary between "a known tester" and "the internet", plus a second boundary
between testers and the admin. Anything more is out of scope until the deployment stops
being a test.

### 7.2 Schema

On `AppBase`, in `sbpeye_app.db`:

| Column | Notes |
|---|---|
| `id` | UUID |
| `email` | Unique, lowercased on write |
| `password_hash` | Argon2 or bcrypt — **never** a bare hash. Adds one dependency |
| `is_admin` | Integer flag. Two roles is all this needs; no RBAC |
| `created_at`, `last_login_at` | |

Sessions: signed HTTP-only cookies, so no sessions table. `itsdangerous` is already an
indirect dependency via Starlette. Set a `SBPEYE_SECRET_KEY` env var; refuse to start
without it in production rather than defaulting to something guessable.

First admin: seeded from `SBPEYE_ADMIN_EMAIL` / `SBPEYE_ADMIN_PASSWORD` on startup if no
admin exists. Self-registration should be closed or invite-only — an open registration form
on a test deploy is an open door to your LLM budget.

### 7.3 Endpoints to gate as admin

Everything that writes the corpus:

| Endpoint | Section |
|---|---|
| `POST /api/circulars/sync` | sync |
| `POST /api/circulars/{id}/generate` | generation |
| `POST /api/laws/**` generation and sync equivalents | generation |
| `POST /api/circulars/{id}/refresh` | refresh |
| `POST /api/circulars/open` | link discovery — **feature loss, accepted** |
| `POST /api/settings`, `POST /api/settings/**/test` | provider config |
| `/debug` and the trace APIs | already gated by `LLM_DEBUG_ALLOWED`; add admin on top |
| Ecodata forced refresh | section 6.2 |
| `POST /api/documents/resolve?refresh=true` | re-ingest — the one document path that still rewrites corpus rows (3.5.2) |

`POST /api/circulars/open` being admin-only means a tester pasting an unknown SBP link gets
a refusal rather than an indexed circular. Accepted deliberately (1.3). The endpoint should
return a clear message saying so, not a bare 403 — otherwise it reads as a bug.

### 7.4 Per-user scoping

`research_workspaces` and `chat_sessions` need a `user_id`. Two sub-decisions:

**Shared or per-user workspaces?** For a small tester group, shared workspaces are arguably
*better* — testers see each other's pinned sets, which surfaces more feedback. Per-user is
more correct and is what production would need.

If per-user, `DEFAULT_WORKSPACE_ID = "default"` (`api/serializers.py`) has to go. It is a
fixed global string, and `_ensure_default_workspace` looks up `is_default == 1` with no owner
filter. Every user's default workspace would collide on the same primary key — and because
`_workspace_chat_session_id` derives chat session ids from workspace ids
(`WORKSPACE_CHAT_SESSION_PREFIX + workspace_id`), they would collide on chat sessions too.

The fix: real UUIDs per user, `is_default` unique per user rather than globally,
`_ensure_default_workspace(db, user_id)`. Do it in the same pass as adding `user_id`, not
after.

**Recommendation: shared workspaces for the test deploy**, per-user chat. Chat is where the
privacy expectation actually sits, and it avoids the `DEFAULT_WORKSPACE_ID` rework until it
buys something. Revisit before any wider release.

### 7.5 Cost exposure that admin gating does not cover

Chat is user-triggered, writes only to the app database, and is therefore outside 1.3
entirely. Every tester conversation hits the configured provider, and with LM Studio
unavailable in the cloud that is a paid API.

Chat is likely the largest per-user cost in the deployment. If spend matters, it needs its
own control — a per-user daily message quota, a cheaper model for chat than for generation
(`AI_CHAT_MODEL` already exists and is separately configurable), or both. This is not
optional if registration is ever opened.

---

## 8. Prepare corpus content before deploying

### 8.1 The finding

Measured on the current corpus:

```
circulars                   3652
  with summary                 5   (0%)
  with tags                 3008   (82%)
  with compliance_checklist    4   (0%)
law versions                 116
  with summary                 2
```

Tags are in good shape. Summaries and checklists are effectively ungenerated.

Combined with 1.3, this means testers would browse 3,652 circulars, see no summaries and no
checklists, and have no button to generate them. The two most visible AI features would
appear broken rather than gated.

### 8.2 The change

Summaries are corpus content — "generated once, shared by all users" — so generating them is
a **build step**, run before the seed is committed, not something deferred to runtime.

```bash
sbpeye circulars summarize --limit N
```

Full coverage is not required for a test deploy. A few hundred of the most recent circulars,
plus anything the testers are specifically being asked to look at, is enough for the app to
feel alive. Checklists are more expensive per item and can stay sparse.

Do this before building the seed, so the generated content ships inside `sbpeye.db` and every
tester gets it for free. Doing it after deploy means generating through the admin UI against
the volume copy, which works but is slower and puts LLM cost on the deployment.

---

## 9. Railway deployment

### 9.1 Container — written, and much larger than this section assumed

Multi-stage `Dockerfile`:

1. **Frontend stage** — `node:20`, `npm ci`, `npm run build`, emit `frontend/dist`.
2. **Python stage** — `python:3.12-slim`, `uv sync --frozen`, copy `src/`, copy the built SPA
   into `src/sbpeye/static/spa/`, copy the seed corpus.

Expect a large image. `torch` (500 MB) arrives via `docling`, `onnxruntime` (291 MB) via
`fastembed`. Since every `docling` import is function-local (`checklist.py`), a build that
omits it would shed roughly 700 MB at the cost of checklist reference-unit extraction — worth
measuring if build times become painful, but not worth doing pre-emptively.

`.dockerignore` must exclude `.venv/`, `files/`, `models/`, `frontend/node_modules/`,
`sbpeye_debug.db`, `benchmarks/results/` and — importantly — **`.env.local`**. That file
currently contains live provider keys; baking it into an image layer would ship them.

#### 9.1.1 Written

`Dockerfile` and `.dockerignore` exist and the image builds. Structure is as described
above: `node:20-slim` runs `npm ci && npm run build`, which vite writes straight to
`../src/sbpeye/static/spa` (its configured `outDir`), and the python stage copies that
bundle in already in the layout the app serves from.

Beyond the plan:

- **The embedding model is baked in** with a build-time `TextEmbedding(...)` call rather
  than copied from `models/` — `models/` is gitignored, so a GitHub-sourced build has no
  copy of it (the same trap as 5.5, in a different place). `FASTEMBED_CACHE_PATH=/opt/models`
  points the app at it. This must stay in step with `EMBEDDING_MODEL`.
- **`libgomp1` is required**, not optional: onnxruntime fails at import without it, and
  fastembed is on the hot path. `libgl1` and `libglib2.0-0` are for opencv via docling and
  matter only when a checklist is generated.
- **No data ships.** `sbpeye.db` is excluded along with `chroma_db/` and `files/`, per 5.3.

`.dockerignore` takes the build context from **7.5 GB to 17.9 MB**.

#### 9.1.2 The image is 19.3 GB

Two separate causes, measured rather than estimated. This section's "expect a large image"
was an understatement by more than an order of magnitude.

**1. A 5.5 GB uv cache was baked into the layer.** With `UV_LINK_MODE=copy`, `uv sync`
leaves every downloaded wheel in `/root/.cache/uv`, and a later `rm` in its own `RUN`
frees the files without shrinking the layer beneath. Fixed by deleting the cache inside
the same `RUN`. This one was a Dockerfile bug, not a dependency problem.

**2. The venv is 5.8 GB, and 4.9 GB of that is GPU tooling.** Measured inside the image:

| Package | Size |
|---|---|
| `nvidia` (CUDA runtime) | 2.7 GB |
| `torch` | 1.2 GB |
| `triton` | 691 MB |
| `opencv_python.libs` + `cv2` | 188 MB |
| `transformers` | 108 MB |
| `rapidocr` | 33 MB |
| everything else | ~900 MB |

`uv.lock` resolves the CUDA build of torch, so a machine with no GPU carries the whole
CUDA runtime. **`docling` is the sole path to all of it** — confirmed from the lockfile:
`torch <- docling-ibm-models, accelerate, torchvision`, `transformers <- docling-ibm-models`,
`opencv-python <- rapidocr`. Nothing else in the tree needs any of them.

#### 9.1.3 Options for the size

1.1 already recorded that every `docling` import is function-local in `checklist.py`, and
that still holds — verified again, ten imports, all inside functions, no module-level
import anywhere in `src/`. So the package can be absent and the application starts
normally; only checklist generation would fail, and it would fail with `ImportError`
unless that path is given a clear message.

| Option | Effect | Cost |
|---|---|---|
| **Drop `docling` from the deployment** | venv 5.8 GB -> ~0.9 GB | Loses checklist reference-unit extraction. 8.1 measured 4 of 3652 circulars with a checklist, so the feature is close to unexercised |
| **Pin torch to the CPU wheel index** | Removes ~4.6 GB, keeps the feature | Changes `pyproject.toml` and re-locks `uv.lock`, which also changes the local venv. FastEmbed already reports `Backend: CPU` locally, so nothing is lost in practice |
| **Accept it** | — | A multi-GB image on every deploy, against 1.1's whole reason for choosing Railway: that a fix reaches testers in two minutes |

**Decided: drop `docling` from the deployment.** It is now a `[project.optional-dependencies]`
extra rather than a base dependency, installed locally with `uv sync --extra checklist` and
deliberately absent from the image.

**Result: 19.3 GB -> 1.51 GB.** The deployment install resolves 112 packages with zero
torch, CUDA, triton, transformers, opencv or rapidocr, and fastembed, onnxruntime, chromadb
and pdfplumber all intact. `libgl1` and `libglib2.0-0` came out of the Dockerfile with
opencv; `libgomp1` stays, because onnxruntime still needs it.

The missing feature announces itself. Every route into Docling funnels through
`checklist._document_converter`, which now raises `DoclingUnavailable` — "Checklist
extraction needs Docling, which is not installed in this build. Install it with
`uv sync --extra checklist`." A bare `ModuleNotFoundError` behind a 500 would read like a
bug rather than a build without a feature. Covered by
`test_missing_docling_build_says_so_instead_of_raising_import_error`.

Note for anyone syncing locally: `uv sync` without `--extra checklist` will now *remove*
docling from an existing virtualenv.

#### 9.1.4 Smoke test — passed

Built and run locally, no Railway involved. This is verification item 3.

| Check | Result |
|---|---|
| Boot to first response | ~6 s |
| `/healthz` with corpus + store on the volume | `{"status":"ok"}`, all three green |
| **Vector search in the container** | Correct, semantically relevant hits |
| SPA | 200, serving the built bundle |
| FastEmbed | `Backend: CPU`, loaded from the baked-in model — no boot-time download |
| Checklist path | `DoclingUnavailable` with the install hint |
| Boot against an **empty** volume | 200, `vector_store: "ok (empty)"`; creates `sbpeye.db`, `sbpeye_app.db`, `sbpeye_debug.db`, `chroma_db/` |

**The chromadb format risk is retired.** 5.3 called it "the single most likely thing in this
plan to fail in a way that is annoying to diagnose", and 5.4 rule 3 kept it live under the
upload route. A `python:3.12-slim` container running the pinned `chromadb>=1.5.9` opened the
486 MB store built on the development machine and returned correct results from it.

One hardening item noted, not done: the container runs as root, so files it creates on the
volume are root-owned. Fine for a test deploy on Railway, wrong for production.

### 9.2 Volume

One Railway volume mounted at `/data`, with `SBPEYE_DATA_DIR=/data`.

Railway does not allow scaling a volume-attached service past one replica. That is not a
constraint this application chafes against — `chromadb.PersistentClient` is single-process by
design (`database.reset_collection` documents why), and the sync and remote-check guards in
`main.py` are module-level `threading.Lock`s that only work in one process. Single instance
is the correct topology, not a compromise. Worth knowing it is a hard ceiling.

Size it for corpus (69 MB + 486 MB) plus attachment growth, not just the databases. Railway
gates volume size by plan — Free/Trial 0.5 GB, Hobby 5 GB, Pro 50 GB — and 0.5 GB does not fit
the corpus alone. **Hobby is the floor and is the plan being taken**; see 5.3 for the budget.

### 9.3 Environment

| Variable | Purpose |
|---|---|
| `SBPEYE_DATA_DIR` | `/data` |
| `SBPEYE_SECRET_KEY` | Cookie signing. Refuse to start without it |
| `SBPEYE_ADMIN_EMAIL` / `SBPEYE_ADMIN_PASSWORD` | First-admin seeding |
| `AI_PROVIDER`, `AI_BASE_URL`, `AI_MODEL`, `AI_CHAT_MODEL` | **Must be set.** Default is `lmstudio` at `localhost:1234`, which does not exist in the cloud |
| Provider key (`OPENAI_API_KEY`, `GROQ_API_KEY`, …) | Per chosen provider |
| `EMBEDDING_PROVIDER` | Keep `fastembed`. Must match the model the shipped Chroma index was built with (`BAAI/bge-base-en-v1.5`) or vector search returns nonsense |
| `LLM_DEBUG_ALLOWED` | Set `false` unless actively debugging — traces store full prompts |
| `SBPEYE_ECODATA_REFRESH_SECONDS` | Section 6 |

Rotate the keys currently in `.env.local` before any of this goes near a deployed image.
They have been sitting in a working tree, and at least one is a live-looking token.

### 9.4 Health check — done

`GET /healthz` returns `{"status", "checks"}` with `corpus_db`, `app_db` and
`vector_store`; 200 when all three answer, 503 otherwise. Point Railway's health check
at it.

Three decisions worth keeping:

- **It does not touch the LLM provider.** Coupling them would let someone else's outage
  roll the container and take search and browsing down with chat.
  `test_healthz_survives_an_llm_provider_outage` pins this.
- **An empty vector store is healthy**, reported as `ok (empty)`. A store that opens but
  holds nothing is an un-uploaded deployment, which is a legible state rather than a
  broken one.
- **Failures report the exception class, not its message.** The endpoint is
  unauthenticated and a SQLAlchemy or Chroma error carries filesystem paths in its text.

### 9.5 Startup — done

`run.py` already avoided `reload` unless `--dev` or `SBPEYE_DEV=1`. The port was hardcoded
to 8000 and is now `int(os.environ.get("PORT") or 8000)`. This was a real blocker, not a
tidy-up: Railway routes to the port it injects, so the health check would never have
connected and the deploy would have been marked failed with the process running fine.

---

## 10. Open decisions

### 10.1 `sync_status` / `ai_generation_jobs` placement

Corpus (current) or app database. Corpus means build-machine job history ships in the seed;
app means reworking transaction boundaries in three job runners that currently commit job
rows and corpus rows together. Low consequence. Section 2.3.

### 10.2 Workspace sharing model

Shared or per-user. Determines whether the `DEFAULT_WORKSPACE_ID` rework is needed now.
Section 7.4.

### 10.3 `local_path` — whether to move it at all, and how — closed

**Option C — the column stays in the corpus.** The A/B/C question turned out to be aimed at
the wrong thing: the defect was a read path calling a full re-ingest, not which table the
pointer lived in. With that fixed the served write is a single pointer column, which 3.4.4
already judged harmless, and the reseed rule that was its last real argument disappeared when
section 5 became a manual upload.

`check-stale` needs no change, so 3.4.3's coupling is gone. `RegDocumentVersion` refetches on
miss with hash verification rather than refusing outright — a better answer than either option
this decision originally offered. Implemented; see 3.5.

### 10.4 `.env.local` writability in production — closed

Persisted on the volume. `MANAGED_ENV_FILE` is `DATA_ROOT / ".env.local"` as of the section 4
work; Railway environment variables still win over file values. Making provider config
read-only is noted as production hardening in 4.3, not done. Section 4.3.

### 10.5 How the vector store reaches the volume — closed

Uploaded directly with `railway volume files upload`, along with `sbpeye.db`. No seeding code,
no release asset, no rebuild-on-boot. Section 5.1.

The residual risk is the chromadb on-disk format (5.4, rule 3), with `sbpeye reindex` as the
documented fallback (5.6).

### 10.6 File tree consolidation — closed

Done as proposed, including the durability split. Section 12.6.

---

## 11. Verification

Before calling the deployment done:

1. **Test suite** diffed against the baseline on clean `main`, not against zero, and
   measured on the machine in use rather than taken from this document (0.4). ✔ — 1 failed
   / 582 passed, the baseline failure only.
2. **Boundary test**: a corpus session must raise on app tables (section 2.5). ✔ confirmed
   against the live files and now covered by a test.
3. **Cold-start test**: ✔ locally (9.1.4). Booted the image against a volume holding
   `sbpeye.db` and `chroma_db/`: health green and vector search returned correct results,
   which is the proof the container's chromadb opened a store built elsewhere (5.4, rule 3).
   Also booted against an empty volume: 200 with `vector_store: "ok (empty)"`. Still to
   confirm on Railway itself, and the PDF-download-on-demand arm (section 3) is untested in
   a container.
4. **Redeploy test**: change a setting through the UI, redeploy the same image, confirm the
   setting survived and the uploaded corpus was untouched.
5. **Auth test**: every endpoint in 7.3 returns 403 for a non-admin session and 401 for no
   session.
6. **Git cleanliness**: after a full session of use against a deployed instance, `git status`
   in a local checkout must show `sbpeye.db` unmodified. That was the original symptom; it is
   the clearest single signal the split worked.
7. **Data-directory test**: boot with `SBPEYE_DATA_DIR` set and confirm nothing mutable was
   written under the source tree. `grep -rn "parents\[2\]" src/` returning more than the
   single `CODE_ROOT` hit means a path has escaped `DATA_ROOT` (4.4).

---

## 12. File tree consolidation — complete

`attachments/` and `cache/` are two top-level mutable trees where one would do. This section
proposes the merge and, more importantly, records what the merge must not destroy.

### 12.1 Current layout

```
DATA_ROOT/
├── attachments/          645 MB
│   ├── laws/              76 MB   archive — irreplaceable
│   └── <circular-uuid>/  569 MB   downloaded PDFs — re-fetchable from sbp.org.pk
└── cache/                527 MB
    ├── html/             318 MB   scraped pages — re-fetchable
    ├── models/           209 MB   FastEmbed ONNX weights — third-party, not our data
    └── parses/           736 KB   docling parses — regenerable, expensive per item
```

The two names describe *where a file came from*. They do not describe **how much it costs to
lose**, which is the only property that matters when something deletes a directory.

### 12.2 Why a flat merge would be a mistake

`attachments/laws/` is the least reproducible data in the system. 3.4.1 established it: SBP
replaces law PDFs in place and keeps no history, `download_law_file` never overwrites an
existing archive file because "that copy is the historical record, and SBP does not keep
another one" (`scraper/laws.py:571-576`), and **2 superseded editions already exist nowhere
else**.

It currently sits one directory away from 569 MB of trivially re-downloadable PDFs, under a
shared name. That is already uncomfortable, and it has already nearly gone wrong: 3.4.3
records a `sbpeye cache check-stale --prune` run that nearly deleted the laws archive.

A merge that puts the archive and the caches in one flat tree makes that near-miss more
likely, not less. The consolidation is only worth doing if it encodes the durability
boundary rather than erasing it.

### 12.3 Proposal

One tree, split by how much it costs to lose:

```
DATA_ROOT/
└── files/
    ├── laws/                archive     — never auto-deleted, by any code path
    ├── circulars/<uuid>/    re-fetchable — deleting costs a download
    └── cache/               disposable   — `rm -rf files/cache` is ALWAYS safe
        ├── html/
        └── parses/
```

And `cache/models/` leaves `DATA_ROOT` entirely: it is 209 MB of third-party weights being
stored as if it were application data. It belongs in the image, via `FASTEMBED_CACHE_PATH`
(5.4, rule 4).

That gives one top-level file directory instead of two, takes 209 MB off the volume, and
makes the rule statable in one line:

> Exactly one path under `files/` may be deleted by anything: `files/cache/`.

`files/` is a placeholder name; `storage/` reads equally well. Not worth a long discussion.

### 12.4 Cost

**1,479 stored paths must be rewritten.** Every `local_path` is relative and prefixed
`attachments/` — 1,371 in `attachments`, 108 in `reg_document_versions`, none absolute. The
rename is a `mv` plus an UPDATE, but with **two prefixes that must not be conflated**:

| From | To |
|---|---|
| `attachments/laws/…` | `files/laws/…` |
| `attachments/<uuid>/…` | `files/circulars/<uuid>/…` |

Order matters — rewriting the generic prefix first would send the laws archive to
`files/circulars/laws/`.

**The collision with section 3 is resolved.** 10.3 closed as option C, so the pointers stay
in the corpus and there is no `DocumentCache` migration to coordinate with. This is now a
single-pass rename of two corpus columns, which is the cheapest this change was ever going to
be.

**`sbpeye cache check-stale` must be updated in the same change** (`cli/commands.py:1692`).
It walks all three trees and reconciles them against the filesystem; it is also the command
that nearly deleted the archive once. 3.4.3 already flags this requirement for section 3.

**Definition sites are few** — the change itself is small:

| Site | Currently |
|---|---|
| `scraper/circulars.py:33-34` | `HTML_CACHE_DIR`, `ATTACHMENTS_DIR` |
| `scraper/laws.py:46` | `LAWS_ARCHIVE_DIR = ATTACHMENTS_DIR / "laws"` |
| `checklist.py:611` | `PARSE_CACHE_DIR` |
| `embeddings.py:76` | FastEmbed cache dir |
| `documents.py:19` | Re-derives `cache/html` inline — a duplicate of `HTML_CACHE_DIR`; fold it in while there |
| `main.py:188/1745/1796`, `api/serializers.py:420` | Path-containment guards |

**One honest regression:** `rm -rf files/` is shorter to type than `rm -rf attachments/` and
destroys considerably more. The mitigation is that `files/cache/` is the only path any code
is permitted to delete, and `check-stale --prune` must be scoped to it explicitly rather than
walking from `files/`.

### 12.5 Timing

**Before the deploy, not after.** 10.3 is closed, so nothing blocks it.

Nothing here is required for Railway. Section 4 already puts both trees on the volume and
neither is uploaded (5.3), so the deploy works either way. But once the deployment is running
there are two attachment trees to migrate instead of one, so the cheap moment is now, and the
blocker is 10.3 rather than anything in this section.

### 12.6 Landed

The layout is now:

```
DATA_ROOT/
├── files/                963 MB
│   ├── laws/              76 MB   archive - nothing may delete from here
│   ├── circulars/        570 MB   re-fetchable
│   └── cache/            318 MB   disposable
│       ├── html/
│       └── parses/
└── models/               209 MB   third-party weights, not application data
```

**All seven roots are defined once, in `env.py`**, next to `DATA_ROOT`. That module imports
only stdlib and `dotenv`, so every consumer can reach it without a cycle — which matters,
because `documents.py` could not import `HTML_CACHE_DIR` from `scraper.circulars` (that would
close `documents -> scraper.circulars -> checklist -> documents`). Folding that duplicate in
was 12.4's loose end and it is gone.

| File | Change |
|---|---|
| `env.py` | `FILES_ROOT`, `LAWS_ARCHIVE_DIR`, `CIRCULAR_FILES_DIR`, `FILES_CACHE_DIR`, `HTML_CACHE_DIR`, `PARSE_CACHE_DIR`, `MODEL_CACHE_DIR` |
| `scraper/circulars.py` | `ATTACHMENTS_DIR` keeps its name — these are attachments whatever the directory is called — and is now an alias for `CIRCULAR_FILES_DIR` |
| `scraper/laws.py` | `LAWS_ARCHIVE_DIR` imported, no longer derived from the attachments tree |
| `checklist.py`, `embeddings.py`, `documents.py` | Import their root instead of deriving it |
| `main.py`, `api/serializers.py` | Containment guards repointed |
| `cli/commands.py` | `check-stale` restructured; `cache migrate-layout` added |
| `.gitignore` | `/files/`, `/models/`; the old entries kept for unmigrated checkouts |

**The guards now admit only their own tree.** They previously shared
`PROJECT_ROOT / "attachments"`, so an attachment path satisfied the law guard and a law path
satisfied the attachment guard. Verified after migration: 400/400 sampled attachments and
108/108 law versions resolve, and each guard rejects the other tree's paths.

**`check-stale` can no longer reach the archive.** It walks `CIRCULAR_FILES_DIR` and
`HTML_CACHE_DIR` into a prunable list, and `LAWS_ARCHIVE_DIR` into a separate report-only
list that `--prune` does not iterate. 3.4.3's near-miss was possible because safety depended
on every law version being present in the expected set; it is now structural. Post-migration
reconciliation: 0 orphaned attachments, 0 unreferenced archive files, 9 orphaned HTML cache
files.

#### 12.6.1 The migration

Done by a one-shot `sbpeye cache migrate-layout` command: it moved the trees, then rewrote
the stored paths — in that order, so an interrupted run finished on a re-run, the rewrite
being driven by what the rows still said.

**The command has since been removed.** The local corpus is migrated (1371 attachment paths
and 108 law version paths under the new prefixes, none left under `attachments/`, no
backslash separators), `sbpeye.db` carries those paths in git, and the Railway volume starts
empty, so nothing left to run it against. `.gitignore` no longer carries `/attachments/` or
`/cache/` either — an unmigrated checkout now surfaces them as untracked files, which is a
more useful signal than silence.

The one case it would still serve: a checkout that pulls the migrated `sbpeye.db` while
holding an old tree on disk. `files/` is gitignored, so every path would resolve to nothing
and ~645 MB would silently re-download. The corpus was built on Windows — the six backslash
rows below are the evidence — so such a machine may exist. Recover the command from git
history if one turns up; it is idempotent and safe to re-run.

Two things its dry run caught that this section had not anticipated:

1. **`attachments/laws` was being swept into `files/circulars/laws`.** Under `--dry-run`
   nothing has moved, so the archive was still sitting in `attachments/` when the
   per-circular sweep ran. A real run would have ordered correctly by luck. It is now
   excluded by name rather than by ordering — 12.4's "order matters" warning showing up in a
   form this section did not predict.
2. **Six rows held Windows backslash separators** (`attachments\<id>\<file>.pdf`), written
   when the corpus was built on Windows. On Linux those are a single filename and never
   resolved, so they were already broken. The migration normalises them and reports the count
   separately. One was a law version, which now resolves — the archive is 108/108 present.

Final counts: 1371 attachment paths, 108 law version paths, 0 cached documents. The 5
attachment rows still reporting a missing file are five of the six Windows rows, whose bytes
were never on this machine; they now re-download correctly, and under 3.5.2 that is a
single-column write.

Suite: 1 failed / 578 passed, baseline failure only.

#### 12.6.2 Test-side change

Path roots are patched through a new `use_tmp_data_root(monkeypatch, tmp_path)` in
`conftest.py`, replacing eight hand-rolled `PROJECT_ROOT` patch pairs.

It exists because two binding styles have to be redirected together: modules that imported a
root at import time hold their own reference, while `api/serializers.py` imports inside the
function and reads `env` at call time. Patching one and not the other leaves a containment
guard comparing against the developer's real tree — 4.2's "fail open or closed", reappearing
in the test harness.

