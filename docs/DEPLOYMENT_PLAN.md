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
567: three new tests, listed in 2.1).

`sync_status` and `ai_generation_jobs` were deliberately **not** moved; see 2.2 and 10.1.

### 0.2 Not started

Sections 3 through 9. Nothing outside `database.py`, `models.py`, `api/serializers.py`,
`api/debug.py`, `main.py`, `ai.py` and `llm_debug.py` has been touched.

### 0.3 Sequencing

Sections 2-4 are prerequisites for everything else and are independent of the auth design,
so they can land first and be verified locally. Section 7 (auth) is the largest single
piece. Sections 6-8 are small and can be done in any order once 2-4 are in.

```
2. DB split ✔ ───────┐
3. attachment cache ─┼─→ 4. data directory ──→ 5. corpus seeding ──→ 9. Railway
6. ecodata refresh ──┘                                    │
                              7. auth + admin gating ─────┤
                              8. corpus content prep ─────┘
```

Section 3 is now the only item on the critical path that is still an open design question
rather than known work — see 3.4.

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
| Shippable corpus | `sbpeye.db` 69 MB + `chroma_db/` 395 MB |
| Not shippable | `attachments/` 638 MB, `cache/` 736 MB |

Desktop remains viable later; the work in sections 3 and 4 is a prerequisite for it too.

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
   baseline on clean `main` exactly. The 6 are the known set (`test_attachments`,
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

## 3. Move the attachment cache mapping into the app database

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
does not strictly need to move. But the *download-on-miss* path for law versions
(`main.py` law document routes) is user-reachable. Either route it through `DocumentCache`
too, or accept the same single-column write. Decide when writing the code; the first is
tidier, the second is smaller.

**But see 3.4 — for law versions specifically, "download-on-miss" is not a safe assumption.**

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
move per 3.4.1 — its download-on-miss route should return an error for a non-current version
rather than re-fetching. Recorded as 10.3; not decided.

---

## 4. Separate code location from data location

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
behaviour you want.

---

## 5. Seed the corpus onto the volume

### 5.1 The rule

The `sbpeye.db` tracked in git is a **seed**. The application never opens it for writing.

On startup:

1. If `DATA_ROOT/sbpeye.db` does not exist, copy the seed from the image and write a marker
   recording the seed's version.
2. If it exists and the marker matches the image's seed version, leave it alone.
3. If it exists and the marker is older, the image carries a newer corpus. **Do not
   overwrite silently** — a newer corpus would discard whatever the admin generated on the
   running deployment. Log loudly and require an explicit opt-in (`SBPEYE_RESEED=1`).

Rule 3 is the one that matters. Without it, every corpus update you ship destroys admin work
done since the last deploy, and the failure is silent.

### 5.2 Seed version marker

A file next to the copied database (`DATA_ROOT/.corpus-seed`) holding the seed's content
hash, or a build-time version string baked into the image. A hash is self-maintaining and
cannot drift from the file it describes.

### 5.3 What ships in the seed

| Included | Excluded |
|---|---|
| `sbpeye.db` (69 MB) | `attachments/` (638 MB) — fetched on demand |
| `chroma_db/` (395 MB) | `cache/` (736 MB) — regenerated |
| | `sbpeye_app.db` — created empty on first boot |
| | `sbpeye_debug.db` — created on demand |

`chroma_db/` is the awkward one. It must ship or vector search is dead on arrival, and
rebuilding it means re-embedding 5,222 sources on container CPU — minutes to tens of minutes
of first-boot time. Shipping it couples the image to a chromadb version whose on-disk format
has changed across releases historically. **Verify** that the pinned `chromadb>=1.5.9`
opens the committed store in a clean container before relying on it; this is the single
most likely thing in this plan to fail in a way that is annoying to diagnose.

Both are large enough that the volume needs to be sized for corpus + attachments growth, not
just the databases.

### 5.4 FTS on first boot

`_warm_up_search_index` (`main.py`) runs `backfill_fts` in a background thread on every
boot and writes to the corpus if the FTS tables are empty. The seed already contains
populated `circulars_fts` and `laws_fts`, so this is a no-op in practice — but if a seed is
ever built without them, first boot performs a large corpus write with no admin involved.
Worth an explicit check when building a seed.

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

### 9.1 Container

Multi-stage `Dockerfile`:

1. **Frontend stage** — `node:20`, `npm ci`, `npm run build`, emit `frontend/dist`.
2. **Python stage** — `python:3.12-slim`, `uv sync --frozen`, copy `src/`, copy the built SPA
   into `src/sbpeye/static/spa/`, copy the seed corpus.

Expect a large image. `torch` (500 MB) arrives via `docling`, `onnxruntime` (291 MB) via
`fastembed`. Since every `docling` import is function-local (`checklist.py`), a build that
omits it would shed roughly 700 MB at the cost of checklist reference-unit extraction — worth
measuring if build times become painful, but not worth doing pre-emptively.

`.dockerignore` must exclude `.venv/`, `attachments/`, `cache/`, `frontend/node_modules/`,
`sbpeye_debug.db`, `benchmarks/results/` and — importantly — **`.env.local`**. That file
currently contains live provider keys; baking it into an image layer would ship them.

### 9.2 Volume

One Railway volume mounted at `/data`, with `SBPEYE_DATA_DIR=/data`.

Railway does not allow scaling a volume-attached service past one replica. That is not a
constraint this application chafes against — `chromadb.PersistentClient` is single-process by
design (`database.reset_collection` documents why), and the sync and remote-check guards in
`main.py` are module-level `threading.Lock`s that only work in one process. Single instance
is the correct topology, not a compromise. Worth knowing it is a hard ceiling.

Size it for corpus (464 MB) plus attachment growth, not just the databases.

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

### 9.4 Health check

There is no health endpoint. Add `GET /healthz` returning 200 once both databases are
openable and the Chroma collection responds. Point Railway's health check at it.

It must **not** depend on the LLM provider being reachable — a provider outage would
otherwise take the container down.

### 9.5 Startup

`run.py` already avoids `reload` unless `--dev` or `SBPEYE_DEV=1`, so it is deployable as-is.
Confirm the port binding matches Railway's injected `PORT` rather than the hardcoded 8000.

---

## 10. Open decisions

### 10.1 `sync_status` / `ai_generation_jobs` placement

Corpus (current) or app database. Corpus means build-machine job history ships in the seed;
app means reworking transaction boundaries in three job runners that currently commit job
rows and corpus rows together. Low consequence. Section 2.3.

### 10.2 Workspace sharing model

Shared or per-user. Determines whether the `DEFAULT_WORKSPACE_ID` rework is needed now.
Section 7.4.

### 10.3 `local_path` — whether to move it at all, and how

Was: "route `RegDocumentVersion` through `DocumentCache` or accept the corpus write."
Widened by the findings in **3.4**, which apply to all three `local_path` columns:

- Choose between option A (implement 3.2 as written), B (overlay with corpus fallback) or
  C (defer — sections 4 and 5 may make it unnecessary). Table in 3.4.5.
- Independently: `RegDocumentVersion.local_path` is an **archive, not a cache** (3.4.1).
  Two superseded law editions already exist only on disk. Its download-on-miss path should
  refuse rather than re-fetch for a non-current version, under any of A/B/C.
- If A or B: `sbpeye cache check-stale` must be updated in the same change (3.4.3).

Section 3 should not be implemented before section 4 either way (3.4.2).

### 10.4 `.env.local` writability in production

Persist on the volume, or make provider config read-only from Railway variables.
Section 4.3.

---

## 11. Verification

Before calling the deployment done:

1. **Test suite** diffed against the 6-failure baseline on clean `main`, not against zero.
   ✔ as of the section 2 work — 6 failed / 569 passed.
2. **Boundary test**: a corpus session must raise on app tables (section 2.5). ✔ confirmed
   against the live files and now covered by a test.
3. **Cold-start test**: build the image, mount an *empty* volume, boot. The seed must copy,
   `sbpeye_app.db` must be created, search must return results (proves Chroma opened), and a
   PDF must download on demand (proves section 3 and the attachments path).
4. **Redeploy test**: change a setting through the UI, redeploy the same image, confirm the
   setting survived and the corpus was not reseeded.
5. **Auth test**: every endpoint in 7.3 returns 403 for a non-admin session and 401 for no
   session.
6. **Git cleanliness**: after a full session of use against a deployed instance, `git status`
   in a local checkout must show `sbpeye.db` unmodified. That was the original symptom; it is
   the clearest single signal the split worked.
