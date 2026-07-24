# Plan: Incorporate SBP Laws & Regulations into SBPEye

Backend-only plan for indexing `https://www.sbp.org.pk/laws-regulations` alongside circulars.
Composable: each phase is independently shippable and useful on its own; later phases only
build on earlier ones, never rewrite them.

---

## 1. Findings (July 2026 analysis)

Sources: live site (verified 2026-07-24), Wayback snapshot of the listing page (2026-07-15),
and the current `sbpeye.db`.

### 1.1 Listing page (`/laws-regulations`)

- Single server-rendered page, five sections, each an HTML table:
  **Laws** (19), **Regulations** (23), **Gazette Notifications** (0 rows observed),
  **Guidelines** (34), **Licensing Guidelines** (0 rows observed). A "Filter By Type"
  checkbox panel filters rows client-side; all data is in the initial HTML.
- Every item row is `<tr class="law-row" data-type="..." data-title="..."
  data-search="..." data-date="YYYY-MM-DD">` — metadata is in attributes, so parsing is
  robust to layout changes.
- `data-date` is unreliable for old laws (placeholder values like "30 December 1881");
  treat it as display metadata only, never as a version signal.

### 1.2 Link taxonomy — four destination types per row

| Type | Pattern | Examples | Handling needed |
|------|---------|----------|-----------------|
| Direct PDF | `/assets/documents/laws_regulations/<file>.pdf` | All Laws, most Regulations | fetch, hash, version, extract |
| Subpage | `/laws-regulations/<slug>` | Foreign Exchange Manual, Reporting Guidelines, CPIS/FIS | recurse: container + children |
| Circular | `/circulars/<slug>` | several Guidelines (e.g. IBD Circular 04 of 2020) | resolve to existing `Circular`, do not duplicate |
| External | `pakistancode.gov.pk/...` | laws marked "(being updated)" | metadata only, never fetched |

### 1.3 Versioning behavior (the core problem)

- SBP **replaces PDFs in place** at the same URL; previous versions disappear from the site.
- Version info appears only in title suffixes ("(Updated on May 2025)", "(updated till
  October 07, 2024)") and occasionally as parallel rows (SME PRs Oct-2024 edition and
  Jan-2026 edition listed simultaneously).
- On the old site, FE Manual chapter URLs carried revision years (`pdf/2018/Chapter-12.pdf`,
  `pdf/2021/Chapter-20.pdf`). On the new site those paths are flat and year-free
  (`/assets/document/Chapter-12-foreign-exchange-manual.pdf`), so **content hashing is the
  only remaining version detector**.
- Consequence: SBPEye should become the historical archive SBP does not keep — every
  fetched file is stored immutably, forever.

### 1.4 Multi-part documents (Foreign Exchange Manual, verified live)

- Container subpage → 22 chapters (one PDF each) + front matter + appendices I–V.
- Chapters revise **independently** (proven by the old per-chapter year paths).
- **Appendix III is itself another `/laws-regulations/<slug>` subpage** whose content is
  ~40 FE notifications as inline HTML (no PDFs) → hierarchy depth ≥ 3, and a document
  "part" can be an HTML page rather than a file.
- A third asset store exists: `/assets/document/` (singular), distinct from
  `/assets/documents/laws_regulations/` and `/assets/documents/circulars/`. Child files may
  also live at arbitrary sbp.org.pk paths — accept anything passing `is_allowed_sbp_url`.

### 1.5 Overlap with circulars (current DB: 3,642 circulars, 1,462 attachments)

- Several "Regulations" PDFs are literally circular annexures re-hosted in the laws folder
  (`CL33-Annex-B.pdf` = AML/CFT/CPF Regulations, `CL44-Annex.pdf`, `C8-RBA-Guidelines.pdf`).
- 23 existing circulars already reference `laws-regulations` URLs in their text; 0
  attachments currently point into the laws asset folder.
