# Plan: Exhaustive Regulatory Inventory Search

Implementation plan for a general-purpose search tool that evaluates every searchable
SBP circular and law/regulation against a user's semantic query, then returns the complete
set of documents that meet an explicit match rule.

The capability is deliberately independent of chat. Its primary implementation is a
transport-neutral Python service that can be called by a CLI command, an HTTP endpoint,
the existing chat agent, or a future MCP server without changing the search semantics.

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
- Inventory search asks: "For every document in scope, does at least one passage meet the
  configured semantic relevance threshold?"

The output is a candidate inventory with supporting passages. It does not require the
query to describe an obligation, does not pre-classify the corpus into a fixed ontology,
and does not require a generative model to decide which documents are included.

### 1.1 Precise completeness claim

The tool may claim:

> All documents that met the recorded semantic match rule in the successfully indexed
> corpus snapshot.

It must not claim that every conceptually relevant document is guaranteed to exceed a
similarity threshold. The response must expose the threshold, embedding model, corpus
coverage, and unsearchable sources so a caller can assess that boundary.

---

## 2. Non-goals

The first implementation will not:

- decide whether a matched passage is legally binding;
- extract or normalize every obligation in advance;
- use an LLM to rewrite the query or accept/reject results;
- summarize away or silently remove matches;
- replace the existing low-latency hybrid search UI;
- search superseded law versions or reconstruct historical "as of" states;
- treat regulation manifests, external stubs, or unreadable files as searchable text.

Optional classification, explanation, summarization, obligation extraction, and historical
research can be layered on top of the complete candidate inventory later.

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
never admitted to its candidate set. Chat also has no law search/detail tool and its tool
loop is model-directed, so it cannot prove that the complete corpus was evaluated.

### 3.1 Measured baseline (2026-08-13)

Read-only counts from the current database:

| Corpus component | Total | Searchable text/index state |
|---|---:|---|
| Circulars | 3,648 | all have body text and an FTS row |
| Circular attachments | 1,465 | 1,324 have extracted text; 685 marked vectorized |
| Attachment extraction gaps | 141 | 98 errors; 43 unsupported |
| Regulation documents | 133 | 5 are circular-backed and must not be duplicated |
| Current regulation versions | 112 | 100 readable non-manifest versions indexed |
| Current non-text law records | 12 | 7 manifests; 5 unsupported XLS files |

The attachment vectorization gap means the inventory feature must include an index audit
and backfill before its result can be described as complete.

---

## 4. Core search model

### 4.1 Logical result documents

The tool returns logical regulatory documents, not raw vector chunks.

#### Circular

One result document is one `Circular`. Its searchable sources are:

- the circular HTML body; and
- every successfully extracted attachment belonging to it.

A hit in an attachment maps back to the parent circular, while retaining the attachment
ID, filename, page, source reference, and matched passage as evidence.

#### Law/regulation

One result document is one searchable `RegDocument` using its current in-force
`RegDocumentVersion`.

- A flat document returns as itself.
- A searchable child/part returns as the child and includes its parent/container details.
- A container manifest has no readable text and is not evaluated.
- A `RegDocument` with `circular_id` resolves to that circular and is not returned as a
  second law result.
- External rows and metadata-only stubs are reported in coverage, not treated as negative
  semantic matches.

Returning child documents independently preserves exact evidence and version identity.
Callers may group them by parent for presentation without changing the underlying result
set.

### 4.2 Match rule

For query `q`, embed `q` once. Normalize the query and every stored chunk embedding, then
calculate cosine similarity:

```text
chunk_score(q, c) = normalized_embedding(q) dot normalized_embedding(c)

document_score(q, d) = max(chunk_score(q, c) for c in chunks(d))

include(d) when document_score(q, d) >= inclusion_threshold
```

Using the maximum is intentional: one clearly relevant provision is enough to make a
long circular relevant. Averaging all chunks would penalize long documents whose other
sections concern unrelated matters.

The evidence returned for each result is the highest-scoring distinct chunks, normally
three. Evidence count affects only the response size, never inclusion.

