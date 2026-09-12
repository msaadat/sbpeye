# Plan: Admin-uploaded documents in the Laws corpus

Companion to `docs/LAWS_REGULATIONS_PLAN.md`. That plan built the laws corpus from one
source, SBP's `/laws-regulations` listing. This one adds a second way for a document to
enter the corpus: an administrator uploads it. The motivating case is Acts and ordinances
SBP lists but does not host (the Banking Companies Ordinance 1962 lives on
pakistancode.gov.pk), and relevant laws SBP does not list at all.

Circulars are untouched. They are SBP's own instruments and have exactly one source.

Composable in the same sense as the parent plan: each phase ships on its own and later
phases only build on earlier ones.

---

## 1. Findings (September 2026 analysis)

Read against the live code, not the parent plan's description of it.

### 1.1 The model already fits

`RegDocument` + `RegDocumentVersion` was built for a living document with a stable identity
and content-hash versions, archived immutably under `files/laws/<document_id>/`. An
uploaded Act is such a document whose bytes arrive by upload rather than download. Every
consumer keys off `current_version` and `is_vectorized`:

| Consumer | Reads | Works unchanged once a version is current? |
|---|---|---|
| `scraper.laws.index_pending_laws` / `index_law_document` | current version, `is_vectorized` | yes |
| `search._law_fts_row`, `_law_scores`, `_law_arm` | current version text | yes |
| `laws_ai.law_corpus` | current version, `local_path` | yes, once the external gate is fixed (§1.2) |
| `inventory.corpus` | current version | yes, once the external gate is fixed |
| chat `search_corpus` / `get_law_details` / `_resolve_law` | title, laws search arm | yes — the tool text already says "Acts", not "SBP's Acts" |
| `laws_links.build_name_index` | top-level documents, external included | yes — an uploaded Act becomes a backlink and `made_under` target by name |
| `/api/laws/{id}/file` | `local_path` | yes |

So: **no new document table**. Provenance rides on the columns that already exist for it,
`RegDocumentVersion.source` (`live` / `wayback`, now also `upload`) and
`RegDocument.is_external`.

### 1.2 Four places assume "every document came from the listing"

1. **Delisting.** `delist_missing()` marks every top-level document absent from a
   complete listing pass as delisted. An uploaded document would disappear from the list,
   search and chat on the next `laws sync`.
2. **Currency.** `select_current_versions()` considers only `source == "live"`. An uploaded
   version can never be the edition in force.
3. **The external gate.** `laws_ai.law_corpus`, `inventory.corpus`, and the Laws view's
   "Hosted outside SBP, so we hold no copy" state all stop at `is_external` instead of
   asking whether a version is held. After an upload, `is_external` is still true — SBP
   still hosts it elsewhere — but the text is here.
4. **Provenance copy.** The Laws view says "captured", "last checked", "SBP's link to this
   file is broken". For an upload the true statement is "uploaded by an administrator on
   this date, from this source". The API already sends `version.source`.

Also: `main._ensure_law_version_cached` answers a missing archive file with "no source
file to download" when `file_url` is null. For an upload that wording is wrong and the
situation is worse — nothing can re-fetch it.

### 1.3 What the existing external rows give us for free

Two top-level rows are `is_external` (the `pakistancode.gov.pk` links): the Banking
Companies Ordinance 1962 and the Deposit Protection Corporation Act 2016. *(This said 19
before implementation measured it; see §10.)* They already carry `RegDocumentLink` edges
from the deterministic name pass — the Ordinance alone has 315 — and are already in the
name index. Uploading their text
attaches a version to a node that is already wired into everything. Identity makes the
attachment automatic: `law_identity("Banking Companies Ordinance, 1962")` is the id the
listing row already has.

### 1.4 Extraction

`scraper.circulars.extract_document_text()` handles `pdf` (pdfplumber, with the `scanned`
status) and `xlsx`. HTML laws are a separate path (`sync_html_version`, scoped to SBP's
`div.border-box`), which does not apply to arbitrary uploaded HTML. Nine of SBP's own law
PDFs are scanned with no text layer; uploads from other sources may be too.

### 1.5 Admin API is read-only by contract

