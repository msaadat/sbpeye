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
    ResearchWorkspace,
    WorkspaceCircular,
)

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


def _document_payload(attachment: Attachment | CachedDocument) -> dict:
    from ..database import PROJECT_ROOT

    local_path = PROJECT_ROOT / attachment.local_path if attachment.local_path else None
    return {
        "id": attachment.id,
        "circular_id": getattr(attachment, "circular_id", None),
        "filename": attachment.filename,
        "file_type": attachment.file_type,
        "original_url": attachment.original_url,
        "cached": bool(local_path and local_path.is_file()),
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

def _settings_payload(config: AIConfig, embedding: EmbeddingConfig) -> dict:
    ai_secret = AIConfig.secret_state(config.provider)
    embedding_secret = EmbeddingConfig.secret_state(embedding.provider)
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
