"""Pure serialization, payload-shaping, and workspace/settings helpers for the API.

These were extracted from ``main.py`` so route handlers stay thin and the response
shapes live in one place (and can be reused from the CLI). Nothing here owns the
FastAPI app; functions take a SQLAlchemy ``Session`` explicitly where they touch the DB.
"""

from datetime import datetime

import json

from sqlalchemy.orm import Session

from ..ai import AIConfig, get_provider_api_key, get_provider_definition, normalize_provider
from ..embeddings import EmbeddingConfig
from ..env import managed_env_path, set_managed_env_value, unset_managed_env_value
from ..models import (
    Attachment,
    CachedDocument,
    ChatSession,
    Circular,
    RegDocument,
    RegDocumentLink,
    RegDocumentVersion,
    ResearchWorkspace,
    WorkspaceCircular,
)
from ..scraper.laws import split_law_title

DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default"
WORKSPACE_CHAT_SESSION_PREFIX = "workspace:"


# --- Primitive formatting helpers ---

def _parse_year(val: str | None) -> int | None:
    return int(val) if val and val.isdigit() else None


def _format_timestamp(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value or "Never"


def _safe_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_json_object(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_circular_ids(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int))]


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _summary_preview(value: str | None, limit: int = 200) -> str | None:
    if not value:
        return None
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


# --- Circular serialization ---

def _circular_summary(
    circular: Circular,
    snippet: str | None = None,
    match_source: str = "circular",
    attachment_id: str | None = None,
    attachment_filename: str | None = None,
    source_ref: str | None = None,
    source_page: int | None = None,
) -> dict:
    return {
        # Lets a caller tell circular results from law results in a mixed list.
        "result_kind": "circular",
        "id": circular.id,
        "title": circular.title,
        "department": circular.department,
        "reference": circular.reference,
        "date": circular.date.strftime("%Y-%m-%d") if circular.date else None,
        "url": circular.url,
        "new_url": circular.new_url or circular.url,
        "old_url": circular.old_url,
        "summary": _summary_preview(circular.summary),
        "tags": _safe_json_list(circular.tags),
        "status": circular.status or "active",
        "snippet": snippet or "",
        "match_source": match_source,
        "attachment_id": attachment_id,
        "attachment_filename": attachment_filename,
        "source_ref": source_ref,
        "source_page": source_page,
    }


# --- Laws & regulations serialization ---

def _law_version_payload(version: RegDocumentVersion | None, include_text: bool = False) -> dict | None:
    """One captured version. `in_force` is state, not a stored flag: a version whose
    effective date has not arrived is captured but pending (see the plan's §3)."""
    if version is None:
        return None
    effective_from = version.effective_from
    payload = {
        "id": version.id,
        "content_hash": version.content_hash,
        "file_url": version.file_url,
        "file_type": version.file_type,
        "version_label": version.version_label,
        "effective_from": _isoformat(effective_from),
        "pending": bool(effective_from and effective_from > datetime.utcnow()),
        "is_current": bool(version.is_current),
        "extraction_status": version.extraction_status,
        "source": version.source,
        "first_seen_at": _isoformat(version.first_seen_at),
        "last_seen_at": _isoformat(version.last_seen_at),
        "has_file": bool(version.local_path),
    }
    if include_text:
        payload["content_text"] = version.content_text
    return payload


class _EmptyAnalysis:
    """Stands in for a document that has no row to carry analysis."""

    summary = None
    tags = None
    summary_generated_at = None
    tags_generated_at = None


def _analysis_row(document: RegDocument):
    """The row carrying this document's summary and tags.

    A container's rollup is on the `RegDocument`; everything else reads from the edition
    in force. Chosen by document shape rather than by falling back from an empty value,
    so a superseded edition's analysis can never surface as the document's own.
    """
    if document.children:
        return document
    return document.current_version or _EmptyAnalysis


