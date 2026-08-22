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

Volume budget: 985 MB in use of 4.6 GB, measured 2026-08-22. The full corpus is ~1.55 GB, so
closing the `files/circulars` gap still leaves well over half the volume free.

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

   The PDF arm is worth being precise about, and it changed twice. Download-on-miss now
   works — SBP is reachable (§2.1) — but `files/circulars` is not on the volume (§2.3), so
   circular attachments are *all* serving by refetch rather than from disk. Law PDFs and the
   HTML cache do serve from the volume. Confirming both halves separately is the point.

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

### 2.1 Reaching SBP from the deployment

**Resolved 2026-08-22. This section previously said SBP blocked the deployment's IP. That
was wrong**, and the mistake is worth keeping on the page because it cost months of manual
corpus syncing. The address was never the problem — `cloudscraper` was.

The symptom was real enough:

```
403 Client Error: Forbidden for url: https://www.sbp.org.pk/circulars/bprd-circular-letter-no-16-of-2026
```

and intermittent, which is what made "IP reputation" look like the answer. Measured from
the deployment, 20 attempts per client against `/circulars/`:

| client | ok | median |
|---|---|---|
| `cloudscraper`, fresh per call | 9/20 | 54ms |
| `cloudscraper`, session reused | 0/20 | 7ms |
| `requests` | 20/20 | 2440ms |
| `curl_cffi` (chrome131) | 20/20 | 2381ms |

The latencies give it away. A success takes ~2400ms because Cloudflare has to fetch from
the origin in Karachi; the failures come back in 7-54ms, which is an edge WAF refusing at
the door with nothing reaching Pakistan at all. And plain `requests` — no challenge
solving, no impersonation — never once saw it.

The cause is that `cloudscraper` rolls a **random browser profile per
`create_scraper()` call** (six calls returned five different profiles, among them Firefox
52, Goanna 4.1 and an Android 3.1 tablet) and emits Chrome's cipher list over HTTP/1.1:

| client | JA4 | ALPN |
|---|---|---|
| `requests` | `t13d1712h1_ab0a1bf427ad_882d495ac381` | HTTP/1.1 |
| `cloudscraper` | `t13d1513h1_`**`8daaf6152771`**`_8e6e362c5eac` | HTTP/1.1 |
| real Chrome / `curl_cffi` | `t13d1516h2_`**`8daaf6152771`**`_02713d6af862` | HTTP/2 |

Note the shared middle field: `cloudscraper` copies Chrome's ciphers but delivers them
with non-Chrome extensions and no HTTP/2, so it reads as *something imitating Chrome and
failing*. Cloudflare flags the imitation. Generic Python it lets through. Every call site
also passed its own `HEADERS`, overriding whatever User-Agent the rolled profile wanted —
so the profile varied per request and so did the verdict. That is the whole of the
"sometimes it works".

`cloudscraper` was built for the 2016-era JavaScript challenge. It does not address
fingerprinting, and here it was not failing to help — it was the thing being blocked.

**Every SBP call site now uses plain `requests`** (`_get_sbp` and `fetch_page` in
`scraper/circulars.py`, `scrape_sbp_news`, `scrape_ecodata_index`, `_download_pdf`, and
three in `main.py`), and the dependency is gone. Two things to keep in mind:

* **Send `HEADERS`.** Without them `requests` announces itself as `python-requests/2.x`,
  which is a louder signal than the one that was getting refused. `scraper/ecodata.py` was
  the one call site passing none, because `cloudscraper` had been supplying a User-Agent
  of its own; it passes `HEADERS` now.
* **`_get_sbp` validates redirects again.** It passes `allow_redirects=False` and walks
  each hop through `normalize_sbp_url`. Under `cloudscraper`, which follows redirects
  itself, the loop never saw a 3xx and every hop was taken unchecked — the docstring
  promised validation that was not happening.

#### Confirming it, here or anywhere else