### 4.3 Exact full-corpus scan

Inventory mode must not use Chroma's approximate `query(..., n_results=N)` operation for
candidate generation. Instead it must:

1. page through all stored chunk records and embeddings with `collection.get()`;
2. validate them against the index ledger described in section 6;
3. sort them by stable chunk ID;
4. construct a normalized embedding matrix;
5. calculate all similarities in a batched matrix operation; and
6. group qualifying chunks by logical document.

Conceptually this still performs:

```python
for document in every_document_in_scope:
    if any(semantic_match(query, chunk) for chunk in document.chunks):
        inventory.add(document)
```

The vectorized matrix calculation is the exact, efficient equivalent. It avoids thousands
of filtered Chroma calls and has no top-K truncation. At the current corpus size, an exact
scan is small enough to be the default design.

### 4.4 Query handling

MVP query handling is intentionally transparent:

- embed the exact user-supplied query;
- do not use an LLM to rewrite it;
- do not silently add synonyms;
- reject blank queries;
- record the normalized text and query-embedding hash in the response.

The request may optionally include explicit `alternate_queries`. Each alternate is
embedded independently and the document score is the maximum across the original query
and alternates. The result identifies which query produced the winning score. This lets a
caller deliberately search `AML`, `anti-money laundering`, and `AML/CFT` without hiding
expansion policy inside the service.

A future lexical union can be added as `match_mode="semantic_or_lexical"`, but it must be
explicit and return `matched_via`. It is not required for the first semantic inventory
tool.

### 4.5 Thresholds

Similarity has no universal model-independent threshold. The tool must support:

- `threshold`: an explicit cosine threshold; or
- `sensitivity`: a named, calibrated profile such as `broad`, `balanced`, or `strict`.

Profiles are keyed by embedding-model fingerprint and stored in configuration. The
response always includes the resolved numeric threshold. An uncalibrated embedding model
must not inherit thresholds from another model; either require an explicit threshold or
fail with a clear configuration error.

For audit research, `broad` should be the default after calibration. A caller can review
more false positives, but it cannot review relevant documents that were excluded below an
overly strict threshold.

No threshold values should be hard-coded before the evaluation in section 11.

---

## 5. Transport-neutral tool contract

Implement the capability as an application service with typed request/response models:

```python
class InventorySearchService:
    def search(
        self,
        request: InventorySearchRequest,
        db: Session,
    ) -> InventorySearchResponse: ...
```

The module must not import FastAPI, chat sessions, `AIClient`, Click, or MCP libraries.
Adapters validate their transport input and call this same service.

Recommended module locations:

```text
src/sbpeye/inventory/
    __init__.py
    schemas.py       # request/response types and validation
    corpus.py        # logical-document mapping and coverage enumeration
    index.py         # full embedding snapshot loader/cache
    service.py       # exact scoring, grouping, filtering, pagination
```

### 5.1 Proposed request

```json
{
  "query": "documents that discuss responsibilities of Internal Audit",
  "alternate_queries": [],
  "sources": ["circulars", "laws"],
  "sensitivity": "broad",
  "threshold": null,
  "filters": {
    "departments": [],
    "start_year": null,
    "end_year": null,
    "circular_statuses": [],
    "law_types": [],
    "include_delisted_laws": false
  },
  "evidence_per_result": 3,
  "page_size": 100,
  "cursor": null
}
```

Rules:

- `sources` defaults to both corpora.
- An empty `circular_statuses` means all circular statuses. Inventory research must not
  silently discard amended, superseded, or cancelled historical circulars.
- Laws use the current version only in MVP.
- Filters reduce the corpus before scoring and are counted in the coverage response.
- `evidence_per_result` is bounded, for example 1-5.
- `page_size` is bounded for tool/MCP payload safety; pagination never changes inclusion.
- An explicit `threshold` takes precedence over `sensitivity` and is echoed back.

### 5.2 Proposed response

