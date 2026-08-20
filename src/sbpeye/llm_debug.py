"""Fail-open, persistent diagnostics for text-generation operations.

The module intentionally has no dependency on :mod:`sbpeye.ai`: the AI provider
gateway imports it, while each recorder write uses its own short database session.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime
import json
import logging
import os
import re
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

# `AppSessionLocal` is the runtime-state database and is used for exactly one thing
# here: reading the `llm_debug_enabled` setting, which lives with the rest of the
# settings. Every trace write goes to `DebugSessionLocal` — a different file entirely,
# and neither of them is the corpus.
from .database import AppSessionLocal, DebugBase, DebugSessionLocal, debug_engine
from .models import LLMTrace, LLMTraceEvent, Settings

logger = logging.getLogger(__name__)

# The recorder creates its own tables. This module is the first place where both the
# debug engine and the trace models are in scope, and every path that writes a trace
# imports it, so there is no ordering left to get wrong.
DebugBase.metadata.create_all(bind=debug_engine)

_FALSE_VALUES = {"0", "false", "no", "off"}
_SECRET_KEYS = {
    "authorization", "proxy_authorization", "api_key", "x_api_key",
    "access_token", "refresh_token", "client_secret", "password",
}
_URL_SECRET_KEYS = _SECRET_KEYS | {"key", "token", "secret"}
_DIAGNOSTIC_SECRET_RE = re.compile(
    r"(?i)(authorization|proxy[_-]authorization|api[_-]key|x[_-]api[_-]key|"
    r"access[_-]token|refresh[_-]token|client[_-]secret|password)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
_DIAGNOSTIC_URL_RE = re.compile(r"https?://[^\s\]\[\)\(<>\"']+")
_locks_guard = threading.Lock()
_trace_locks: dict[str, threading.RLock] = {}


@dataclass(frozen=True)
class TraceHandle:
    trace_id: str
    enabled: bool
    origin: str
    operation: str
    stage: str | None = None
    started_monotonic: float = 0.0


_current_trace: ContextVar[TraceHandle | None] = ContextVar(
    "sbpeye_llm_trace", default=None
)

_T = TypeVar("_T")
_EXHAUSTED = object()


def _reset_trace(token: Token | None) -> None:
    """Restore the previous handle, tolerating a token from a dead context.

    Diagnostics must never be the reason a request fails. A generator that was
    not wrapped in :func:`bind_context` can be resumed under a context the token
    does not belong to; clearing the variable is the correct approximation there,
    since the context being unwound is about to be discarded anyway.
    """
    if token is None:
        return
    try:
        _current_trace.reset(token)
    except ValueError:
        _current_trace.set(None)


def bind_context(iterable: Iterable[_T]) -> Iterator[_T]:
    """Step an iterator inside one captured context for its whole lifetime.

    A generator owns no context of its own (PEP 567): it runs in whichever
    context resumes it. Under a ``StreamingResponse`` each ``next()`` is
    dispatched to an anyio worker thread holding its own copy of the request
    context, so a ``ContextVar`` set while producing one chunk is invisible while
    producing the next. For tracing — whose whole notion of "the current trace"
    lives in a ContextVar — that meant the handle opened on the first chunk was
    gone by the second: nested ``trace_operation`` calls saw no parent and opened
    orphan traces with no chat session on them, later ``emit_event`` calls found
    nothing active and dropped the payloads, and the closing ``reset`` raised
    ``ValueError`` out of the route. Pinning every resumption to the same context
    object makes the trace opened in the first chunk still current in the last.

    Wrap the outermost generator of a streaming response; everything it drives
    inherits the pinned context.
    """
    iterator = iter(iterable)
    context = copy_context()

    def step() -> Any:
        # StopIteration must not cross ``Context.run`` and escape this generator:
        # PEP 479 would turn it into a RuntimeError instead of a clean stop.
        try:
            return next(iterator)
        except StopIteration:
            return _EXHAUSTED

    try:
        while True:
            item = context.run(step)
            if item is _EXHAUSTED:
                return
            yield item
    finally:
        # Close the wrapped generator inside the pinned context too, so its
        # cleanup — the ``finally`` that closes a trace on client disconnect —
        # still sees the handle it opened.
        close = getattr(iterator, "close", None)
        if close is not None:
            context.run(close)


def debug_allowed() -> bool:
    return os.getenv("LLM_DEBUG_ALLOWED", "true").strip().lower() not in _FALSE_VALUES


def debug_setting_enabled(db=None) -> bool:
    owns_session = db is None
    session = db or AppSessionLocal()  # app DB: Settings lives there
    try:
        row = session.query(Settings).filter(Settings.key == "llm_debug_enabled").first()
        return bool(row and str(row.value).strip().lower() in {"1", "true", "yes", "on"})
    except Exception:
        logger.warning("LLM trace setting could not be read", exc_info=True)
        return False
    finally:
        if owns_session:
            session.close()


def debug_enabled(db=None) -> bool:
    return debug_allowed() and debug_setting_enabled(db)


def current_trace() -> TraceHandle | None:
    handle = _current_trace.get()
    return handle if handle and handle.enabled else None


def _key_name(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([
            (key, "[REDACTED]" if _key_name(key) in _URL_SECRET_KEYS else item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        ])
        return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
    except Exception:
        return value


def _sanitize_diagnostic_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = _DIAGNOSTIC_URL_RE.sub(lambda match: _safe_url(match.group(0)), value)
    return _DIAGNOSTIC_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
    )


def to_jsonable(value: Any, _seen: set[int] | None = None) -> Any:
    """Convert SDK/Pydantic/domain values without losing prompt-sized strings."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return {"type": type(value).__name__, "repr": "<recursive>"}
    seen.add(identity)
    try:
        if is_dataclass(value):
            return to_jsonable(asdict(value), seen)
        if hasattr(value, "model_dump"):
            try:
                return to_jsonable(value.model_dump(mode="json"), seen)
            except TypeError:
                return to_jsonable(value.model_dump(), seen)
        if isinstance(value, Mapping):
            return {str(key): to_jsonable(item, seen) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [to_jsonable(item, seen) for item in value]
        if hasattr(value, "__dict__"):
            return {
                str(key): to_jsonable(item, seen)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
        rendered = repr(value)
        return {"type": type(value).__name__, "repr": rendered[:2000]}
    finally:
        seen.discard(identity)


def sanitize(value: Any, *, parent_key: str | None = None) -> Any:
    converted = to_jsonable(value)
    if isinstance(converted, dict):
        clean: dict[str, Any] = {}
        for key, item in converted.items():
            normalized = _key_name(key)
            if normalized in {"headers", "http_headers", "request_headers", "response_headers"}:
                continue
            clean[key] = "[REDACTED]" if normalized in _SECRET_KEYS else sanitize(
                item, parent_key=normalized
            )
        return clean
    if isinstance(converted, list):
        return [sanitize(item, parent_key=parent_key) for item in converted]
    if isinstance(converted, str):
        if parent_key in {"url", "request_url", "base_url"}:
            return _safe_url(converted)
        if parent_key in {"body", "provider_body"}:
            try:
                return sanitize(json.loads(converted), parent_key=parent_key)
            except (TypeError, ValueError):
                pass
    return converted


def serialize_payload(value: Any) -> tuple[str, int]:
    text = json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))
    return text, len(text.encode("utf-8"))