`api/admin.py` stays read-only so `/index/audit` can advertise `write=False` and be
believed (see `AdminSyncTab.vue` header). Writing routes live in `main.py`; the circular
sync route there is the pattern for admin-triggered background work (`SyncStatus` row,
lock, daemon thread).

### 1.6 Deployment

`files/` and `sbpeye.db` are not in the image. `scripts/sync_volume.py` pushes the local
tree **to** the volume and has no pull direction. An upload made through the console on
Railway therefore exists on the volume and nowhere else, in both its bytes and its DB row.
The deployment runbook re-uploads `sbpeye.db` wholesale, which would overwrite rows created
on the volume.

---

## 2. Decisions

Recorded from the design review, 2026-09-08.

1. **No issuer facet now.** A free-text `source_note` carries the citation; an `issuer`
   column and facet can be added later without touching anything here.
2. **Uploads attach to existing external documents automatically**, by title identity.
3. **An upload may override SBP's own copy.** An admin can pin an uploaded version as the
   edition in force for a document SBP hosts (the scanned-PDF case). Unpinning hands
   currency back to SBP's copy.
4. **Single file per document.** No hierarchy for uploads in v1.
5. **Both the production console and a local CLI upload**, with the operating rule in §6
   so the two cannot silently overwrite each other.

---

## 3. Design principles

1. **An upload is a version, not a new kind of document.** Reuse identity, archive,
   currency, indexing and analysis as they stand; add provenance, not structure.
2. **Identity = normalized title**, exactly as for listing rows. A collision is a feature.
3. **Never delete.** No hard delete of an uploaded document or version. Withdrawal is
   `delisted_at`, which every reader already honours, and it is reversible.
4. **Uploads are the least reproducible bytes in the system.** They go in the archive under
   invariant 3.7, and a backup path exists before the feature reaches production.
5. **Say what happened at upload time.** Extraction runs synchronously so the response can
   report "no text layer, will not be searchable" rather than leaving it to be found later.
6. **Every text-mutating path pairs its FTS write with its Chroma write.**

---

## 4. Data model

Columns only, via `database._ensure_columns()`. No new tables.

```python
class RegDocument(Base):
    # sbp_listing | upload. Who put this row here. Delisting only ever touches
    # sbp_listing rows; the listing says nothing about a document it never carried.
    origin = Column(String, nullable=False, default="sbp_listing", index=True)
    # Admin-written citation for the text we hold, e.g. "Consolidated text from
    # pakistancode.gov.pk, as of March 2024". Shown in the provenance bar.
    source_note = Column(Text, nullable=True)


class RegDocumentVersion(Base):
    # source gains the value "upload" beside "live" and "wayback".
    # Admin override of currency (§5). At most one pinned version per document,
    # enforced at pin time. Only meaningful when a live version also exists; for a
    # document with uploads only, uploads compete among themselves and no pin is needed.
    pinned = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(String, nullable=True)      # User.id from sbpeye_app.db
    original_filename = Column(String, nullable=True)
```

**Identity.** `law_identity(title)` unchanged. The upload form previews the resolution
before commit: "new document" or "attaches to *Banking Companies Ordinance, 1962* (listed
by SBP, hosted externally, no text held)". An explicit `document_id` in the request
overrides title resolution for the case where the admin's title wording differs from SBP's.

**When SBP later lists an uploaded document.** `upsert_document` sets `origin =
"sbp_listing"` and the listing's `source_url`/`is_external`; the uploaded versions stay as
history and compete under the tier rule in §5. `source_note` is kept.

**Archive layout.** Unchanged: `files/laws/<document_id>/<hash8>-<original_filename>`.
Written once, never overwritten. `file_url` is NULL for uploads; that is what tells
`_ensure_law_version_cached` there is nothing to fetch.

**Accepted file types (v1).** `pdf` and `txt`. `txt` bypasses extraction — the bytes are the
text, decoded as UTF-8 — and is added to `laws_ai.PARSEABLE_LAW_FILE_TYPES`. HTML is
deferred: it needs a cleaner that is not SBP-specific. DOCX/XLS are rejected with a
message, as the analysis pipeline cannot read them either.

---

## 5. Currency rule with uploads

