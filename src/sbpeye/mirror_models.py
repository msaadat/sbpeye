"""Mirror metadata lives in the corpus; importing registers fresh-install tables."""

from sqlalchemy import Column, String, Text, Integer, DateTime, Boolean, Index
from .orm_bases import Base


class IdentityAlias(Base):
    __tablename__ = "identity_alias"
    kind = Column(String, primary_key=True)
    old_id = Column(String, primary_key=True)
    new_id = Column(String, nullable=False)
    migration_id = Column(String, nullable=False)
    created_at = Column(Text, nullable=False)


class IdentityMigration(Base):
    __tablename__ = "identity_migration"
    id = Column(String, primary_key=True)
    manifest_hash = Column(String, nullable=False)
    phase = Column(String, nullable=False)
    manifest = Column(Text, nullable=False)


class MirrorAudit(Base):
    __tablename__ = "mirror_audit"
    id = Column(String, primary_key=True)
    status = Column(String, nullable=False, index=True)
    identity_version = Column(Integer, nullable=False, default=2)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    local_observed_at = Column(DateTime)
    parameters = Column(Text, nullable=False, default="{}")
    error_code = Column(String)
    error = Column(Text)
    pages_total = Column(Integer, default=0)
    pages_completed = Column(Integer, default=0)
    pages_failed = Column(Integer, default=0)
    raw_total = Column(Integer, default=0)
    distinct_total = Column(Integer, default=0)
    local_total = Column(Integer, default=0)
    counts = Column(Text, default="{}")
    coverage = Column(Text, default="{}")
    diagnostics = Column(Text, default="[]")


class MirrorAuditItem(Base):
    __tablename__ = "mirror_audit_item"
    audit_id = Column(String, primary_key=True)
    item_key = Column(String, primary_key=True)
    bucket = Column(String, nullable=False)
    identity = Column(String, index=True)
    matched_id = Column(String)
    department = Column(String)
    year = Column(Integer)
    sort_date = Column(DateTime)
    descriptor = Column(Text, nullable=False)
    variants = Column(Text, default="[]")
    evidence = Column(Text, default="{}")
    __table_args__ = (Index("ix_mirror_findings_bucket", "audit_id", "bucket"),)


class MirrorGap(Base):
    __tablename__ = "mirror_gap"
    id = Column(String, primary_key=True)
    identity_version = Column(Integer, nullable=False, default=2)
    descriptor = Column(Text, nullable=False)
    variants = Column(Text, nullable=False, default="[]")
    department = Column(String)
    year = Column(Integer)
    sort_date = Column(DateTime)
    raw_date = Column(String)
    status = Column(String, nullable=False, default="pending")
    first_seen_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    last_audit_id = Column(String, index=True)
    eligible = Column(Boolean, nullable=False, default=True)
    eligibility_reason = Column(String)
    eligibility_checked_at = Column(DateTime)
    eligibility_source = Column(String)
    resolution_id = Column(String)
    resolved_at = Column(DateTime)
    attempts = Column(Integer, nullable=False, default=0)
    failures_since_requeue = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime)
    last_error_stage = Column(String)
    last_error = Column(Text)
    skip_reason = Column(Text)
    active_attempt_id = Column(String, unique=True)
    __table_args__ = (
        Index("ix_mirror_gap_selection", "status", "year", "sort_date", "id"),
        Index("ix_mirror_gap_failures", "status", "failures_since_requeue", "id"),
    )


class MirrorAttempt(Base):
    __tablename__ = "mirror_attempt"
    id = Column(String, primary_key=True)
    gap_id = Column(String, index=True)
    job_id = Column(String, nullable=False, index=True)
    circular_identity = Column(String, nullable=False)
    origin = Column(String, nullable=False)
    descriptor = Column(Text, nullable=False)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    outcome = Column(String, nullable=False)
    stages = Column(Text, nullable=False, default="{}")
    error_stage = Column(String)
    error_type = Column(String)
    error = Column(Text)
    __table_args__ = (Index("uq_mirror_attempt_job_identity", "job_id", "circular_identity", unique=True),)
