# Plan: Exhaustive Regulatory Inventory Search

Implementation plan for a general-purpose search tool that evaluates every searchable
SBP circular and law/regulation against a user's semantic query, then returns the complete
set of documents that discuss it — each with a citable reference, a locator, and the exact
passage that caused it to match.

The capability is deliberately independent of chat. Its primary implementation is a
transport-neutral Python service that can be called by a CLI command, an HTTP endpoint,
the existing chat agent, or a future MCP server without changing the search semantics.

> **Revision note (2026-08-13).** This plan was substantially rewritten after the original
> design was measured against the live index. The first version made inclusion a pure
> cosine threshold over dense chunk embeddings and forbade any LLM involvement. Measurement
> showed that rule cannot reach usable recall at any threshold (section 4). Recall now
> belongs to a lexical layer, dense similarity is a supplement, and an LLM adjudicates
> relevance and extracts citable spans. Section 4 records the evidence so this decision is
> not silently re-litigated later.

---

## 1. Goal

Support broad research questions such as:

- "List all circulars that talk about AML."
- "Find all circulars and regulations concerning contact centers."
- "List documents in which responsibilities of Internal Audit are discussed."
- "Which regulatory documents discuss outsourcing of customer-support functions?"

This is an **inventory search**, not a conventional ranked search. The difference is the
completeness boundary:

- Conventional search asks: "What are the best 20 matches?"
- Inventory search asks: "For every document in scope, does it discuss this subject?"

The output is a reviewed inventory: one row per matching regulatory document, carrying the
document reference, a locator into the source, and the specific text that discusses the
query subject. It does not require the query to describe an obligation and does not
pre-classify the corpus into a fixed ontology.

### 1.1 The question being answered is *mention*, not *aboutness*

This distinction drives the whole architecture. "List all circulars that talk about AML"
is satisfied by a circular whose main subject is NPL reporting but which carries one
paragraph of AML instruction. It is a **mention/coverage** question.

Dense passage embeddings answer a different question — *what is this passage mostly
about?* — because a mean-pooled vector over a long span is dominated by the span's
dominant topic. Section 4 quantifies how far apart those two questions are in practice.

### 1.2 Precise completeness claim

The tool may claim:

> All documents in the successfully indexed corpus snapshot that were retrieved by the
> recorded term set or semantic band, and that the adjudication step judged to discuss the
> query subject.

It must not claim that every conceptually relevant document is guaranteed to be found. The
response must expose the resolved term set, the retrieval parameters, corpus coverage, and
unsearchable sources so a caller can assess that boundary.

---

## 2. Non-goals

The first implementation will not:

- decide whether a matched passage is legally binding;
- extract or normalize every obligation in advance;
- summarize away or silently remove matches;
- replace the existing low-latency hybrid search UI;
- search superseded law versions or reconstruct historical "as of" states;
- treat regulation manifests, external stubs, or unreadable files as searchable text;
- guarantee bit-identical output across runs (see section 5.4 — reproducibility was
  explicitly relaxed as a requirement; correctness of the returned inventory is what
  matters).

Optional classification, obligation extraction, and historical research can be layered on
top of the inventory later.

---

## 3. Current architecture and gaps

SBPEye already has most of the raw material:

- circular bodies and attachments are split with `prepare_reference_chunks()`;
- law/regulation current versions use the same chunker;
- chunks retain document IDs, source references, offsets, and page bounds;
- ChromaDB holds chunk embeddings for circulars, attachments, and current laws;
- circular and law FTS5 indexes cover lexical search;
- law chunks are discriminated with `kind="law"`; circular search is restricted to
  `doc_type in (circular, attachment)`;
- law version currency and circular-backed law deduplication already exist.

The existing `SearchEngine` cannot provide inventory semantics because each retrieval arm
has a 50-candidate cutoff. Asking it for a large page does not recover documents that were
never admitted to its candidate set.

### 3.1 Measured baseline (2026-08-13)

Read-only counts from the live database and Chroma collection:

| Corpus component | Total | Searchable text/index state |
|---|---:|---|
| Circulars | 3,649 | all have body text and an FTS row |
| Circular attachments | 1,465 | 1,324 have extracted text; 685 marked vectorized |
| Attachment extraction gaps | 141 | 98 errors; 43 unsupported |
| Regulation documents | 133 | 5 are circular-backed and must not be duplicated |
| Current regulation versions | 112 | 100 marked vectorized; 12 non-text |
| **Chunks in Chroma** | **14,734** | 768-dim, ~45 MB float32, 1.0 s to page out |
| **Logical documents in the vector index** | **3,727** | 3,639 circular + **88 law** |

Two gaps are already visible in production data and are not hypothetical:

1. **Attachment vectorization gap.** 1,324 attachments have text; 685 are marked
   vectorized. Treated as a data issue to be repaired, not a design constraint.