def _law_summary(document: RegDocument, snippet: str | None = None) -> dict:
    """List-shaped payload. `result_kind` lets a caller badge mixed search results."""
    current = document.current_version
    # "(Updated till July 16, 2026)" is state, not name. Raw `title` stays as SBP wrote
    # it; `display_title`/`version_suffix` are the split, done here because the phrase
    # set that recognises a suffix lives (and is tested) in the scraper.
    display_title, version_suffix = split_law_title(document.title)
    return {
        "result_kind": "law",
        "id": document.id,
        "title": document.title,
        "display_title": display_title,
        "version_suffix": version_suffix,
        "doc_type": document.doc_type,
        "part_label": document.part_label,
        "part_order": document.part_order,
        "parent_id": document.parent_id,
        # A part is meaningless without its container — "EXPORTS" is Chapter 12 of the
        # Foreign Exchange Manual or it is nothing. Callers that hold the whole corpus can
        # resolve `parent_id` themselves; the ones that show a law among circulars cannot.
        "parent_title": (
            split_law_title(document.parent.title)[0] if document.parent else None
        ),
        "source_url": document.source_url,
        "is_external": bool(document.is_external),
        "circular_id": document.circular_id,
        "listed_date": _isoformat(document.listed_date),
        "delisted_at": _isoformat(document.delisted_at),
        "version_count": len(document.versions),
        "current_version": _law_version_payload(current),
        # Analysis of the edition in force, never of an older one (LAWS_AI_PLAN.md §4).
        # A document whose current edition has not been analysed is un-analysed, even
        # when a superseded version carries a summary — showing that one would describe
        # wording SBP no longer publishes.
        #
        # A container is the exception and is selected on explicitly, not by falling back:
        # it *has* a current version (a manifest), so a fallback would never fire, and its
        # rollup lives on the document because the manifest's hash changes whenever any
        # part does.
        "summary": _summary_preview(_analysis_row(document).summary),
        "tags": _safe_json_list(_analysis_row(document).tags),
        "snippet": snippet or "",
    }


# How much an edge is worth believing when two describe the same pair. A hyperlink to the
# document beats SBP's own grouping, which beats a model's judgement, which beats the
# document's name turning up in the text — the last of which can be an incidental mention.
_DETECTION_PRECEDENCE = ("url_scan", "listing", "ai", "name_match")


def _link_strength(link: RegDocumentLink) -> tuple[float, int]:
    precedence = _DETECTION_PRECEDENCE.index(link.detected_via) if (
        link.detected_via in _DETECTION_PRECEDENCE
    ) else len(_DETECTION_PRECEDENCE)
    return (link.confidence or 0.0, -precedence)


def _circular_regulations(circular: Circular) -> list[dict]:
    """The regulations a circular cites — the mirror of a law's `linked_circulars`.

    The graph was one-way in the UI: 688 circulars carry links, but a circular's payload
    never mentioned them, so the 809 edges were only navigable regulation-first. People
    read circular-first.

    Deduped by document: a few pairs are detected twice and the payload should name a
    document once. Confidence decides, and where it is absent — which is every duplicate
    in the corpus today — detection method breaks the tie, so the winner does not depend
    on the order rows happen to come back in.
    """
    best: dict[str, RegDocumentLink] = {}
    for link in circular.reg_links:
        if link.document is None:
            continue
        seen = best.get(link.document_id)
        if seen is None or _link_strength(link) > _link_strength(seen):
            best[link.document_id] = link

    return [
        {
            "document": _law_summary(link.document),
            "link_type": link.link_type,
            "detected_via": link.detected_via,
            "confidence": link.confidence,
        }
        for link in sorted(best.values(), key=_regulation_sort_key)
    ]


def _regulation_sort_key(link: RegDocumentLink) -> tuple:
    """Group parts under their container, container first, then chapter order.

    Sorting these by title alone strands the container among its own chapters — an FE
    circular revising four chapters lists them as BLOCKED ACCOUNTS, DEALINGS…, Foreign
    Exchange Manual, INTRODUCTORY. Grouping reads as the manual and the chapters of it.
    """
    document = link.document
    container = document.parent or document
    return (
        document.doc_type or "",
        split_law_title(container.title)[0],
        document.parent_id is not None,
        document.part_order if document.part_order is not None else 0,
        split_law_title(document.title)[0],
    )


