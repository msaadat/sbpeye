# UI redesign — "the register"

A visual and structural redesign of the SPA, worked out from the 2026-10-02 design review and
the mockups on the design canvas
[SBPEye Redesign Concepts](https://claude.ai/artifact/6nuUsRHyj6mES3EgBYQt63) (private until
shared from its Share menu). The canvas holds eight artboards: Today, Circulars search,
Circular reader, Ask (dark), Laws, two phone screens and a Foundations sheet. Every figure,
title and passage on them is real corpus data, so the mockups double as acceptance targets.

The idea in one line: **SBPEye reads like a regulatory register** — the document leads, its
standing is always in view, and every claim carries a receipt.

1. **The document leads.** Circulars and laws are set as reading text (serif, ~18px, ~68
   characters a line). Navigation steps back to an icon rail while reading.
2. **Standing is never hidden.** Every circular says whether it is in force, amended,
   superseded or cancelled, and what to read instead.
3. **Lineage before graphs.** An amendment chain reads top to bottom by date with plain verbs;
   the node graph becomes an option.
4. **Answers carry receipts.** Every figure in a chat answer links to the quoted passage it
   came from, and each source shows its own standing.
5. **One shell.** One labelled sidebar, one search box across circulars, laws and questions,
   and the six utility icons folded into an Account menu.

Each phase is independently shippable. Within a phase, items land in table order unless noted.

**Status:** Foundations FN1–FN8 landed (2026-10-03); every screen passes the contrast sweep in
both themes and at 375px. Everything else open.

---

## 0. Tracker

IDs are two-letter phase codes, because `F1`–`F7` already belong to `LAWS_FRONTEND_PLAN.md` and
`P`, `R`, `C`, `D` to the performance, chat and defect documents.

| # | Item | Where | Effort | State |
|---|---|---|---|---|
| **FN1** | Type families: IBM Plex Sans / Source Serif 4 / IBM Plex Mono, self-hosted | `main.ts`, `index.html`, `styles.css` | S | ☑ landed |
| **FN2** | Type scale 12 · 13 · 14 · 15 · 18 · 24 · 34, floor 12px | `styles.css` tokens | S | ☑ landed |
| **FN3** | Colour palette, flat ground, PrimeVue surface ramp, brand mark | `styles.css`, `main.ts`, `auth_routes.py` | S | ☑ landed |
| **FN4** | Standing system: four states, one helper, one chip | `lib/circularStatus.ts`, `styles.css` | S | ☑ landed |
| **FN5** | Relationship verb colours | `styles.css`, `CircularGraph.vue`, `RelationshipGroups.vue` | XS | ☑ landed |
| **FN6** | References and counts in mono | `styles.css` | XS | ☑ landed |
| **FN7** | Serif on document surfaces (titles, letter body, page headings) | `styles.css` | S | ☑ landed |
| **FN8** | Show all-caps SBP titles in title case | `lib/displayTitle.ts` | S | ☑ landed |
| **SH1** | Labelled sidebar with counts and a workspace section | `App.vue` | M | ☐ |
| **SH2** | Account menu: sync, AI key, news, about, theme, sign out | `App.vue` | S | ☐ |
| **SH3** | One search box: circulars, laws, or ask (`/` to focus) | `App.vue`, router | M | ☐ |
| **SH4** | Reading mode: sidebar collapses to an icon rail | `App.vue`, `CircularsView.vue`, `LawsView.vue` | S | ☐ |
| **SH5** | One page-header pattern for Values, Settings, EcoData, Admin | views | S | ☐ |
| **SR1** | Facet rail: standing, department, issued (with per-year bars) | `CircularsView.vue` | M | ☐ |
| **SR2** | Preview pane: standing, changed-by, changes, actions | `CircularsView.vue` | M | ☐ |
| **SR3** | Superseded hidden by default, one row to show them | `CircularsView.vue`, search API | S | ☐ |
| **SR4** | Corpus tabs: Circulars · Laws · Ask about the query | `CircularsView.vue` | S | ☐ |
| **RD1** | Standing banner with "Read consolidated version" | `CircularDetailPane.vue` | S | ☐ |
| **RD2** | Document header and reading column | `CircularDetailPane.vue` | S | ☐ |
| **RD3** | Labelled action bar (Pin, Ask about this, Original PDF, More) | `CircularDetailPane.vue` | S | ☐ |
| **RD4** | Margin change notes from consolidation requirements | `CircularDetailPane.vue`, `ConsolidatedView.vue` | L | ☐ |
| **RD5** | Inline references as text links, not pills | `CircularDetailPane.vue`, `styles.css` | XS | ☐ |
| **LN1** | Lineage tab: vertical timeline, oldest first | new `LineageTimeline.vue` | M | ☐ |
| **LN2** | Graph becomes "View as graph" from the timeline | `CircularDetailPane.vue` | XS | ☐ |
| **AK1** | Sources panel: quoted passage, standing, "open at passage" | `ChatView.vue` | M | ☐ |
| **AK2** | Numbered citation chips in the answer | `ChatView.vue`, answer renderer | M | ☐ |
| **AK3** | Key-figure tiles when an answer is a set of values | answer renderer | M | ☐ |
| **AK4** | Composer scope chip (circulars and laws / attached) | `ChatView.vue` | S | ☐ |
| **LW1** | Text view first, PDF as a toggle | `LawsView.vue` | M | ☐ |
| **LW2** | Amendment footnotes as inline provenance markers | `LawsView.vue`, law text parser | L | ☐ |
| **LW3** | Editions panel and contents outline | `LawsView.vue` | M | ☐ |
| **TD1** | `/today` home: follows, latest, standing, releases | new `TodayView.vue`, router | M | ☐ |
| **TD2** | "Changes to what you follow" from workspace pins | API + `TodayView.vue` | M | ☐ |
| **MB1** | Phone reader: inline change notes, segmented Letter · Lineage · Requires | `CircularDetailPane.vue` | S | ☐ |
| **MB2** | Phone search: result cards, filter chips | `CircularsView.vue` | S | ☐ |
| **CL1** | Refresh `frontend/public/about.html` screenshots | `about.html` | S | ☐ |
| **CL2** | Record the conventions in `AGENTS.md` | `AGENTS.md` | XS | ☐ |

