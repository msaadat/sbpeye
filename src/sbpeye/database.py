import chromadb
import logging
import os
from pathlib import Path
from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .embeddings import EmbeddingConfig, create_embedding_backend

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SQLALCHEMY_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'sbpeye.db'}"
CHROMA_DB_DIR = PROJECT_ROOT / "chroma_db"

# LLM traces live in their own file. They are diagnostics, not application data:
# high-churn, disposable, and an order of magnitude larger per row than anything
# else here (payload blobs of whole prompts). Keeping them in `sbpeye.db` meant
# every backup, copy and VACUUM of the corpus carried tens of megabytes of debug
# output, and a stray recorder write could touch the file the app serves from.
DEBUG_DATABASE_PATH = Path(
    os.getenv("SBPEYE_DEBUG_DB") or (PROJECT_ROOT / "sbpeye_debug.db")
)
DEBUG_DATABASE_URL = f"sqlite:///{DEBUG_DATABASE_PATH}"

# Runtime state lives in its own file, for the mirror-image reason traces do. The
# corpus above is generated once, shared by every user and shipped with the build;
# workspaces, chat and settings are created by whoever is using the app and have to
# survive a deployment that replaces the corpus. Keeping them in `sbpeye.db` meant
# the shipped file was rewritten on nearly every request — it showed permanently
# dirty in git — and any redeploy silently reverted whatever had been saved through
# the Settings UI.
APP_DATABASE_PATH = Path(
    os.getenv("SBPEYE_APP_DB") or (PROJECT_ROOT / "sbpeye_app.db")
)
APP_DATABASE_URL = f"sqlite:///{APP_DATABASE_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

debug_engine = create_engine(
    DEBUG_DATABASE_URL, connect_args={"check_same_thread": False}
)
DebugSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=debug_engine
)

app_engine = create_engine(
    APP_DATABASE_URL, connect_args={"check_same_thread": False}
)
AppSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=app_engine)

Base = declarative_base()
# A separate metadata, so `Base.metadata.create_all(engine)` can never recreate the
# trace tables inside the application database.
DebugBase = declarative_base()
# Likewise for runtime state: its own metadata keeps `create_all` on either of the
# other two bases from resurrecting these tables in the wrong file.
AppBase = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_debug_db():
    db = DebugSessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_app_db():
    db = AppSessionLocal()
    try:
        yield db
    finally:
        db.close()

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


