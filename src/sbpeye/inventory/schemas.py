"""Request and response types for inventory search.

Deliberately plain dataclasses: this contract is shared by the CLI, a future HTTP route,
and a future MCP tool, and none of them should have to agree on a validation library.
``to_dict`` produces the JSON shape documented in section 9.2 of the plan.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SourceName = Literal["circulars", "laws"]
LocatorKind = Literal["page", "offset", "chunk"]

DEFAULT_SEMANTIC_BAND = 300
DEFAULT_MAX_CANDIDATES = 1200
DEFAULT_EVIDENCE_PER_RESULT = 3


class InventoryError(Exception):
    """Base for expected, reportable failures. ``code`` is part of the contract."""

    code = "inventory_error"


class InvalidQuery(InventoryError):
    code = "invalid_query"


class EmbeddingFingerprintMismatch(InventoryError):
    code = "embedding_fingerprint_mismatch"


class SemanticIndexIncomplete(InventoryError):
    code = "semantic_index_incomplete"


class VectorStoreUnavailable(InventoryError):
    code = "vector_store_unavailable"


class LLMUnavailable(InventoryError):
    code = "llm_unavailable"


@dataclass
class InventoryFilters:
    departments: list[str] = field(default_factory=list)
    start_year: int | None = None
    end_year: int | None = None
    circular_statuses: list[str] = field(default_factory=list)
    law_types: list[str] = field(default_factory=list)
    include_delisted_laws: bool = False


@dataclass
class InventorySearchRequest:
    query: str
    alternate_queries: list[str] = field(default_factory=list)
    generate_terms: bool = True
    use_hyde: bool = True
    sources: list[SourceName] = field(default_factory=lambda: ["circulars", "laws"])
    semantic_band: int = DEFAULT_SEMANTIC_BAND
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    skip_adjudication: bool = False
    extract_spans: bool = True
    evidence_per_result: int = DEFAULT_EVIDENCE_PER_RESULT
    filters: InventoryFilters = field(default_factory=InventoryFilters)
    require_complete_coverage: bool = False

    def validate(self) -> None:
        if not (self.query or "").strip():
            raise InvalidQuery("query must not be blank")
        if not self.sources:
            raise InvalidQuery("at least one source corpus is required")
        unknown = set(self.sources) - {"circulars", "laws"}
        if unknown:
            raise InvalidQuery(f"unknown source(s): {sorted(unknown)}")
        if self.semantic_band < 0:
            raise InvalidQuery("semantic_band must not be negative")
        if self.max_candidates < 1:
            raise InvalidQuery("max_candidates must be at least 1")
        if not 1 <= self.evidence_per_result <= 5:
            raise InvalidQuery("evidence_per_result must be between 1 and 5")

    @property
    def include_circulars(self) -> bool:
        return "circulars" in self.sources

    @property
    def include_laws(self) -> bool:
        return "laws" in self.sources


@dataclass
class Evidence:
    source_kind: str
    source_id: str
    source_label: str
    locator_kind: LocatorKind
    source_ref: str
    score: float
    passage: str
    extracted_text: str = ""
    extraction_verified: bool = False
    page_start: int | None = None
    page_end: int | None = None
    source_start: int | None = None
    source_end: int | None = None


@dataclass
class InventoryResult:
    result_kind: str
    document_id: str
    title: str
    reference: str = ""
    department: str = ""
    date: str | None = None
    status: str = ""
    matched_via: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    semantic_score: float = 0.0
    judge_reason: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    # Law-only fields; omitted from the payload when empty.
    law_type: str = ""
    version_id: str = ""
    version_label: str = ""
    parent_id: str = ""
    parent_title: str = ""


@dataclass
class ExcludedResult:
    document_id: str
    result_kind: str
    reference: str
    title: str
    matched_via: list[str] = field(default_factory=list)
    judge_reason: str = ""


@dataclass
class Coverage:
    logical_documents_in_scope: int = 0
    candidates_lexical: int = 0
    candidates_semantic: int = 0
    candidates_union: int = 0
    candidates_truncated: int = 0
    adjudicated_included: int = 0
    adjudicated_excluded: int = 0
    adjudicated_undetermined: int = 0
    source_units_expected: int = 0
    source_units_indexed: int = 0
    excluded_by_design: dict[str, int] = field(default_factory=dict)
    unsearchable: dict[str, int] = field(default_factory=dict)
    stale_or_missing_index: int = 0
    is_complete: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class RetrievalPolicy:
    term_set: list[str] = field(default_factory=list)
    term_set_source: dict[str, list[str]] = field(default_factory=dict)
    hyde_passage: str = ""
    embedding_model: str = ""
    embedding_fingerprint: str = ""
    chunker_version: str = ""
    semantic_band: int = 0
    judge_model: str = ""
    judge_prompt_version: str = ""


@dataclass
class InventorySearchResponse:
    query: str
    snapshot_id: str = ""
    retrieval_policy: RetrievalPolicy = field(default_factory=RetrievalPolicy)
    coverage: Coverage = field(default_factory=Coverage)
    matched_documents: int = 0
    results: list[InventoryResult] = field(default_factory=list)
    excluded: list[ExcludedResult] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Law-only keys are noise on a circular row.
        for result in payload["results"]:
            if result["result_kind"] != "law":
                for key in (
                    "law_type", "version_id", "version_label", "parent_id", "parent_title"
                ):
                    result.pop(key, None)
        return payload
