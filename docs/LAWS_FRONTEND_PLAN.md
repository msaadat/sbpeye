# Plan: Presenting Laws & Regulations in the SBPEye UI

Frontend plan for the corpus built by `docs/LAWS_REGULATIONS_PLAN.md` (phases 1–6a, live).
Backend is done and stable; nothing here requires new scraping.

---

## 1. What we are presenting (measured 2026-08-11)

| | count |
|---|---|
| Documents | **133** — 75 top-level, 58 parts |
| Containers | 7 (FE Manual 26 parts, Reporting Guidelines 9, Reporting Guides 8, FIS 7, CPIS 4, MFB Licensing 3, Draft ATM 1) |
| Versions | 114 (112 current) |
| Type split | guideline 66, regulation 48, law 19 |
| Circular ↔ regulation links | 809, across 688 circulars and 58 documents |
| Documents with **no content** | 21 (14 stubs, 2 external, 5 dead SBP links) |
| Documents with **>1 version** | **2** |
| Documents with a version label | 6 |
| Summaries / tags | 0 — phase 7 has not run |

Five facts about this data drive every decision below.

**1.1 A law has no reference and no date.** The circular result card is built around
`reference · date` (`CircularResultContent.vue`). For a law both are empty: 37 of 75
top-level rows have no `listed_date` at all, and the dates that exist are placeholders
("30 December 1881"). Reusing that card would render a corpus of blank identity lines.

**1.2 Nearly half the corpus is parts.** 58 of 133 documents are chapters and appendices
whose titles are subject lines — "EXPORTS", "IMPORTS", "Authorized Dealers", "Preface".
Listed flat they are noise; they only mean something as *Chapter 12 of the Foreign
Exchange Manual*. A flat list is therefore wrong by default, and any search result for a
part must carry its container.

**1.3 The version timeline is the product, and it is almost empty today.** SBPEye exists
here because SBP replaces PDFs in place and keeps no history — but we have only been
watching since August 2026, so exactly **2 documents have more than one version**. The UI
must make the archive legible on day one *and* still read honestly when a document has a
single version. Designing a rich timeline that renders as one lonely dot for 131 of 133
documents is the main trap in this plan.

**1.4 One in six documents has nothing to show.** 21 documents have no version: rows that
resolve to a circular, laws hosted on pakistancode.gov.pk, and 5 whose SBP link is dead.
These are not errors and must not look like empty results — each has a different correct
destination.

**1.5 Link counts are wildly skewed.** The histogram runs 0 (75 documents) to **314**
(Banking Companies Ordinance). "Circulars referencing this" cannot be a dumped list.

---

## 2. What the API already provides

Built in phases 5–6a, all live:

- `GET /api/laws` — list/search; `q`, `doc_type`, `parent_id`, `top_level`, `include_delisted`, paging
- `GET /api/laws/types` — facet counts
- `GET /api/laws/{id}` — current version, full version timeline, parts, parent, linked circulars
- `GET /api/laws/{id}/versions/{vid}` — version detail incl. extracted text, `pending` state, archive reference
- `GET /api/laws/{id}/file` — serves the archived file **from our disk**
- `GET /api/circulars/search?source=circulars|laws|all&doc_type=` — every item carries `result_kind`

### 2.1 Gaps to close before UI work (small, backend)

1. ~~**The link graph is one-way in the UI.**~~ **Closed.** `/api/circulars/{id}` now
   carries a `regulations` array — the mirror of a law's `linked_circulars`, deduped by
   document and with parts grouped under their container. See `LAWS_FRONTEND_MVP.md` §4.
2. ~~**No "recently changed" ordering.**~~ **Closed.** `/api/laws?sort_by=captured` orders
   by the in-force version's `first_seen_at`, newest first, with the documents we hold
   nothing for sorted last. Note it reads as one flat tie until the watch period
   lengthens — every current version was captured in the same sync.
3. **Archived files cannot be previewed** — *half closed.* Laws no longer need this: the
   reader iframes `/api/laws/{id}/file`, which serves `local_path` from our disk (and
   since the MVP, with an `inline` disposition, so browsers render it instead of
   downloading it). But `PdfPreviewDialog.vue` still goes through `/api/pdf_preview?url=`
   and re-fetches from sbp.org.pk for **circulars** — the one thing the archive exists to
   avoid, and it fails outright for documents SBP has since broken. No longer blocks F3.