def _rebuild_table(conn, table: str, columns_sql: str, carried: str) -> None:
    """Recreate `table` with a new definition, carrying `carried` columns across.

    SQLite cannot drop a NOT NULL constraint in place, and two tables here had to lose one
    when laws joined circulars as an analysis subject: `ai_generation_jobs.circular_id` and
    `circular_entities.circular_id`, both of which a law-sourced row leaves empty. This is
    the only shape of migration in this module that is not an ALTER, so it is written once.

    Rows are preserved. The insert names its columns explicitly rather than relying on
    positional order, so adding a column to the new definition can never silently shift
    existing data into the wrong field.
    """
    conn.execute(text(f"CREATE TABLE {table}_migrated ({columns_sql})"))
    conn.execute(text(
        f"INSERT INTO {table}_migrated ({carried}) SELECT {carried} FROM {table}"
    ))
    conn.execute(text(f"DROP TABLE {table}"))
    conn.execute(text(f"ALTER TABLE {table}_migrated RENAME TO {table}"))


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

        if "ai_generation_jobs" in table_names:
            columns = {c["name"]: c for c in insp.get_columns("ai_generation_jobs")}
            new_columns = [
                ("progress_total", "INTEGER DEFAULT 0"),
                ("progress_completed", "INTEGER DEFAULT 0"),
                ("result_status", "VARCHAR"),
                ("target_kind", "VARCHAR DEFAULT 'circular'"),
                ("document_id", "VARCHAR"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in columns:
                    conn.execute(text(
                        f"ALTER TABLE ai_generation_jobs ADD COLUMN {col_name} {col_type}"
                    ))
            # A law job has no circular, and the original table declared `circular_id
            # NOT NULL`. SQLite cannot drop a constraint in place, so the table is rebuilt
            # — the one migration here that is not an ALTER. Rows are carried across: they
            # are a work log, and 35 of them existed when this shipped.
            if "circular_id" in columns and not columns["circular_id"]["nullable"]:
                _rebuild_table(
                    conn,
                    "ai_generation_jobs",
                    "id VARCHAR NOT NULL PRIMARY KEY, "
                    "target_kind VARCHAR NOT NULL DEFAULT 'circular', "
                    "circular_id VARCHAR REFERENCES circulars (id), "
                    "document_id VARCHAR REFERENCES reg_documents (id), "
                    "feature VARCHAR NOT NULL, "
                    "status VARCHAR NOT NULL, "
                    "error TEXT, "
                    "progress_total INTEGER DEFAULT 0, "
                    "progress_completed INTEGER DEFAULT 0, "
                    "result_status VARCHAR, "
                    "created_at DATETIME NOT NULL, "
                    "started_at DATETIME, "
                    "completed_at DATETIME",
                    "id, target_kind, circular_id, document_id, feature, status, error, "
                    "progress_total, progress_completed, result_status, created_at, "
                    "started_at, completed_at",
                )
            conn.execute(text(
                "UPDATE ai_generation_jobs SET target_kind = 'circular' "
                "WHERE target_kind IS NULL"
            ))

        if "circular_entities" in table_names:
            columns = {c["name"]: c for c in insp.get_columns("circular_entities")}
            for col_name, col_type in (
                ("subject_kind", "VARCHAR DEFAULT 'circular'"),
                ("document_id", "VARCHAR"),
                ("version_id", "VARCHAR"),
            ):
                if col_name not in columns:
                    conn.execute(text(
                        f"ALTER TABLE circular_entities ADD COLUMN {col_name} {col_type}"
                    ))
            # A law-sourced value has no circular (LAWS_AI_PLAN.md §4.2).
            if "circular_id" in columns and not columns["circular_id"]["nullable"]:
                _rebuild_table(
                    conn,
                    "circular_entities",
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "subject_kind VARCHAR NOT NULL DEFAULT 'circular', "
                    "circular_id VARCHAR REFERENCES circulars (id), "
                    "document_id VARCHAR REFERENCES reg_documents (id), "
                    "version_id VARCHAR REFERENCES reg_document_versions (id), "
                    "entity_type VARCHAR NOT NULL, "
                    "metric VARCHAR, comparator VARCHAR, value_numeric FLOAT, "
                    "value_high FLOAT, unit VARCHAR, value_text VARCHAR, subject VARCHAR, "
                    "effective_date DATETIME, context_snippet TEXT, "
                    "source_unit_id VARCHAR, page_start INTEGER, confidence FLOAT, "
                    "created_at DATETIME NOT NULL",
                    "id, subject_kind, circular_id, document_id, version_id, entity_type, "
                    "metric, comparator, value_numeric, value_high, unit, value_text, "
                    "subject, effective_date, context_snippet, source_unit_id, "
                    "page_start, confidence, created_at",
                )
            conn.execute(text(
                "UPDATE circular_entities SET subject_kind = 'circular' "
                "WHERE subject_kind IS NULL"
            ))
            for statement in (
                "CREATE INDEX IF NOT EXISTS ix_entities_subject_kind "
                "ON circular_entities (subject_kind)",
                "CREATE INDEX IF NOT EXISTS ix_entities_document "
                "ON circular_entities (document_id)",
            ):
                conn.execute(text(statement))

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
                # AI analysis of this edition — see LAWS_AI_PLAN.md §4 for why it hangs
                # off the version rather than the document.
                ("summary", "TEXT"),
                ("tags", "TEXT"),
                ("compliance_checklist", "TEXT"),
                ("summary_generated_at", "DATETIME"),
                ("tags_generated_at", "DATETIME"),
                ("checklist_generated_at", "DATETIME"),
                ("entities_generated_at", "DATETIME"),
                ("relationships_generated_at", "DATETIME"),
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

        # Law → law edges (LAWS_AI_PLAN.md phase E). Created here rather than left to
        # create_all for the same reason as the ledger below: a CLI-only process runs
        # create_all before models.py has registered anything.
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS reg_document_relationships ("
            "id INTEGER PRIMARY KEY, "
            "source_document_id VARCHAR NOT NULL REFERENCES reg_documents (id), "
            "target_document_id VARCHAR REFERENCES reg_documents (id), "
            "target_reference VARCHAR, "
            "type VARCHAR, "
            "confidence FLOAT, "
            "detected_via VARCHAR, "
            "created_at DATETIME NOT NULL)"
        ))
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_reg_rel_source "
            "ON reg_document_relationships (source_document_id)",
            "CREATE INDEX IF NOT EXISTS ix_reg_rel_target "
            "ON reg_document_relationships (target_document_id)",
        ):
            conn.execute(text(statement))

        # LLM trace tables are deliberately absent here: they live in the debug
        # database now, and `llm_debug` creates them there on import. Recreating them
        # in this file would resurrect the empty shells `_relocate_trace_tables` drops.

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

def _ensure_app_columns(bind=None):
    """The `_ensure_columns` self-healing pass, for the runtime-state database.

    These two tables were created by an earlier build inside `sbpeye.db`, so the
    copies `_relocate_app_tables` carries across are built from that older DDL and
    can be missing columns the models now declare. Both blocks no-op on a database
    created fresh by `AppBase.metadata.create_all`, which already has them.
    """
    with (bind or app_engine).begin() as conn:
        insp = inspect(conn)
        table_names = insp.get_table_names()

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

        if "research_workspaces" in table_names:
            existing = {c["name"] for c in insp.get_columns("research_workspaces")}
            if "is_default" not in existing:
                conn.execute(text(
                    "ALTER TABLE research_workspaces ADD COLUMN is_default INTEGER DEFAULT 0"
                ))