**Order.** Foundations first: they restyle every screen without moving anything, so each later
phase only changes layout. Then Reader (RD1–RD3, RD5) and Lineage, which carry the core idea
and need no new data. Then Search and Shell. Ask, Laws and Today need new data paths and come
last. RD4, LW2 and TD2 are the three large items; each is optional for its phase.

---

## 1. How each item is verified

UI work runs against a copy of the corpus, never the real data root:

```bash
bash scripts/demo/prepare.sh <scratch>/dataroot
```

then the `sbpeye-scratch` entry in `.claude/launch.json` (port 8123) with `SBPEYE_DATA_DIR`
pointing at it. A rebuilt SPA needs a reload: since `c37af94`, `index.html` is served
`no-cache`, but a browser holding a copy from before that may keep it until it expires.

Every item is checked in light, dark and at 375px, with no horizontal overflow, and passes a
contrast sweep (WCAG AA: 4.5:1 for text, 3:1 for large text and meaningful icons). The sweep
computes against the composited background, which is what caught the `--sbp-green`-as-text
failures in the review.

---

## 2. Foundations (FN) ☑ landed

Restyle only: no layout moves, so every screen changes look without changing behaviour. The
Foundations artboard is the specification.

### FN1 — Type families ☑

| Role | Family | Token |
|---|---|---|
| Interface | IBM Plex Sans 400/500/600/700 | `--sbp-font-sans` |
| Document titles and reading text | Source Serif 4 (variable, optical sizes) | `--sbp-font-serif` |
| References, counts, dates in lists | IBM Plex Mono 400/500 | `--sbp-font-mono` |

Inter is the default face of half the web and reads as generic; Plex was drawn for
institutional and technical work, and a serif for the document text is what makes a circular
read as an official instrument rather than a UI string.