def _law_entity_payload(entity) -> dict:
    """One regulatory value, shaped exactly like `CircularDetail.entities` entries.

    Field-for-field with the circular arm on purpose: the detail rail that renders these
    is meant to be one shared component, not two that drift.
    """
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "metric": entity.metric,
        "comparator": entity.comparator,
        "value_numeric": entity.value_numeric,
        "value_high": entity.value_high,
        "unit": entity.unit,
        "value_text": entity.value_text,
        "subject": entity.subject,
        "effective_date": _isoformat(entity.effective_date),
        "context_snippet": entity.context_snippet,
        "page_start": entity.page_start,
        "confidence": entity.confidence,
    }


def _law_relationship_payload(edge, other) -> dict:
    """One law → law edge, from the perspective of the document being viewed."""
    return {
        "type": edge.type or "references",
        "confidence": edge.confidence,
        "detected_via": edge.detected_via,
        "target_reference": edge.target_reference,
        "document": (
            {
                "id": other.id,
                "title": other.title,
                "display_title": split_law_title(other.title)[0],
                "doc_type": other.doc_type,
                "part_label": other.part_label,
            }
            if other is not None else None
        ),
    }


def _law_detail(document: RegDocument) -> dict:
    """Detail payload: what is in force, the whole timeline, parts, and linked circulars."""
    payload = _law_summary(document)
    current = document.current_version
    payload.update({
        # The list payload clips to a 200-char preview for result cards; the detail rail
        # renders the summary in full, as the circular detail endpoint already does.
        "summary": _analysis_row(document).summary,
        # Field-for-field with `CircularDetail.generation`, so the detail rail can be one
        # component rather than two. A null means "not analysed", which for a freshly
        # captured edition is the honest answer even when the previous one was analysed.
        "generation": {
            # Summary and tags follow the rollup rule; the rest are version-only, and a
            # container legitimately reports null for them — it can never produce one.
            "summary": _isoformat(_analysis_row(document).summary_generated_at),
            "tags": _isoformat(_analysis_row(document).tags_generated_at),
            "checklist": _isoformat(current.checklist_generated_at) if current else None,
            "entities": _isoformat(current.entities_generated_at) if current else None,
            "relationships": (
                _isoformat(current.relationships_generated_at) if current else None
            ),
        },
        "checklist_available": bool(current and current.compliance_checklist),
        # Only the edition in force states values that are true today; an earlier
        # edition's rows stay in the table for version comparison, not for the reader.
        "entities": [
            _law_entity_payload(entity)
            for entity in sorted(
                (e for e in document.entities if current and e.version_id == current.id),
                key=lambda e: (e.entity_type or "", e.metric or ""),
            )
        ],
        # Law → law edges, both ways. Shaped like `CircularDetail.relationships` so the
        # same grouped-pills rail renders either corpus.
        "relationships": {
            "outgoing": [
                _law_relationship_payload(edge, edge.target)
                for edge in document.outgoing_relationships
            ],
            "incoming": [
                _law_relationship_payload(edge, edge.source)
                for edge in document.incoming_relationships
            ],
        },
        "normalized_title": document.normalized_title,
        "page_slug": document.page_slug,
        "first_seen_at": _isoformat(document.first_seen_at),
        "last_seen_at": _isoformat(document.last_seen_at),
        "versions": [
            _law_version_payload(version)
            for version in sorted(
                document.versions,
                key=lambda v: v.first_seen_at or datetime.min,
                reverse=True,
            )
        ],
        "parent": (
            {
                "id": document.parent.id,
                "title": document.parent.title,
                "display_title": split_law_title(document.parent.title)[0],
            }
            if document.parent else None
        ),
        "children": [
            {
                "id": child.id,
                "title": child.title,
                "display_title": split_law_title(child.title)[0],
                "part_label": child.part_label,
                "part_order": child.part_order,
                "has_content": child.current_version is not None,
                "delisted_at": _isoformat(child.delisted_at),
            }
            for child in sorted(
                document.children, key=lambda c: (c.part_order is None, c.part_order or 0)
            )
        ],
        "circular": (
            _circular_summary(document.circular) if document.circular else None
        ),
        "linked_circulars": [
            {
                "circular": _circular_summary(link.circular),
                "link_type": link.link_type,
                "detected_via": link.detected_via,
                "confidence": link.confidence,
            }
            for link in document.circular_links
            if link.circular is not None
        ],
    })
    return payload