- Circulars are the *change mechanism* for regulations: a circular announcing an amendment
  to the Prudential Regulations is the trigger that a new version exists.

### 1.6 Historical backfill source

- The Wayback Machine has captures of the listing page, many asset URLs, and the old
  site's year-stamped FE Manual chapter PDFs (`.../fe_manual/pdf/<year>/...`) —
  recoverable pre-history.

---

## 2. Design principles

1. **Laws/regulations are not circulars.** Circulars are immutable dated events; these are
   living documents with stable identity and changing content. Separate models.
2. **Identity = normalized title** (version suffixes stripped), mirroring how
   `circular_identity()` uses `normalize_reference()`. "PRs for MFBs (Updated on May 2025)"
   and a future "(Updated 2027)" resolve to the same document.
3. **Versions = content hashes.** No trust in URLs, dates, or titles for change detection.
4. **Archive immutably.** Never overwrite a downloaded file; never delete a delisted row.
5. **Reuse existing plumbing:** `_get_sbp`/cloudscraper, HTML disk cache, pdfplumber
   extraction, `extract_sbp_text()`, FTS5 + Chroma indexing conventions, Click CLI verbs.
6. **Every text-mutating code path pairs its FTS write with its Chroma write** (same rule
   AGENTS.md states for circulars).

---

## 3. Data model

Three new tables (created via the existing `_ensure_columns()` mechanism in `database.py`,
no Alembic).

```python
class RegDocument(Base):
    __tablename__ = "reg_documents"

    id            = Column(String, primary_key=True)   # uuid5 — see identity rules below
    title         = Column(String, nullable=False)     # raw current title from listing
    normalized_title = Column(String, index=True)      # identity basis, suffixes stripped
    doc_type      = Column(String, index=True)  # law|regulation|guideline|gazette|licensing
    source_url    = Column(String)               # listing link (PDF, subpage, or external)
    page_slug     = Column(String, nullable=True)      # for /laws-regulations/<slug> pages
    # Hierarchy (FE Manual etc.). NULL for flat single-file documents.
    parent_id     = Column(String, ForeignKey("reg_documents.id"), nullable=True, index=True)
    part_label    = Column(String, nullable=True)      # "Chapter 12", "Appendix III"
    part_order    = Column(Integer, nullable=True)     # listing position within parent
    # Dedupe path: set when the listing item is actually a circular page.
    circular_id   = Column(String, ForeignKey("circulars.id"), nullable=True)
    is_external   = Column(Integer, default=0)         # pakistancode.gov.pk etc.
    listed_date   = Column(DateTime, nullable=True)    # data-date; display only
    first_seen_at = Column(DateTime)
    last_seen_at  = Column(DateTime)
    delisted_at   = Column(DateTime, nullable=True)    # vanished from listing; never delete
    # AI enrichment timestamps, mirroring Circular
    summary       = Column(Text, nullable=True)
    tags          = Column(Text, nullable=True)
    summary_generated_at = Column(DateTime, nullable=True)
    tags_generated_at    = Column(DateTime, nullable=True)


class RegDocumentVersion(Base):
    __tablename__ = "reg_document_versions"

    id            = Column(String, primary_key=True)
    document_id   = Column(String, ForeignKey("reg_documents.id"), index=True)
    content_hash  = Column(String, index=True)  # sha256 of PDF bytes, or of cleaned text
                                                # for HTML-content pages (raw HTML churns
                                                # on template tweaks)
    file_url      = Column(String, nullable=True)   # NULL for HTML-content versions
    local_path    = Column(String, nullable=True)   # immutable archive copy
    file_type     = Column(String)                  # pdf | doc | html | ...
    version_label = Column(String, nullable=True)   # parsed "(Updated till June 2024)"
    content_text  = Column(Text, nullable=True)     # extracted text
    extraction_status = Column(String, default="pending")
    extraction_error  = Column(Text, nullable=True)
    is_vectorized = Column(Integer, default=0)
    is_current    = Column(Integer, default=1, index=True)
    # Parsed from title suffixes like "(to be applicable from January 1, 2026)".
    # Drives the collision rule for parallel editions — see identity rules below.
    effective_from = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime)
    last_seen_at  = Column(DateTime)
    # Provenance: live | wayback (for backfilled history)
    source        = Column(String, default="live")


class RegDocumentLink(Base):
    __tablename__ = "reg_document_links"

    id            = Column(Integer, primary_key=True)
    circular_id   = Column(String, ForeignKey("circulars.id"), index=True)
    document_id   = Column(String, ForeignKey("reg_documents.id"), index=True)
    link_type     = Column(String)   # amends | annexure_of | references | implements
    detected_via  = Column(String)   # url_scan | ai | listing
    confidence    = Column(Float, nullable=True)
    created_at    = Column(DateTime)
```