Self-hosted through `@fontsource` (`@fontsource/ibm-plex-sans`, `@fontsource/ibm-plex-mono`,
`@fontsource-variable/source-serif-4`, imported in `main.ts`), so the woff2 files are
content-hashed into `/spa/assets` and inherit P7's year-long cache. That also **closes P12**
(`PERFORMANCE_PLAN.md` §13): measured, no request to any `fonts.g*` origin from the SPA. What a
page fetches is only the faces it uses: Plex Sans Latin is 23 KB a weight, Plex Mono 15 KB, the
serif's Latin file 122 KB (130 KB more if a letter has italics). The server-rendered login page
cannot import the bundle and loads Plex Sans and the serif from Google Fonts.

Weights the CSS asked for but no longer exist as files — 650 and 800, from the Inter variable
font — are normalised to 600 and 700.

### FN2 — Type scale ☑

| Token | Was | Now | Used for |
|---|---|---|---|
| `--sbp-fs-eyebrow` | 11px | **12px** | uppercase labels, badges |
| `--sbp-fs-meta` | 12px | **13px** | counts, dates, snippets |
| `--sbp-fs-sm` | 13px | **14px** | list rows, chips, controls |
| `--sbp-fs-body` | 15px | 15px | prose, chat |
| `--sbp-fs-title` | 18px | 18px | pane titles |
| `--sbp-fs-reading` | — | **18px** | document text |
| `--sbp-fs-heading` | — | **24px** | document titles |
| `--sbp-fs-display` | — | **34px** | page titles |

Nothing in running UI goes below 12px — measured on every view; the two badges that had
literal 11px sizes (`AdminLibraryTab.vue` `.version-badge`, `LawsView.vue` `.edition-badge`) now
use the token. The sidebar nav item widened from 3.1rem to 3.4rem so "Circulars" fits at 12px.

### FN3 — Colour ☑

| Token | Light | Dark |
|---|---|---|
| `--sbp-bg` (ground) | `#f3f5f2` | `#0e1411` |
| `--sbp-surface` | `#ffffff` | `#141c18` |
| `--sbp-subtle` | `#eef1ed` | `#1a241f` |
| `--sbp-border` | `#dce2dd` | `#26322c` |
| `--sbp-text` | `#15201b` | `#e6eee9` |
| `--sbp-muted` | `#5d6a64` (5.2:1 on ground) | `#93a39b` (6.6:1) |
| `--sbp-green-text` | `#156f52` | `#6cc59f` (8.5:1) |
| `--sbp-gold-text` (new) | `#765709` (6.7:1) | `#e8bf63` (10:1) |

Green means SBP and action; gold means something changed and is never used as text in its fill
shade. The body's green gradient wash is gone — the ground is flat. The brand mark's
green-to-gold gradient (white text on its gold end was ~3:1) becomes solid green with a gold
base bar. The PrimeVue `surface` ramp in `main.ts` follows the table, and the login page uses
the same values.

Info severity went the same way as warn did in the review: Aura's stock blue was off-palette
and read 4.2:1 as dark-theme message text, so `blue` and `sky` now map to a slate ramp in the
"Clarifies" hue (two ramps, because Message reads step 500 as text and Button reads it as a fill
under white). Links inside a letter used `--sbp-green-strong`, which was under 2:1 on the dark
letter frame; they use `--sbp-green-text`.

### FN4 — Standing ☑

One helper, `lib/circularStatus.ts` (`circularStanding`, `standingLabel`, `standingColor`),
maps the stored status onto four states, and one chip (`.status-chip.status-<state>`) renders
them in the results list, detail header and law reader; graph nodes colour the label the same
way:

| State | Stored values | Chip | Means |
|---|---|---|---|
| In force | `active`, `indexed` | green | nothing later changes it |
| Amended | `amended` | gold | parts changed later — offer the consolidated version |
| Superseded | `superseded`, `replaced` | grey | replaced; kept for history |
| Cancelled | `cancelled`, `withdrawn` | red | withdrawn by SBP |

Labels are words, not the stored value: "In force", not "active". The change from the review
fix (`c37af94`) is that superseded is now grey rather than gold — gold is reserved for
"changed", so an amended and a superseded circular no longer look alike.

