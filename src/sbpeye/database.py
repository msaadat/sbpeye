import chromadb
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .embeddings import EmbeddingConfig, create_embedding_backend

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQLALCHEMY_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'sbpeye.db'}"
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
    return collection.count() > 0


def reset_collection():
    """Drop and recreate the shared collection, rebinding every module-level handle.

    Dropping the collection is the only cheap way to reset it: deleting its records
    one batch at a time makes Chroma tombstone and recompact a 100 MB+ HNSW segment,
    which is both slow and the operation most likely to leave the segment out of sync
    with the SQLite metadata.

    The catch is that ``search``, ``chat_retrieval``, and ``scraper.circulars`` bind
    ``collection`` at import, so a naive drop leaves them writing through a dead handle —
    which is how a rebuild silently lost the law arm. Rebinding them here keeps the drop
    safe. Callers must hold the only open handle on the store: Chroma's PersistentClient
    is single-process, and a second process (a running dev server) writing at the same
    time is what corrupts it.
    """
    global collection
    import sys

    try:
        chroma_client.delete_collection("circulars")
    except Exception:
        pass
    collection = chroma_client.get_or_create_collection(
        name="circulars", embedding_function=None
    )
    for module_name in (
        "sbpeye.search",
        "sbpeye.chat_retrieval",
        "sbpeye.scraper.circulars",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "collection"):
            module.collection = collection
    return collection