`select_current_versions()` becomes tiered. Within a tier the existing rule is unchanged
(future `effective_from` is pending; latest arrived `effective_from` wins; otherwise the
version the listing pass observed, then most recently captured).

| Tier | Versions | Note |
|---|---|---|
| 0 | `pinned = 1` | The admin override. At most one per document. |
| 1 | `source = "live"` | SBP's own copy, authoritative by default. |
| 2 | `source = "upload"` | Everything the admin supplied. |
| — | `source = "wayback"` | History only, as today. |

The highest tier with an *eligible* version supplies the winner. A pinned version whose
`effective_from` is still in the future is pending like any other, and the next tier
decides meanwhile.

Consequences worth stating:

- For an external document, or an upload-origin one, tier 2 is the only tier, so the
  newest upload (by `effective_from`, then upload time) is current with no pin.
- Uploading a second edition to such a document supersedes the first, exactly as a new
  SBP hash supersedes an old one. Both stay in the timeline.
- For a document SBP hosts, an upload is captured but *not* current until pinned. The
  response says so.
- The sync re-evaluates currency every run, so unpinning takes effect immediately and a
  new live capture never displaces a pin.
- `index_pending_laws` keys on the current version's `is_vectorized`, and
  `vectorize_law_document` zeroes it on every other version, so a currency flip in either
  direction re-indexes with no extra bookkeeping. `vectorize_law_document` passes the
  version's real `local_path` to Docling for PDFs, which uploads also have.

---

## 6. Operating rule for two upload paths

Two writers, one archive and one database, no merge tool. The rule:

**Production is the writer. Local is for seeding before a push.**

- Routine uploads happen through the console on the deployment.
- Before anything pushes `sbpeye.db` to the volume, run `sync_volume.py pull laws`
  (§7, phase 5) and confirm the local DB already contains the volume's upload rows.
  Pushing a DB that lacks them orphans the files and loses the documents.
- Local CLI upload (`sbpeye laws upload`) is for bulk seeding on a fresh corpus before the
  first push, and for development. Its output is pushed with the normal
  `sync_volume.py push laws` plus the DB.

This is a runbook rule, not a code guard, and it is written into `VOLUME_SYNC.md` and the
deployment plan's risk table in phase 5.

---

## 7. Phases

### Phase 1 — Columns and the two rules (no upload path yet)

**Deliverable:** the schema exists and the listing sync can no longer harm an uploaded row.
Zero behaviour change for the existing corpus.

- Add the columns in §4 to `models.py` and `database._ensure_columns()`.
- `delist_missing()` and `delist_missing_children()`: filter `origin == "sbp_listing"`.
- `select_current_versions()`: the tier rule in §5.
- Fix the external gate: `laws_ai.law_corpus`, `inventory.corpus` and `_law_section`'s
  `stubs` count decide on "no current version" first and report `is_external` only when
  that is why. `GAP_EXTERNAL` keeps its wording for the no-version case.
- `_ensure_law_version_cached`: with `file_url` null and `source == "upload"`, report
  "the uploaded file is missing from the archive".
- Tests (`tests/test_laws_sync.py`): an upload-origin document survives a complete pass;
  an uploaded version on an external document becomes current; an unpinned upload on a
  live document does not; a pinned one does; unpinning restores the live version; a
  future-dated pin is pending. Tests (`tests/test_laws_ai.py`): an external document with
  an uploaded version is analysable.

### Phase 2 — The upload service and CLI

**Deliverable:** `sbpeye laws upload` puts a file into the corpus end to end.

