# Multi-User Web Deployment

SBPEye runs as a shared, authenticated web application on Railway. The build-out is done and
the service is live; this document is now **what is left, and how to operate what exists**.

Scope is still a **test deploy** — a small set of known testers, not a production service.
Decisions that would be wrong for production were taken anyway where they bought a materially
faster path to feedback; those that are still outstanding are in §1.3.

The implementation narrative for the sections that are complete — the database split, the data
root, the cache paths, ecodata scheduling, authentication, the container, and the file tree
consolidation — has been removed from this document. It is in git history, commits `f87a67a`
through `984893d`.

---

## 0. Where this stands

### 0.1 Live

| Piece | State |
|---|---|
| Service | Deployed from the GitHub repo; Railway builds the `Dockerfile`, no Nixpacks |
| Image | 1.51 GB (`docling` is an optional extra, §3.11) |
| Plan | Hobby — the floor, since Free/Trial caps volumes at 0.5 GB |
| Volume | `sbpeye-volume-76qt`, mounted at `/data`, `SBPEYE_DATA_DIR=/data` |
| `sbpeye.db` | 70 MB — uploaded |
| `chroma_db/` | 522 MB — uploaded |
| `files/` | 963 MB, 5251 files — uploaded in full via `scripts/sync_volume.py` |
| Health check | `GET /healthz` — `corpus_db`, `app_db`, `vector_store` |

The redeploy that makes the process open the **uploaded** store rather than the empty one it
created on its own first boot has been done. `chromadb.PersistentClient` is constructed at
import (`database.py:88`), so that redeploy was the only thing that could pick it up; before
it, search returned nothing against a 522 MB store sitting on the volume.

Volume budget: roughly 1.55 GB at rest against Hobby's 5 GB.

### 0.2 Test baseline

Diff against the baseline on the machine you are actually running, measured by stashing the
change rather than taken from this document.

| Machine | Clean `main` |
|---|---|
| Linux | **1 failed / 575 passed** — `test_attachments::test_fetch_page_cached_uses_uuid_filename` |
| Windows (`.venv/Scripts/python.exe`) | **6 failed / 569 passed** — the above plus `test_llm_debug::test_only_gateway_calls_chat_completions_create` and four in `test_routes_smoke` around document redownload |

Last measured on the working tree: 1 failed / 582 passed, the baseline failure only.

---

## 1. Remaining work

### 1.1 Verification against the live deployment

Three items are outstanding, all of which need the running service.

1. **Cold start on Railway.** `/healthz` green on all three checks, and a search returning
   correct results — the latter is the proof that the container's `chromadb>=1.5.9` opened a
   store built on the development machine. This passed locally against the same image
   (boot ~6 s, correct semantically relevant hits) but has not been confirmed on Railway
   itself.

   The PDF arm is worth being precise about: download-on-miss cannot succeed from Railway at
   all while the IP block stands (§2.1), so what needs confirming is that documents and law
   PDFs **serve from the volume**, not that they refetch.

2. **Redeploy test.** Change a setting through the UI, redeploy the same image, confirm the
   setting survived and the uploaded corpus was untouched. This is what proves `DATA_ROOT`
   and the volume are doing their job.

3. **Git cleanliness.** After a full session of real use against the deployed instance,
   `git status` in a local checkout must show `sbpeye.db` unmodified. That was the original
   symptom the whole database split existed to fix, and it is the clearest single signal it
   worked. Note the file is legitimately dirty from local corpus work — the check is that a
   session spent *only* on the deployment leaves it clean.

Already verified and not repeated here: the test suite against baseline, the corpus/app
database boundary, the authentication boundary in a container, and the local cold start.

### 1.2 Open decision — `sync_status` / `ai_generation_jobs` placement

The only decision still open. Both tables are operational runtime state and would normally
live in `sbpeye_app.db`, but they are still on `Base` in the corpus.

