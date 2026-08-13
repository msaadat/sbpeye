# Laws frontend — MVP shipped, and what comes next

Companion to `docs/LAWS_FRONTEND_PLAN.md`. Records what the MVP actually does, what it
deliberately leaves out, and the order to build the rest in.

**Status:** MVP (§1) shipped; Step 0 (archive) and Step 1 (row legibility) done — §2, §3.
Next up is Step 2, the two API gaps in §5.

---

## 1. What shipped

A `/laws` destination built on the **library + reader** model: a persistent tree of the
whole corpus on the left, a persistent viewer on the right. Clicking a document changes
only the viewer — no page transition, no contents screen, no dialog.

| File | Change |
|---|---|
| `frontend/src/lib/api.ts` | `LawSummary` / `LawDetail` / `LawVersion` types, `getLaws`, `getLawTypes`, `getLawDetail`, `buildLawFileUrl` |
| `frontend/src/router/index.ts` | `/laws`, `/laws/:id` |
| `frontend/src/App.vue` | Sixth rail item, `pi pi-book`, placed next to Circulars |
| `frontend/src/views/LawsView.vue` | The view: library tree, reader, provenance bar |
| `src/sbpeye/main.py` | `/laws` and `/laws/{path}` SPA routes — the backend serves the SPA per-path, so a new route needs registering here too |
| `src/sbpeye/api/serializers.py` | `part_order` added to `_law_summary`. Without it the list payload cannot order a container's parts, and the FE Manual rendered alphabetically — "Chapter 2 AUTHORIZED DEALERS" first. Additive, and mixed search gets it too |

**Behaviour**

- The whole corpus (133 documents) is fetched once at mount and held client-side. At this
  size a tree beats paging, and it means containers know their part counts without a
  second request.
- Top-level documents are grouped by type in authority order — law, regulation, guideline.
- Containers expand in place. Selecting a part keeps its container open, so a part is
  never shown without its context (plan §1.2).
- The viewer is `<iframe src="/api/laws/{id}/file?version_id=…">` — the archived file from
  our disk, never re-fetched from sbp.org.pk (plan principle 4).
- Documents with no file get an honest destination instead: the circular it really is, an
  outbound link for externally-hosted statutes, "pick a part" for containers, or a plain
  note that SBP's link is broken (plan §1.4).
- A provenance bar under the viewer carries custody and citations, collapsed to one line,
  expanding on click. With one edition it reads as a sentence, not a one-dot timeline
  (plan §1.3).
- Search calls `/api/laws?q=` and shows flat results; a matched part carries its container
  name above the title.

Verified against live data on `localhost:8000`: 75 top-level documents (19 law,
22 regulation, 34 guideline), the Foreign Exchange Manual deep-linking open to its
26 chapters in chapter order, and a chapter opening with breadcrumb, part label, custody
line and 4 citing circulars. `npm run typecheck` passes.

The Python test suite was originally recorded here as unrunnable — a chromadb
`PanicException` during collection. That was overstated: the panic is confined to the
modules that import chromadb, and the laws suites run clean when named directly, so
`part_order` *is* test-covered. See §6.

---

## 2. Step 0 — the archive, now populated ✅

The MVP shipped against an empty archive: 106 of 114 versions recorded a `local_path`
under `attachments/laws/` and none of those files existed, so `GET /api/laws/{id}/file`
returned `{"error":"Archived file is missing"}` for every document and the viewer rendered
that JSON. It was local machine state, not a code fault.

The sync has since been run. `attachments/laws/` now holds 75 MB and **all 106 paths
resolve** — re-check any time with:

```bash
python -c "import sqlite3,pathlib; c=sqlite3.connect('sbpeye.db'); rows=[r[0] for r in c.execute('select local_path from reg_document_versions where local_path is not null')]; print(len(rows),'rows;',sum(1 for r in rows if not pathlib.Path(r).is_file()),'missing')"
```