- New module `src/sbpeye/laws_upload.py`:
  - `resolve_upload_target(db, title, document_id=None) -> UploadTarget` — identity
    resolution, returned to the form for its preview.
  - `store_upload(db, *, path_or_bytes, filename, title, doc_type, source_url,
    source_note, version_label, effective_from, uploaded_by, document_id=None,
    pin=False) -> UploadResult`. Steps, in one transaction:
    1. Validate type (§4) and sniff the bytes with `_content_matches_file_type`.
    2. sha256. If a version with that hash exists on **any** document, return it as
       `duplicate_of` and store nothing — the same bytes are the same edition.
    3. Archive under the document's directory with `_archive_name`-style naming
       (hash8 + original filename), never overwriting.
    4. Upsert the `RegDocument`: new rows get `origin="upload"`, `is_external=0`,
       `source_url` from the form; existing rows keep their `origin` and gain
       `source_note` only if given.
    5. Create the version (`source="upload"`, `is_current=0`, `pinned` per request),
       extract text synchronously, store `extraction_status`.
    6. `select_current_versions(db, {document.id})`.
    7. Return the document, the version, and `will_be_searchable` /
       `extraction_status` / `is_current` so the caller can say what happened.
  - `finish_upload(db, document_id)` — the deferred work: `index_law_document`, then a
    backlink pass **scoped to this document** (a name index built from this one row, run
    over every circular with `link_circular_to_laws`; `backlink_circulars(rescan=True)`
    would rescan 3,600 circulars against 133 names to add one). No LLM.
  - `withdraw(db, document_id)` / `restore(db, document_id)` — set/clear `delisted_at`,
    and re-run `index_law_document` so the FTS row and Chroma chunks follow.
  - `pin_version(db, version_id)` / `unpin_version(db, version_id)` — enforce one pin per
    document, re-select currency, re-index.
- CLI: `sbpeye laws upload PATH --title ... --type law|regulation|guideline [--document-id]
  [--source-url] [--note] [--version-label] [--effective-from] [--pin] [-v]`, plus
  `sbpeye laws withdraw ID`, `sbpeye laws restore ID`, `sbpeye laws pin VERSION_ID`,
  `sbpeye laws unpin VERSION_ID`. Runs are recorded in `SyncStatus` with
  `kind="laws_upload"` so they appear in the Runs tab; `circular_sync_only()` already
  excludes any non-circular kind.
- Tests: hash dedupe across documents; attach-to-external by title; explicit
  `document_id`; scanned PDF reports not searchable; `txt` path; withdraw removes the FTS
  row and Chroma chunks and restore brings them back; scoped backlink links a circular
  that names the uploaded Act and nothing else.

### Phase 3 — API

**Deliverable:** the console can do everything the CLI can.

Routes in `main.py`, all `Depends(require_admin)`, using the same service:

| Route | Does |
|---|---|
| `POST /api/laws/upload/resolve` | `{title, document_id?}` → the preview: new or attaches-to, with the target's summary. |
| `POST /api/laws/upload` | multipart: file + form fields from §7 phase 2. Extraction synchronous; indexing and backlink in a daemon thread under a `SyncStatus` row, as circular sync does. Returns 202 with the document payload and `extraction_status`, `is_current`, `indexing: "queued"`. |
| `POST /api/laws/{id}/withdraw`, `.../restore` | as named |
| `POST /api/laws/{id}/versions/{vid}/pin`, `.../unpin` | as named |
| `GET /api/laws/uploads` | upload-origin documents plus external documents with no held version (the suggested targets), with extraction and index state. |

Payload additions in `serializers.py`: `origin`, `source_note` on the document;
`pinned`, `uploaded_by`, `original_filename` on the version. `_law_summary`'s
`is_external` stays true after an upload — the reader distinguishes the cases by
`current_version`, not by that flag.

Size cap: `LAWS_UPLOAD_MAX_BYTES`, default 50 MB, enforced while streaming to the temp
file so an oversized upload never reaches memory. Tests in `tests/test_routes_smoke.py`
and a new `tests/test_laws_upload_api.py`: non-admin gets 403; oversize gets 413;
duplicate returns the existing version; the response carries the searchability verdict.

### Phase 4 — Frontend

**Deliverable:** admins upload from the console; every reader sees honest provenance.

- **Admin tab "Library"** (`views/admin/AdminLibraryTab.vue`, route `/admin/library`,
  seventh tab): the upload form with the resolve preview, then a table from
  `/api/laws/uploads` with per-row withdraw/restore and per-version pin/unpin. Suggested
  targets (external, no text) sit at the top, since they are the reason the tab exists.
- **Laws view**: the "Hosted outside SBP" empty state keeps its wording when no version is
  held and gains an admin-only "Add its text" button that opens the Library tab with the
  document preselected (`adminOnly.ts` supplies the non-admin hint). When a version *is*
  held, the reader shows it as for any other document. The provenance bar branches on
  `current_version.source`: "Uploaded by an administrator on {date}. {source_note}",
  with the `source_url` as the outbound link where given; "Pinned over SBP's copy" when
  `pinned`. The version timeline labels each edition by source.
