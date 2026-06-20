from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base

class Circular(Base):
    __tablename__ = "circulars"

    id = Column(String, primary_key=True, index=True)
    reference = Column(String)
    title = Column(String, nullable=False)
    department = Column(String)
    date = Column(DateTime)
    url = Column(String)
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

    amends = relationship("CircularRelationship", foreign_keys="[CircularRelationship.source_id]", back_populates="source")
    amended_by = relationship("CircularRelationship", foreign_keys="[CircularRelationship.target_id]", back_populates="target")
    attachments = relationship(
        "Attachment", back_populates="circular", cascade="all, delete-orphan"
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
    __tablename__ = "ai_generation_jobs"

    id = Column(String, primary_key=True)
    circular_id = Column(String, ForeignKey("circulars.id"), nullable=False, index=True)
    feature = Column(String, nullable=False)
    status = Column(String, nullable=False, default="queued", index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

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
    last_sync_date = Column(DateTime)
    status = Column(String)
    ecodata_index_time = Column(DateTime, nullable=True)

class Settings(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(Text)

class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    messages = relationship("ChatMessage", back_populates="session", order_by="ChatMessage.created_at")

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True)
    session_id = Column(String, ForeignKey("chat_sessions.id"), index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    circular_ids = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")