```json
{
  "query": "documents that discuss responsibilities of Internal Audit",
  "snapshot_id": "sha256:...",
  "match_policy": {
    "algorithm": "exact_chunk_cosine_max",
    "embedding_model": "...",
    "embedding_fingerprint": "sha256:...",
    "chunker_version": "...",
    "threshold": 0.0,
    "alternate_queries": []
  },
  "coverage": {
    "logical_documents_in_scope": 0,
    "logical_documents_evaluated": 0,
    "source_units_expected": 0,
    "source_units_indexed": 0,
    "chunks_evaluated": 0,
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
      "score": 0.0,
      "winning_query": "...",
      "evidence": [
        {
          "source_kind": "attachment",
          "source_id": "...",
          "source_label": "framework.pdf",
          "page_start": 12,
          "page_end": 12,
          "source_ref": "...",
          "score": 0.0,
          "passage": "..."
        }
      ]
    }
  ],
  "next_cursor": null
}
```

Law results additionally include `law_type`, `version_id`, `version_label`, `parent_id`,
and `parent_title`. Circular evidence may come from either the body or an attachment.

Results sort by score descending, then `result_kind`, then stable document ID. The stable
tie-break makes repeated runs and pagination reproducible.

### 5.3 Pagination and snapshot safety

The continuation cursor must encode or server-side reference:

- request hash;
- corpus `snapshot_id`;
- threshold/model fingerprint; and
- the last `(score, result_kind, document_id)` sort key.

If the index changes between pages, reject the cursor with `snapshot_changed` rather than
mixing two corpora into one claimed inventory. Saved research runs can later persist all
matched IDs, but persistence is not required for the initial synchronous tool.

### 5.4 Future MCP surface

A future MCP server should expose the core operation approximately as:

```text
search_regulatory_inventory
```

Its input schema mirrors `InventorySearchRequest`; its structured result mirrors
`InventorySearchResponse`. If payload limits require it, a second
`get_regulatory_inventory_page` tool may consume the continuation cursor.

The MCP handler must contain no search logic. It opens a database session, invokes
`InventorySearchService.search()`, and serializes the result. Chat integration, if added,
registers the same contract as an AI tool rather than implementing another search path.

---

## 6. Index ledger and coverage

The current `Attachment.is_vectorized` and `RegDocumentVersion.is_vectorized` fields are
not sufficient:

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

### 6.1 Snapshot identity

Compute the semantic corpus snapshot from the sorted set of active ledger records:

```text
source_kind + source_id + logical_document_id + version_id + content_hash
+ expected_chunks + embedding_fingerprint + chunker_version + status
```

Hashing this manifest produces `snapshot_id`. It changes whenever searchable content,
currency, extraction state, chunking, or embeddings change.

### 6.2 Fail closed on index inconsistency

Before scoring, compare the ledger with Chroma metadata and the expected database scope.

- Extra/stale vector chunks are not scored.
- Missing chunks make the source a coverage gap.
- An embedding dimension or fingerprint mismatch aborts the search.
- A missing attachment does not make its circular body disappear, but it is reported as
  an unsearchable source under that circular.
- Manifests and circular-backed law rows are `excluded_by_design`, not errors.

`coverage.is_complete` is true only when every expected text-bearing source in scope has
a current, internally consistent index entry. Extraction errors and unsupported source
files make it false even if the parent document has other searchable text.

---

## 7. Embedding snapshot loader and cache

Create a `CorpusEmbeddingSnapshot` abstraction containing:

- stable chunk IDs;
- normalized NumPy embedding matrix;
- chunk text and provenance metadata;
- logical-document mappings;
- ledger-derived `snapshot_id`; and
- embedding fingerprint/dimension.

Load Chroma in bounded pages rather than assuming one unbounded `get()`. Filter and
validate records in application code so collection-specific query filters cannot silently
omit a corpus. Sort by chunk ID before building the matrix.

Cache the immutable snapshot in process by `snapshot_id`. A later call against the same
snapshot embeds only the query and reuses the matrix. Invalidate and rebuild the cache
when the ledger snapshot changes. Guard cache replacement with a lock, but let concurrent
readers share a completed snapshot.