**One bug surfaced the moment there were bytes to serve.** `get_law_file` used
`FileResponse(candidate, filename=...)`, which sets `content-disposition: attachment` —
so the browser downloaded the PDF instead of rendering it, and the reader's iframe stayed
blank. Fixed by passing `content_disposition_type="inline"` (`main.py`). The filename is
kept, so saving from the viewer still gets a meaningful name. **Nobody could have caught
this before the archive was populated**, which is why it outlived the MVP.

---

## 3. Step 1 — making the rows read correctly ✅

| File | Change |
|---|---|
| `src/sbpeye/scraper/laws.py` | `split_law_title()` — a public wrapper over the existing, tested `_split_version_suffix` |
| `src/sbpeye/api/serializers.py` | `display_title` + `version_suffix` on `_law_summary`; `display_title` on the detail payload's `parent` and `children` |
| `src/sbpeye/main.py` | `content_disposition_type="inline"` on `get_law_file` (§2) |
| `frontend/src/lib/api.ts` | The three new fields on `LawSummary` / `LawChild` / `parent` |
| `frontend/src/views/LawsView.vue` | Type facets, holdings, `display_title` throughout, two layout fixes |
| `tests/test_laws_sync.py` | `split_law_title` coverage, including the acronyms that must survive |

**Version suffixes are split server-side, not in the browser.** The plan called this
"pure frontend, no API work", but the rule for recognising a suffix already exists and is
tested in `scraper/laws.py` — the phrase set spans `Updated till…`, `as modified up to…`,
`being updated`, `w.e.f.`, and a comma-led form, while `(FIS)`, `(URDU)`, `(Apr-Jun)` and
`(Amendment) Act` must survive untouched. A TypeScript copy would have drifted the first
time SBP invented a new phrasing. Two fields on a serializer that already carries
`part_order` was the cheaper and more honest fix. Measured over the whole corpus: **8 of
133 titles carry a suffix, and no acronym is a false positive.**

- Rows now read *"Prudential Regulations for SME Financing"* with *"Updated till July 16,
  2026"* on the status line; the reader header does the same.
- **Type facets** — `All 75 · Law 19 · Regulation 22 · Guideline 34`, driven by
  `/api/laws/types` for which types exist, but showing *top-level* counts, because those
  are the rows the tree actually renders. The API's corpus-wide count (which includes the
  58 parts) would contradict what the user is looking at, so it lives in the tooltip. The
  filter also re-runs an active search server-side via `doc_type`.
- **Holdings** — a container now reads `26 parts · 25 held`, and where the two differ a
  2px meter appears under the row. Expanded, each unheld part is marked `not held`. This
  makes the Reporting Guidelines' `9 parts · none held` visible at a glance instead of
  hiding behind a bare part count.

**Two pre-existing layout bugs found while verifying, both fixed:**

1. Search-result rows were being squeezed into the 0.85rem caret column — **13.6px wide
   instead of 261px**, one word per line. `.node` is a two-column grid and only tree rows
   supply the caret span; search rows fell into column 1. Now `.node.is-flat`.
2. Parts whose `part_label` equals their title rendered as *"NBFCs — NBFCs"*.

Verified against live data on `localhost:8001`: the facet filter narrows to 22 regulation
rows, a search for "export proceeds" returns 49 matches with parts carrying their
container and narrows to 27 under the Regulation facet, the Foreign Exchange Manual's
Chapter 12 loads Chrome's PDF embedder against the archived 791 KB file, and the
Reporting Guidelines container explains that SBP's link is broken for all 9 of its parts.
`npm run typecheck` clean; **168 backend tests pass** (see §6).

---

## 4. Deliberately left out

Not bugs — scope. Roughly in the order they hurt:

1. **Search results are flat**, not nested under their container, and the reader does not
   highlight matches.
2. **No Text view.** PDF only, so no in-document search, no copying from scans.
3. **No section navigation inside a single long PDF** — needs a label→page map.
4. **No delisted filter, no "recently changed" view, no mixed search, no "regulations
   cited" on circulars.** These are plan phases F5–F7.