2. **Law index divergence.** SQLite says 100 current law versions are vectorized. Chroma
   holds chunks for only **88** distinct law documents. The `is_vectorized` boolean cannot
   distinguish "indexed" from "processed but produced zero chunks" — `vectorize_law_document`
   deliberately marks zero-chunk documents as vectorized — so a divergence of this kind is
   invisible to the application. Section 10 exists because of this.

---

## 4. Why threshold-only semantic inclusion was rejected

The original design made inclusion `max_chunk_cosine(query, document) >= threshold`, with
a target of ≥98 % recall for a calibrated `broad` profile. This was measured against the
live index using `BAAI/bge-base-en-v1.5` (the configured model) with lexical ground truth —
the set of circulars whose body or attachment text literally contains the term.

### 4.1 Recall versus review volume

Document-level recall, and the number of documents returned, across the full 3,727-document
index:

| Query | Ground truth | thr 0.50 | thr 0.55 | thr 0.60 | thr 0.65 |
|---|---:|---|---|---|---|
| "anti-money laundering" | 266 | **100 %** / 3,409 | 80 % / 1,687 | 28 % / 135 | 13 % / 37 |
| "AML" | 266 | 71 % / 2,057 | 23 % / 144 | 11 % / 34 | 3 % / 7 |
| "contact center requirements" | 41 | 93 % / 1,923 | 78 % / 296 | 27 % / 46 | 5 % / 2 |
| "call centre" | 41 | 73 % / 594 | 32 % / 41 | 12 % / 6 | 7 % / 3 |
| "internal audit responsibilities" | 160 | 96 % / 3,060 | 80 % / 1,315 | 53 % / 306 | 19 % / 41 |
| "internal audit function" | 160 | 88 % / 1,881 | 63 % / 436 | 36 % / 84 | 12 % / 21 |
| "outsourcing of customer support functions" | 73 | 88 % / 1,191 | 69 % / 155 | 33 % / 33 | 10 % / 8 |
| "outsourcing arrangements" | 73 | 95 % / 2,578 | 74 % / 636 | 29 % / 64 | 15 % / 13 |

There is no threshold that yields an inventory. At ≥95 % recall the result is 80-90 % of
the corpus; at any reviewable size recall is 10-30 %. The 98 % target was unreachable.

### 4.2 The ordering, not the cutoff, is the problem

Worst-ranked true positives: contact centre 3,153 / 3,727; AML 3,385; internal audit
3,364; outsourcing 2,978. Documents that literally contain the term sit in the bottom
decile of semantic similarity.

Concrete inversions:

- For "contact center requirements", two **Primary Dealer System** circulars score 0.6352,
  above the genuinely relevant *Consumer Grievances Handling Mechanism* at 0.6308 — while
  *FE Circular 11 of 2006, "Code Numbers for Call Centre Agents"* sits at rank **43**
  (0.6018).
- For "AML", *FSD Circular 01 of 2020, "Guidelines on Stress Testing"* (0.6458) outranks a
  dozen genuine AML circulars.

No threshold separates these. The score scale is also not query-stable: "AML" and
"anti-money laundering" are the same concept with distributions ~0.05 apart, producing a
4× difference in result count at the same threshold. A single number per
(embedding model, sensitivity) profile therefore cannot hold.

### 4.3 Cause

`prepare_reference_chunks` builds ~350-word chunks as `"{doc_label}. {ref}. {body}"`. A
mean-pooled 768-dimensional vector over 350 words encodes the passage's dominant topic. A
circular about NPL reporting containing one AML paragraph yields a vector that says "NPL
reporting". The fixed title and `"Page 12."` prefix on every chunk adds a constant,
correlated component on top of that.

### 4.4 What this implies

The FTS5 index already achieves 100 % recall on the mention question by construction, in
milliseconds — the ground-truth column above *is* lexical output. Recall therefore belongs
to the lexical layer. Dense similarity is retained for vocabulary mismatch, where it is
genuinely needed and where lexical search returns nothing.

---

## 5. Core search model

### 5.1 Three layers

```text
Layer 0  Term-set generation (LLM, strictly additive)
             query -> {verbatim terms} ∪ {generated terms}

Layer 1  Retrieval / recall (mechanical, no LLM in the loop)
             FTS5 boolean OR over the term set
           ∪ dense semantic band (exact full-corpus scan, no top-K)
           = candidate set

Layer 2  Adjudication (LLM)
             per candidate: does this passage discuss the query subject? yes/no + reason

Layer 3  Extraction (LLM)
             per included document: the verbatim span to cite
```

Recall is owned by layers 0 and 1 and is mechanical and inspectable. The LLM never
generates candidates and never widens the corpus; it only labels and extracts within what
layer 1 enumerated.

### 5.2 Logical result documents

The tool returns logical regulatory documents, not raw vector chunks.

#### Circular

One result document is one `Circular`. Its searchable sources are the circular HTML body
and every successfully extracted attachment belonging to it. A hit in an attachment maps
back to the parent circular while retaining the attachment ID, filename, page, source
reference, and matched passage as evidence.