If memory becomes a concern, score in fixed-size batches while maintaining each
document's maximum and evidence heap. The result is identical to a single large matrix;
batching is an implementation detail.

---

## 8. Tool behavior and error policy

The operation must be usable without chat or any generative AI configuration.

Expected errors include:

- `invalid_query`
- `invalid_threshold`
- `embedding_model_uncalibrated`
- `embedding_fingerprint_mismatch`
- `semantic_index_incomplete` when strict coverage is requested
- `vector_store_unavailable`
- `snapshot_changed`
- `invalid_cursor`

Support a request option such as `require_complete_coverage`:

- `false` (default): return results plus explicit coverage warnings;
- `true`: fail before returning an inventory if any in-scope searchable source is stale,
  missing, or failed.

Do not fall back silently to ordinary top-K search. Doing so would change the meaning of
the tool.

---

## 9. Implementation phases

### Phase 1 — Canonical corpus and ledger

1. Add the `semantic_index_sources` model and automatic migration/table creation.
2. Centralize source enumeration for circular bodies, attachments, and current law
   versions.
3. Define the embedding fingerprint and chunker version.
4. Update circular, attachment, and law vectorization paths to write ledger records.
5. Add a backfill/audit command that reconciles SQLite, Chroma, and the ledger.
6. Re-index the 639 text-bearing attachments currently not marked vectorized, or explain
   every failure in the ledger.

Suggested operational commands:

```bash
sbpeye inventory index --audit
sbpeye inventory index --repair
sbpeye inventory status
```

The repair command must use the normal paired indexing functions and must never mutate
archived source files.

**Deliverable:** the application can prove which source units are searchable and why the
remainder are not.

### Phase 2 — Exact inventory service

1. Implement typed schemas and request validation.
2. Implement the paged Chroma snapshot loader and matrix cache.
3. Implement exact cosine scoring with no candidate cutoff.
4. Map chunks to logical documents and retain top evidence passages.
5. Apply source and metadata filters before scoring.
6. Produce coverage, match-policy, and snapshot metadata.
7. Implement stable ordering and cursor validation.

**Deliverable:** a pure Python call returns a reproducible, exhaustive threshold-based
inventory without invoking chat or an LLM.

### Phase 3 — Independent executable adapter

Add a CLI adapter as the first non-chat client:

```bash
sbpeye inventory search "AML" --source all --sensitivity broad
sbpeye inventory search "contact center requirements" --source circulars \
  --threshold 0.62 --format json
```

Human-readable output shows coverage first. JSON output follows the future MCP schema so
it doubles as contract testing.

An HTTP endpoint may be added if the SPA needs it, but it must remain a thin adapter.

**Deliverable:** the feature can be exercised and automated independently of chat.

### Phase 4 — Calibration and evaluation

1. Build a reviewed relevance set for at least:
   - AML/AML-CFT;
   - Internal Audit responsibilities;
   - contact/call centers;
   - outsourcing;
   - consumer complaints;
   - quantitative prudential topics.
2. Include short circulars, long frameworks, attachment-only hits, acronyms, alternate
   spelling, law parts, and hard negatives containing incidental mentions.
3. Plot recall and review volume at threshold intervals for the configured embedding
   model.
4. Choose `broad`, `balanced`, and `strict` profiles from measured results.
5. Store profile values with the embedding fingerprint and evaluation-set version.

The principal acceptance target for `broad` is recall. A starting target is at least 98%
on the reviewed set; precision determines review workload but must not be optimized at the
expense of hidden misses.

**Deliverable:** named sensitivity settings have documented empirical meaning.

### Phase 5 — Optional consumers

- MCP adapter exposing `search_regulatory_inventory`.
- Chat tool registration using the same service and response.
- SPA inventory/research page with filters, evidence, coverage, and export.
- Saved research runs and reviewer inclusion/exclusion decisions.
- Optional explicit lexical-union mode.
- Historical law-version/as-of search after historical embeddings are supported.

These consumers do not change inventory inclusion semantics.

---

## 10. Testing strategy

### 10.1 Unit tests