They were left there because their writers — `circular_ai._run_generation_job`,
`laws_ai.run_law_generation_job`, `main._run_circular_sync` — interleave job-row commits with
corpus writes inside a single session. `update_progress` (`circular_ai.py:203`) commits the
job row and the circular together. Moving them is not a `Depends` change; it is a rework of
transaction boundaries in three of the most failure-sensitive functions in the codebase, with
no shared transaction available across two SQLite files.

The cost of leaving them is that job history from the build machine ships inside the uploaded
corpus. Low consequence; should not block anything.

### 1.3 Hardening carried forward

Each is a knowing trade for a test deploy, and each is wrong for production.

| Item | Detail |
|---|---|
| **Password floor is 8** | `auth.MIN_PASSWORD_LENGTH`, relaxed from 12 on request. Twelve is the better number; it belongs back there before this stops being a test, and it is one constant. It governs every account including the seeded admin, which is the right way round |
| **The container runs as root** | So files it creates on the volume, and everything `sync_volume.py` extracts, are root-owned |
| **No backup of `sbpeye_app.db`** | It holds every user, their encrypted provider keys and all chat history, and nothing else has a copy. A dependency-override leak once had the test suite writing to the developer's real copy, and chat rows were lost with no way to establish when. Worth a scheduled copy off the volume |
| **`.env.local` is writable in production** | `MANAGED_ENV_FILE` is `DATA_ROOT / ".env.local"`, so the Settings UI persists across redeploys. The more correct answer is provider config from Railway variables only, with the secret-writing paths refusing. Railway variables already win over file values either way |
| **`DATA_ROOT` is not self-creating** | Nothing creates the directory. It matters for the staging procedure in §2.4, where `railway ssh … mkdir -p` covers it, but making `env.py` create it is worth doing on its own merits |
| **Keys in the local `.env.local`** | Rotate them. Hygiene rather than a deployment prerequisite — since per-user keys (§3.5) the deployment does not need those values in its environment at all |

---

## 2. Operating the deployment

### 2.1 The deployment is read-only toward SBP

SBP blocks the deployment's IP. Opening a circular returned:

```
403 Client Error: Forbidden for url: https://www.sbp.org.pk/circulars/bprd-circular-letter-no-16-of-2026
```

Every outbound SBP request goes through `cloudscraper` — `_get_sbp`
(`scraper/circulars.py:78`) and `scrape_ecodata_index` both — and cloudscraper solves a
JavaScript challenge, not an IP-reputation block. From a datacenter range there is nothing to
solve. This is not a bug in the app and no amount of retrying fixes it.

What it takes out, all of it at request time:

| Path | Who reaches it |
|---|---|
| `GET /api/circulars/{id}/source` on a cache miss | Any user — the visible failure |
| Attachment download-on-miss | Any user opening a PDF not already on the volume |
| Law archive refetch | Any user opening a law document |
| Circular sync, refresh, open-by-link | Admin only |
| The scheduled EcoData refresh | Nobody — it fails on a timer and logs |

**The mitigation is that everything fetchable is already on the volume** — `files/` was
uploaded for exactly this reason. `cache/html` alone covers 3649 of 3653 circulars (99%),
because `fetch_page_cached` consults the cache before the network and the key is
`uuid5(NAMESPACE_URL, url)`, stable across machines.

**Set `SBPEYE_ECODATA_REFRESH_SECONDS=0`** unless the block is lifted, or the scheduler throws
hourly into the logs for a scrape that cannot succeed.

#### The operating model this forces

Corpus updates happen on a machine whose IP is not blocked: sync locally, then re-upload
`sbpeye.db`, `chroma_db/` (§2.4) and whatever new cache and attachment files the sync produced
(§2.5).

That is less of a departure than it sounds — every corpus write was already admin-only, and
this moves "admin" from a session on the deployment to a shell on the maintainer's machine. It
does mean the admin UI's sync and generation buttons cannot work from the deployment, and it
makes re-upload a routine operation rather than a one-off.

If the deployment should ever sync for itself, the options are an egress proxy with a
residential or allowlisted IP, or asking SBP to allow the range. Neither is in scope.

### 2.2 Environment