#### Law/regulation

One result document is one searchable `RegDocument` using its current in-force
`RegDocumentVersion`.

- A flat document returns as itself.
- A searchable child/part returns as the child and includes its parent/container details.
- A container manifest has no readable text and is not evaluated.
- A `RegDocument` with `circular_id` resolves to that circular and is not returned as a
  second law result.
- External rows and metadata-only stubs are reported in coverage, not treated as negative
  matches.

Callers may group children by parent for presentation without changing the result set.

### 5.3 Why the LLM is in the design

A per-candidate filter cannot add recall — it only removes. The recall contributions are:

- **Term-set generation (layer 0)** directly expands the FTS arm. Highest leverage.
- **HyDE (section 6.3)** repairs the query/passage shape mismatch in the dense arm.

The adjudication filter is nonetheless what *buys* recall: a net wide enough for high
recall is only usable if something makes several hundred candidates reviewable. Recall and
the precision filter are coupled, which is the argument for including both.

### 5.4 Reproducibility is explicitly relaxed

Bit-identical reruns are **not** a requirement. Correctness of the returned inventory is.
Consequently the design does not pin the judge model into the snapshot identity and does
not key judgment caches for determinism. Caching exists for cost and latency only.

The response still records the term set, retrieval parameters, judge model, and prompt
version, so any given run can be explained after the fact.

---

## 6. Retrieval layer

### 6.1 Term-set generation (layer 0)

Invoked as a tool call with no human in the loop. Two rules make that safe:

- **Strictly additive.** The verbatim query terms are always members of the set. Generated
  terms may only union into it, never replace or prune. The LLM therefore cannot lower
  recall below the naive lexical baseline; the worst case is added noise, which layer 2
  removes.
- **Echoed in the response.** The resolved term set ships in the output as the audit
  trail. It cannot be approved in advance, but it is always visible after the fact.

The generator is prompted for regulatory-domain vocabulary: acronym expansions and
contractions (`AML` ↔ `anti-money laundering` ↔ `AML/CFT`), British/American spelling
variants (`centre`/`center`), SBP- and Pakistan-specific terminology, and closely
associated instruments (`CDD`, `STR`, `beneficial ownership` for AML).

A caller may supply explicit `alternate_queries`; these are merged into the term set and
marked as caller-supplied. A caller may also disable generation entirely, in which case
the term set is exactly the verbatim query.

### 6.2 Lexical arm

FTS5 boolean OR over the resolved term set, across both `circulars_fts` and the law FTS
table. This arm has no candidate cutoff — every document containing any term is a
candidate. It is the recall backbone.

Multi-word terms are matched as phrases. The existing `_fts_reference_tokens` digit
padding behaviour is retained.

### 6.3 Dense semantic arm

Retained to catch documents that discuss the subject without using any term in the set
(the vocabulary-mismatch case). Its role is supplementary, so it is expressed as a **band**
rather than a threshold:

- take the top `semantic_band` documents by max chunk cosine (default 300), or
- all documents above a low floor, whichever is smaller.

**HyDE.** The bare query is not embedded directly. The LLM writes a short hypothetical
regulatory passage answering the query, and *that* is embedded. This addresses the measured
defect in section 4.2 — a 3-word query and a 350-word regulatory passage occupy different
regions of the embedding space, which is why "AML" and "anti-money laundering" score ~0.05
apart. A synthetic passage matches passage-shaped text. The raw query is embedded as well
and the document score is the maximum of the two.

**Exact scan, no top-K.** Chroma's approximate `query(..., n_results=N)` is not used for
band construction. The service pages all chunk records and embeddings with
`collection.get()`, validates them against the ledger, builds a normalized matrix, and
computes all similarities in one batched operation. At 14,734 chunks / 45 MB / 1.0 s load
this is cheap and removes any ANN recall uncertainty. This constraint survives from the
original plan and must be preserved.

### 6.4 Candidate union and truncation

The candidate set is the union of both arms, deduplicated by logical document. Typical
volumes measured against the live corpus: 41-266 documents from the lexical arm before
term expansion, plus up to 300 from the band, giving roughly 300-700 candidates. Broad
queries can exceed this.

`max_candidates` bounds the union for cost control (default 1,200). When truncation is
necessary:

- lexical exact-term matches are **never** truncated before semantic-band-only candidates;
- within the band, lowest scores are dropped first;
- the truncation, its count, and the rule applied are reported in `coverage.warnings`.

Truncation must never be silent.

---

## 7. Adjudication layer (LLM)

For each candidate, the judge receives the query, the resolved term set, and the candidate's
best-matching passage or passages, and returns a binary verdict plus a one-line reason.

- Candidates are batched (10-20 per call) and calls are issued concurrently.
- Every candidate and every verdict is persisted. Excluded documents remain retrievable
  with the model's stated reason, so nothing is silently dropped.
