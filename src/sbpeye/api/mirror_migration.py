"""Explicit administrator actions for online identity maintenance."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..auth_routes import require_admin
from .. import migration_console

router = APIRouter(prefix="/api/circulars/mirror/identity", dependencies=[Depends(require_admin)])


class AttachmentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    detection_url: str = Field(min_length=1, max_length=2000)


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_id: str = Field(min_length=1, max_length=100)
    source_text: str = Field(min_length=1, max_length=2_000_000)
    note: str = Field(min_length=10, max_length=5000)
    scopes: list[Literal["analysis", "dependents"]] = Field(default_factory=list, max_length=2)
    attachments: list[AttachmentEvidence] = Field(default_factory=list, max_length=100)


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: str = Field(min_length=64, max_length=64)


class RemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    removal_hash: str = Field(min_length=64, max_length=64)


def action(callback):
    try:
        callback()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return migration_console.console.status()


@router.get("")
def identity_status():
    return migration_console.console.status()


@router.get("/records/{old_id}")
def identity_record(old_id: str):
    try:
        return migration_console.console.record(old_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/prepare", status_code=202)
def prepare_identity():
    return action(migration_console.console.prepare)


@router.post("/review", status_code=202)
def review_identity(data: ReviewEvidence, user=Depends(require_admin)):
    if not data.source_text.strip() or not data.note.strip() or not (data.scopes or data.attachments):
        raise HTTPException(422, "Supply source evidence, a review note, and the items you verified.")
    return action(lambda: migration_console.console.review(
        data.old_id, data.source_text, data.note, data.scopes,
        [item.model_dump() for item in data.attachments], user.id,
    ))


@router.post("/apply", status_code=202)
def apply_identity(data: ApplyRequest):
    return action(lambda: migration_console.console.apply(data.manifest_hash))


@router.post("/cancel")
def cancel_identity():
    return action(migration_console.console.cancel)


@router.post("/remove", status_code=202)
def remove_legacy_circulars(data: RemoveRequest):
    return action(lambda: migration_console.console.remove(data.removal_hash))