def _trace_lock(trace_id: str) -> threading.RLock:
    with _locks_guard:
        return _trace_locks.setdefault(trace_id, threading.RLock())


def _release_trace_lock(trace_id: str) -> None:
    with _locks_guard:
        _trace_locks.pop(trace_id, None)


def _create_trace(
    operation: str,
    origin: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    job_id: str | None = None,
    chat_session_id: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    command_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TraceHandle | None:
    if not debug_enabled():
        return None
    trace_id = str(uuid.uuid4())
    now = datetime.utcnow()
    try:
        metadata_json, _ = serialize_payload(metadata or {})
    except Exception:
        logger.warning("LLM trace metadata could not be serialized", exc_info=True)
        return None
    db = DebugSessionLocal()
    try:
        db.add(LLMTrace(
            id=trace_id, operation=operation, origin=origin, status="running",
            provider=provider, model=model, job_id=job_id,
            chat_session_id=chat_session_id, target_kind=target_kind,
            target_id=target_id, command_name=command_name,
            metadata_json=metadata_json, started_at=now, updated_at=now,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("LLM trace could not be started", exc_info=True)
        return None
    finally:
        db.close()
    handle = TraceHandle(trace_id, True, origin, operation, None, time.monotonic())
    emit_event("operation_started", {
        "origin": origin, "operation": operation, "provider": provider,
        "model": model, "job_id": job_id, "chat_session_id": chat_session_id,
        "target_kind": target_kind, "target_id": target_id,
        "command_name": command_name, "metadata": metadata or {},
    }, handle=handle)
    return handle


def emit_event(
    kind: str,
    payload: Any,
    *,
    stage: str | None = None,
    attempt_id: str | None = None,
    attempt_number: int | None = None,
    elapsed_ms: int | None = None,
    handle: TraceHandle | None = None,
) -> int | None:
    active = handle or current_trace()
    if not active or not active.enabled:
        return None
    try:
        payload_json, payload_bytes = serialize_payload(payload)
    except Exception:
        logger.warning("LLM trace event could not be serialized", exc_info=True)
        return None
    lock = _trace_lock(active.trace_id)
    with lock:
        for retry in range(3):
            db = DebugSessionLocal()
            try:
                trace = db.query(LLMTrace).filter(LLMTrace.id == active.trace_id).first()
                if trace is None:
                    return None
                sequence = int(db.query(func.max(LLMTraceEvent.sequence)).filter(
                    LLMTraceEvent.trace_id == active.trace_id
                ).scalar() or 0) + 1
                resolved_attempt_number = (
                    attempt_number
                    if attempt_number is not None
                    else ((trace.attempt_count or 0) + 1 if kind == "provider_request" else None)
                )
                event = LLMTraceEvent(
                    trace_id=active.trace_id, sequence=sequence, kind=kind,
                    stage=stage or active.stage, attempt_id=attempt_id,
                    attempt_number=resolved_attempt_number, payload_json=payload_json,
                    payload_bytes=payload_bytes, created_at=datetime.utcnow(),
                    elapsed_ms=elapsed_ms,
                )
                db.add(event)
                trace.payload_bytes = (trace.payload_bytes or 0) + payload_bytes
                trace.updated_at = datetime.utcnow()
                if kind == "provider_request":
                    trace.attempt_count = (trace.attempt_count or 0) + 1
                    trace.provider = trace.provider or str(payload.get("provider") or "") or None
                    kwargs = payload.get("kwargs", {}) if isinstance(payload, dict) else {}
                    trace.model = trace.model or str(kwargs.get("model") or "") or None
                if kind == "provider_response" and isinstance(payload, dict):
                    usage = payload.get("usage") or {}
                    for attr, key in (
                        ("prompt_tokens", "prompt_tokens"),
                        ("completion_tokens", "completion_tokens"),
                        ("total_tokens", "total_tokens"),
                    ):
                        value = usage.get(key) if isinstance(usage, dict) else None
                        if isinstance(value, int):
                            setattr(trace, attr, (getattr(trace, attr) or 0) + value)
                db.commit()
                return resolved_attempt_number if kind == "provider_request" else sequence
            except (IntegrityError, OperationalError):
                db.rollback()
                if retry == 2:
                    logger.warning("LLM trace event could not be recorded", exc_info=True)
                else:
                    time.sleep(0.01 * (retry + 1))
            except Exception:
                db.rollback()
                logger.warning("LLM trace event could not be recorded", exc_info=True)
                return None
            finally:
                db.close()
    return None


def finish_trace(
    handle: TraceHandle | None,
    status: str = "succeeded",
    error: BaseException | None = None,
) -> None:
    if not handle or not handle.enabled:
        return
    state_db = DebugSessionLocal()
    try:
        existing_status = state_db.query(LLMTrace.status).filter(
            LLMTrace.id == handle.trace_id
        ).scalar()
        if existing_status in {"succeeded", "failed", "cancelled"}:
            return
    except Exception:
        # The normal completion path below remains fail-open and will make its own
        # best effort; this preliminary guard only prevents a duplicate terminal.
        pass
    finally:
        state_db.close()
    duration_ms = max(0, round((time.monotonic() - handle.started_monotonic) * 1000))
    kind = {
        "succeeded": "operation_completed", "failed": "operation_failed",
        "cancelled": "operation_cancelled",
    }.get(status, "operation_failed")
    error_payload = exception_payload(error) if error else None
    emit_event(kind, {
        "status": status, "duration_ms": duration_ms,
        "error": error_payload,
    }, elapsed_ms=duration_ms, handle=handle)
    db = DebugSessionLocal()
    try:
        trace = db.query(LLMTrace).filter(LLMTrace.id == handle.trace_id).first()
        if trace:
            trace.status = status
            trace.updated_at = datetime.utcnow()
            trace.completed_at = trace.updated_at
            trace.duration_ms = duration_ms
            if error:
                trace.error_type = type(error).__name__
                trace.error_message = _sanitize_diagnostic_text(str(error))
            db.commit()
    except Exception:
        db.rollback()
        logger.warning("LLM trace could not be completed", exc_info=True)
    finally:
        db.close()
        _release_trace_lock(handle.trace_id)


@contextmanager
def trace_operation(operation: str, origin: str, **kwargs: Any) -> Iterator[TraceHandle | None]:
    existing = current_trace()
    if existing:
        yield existing
        return
    handle = _create_trace(operation, origin, **kwargs)
    token: Token | None = _current_trace.set(handle) if handle else None
    try:
        yield handle
    except (GeneratorExit, KeyboardInterrupt) as exc:
        finish_trace(handle, "cancelled", exc)
        raise
    except BaseException as exc:
        finish_trace(handle, "failed", exc)
        raise
    else:
        finish_trace(handle, "succeeded")
    finally:
        _reset_trace(token)


@contextmanager
def trace_span(stage: str, metadata: Mapping[str, Any] | None = None):
    active = current_trace()
    if not active:
        yield None
        return
    child = replace(active, stage=stage)
    token = _current_trace.set(child)
    if metadata:
        emit_event("context", dict(metadata), stage=stage, handle=child)
    try:
        yield child
    finally:
        _reset_trace(token)


def ensure_implicit_trace(provider: str | None = None, model: str | None = None):
    """Return ``(handle, owns_trace, context_token)`` for the provider gateway."""
    active = current_trace()
    if active:
        return active, False, None
    handle = _create_trace(
        "implicit.completion", "implicit", provider=provider, model=model
    )
    token = _current_trace.set(handle) if handle else None
    return handle, bool(handle), token


def close_implicit_trace(
    handle: TraceHandle | None,
    owns_trace: bool,
    token: Token | None,
    *,
    error: BaseException | None = None,
    cancelled: bool = False,
) -> None:
    if owns_trace:
        finish_trace(handle, "cancelled" if cancelled else ("failed" if error else "succeeded"), error)
        _reset_trace(token)


def run_in_copied_context(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one worker in its own fresh context snapshot."""
    return copy_context().run(function, *args, **kwargs)


def exception_payload(exc: BaseException | None) -> dict[str, Any]:
    if exc is None:
        return {}
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    body = getattr(exc, "body", None)
    if body is None and response is not None:
        body = getattr(response, "text", None)
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None) if request is not None else None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            body = _sanitize_diagnostic_text(body)
    return sanitize({
        "type": type(exc).__name__, "message": _sanitize_diagnostic_text(str(exc)),
        "status": status, "body": body,
        "request_url": str(url) if url else None,
        "traceback": _sanitize_diagnostic_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        ),
    })


def fail_interrupted_traces() -> int:
    """Close rows which cannot still be active after a server restart."""
    db = DebugSessionLocal()
    try:
        rows = db.query(LLMTrace).filter(LLMTrace.status == "running").all()
        ids = [row.id for row in rows]
    finally:
        db.close()
    for trace_id in ids:
        handle = TraceHandle(trace_id, True, "implicit", "implicit.completion", started_monotonic=time.monotonic())
        error = RuntimeError("Trace was interrupted by a server restart.")
        emit_event("operation_failed", {
            "status": "failed", "reason": "interrupted_by_restart",
            "error": exception_payload(error),
        }, handle=handle)
        db = DebugSessionLocal()
        try:
            row = db.query(LLMTrace).filter(LLMTrace.id == trace_id, LLMTrace.status == "running").first()
            if row:
                row.status = "failed"
                row.error_type = "InterruptedByRestart"
                row.error_message = str(error)
                row.completed_at = datetime.utcnow()
                row.updated_at = row.completed_at
                db.commit()
        except Exception:
            db.rollback()
            logger.warning("Interrupted LLM trace could not be repaired", exc_info=True)
        finally:
            db.close()
    return len(ids)