- `api.ts`: fields from phase 3, `uploadLaw`, `resolveLawUpload`, `withdrawLaw`,
  `restoreLaw`, `pinLawVersion`, `unpinLawVersion`, `getLawUploads`.
- Type chips need nothing: `/api/laws/types` already derives from the data and
  `typeLabel` handles any value.

### Phase 5 — Deployment and backup

**Deliverable:** an upload made on the volume can be recovered.

- `scripts/sync_volume.py pull laws [--apply]`: the reverse of `push` for one subtree,
  same chunked tar path, same size-and-path verification. Never deletes locally.
- `VOLUME_SYNC.md`: the operating rule from §6, and `pull laws` in the quick reference.
- `DEPLOYMENT_PLAN.md`: a risk-table row — "uploaded law files and rows exist only on the
  volume until pulled" — and a note under 3.7 that the archive now holds bytes with no
  origin URL at all.
- Admin Deployment tab: show the count of upload-origin documents whose files are not yet
  known to be pulled? Deferred — the pull tool's own `status` answers it.

---

## 8. Explicit non-goals (for now)

- An `issuer` column or facet (decision 1). `source_note` is the placeholder.
- Multi-file documents, uploaded chapters, or attaching an uploaded part to an SBP
  container (decision 4).
- HTML or DOCX uploads.
- OCR for scanned uploads. The upload reports the gap; it does not close it.
- Hard deletion of anything under `files/laws/`.
- Merging two databases. §6 is the rule instead.
- Non-admin uploads or a review queue.

## 9. Open questions, as answered in implementation

All three went the way they were leaning.

1. **`RegDocument.source_url` for an upload-origin document is optional.** An Act typed in
   from a gazette has no URL, and `source_note` carries the citation. The form asks for it
   and marks it optional.
2. **A pinned upload does not suppress `refetch_requested`.** SBP's copy keeps being
   captured as history; the pin decides which edition is *in force*, which is a different
   question from which editions are worth holding.
3. **The scoped backlink does not run the AI `relationships` pass.** `finish_upload` runs
   `index_law_document` and a deterministic name/URL scan over the circulars, and no LLM.
   Typing law→law edges stays on the reader's existing Generate button.

## 10. Implementation notes

Where the code diverged from the plan, and why.

- **Two counts in the plan were wrong about the corpus.** There are **2** external
  top-level rows, not 19 (`Banking Companies Ordinance 1962` and `Deposit Protection
  Corporation Act 2016`), and the Ordinance carries **315** name-matched circular
  backlinks, not 314. The design is unaffected — both are still the motivating case.
- **The Library tab lives at `/admin/documents/library`, not `/admin/library`.** The plan
  said "seventh tab" when the console was a flat row of them; it is now sectioned, and
  Library belongs under Documents beside Workbench and Corpus statistics.
- **`_law_section`'s `external` count now means "external *and* holding nothing".** The
  console presents it under "Awaiting content", which stopped being true of an external
  row once an upload attached text to it. A new `external_held` counts the other case.
- **`searchable_law_version` became the single definition of what search sees**, shared by
  `index_law_fts` and `vectorize_law_document`, and it now honours `delisted_at`. Both
  search arms already filtered delisted documents at query time; the index holding rows
  that the query drops a moment later is how `withdraw` left a title-only FTS row behind.
- **`txt` joined `PARSEABLE_LAW_FILE_TYPES`.** It carries no `local_path` into the corpus
  payload, so like `html` it reaches Docling through the Markdown-string branch.
- **`sync_volume.py pull` fetches over `railway ssh` + base64**, because the CLI has no
  counterpart to `volume files upload` that fetches and `ssh` is the one remote primitive
  the script already depends on. It batches the file list (argv is an ARG_MAX budget),
  refuses a payload over 256 MB rather than streaming it through a pipe, and never deletes
  or overwrites — a size mismatch is reported as a conflict, since under `files/laws/` a
  filename carries its own content hash.