**Identity rules**

- Top-level document: `uuid5(NAMESPACE_URL, "sbp-law:" + normalized_title)`.
  Normalization strips `(Updated ...)`, `(as of ...)`, `(updated till ...)`,
  `(to be applicable from ...)`, trailing dates, and collapses whitespace/case.
- Child of a container: `uuid5(NAMESPACE_URL, "sbp-law:" + parent_slug + ":" + part_key)`
  where `part_key` is the chapter number / appendix numeral — **not** the title, because
  titles get re-worded across editions while "Chapter 13" stays Chapter 13.
- Container manifest versions: the container gets a `RegDocumentVersion` whose
  `content_text` is a manifest JSON (ordered child ids + their current hashes) and whose
  `content_hash` is computed over the child hashes. A new manifest row appears only when a
  child actually changed — it doubles as the change log ("which chapters changed between
  any two sync dates").

**Collision rule (parallel editions)**

The listing can carry **multiple live rows that normalize to the same identity** — observed
today: "PRs for SME Financing, updated till October 07, 2024" alongside "PRs for SME
Financing (to be applicable from January 1, 2026)". Both are the same `RegDocument`; the
naive per-row sync would flip `is_current` back and forth between their two hashes on every
pass. Rule:

1. Group all live listing rows by resolved document id **before** deciding currency; each
   row's content is fetched, hashed, and stored as its own version (dedupe by hash as
   usual). Fetch order must never influence `is_current`.
2. Parse `effective_from` from the title where present ("to be applicable from ...",
   "applicable from ..."). Currency selection, evaluated once per document per sync:
   the current version is the one with the **latest `effective_from` that is ≤ today**;
   versions with a future `effective_from` stay `is_current=0` (pending) until their date
   passes. If no `effective_from` parses, fall back to latest `listed_date`, then to
   listing row order.
3. Because a pending version becomes current by the mere passage of its effective date,
   sync must **re-evaluate currency on every run**, not only when a new hash appears.
4. Only the current version is vectorized/indexed for search (per phase 5); pending
   versions surface in the version timeline with a "not yet in force" state derivable
   from `effective_from > now`.

**Archive layout**

```
attachments/laws/<document_id>/<hash8>-<original_filename>
```

Written once, never overwritten. `local_path` stores the relative path (same convention as
`Attachment.local_path`).

---

## 4. Phases

Each phase ends with a working system and its own tests. Order within a phase is fixed;
phases 5–8 are largely independent of each other and can be re-prioritized.

### Phase 1 — Models + migration (no scraping)

**Deliverable:** tables exist; app boots; zero behavior change.

- Add the three models to `models.py`.
- Extend `database.py` table creation / `_ensure_columns()`.
- Unit test: fresh DB creates tables; existing DB migrates in place.

### Phase 2 — Listing scraper for flat documents (the 80% case)

**Deliverable:** `sbpeye laws sync` captures every direct-PDF item with versioning.