| Variable | Purpose |
|---|---|
| `SBPEYE_DATA_DIR` | `/data` |
| `SBPEYE_SECRET_KEY` | Cookie signing and key encryption. The container refuses to start without it; 32 characters minimum |
| `SBPEYE_ADMIN_EMAIL` / `SBPEYE_ADMIN_PASSWORD` | First-admin seeding. Only consulted when no admin exists, so leaving them set does not resurrect a deleted account |
| `SBPEYE_ECODATA_REFRESH_SECONDS` | `0` while the IP block stands (§2.1). Default 3600 |
| `AI_PROVIDER`, `AI_BASE_URL`, `AI_MODEL`, `AI_CHAT_MODEL` | Deployment-level config for admin corpus generation. Defaults to `mistral`, so a missing value is a provider error rather than a localhost connection error. Testers' chat uses their own keys (§3.5) |
| Provider key (`OPENAI_API_KEY`, `GROQ_API_KEY`, …) | Per chosen provider |
| `EMBEDDING_PROVIDER` | Leave at `fastembed`. It must match the model the uploaded Chroma index was built with (`BAAI/bge-base-en-v1.5`) or search returns nonsense rather than an error |
| `FASTEMBED_CACHE_PATH` | `/opt/models` — the weights baked into the image. Set in the `Dockerfile`; must stay in step with `EMBEDDING_MODEL` |
| `LLM_DEBUG_ALLOWED` | `false` unless actively debugging — traces store full prompts, including other users' chat turns |

**Only the first three rows are required.** Since per-user provider keys, the AI variables are
optional: the admin signs in, sets their own provider under Settings, and presses **Use my
provider** in the admin console to copy it into the deployment configuration.

### 2.3 What lives where

| On the volume | In the image | Nowhere — created on demand |
|---|---|---|
| `sbpeye.db` (70 MB) | The SPA bundle, `src/` | `sbpeye_app.db` — first boot |
| `chroma_db/` (522 MB) | `models/` → `/opt/models` (209 MB) | `sbpeye_debug.db` |
| `files/` (963 MB) | | `.env.local` — written by the Settings UI |

`files/` splits by how much it costs to lose, and that split is load-bearing:

```
files/
├── laws/       76 MB   archive — irreplaceable, nothing may delete from here
├── circulars/ 570 MB   re-fetchable (given an unblocked IP)
└── cache/     318 MB   disposable — `rm -rf files/cache` is ALWAYS safe
    ├── html/
    └── parses/
```

No data ships in the image: `sbpeye.db`, `chroma_db/` and `files/` are all excluded by
`.dockerignore`, which takes the build context from 7.5 GB to 17.9 MB.

### 2.4 Re-uploading the corpus

Two known paths replaced wholesale, so `--overwrite` is **required**, not optional — the paths
already exist:

```bash
railway volume files -v sbpeye-volume-76qt upload --overwrite ./sbpeye.db /sbpeye.db
```

```bash
railway volume files -v sbpeye-volume-76qt upload --overwrite ./chroma_db /chroma_db
```

`/` is the volume root, which is the `/data` mount. 521 MB across 11 files takes a few minutes.

Four things that will otherwise cost an afternoon:

- **Flag position.** `-v/--volume` is an option on `files`, not on `volume`, so it sits between
  the two. `railway volume <name> files …` and `railway volume -v <name> files …` are both
  rejected as an unrecognised subcommand.
- **The volume is only reachable through a running container.** `railway volume files` proxies
  through it: with no active deployment it refuses — *"Service … has no active deployment in
  environment production."* The service cannot be stopped first.
- **Chroma must never be written under a live process.** `PersistentClient` is single-process
  and `/healthz` calls `collection.count()`, so the store is open from the moment the container
  is ready. This is the one way to corrupt it on this route.
- **`railway ssh` is a real shell in the container**, and the image has `/usr/bin/tar`:
  `railway ssh -s <service> -- sh -c '<command>'`. Use it for verification instead of
  `railway volume files list`, which truncates silently (§2.5).