5. **Cited-by is capped at 8 with no "show all"** — fine for most, wrong for the Banking
   Companies Ordinance's 314.
6. **No empty-corpus state**, no retry affordance on a failed load.
7. **Nothing is responsive below ~900px** and the library width is fixed at 300px.

---

## 5. Order to build the rest

### ~~Step 0 — populate the archive~~ ✅ §2
### ~~Step 1 — make the rows read correctly~~ ✅ §3

### Step 2 — close the two API gaps that block real features
- `sort_by=captured` on `/api/laws` → unlocks "Recently changed", which is the single most
  valuable view this corpus can offer, because it is the thing SBP itself cannot tell you.
- `regulations` array on `/api/circulars/{id}` → unlocks the reverse link.

Both are small and both are already named in the plan (§2.1).

### Step 3 — "Recently changed" and the arrival panel
A tab above the library plus an overview in the reader when nothing is selected: what
changed in the last 30 days, and the most-cited documents. Turns the empty state into the
product's argument for itself.

### Step 4 — search that respects the hierarchy
Nest hits under their container in the tree; show a per-document hit count. Needs
`parent_id` on every search hit — check whether the search path already returns it, since
`_law_summary` includes it and the search results reuse that serializer.

### Step 5 — the reverse link on circulars
"Regulations cited" in `CircularDetailPane`'s rail, each entry opening `/laws/:id`. This is
what makes 809 links useful in the direction people actually read.

### Step 6 — decide on Text view
Only worth it once someone has used the PDF viewer in anger. The native iframe cannot
highlight our search terms; a Text view built on the extracted text can, and also covers
copying. Check first whether extraction quality justifies it — `extraction_status` is
already on every version, so count the failures before committing.

### Step 7 — section navigation inside long PDFs
Read the PDF bookmark outline at capture time, store `label → page` beside the version,
and jump the iframe with `#page=N`. Cheap where outlines exist, impossible where they
don't — sample a handful with `pypdf` before committing.

---

## 6. Open items

- ~~**Two figures in the plan disagree.**~~ **Reconciled — both were wrong.** The 21
  no-content documents actually break down as **11 parts · 5 circular stubs · 3 dead
  top-level links · 2 externally hosted**. Plan §1.4's "14 stubs / 2 external / 5 dead"
  conflates the two levels; §4.3's "9 dead Reporting Guidelines parts" is right but is 9
  of the 11 dead parts — the other 2 are CPIS questionnaires. The holdings meter now
  renders this from the data rather than from a number in a doc:

  ```sql
  select d.parent_id is null as top_level, d.is_external, d.circular_id is not null
  from reg_documents d
  where d.id not in (select document_id from reg_document_versions)
  group by 1, 2, 3;
  ```

- **The two pakistancode.gov.pk statutes** — including the Banking Companies Ordinance,
  the most-cited document in the corpus at 314 links — still cannot be opened. Now that
  the archive is populated and everything else renders, this is the largest remaining
  hole in "read-first", and the highest-value backend item.
- **`/api/laws/{id}/file` ignores a bad `version_id`** and falls through to a 404 that
  reads the same as "no file". Still worth distinguishing.
- **The test suite runs after all — the earlier claim was too pessimistic.** `pytest` over
  the whole `tests/` directory still dies during collection on a chromadb `PanicException`
  (`range start index 10 out of range for slice of length 9`), unrelated to this work. But
  that failure is confined to the modules that import chromadb; naming the laws and route
  suites directly runs them clean:

  ```bash
  pytest tests/test_laws_sync.py tests/test_laws_hierarchy.py tests/test_laws_search.py tests/test_laws_backlink.py tests/test_laws_circular_rows.py tests/test_routes_registered.py tests/test_routes_smoke.py
  ```

  168 pass, and both serializer changes (`part_order` from the MVP, `display_title` from
  Step 1) are covered. Finding which module trips the panic is still worth doing.
