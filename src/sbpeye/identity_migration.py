"""Read-only migration planning. This module must never import application storage.

The manifest inventories surviving records; it cannot reconstruct overwritten records
or establish the provenance of their AI analyses merely from a matching foreign key.
"""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .circular_identity import IDENTITY_VERSION, CIRCULAR_REFERENCE_RE, circular_identity, reference_conflicts
from .index_identity import ledger_row_id

SCALAR_DEPENDENCIES = {
    "circulars": {"id"}, "circular_relationships": {"source_id", "target_id"},
    "circular_entities": {"circular_id"}, "attachments": {"id", "circular_id"},
    "reg_documents": {"circular_id"}, "reg_document_links": {"circular_id"},
    "ai_generation_jobs": {"circular_id"},
    "circular_consolidations": {"chain_id", "as_of_circular_id"},
    "semantic_index_sources": {"source_id", "logical_document_id"},
    "circulars_fts": {"circular_id"}, "workspace_circulars": {"circular_id"},
    "research_workspaces": {"last_circular_id"},
}
JSON_DEPENDENCIES = {
    "circular_consolidations": {"member_ids", "requirements"},
    "chat_sessions": {"circular_ids"}, "chat_messages": {"circular_ids"},
}


def known_dependency(dependency):
    table, column = dependency["table"], dependency["column"]
    if dependency["paths"] == [[]]:
        return column in SCALAR_DEPENDENCIES.get(table, set())
    if column not in JSON_DEPENDENCIES.get(table, set()):
        return False
    if column == "requirements":
        return all(path and path[-1] in {"introduced_by", "circular_id"} for path in dependency["paths"])
    return all(len(path) == 1 and isinstance(path[0], int) for path in dependency["paths"])


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(connection):
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ) if not row[0].startswith(("circulars_fts_", "laws_fts_"))]


