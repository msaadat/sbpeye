"""Durable, server-owned identity review and migration on the mounted data volume."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .identity_migration import (
    _tables, _where, apply_migration, identity_preflight, open_readonly,
    plan_migration, quote_identifier,
)
from .maintenance import maintenance


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.flush()
        import os
        os.fsync(target.fileno())
    for attempt in range(6):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            # Windows virus scanners/readers may briefly deny an atomic replace.
            if attempt == 5:
                raise
            time.sleep(.02 * (2 ** attempt))


class MigrationConsole:
    def __init__(self, root, corpus, app, html_cache, *, gate=maintenance, repair=None, remove_vectors=None):
        self.root = Path(root)
        self.corpus, self.app = Path(corpus), Path(app) if app else None
        self.html_cache = Path(html_cache)
        self.gate, self.repair = gate, repair
        self.remove_vectors = remove_vectors
        self.operation = threading.Lock()
        self.state_lock = threading.RLock()
        self.worker = None
        self.state = self._read("state.json", {"status": "idle", "maintenance": False})
        self.gate.pause(self.state.get("maintenance", False))

    def _read(self, name, default=None):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def _update(self, **changes):
        with self.state_lock:
            state = {**self.state, **changes}
            write_json(self.root / "state.json", state)
            self.state = state
            self.gate.pause(state["maintenance"])

    def _journal(self):
        with closing(open_readonly(self.corpus)) as db:
            if "identity_migration" not in _tables(db):
                return None
            row = db.execute("SELECT * FROM identity_migration WHERE phase != 'complete' LIMIT 1").fetchone()
            return dict(row) if row else None

    def recover(self):
        """Run at boot before serving requests or starting corpus jobs; never from GET."""
        journal = self._journal()
        if journal:
            # The journal, rather than a browser/localStorage value, owns resume.
            manifest = json.loads(journal["manifest"])
            removal = manifest.get("operation") == "remove_legacy"
            write_json(self.root / ("removal.json" if removal else "manifest.json"), manifest)
            self._update(status="interrupted", maintenance=True, apply_started=True,
                         operation="remove_legacy" if removal else "migrate",
                         phase=journal["phase"], error="Maintenance was interrupted. Resume to finish the remaining phases.")
        elif self.state.get("maintenance"):
            # A crash after the final journal commit needs only verification/release.
            if self.state.get("apply_started"):
                self._update(status="interrupted", error="Migration stopped before the console recorded completion. Resume to verify it.")
            elif self.state["status"] not in {"review", "failed"}:
                self._update(status="failed", error="Preparation was interrupted. Prepare the review again or leave maintenance.")

    def status(self):
        with self.state_lock:
            result = dict(self.state)
        report = self._read("manifest.json", {})
        removal = self._read("removal.json", {})
        if result.get("operation") == "remove_legacy" and removal:
            report = removal
        result.update(
            busy=self.operation.locked(),
            can_cancel=bool(result.get("maintenance") and not result.get("apply_started")),
            manifest_hash=report.get("manifest_hash"),
            mappings=[{key: row[key] for key in ("old_id", "new_id", "reference", "url", "conflicts")}
                      for row in report.get("mappings", [])],
            conflicts=report.get("conflicts", []), drift_count=len(report.get("drift", [])),
            can_apply=result["status"] == "review" and bool(report.get("mappings")) and not report.get("conflicts"),
            removal_hash=removal.get("manifest_hash"),
            removal_count=len(removal.get("mappings", [])),
            removal_attachment_count=sum(len(row["attachments"]) for row in removal.get("mappings", [])),
            removal_conflicts=removal.get("conflicts", []),
            can_remove=result["status"] == "review" and not result.get("apply_started") and bool(removal.get("mappings")) and not removal.get("conflicts"),
        )
        return result

    def record(self, old_id):
        # Snapshot reads remain safe even while apply runs. No caller-selected paths.
        records = self._read("records.json", {})
        if old_id not in records:
            raise ValueError("Unknown review record. Prepare a review first.")
        result = dict(records[old_id])
        report = self._read("manifest.json", {})
        result["review"] = report.get("review_evidence", {}).get(old_id, {})
        return result

    def _start(self, status, work, *, manifest_hash=None, removal_hash=None):
        if not self.operation.acquire(blocking=False):
            raise ValueError("A migration operation is already running.")
        allowed = {
            "preparing": {"idle", "cancelled", "complete", "review", "failed"},
            "reviewing": {"review"},
            "applying": {"review", "failed", "interrupted"},
            "removing": {"review", "failed", "interrupted"},
        }
        if self.state["status"] not in allowed[status] or (status not in {"applying", "removing"} and self.state.get("apply_started")):
            self.operation.release()
            raise ValueError("The migration state changed. Refresh before continuing.")
        if manifest_hash and self._read("manifest.json", {}).get("manifest_hash") != manifest_hash:
            self.operation.release()
            raise ValueError("The review changed. Refresh before continuing.")
        if removal_hash and self._read("removal.json", {}).get("manifest_hash") != removal_hash:
            self.operation.release()
            raise ValueError("The affected list changed. Refresh before removing circulars.")
        if self.state.get("apply_started") and (status == "removing") != (self.state.get("operation") == "remove_legacy"):
            self.operation.release()
            raise ValueError("Resume the operation already in progress.")
        try:
            operation = "remove_legacy" if status == "removing" else "migrate" if status == "applying" else self.state.get("operation")
            self._update(status=status, maintenance=True, error=None, operation=operation)
            def run():
                try:
                    work()
                except Exception as exc:
                    self._update(status="failed", error=str(exc))
                finally:
                    self.operation.release()
            self.worker = threading.Thread(target=run, name="identity-maintenance", daemon=True)
            self.worker.start()
        except BaseException:
            self.operation.release()
            self._update(status="failed", error="Could not start the maintenance worker. Retry or leave maintenance.")
            raise

    def prepare(self):
        if self.state.get("apply_started") or self._journal():
            raise ValueError("An applied migration must be resumed, not replaced.")
        def work():
            self.gate.drain()
            from .circular_jobs import CIRCULAR_JOB_LOCK
            if not CIRCULAR_JOB_LOCK.acquire(timeout=300):
                raise ValueError("A circular job is still running. Retry after it finishes.")
            try:
                from .identity_removal import plan_removal
                report = plan_migration(self.corpus, self.app if self.app and self.app.exists() else None)
                removal = plan_removal(self.corpus, self.app if self.app and self.app.exists() else None)
                records = {}
                with closing(open_readonly(self.corpus)) as db:
                    for mapping in report["mappings"]:
                        old = mapping["old_id"]
                        row = dict(db.execute("SELECT * FROM circulars WHERE id=?", (old,)).fetchone())
                        dependencies = []
                        for dependency in mapping["corpus_dependencies"]:
                            if dependency["table"] in {"circulars", "circulars_fts", "semantic_index_sources", "attachments"}:
                                continue
                            clause, args = _where(dependency["key"])
                            found = db.execute(f"SELECT * FROM {quote_identifier(dependency['table'])} WHERE {clause}", args).fetchone()
                            dependencies.append({"table": dependency["table"], "column": dependency["column"], "row": dict(found)})
                        cache = self.html_cache / f"{uuid.uuid5(uuid.NAMESPACE_URL, row.get('url') or '')}.html"
                        records[old] = {"circular": row, "attachments": mapping["attachments"],
                                        "dependencies": dependencies, "app_dependencies": mapping["app_dependencies"],
                                        "cached_html": cache.read_text(encoding="utf-8", errors="replace") if cache.is_file() else None}
                write_json(self.root / "records.json", records)
                write_json(self.root / "manifest.json", report)
                write_json(self.root / "removal.json", removal)
                self._update(status="review", apply_started=False, phase=None,
                             error=None, backup_directory=None, operation=None, removed_count=0)
            finally:
                CIRCULAR_JOB_LOCK.release()
        self._start("preparing", work)

    def review(self, old_id, source_text, note, scopes, attachments, reviewer):
        if self.state["status"] != "review" or self.state.get("apply_started"):
            raise ValueError("Prepare a review before submitting evidence.")
        report = self._read("manifest.json")
        mapping = next((item for item in report["mappings"] if item["old_id"] == old_id), None)
        if mapping is None:
            raise ValueError("Unknown review record.")
        def work():
            # Browser supplies bytes and notes, never filesystem paths or a manifest.
            digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            source = self.root / "evidence" / f"{digest}.html"
            source.parent.mkdir(parents=True, exist_ok=True)
            if not source.exists():
                with source.open("xb") as target:
                    target.write(source_text.encode("utf-8"))
            proof = {"source_path": str(source.resolve()), "source_hash": digest,
                     "note": note, "reviewer_id": str(reviewer)}
            evidence = report.get("review_evidence", {})
            review = evidence.setdefault(old_id, {"row_fingerprint": mapping["row_fingerprint"]})
            for scope in scopes:
                review[scope] = proof
            known = {item["id"] for item in mapping["attachments"]}
            previous = {item["id"]: item for item in review.get("attachments", [])}
            for item in attachments:
                if item["id"] not in known:
                    raise ValueError("Unknown attachment in review.")
                previous[item["id"]] = {**proof, **item}
            review["attachments"] = list(previous.values())
            reviewed = plan_migration(self.corpus, self.app if self.app and self.app.exists() else None, evidence=evidence)
            write_json(self.root / "manifest.json", reviewed)
            self._update(status="review", error=None)
        self._start("reviewing", work, manifest_hash=report["manifest_hash"])

    def apply(self, manifest_hash):
        if self.state.get("apply_started") and self.state.get("operation") == "remove_legacy":
            raise ValueError("Resume the removal already in progress.")
        if self.state["status"] not in {"review", "failed", "interrupted"}:
            raise ValueError("The migration is not ready to apply or resume.")
        manifest = self._read("manifest.json", {})
        if not manifest_hash or manifest_hash != manifest.get("manifest_hash"):
            raise ValueError("The review changed. Refresh and check the current mappings before applying.")
        if manifest.get("conflicts") or not manifest.get("mappings"):
            raise ValueError("Resolve the listed conflicts before applying.")
        def work():
            self.gate.drain()
            from .circular_jobs import CIRCULAR_JOB_LOCK
            if not CIRCULAR_JOB_LOCK.acquire(timeout=300):
                raise ValueError("A circular job is still running. Resume after it finishes.")
            try:
                backup = self.root / "backups"
                self._update(apply_started=True, backup_directory=str(backup), phase="backing_up")
                if self.repair is None:
                    from .mirror_repairs import repair_migration_indexes
                    repair = repair_migration_indexes
                else:
                    repair = self.repair
                try:
                    result = apply_migration(manifest, backup, repair_indexes=repair,
                                             phase_hook=lambda phase: self._update(phase=phase))
                except Exception:
                    with closing(open_readonly(self.corpus)) as db:
                        journaled = "identity_migration" in _tables(db) and db.execute(
                            "SELECT 1 FROM identity_migration WHERE id=?", (manifest_hash,),
                        ).fetchone()
                    if not journaled:
                        # A rejected stale manifest or failed backup has changed no
                        # identities; permit a fresh review or leaving maintenance.
                        self._update(apply_started=False)
                    raise
                with closing(open_readonly(self.corpus)) as db:
                    identity_preflight(db)
                self._update(status="complete", phase=result["phase"], maintenance=False, apply_started=False, error=None)
            finally:
                CIRCULAR_JOB_LOCK.release()
        self._start("applying", work, manifest_hash=manifest_hash)

    def cancel(self):
        if not self.operation.acquire(blocking=False):
            raise ValueError("Wait for the current operation to finish.")
        try:
            if self.state.get("apply_started") or self._journal():
                raise ValueError("Migration has started. Resume it before leaving maintenance.")
            self._update(status="cancelled", maintenance=False, error=None)
        finally:
            self.operation.release()

    def remove(self, removal_hash):
        """Retire the server-selected legacy rows; a browser cannot choose arbitrary IDs."""
        if self.state.get("apply_started") and self.state.get("operation") != "remove_legacy":
            raise ValueError("Resume the identity migration already in progress.")
        manifest = self._read("removal.json", {})
        if not removal_hash or removal_hash != manifest.get("manifest_hash"):
            raise ValueError("Prepare the affected list again before removing circulars.")
        if not manifest.get("mappings") or manifest.get("conflicts"):
            raise ValueError("The affected list has unresolved structural conflicts.")
        def work():
            from .circular_jobs import CIRCULAR_JOB_LOCK
            from .identity_removal import apply_removal, remove_legacy_vectors
            self.gate.drain()
            if not CIRCULAR_JOB_LOCK.acquire(timeout=300):
                raise ValueError("A circular job is still running. Retry after it finishes.")
            try:
                backup = self.root / "backups"
                self._update(apply_started=True, backup_directory=str(backup), phase="backing_up")
                try:
                    result = apply_removal(manifest, backup,
                                           remove_vectors=self.remove_vectors or remove_legacy_vectors,
                                           phase_hook=lambda phase: self._update(phase=phase))
                except Exception:
                    with closing(open_readonly(self.corpus)) as db:
                        journaled = "identity_migration" in _tables(db) and db.execute(
                            "SELECT 1 FROM identity_migration WHERE id=?", (removal_hash,),
                        ).fetchone()
                    if not journaled:
                        self._update(apply_started=False)
                    raise
                with closing(open_readonly(self.corpus)) as db:
                    identity_preflight(db)
                self._update(status="complete", phase="complete", maintenance=False, apply_started=False,
                             removed_count=result["removed_count"], error=None)
            finally:
                CIRCULAR_JOB_LOCK.release()
        self._start("removing", work, removal_hash=removal_hash)


def make_console():
    from .database import APP_DATABASE_PATH
    from .env import DATA_ROOT, HTML_CACHE_DIR
    return MigrationConsole(DATA_ROOT / "maintenance" / "identity", DATA_ROOT / "sbpeye.db", APP_DATABASE_PATH, HTML_CACHE_DIR)


console = make_console()