Those last two collide, and the way to get the guarantee back is to move the running app out of
the way first: create the staging directory, point the app at it and redeploy so the process
opens `/data/_staging/chroma_db` and never touches the target, upload to `/chroma_db`, then set
`SBPEYE_DATA_DIR` back to `/data` and redeploy onto the new store.

```bash
railway ssh -s <service> -- mkdir -p /data/_staging
```

On a deployment with no users and no admin actions in flight the exposure is acceptable
without the staging dance — everything that writes Chroma is admin-triggered — but with testers
using it, "nothing writes during the window" stops being true. Either way, redeploy afterwards:
the running process is left holding replaced files until it does.

**On Windows, export `MSYS_NO_PATHCONV=1`** for any raw `railway` command carrying a remote
path. Git Bash rewrites a leading slash before the CLI sees it — `list /` became
`Failed to list remote directory /data/D:/Progs/Git/`.

### 2.5 Pushing the file trees

Use `scripts/sync_volume.py`, never a raw directory upload. **Operator runbook:
[VOLUME_SYNC.md](VOLUME_SYNC.md).**

```bash
python scripts/sync_volume.py push laws --apply
```

Then `cache/parses`, `cache/html`, `circulars`, and `status` to confirm. Laws first: it is the
one tree that cannot be re-fetched. Re-run any command that dies; it resumes.

Why the tool exists, measured against CLI 5.41.2:

- **A directory upload onto an existing path nests rather than merges.** `upload <dir>
  <remote>` has `cp -r` semantics, so re-running a timed-out upload is not a resume — it is a
  second, misplaced copy. It does not merge, skip, or error, and `--overwrite` does not change
  it. This happened: 2662 files landed one level too deep, and nothing warned.
- **`railway volume files list` truncates without saying so.** It reported 5 files in a
  directory holding 2561. No pagination cursor, no indication the result is partial, so any
  verification built on it can report a full directory as empty. Use `find` over `railway ssh`
  instead, which returns the whole tree with sizes in one call.
- **Each CLI invocation costs ~6.5 s of handshake.** Diffing the tree with `list` (1985
  directories) is ~3.6 hours; uploading 2664 files one at a time is ~4.8 hours. The tool tars,
  splits, uploads a handful of chunks, and extracts in-container instead.
- **Size decides what to re-send, not presence.** A file interrupted mid-upload is present and
  short; treating existence as success leaves it truncated forever. Six such files existed
  after the timed-out run.

### 2.6 Rebuilding the vector store

If the uploaded store is ever lost or unopenable, it rebuilds from `sbpeye.db` alone — no file
trees needed.

```bash
sbpeye reindex
```

44,106 chunks (27,740 attachment, 8,486 circular, 8,169 law). `attachment_document()`
(`scraper/circulars.py:925`) feeds the chunker `attachment.content_text` and nothing else — no
`local_path`, no file read — and indexing routes to `prepare_reference_chunks`, which chunks
without invoking the PDF pipeline. So the input is 16 MB of `content_text` already inside the
corpus; the 570 MB of circular PDFs is irrelevant to a rebuild.

Cost is time: roughly 18 chunks/s once warm on a desktop CPU puts it near 40 minutes, and a
container will be slower. One rough measurement, not a benchmark.

If this is ever wired in as automatic recovery rather than run by hand, note the hazard:
`reset_collection()` (`database.py:100`) drops the collection and rebinds the module-level
handle that `search`, `chat_retrieval` and `scraper.circulars` bound at import. The CLI can
assume it is alone; a rebuild inside the serving process has readers arriving mid-swap.

### 2.7 Standing the service up from scratch

Kept because it is the recovery procedure, not because anything here is pending.

1. **Create the service** from the GitHub repo; Railway detects the `Dockerfile`.
2. **Upgrade to Hobby** before adding the volume — Free/Trial caps volumes at 0.5 GB, which
   does not fit the corpus alone.