def _dependencies(connection, old_id):
    """Inventory exact scalar and structured occurrences, including unknown schemas.

    Prose mentions are deliberately excluded: historical chat prose is immutable.
    JSON paths are captured so apply can use typed updates rather than text replacement.
    """
    found = []
    def paths(value, path=()):
        if value == old_id:
            yield list(path)
        elif isinstance(value, dict):
            for key, item in value.items():
                yield from paths(item, (*path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from paths(item, (*path, index))

    for table in _tables(connection):
        if table in {"identity_migration", "identity_alias"}:
            continue
        columns = list(connection.execute(f"PRAGMA table_info({quote_identifier(table)})"))
        pk = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        predicate = " OR ".join(f"instr({quote_identifier(col[1])}, ?) > 0" for col in columns)
        for row in connection.execute(
            f"SELECT * FROM {quote_identifier(table)} WHERE {predicate}",
            [old_id] * len(columns),
        ):
            row = dict(row)
            for column, value in row.items():
                occurrences = []
                if value == old_id:
                    occurrences = [[]]
                elif isinstance(value, str) and value.lstrip().startswith(("[", "{")):
                    try:
                        occurrences = list(paths(json.loads(value)))
                    except (ValueError, TypeError):
                        pass
                if occurrences:
                    found.append({"table": table, "key": {k: row[k] for k in pk},
                                  "column": column, "paths": occurrences,
                                  "row_fingerprint": fingerprint(row)})
    return found


def _unique_conflicts(connection, dependencies, old, new):
    for dependency in dependencies:
        if dependency["paths"] != [[]] or not dependency["key"]:
            continue
        table, column = dependency["table"], dependency["column"]
        clause, values = _where(dependency["key"])
        row = connection.execute(f"SELECT * FROM {quote_identifier(table)} WHERE {clause}", values).fetchone()
        if row is None:
            continue
        for index in connection.execute(f"PRAGMA index_list({quote_identifier(table)})"):
            if not index[2]:
                continue
            columns = [entry[2] for entry in connection.execute(f"PRAGMA index_info({quote_identifier(index[1])})")]
            if column not in columns or any(name is None for name in columns):
                continue
            candidate = {name: new if row[name] == old else row[name] for name in columns}
            if any(value is None for value in candidate.values()):
                continue
            check, args = _where(candidate)
            if connection.execute(f"SELECT 1 FROM {quote_identifier(table)} WHERE {check}", args).fetchone():
                return True
    return False


def _verified_evidence(item):
    """Operator-supplied provenance must name immutable, fingerprinted source evidence."""
    if not isinstance(item, dict) or not str(item.get("note", "")).strip():
        return False
    path = Path(item.get("source_path", ""))
    return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item.get("source_hash")


def plan_migration(corpus_path: Path, app_path: Path | None = None, *, evidence=None, listing_snapshot=None) -> dict:
    corpus_path = corpus_path.resolve()
    app_path = app_path.resolve() if app_path else None
    report = {"schema_version": 1, "identity_version": IDENTITY_VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "corpus_path": str(corpus_path), "app_path": str(app_path) if app_path else None,
              "collision_groups": None, "mappings": [], "drift": [], "conflicts": []}
    report["review_evidence"] = evidence or {}
    if listing_snapshot is not None:
        from collections import defaultdict
        groups = defaultdict(set)
        for item in listing_snapshot:
            groups[circular_identity(item.get("reference"), item["url"], identity_version=1)].add(
                circular_identity(item.get("reference"), item["url"]))
        report["collision_groups"] = sum(len(group) > 1 for group in groups.values())
        report["listing_fingerprint"] = fingerprint(listing_snapshot)
    with closing(open_readonly(corpus_path)) as corpus:
        corpus.execute("BEGIN")
        rows = [dict(row) for row in corpus.execute("SELECT * FROM circulars ORDER BY id")]
        report["corpus_fingerprint"] = fingerprint(rows)
        ids = {row["id"] for row in rows}
        for row in rows:
            reference, url = row.get("reference"), row.get("url") or ""
            legacy = circular_identity(reference, url, identity_version=1)
            current = circular_identity(reference, url)
            if row["id"] == current:
                continue
            parsed = CIRCULAR_REFERENCE_RE.search(reference or "")
            if row["id"] != legacy or legacy == current or not parsed or not parsed.group("slash_year"):
                report["drift"].append({"id": row["id"], "expected_id": current,
                                        "reference": reference, "url": url})
                continue
            conflicts = reference_conflicts(reference)
            if current in ids:
                conflicts.append("occupied_destination")
            dependencies = _dependencies(corpus, row["id"])
            if _unique_conflicts(corpus, dependencies, row["id"], current):
                conflicts.append("dependency_uniqueness_conflict")
            if any(not known_dependency(item) for item in dependencies):
                conflicts.append("unknown_structured_dependency")
            if any(item["table"] in {"circular_entities", "circular_relationships", "reg_document_links", "ai_generation_jobs", "circular_consolidations"} for item in dependencies):
                conflicts.append("dependent_provenance_unverified")
            attachments = []
            if "attachments" in _tables(corpus):
                attachments = [dict(item) for item in corpus.execute(
                    "SELECT * FROM attachments WHERE circular_id=? ORDER BY id", (row["id"],))]
            # Surviving AI/attachments may belong to a previous occupant of this ID.
            if any(row.get(key) for key in ("summary", "tags", "compliance_checklist")):
                conflicts.append("analysis_provenance_unverified")
            if attachments:
                conflicts.append("attachment_detection_provenance_unverified")
            mapping = {"old_id": row["id"], "new_id": current, "reference": reference,
                       "url": url, "row_fingerprint": fingerprint(row),
                       "corpus_dependencies": dependencies, "app_dependencies": [],
                       "attachments": attachments, "attachment_mappings": [], "conflicts": conflicts}
            review = (evidence or {}).get(row["id"], {})
            if review and review.get("row_fingerprint") != mapping["row_fingerprint"]:
                conflicts.append("review_fingerprint_mismatch")
                review = {}
            for scope, reason in (("analysis", "analysis_provenance_unverified"), ("dependents", "dependent_provenance_unverified")):
                if reason in conflicts and _verified_evidence(review.get(scope)):
                    conflicts.remove(reason)
            reviewed_attachments = {item.get("id"): item for item in review.get("attachments", [])}
            for attachment in attachments:
                proof = reviewed_attachments.get(attachment["id"], {})
                detection = proof.get("detection_url", "")
                expected = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{row['id']}:{detection}"))
                if not _verified_evidence(proof) or expected != attachment["id"]:
                    continue
                from html.parser import HTMLParser
                from urllib.parse import urljoin
                class Links(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.urls = set()
                    def handle_starttag(self, tag, attrs):
                        if tag.lower() == "a":
                            href = dict(attrs).get("href")
                            if href:
                                self.urls.add(href)
                links = Links()
                links.feed(Path(proof["source_path"]).read_text(encoding="utf-8", errors="replace"))
                bases = [url, row.get("old_url") or url, "https://www.sbp.org.pk/assets/documents/circulars/"]
                from .sbp_urls import normalize_sbp_url
                detected_urls = set()
                for href in links.urls:
                    for base in bases:
                        try:
                            detected_urls.add(normalize_sbp_url(urljoin(base, href)))
                        except ValueError:
                            pass
                if detection not in detected_urls:
                    continue
                new_attachment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{current}:{detection}"))
                if corpus.execute("SELECT 1 FROM attachments WHERE id=?", (new_attachment_id,)).fetchone():
                    conflicts.append("occupied_attachment_destination")
                    continue
                attachment_dependencies = _dependencies(corpus, attachment["id"])
                if any(not known_dependency(item) for item in attachment_dependencies):
                    conflicts.append("unknown_attachment_dependency")
                    continue
                mapping["attachment_mappings"].append({"old_id": attachment["id"], "new_id": new_attachment_id,
                    "corpus_dependencies": attachment_dependencies, "app_dependencies": [], "evidence": proof})
            if attachments and len(mapping["attachment_mappings"]) == len(attachments):
                conflicts.remove("attachment_detection_provenance_unverified")
            report["mappings"].append(mapping)
        corpus.rollback()
    if app_path:
        with closing(open_readonly(app_path)) as app:
            app.execute("BEGIN")
            for mapping in report["mappings"]:
                mapping["app_dependencies"] = _dependencies(app, mapping["old_id"])
                if _unique_conflicts(app, mapping["app_dependencies"], mapping["old_id"], mapping["new_id"]):
                    mapping["conflicts"].append("app_uniqueness_conflict")
                if any(not known_dependency(item) for item in mapping["app_dependencies"]):
                    mapping["conflicts"].append("unknown_app_dependency")
                for attachment in mapping["attachment_mappings"]:
                    attachment["app_dependencies"] = _dependencies(app, attachment["old_id"])
                    if any(not known_dependency(item) for item in attachment["app_dependencies"]):
                        mapping["conflicts"].append("unknown_attachment_app_dependency")
            app.rollback()
    for mapping in report["mappings"]:
        if mapping["conflicts"]:
            report["conflicts"].append({"old_id": mapping["old_id"], "reasons": mapping["conflicts"]})
    report["manifest_hash"] = fingerprint(report)
    return report


def identity_preflight(connection) -> None:
    """Reject identity-keyed writes until re-keying and index recovery are complete."""
    if "identity_migration" in _tables(connection):
        if connection.execute("SELECT 1 FROM identity_migration WHERE phase != 'complete' LIMIT 1").fetchone():
            raise ValueError("Identity migration is incomplete; resume its reviewed manifest first.")
    for row in connection.execute("SELECT id, reference, url FROM circulars"):
        old = circular_identity(row[1], row[2] or "", identity_version=1)
        parsed = CIRCULAR_REFERENCE_RE.search(row[1] or "")
        if parsed and parsed.group("slash_year") and row[0] == old and old != circular_identity(row[1], row[2] or ""):
            raise ValueError("Legacy slash-year identities remain; review and apply an identity migration first.")


def _write_connection(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _where(key):
    if not key:
        raise ValueError("Cannot migrate a dependency without a primary key")
    return " AND ".join(f"{quote_identifier(k)} IS ?" for k in key), list(key.values())


def _update_dependency(connection, dependency, old, new):
    table, column = dependency["table"], dependency["column"]
    if table in {"circulars", "circulars_fts"}:
        return
    clause, values = _where(dependency["key"])
    row = connection.execute(f"SELECT * FROM {quote_identifier(table)} WHERE {clause}", values).fetchone()
    if row is None:
        # A previous update in this transaction may have changed a composite key.
        key = {k: new if v == old else v for k, v in dependency["key"].items()}
        clause, values = _where(key)
        row = connection.execute(f"SELECT * FROM {quote_identifier(table)} WHERE {clause}", values).fetchone()
    if row is None:
        raise ValueError(f"Missing dependency {table} {dependency['key']}")
    value = row[column]
    if dependency["paths"] == [[]]:
        if value not in (old, new):
            raise ValueError("Dependency changed since planning")
        value = new
    else:
        value = json.loads(value)
        for path in dependency["paths"]:
            parent = value
            for component in path[:-1]:
                parent = parent[component]
            if parent[path[-1]] not in (old, new):
                raise ValueError("Structured dependency changed since planning")
            parent[path[-1]] = new
        value = json.dumps(value, ensure_ascii=False)
    connection.execute(f"UPDATE {quote_identifier(table)} SET {quote_identifier(column)}=? WHERE {clause}", [value, *values])


def _backup(connection, path):
    # Reserve the name without overwriting an operator's earlier backup.
    with Path(path).open("xb"):
        pass
    with closing(sqlite3.connect(path)) as backup:
        connection.backup(backup)


def apply_migration(manifest: dict, backup_dir: Path, *, repair_indexes, phase_hook=lambda phase: None):
    """Apply/resume a conflict-free reviewed manifest with an injected index repair.

    The CLI checks exclusive storage access before calling this or importing Chroma.
    A repair callback must verify changed sources before returning. SQLite commits are
    journalled atomically; vector repair may be replayed after interruption.
    """
    if manifest.get("operation") == "remove_legacy":
        raise ValueError("A removal must use the removal workflow, not identity re-keying.")
    supplied_hash = manifest.get("manifest_hash")
    if supplied_hash != fingerprint({k: v for k, v in manifest.items() if k != "manifest_hash"}):
        raise ValueError("Manifest hash mismatch")
    if manifest.get("identity_version") != IDENTITY_VERSION or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest version")
    if manifest["conflicts"] or any(mapping["conflicts"] for mapping in manifest["mappings"]):
        raise ValueError("Manifest has unresolved provenance or destination conflicts")
    migration_id = supplied_hash
    corpus_path, app_path = manifest["corpus_path"], manifest["app_path"]
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    with closing(_write_connection(corpus_path)) as corpus:
        corpus.execute("CREATE TABLE IF NOT EXISTS identity_migration (id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, phase TEXT NOT NULL, manifest TEXT NOT NULL)")
        corpus.execute("CREATE TABLE IF NOT EXISTS identity_alias (kind TEXT NOT NULL, old_id TEXT NOT NULL, new_id TEXT NOT NULL, migration_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(kind,old_id))")
        corpus.commit()
        journal = corpus.execute("SELECT * FROM identity_migration WHERE id=?", (migration_id,)).fetchone()
        phase = journal["phase"] if journal else "planned"
        if phase == "complete":
            return {"migration_id": migration_id, "phase": phase}
        if not journal:
            fresh = plan_migration(Path(corpus_path), Path(app_path) if app_path else None, evidence=manifest.get("review_evidence"))
            if fresh["corpus_fingerprint"] != manifest["corpus_fingerprint"] or fresh["mappings"] != manifest["mappings"]:
                raise ValueError("Database or dependencies changed since dry run; regenerate the manifest")
            _backup(corpus, backup_dir / f"{migration_id}-corpus.db")
            if app_path:
                with closing(open_readonly(Path(app_path))) as app:
                    _backup(app, backup_dir / f"{migration_id}-app.db")
            with (backup_dir / f"{migration_id}.json").open("x", encoding="utf-8") as target:
                json.dump(manifest, target, indent=2, ensure_ascii=False)
            corpus.execute("INSERT INTO identity_migration VALUES (?,?,?,?)", (migration_id, supplied_hash, phase, json.dumps(manifest)))
            corpus.commit()
        if phase == "planned":
            corpus.execute("BEGIN IMMEDIATE")
            corpus.execute("PRAGMA defer_foreign_keys=ON")
            try:
                for mapping in manifest["mappings"]:
                    old, new = mapping["old_id"], mapping["new_id"]
                    row = corpus.execute("SELECT * FROM circulars WHERE id=?", (old,)).fetchone()
                    if row is None or fingerprint(dict(row)) != mapping["row_fingerprint"]:
                        raise ValueError("Circular changed since dry run")
                    if corpus.execute("SELECT 1 FROM identity_alias WHERE old_id IN (?,?) OR new_id IN (?,?)", (old, new, old, new)).fetchone():
                        raise ValueError("Alias would form a chain or conflict")
                    for dependency in mapping["corpus_dependencies"]:
                        if not known_dependency(dependency):
                            raise ValueError("Unknown dependency")
                        if dependency["table"] == "circular_relationships" and dependency["column"] == "target_id":
                            clause, values = _where(dependency["key"])
                            relationship = corpus.execute(f"SELECT target_reference FROM circular_relationships WHERE {clause}", values).fetchone()
                            from .circular_identity import _reference_parts
                            target = _reference_parts(relationship[0])
                            survivor = _reference_parts(mapping["reference"])
                            if target and survivor and target["year"] and target["year"] != survivor["year"]:
                                target_id = circular_identity(relationship[0], "")
                                exists = corpus.execute("SELECT 1 FROM circulars WHERE id=?", (target_id,)).fetchone()
                                corpus.execute(f"UPDATE circular_relationships SET target_id=? WHERE {clause}", [target_id if exists else None, *values])
                                continue
                        _update_dependency(corpus, dependency, old, new)
                    for attachment in mapping.get("attachment_mappings", []):
                        for dependency in attachment["corpus_dependencies"]:
                            _update_dependency(corpus, dependency, attachment["old_id"], attachment["new_id"])
                        corpus.execute("UPDATE attachments SET is_vectorized=0 WHERE id=?", (attachment["new_id"],))
                        corpus.execute("INSERT INTO identity_alias VALUES ('attachment',?,?,?,?)", (attachment["old_id"], attachment["new_id"], migration_id, datetime.now(timezone.utc).isoformat()))
                    corpus.execute("UPDATE circulars SET id=? WHERE id=?", (new, old))
                    tables = _tables(corpus)
                    if "circulars_fts" in tables:
                        # IDs change; content is identical. FTS5 updates its own postings.
                        corpus.execute("UPDATE circulars_fts SET circular_id=? WHERE circular_id=?", (new, old))
                    if "semantic_index_sources" in tables:
                        for ledger in corpus.execute("SELECT id, source_kind, source_id FROM semantic_index_sources WHERE logical_kind='circular' AND logical_document_id=?", (new,)).fetchall():
                            ledger_id = ledger_row_id(f"{ledger['source_kind']}:{ledger['source_id']}")
                            corpus.execute("UPDATE semantic_index_sources SET id=?, status='stale' WHERE id=?", (ledger_id, ledger["id"]))
                    if "circular_consolidations" in tables:
                        corpus.execute("UPDATE circular_consolidations SET stale=1 WHERE chain_id=? OR as_of_circular_id=? OR instr(member_ids,?)>0", (new, new, new))
                    corpus.execute("INSERT INTO identity_alias VALUES ('circular',?,?,?,?)", (old, new, migration_id, datetime.now(timezone.utc).isoformat()))
                if corpus.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Foreign-key validation failed")
                corpus.execute("UPDATE identity_migration SET phase='corpus_committed' WHERE id=?", (migration_id,))
                corpus.commit()
            except BaseException:
                corpus.rollback()
                raise
            phase = "corpus_committed"
            phase_hook(phase)
        if phase == "corpus_committed":
            if app_path:
                with closing(_write_connection(app_path)) as app:
                    with app:
                        for mapping in manifest["mappings"]:
                            for dependency in mapping["app_dependencies"]:
                                if not known_dependency(dependency):
                                    raise ValueError("Unknown application dependency")
                                _update_dependency(app, dependency, mapping["old_id"], mapping["new_id"])
                            for attachment in mapping.get("attachment_mappings", []):
                                for dependency in attachment["app_dependencies"]:
                                    _update_dependency(app, dependency, attachment["old_id"], attachment["new_id"])
            corpus.execute("UPDATE identity_migration SET phase='app_committed' WHERE id=?", (migration_id,))
            corpus.commit()
            phase = "app_committed"
            phase_hook(phase)
        if phase == "app_committed":
            repair_indexes(manifest)
            corpus.execute("UPDATE identity_migration SET phase='indexes_verified' WHERE id=?", (migration_id,))
            corpus.commit()
            phase_hook("indexes_verified")
        corpus.execute("UPDATE identity_migration SET phase='complete' WHERE id=?", (migration_id,))
        corpus.commit()
    return {"migration_id": migration_id, "phase": "complete"}