- Judge failures (timeout, parse error) mark the candidate `undetermined` rather than
  excluded, and `undetermined` counts surface in coverage.
- A `skip_adjudication` request option returns the raw candidate union for debugging and
  for calibration runs.

Estimated cost per query at measured volumes: ~700 candidates × ~400 tokens ≈ 280 k input
tokens, or roughly 40-70 batched calls. A Haiku-class model is appropriate.

---

## 8. Extraction layer and output locators

### 8.1 Span extraction

A 120-350 word chunk is not a citation. For each included document the LLM returns the
verbatim span within the matched passage that discusses the query subject.

**Hard constraint:** the returned span must be verified as a literal substring of the
source chunk before it is emitted. If verification fails, fall back to the full chunk and
flag `extraction_verified: false`. For regulatory output, a paraphrase rendered as a
quotation is the one failure mode that cannot ship.

### 8.2 Locators are not always pages

`prepare_reference_chunks` assigns `Page N` only when `PAGE_MARKER_RE` matches, which is
the PDF-attachment path. Circular HTML bodies get `ref = "Chunk N"`, and `page_start` is
omitted from the Chroma metadata entirely by the conditional in that function. The whole
circular-body arm therefore has no page number to display.

The result schema must not assume a page. Each evidence item carries:

| Field | Meaning |
|---|---|
| `locator_kind` | `page`, `offset`, or `chunk` |
| `page_start` / `page_end` | populated when `locator_kind == "page"` |
| `source_start` / `source_end` | character offsets, already stored by the chunker |
| `source_ref` | the human-readable `ref` string |

Offsets are already persisted for every chunk, so HTML bodies can carry a usable anchor
without re-chunking work beyond section 12.

### 8.3 Target output shape

The user-facing deliverable is one row per matching document:

```text
circular / regulation reference | locator | extracted relevant text
```

The full response (section 9.2) is a superset of this.

---

## 9. Transport-neutral tool contract

Implement the capability as an application service with typed request/response models:

```python
class InventorySearchService:
    def search(
        self,
        request: InventorySearchRequest,
        db: Session,
    ) -> InventorySearchResponse: ...
```

The module must not import FastAPI, chat sessions, Click, or MCP libraries. It *may*
depend on an injected LLM client interface for layers 0, 2, and 3; that dependency is
expressed as a narrow protocol so tests can substitute a stub and so no adapter-specific
client leaks into the service.

Recommended module locations:

```text
src/sbpeye/inventory/
    __init__.py
    schemas.py       # request/response types and validation
    corpus.py        # logical-document mapping and coverage enumeration
    index.py         # full embedding snapshot loader/cache
    terms.py         # layer 0 term-set generation
    retrieval.py     # layer 1 lexical + dense band union
    adjudicate.py    # layer 2 relevance judgments
    extract.py       # layer 3 span extraction with substring verification
    service.py       # orchestration, filtering, ordering
```

### 9.1 Proposed request

```json
{
  "query": "documents that discuss responsibilities of Internal Audit",
  "alternate_queries": [],
  "generate_terms": true,
  "use_hyde": true,
  "sources": ["circulars", "laws"],
  "semantic_band": 300,
  "max_candidates": 1200,
  "skip_adjudication": false,
  "filters": {
    "departments": [],
    "start_year": null,
    "end_year": null,
    "circular_statuses": [],
    "law_types": [],
    "include_delisted_laws": false
  },
  "require_complete_coverage": false
}
```

Rules:

- `sources` defaults to both corpora.
- An empty `circular_statuses` means all circular statuses. Inventory research must not
  silently discard amended, superseded, or cancelled historical circulars.
- Laws use the current version only in MVP.
- Filters reduce the corpus before retrieval and are counted in the coverage response.

### 9.2 Proposed response

```json
{
  "query": "documents that discuss responsibilities of Internal Audit",
  "snapshot_id": "sha256:...",
  "retrieval_policy": {
    "term_set": ["internal audit", "audit committee", "internal control function"],
    "term_set_source": {"verbatim": [...], "generated": [...], "caller": [...]},
    "hyde_passage": "...",
    "embedding_model": "...",
    "embedding_fingerprint": "sha256:...",
    "chunker_version": "...",
    "semantic_band": 300,
    "judge_model": "...",
    "judge_prompt_version": "..."
  },
  "coverage": {
    "logical_documents_in_scope": 0,
    "candidates_lexical": 0,
    "candidates_semantic": 0,
    "candidates_union": 0,
    "candidates_truncated": 0,
    "adjudicated_included": 0,
    "adjudicated_excluded": 0,
    "adjudicated_undetermined": 0,
    "source_units_expected": 0,
    "source_units_indexed": 0,
    "excluded_by_design": {},
    "unsearchable": {},
    "stale_or_missing_index": 0,
    "is_complete": false,
    "warnings": []
  },
  "matched_documents": 0,
  "results": [
    {
      "result_kind": "circular",
      "document_id": "...",
      "title": "...",
      "reference": "...",
      "department": "...",
      "date": "YYYY-MM-DD",
      "status": "active",
      "matched_via": ["lexical", "semantic"],
      "matched_terms": ["internal audit"],
      "semantic_score": 0.0,
      "judge_reason": "...",
      "evidence": [
        {
          "source_kind": "attachment",
          "source_id": "...",
          "source_label": "framework.pdf",
          "locator_kind": "page",
          "page_start": 12,
          "page_end": 12,
          "source_start": 4820,
          "source_end": 5190,
          "source_ref": "Page 12",
          "extracted_text": "...",
          "extraction_verified": true,
          "passage": "..."
        }
      ]
    }
  ],
  "excluded": [
    {"document_id": "...", "reference": "...", "matched_via": ["semantic"], "judge_reason": "..."}
  ]
}
```