- Cosine score and threshold equality are exact and repeatable.
- Maximum chunk score determines document inclusion.
- An attachment hit returns its parent circular and attachment evidence.
- A law child returns parent context but retains the child document ID.
- Circular-backed law rows do not produce duplicates.
- Manifests, external rows, and stubs have the correct coverage classification.
- Multiple query variants use the maximum score and report the winning query.
- Filters alter both evaluated scope and coverage counts.
- Stable tie-breaking produces repeatable pagination.
- Cursor/request/snapshot mismatches are rejected.

### 10.2 Exhaustiveness tests

- Create more than 50 matching documents and prove every above-threshold document is
  returned across pages.
- Place the only relevant chunk beyond the first Chroma `get()` page.
- Prove the implementation calls no top-K `collection.query()` path.
- Compare the vectorized batch result with a literal document-by-document loop.
- Test a document with many irrelevant chunks and one qualifying passage.

### 10.3 Coverage and failure tests

- Missing, extra, duplicated, stale, and wrong-dimension vectors are detected.
- Extraction failure is reported separately from a semantic non-match.
- Partial coverage returns warnings when permitted and fails when strict coverage is set.
- Changing content hash, chunker version, current law version, or embedding fingerprint
  changes `snapshot_id`.
- Vector-store failure never falls back to normal hybrid search.

### 10.4 Contract tests

- CLI JSON exactly validates against `InventorySearchResponse`.
- A mock MCP adapter can pass the request and response without transforming semantics.
- Large result sets paginate without loss or duplication.

### 10.5 Regression tests

- Existing normal circular/law hybrid search behavior remains unchanged.
- Circular vector queries retain their `doc_type` filter.
- Law vector queries retain their `kind="law"` filter.
- Only current law versions remain in the live inventory index.
- FTS and Chroma writes continue to travel together on existing mutation paths.

---

## 11. Acceptance criteria

The MVP is complete when:

1. The service can be called directly without chat, FastAPI, or a generative AI client.
2. Every indexed chunk in the requested corpus is evaluated exactly once per query variant
   or is rejected by an explicit scope/ledger rule.
3. Inclusion has no top-K or candidate-count cutoff.
4. Every result contains one or more exact stored evidence passages with source provenance.
5. Attachments roll up to circulars; law versions and hierarchy remain traceable;
   circular-backed laws are deduplicated.
6. The response identifies the corpus snapshot, embedding fingerprint, chunker version,
   threshold, algorithm, and coverage gaps.
7. Identical requests against an unchanged snapshot produce identical matched document
   IDs, scores, ordering, and evidence.
8. Pagination returns the same complete result set as an unpaginated internal run.
9. Strict coverage mode refuses to issue a complete inventory when any expected in-scope
   source is missing or stale.
10. The calibrated broad profile reaches the agreed recall target on the reviewed SBP
    evaluation set.

---

## 12. Design decisions to preserve

1. **Inventory search is a separate path.** Do not implement it by increasing
   `SearchEngine.CANDIDATE_COUNT` or requesting a larger page.
2. **No LLM controls inclusion.** Generative analysis may consume results later, but the
   recorded query, embeddings, threshold, and filters determine membership.
3. **All-chunk scoring is exact.** Chroma remains the persistent embedding store, but its
   top-K query API is not the inventory candidate generator.
4. **A result is a regulatory document, not a chunk.** Chunks are evidence and map to
   circulars or law documents deterministically.
5. **Coverage is part of the result.** Unreadable or stale sources can never be silently
   treated as non-matches.
6. **The service owns semantics; adapters own transport.** CLI, HTTP, chat, and MCP must
   not fork the search algorithm.
7. **Current law currency rules remain authoritative.** Inventory indexing must use the
   same `is_current` selection and manifest exclusion as existing law search.
8. **Existing corpus boundaries remain enforced.** Shared Chroma storage never implies an
   unfiltered normal search, and circular-backed regulation rows are never indexed twice.

This design provides the simple mental model required for audit research: define a query
and threshold, evaluate the whole searchable corpus, return every matching document with
the passages that caused it to match, and disclose anything the system could not search.