---

## 3. Design principles

1. **Do not dress a law as a circular.** Different identity (name, not reference),
   different time model (in force / superseded / pending, not a publication date),
   different structure (parts). It gets its own card and its own detail layout.
2. **The container is the document; the part is the unit of change.** Browse by container,
   read and cite by part. Never show a part without its container in the same breath.
3. **Show what is in force, then the history.** The default answer to "what does this
   regulation say" is the current version. History is one click away and never in the way.
4. **Never link to sbp.org.pk for content we hold.** The archive is the point.
5. **A document with no content still deserves an honest destination** — the circular it
   really is, the external site that hosts it, or a plain "SBP's link is broken".
6. **Badge, don't blend.** In a mixed result list `result_kind` must be visible at a
   glance, or users will read a regulation as a circular.

---

## 4. The screens

### 4.1 Regulations list — `/laws`

Reuses the shell users already know from `CircularsView`: a filter bar, a results list, a
resizable detail pane (`useResizablePane`), route-driven state.

```
┌ Regulations ─────────────────────────────────────────────────────────────────┐
│ [ search…                    ]  Type ▾ All (133)   ☐ Include parts           │
├──────────────────────────────┬───────────────────────────────────────────────┤
│ ▸ Foreign Exchange Manual    │                                               │
│   REGULATION · 26 parts      │            (detail pane)                      │
│ ─────────────────────────────│                                               │
│   Prudential Regulations for │                                               │
│   SME Financing              │                                               │
│   REGULATION · Updated till  │                                               │
│   July 16, 2026              │                                               │
│ ─────────────────────────────│                                               │
│   Banking Companies          │                                               │
│   Ordinance 1962             │                                               │
│   LAW · hosted externally ↗  │                                               │
└──────────────────────────────┴───────────────────────────────────────────────┘
```

**Row anatomy** (`LawResultContent.vue`, sibling of `CircularResultContent.vue`):

| Slot | Content | Why |
|---|---|---|
| Title | document title, version suffix stripped | The suffix is state, not name (§1.1) |
| Eyebrow | `LAW` / `REGULATION` / `GUIDELINE` tag | The only classification a law has |
| Status line | one of: `Updated till July 16, 2026` · `26 parts` · `hosted externally` · `is a circular` · `SBP link broken` | Covers §1.4 without a separate empty state |
| Snippet | search snippet, when searching | Same `v-html` treatment as circulars |
| Part context | `Chapter 12 · Foreign Exchange Manual` when the row is a part | §1.2 — never orphan a part |

**Defaults:** `top_level=true`, so the 58 parts stay out of the list until either the user
ticks "Include parts" or searches (a search for "export proceeds" *should* surface Chapter
12 directly — that is the best answer). Type filter is driven by `/api/laws/types`.