Law results additionally include `law_type`, `version_id`, `version_label`, `parent_id`,
and `parent_title`.

Results sort by `matched_via` (lexical first), then semantic score descending, then
`result_kind`, then document ID.

### 9.3 No pagination in MVP

The original design specified a continuation cursor with snapshot pinning and a
`snapshot_changed` rejection path. That machinery guarded a query that completes in about
a second and returns at most a few hundred rows. It is removed.

The service returns the complete result set in one response. If a result set ever exceeds
a configured `max_results`, the response is truncated with an explicit `truncated: true`
flag and a warning — never a silent cut. Pagination can be reintroduced if a real consumer
needs it.

### 9.4 Future MCP surface

A future MCP server should expose the core operation approximately as
`search_regulatory_inventory`. Its input schema mirrors `InventorySearchRequest`; its
structured result mirrors `InventorySearchResponse`.

The MCP handler must contain no search logic. It opens a database session, invokes
`InventorySearchService.search()`, and serializes the result. Chat integration, if added,
registers the same contract as an AI tool rather than implementing another search path.

---

## 10. Index ledger and coverage

The current `Attachment.is_vectorized` and `RegDocumentVersion.is_vectorized` fields are
not sufficient — section 3.1 shows the divergence they already permit:

- circular bodies have no equivalent vectorization ledger;
- a boolean does not identify the embedding model or chunker version;
- it does not prove the expected number of chunks is present;
- it cannot distinguish empty text, unsupported input, extraction failure, indexing
  failure, or stale content.

Add a `semantic_index_sources` table with one row per physical searchable source:

| Field | Purpose |
|---|---|
| `source_kind` | `circular`, `attachment`, or `law_version` |
| `source_id` | circular, attachment, or version ID |
| `logical_kind` | `circular` or `law` |
| `logical_document_id` | parent circular or `RegDocument` ID |
| `version_id` | current law version where applicable |
| `content_hash` | hash of the exact extracted source text |
| `chunker_version` | invalidates old chunk layouts |
| `embedding_fingerprint` | provider/model/dimension/config identity |
| `expected_chunks` | number produced by the canonical chunker |
| `indexed_chunks` | number confirmed in the vector store |
| `status` | `indexed`, `empty`, `unsupported`, `extraction_error`, `index_error`, `stale` |
| `error` | diagnostic detail |
| `indexed_at` | audit timestamp |

Use a composite uniqueness constraint appropriate to source identity. A law version is
version-specific even though only the current version is indexed.

Every indexing path must update the ledger in the same operation boundary as its current
Chroma/FTS write. When text, the chunker, or the embedding fingerprint changes, mark the
entry stale until the replacement succeeds.

Note that the `status` values distinguish `empty` from `indexed`. That is precisely the
distinction `is_vectorized` collapses, and why the 100-vs-88 law gap in section 3.1 could
persist without the application noticing.

### 10.1 Snapshot identity

Compute the semantic corpus snapshot from the sorted set of active ledger records:

```text
source_kind + source_id + logical_document_id + version_id + content_hash
+ expected_chunks + embedding_fingerprint + chunker_version + status
```

Hashing this manifest produces `snapshot_id`. It changes whenever searchable content,
currency, extraction state, chunking, or embeddings change. Per section 5.4 the judge model
is **not** part of snapshot identity; it is recorded in `retrieval_policy` instead.

### 10.2 Fail closed on index inconsistency

Before retrieval, compare the ledger with Chroma metadata and the expected database scope.

- Extra/stale vector chunks are not scored.
- Missing chunks make the source a coverage gap.
- An embedding dimension or fingerprint mismatch aborts the search.
- A missing attachment does not make its circular body disappear, but it is reported as an
  unsearchable source under that circular.
- Manifests and circular-backed law rows are `excluded_by_design`, not errors.

`coverage.is_complete` is true only when every expected text-bearing source in scope has a
current, internally consistent index entry. Extraction errors and unsupported source files
make it false even if the parent document has other searchable text.

A source missing from the **vector** index is not necessarily missing from FTS. Coverage
must report the two arms separately, since a document can still be found lexically while
its embeddings are stale.

