# Laws frontend — MVP shipped, and what comes next

Companion to `docs/LAWS_FRONTEND_PLAN.md`. Records what the MVP actually does, what it
deliberately leaves out, and the order to build the rest in.

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

The Python test suite could **not** be run to confirm the serializer change: `pytest`
fails during collection with a chromadb `PanicException` in this environment. That failure
reproduces with the backend edits stashed, so it predates this work — but it does mean the
`part_order` addition is unverified by tests.

---

## 2. Blocker found while verifying: the law archive is empty

**106 of 114 versions record a `local_path` under `attachments/laws/`. None of those files
exist on disk — the directory itself is absent.** `attachments/` holds 1,862 circular
folders (563 MB) and no `laws/` folder at all.

So `GET /api/laws/{id}/file` returns `{"error":"Archived file is missing"}` for every
document, and the MVP's viewer renders that JSON. The frontend is doing the right thing;
there is nothing to show it.

`attachments/` is gitignored, so this is local machine state, not a code fault — the
download path in `scraper/laws.py` looks correct. Most likely the laws sync has not been
run on this machine, or the archive was cleaned after the sync that wrote those rows.

**Fix before anything else:**

```bash
sbpeye laws sync --force
```

Then re-check that the paths resolve:

```bash
python -c "import sqlite3,pathlib; c=sqlite3.connect('sbpeye.db'); rows=[r[0] for r in c.execute('select local_path from reg_document_versions where local_path is not null')]; print(len(rows),'rows;',sum(1 for r in rows if not pathlib.Path(r).is_file()),'missing')"
```

Nothing below is worth building until this returns `0 missing`.

---

## 3. Deliberately left out of the MVP

Not bugs — scope. Roughly in the order they hurt:

1. **Version suffixes are still in titles.** Rows read *"Prudential Regulations for SME
   Financing (Updated till July 16, 2026)"*. The suffix is state, not name (plan §1.1) —
   strip it into the status line.
2. **No type filter or facet counts.** `getLawTypes()` is written and unused.
3. **No holdings bar.** Containers say "26 parts" but not how many we actually hold.
4. **Search results are flat**, not nested under their container, and the reader does not
   highlight matches.
5. **No Text view.** PDF only, so no in-document search, no copying from scans.
6. **No section navigation inside a single long PDF** — needs a label→page map.
7. **No delisted filter, no "recently changed" view, no mixed search, no "regulations
   cited" on circulars.** These are plan phases F5–F7.
8. **Cited-by is capped at 8 with no "show all"** — fine for most, wrong for the Banking
   Companies Ordinance's 314.
9. **No empty-corpus state**, no retry affordance on a failed load.
10. **Nothing is responsive below ~900px** and the library width is fixed at 300px.

---

## 4. Order to build the rest

### Step 0 — populate the archive
§2 above. Everything else is unverifiable until the viewer has bytes to render.

### Step 1 — make the rows read correctly (half a day)
Strip version suffixes from titles and move them to the status line; add the type filter
using `getLawTypes()`; add the holdings bar to container rows. Pure frontend, no API work.
This is the largest legibility gain per hour on the list.

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

## 5. Open items

- **Two figures in the plan disagree.** §1.4 breaks the 21 no-content documents into
  14 stubs / 2 external / 5 dead links; §4.3 refers to "9 dead Reporting Guidelines parts".
  Reconcile before any UI renders either number.
- **The two pakistancode.gov.pk statutes** — including the Banking Companies Ordinance,
  the most-cited document in the corpus at 314 links — cannot be opened at all. If
  read-first is the goal, capturing them is the highest-value backend item after the
  archive is repopulated.
- **`/api/laws/{id}/file` ignores a bad `version_id`** and falls through to a 404 that
  reads the same as "no file". Worth distinguishing once the archive is back.
- **The Python test suite does not run here** — `pytest` dies during collection on a
  chromadb `PanicException` (`range start index 10 out of range for slice of length 9`),
  with or without this branch's changes. Unrelated to the laws work, but it means nothing
  backend-side is currently test-covered on this machine.