3. **Add a volume, mount path `/data`.**
4. **Set the three required variables** (§2.2). Generate the secret with
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
5. **Point the health check at `/healthz`.**
6. **Deploy and let the first boot finish.** It comes up with an empty corpus and reports
   `vector_store: "ok (empty)"` — the un-uploaded state, not a fault.
7. **Upload the corpus** (§2.4), then **push `files/`** (§2.5). Do not browse the app while
   either runs.
8. **Redeploy**, and confirm `/healthz` reports `vector_store: "ok"` rather than `"ok (empty)"`.
   A search returning results is the proof the container opened a store built elsewhere.
9. **Sign in as the admin**, set your own provider key under Settings, then press **Use my
   provider** in the admin console so corpus generation has credentials too.
10. **Add testers** from the admin console. Each sets their own provider key on first sign-in;
    chat does not work for them until they do, by design (§3.5).

---

## 3. Invariants

Things that must stay true. Each was learned the expensive way, and each is a latent bug on the
next edit that forgets it.

**3.1 `db` is the corpus session, `app_db` is runtime state.** A helper that takes `db` and
receives an app session is a silent 500 — `/api/debug/status` logged `no such table: settings`
and reported tracing as disabled for exactly this reason. Grepping for `db.query(...)` against
the five app models finds violations; hits inside helpers whose session is a parameter are
fine, so check their callers instead.

**3.2 A corpus session must raise on app tables.** Asserted by
`test_corpus_session_cannot_see_runtime_tables`. If it ever succeeds, the relocation has not
run against that file and the two databases are out of sync.

**3.3 Every mutable path derives from `DATA_ROOT`.** This grep must return exactly one hit, the
`CODE_ROOT` definition in `env.py` — four modules once re-derived the root inline, which would
have left them writing to the ephemeral image layer, invisible until a redeploy ate the data:

```bash
grep -rn "parents\[2\]" src/
```

`SBPEYE_DATA_DIR` must be a **real process environment variable**. It is read before any env
file loads, because it is what says where those files are; setting it in `.env.local` cannot
work.

**3.4 Authentication is middleware with an allowlist, not a `Depends` per route.** A new route
is therefore private by default, and the cost of getting it wrong is a login prompt rather than
a public endpoint. The allowlist is `/healthz`, `/login`, the login/logout APIs and static
assets. The **admin** gate stays per-route, because it genuinely varies.

**3.5 Every user pays for their own chat.** `get_ai_client_for_user` **refuses to fall back**
to the deployment key — a tester without their own gets "add your API key in Settings", not
somebody else's bill. The deployment-level config drives admin-triggered corpus generation
only. Keys are Fernet-encrypted at rest and write-only through the API.

**3.6 Rotating `SBPEYE_SECRET_KEY` makes every stored user key unreadable.** Encryption is
keyed off a SHA-256 of it, so each user re-enters theirs. Rotation already logs everyone out,
so it is the same event.

**3.7 Exactly one path under `files/` may be deleted by anything: `files/cache/`.** The laws
archive is the least reproducible data in the system — SBP replaces law PDFs in place and keeps
no history, and **2 superseded editions already exist nowhere else**. `cache check-stale` walks
`CIRCULAR_FILES_DIR` and `HTML_CACHE_DIR` into a prunable list and `LAWS_ARCHIVE_DIR` into a
separate report-only list that `--prune` does not iterate. It nearly deleted the archive once,
back when that safety was a matter of every version happening to be present.

**3.8 A law version is served only if the bytes hash to that version's own hash.** `file_url`
always serves whichever edition is current, so for a superseded version those are different
bytes by definition. On a mismatch nothing is re-pointed; the bytes land in the archive under
`_archive_name(content_hash, url)`, so a replaced edition physically cannot overwrite the
historical record.

**3.9 A plain cache miss writes `local_path` and nothing else.** It must never call
`process_attachment`, which commits seven columns including `is_vectorized = 0` — a ledger with
four consumers, one of which tells the chat model a retrievable source is unindexed. Only
`refresh=True` re-ingests. `tests/test_routes_smoke.py` carries a shared `_unexpected_reingest`
guard that fails any test in which a plain miss reaches the re-ingest path.