---

## 11. Embedding snapshot loader and cache

Create a `CorpusEmbeddingSnapshot` abstraction containing stable chunk IDs, a normalized
NumPy embedding matrix, chunk text and provenance metadata, logical-document mappings, the
ledger-derived `snapshot_id`, and the embedding fingerprint/dimension.

Load Chroma in bounded pages rather than assuming one unbounded `get()`. Filter and
validate records in application code so collection-specific query filters cannot silently
omit a corpus. Sort by chunk ID before building the matrix.

Cache the immutable snapshot in process by `snapshot_id`; a later call against the same
snapshot embeds only the query and reuses the matrix. Invalidate and rebuild when the
ledger snapshot changes. Guard cache replacement with a lock, but let concurrent readers
share a completed snapshot.

Measured today: 14,734 chunks load in 1.0 s at 45 MB. After the re-chunking in section 12
expect roughly 55-70 k chunks at 170-215 MB float32 — still comfortably in process. Store
as float16 if that becomes a concern; batch scoring while maintaining each document's
maximum is an equivalent implementation detail.

---

## 12. Chunking and embedding configuration

### 12.1 Measured effect of finer chunks

Comparison on a 191-document subset (41 documents literally containing `call cent*`, 150
distractors), current 350-word + title prefix versus 100-word with the prefix removed,
same `bge-base-en-v1.5` model, document-level recall at K documents returned:

| Query | Config | R@25 | R@50 | R@100 | R@150 |
|---|---|---|---|---|---|
| "call centre" | 350 w + prefix | 49 % | 73 % | 90 % | 98 % |
| | **100 w, no prefix** | **56 %** | **90 %** | **98 %** | **100 %** |
| "call center agent handling customer queries" | 350 w + prefix | 54 % | 83 % | 93 % | 98 % |
| | **100 w, no prefix** | 54 % | **95 %** | **100 %** | **100 %** |
| "contact center requirements" | 350 w + prefix | 61 % | 80 % | 93 % | 95 % |
| | **100 w, no prefix** | 56 % | 80 % | **95 %** | **100 %** |

Honest reading: finer chunks help materially in the **tail**, which is exactly what
inventory search needs, but do not fix the head of the ranking — R@10 and R@25 are flat.
This is an improvement to the dense arm, not a substitute for the lexical arm.

### 12.2 Chunking changes

- **Chunk at ~120-150 words with ~30 words overlap.** 100 tested well; slightly larger
  keeps enough context for layer 3 to quote sensibly.
- **Remove the `"{doc_label}. {ref}. "` prefix from the embedded text.** Keep both in
  metadata. The prefix contributes a constant, correlated component to every chunk of a
  document and is a likely cause of inversions such as the Primary Dealer result in
  section 4.2.
- Preserve `source_start` / `source_end` offsets and page bounds exactly as today — section
  8.2 depends on them.
- Bump `chunker_version` so the ledger invalidates every existing entry.

### 12.3 Embedding model

Current: `BAAI/bge-base-en-v1.5`, 768-dim, via fastembed 0.8.0.

- **Dense upgrade is secondary priority.** `mixedbread-ai/mxbai-embed-large-v1` or
  `snowflake/snowflake-arctic-embed-l` (both 1024-dim, both available in the installed
  fastembed) over bge-base. Expect a modest gain; this is not where the recall problem
  lives.
- **Integration risk if the model changes.** `embed_queries` and `embed_documents`
  currently return *identical* vectors for bge-base in this build (measured cosine
  1.0000). E5, Nomic, and Arctic **require** asymmetric `query:` / `passage:` prefixes. If
  the model is swapped without making that split actually apply the right prefix, recall
  degrades silently with no error. Any model change must be accompanied by a test
  asserting query and document encodings differ where the model requires it.
- **Sparse / late-interaction is the more promising lever, deferred to Phase 5.**
  fastembed 0.8.0 has `prithivida/Splade_PP_en_v1`, `Qdrant/bm42-all-minilm-l6-v2-attentions`,
  and `Qdrant/minicoil-v1` available, plus late-interaction
  (`answerdotai/answerai-colbert-small-v1`, `jinaai/jina-colbert-v2`). SPLADE performs
  *learned* term expansion — connecting "call centre" to "helpline" and "contact centre"
  in the lexical space itself, which is the mention question stated directly. It is held
  back from the MVP because LLM term-set generation (section 6.1) addresses the same
  expansion need, so the two would be redundant. SPLADE is the designated fallback if term
  generation underperforms in Phase 4.

### 12.4 Re-index cost

Measured sustained throughput on this machine: **8.5 chunks/sec**. At 120-word granularity
the corpus grows to roughly 55-70 k chunks, so a full re-index is about **2 hours** on CPU;
a 1024-dim model is 2-3× that.