*Deviation from the mockup:* the results list does not show "In force". It is the default, and
a green chip on every row of the ~300px rail pushed the reference out of view; the detail header
always shows it. Revisit in SR1, which widens the list.

### FN5 — Relationship verbs ☑

| Verb | Colour | Token |
|---|---|---|
| Amends | gold ink | `--sbp-rel-amends` |
| Adds to | green ink | `--sbp-rel-adds-to` |
| Clarifies | slate | `--sbp-rel-clarifies` |
| Supersedes | grey | `--sbp-rel-supersedes` |
| Cancels | red | `--sbp-rel-cancels` |

Applied to graph edges and legend and to the relationship group labels in the detail panes
(`RelationshipGroups.vue` sets `data-rel` from the edge type). They follow the standing colours:
what *amends* is gold like what *is amended*, and the label is coloured by verb, not by
direction — "Amends" and "Amended by" are the same edge read from either end.

### FN6 — References and counts in mono ☑

The reference eyebrow above a document title, the references in relationship lists, the
relationship counts and the results count set in Plex Mono, in their stored case (the eyebrow
was CSS-uppercased). References are identifiers; mono makes "No. 03 of 2022" scan as one.

*Deviation:* references in the results rail stay in the sans with tabular figures. In mono at
~300px they were cut off before the circular number. SR1 widens the list and can move them.

### FN7 — Serif on document surfaces ☑

Circular and law titles (24px), the circular letter body (at `--sbp-fs-reading`, 1.7 line
height, capped at 46rem — the old 60rem of 15px sans ran past 110 characters a line), the laws
landing heading and page headings. Headings default to the interface face (`premium.css` had
pinned every `h1`–`h6` to Inter); only these set the serif. Chat answers stay in the interface
face until AK3.

The letter's margins were then trimmed: the section around the paper from 0.8/0.9rem to
0.5/0.625rem and the paper's own padding from 2rem to 1.25/1.5rem, so at 1440px the text sits
35px in from its column instead of 47px and the line runs 629px instead of 604px.

### FN8 — Title case for all-caps titles ☑

509 of 3,653 circular titles (14%) arrive entirely in capitals, and 568 of 3,788 circular and
law titles are under 15% lowercase. Decided 2026-10-03: show them in title case, at display
only — recorded as an exception in `AGENTS.md` § Known issues on the SBP site.

`frontend/src/lib/displayTitle.ts` converts a title only when it is shouting (under 15%
lowercase), which no ordinary mixed-case title approaches. Within it a word keeps its capitals
when it is an acronym — any vowel-less word (SBP, BPRD, PLS, MMTS) or one on a short list (IFRS,
FE, CIB, SME, PRISM…) — a Roman numeral, a dotted abbreviation (A.H., H.O.T.), or a code with an
ampersand or digit (R&D, DAP4). Words SBP already cased (DFIs, eCIB) are left as written, except
typos like "OPERATIONs". Small words go lowercase mid-title, "A" too unless it follows a label
("Schedule A"), and ordinals become "3rd". Checked over the whole corpus: every word it leaves
in capitals is an acronym or numeral.

