"""Back up and retire legacy circulars so the mirror can fetch clean replacements.

This is deliberately distinct from re-keying: unverified derived data is discarded,
not blessed by a provenance override. Files and application research are retained.
"""

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path

from .identity_migration import (
    _backup, _dependencies, _tables, _where, _write_connection, fingerprint,
    known_dependency, open_readonly, plan_migration, quote_identifier,
)

OPERATION = "remove_legacy"
# These are historical observations, not current foreign-key relationships.
HISTORY = {"mirror_audit_item", "mirror_attempt", "sync_status"}
PROVENANCE = {"analysis_provenance_unverified", "dependent_provenance_unverified",
              "attachment_detection_provenance_unverified", "unknown_structured_dependency",
              "unknown_app_dependency", "app_uniqueness_conflict", "review_fingerprint_mismatch"}


def plan_removal(corpus_path, app_path=None):
    report = plan_migration(Path(corpus_path), Path(app_path) if app_path else None)
    report["operation"] = OPERATION
    report["conflicts"] = []
    report["review_evidence"] = {}
    with closing(open_readonly(Path(corpus_path))) as db:
        tables = _tables(db)
        for row in report["mappings"]:
            conflicts = [item for item in row["conflicts"] if item not in PROVENANCE]
            dependencies = row["corpus_dependencies"]
            attachment_dependencies = []
            for attachment in row["attachments"]:
                attachment_dependencies.extend(_dependencies(db, attachment["id"]))
            for dependency in [*dependencies, *attachment_dependencies]:
                if dependency["table"] not in HISTORY and not known_dependency(dependency) and not (
                    dependency["table"] == "mirror_gap" and dependency["column"] == "resolution_id"
                ):
                    conflicts.append(f"Unsupported dependency: {dependency['table']}.{dependency['column']}")
            row["attachment_dependencies"] = attachment_dependencies
            if "identity_alias" in tables and db.execute(
                "SELECT 1 FROM identity_alias WHERE old_id IN (?,?) OR new_id IN (?,?)",
                (row["old_id"], row["new_id"], row["old_id"], row["new_id"]),
            ).fetchone():
                conflicts.append("An existing identity alias overlaps this removal.")
            row["conflicts"] = sorted(set(conflicts))
            if row["conflicts"]:
                report["conflicts"].append({"old_id": row["old_id"], "reasons": row["conflicts"]})
    report.pop("manifest_hash", None)
    report["manifest_hash"] = fingerprint(report)
    return report