`onnxruntime` is installed **CPU-only** here — available providers are
`['AzureExecutionProvider', 'CPUExecutionProvider']` — although the machine has a GPU and
`FastEmbedBackend._select_providers()` already prefers CUDA/MIGraphX when present.
Installing a GPU execution provider would cut re-indexing to minutes and make chunking and
model choices cheap to iterate on. Worth doing before Phase 4 calibration.

---

## 13. Error policy

The retrieval layers must be usable without any generative AI configuration —
`generate_terms: false`, `use_hyde: false`, `skip_adjudication: true` yields a pure
mechanical lexical+dense union. This keeps layer 1 independently testable and gives a
fallback when the LLM is unavailable.

Expected errors:

- `invalid_query`
- `embedding_fingerprint_mismatch`
- `semantic_index_incomplete` when strict coverage is requested
- `vector_store_unavailable`
- `llm_unavailable` — only raised when an LLM layer was requested and failed hard

`require_complete_coverage`:

- `false` (default): return results plus explicit coverage warnings;
- `true`: fail before returning an inventory if any in-scope searchable source is stale,
  missing, or failed.

Do not fall back silently to ordinary top-K search. Do not silently drop LLM layers on
failure — degrade explicitly and record it in `coverage.warnings`.

---

## 14. Implementation phases

### Phase 1 — Canonical corpus and ledger

1. Add the `semantic_index_sources` model and automatic migration/table creation.
2. Centralize source enumeration for circular bodies, attachments, and current law versions.
3. Define the embedding fingerprint and chunker version.
4. Update circular, attachment, and law vectorization paths to write ledger records.
5. Add a backfill/audit command that reconciles SQLite, Chroma, and the ledger.
6. Repair the attachment vectorization gap, or explain every failure in the ledger.

```bash
sbpeye inventory index --audit
sbpeye inventory index --repair
sbpeye inventory status
```

The repair command must use the normal paired indexing functions and must never mutate
archived source files.

**Deliverable:** the application can prove which source units are searchable and why the
remainder are not.

### Phase 2 — Re-chunk and re-index

1. Implement the section 12.2 chunking changes behind the bumped `chunker_version`.
2. Re-index the full corpus; verify ledger counts.
3. Re-run the section 4.1 and 12.1 measurements against the new index and record them here
   so the effect of the change is on file.

**Deliverable:** the dense arm operates on mention-sized chunks with clean text.

### Phase 3 — Retrieval service (layers 0-1)

1. Implement typed schemas and request validation.
2. Implement the paged Chroma snapshot loader and matrix cache.
3. Implement the FTS boolean-OR arm over a term set.
4. Implement exact dense band scoring with no candidate cutoff, with HyDE.
5. Implement term-set generation with the strictly-additive guarantee.
6. Implement union, deduplication, filters, and the truncation policy.
7. Produce coverage and retrieval-policy metadata.

**Deliverable:** a pure Python call returns the complete candidate union with provenance,
runnable with the LLM layers disabled.

### Phase 4 — Adjudication, extraction, and CLI (layers 2-3)

1. Implement batched relevance judgment with persisted verdicts and reasons.
2. Implement span extraction with mandatory substring verification.
3. Implement locator resolution per section 8.2.
4. Add the CLI adapter:

```bash
sbpeye inventory search "AML" --source all
sbpeye inventory search "contact center requirements" --source circulars --format json
sbpeye inventory search "outsourcing" --no-llm      # layers 0/2/3 disabled
```

Human-readable output shows coverage first, then `reference | locator | extracted text`.
JSON output follows the future MCP schema so it doubles as contract testing.

**Deliverable:** the feature produces the intended output shape, exercisable independently
of chat.

### Phase 5 — Calibration and optional consumers

Calibration:

1. Build a reviewed relevance set for AML/AML-CFT, Internal Audit responsibilities,
   contact/call centers, outsourcing, consumer complaints, and quantitative prudential
   topics.
2. Include short circulars, long frameworks, attachment-only hits, acronyms, alternate
   spellings, law parts, and hard negatives containing incidental mentions.
3. Measure end-to-end recall and reviewer workload; tune `semantic_band`,
   `max_candidates`, and the term-generation prompt.
4. Evaluate SPLADE (section 12.3) against LLM term generation and adopt it if it adds
   recall the term set misses.

Optional consumers:

- MCP adapter exposing `search_regulatory_inventory`.
- Chat tool registration using the same service and response.
- SPA inventory/research page with filters, evidence, coverage, and export.
- Saved research runs and reviewer inclusion/exclusion decisions.
- Historical law-version/as-of search after historical embeddings are supported.

These consumers do not change inventory semantics.

---

## 15. Testing strategy

### 15.1 Unit tests

- Term-set generation is strictly additive: verbatim terms always survive, and a
  degenerate or empty LLM response cannot shrink the set.