def _document_payload(attachment: Attachment | CachedDocument) -> dict:
    from ..database import PROJECT_ROOT

    local_path = None
    if attachment.local_path:
        candidate_path = (PROJECT_ROOT / attachment.local_path).resolve()
        attachments_root = (PROJECT_ROOT / "attachments").resolve()
        if attachments_root in candidate_path.parents and candidate_path.is_file():
            local_path = candidate_path
    return {
        "id": attachment.id,
        "circular_id": getattr(attachment, "circular_id", None),
        "filename": attachment.filename,
        "file_type": attachment.file_type,
        "original_url": attachment.original_url,
        "cached": bool(local_path),
        "content_url": f"/api/documents/{attachment.id}/content",
        "extraction_status": getattr(attachment, "extraction_status", None),
        "error": getattr(attachment, "extraction_error", None) or getattr(attachment, "error", None),
    }


# --- Research workspaces ---

def _workspace_search_state(value: str | None) -> dict:
    return _safe_json_object(value) or {}


def _sorted_workspace_pinned_links(workspace: ResearchWorkspace) -> list[WorkspaceCircular]:
    def _sort_key(link: WorkspaceCircular) -> tuple:
        circular_date = link.circular.date if link.circular and link.circular.date else datetime.min
        added_at = link.added_at or datetime.min
        title = (link.circular.title if link.circular and link.circular.title else "").lower()
        circular_id = link.circular_id or ""
        return (circular_date, added_at, title, circular_id)

    return sorted(
        [
            link
            for link in list(workspace.pinned_circulars or [])
            if link.circular is not None
        ],
        key=_sort_key,
        reverse=True,
    )