def _ensure_columns(bind=None):
    with (bind or engine).begin() as conn:
        insp = inspect(conn)
        table_names = insp.get_table_names()
        if "circulars" in table_names:
            existing = {c["name"] for c in insp.get_columns("circulars")}
            new_columns = [
                ("new_url", "VARCHAR"),
                ("old_url", "VARCHAR"),
                ("indexed_at", "DATETIME"),
                ("summary", "TEXT"),
                ("tags", "TEXT"),
                ("compliance_checklist", "TEXT"),
                ("status", "VARCHAR(20) DEFAULT 'active'"),
                ("summary_generated_at", "DATETIME"),
                ("tags_generated_at", "DATETIME"),
                ("checklist_generated_at", "DATETIME"),
                ("relationships_generated_at", "DATETIME"),
                ("attachments_scanned_at", "DATETIME"),
                ("entities_generated_at", "DATETIME"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE circulars ADD COLUMN {col_name} {col_type}"))

            # Existing rows predate indexed_at tracking; backfill with the publication
            # date as the closest available approximation (real scrape time is lost).
            conn.execute(text(
                "UPDATE circulars SET indexed_at = date WHERE indexed_at IS NULL"
            ))

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

        if "sync_status" in table_names:
            existing = {c["name"] for c in insp.get_columns("sync_status")}
            new_columns = [
                ("job_id", "VARCHAR"),
                ("kind", "VARCHAR"),
                ("started_at", "DATETIME"),
                ("completed_at", "DATETIME"),
                ("error", "TEXT"),
                ("parameters", "TEXT"),
                ("processed_count", "INTEGER"),
                ("skipped_count", "INTEGER"),
                ("error_count", "INTEGER"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE sync_status ADD COLUMN {col_name} {col_type}"
                    ))

        if "research_workspaces" in table_names:
            existing = {c["name"] for c in insp.get_columns("research_workspaces")}
            if "is_default" not in existing:
                conn.execute(text(
                    "ALTER TABLE research_workspaces ADD COLUMN is_default INTEGER DEFAULT 0"
                ))

        # Laws & regulations (see docs/LAWS_REGULATIONS_PLAN.md). The tables themselves
        # come from create_all; these blocks add columns to databases created by an
        # earlier build, the same self-healing pattern used for circulars above.
        reg_columns = {
            "reg_documents": [
                ("title", "VARCHAR"),
                ("normalized_title", "VARCHAR"),
                ("doc_type", "VARCHAR"),
                ("source_url", "VARCHAR"),
                ("page_slug", "VARCHAR"),
                ("parent_id", "VARCHAR"),
                ("part_label", "VARCHAR"),
                ("part_order", "INTEGER"),
                ("circular_id", "VARCHAR"),
                ("is_external", "INTEGER DEFAULT 0"),
                ("listed_date", "DATETIME"),
                ("first_seen_at", "DATETIME"),
                ("last_seen_at", "DATETIME"),
                ("delisted_at", "DATETIME"),
                ("refetch_requested", "INTEGER DEFAULT 0"),
                ("summary", "TEXT"),
                ("tags", "TEXT"),
                ("summary_generated_at", "DATETIME"),
                ("tags_generated_at", "DATETIME"),
            ],
            "reg_document_versions": [
                ("document_id", "VARCHAR"),
                ("content_hash", "VARCHAR"),
                ("file_url", "VARCHAR"),
                ("local_path", "VARCHAR"),
                ("file_type", "VARCHAR"),
                ("version_label", "VARCHAR"),
                ("content_text", "TEXT"),
                ("extraction_status", "VARCHAR DEFAULT 'pending'"),
                ("extraction_error", "TEXT"),
                ("is_vectorized", "INTEGER DEFAULT 0"),
                ("is_current", "INTEGER DEFAULT 1"),
                ("effective_from", "DATETIME"),
                ("first_seen_at", "DATETIME"),
                ("last_seen_at", "DATETIME"),
                ("source", "VARCHAR DEFAULT 'live'"),
            ],
            "reg_document_links": [
                ("circular_id", "VARCHAR"),
                ("document_id", "VARCHAR"),
                ("link_type", "VARCHAR"),
                ("detected_via", "VARCHAR"),
                ("confidence", "FLOAT"),
                ("created_at", "DATETIME"),
            ],
        }
        for table_name, columns in reg_columns.items():
            if table_name not in table_names:
                continue
            existing = {c["name"] for c in insp.get_columns(table_name)}
            for col_name, col_type in columns:
                if col_name not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                    ))

        # Persistent FTS5 lexical index for keyword search (see search.py). Rows are
        # maintained application-side (cells hold tokenize() output); this just ensures
        # the virtual table exists. Backfill happens lazily on first server start.
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS circulars_fts USING fts5("
            "circular_id UNINDEXED, title, reference, body, tokenize='unicode61')"
        ))
        # Laws & regulations keep their own FTS table: different columns (a law has a
        # part label, not a reference), and nothing about circular search can regress.
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS laws_fts USING fts5("
            "document_id UNINDEXED, title, part_label, body, tokenize='unicode61')"
        ))

        # Semantic index ledger (see docs/INVENTORY_SEARCH_PLAN.md section 10). Created
        # here rather than left to create_all: this module runs create_all before
        # models.py has registered anything, so a CLI-only process would never get it.
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS semantic_index_sources ("
            "id VARCHAR PRIMARY KEY, "
            "source_kind VARCHAR NOT NULL, "
            "source_id VARCHAR NOT NULL, "
            "logical_kind VARCHAR NOT NULL, "
            "logical_document_id VARCHAR NOT NULL, "
            "version_id VARCHAR, "
            "content_hash VARCHAR, "
            "chunker_version VARCHAR, "
            "embedding_fingerprint VARCHAR, "
            "expected_chunks INTEGER NOT NULL DEFAULT 0, "
            "indexed_chunks INTEGER NOT NULL DEFAULT 0, "
            "status VARCHAR NOT NULL DEFAULT 'stale', "
            "error TEXT, "
            "indexed_at DATETIME, "
            "CONSTRAINT uq_semantic_index_source UNIQUE (source_kind, source_id))"
        ))
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_sis_logical ON semantic_index_sources "
            "(logical_kind, logical_document_id)",
            "CREATE INDEX IF NOT EXISTS ix_sis_status ON semantic_index_sources (status)",
            "CREATE INDEX IF NOT EXISTS ix_sis_source ON semantic_index_sources "
            "(source_kind, source_id)",
        ):
            conn.execute(text(statement))

Base.metadata.create_all(bind=engine)
_ensure_columns()