**3.10 One replica, always.** `chromadb.PersistentClient` is single-process by design and the
sync and remote-check guards in `main.py` are module-level `threading.Lock`s. Railway does not
allow scaling a volume-attached service past one replica, which makes this a hard ceiling
rather than a convention — and it is the correct topology here, not a compromise.

**3.11 `docling` is an optional extra, absent from the image.** It was the sole path to torch,
CUDA, triton, transformers and opencv — 19.3 GB of image, against 1.51 GB without. Every
docling import is function-local in `checklist.py`, so the app starts and serves normally;
checklist generation raises `DoclingUnavailable` with the install hint rather than a bare
`ModuleNotFoundError`. Locally, `uv sync` **without** `--extra checklist` will *remove* it from
an existing virtualenv.

**3.12 An unmigrated checkout needs `cache migrate-layout`, which no longer exists.** The
command was removed after the local corpus was migrated. A checkout that pulls the migrated
`sbpeye.db` while holding an old `attachments/`+`cache/` tree would resolve every path to
nothing and silently re-download ~645 MB. Recover the command from git history if one turns up;
it is idempotent and safe to re-run.

**3.13 The uploaded corpus must carry populated FTS tables.** `_warm_up_search_index` runs
`backfill_fts` in a background thread on every boot and writes to the corpus if they are empty,
which would be a large corpus write on first boot with no admin involved. The current
`sbpeye.db` has them.

---

## 4. Decisions taken

Recorded so they are not silently re-litigated.

**4.1 Railway, not desktop packaging.** Rejected **for now**, not on technical grounds — a
desktop build dissolves the auth requirement, the single-writer constraints and the secrets
problem entirely — but on iteration speed. A fix reaches every tester in two minutes on
Railway and requires re-downloading a ~700 MB bundle on desktop. Desktop remains viable later,
and the data-root work is a prerequisite for it too, so that part is already paid.

**4.2 Two databases, both SQLite.** `sbpeye.db` (corpus, tracked in git) and `sbpeye_app.db`
(users, chat, workspaces, settings — never shipped). Traces have a third, `sbpeye_debug.db`.
`settings` lives with runtime state despite being operator configuration, because the Settings
UI writes it: shipped with the corpus, every saved provider change would be reverted the next
time that file was replaced.

Postgres is not needed. Corpus writes are admin-only, so the corpus has a single writer, and
the app database's write volume is a handful of testers' chat messages.

**4.3 Corpus writes are admin-only.** Sync, AI generation, refresh and link discovery are
admin-gated, which makes the corpus **single-writer by policy** and caps LLM spend from the
corpus side. `POST /api/circulars/open` being admin-only means a tester pasting an unknown SBP
link gets a refusal rather than an indexed circular — accepted deliberately, and the endpoint
says so rather than returning a bare 403.

Two writers do not fit the rule and are handled individually: a plain document cache miss
(§3.9), and the scheduled EcoData refresh, which is the application refreshing its own scraped
index on a timer rather than anyone acting.

**4.4 Workspaces are shared, chat is per-user.** Chat is where the privacy expectation actually
sits, and sharing workspaces avoids the `DEFAULT_WORKSPACE_ID` rework until it buys something.
Because of the combination, the workspace chat session id carries the owner
(`workspace:<user_id>:<workspace_id>`) — derived from the workspace alone, every tester in the
default workspace would have landed in one another's conversation. Revisit before any wider
release.

**4.5 The corpus is uploaded, not shipped.** No seeding code, no `.corpus-seed` marker, no
`SBPEYE_RESEED` opt-in. `chroma_db/` is gitignored and cannot be tracked anyway — `chroma.sqlite3`
is 311 MB and `data_level0.bin` 152 MB, both over GitHub's 100 MB hard limit, and Git LFS was
rejected at 486 MB per corpus version against a 1 GB free quota. Updating the corpus later is
§2.4, run deliberately. The protection against overwriting admin work is that it is manual,
which is stronger than a marker file and is zero code.