def _delete_source(db, mapping, migration_id):
    old, new = mapping["old_id"], mapping["new_id"]
    tables = _tables(db)
    if "circular_relationships" in tables:
        targets = [row[0] for row in db.execute("SELECT target_id FROM circular_relationships WHERE source_id=? AND target_id IS NOT NULL", (old,))]
        columns = {row[1] for row in db.execute("PRAGMA table_info(circulars)")}
        if "relationships_generated_at" in columns:
            db.execute("UPDATE circulars SET relationships_generated_at=NULL WHERE id IN (SELECT source_id FROM circular_relationships WHERE target_id=?)", (old,))
        db.execute("DELETE FROM circular_relationships WHERE source_id=?", (old,))
        db.execute("UPDATE circular_relationships SET target_id=NULL WHERE target_id=?", (old,))
        if "status" in columns:
            for target in targets:
                kinds = {row[0] for row in db.execute("SELECT type FROM circular_relationships WHERE target_id=?", (target,))}
                status = "cancelled" if "cancels" in kinds else "superseded" if "supersedes" in kinds else "amended" if kinds else "active"
                db.execute("UPDATE circulars SET status=? WHERE id=?", (status, target))
    if "circular_consolidations" in tables:
        # Discard the entire derived chain if any provenance points at the removed
        # circular. A partially retained consolidation would assert stale obligations.
        keys = {json.dumps(item["key"], sort_keys=True) for item in mapping["corpus_dependencies"]
                if item["table"] == "circular_consolidations"}
        for key in keys:
            clause, args = _where(json.loads(key))
            db.execute(f"DELETE FROM circular_consolidations WHERE {clause}", args)
    for table in ("circular_entities", "reg_document_links", "attachments"):
        if table in tables:
            db.execute(f"DELETE FROM {quote_identifier(table)} WHERE circular_id=?", (old,))
    if "reg_documents" in tables:
        # Preserve the regulation/listing stub; a later laws sync resolves its link.
        db.execute("UPDATE reg_documents SET circular_id=NULL WHERE circular_id=?", (old,))
    if "ai_generation_jobs" in tables:
        db.execute("UPDATE ai_generation_jobs SET circular_id=NULL, result_status='source_removed' WHERE circular_id=?", (old,))
    if "circulars_fts" in tables:
        db.execute("DELETE FROM circulars_fts WHERE circular_id=?", (old,))
    if "semantic_index_sources" in tables:
        db.execute("DELETE FROM semantic_index_sources WHERE logical_kind='circular' AND logical_document_id=?", (old,))
        db.execute("DELETE FROM semantic_index_sources WHERE source_kind='circular' AND source_id=?", (old,))
        for attachment in mapping["attachments"]:
            db.execute("DELETE FROM semantic_index_sources WHERE source_kind='attachment' AND source_id=?", (attachment["id"],))
    if "mirror_gap" in tables:
        db.execute("""UPDATE mirror_gap SET resolution_id=NULL, resolved_at=NULL, status='pending',
                      eligible=0, eligibility_reason='requires_audit'
                      WHERE resolution_id=?""", (old,))
    db.execute("DELETE FROM circulars WHERE id=?", (old,))
    # App pins/notes/history remain byte-for-byte intact. Existing alias-aware readers
    # resolve them to this survivor's corrected identity once backfill restores it.
    db.execute("INSERT INTO identity_alias VALUES ('circular',?,?,?,?)",
               (old, new, migration_id, datetime.now(timezone.utc).isoformat()))


def _foreign_key_violations(db):
    """Snapshot violations and their rows, not just the number of broken links."""
    violations = Counter()
    for violation in db.execute("PRAGMA foreign_key_check").fetchall():
        table, rowid, parent, foreign_key = tuple(violation)
        quoted = quote_identifier(table)
        if rowid is None:
            # WITHOUT ROWID tables have no row locator in foreign_key_check.
            # Conservatively require their entire contents to stay unchanged.
            rows = frozenset(tuple(row) for row in db.execute(f"SELECT * FROM {quoted}"))
        else:
            columns = {row[1].lower() for row in db.execute(f"PRAGMA table_info({quoted})")}
            locator = next((name for name in ("rowid", "_rowid_", "oid") if name not in columns), None)
            if locator is None:
                raise ValueError(f"Cannot verify existing foreign-key violations in {table}")
            rows = tuple(db.execute(f"SELECT * FROM {quoted} WHERE {locator}=?", (rowid,)).fetchone())
        violations[(table, rowid, parent, foreign_key, rows)] += 1
    return violations