Applied to every user-facing title — results, detail header, graph, chat context and research
steps, consolidated view, the laws tree, reader and cards. The admin console keeps the raw
title. Not touched: titles SBP typed in mixed case but cased oddly ("Implementation Of …
(Ifrs 9)"), and capitals inside a letter's own text.

---

## 3. Shell (SH)

Matches the Today and Search artboards. **SH1** replaces the 56px icon-and-tiny-label rail with
a 248px labelled sidebar: Today, Circulars (count), Laws & regulations (count), Ask, Regulatory
values, Economic data, then the active workspace's pins. **SH2** folds the six utility icons
(sync, AI key, news, about, theme, sign out) into one Account row at the bottom, with sync
status as a quiet "In step with sbp.org.pk · newest circular …" line above it. **SH3** is one
top search field across both corpora; Enter searches circulars, a tab switches to laws, and
"Ask about …" hands the query to chat. **SH4**: on a document route the sidebar collapses to a
72px icon rail. **SH5**: Values, Settings, EcoData and Admin get the same page header (eyebrow,
serif title, one-line description) and drop the centred card layout.

Done when: the six icons are gone from the rail; every view has the same shell; at 375px the
bottom tab bar is unchanged.

## 4. Search (SR)

**SR1** moves Department, Tag and year out of the dropdown row into a left facet rail, with the
year facet drawn as per-year bars of the current result set. **SR2** adds a preview pane:
standing, "changed by", "it changes", summary or a "Draft summary" empty state, and the actions.
**SR3** hides superseded results by default and says so in one row ("Superseded matches are
hidden by the Standing filter · Show them"). **SR4** puts Circulars · Laws · Ask about "…" tabs
above the results.

## 5. Reader (RD) and Lineage (LN)

The Circular reader artboard. **RD1**: an amended circular opens with a gold banner naming the
latest amending circular and a "Read consolidated version" button; superseded and cancelled get
the same banner in their colours, naming the replacement. **RD2**: reference eyebrow (mono),
serif title, a one-line meta row (department · date · addressees), and the letter at reading
size. **RD3**: the seven icon-only buttons become Pin · Ask about this · Original PDF and a More
menu (refresh, generate analysis, graph, consolidated). **RD5**: inline references render as
underlined text, not pills that break the line.

**RD4** is the large one: sentences a later circular changed are underlined in gold, with a
margin note saying what the text now says and which circular changed it. The data exists —
`circular_consolidations.requirements` carries `old_text`, `text`, `last_changed_by` — but
matching a requirement back to a sentence of the letter needs a fuzzy text anchor and must
degrade to "no note" rather than a wrong one. On a phone the notes sit inline under the
paragraph (MB1).

**LN1** replaces the relationship chips with a vertical timeline in the right panel: date,
verb, reference, one line of what changed, and a "You are reading" marker. **LN2** keeps the
graph behind "View as graph".

## 6. Ask (AK)

The Ask artboard. **AK1**: a sources panel beside the thread, one card per circular or law the
turn read, with the quoted passage, its standing, and "Open at this passage". When one source
amends another the card says so ("Amended by source 1. The answer reads the two together").
**AK2**: citations render as numbered chips that scroll the panel to their source. **AK3**:
when the answer is a set of values, the model returns them structured and they render as
tiles. **AK4**: the composer carries its scope as a chip.

The passage ledger already records which chunks each turn sent, so AK1 is a rendering problem;
AK3 needs a structured-output contract with the model.

## 7. Laws (LW)

The Laws artboard. **LW1** shows the extracted text first, with the PDF one toggle away.
**LW2** turns the Act's numbered amendment footnotes ("45 Substituted by Act No. VI of 2022")
into inline markers on the clause they annotate — a parser over `content_text`, verified on the
SBP Act before generalising. **LW3** adds the editions panel ("1 edition held · captured 11 Aug
2026 · no change detected since") and a contents outline built from the Act's own table of
contents.

## 8. Today (TD)

The Main artboard. **TD1** adds a `/today` route as the landing page: latest circulars, the
corpus standing bar (2,623 in force · 757 amended · 236 superseded · 37 cancelled on
2026-10-03), latest statistical releases, and an Ask box. **TD2** adds "Changes to what you
follow": new relationship edges whose target is a pinned circular, since the user's last visit.

## 9. Phone (MB)

The two phone artboards. **MB1**: on a phone the reader shows the standing banner, a segmented
Letter · Lineage · Requires control, and change notes inline. **MB2**: results become cards
with the standing chip on the reference line; filters become a chip row.

## 10. Close-out (CL)

**CL1**: the showcase page embeds live screenshots, which go stale with this work; refresh them
once SH and RD land (see the Playwright notes in the project memory). **CL2**: record the font,
token and standing conventions in `AGENTS.md` next to the existing frontend notes.

---

## 11. Not doing

| Idea | Why not |
|---|---|
| A component library swap | PrimeVue stays; the preset already carries the tokens (`main.ts`). |
| Per-user theming beyond light/dark | No request for it, and two themes already need two contrast sweeps. |
| Animating the lineage timeline | Motion adds nothing to reading a chain; `prefers-reduced-motion` is respected anyway. |