- The lexical arm returns every document containing any term, with no cutoff.
- Dense band construction uses `collection.get()`, never `collection.query()`.
- Union deduplication maps attachment hits to parent circulars.
- A law child returns parent context but retains the child document ID.
- Circular-backed law rows do not produce duplicates.
- Manifests, external rows, and stubs have the correct coverage classification.
- Filters alter both evaluated scope and coverage counts.
- Extraction rejects a span that is not a literal substring and falls back to the chunk.
- `locator_kind` is `page` for PDF attachment hits and `offset`/`chunk` for HTML bodies.
- Truncation never drops a lexical exact-term match before a semantic-band-only candidate.

### 15.2 Exhaustiveness tests

- Create more than 50 matching documents and prove every one is returned.
- Place the only relevant chunk beyond the first Chroma `get()` page.
- Compare the vectorized batch result with a literal document-by-document loop.
- Test a document with many irrelevant chunks and one qualifying passage.
- A document found only lexically and a document found only semantically both appear, with
  correct `matched_via`.

### 15.3 Coverage and failure tests

- Missing, extra, duplicated, stale, and wrong-dimension vectors are detected.
- Extraction failure is reported separately from a semantic non-match.
- Vector-index gaps are reported separately from FTS gaps.
- Partial coverage returns warnings when permitted and fails when strict coverage is set.
- Changing content hash, chunker version, current law version, or embedding fingerprint
  changes `snapshot_id`; changing the judge model does not.
- Vector-store failure never falls back to normal hybrid search.
- LLM failure degrades explicitly: `undetermined` verdicts surface, layers are not
  silently skipped.
- With all LLM layers disabled the service still returns a candidate union.

### 15.4 Contract tests

- CLI JSON exactly validates against `InventorySearchResponse`.
- A mock MCP adapter can pass the request and response without transforming semantics.

### 15.5 Regression tests

- Existing normal circular/law hybrid search behavior remains unchanged.
- Circular vector queries retain their `doc_type` filter.
- Law vector queries retain their `kind="law"` filter.
- Only current law versions remain in the live inventory index.
- FTS and Chroma writes continue to travel together on existing mutation paths.

---

## 16. Acceptance criteria

The MVP is complete when:

1. The retrieval layers can be called without chat, FastAPI, or any generative AI client.
2. Every indexed chunk in the requested corpus is evaluated exactly once per query variant
   or is rejected by an explicit scope/ledger rule.
3. Candidate generation has no top-K or ANN cutoff, and the lexical arm has no cutoff at all.
4. Generated terms can only widen the term set, never narrow it, and the resolved set is
   returned in the response.
5. Every result contains at least one evidence item whose `extracted_text` is verified as a
   literal substring of stored source text, with a resolved locator.
6. Attachments roll up to circulars; law versions and hierarchy remain traceable;
   circular-backed laws are deduplicated.
7. The response identifies the corpus snapshot, embedding fingerprint, chunker version,
   term set, retrieval parameters, judge model, and coverage gaps.
8. Adjudicated exclusions are returned with reasons rather than silently dropped.
9. Strict coverage mode refuses to issue a complete inventory when any expected in-scope
   source is missing or stale.
10. On the reviewed evaluation set, end-to-end recall materially exceeds the section 4.1
    threshold-only baseline at a reviewer workload the caller accepts. The specific target
    is set in Phase 5 from measurement, not asserted in advance.

---

## 17. Design decisions to preserve

1. **Inventory search is a separate path.** Do not implement it by increasing
   `SearchEngine.CANDIDATE_COUNT` or requesting a larger page.
2. **Recall is mechanical.** The LLM generates terms and judges candidates, but the set of
   documents *considered* is produced by FTS and an exact dense scan. No LLM widens or
   defines the corpus.
3. **Term generation is strictly additive.** It can only ever add to the verbatim query
   terms. This is what makes an unsupervised tool call safe.
4. **All-chunk scoring is exact.** Chroma remains the persistent embedding store, but its
   top-K query API is not the candidate generator.
5. **A result is a regulatory document, not a chunk.** Chunks are evidence and map to
   circulars or law documents deterministically.
6. **Coverage is part of the result.** Unreadable or stale sources can never be silently
   treated as non-matches, and adjudicated exclusions are returned with reasons.
7. **Cited text is verified, never generated.** Any span presented as a quotation must be a
   literal substring of stored source text.
8. **The service owns semantics; adapters own transport.** CLI, HTTP, chat, and MCP must
   not fork the search algorithm.
9. **Current law currency rules remain authoritative.** Inventory indexing must use the
   same `is_current` selection and manifest exclusion as existing law search.
10. **Existing corpus boundaries remain enforced.** Shared Chroma storage never implies an
    unfiltered normal search, and circular-backed regulation rows are never indexed twice.
11. **Section 4 is a measurement, not an opinion.** Any future proposal to return to
    threshold-only semantic inclusion must first reproduce those numbers and show what
    changed.

This design provides the mental model required for audit research: expand the query into an
explicit term set, retrieve exhaustively by term and by meaning, have a model judge each
candidate and quote the text that made it relevant, and disclose everything the system
could not search.