_TRACE_TABLES = ("llm_traces", "llm_trace_events")
# Runtime state, moved out of `sbpeye.db` for the reasons given at APP_DATABASE_PATH.
# `settings` belongs here rather than with the corpus because the Settings UI writes
# it at runtime: shipped with the corpus, every saved provider change would be lost
# the next time the corpus file was replaced.
_APP_TABLES = (
    "research_workspaces",
    "workspace_circulars",
    "chat_sessions",
    "chat_messages",
    "settings",
)


def _relocate_tables(
    tables: tuple[str, ...], source, target, target_path: Path
) -> int:
    """Move `tables` out of `sbpeye.db` and into `target`, dropping the originals.

    Both the trace split and the runtime-state split are the same migration: copy
    the rows across, drop the old tables and reclaim the pages, so an existing
    checkout keeps its data and stops paying for it in `sbpeye.db`. A no-op once
    the tables are gone.

    The schema is replayed from `sqlite_master` rather than from the ORM metadata:
    this runs at module import, before `models` has been imported, so that metadata
    is still empty here. Copying the recorded DDL also carries the indexes across
    verbatim and keeps the migration correct if the model later changes.

    The engines are parameters so each move can be exercised against throwaway files.
    """
    with source.begin() as conn:
        legacy = [
            (row[0], row[1], row[2])
            for row in conn.execute(text(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND (name IN :names OR tbl_name IN :names)"
            ).bindparams(bindparam("names", expanding=True)), {"names": list(tables)})
        ]
    legacy_tables = {name for kind, name, _ in legacy if kind == "table"}
    if not legacy_tables:
        return 0

    with target.begin() as conn:
        present = set(inspect(conn).get_table_names())
        for kind, name, sql in legacy:
            if kind == "table" and name in present:
                continue
            if kind == "index" and present:
                continue
            conn.execute(text(sql))

    # Release the pooled connection that just built the schema; the copy below needs
    # the target file free of other handles before it can be detached again.
    target.dispose()

    moved = 0
    # One connection with both files attached: no row crosses into Python, and the
    # copy is a single transaction that either lands whole or not at all. SQLite
    # forbids ATTACH/DETACH inside a transaction, so the connection runs in
    # autocommit and the transaction is opened by hand around the data statements.
    with source.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text("ATTACH DATABASE :path AS moved_db"), {"path": str(target_path)}
        )
        try:
            conn.execute(text("BEGIN"))
            try:
                for table in tables:
                    if table not in legacy_tables:
                        continue
                    # OR IGNORE: a half-finished earlier attempt must not abort the move.
                    result = conn.execute(text(
                        f"INSERT OR IGNORE INTO moved_db.{table} SELECT * FROM main.{table}"
                    ))
                    moved += result.rowcount or 0
                    conn.execute(text(f"DROP TABLE main.{table}"))
                conn.execute(text("COMMIT"))
            except Exception:
                conn.execute(text("ROLLBACK"))
                raise
        finally:
            conn.execute(text("DETACH DATABASE moved_db"))

    # VACUUM needs its own connection outside a transaction. Reclaiming the pages is
    # the point of the move, so a failure here is worth a line in the log.
    try:
        with source.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM"))
    except Exception:
        logging.getLogger(__name__).warning(
            "sbpeye.db could not be vacuumed after relocating tables out", exc_info=True
        )
    return moved


def _relocate_trace_tables(
    source=None, target=None, target_path: Path | None = None
) -> int:
    """Move any trace rows still living in `sbpeye.db` into the debug database."""
    return _relocate_tables(
        _TRACE_TABLES,
        source if source is not None else engine,
        target if target is not None else debug_engine,
        target_path if target_path is not None else DEBUG_DATABASE_PATH,
    )


def _relocate_app_tables(
    source=None, target=None, target_path: Path | None = None
) -> int:
    """Move workspaces, chat, and settings out of `sbpeye.db` into the app database."""
    return _relocate_tables(
        _APP_TABLES,
        source if source is not None else engine,
        target if target is not None else app_engine,
        target_path if target_path is not None else APP_DATABASE_PATH,
    )


Base.metadata.create_all(bind=engine)
# The debug tables are created by `llm_debug`, and the runtime-state tables at the
# foot of `models`; both import `models` and so are the first point where their
# metadata actually knows about them.
_relocate_trace_tables()
_relocate_app_tables()
_ensure_columns()
_ensure_app_columns()

# Last, because it reads `settings` — which the relocation above has just moved into
# the application database. Any earlier and a checkout migrating for the first time
# would look for the table before it arrived and silently fall back to environment
# defaults for the rest of the process.
embedding_config = EmbeddingConfig.from_database(app_engine)
embedding_backend = create_embedding_backend(embedding_config)
