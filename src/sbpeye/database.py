import chromadb
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .embeddings import EmbeddingConfig, create_embedding_backend

SQLALCHEMY_DATABASE_URL = "sqlite:///./sbpeye.db"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHROMA_DB_DIR = PROJECT_ROOT / "chroma_db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

embedding_config = EmbeddingConfig.from_database(engine)
embedding_backend = create_embedding_backend(embedding_config)

chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))

try:
    collection = chroma_client.get_or_create_collection(name="circulars", embedding_function=None)
except ValueError:
    collection = chroma_client.get_collection(name="circulars", embedding_function=None)


def has_vector_store_data() -> bool:
    return (CHROMA_DB_DIR / "chroma.sqlite3").exists()


def _ensure_columns():
    insp = inspect(engine)
    table_names = insp.get_table_names()
    with engine.begin() as conn:
        if "circulars" in table_names:
            existing = {c["name"] for c in insp.get_columns("circulars")}
            new_columns = [
                ("summary", "TEXT"),
                ("tags", "TEXT"),
                ("compliance_checklist", "TEXT"),
                ("status", "VARCHAR(20) DEFAULT 'active'"),
                ("summary_generated_at", "DATETIME"),
                ("tags_generated_at", "DATETIME"),
                ("checklist_generated_at", "DATETIME"),
                ("relationships_generated_at", "DATETIME"),
                ("attachments_scanned_at", "DATETIME"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE circulars ADD COLUMN {col_name} {col_type}"))

            # Existing stored output predates generation tracking. Backfill it once so
            # the frontend correctly presents those actions as regeneration.
            conn.execute(text(
                "UPDATE circulars SET summary_generated_at = CURRENT_TIMESTAMP "
                "WHERE summary_generated_at IS NULL AND summary IS NOT NULL AND summary != ''"
            ))
            conn.execute(text(
                "UPDATE circulars SET tags_generated_at = CURRENT_TIMESTAMP "
                "WHERE tags_generated_at IS NULL AND tags IS NOT NULL AND tags != ''"
            ))
            conn.execute(text(
                "UPDATE circulars SET checklist_generated_at = CURRENT_TIMESTAMP "
                "WHERE checklist_generated_at IS NULL AND compliance_checklist IS NOT NULL "
                "AND compliance_checklist != ''"
            ))
            if "circular_relationships" in table_names:
                conn.execute(text(
                    "UPDATE circulars SET relationships_generated_at = CURRENT_TIMESTAMP "
                    "WHERE relationships_generated_at IS NULL AND id IN "
                    "(SELECT DISTINCT source_id FROM circular_relationships)"
                ))

        if "circular_relationships" in table_names:
            existing = {c["name"] for c in insp.get_columns("circular_relationships")}
            new_columns = [
                ("target_reference", "TEXT"),
                ("confidence", "FLOAT"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE circular_relationships ADD COLUMN {col_name} {col_type}"))

        if "attachments" in table_names:
            existing = {c["name"] for c in insp.get_columns("attachments")}
            new_columns = [
                ("extraction_status", "VARCHAR DEFAULT 'pending'"),
                ("extraction_error", "TEXT"),
                ("is_vectorized", "INTEGER DEFAULT 0"),
                ("created_at", "DATETIME"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE attachments ADD COLUMN {col_name} {col_type}"
                    ))

        if "chat_sessions" in table_names:
            existing = {c["name"] for c in insp.get_columns("chat_sessions")}
            if "circular_ids" not in existing:
                conn.execute(text(
                    "ALTER TABLE chat_sessions ADD COLUMN circular_ids TEXT"
                ))
            if "updated_at" not in existing:
                conn.execute(text(
                    "ALTER TABLE chat_sessions ADD COLUMN updated_at DATETIME"
                ))
                conn.execute(text(
                    "UPDATE chat_sessions SET updated_at = created_at "
                    "WHERE updated_at IS NULL"
                ))

        if "ai_generation_jobs" in table_names:
            existing = {c["name"] for c in insp.get_columns("ai_generation_jobs")}
            new_columns = [
                ("progress_total", "INTEGER DEFAULT 0"),
                ("progress_completed", "INTEGER DEFAULT 0"),
                ("result_status", "VARCHAR"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE ai_generation_jobs ADD COLUMN {col_name} {col_type}"
                    ))

Base.metadata.create_all(bind=engine)
_ensure_columns()