- New `scraper/laws.py`:
  - `fetch_listing()` — parse `tr.law-row` rows (`data-type`, `data-title`, `data-date`).
  - `route_link()` — classify each row into the four link types (§1.2). In this phase,
    only **direct PDF** and **external** are processed; subpage and circular rows are
    stored as `RegDocument` stubs (metadata only) for later phases.
  - `sync_document(doc, url)` — download via `_get_sbp`, sha256, compare against known
    version hashes:
    - known hash → bump that version's `last_seen_at`;
    - new hash → archive file, insert new version;
    - listing row gone → set `delisted_at` (keep everything).
  - Currency selection runs **after** all rows are processed, grouped by document id, per
    the collision rule in §3 — never as a side effect of fetching a single row. This also
    re-promotes pending versions whose `effective_from` has passed since the last sync.
  - Title normalization + `version_label`/`effective_from` parsing helpers (pure
    functions, unit-testable — include the parallel SME PRs rows as a test case).
- Text extraction via the existing pdfplumber pipeline (reuse the attachment extraction
  code path; factor a shared helper if needed rather than duplicating).
- CLI: `sbpeye laws sync [--type law|regulation|...] [--limit N] [--force] [-v]`,
  `sbpeye laws status` (counts by type, versions, pending extractions).
- Record runs in `SyncStatus` with a distinguishing `parameters` payload.

### Phase 3 — Hierarchy: subpages, children, manifests

**Deliverable:** FE Manual and other subpage items fully captured.

- Subpage handler in `scraper/laws.py`:
  - Fetch `/laws-regulations/<slug>`, parse content tables (plain `<tr>` rows: number +
    title + link; no data-attrs on subpages).
  - Create child `RegDocument`s (`parent_id`, `part_label`, `part_order`) with identity
    from `part_key`.
  - **Recurse** when a child links to another `/laws-regulations/<slug>` page (Appendix III
    case), with a visited-set to prevent cycles.
  - HTML-content children (no file links): `content_text` from `extract_sbp_text()`,
    version hash over cleaned text, `file_type="html"`, `file_url=NULL`.
  - Child files may live at any sbp.org.pk path (`/assets/document/`, `/epd/`, ...) —
    gate on `is_allowed_sbp_url` only.
  - After syncing children, write the container manifest version if any child changed.
- Tests against saved fixture HTML of the FE Manual page and Appendix III page.

### Phase 4 — Circular-typed rows: resolve, don't duplicate

**Deliverable:** Guidelines that are circulars point at the existing `Circular` rows.

- For rows linking to `/circulars/<slug>`: resolve via the existing by-URL lookup
  (`normalize_sbp_url` + `Circular.url/new_url/old_url` matching), set
  `RegDocument.circular_id`, create a `RegDocumentLink(link_type="listing",
  detected_via="listing")`. No content is stored on the RegDocument side.
- If the circular is not yet in the DB, keep the stub and retry on later syncs (the
  circular sync may simply not have reached it yet).

### Phase 5 — Search indexing

**Deliverable:** laws/regulations appear in hybrid search with a source filter.

- FTS: new `laws_fts` FTS5 virtual table (or extend the unified index with a `doc_kind`
  column — decide when touching `search.py`; separate table is lower-risk). App-layer
  `index_law_fts(db, version)` mirroring `index_circular_fts`, called from every code path
  that writes `content_text`.
- Chroma: embed current versions' text into the existing collection with metadata
  `{"kind": "law", "doc_type": ..., "document_id": ...}` so the vector side can filter.
  Only `is_current` versions are vectorized; superseded versions are removed from Chroma
  (history stays in SQLite/archive).
- `search.py`: add a `source` filter (`circulars` | `laws` | `all`) to the hybrid search;
  RRF fusion unchanged. Results carry `result_kind` so API consumers can badge them.
- API (backend only, consumed later by the SPA):
  - `GET /api/laws` — list, filters: `doc_type`, `q`, parent/children expansion.
  - `GET /api/laws/{id}` — detail: current version, version timeline, children, linked
    circulars.
  - `GET /api/laws/{id}/versions/{vid}` — version detail incl. archived file reference
    (served through the existing `/api/pdf_preview` mechanism via `local_path`).