def apply_removal(manifest, backup_dir, *, remove_vectors, phase_hook=lambda phase: None):
    expected = fingerprint({key: value for key, value in manifest.items() if key != "manifest_hash"})
    if manifest.get("operation") != OPERATION or manifest.get("manifest_hash") != expected:
        raise ValueError("Removal manifest mismatch")
    if manifest.get("identity_version") != 2 or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported removal manifest")
    if not manifest["mappings"] or manifest["conflicts"] or any(row["conflicts"] for row in manifest["mappings"]):
        raise ValueError("Removal has unresolved structural conflicts")
    migration_id = expected
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    with closing(_write_connection(manifest["corpus_path"])) as db:
        db.execute("CREATE TABLE IF NOT EXISTS identity_migration (id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, phase TEXT NOT NULL, manifest TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS identity_alias (kind TEXT NOT NULL, old_id TEXT NOT NULL, new_id TEXT NOT NULL, migration_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind,old_id))")
        db.commit()
        other = db.execute("SELECT 1 FROM identity_migration WHERE phase != 'complete' AND id != ?", (migration_id,)).fetchone()
        if other:
            raise ValueError("Finish the active migration before starting removal.")
        journal = db.execute("SELECT * FROM identity_migration WHERE id=?", (migration_id,)).fetchone()
        phase = journal["phase"] if journal else "planned"
        if not journal:
            fresh = plan_removal(manifest["corpus_path"], manifest["app_path"])
            if fresh["corpus_fingerprint"] != manifest["corpus_fingerprint"] or fresh["mappings"] != manifest["mappings"]:
                raise ValueError("The circulars or dependencies changed. Prepare the affected list again.")
            _backup(db, backup_dir / f"{migration_id}-corpus.db")
            if manifest["app_path"]:
                with closing(open_readonly(Path(manifest["app_path"]))) as app:
                    _backup(app, backup_dir / f"{migration_id}-app.db")
            with (backup_dir / f"{migration_id}.json").open("x", encoding="utf-8") as target:
                json.dump(manifest, target, indent=2, ensure_ascii=False)
            db.execute("INSERT INTO identity_migration VALUES (?,?,?,?)", (migration_id, migration_id, phase, json.dumps(manifest)))
            db.commit()
        if phase == "planned":
            db.execute("BEGIN IMMEDIATE")
            db.execute("PRAGMA defer_foreign_keys=ON")
            try:
                existing_violations = _foreign_key_violations(db)
                for mapping in manifest["mappings"]:
                    row = db.execute("SELECT * FROM circulars WHERE id=?", (mapping["old_id"],)).fetchone()
                    if row is None or fingerprint(dict(row)) != mapping["row_fingerprint"]:
                        raise ValueError("Circular changed since removal was prepared")
                for mapping in manifest["mappings"]:
                    _delete_source(db, mapping, migration_id)
                if _foreign_key_violations(db) - existing_violations:
                    raise ValueError("Removal would create new or changed invalid foreign keys")
                db.execute("UPDATE identity_migration SET phase='corpus_committed' WHERE id=?", (migration_id,))
                db.commit()
            except BaseException:
                db.rollback()
                raise
            phase = "corpus_committed"
            phase_hook(phase)
        if phase == "corpus_committed":
            remove_vectors(manifest)
            db.execute("UPDATE identity_migration SET phase='indexes_verified' WHERE id=?", (migration_id,))
            db.commit()
            phase_hook("indexes_verified")
        db.execute("UPDATE identity_migration SET phase='complete' WHERE id=?", (migration_id,))
        db.commit()
    return {"migration_id": migration_id, "phase": "complete", "removed_count": len(manifest["mappings"])}


def remove_legacy_vectors(manifest):
    from .database import collection
    for mapping in manifest["mappings"]:
        selectors = [{"circular_id": mapping["old_id"]}, *[
            {"attachment_id": item["id"]} for item in mapping["attachments"]]]
        for selector in selectors:
            chunks = collection.get(where=selector, include=["metadatas"])
            ids = [identity for identity, metadata in zip(chunks["ids"], chunks["metadatas"])
                   if metadata and metadata.get("kind") != "law" and metadata.get("doc_type") in {"circular", "attachment"}]
            if ids:
                collection.delete(ids=ids)
                if collection.get(ids=ids, include=[])["ids"]:
                    raise RuntimeError("Removed circular search entries still remain. Resume cleanup.")


def requires_fresh_fetch(session, circular_id):
    """A replacement must reach SBP rather than silently recycling cached HTML."""
    from .mirror_models import IdentityAlias, IdentityMigration
    migrations = session.query(IdentityMigration.manifest).join(
        IdentityAlias, IdentityAlias.migration_id == IdentityMigration.id,
    ).filter(IdentityAlias.kind == "circular", IdentityAlias.new_id == circular_id).all()
    return any(json.loads(row[0]).get("operation") == OPERATION for row in migrations)
