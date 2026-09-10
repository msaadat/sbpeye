"""Validated transport-neutral mirror requests."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    workers: int = Field(default=4, ge=1, le=8)
    delay: float = Field(default=0.5, ge=0, le=10)


class BackfillRequest(AuditRequest):
    workers: int = Field(default=2, ge=1, le=8)
    size: int = Field(default=50, ge=1, le=500)
    statuses: list[Literal["pending", "failed"]] = Field(default_factory=lambda: ["pending"])
    years: list[Annotated[int, Field(ge=1000, le=9999)]] = Field(default_factory=list)
    departments: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list)
    order: Literal["newest_first", "oldest_first", "fewest_attempts"] = "newest_first"
    max_attempts: int = Field(default=3, ge=1, le=20)
    include_attachments: StrictBool = False
    repeat_until_done: StrictBool = False
    llm_features: list[Literal["summary", "tags", "checklist", "relationships", "entities", "consolidation"]] = Field(default_factory=list)


class SkipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=1000)
