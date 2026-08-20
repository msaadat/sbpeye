from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Float, Index, UniqueConstraint, or_,
)
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import AppBase, Base, DebugBase, app_engine

class Circular(Base):
    __tablename__ = "circulars"

    id = Column(String, primary_key=True, index=True)
    reference = Column(String)
    title = Column(String, nullable=False)
    department = Column(String)
    date = Column(DateTime)
    # When this row was first scraped/indexed, as opposed to `date` (the circular's
    # publication date parsed from SBP listings). Powers the "indexed today" stat.
    indexed_at = Column(DateTime, nullable=True)
    url = Column(String)
    # After the July 2026 SBP redesign a circular is reachable at two URLs: its slug on
    # the new site (new_url) and its original path on the frozen archive.sbp.org.pk
    # mirror (old_url). `url` mirrors the primary (new) URL for backward compatibility.
    new_url = Column(String, nullable=True)
    old_url = Column(String, nullable=True)
    content_text = Column(Text)
    summary = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    compliance_checklist = Column(Text, nullable=True)
    status = Column(String, default="active")
    summary_generated_at = Column(DateTime, nullable=True)
    tags_generated_at = Column(DateTime, nullable=True)
    checklist_generated_at = Column(DateTime, nullable=True)
    relationships_generated_at = Column(DateTime, nullable=True)
    attachments_scanned_at = Column(DateTime, nullable=True)
    entities_generated_at = Column(DateTime, nullable=True)

    @property
    def display_name(self) -> str:
        """Human label for a circular: its reference, or the title when unreferenced."""
        return self.reference or self.title

    amends = relationship("CircularRelationship", foreign_keys="[CircularRelationship.source_id]", back_populates="source")
    amended_by = relationship("CircularRelationship", foreign_keys="[CircularRelationship.target_id]", back_populates="target")
    attachments = relationship(
        "Attachment", back_populates="circular", cascade="all, delete-orphan"
    )
    entities = relationship(
        "CircularEntity", back_populates="circular", cascade="all, delete-orphan"
    )
    reg_links = relationship(
        "RegDocumentLink",
        back_populates="circular",
        cascade="all, delete-orphan",
    )

class CircularRelationship(Base):
    __tablename__ = "circular_relationships"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(String, ForeignKey("circulars.id"))
    target_id = Column(String, ForeignKey("circulars.id"), nullable=True)
    target_reference = Column(String, nullable=True)
    type = Column(String)
    confidence = Column(Float, nullable=True)

    source = relationship("Circular", foreign_keys=[source_id], back_populates="amends")
    target = relationship("Circular", foreign_keys=[target_id], back_populates="amended_by")


class CircularEntity(Base):
    """A structured regulatory value (ratio, threshold, limit, or date) extracted
    from a circular *or* from a law/regulation. Stored normalized so the corpus is
    queryable by numeric range, metric, unit, and subject — e.g. "thresholds above 10%"
    or the current MCR for MFBs.

    One table with a `subject_kind` discriminator rather than a parallel law table: a CAR
    requirement is a CAR requirement whichever instrument states it, and forking the table
    would fork `/api/circulars/entities/query`, the values browser, the Excel export and
    the chat tool into permanent unions. The name is historical — it predates the laws
    corpus — and the columns, not the table name, carry the meaning.

    A law-sourced value carries `version_id`, because a ratio extracted from the Oct 2024
    edition of the SME PRs is not a claim about the 2026 edition (LAWS_AI_PLAN.md §4.2).
    """

    __tablename__ = "circular_entities"

    id = Column(Integer, primary_key=True, index=True)
    # circular | law. Says which of the two subject columns below is populated.
    subject_kind = Column(String, nullable=False, default="circular", index=True)
    circular_id = Column(String, ForeignKey("circulars.id"), nullable=True, index=True)
    document_id = Column(String, ForeignKey("reg_documents.id"), nullable=True, index=True)
    # The edition the value was read out of; NULL for circulars, which are immutable.
    version_id = Column(
        String, ForeignKey("reg_document_versions.id"), nullable=True, index=True
    )
    # ratio | monetary_threshold | percentage_limit | numeric_limit | deadline | effective_date
    entity_type = Column(String, nullable=False, index=True)
    # canonical metric name, e.g. CAR, LCR, NSFR, MCR, Paid-up Capital, Leverage Ratio
    metric = Column(String, nullable=True, index=True)
    # min | max | exactly | range
    comparator = Column(String, nullable=True)
    # value normalized to base units: "Rs. 23 billion" -> 23000000000.0; "8%" -> 8.0
    value_numeric = Column(Float, nullable=True)
    value_high = Column(Float, nullable=True)
    # % | PKR | USD | times | days | months
    unit = Column(String, nullable=True, index=True)
    value_text = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    effective_date = Column(DateTime, nullable=True)
    context_snippet = Column(Text, nullable=True)
    source_unit_id = Column(String, nullable=True)
    page_start = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    circular = relationship("Circular", back_populates="entities")
    document = relationship("RegDocument", back_populates="entities")
    version = relationship("RegDocumentVersion")