`sbp_reachability` is what produced the table above and is the tool to re-run if this ever
regresses. It measures a *rate*: N attempts, a control host so "no outbound HTTP at all"
is not mistaken for a block, body checks so a Cloudflare interstitial served as `200` is
not scored as success, and two arms — `requests` (what the scrapers use) and `curl_cffi`
(Chrome's real fingerprint). If they ever diverge, the fix is a client change and
`curl_cffi` is where to go; if they fail alike, it is the address.

`curl_cffi` is **not** a dependency — 38 MB of libcurl-impersonate for a diagnostic arm,
against a Dockerfile that already turns down weight it does not need (§9.1.3). Its arm
skips itself when absent, so run `uv add curl-cffi` and redeploy if an investigation wants
the comparison back.

It has to run from the address SBP sees, which rules out `railway run` — that executes
locally, and a maintainer's machine is not blocked:

```
railway ssh -- python -m sbpeye.sbp_reachability --attempts 20
```

or, without a shell, `GET /api/admin/sbp-reachability?attempts=5` (admin-only, serialized,
capped at 20 attempts per cell). Same code behind both. It reports the Cloudflare ray IDs,
which are what SBP's side needs to look up a specific refusal.

It probes `/circulars/` only unless given `-t`, and it is serial, so budget roughly three
seconds per request: the default run is 6 requests. Progress goes to stderr as each one
lands — a silent run of this is a bug, not patience, because "hung" and "the network is
being blocked" are precisely the two things it has to tell apart.

#### What this unblocks

The rest of this section described an operating model built on the block: corpus updates
performed on a maintainer's machine and re-uploaded, `SBPEYE_ECODATA_REFRESH_SECONDS=0` to
stop the scheduler throwing hourly, and on-demand fetches failing for users. **None of
that is forced any more.** Before lifting any of it, re-run the probe from the deployment
and confirm `requests` still scores 20/20 — the measurement is cheap and the failure mode
is silent. The paths that were affected:

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

#### The operating model this replaces

The block forced corpus updates onto a machine whose IP was not blocked: sync locally, then
re-upload `sbpeye.db`, `chroma_db/` (§2.4) and whatever new cache and attachment files the sync
produced (§2.5). Re-upload was a routine operation, and the admin console could report but
never write.

**Sync now runs on the deployment**, from the admin console's Sync tab (§2.8). Uploading is the
fallback, not the path. The one tree that keeps the old model is `files/laws`: it is an archive
nothing may re-fetch (invariant 3.7), so it goes up through `scripts/sync_volume.py` and only
ever that way.

### 2.2 Environment

| Variable | Purpose |
|---|---|
| `SBPEYE_DATA_DIR` | `/data` |
| `SBPEYE_SECRET_KEY` | Cookie signing and key encryption. The container refuses to start without it; 32 characters minimum |
| `SBPEYE_ADMIN_EMAIL` / `SBPEYE_ADMIN_PASSWORD` | First-admin seeding. Only consulted when no admin exists, so leaving them set does not resurrect a deleted account |
| `SBPEYE_ECODATA_REFRESH_SECONDS` | Default 3600. **Currently unset in production**, so the scheduler runs hourly — see §2.8 for what that writes |
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

**`files/circulars` is not on the volume.** Measured 2026-08-22: the push in §2.5 covered
`laws` and `cache` and stopped there, so the volume holds 985 MB of a 1.5 GB corpus and
`/data/files` has two entries, not three. The ledger in `sbpeye.db` says those 570 MB of
attachments exist; the disk says otherwise, and every attachment open is a download-on-miss
that only started working again when SBP became reachable. Closing the gap is now a
**Re-download** sync from the console (§2.8) rather than an upload — the deployment fetching
570 MB itself beats pushing it through the CLI. Headroom is not the constraint: 3.6 GB free.

```
/data/files/laws     76M   ✓        /data/chroma_db  522M  ✓
/data/files/cache   318M   ✓        /data/sbpeye.db   70M  ✓
/data/files/circulars      ✗ absent
```

No data ships in the image: `sbpeye.db`, `chroma_db/` and `files/` are all excluded by
`.dockerignore`, which takes the build context from 7.5 GB to 17.9 MB.

### 2.4 Re-uploading the corpus

**Checkpoint first.** The databases run in WAL mode (invariant 3.14), which means recent commits
sit in `sbpeye.db-wal` until something folds them back into `sbpeye.db`. Uploading is a file
copy, so a `.db` taken from a running or killed app ships without them. This is not theoretical
and not subtle in size: an interrupted sync left `sbpeye.db` at 4 KB with 869 KB stranded in the
sidecar — a file that reads correctly through SQLite locally and arrives on the volume empty.

Stopping the app cleanly is enough; `checkpoint_sqlite()` runs on lifespan shutdown and leaves a
zero-length `-wal`. If the process was killed, reopen the database once and close it, or run:

```bash
sqlite3 sbpeye.db 'PRAGMA wal_checkpoint(TRUNCATE);'
```

Then check `ls -l sbpeye.db-wal` reads 0 before uploading anything.

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

**This is the fallback path now, with one exception.** With SBP reachable, `circulars` and
`cache` are cheaper for the deployment to fetch than for a maintainer to upload, so they come
from a console sync (§2.8). `files/laws` is the exception and stays upload-only: SBP replaces
law PDFs in place and keeps no history, two superseded editions already exist nowhere else, and
a re-fetch cannot reproduce them (invariant 3.7). The tool also remains the recovery path for
a volume that has to be rebuilt from a known-good local tree.

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

### 2.8 Syncing from the admin console

**Admin → Sync.** Three controls, in the order the tab presents them, because that is the order
the questions arrive in.

**Can this server reach SBP?** runs `sbp_reachability` from the container and reports a verdict,
the egress IP and the per-arm success rate. It is a button rather than a page load because it
costs ~2.5 s per attempt and talks to SBP; it writes nothing. Run it before a large sync — the
block was silent once and would be again.

**Circular sync** posts to `POST /api/circulars/sync`, which is where it always lived. Pressed
with defaults it is the same incremental run as the sidebar button: newest first, stopping at
the latest date the corpus already holds, one worker. The options that matter:

| Option | What it is for |
|---|---|
| **Workers** | Concurrent writers against the database this deployment serves from. 2–3 shortens a backfill; 8 is for a machine with no users on it |
| **Walk the full listing** | Ignores the stop-at-date, reads every listing page. Needed to backfill older years |
| **Re-download files already held** | Refetches attachments the ledger claims exist. **This is how to close the `files/circulars` gap in §2.3** |

There is no progress on the wire — `SyncStatus` gets its counts written once, at the end — so
the tab shows a state, not a bar. A run killed by a redeploy is released as `failed` on the next
boot by `fail_interrupted_sync_jobs`, so nothing stays "running" for ever.

**EcoData index** re-scrapes the economic-data index: entry rows only, no files and no vectors.
It is a `DELETE` of `ecodata_entries` followed by a re-insert of whatever parsed, so a refresh
against a partial page leaves a partial index until the next good one. The scheduler runs this
too, hourly, and `SBPEYE_ECODATA_REFRESH_SECONDS` is unset in production (§2.2) — meaning the
deployment already rewrites that table on its own, unattended. Set the variable to `0` if that
is not wanted; it is the only unattended corpus writer.

**What did not move.** `api/admin.py` is still read-only, all of it. The writes stayed on
`/circulars/sync` and `/ecodata/refresh` in `main.py`, where they already held the process-wide
lock and already wrote the `SyncStatus` rows the Runs tab reads; the console calls across.
Re-indexing, laws sync and corpus-wide AI generation are still CLI commands.

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

**3.14 The databases are WAL, so a `.db` file alone is not the database.** `database.py` sets
`journal_mode=WAL` on all three engines, because sync now runs inside the web process — up to
eight worker sessions writing while users read — and under the default rollback journal that is
`database is locked` on ordinary page loads, not a slow sync. The cost is that recent commits
live in `sbpeye.db-wal` until checkpointed, and **every path that moves this corpus between
machines is a file copy**: §2.4's upload, `scripts/sync_volume.py`, and `git add sbpeye.db`,
which is tracked. `checkpoint_sqlite()` runs on lifespan shutdown so a clean stop leaves a
zero-length `-wal`; a killed process does not. Check `ls -l sbpeye.db-wal` before copying, and
never copy one out from under a running app. The sidecars are gitignored so they cannot be
committed alongside a stale `.db`.

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