### 4.2 Document detail — `/laws/:id`

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Prudential Regulations for SME Financing              REGULATION             │
│ In force · Updated till July 16, 2026 · captured 11 Aug 2026   [ Open PDF ]  │
├────────────────────────────────────────────┬─────────────────────────────────┤
│                                            │ VERSIONS                        │
│   (in-force document: archived PDF         │  ● In force  Updated till Jul…  │
│    viewer, or extracted text, or           │              captured 11 Aug 26 │
│    inline HTML for Appendix III)           │                                 │
│                                            │ CITED BY                        │
│                                            │  14 circulars ▸                 │
│                                            │   SMEFD Cir. 09 of 2026         │
│                                            │   BPRD Cir. Ltr 24 of 2008      │
│                                            │   … show all                    │
└────────────────────────────────────────────┴─────────────────────────────────┘
```

Mirrors `CircularDetailPane`'s two-column split (source left, rail right) so the
interaction model carries over.

**Versions rail — the §1.3 problem.** With one version, render a single line, not a
timeline: *"In force since we first captured it, 11 Aug 2026. No changes seen since."*
That sentence is honest, explains what SBPEye is doing, and grows into a real timeline the
first time SBP swaps a PDF. With two or more, it becomes a dated list, newest first, each
row linking to `/api/laws/{id}/versions/{vid}`: label, capture date, file size, and for a
future-dated edition a `Not yet in force` tag with its effective date.

**Cited by — the §1.5 problem.** Collapsed to a count with the 5 most recent circulars
expanded; "show all" opens the full list. For the Banking Companies Ordinance this is 314
rows and must never render inline by default.

**No-content documents** replace the source column with the honest destination: a link
through to the circular (`circular_id` is set), an outbound link to pakistancode.gov.pk
(`is_external`), or a plain note that SBP's link is broken and we retry every sync.

### 4.3 Container detail — the Foreign Exchange Manual

A container has no text of its own; its version is a manifest. So the source column
becomes the parts table — which is exactly what the SBP page shows, minus the dead links.

```
│ Foreign Exchange Manual                                REGULATION            │
│ 26 parts · manifest captured 11 Aug 2026                                     │
├──────────────────────────────────────────────┬───────────────────────────────┤
│  Chapter 1   INTRODUCTORY                 ✓  │ VERSIONS                      │
│  Chapter 2   AUTHORIZED DEALERS           ✓  │  ● manifest, 11 Aug 2026      │
│  …                                           │                               │
│  Chapter 12  EXPORTS                      ✓  │ CITED BY                      │
│  Appendix III NOTIFICATIONS…  (web page)  ✓  │  40 circulars ▸               │
```

Each part row links to its own `/laws/:id`. A part detail shows a breadcrumb back to the
container. `has_content: false` renders as a muted "SBP link broken" marker rather than a
tick — this is where the 9 dead Reporting Guidelines parts live, and pretending they are
fine would be the wrong call.

### 4.4 Mixed search

`CircularsView` gains a source segment control — **Circulars · Regulations · All** — bound
to the existing `source` query param, and results render by `result_kind`. A visible tag on
each row is mandatory (§principle 6); the two card components already differ enough that
scanning stays easy.

### 4.5 Circulars gain a "Regulations cited" section

**Shipped.** `CircularDetailPane`'s rail carries a "Regulations cited" section, each entry
linking to `/laws/:id`, with parts grouped under their container in chapter order. This is
what makes the 809 links useful in the direction users actually read: circular first,
regulation second. See `LAWS_FRONTEND_MVP.md` §5 — including the gating bug it exposed,
which was hiding the section on the 94 circulars with no AI analysis.

---

## 5. Phases

Each ends with something usable; F1–F3 are the spine.

| Phase | Deliverable | Touches |
|---|---|---|
| **F0** | Close the three API gaps in §2.1 | `main.py`, `api/serializers.py` |
| **F1** | Typed API client for laws, no UI | `lib/api.ts` |
| **F2** | `/laws` list view + nav item + `LawResultContent` | `router`, `App.vue`, new `LawsView.vue`, new component |
| **F3** | Document detail pane: in-force viewer, versions rail, cited-by | new `LawDetailPane.vue` |
| **F4** | Container/parts presentation + breadcrumbs | `LawDetailPane.vue` |
| **F5** | Source toggle + badged mixed results | `CircularsView.vue`, `lib/api.ts` |
| ~~**F6**~~ ✅ | "Regulations cited" in circular detail | `CircularDetailPane.vue` |
| **F7** | "Recently changed" view (needs F0 gap 2) | `LawsView.vue` |

---

## 6. Non-goals (for now)

- Version **diffing** — needs ≥2 versions of something worth diffing (we have 2 documents)
  and the AI pass from backend phase 7.
- Summaries and tags on law rows — backend phase 7 has not run; the card leaves room.
- Workspaces / pinning for regulations — the workspace model is circular-shaped
  (`WorkspaceCircular`); extending it is its own plan.
- Chat over regulations — `chat_retrieval.py` only knows circulars.
- Editing, annotating, or exporting regulation sets.

## 7. Open questions

1. Should `/laws` be a **top-level nav item** (sixth icon in the sidebar) or a tab inside
   Circulars? Leaning top-level: it is a distinct corpus with its own browse model, and the
   sidebar has room.
2. Do we surface **delisted** documents at all? They are kept forever server-side.
   Suggestion: hidden by default, `include_delisted` behind a filter, shown with a
   "removed from SBP's listing on <date>" banner — that is a genuinely interesting fact.
3. For the 12 **scanned** current versions there is no text to show, only the PDF. Is the
   viewer enough, or do we want an explicit "no text layer" note? (No OCR is planned.)