- CLI: `sbpeye laws reindex`.

### Phase 6 — Cross-linking with circulars

**Deliverable:** bidirectional circular ↔ regulation graph.

- **URL backfill pass** (`sbpeye laws backlink`): scan all circulars' `content_text` and
  attachment `original_url`s for `laws_regulations` / `laws-regulations` /
  `/assets/document/` URLs; match to `RegDocument`/version `file_url`s; write
  `RegDocumentLink(detected_via="url_scan")`. (23 circulars match today.)
- **Forward hook:** when circular sync encounters a link into a laws asset store, create
  the link at scrape time instead of waiting for the next backfill.
- **AI relationships:** extend `extract_relationships()` so a circular can target a
  regulation by name ("Prudential Regulations for Consumer Financing are amended...");
  resolve the name against `normalized_title` (+ part_label for FE Manual chapters:
  "Chapter 12 of the FE Manual"); write `RegDocumentLink(detected_via="ai", link_type=
  "amends", confidence=...)`.
- **Change-detection trigger:** when a new circular links to (or is AI-detected as
  amending) a RegDocument, mark that document for immediate re-fetch on the next
  `laws sync` (add a `refetch_requested` flag or reuse `last_seen_at=NULL` semantics).
  This is how new versions get discovered promptly instead of on a schedule.

### Phase 7 — AI enrichment

**Deliverable:** summaries and tags for regulations; chapter-granular for manuals.

- Reuse `summarize()` / `generate_tags()` pipelines over `RegDocument` current-version
  text; chapter-sized children are the summarization unit for containers (containers get a
  rollup of child summaries, not a monolithic pass).
- CLI: `sbpeye laws summarize`, `sbpeye laws tags` with the standard
  `--force/--limit/--delay/-v` options; store `*_generated_at` like circulars.
- (Deferred, valuable later: version-diff summaries — "what changed between the Oct 2024
  and Jan 2026 SME PRs" — needs ≥2 captured versions first, which phases 2+8 produce.)

### Phase 8 — Historical backfill

**Deliverable:** pre-seeded version history that SBP no longer serves.

- `sbpeye laws backfill` — query the Wayback CDX API for captures of known `file_url`s
  and of the old site's legacy paths (e.g. `www.sbp.org.pk/fe_manual/pdf/<year>/...`);
  download distinct-hash captures; map them to existing document identities (chapter
  number → child id; normalized title → top-level id); insert as `source="wayback"`
  versions with `is_current=0`, timestamped by capture date (or the year in a legacy
  path where present).
- Dedupe by `content_hash` — a backfilled file identical to a known version only widens
  that version's `first_seen_at`.

---

## 5. Open questions / verify during implementation

1. **Gazette Notifications & Licensing Guidelines** sections rendered 0 rows in the
   snapshot. Re-check on the live page; if they populate later, phase 2 handles them for
   free (they're just more `law-row`s). The separate `/notifications` site section is
   **out of scope** for this plan.
2. Whether any listing PDFs are scanned images (no text layer) — if so, extraction falls
   back to the same handling attachments use today (status `failed`/empty text; OCR is out
   of scope).
3. Rate limiting: the listing has ~80 items; FE Manual adds ~30 files. A full cold sync is
   ~110 downloads — keep the existing polite delay/throttle conventions from circular sync.
4. Whether `data-date` on *new* Regulations rows is reliable enough to display (it looked
   correct for recent items, fabricated for old laws).

## 6. Explicit non-goals (for now)

- Frontend (views, routes, badges) — separate plan once phase 5's API is stable.
- OCR for image-only PDFs.
- Splitting Appendix III into per-notification documents (start with one HTML document).
- The `/notifications` section of the SBP site.
- Version-diff AI summaries (noted in phase 7 as deferred).