class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(String, primary_key=True)
    circular_id = Column(
        String, ForeignKey("circulars.id"), nullable=False, index=True
    )
    filename = Column(String, nullable=False)
    original_url = Column(String, nullable=False)
    local_path = Column(String, nullable=True)
    file_type = Column(String, nullable=True)
    content_text = Column(Text, nullable=True)
    extraction_status = Column(String, nullable=False, default="pending")
    extraction_error = Column(Text, nullable=True)
    is_vectorized = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    circular = relationship("Circular", back_populates="attachments")


class RegDocument(Base):
    """A law, regulation, guideline or other item from SBP's /laws-regulations listing.

    Unlike a circular — an immutable dated event — a regulation is a living document
    with a stable identity and changing content: SBP replaces the PDF in place at the
    same URL and only hints at the change in the title suffix. So identity here is the
    *normalized title* (version suffixes stripped), and the actual content lives in
    `RegDocumentVersion` rows keyed by content hash.

    Multi-part documents (the Foreign Exchange Manual and its chapters/appendices) are
    modelled as a container row with `parent_id` children; a child's own children are
    allowed, since Appendix III is itself a subpage. Listing rows that turn out to be
    circulars are not duplicated — they carry `circular_id` and no content of their own.
    """

    __tablename__ = "reg_documents"

    id = Column(String, primary_key=True, index=True)
    title = Column(String, nullable=False)
    normalized_title = Column(String, index=True)
    # law | regulation | guideline | gazette | licensing
    doc_type = Column(String, index=True)
    source_url = Column(String, nullable=True)
    page_slug = Column(String, nullable=True)
    # Hierarchy. NULL for flat single-file documents.
    parent_id = Column(String, ForeignKey("reg_documents.id"), nullable=True, index=True)
    part_label = Column(String, nullable=True)
    part_order = Column(Integer, nullable=True)
    # Dedupe path: set when the listing item is actually an already-indexed circular.
    circular_id = Column(String, ForeignKey("circulars.id"), nullable=True, index=True)
    is_external = Column(Integer, nullable=False, default=0)
    # From the listing's data-date attribute; display metadata only, never a version
    # signal (old laws carry fabricated placeholder dates).
    listed_date = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    # Set when the row vanishes from the listing. Rows are never deleted.
    delisted_at = Column(DateTime, nullable=True)
    # Raised when a newly scraped circular references this document — a circular is the
    # change mechanism for a regulation, so it is the earliest signal a new edition may
    # exist. A flagged document is synced even by a filtered pass, then cleared.
    refetch_requested = Column(Integer, nullable=False, default=0, index=True)
    summary = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    summary_generated_at = Column(DateTime, nullable=True)
    tags_generated_at = Column(DateTime, nullable=True)

    versions = relationship(
        "RegDocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="RegDocumentVersion.first_seen_at",
    )
    children = relationship(
        "RegDocument",
        back_populates="parent",
        order_by="RegDocument.part_order",
    )
    parent = relationship("RegDocument", back_populates="children", remote_side=[id])
    circular = relationship("Circular", foreign_keys=[circular_id])
    circular_links = relationship(
        "RegDocumentLink",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    entities = relationship(
        "CircularEntity",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    outgoing_relationships = relationship(
        "RegDocumentRelationship",
        foreign_keys="[RegDocumentRelationship.source_document_id]",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    incoming_relationships = relationship(
        "RegDocumentRelationship",
        foreign_keys="[RegDocumentRelationship.target_document_id]",
        back_populates="target",
    )

    @property
    def current_version(self):
        """The version in force today, or None (external/stub/circular-backed rows)."""
        for version in self.versions:
            if version.is_current:
                return version
        return None


class RegDocumentVersion(Base):
    """One captured state of a `RegDocument`, identified by content hash.

    SBP keeps no history of its own — superseded PDFs simply vanish from the site — so
    every fetched file is archived immutably under `files/laws/<document_id>/` and
    never overwritten. Hashes are the only trustworthy change detector: URLs, titles and
    listing dates all stay put across revisions.

    Currency is *not* "the newest row": the listing can carry two live editions of the
    same document at once (an in-force one and one applicable from a future date), so
    `is_current` is decided per document per sync from `effective_from`, and versions
    whose effective date has not arrived stay pending with `is_current = 0`.
    """

    __tablename__ = "reg_document_versions"

    id = Column(String, primary_key=True, index=True)
    document_id = Column(
        String, ForeignKey("reg_documents.id"), nullable=False, index=True
    )
    # sha256 of the file bytes, or of the cleaned text for HTML-content pages (raw HTML
    # churns on unrelated template tweaks).
    content_hash = Column(String, index=True)
    file_url = Column(String, nullable=True)
    local_path = Column(String, nullable=True)
    file_type = Column(String, nullable=True)
    # Parsed from the title suffix, e.g. "Updated till June 2024".
    version_label = Column(String, nullable=True)
    content_text = Column(Text, nullable=True)
    extraction_status = Column(String, nullable=False, default="pending")
    extraction_error = Column(Text, nullable=True)
    is_vectorized = Column(Integer, nullable=False, default=0)
    is_current = Column(Integer, nullable=False, default=1, index=True)
    # AI analysis of *this edition*, not of the document (docs/LAWS_AI_PLAN.md §4). SBP
    # replaces the PDF in place, so analysis hung off `RegDocument` would silently survive
    # the replacement and describe wording no longer in force. A version's bytes never
    # change, so its analysis never needs invalidating — and a new capture correctly reads
    # as "not analysed yet" rather than as a stale answer. Containers are the exception:
    # a manifest has no text, so their rollup stays on `RegDocument.summary`.
    summary = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    compliance_checklist = Column(Text, nullable=True)
    summary_generated_at = Column(DateTime, nullable=True)
    tags_generated_at = Column(DateTime, nullable=True)
    checklist_generated_at = Column(DateTime, nullable=True)
    entities_generated_at = Column(DateTime, nullable=True)
    relationships_generated_at = Column(DateTime, nullable=True)
    # Parsed from suffixes like "(to be applicable from January 1, 2026)"; drives
    # currency selection between parallel editions.
    effective_from = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    # live | wayback (backfilled history)
    source = Column(String, nullable=False, default="live")

    document = relationship("RegDocument", back_populates="versions")


class RegDocumentLink(Base):
    """An edge between a circular and a law/regulation.

    Circulars are the change mechanism for regulations — a circular announcing an
    amendment is the signal that a new version exists — so this table is both a
    navigation aid and the trigger for re-fetching a document.
    """

    __tablename__ = "reg_document_links"

    id = Column(Integer, primary_key=True, index=True)
    circular_id = Column(String, ForeignKey("circulars.id"), nullable=False, index=True)
    document_id = Column(
        String, ForeignKey("reg_documents.id"), nullable=False, index=True
    )
    # amends | annexure_of | references | implements | listing
    link_type = Column(String, nullable=True)
    # url_scan | ai | listing
    detected_via = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    circular = relationship("Circular", back_populates="reg_links")
    document = relationship("RegDocument", back_populates="circular_links")


class RegDocumentRelationship(Base):
    """An edge from one law/regulation to another.

    Separate from `RegDocumentLink` — that table is circular ↔ document and its
    `circular_id` is NOT NULL — and shaped like `CircularRelationship`, including the
    unresolved `target_reference` arm.

    The vocabulary was fixed from the corpus rather than guessed (LAWS_AI_PLAN.md §10):
    running the deterministic name pass over all 133 documents produced 121 candidate
    edges, of which the great majority are definitional cross-references ("a bank as
    defined in the Banking Companies Ordinance"). 36 documents carry "in exercise of the
    powers conferred"-style language, 4 carry amendment language and 2 repeal language.

    - `made_under`  subordinate legislation issued under a parent Act — the relation with
                    real authority, and the most common non-incidental one
    - `amends`      an amendment Act altering its principal Act
    - `repeals`     one instrument repealing another
    - `references`  a definitional or incidental mention, which is most of them

    There is deliberately no `supersedes`: between two *laws* supersession is an edition
    change, and `RegDocumentVersion.is_current` already models that. Recording it here
    too would create a second, driftable answer to the same question.
    """

    __tablename__ = "reg_document_relationships"

    id = Column(Integer, primary_key=True, index=True)
    source_document_id = Column(
        String, ForeignKey("reg_documents.id"), nullable=False, index=True
    )
    target_document_id = Column(
        String, ForeignKey("reg_documents.id"), nullable=True, index=True
    )
    # The raw name when it could not be resolved to a row we hold.
    target_reference = Column(String, nullable=True)
    type = Column(String, index=True)
    confidence = Column(Float, nullable=True)
    # name_match | ai
    detected_via = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    source = relationship(
        "RegDocument",
        foreign_keys=[source_document_id],
        back_populates="outgoing_relationships",
    )
    target = relationship(
        "RegDocument",
        foreign_keys=[target_document_id],
        back_populates="incoming_relationships",
    )


class CircularConsolidation(Base):
    """The consolidated requirement state of an amendment chain.

    One row per chain, keyed by the base (oldest) circular's id. `requirements`
    holds the merged, provenance-tracked requirement list produced by folding
    each amending circular into the base (see consolidation.py). `stale` is set
    when a relationships pass touches a chain member, signalling that the
    stored merge may no longer include the newest amendment."""

    __tablename__ = "circular_consolidations"

    chain_id = Column(String, ForeignKey("circulars.id"), primary_key=True)
    member_ids = Column(Text, nullable=False)
    as_of_circular_id = Column(String, ForeignKey("circulars.id"), nullable=False)
    requirements = Column(Text, nullable=False)
    model = Column(String, nullable=True)
    stale = Column(Integer, nullable=False, default=0)
    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CachedDocument(Base):
    __tablename__ = "cached_documents"

    id = Column(String, primary_key=True)
    filename = Column(String, nullable=False)
    original_url = Column(String, nullable=False, unique=True, index=True)
    local_path = Column(String, nullable=True)
    file_type = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AIGenerationJob(Base):
    """One background AI analysis run, over a circular or over a law/regulation.

    One table with a discriminator rather than a sibling per corpus: the poll endpoint,
    the frontend's polling loop and `fail_interrupted_ai_jobs()` are all target-agnostic
    already, and a second table would fork three generic things to no benefit.
    """

    __tablename__ = "ai_generation_jobs"

    id = Column(String, primary_key=True)
    # circular | law. Says which of the two id columns below carries the target.
    target_kind = Column(String, nullable=False, default="circular", index=True)
    circular_id = Column(String, ForeignKey("circulars.id"), nullable=True, index=True)
    document_id = Column(String, ForeignKey("reg_documents.id"), nullable=True, index=True)
    feature = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued", index=True)
    error = Column(Text, nullable=True)
    progress_total = Column(Integer, nullable=False, default=0)
    progress_completed = Column(Integer, nullable=False, default=0)
    result_status = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class ResearchWorkspace(AppBase):
    """A user's research workspace: a named selection of circulars plus its search state.

    On ``AppBase``, not ``Base``: this is runtime state, created by whoever is using the
    app, and lives in the application database alongside chat and settings rather than in
    the shipped corpus.
    """

    __tablename__ = "research_workspaces"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    is_default = Column(Integer, nullable=False, default=0)
    search_state = Column(Text, nullable=True)
    # A circular id, but not a ForeignKey: circulars live in the corpus database, and
    # SQLite cannot enforce a reference across files. Readers must tolerate an id whose
    # circular is absent — a corpus rebuild can retire one out from under a workspace.
    last_circular_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    pinned_circulars = relationship(
        "WorkspaceCircular",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="WorkspaceCircular.added_at.desc()",
    )


class WorkspaceCircular(AppBase):
    __tablename__ = "workspace_circulars"

    workspace_id = Column(
        String, ForeignKey("research_workspaces.id"), primary_key=True
    )
    # Cross-database, so no ForeignKey and no `circular` relationship — the ORM cannot
    # lazy-load across two engines. Callers resolve these ids against a corpus session
    # themselves (see `_load_workspace_circulars` in api/serializers.py), which also
    # turns what used to be one query per pinned row into a single batched lookup.
    circular_id = Column(String, primary_key=True)
    role = Column(String, nullable=False, default="pinned")
    note = Column(Text, nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_viewed_at = Column(DateTime, nullable=True)

    workspace = relationship("ResearchWorkspace", back_populates="pinned_circulars")

class EcoDataSeries(Base):
    __tablename__ = "ecodata_series"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    date = Column(DateTime, index=True)
    value = Column(Float)

class EcoDataEntry(Base):
    __tablename__ = "ecodata_entries"

    id = Column(String, primary_key=True, index=True)
    section = Column(String, index=True)
    subsection = Column(String, nullable=True)
    description = Column(String, nullable=False)
    url = Column(String, nullable=True)
    frequency = Column(String, nullable=True)
    format_url = Column(String, nullable=True)
    format_type = Column(String, nullable=True)
    last_update = Column(String, nullable=True)
    archive_url = Column(String, nullable=True)
    archive_updated = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    is_quick_link = Column(Integer, default=0)

class EcoDataCache(Base):
    __tablename__ = "ecodata_cache"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, unique=True, nullable=False)
    summary_markdown = Column(Text)
    created_at = Column(DateTime)

class SyncStatus(Base):
    __tablename__ = "sync_status"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(String, nullable=True, index=True)
    # Which corpus this run scraped: circulars | laws. NULL on rows written before
    # laws sync existed, which are all circular runs — see CIRCULAR_SYNC_KINDS.
    kind = Column(String, nullable=True, index=True)
    last_sync_date = Column(DateTime)
    status = Column(String)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    parameters = Column(Text, nullable=True)
    processed_count = Column(Integer, nullable=True)
    skipped_count = Column(Integer, nullable=True)
    error_count = Column(Integer, nullable=True)
    ecodata_index_time = Column(DateTime, nullable=True)

def circular_sync_only():
    """Filter selecting the SyncStatus rows the circular-sync UI owns.

    Laws runs share the table (`kind="laws"`) but must stay out of the circular sync
    banner. Rows written before `kind` existed are all circular runs, hence the NULL
    arm — `kind.in_((None, "circulars"))` would silently drop them, since SQL's IN
    never matches NULL.
    """
    return or_(SyncStatus.kind.is_(None), SyncStatus.kind == "circulars")


class Settings(AppBase):
    """Operator configuration written by the Settings UI — AI provider, embedding, debug.

    On ``AppBase`` because it is written at runtime. Shipped with the corpus it would be
    reverted to the build's values every time that file was replaced.
    """

    __tablename__ = "settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(Text)


class LLMTrace(DebugBase):
    """Durable summary row for one logical text-generation operation.

    On ``DebugBase``, not ``Base``: traces live in their own database file, so they
    must not be created alongside the application tables.
    """

    __tablename__ = "llm_traces"

    id = Column(String, primary_key=True)
    schema_version = Column(Integer, nullable=False, default=1)
    operation = Column(String, nullable=False, index=True)
    origin = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="running", index=True)
    provider = Column(String, nullable=True, index=True)
    model = Column(String, nullable=True, index=True)
    job_id = Column(String, nullable=True, index=True)
    chat_session_id = Column(String, nullable=True, index=True)
    target_kind = Column(String, nullable=True)
    target_id = Column(String, nullable=True, index=True)
    command_name = Column(String, nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}")
    attempt_count = Column(Integer, nullable=False, default=0)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    payload_bytes = Column(Integer, nullable=False, default=0)
    error_type = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)


class LLMTraceEvent(DebugBase):
    """Append-only event in an :class:`LLMTrace` timeline."""

    __tablename__ = "llm_trace_events"
    __table_args__ = (
        UniqueConstraint("trace_id", "sequence", name="uq_llm_trace_event_sequence"),
        Index("ix_llm_trace_event_trace_sequence", "trace_id", "sequence"),
        Index("ix_llm_trace_event_trace_id", "trace_id", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_id = Column(String, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    kind = Column(String, nullable=False, index=True)
    stage = Column(String, nullable=True)
    attempt_id = Column(String, nullable=True, index=True)
    attempt_number = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    elapsed_ms = Column(Integer, nullable=True)


def upsert_settings(db, values: dict[str, str]) -> None:
    """Insert or update the given Settings key/value rows, then commit."""
    for key, value in values.items():
        row = db.query(Settings).filter(Settings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Settings(key=key, value=value))
    db.commit()

class ChatSession(AppBase):
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=True)
    circular_ids = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "ChatMessage",
        back_populates="session",
        order_by="(ChatMessage.created_at, ChatMessage.id)",
    )

class ChatMessage(AppBase):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True)
    session_id = Column(String, ForeignKey("chat_sessions.id"), index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    circular_ids = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")


class SemanticIndexSource(Base):
    """One row per physical searchable source, recording what is actually in the index.

    `Attachment.is_vectorized` and `RegDocumentVersion.is_vectorized` cannot support an
    inventory-completeness claim: circular bodies have no flag at all, a boolean names no
    embedding model or chunker, it cannot prove the expected chunk count is present, and
    it collapses "indexed" with "processed but produced zero chunks" — which is how the
    live index came to hold 88 law documents while SQLite claimed 100.

    See docs/INVENTORY_SEARCH_PLAN.md section 10.
    """

    __tablename__ = "semantic_index_sources"

    id = Column(String, primary_key=True)
    # circular | attachment | law_version
    source_kind = Column(String, nullable=False, index=True)
    source_id = Column(String, nullable=False, index=True)
    # The logical document a hit rolls up to: circular | law
    logical_kind = Column(String, nullable=False, index=True)
    logical_document_id = Column(String, nullable=False, index=True)
    # Current law version where applicable; NULL for circulars and attachments.
    version_id = Column(String, nullable=True)
    content_hash = Column(String, nullable=True)
    chunker_version = Column(String, nullable=True)
    embedding_fingerprint = Column(String, nullable=True)
    expected_chunks = Column(Integer, nullable=False, default=0)
    indexed_chunks = Column(Integer, nullable=False, default=0)
    # indexed | empty | unsupported | extraction_error | index_error | stale
    status = Column(String, nullable=False, default="stale", index=True)
    error = Column(Text, nullable=True)
    indexed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("source_kind", "source_id", name="uq_semantic_index_source"),
    )


# Here rather than in `database`, for the same reason `llm_debug` creates the trace
# tables: `database` runs its own `create_all` at import, before this module has been
# imported, so `AppBase.metadata` is still empty at that point. This is the first place
# it knows about the tables above.
AppBase.metadata.create_all(bind=app_engine)