def _ensure_default_workspace(db: Session) -> ResearchWorkspace:
    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.is_default == 1
    ).first()
    if workspace:
        return workspace

    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == DEFAULT_WORKSPACE_ID
    ).first()
    if workspace:
        workspace.is_default = 1
        if not workspace.name:
            workspace.name = DEFAULT_WORKSPACE_NAME
        workspace.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(workspace)
        return workspace

    workspace = ResearchWorkspace(
        id=DEFAULT_WORKSPACE_ID,
        name=DEFAULT_WORKSPACE_NAME,
        is_default=1,
        search_state=json.dumps({}),
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return workspace


def _workspace_payload(workspace: ResearchWorkspace, include_circulars: bool = True) -> dict:
    pinned_links = _sorted_workspace_pinned_links(workspace)
    pinned_circulars = [
        _circular_summary(link.circular)
        for link in pinned_links
    ] if include_circulars else []

    return {
        "id": workspace.id,
        "name": workspace.name,
        "is_default": bool(workspace.is_default),
        "search_state": _workspace_search_state(workspace.search_state),
        "last_circular_id": workspace.last_circular_id,
        "pinned_circular_ids": [
            link.circular_id for link in pinned_links
        ],
        "pinned_circulars": pinned_circulars,
        "pinned_count": len(pinned_links),
        "created_at": _isoformat(workspace.created_at),
        "updated_at": _isoformat(workspace.updated_at or workspace.created_at),
    }


def _workspace_chat_session_id(workspace_id: str) -> str:
    return f"{WORKSPACE_CHAT_SESSION_PREFIX}{workspace_id}"


def _workspace_id_from_chat_session(session_id: str | None) -> str | None:
    if not isinstance(session_id, str):
        return None
    if not session_id.startswith(WORKSPACE_CHAT_SESSION_PREFIX):
        return None
    workspace_id = session_id[len(WORKSPACE_CHAT_SESSION_PREFIX):]
    return workspace_id or None


def _workspace_circular_ids(workspace: ResearchWorkspace) -> list[str]:
    return [
        link.circular_id
        for link in _sorted_workspace_pinned_links(workspace)
    ]


def _workspace_circular_summaries(workspace: ResearchWorkspace) -> list[dict]:
    return [
        _circular_summary(link.circular)
        for link in _sorted_workspace_pinned_links(workspace)
    ]


def _chat_session_payload(session: ChatSession) -> dict:
    return {
        "id": session.id,
        "title": session.title,
        "session_type": "chat",
        "created_at": _isoformat(session.created_at),
        "updated_at": _isoformat(session.updated_at or session.created_at),
    }


def _workspace_chat_session_payload(
    workspace: ResearchWorkspace,
    session: ChatSession | None = None,
) -> dict:
    return {
        "id": _workspace_chat_session_id(workspace.id),
        "title": workspace.name,
        "session_type": "workspace",
        "workspace_id": workspace.id,
        "is_default_workspace": bool(workspace.is_default),
        "pinned_count": len(list(workspace.pinned_circulars or [])),
        "circular_ids": _workspace_circular_ids(workspace),
        "created_at": _isoformat(session.created_at if session else workspace.created_at),
        "updated_at": _isoformat(
            (session.updated_at or session.created_at) if session else (workspace.updated_at or workspace.created_at)
        ),
    }


def _get_workspace_for_chat_session(
    db: Session,
    session_id: str | None,
) -> ResearchWorkspace | None:
    workspace_id = _workspace_id_from_chat_session(session_id)
    if not workspace_id:
        return None
    if workspace_id == DEFAULT_WORKSPACE_ID:
        return _ensure_default_workspace(db)
    return db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()


# --- Settings ---

def _settings_payload(config: AIConfig, embedding: EmbeddingConfig, db=None) -> dict:
    from ..llm_debug import debug_allowed, debug_setting_enabled

    ai_secret = AIConfig.secret_state(config.provider)
    embedding_secret = EmbeddingConfig.secret_state(embedding.provider)
    debug_is_allowed = debug_allowed()
    debug_is_enabled = debug_setting_enabled(db)
    return {
        "provider": config.provider,
        "base_url": config.base_url,
        "api_key": "",
        "api_key_configured": ai_secret["api_key_configured"],
        "api_key_env_var": ai_secret["api_key_env_var"],
        "model": config.model,
        "chat_model": config.chat_model,
        "max_context_tokens": config.max_context_tokens,
        "ai_provider": config.provider,
        "ai_base_url": config.base_url,
        "ai_api_key": "",
        "ai_model": config.model,
        "ai_chat_model": config.chat_model,
        "ai_max_context_tokens": config.max_context_tokens,
        "embedding_provider": embedding.provider,
        "embedding_model": embedding.model,
        "embedding_base_url": embedding.base_url,
        "embedding_api_key": "",
        "embedding_api_key_configured": embedding_secret["api_key_configured"],
        "embedding_api_key_env_var": embedding_secret["api_key_env_var"],
        "managed_env_file": str(managed_env_path().name),
        "llm_debug_allowed": debug_is_allowed,
        "llm_debug_enabled": debug_is_enabled,
        "llm_debug_effective": debug_is_allowed and debug_is_enabled,
    }


def _save_ai_secret(provider: str, api_key: str | None, clear_secret: bool) -> None:
    definition = get_provider_definition(provider)
    secret_state = AIConfig.secret_state(provider)
    target_env_var = definition.api_key_env_vars[0]
    if clear_secret:
        unset_managed_env_value(str(secret_state["api_key_env_var"]))
        return
    if api_key is None or not api_key.strip():
        return
    set_managed_env_value(target_env_var, api_key.strip())


def _save_embedding_secret(api_key: str | None, clear_secret: bool) -> None:
    target_env_var = "EMBEDDING_API_KEY"
    secret_state = EmbeddingConfig.secret_state()
    if clear_secret:
        unset_managed_env_value(str(secret_state["api_key_env_var"]))
        return
    if api_key is None or not api_key.strip():
        return
    set_managed_env_value(target_env_var, api_key.strip())
