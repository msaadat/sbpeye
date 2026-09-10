import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from sbpeye.circular_identity import (
    _reference_parts, circular_identity, iter_circular_references,
    normalize_reference, reference_conflicts,
)
from sbpeye.identity_migration import plan_migration


def test_slash_years_separate_overwritten_identities():
    references = [f"FD Circular No. 1/{year}" for year in range(2014, 2021)]
    assert len({circular_identity(ref, "url") for ref in references}) == 7
    assert len({circular_identity(ref, "url", identity_version=1) for ref in references}) == 1


@pytest.mark.parametrize("reference", [
    "BPRD Circular No. 04 of 2025", "DMMD Circular Letter No. 03 of 2023",
    "BSD\xa0Circular No. 2 of 2001", "BC & CPD Circular No 01 of 2022",
    "SH&SFD Circular No. 05 of 2026 of 2026", "FD Circular No. 4",
    "DMMD Circular No. 20, 21 and 22 dated November 03, 2011", "Unreferenced notice", "",
])
def test_existing_references_unchanged(reference):
    assert circular_identity(reference, "url") == circular_identity(reference, "url", identity_version=1)


def test_year_precedence_and_conflicts():
    assert _reference_parts("FD Circular No. 4 / 2016", 2020)["year"] == 2016
    assert _reference_parts("FD Circular No. 4/2016 of 2017", 2020)["year"] == 2017
    assert reference_conflicts("FD Circular No. 4/2016 of 2017") == ["conflicting_explicit_years"]
    assert reference_conflicts("FD Circular No. 4 of 2016 of 2016") == []
    assert reference_conflicts("FD Circular No. 4 of 2016 of 2017")
    assert _reference_parts("FD Circular No. 4 dated May 8, 2003", 2020)["year"] == 2020
    assert _reference_parts("FD Circular No. 4 dated May 8, 2003")["year"] == 2003


def test_grouped_slash_year_and_letter():
    refs = list(iter_circular_references("FD Circular Letter No. 01, 2 and 3 / 2016"))
    assert [(ref.number, ref.year, ref.is_letter) for ref in refs] == [(1, 2016, True), (2, 2016, True), (3, 2016, True)]
    assert normalize_reference("FD Circular No. 4/2016") == "FD CIRCULAR NO 4 OF 2016"
    assert circular_identity("FD Circular No. 4/2016", "a") != circular_identity("FD Circular Letter No. 4/2016", "a")


def test_manifest_read_only_and_destination_conflict(tmp_path):
    path = tmp_path / "corpus.db"
    reference = "FD Circular No. 1/2016"
    old = circular_identity(reference, "url", identity_version=1)
    new = circular_identity(reference, "url")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE circulars (id TEXT PRIMARY KEY, reference TEXT, url TEXT, summary TEXT)")
        db.executemany("INSERT INTO circulars VALUES (?, ?, ?, ?)", [(old, reference, "url", "uncertain"), (new, reference, "url2", None)])
    before = path.read_bytes()
    report = plan_migration(path)
    assert path.read_bytes() == before
    assert report["collision_groups"] is None
    assert set(report["mappings"][0]["conflicts"]) == {"occupied_destination", "dependency_uniqueness_conflict", "analysis_provenance_unverified"}
    assert not list(tmp_path.glob("*chroma*"))


def test_pure_import_does_not_bootstrap_storage():
    source = str(Path(__file__).resolve().parents[1] / "src")
    command = f"import sys; sys.path.insert(0, {source!r}); import sbpeye.identity_migration; assert 'sbpeye.database' not in sys.modules; assert 'chromadb' not in sys.modules"
    subprocess.run([sys.executable, "-c", command], check=True)


@pytest.mark.parametrize("crash_phase", ["corpus_committed", "app_committed", "indexes_verified"])
def test_migration_resumes_after_commits_and_preserves_app_json(tmp_path, crash_phase):
    from sbpeye.identity_migration import apply_migration, identity_preflight
    corpus, app = tmp_path / "corpus.db", tmp_path / "app.db"
    ref = "FD Circular No. 1/2016"
    old, new = circular_identity(ref, "url", identity_version=1), circular_identity(ref, "url")
    with sqlite3.connect(corpus) as db:
        db.execute("CREATE TABLE circulars (id TEXT PRIMARY KEY, reference TEXT, url TEXT)")
        db.execute("INSERT INTO circulars VALUES (?,?,?)", (old, ref, "url"))
    with sqlite3.connect(app) as db:
        db.execute("CREATE TABLE chat_messages (id TEXT PRIMARY KEY, circular_ids TEXT, content TEXT)")
        db.execute("INSERT INTO chat_messages VALUES (?,?,?)", ("message", json.dumps([old]), f"Historical [[c:{old}]]"))
    manifest = plan_migration(corpus, app)
    repairs = []
    def crash(phase):
        if phase == crash_phase:
            raise RuntimeError("power loss")
    with pytest.raises(RuntimeError):
        apply_migration(manifest, tmp_path / "backups", repair_indexes=lambda value: repairs.append(value), phase_hook=crash)
    with sqlite3.connect(corpus) as db:
        with pytest.raises(ValueError, match="incomplete"):
            identity_preflight(db)
    result = apply_migration(manifest, tmp_path / "backups", repair_indexes=lambda value: repairs.append(value))
    assert result["phase"] == "complete"
    with sqlite3.connect(corpus) as db:
        identity_preflight(db)
        assert db.execute("SELECT id FROM circulars").fetchone()[0] == new
        assert db.execute("SELECT new_id FROM identity_alias WHERE old_id=?", (old,)).fetchone()[0] == new
    with sqlite3.connect(app) as db:
        row = db.execute("SELECT circular_ids, content FROM chat_messages").fetchone()
        assert json.loads(row[0]) == [new]
        assert old in row[1]
    count = len(repairs)
    apply_migration(manifest, tmp_path / "backups", repair_indexes=lambda value: repairs.append(value))
    assert len(repairs) == count


def test_reviewed_attachment_mapping_preserves_bytes_and_rekeys_ledger(tmp_path):
    import hashlib
    import uuid
    from sbpeye.identity_migration import apply_migration
    from sbpeye.index_identity import ledger_row_id
    corpus = tmp_path / "corpus.db"
    reference, url = "FD Circular No. 1/2016", "https://www.sbp.org.pk/circulars/fd-1"
    old, new = circular_identity(reference, url, identity_version=1), circular_identity(reference, url)
    detection_url = "https://www.sbp.org.pk/assets/documents/circulars/annex.pdf"
    attachment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{old}:{detection_url}"))
    new_attachment = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{new}:{detection_url}"))
    source = tmp_path / "cached.html"
    source.write_text(f'<h1>{reference}</h1><a href="{detection_url}">Annexure</a>', encoding="utf-8")
    archived = tmp_path / "archived.pdf"
    archived.write_bytes(b"%PDF original archive")
    with sqlite3.connect(corpus) as db:
        db.execute("CREATE TABLE circulars (id TEXT PRIMARY KEY, reference TEXT, url TEXT)")
        db.execute("INSERT INTO circulars VALUES (?,?,?)", (old, reference, url))
        db.execute("CREATE TABLE attachments (id TEXT PRIMARY KEY, circular_id TEXT REFERENCES circulars(id), original_url TEXT, local_path TEXT, is_vectorized INTEGER)")
        db.execute("INSERT INTO attachments VALUES (?,?,?,?,1)", (attachment_id, old, "https://www.sbp.org.pk/fallback.pdf", str(archived)))
        db.execute("CREATE TABLE semantic_index_sources (id TEXT PRIMARY KEY, source_kind TEXT, source_id TEXT, logical_kind TEXT, logical_document_id TEXT, status TEXT)")
        db.execute("INSERT INTO semantic_index_sources VALUES (?,?,?,?,?,?)", (ledger_row_id(f"attachment:{attachment_id}"), "attachment", attachment_id, "circular", old, "indexed"))
    initial = plan_migration(corpus)
    assert initial["conflicts"]
    evidence = {old: {"row_fingerprint": initial["mappings"][0]["row_fingerprint"], "attachments": [
        {"id": attachment_id, "detection_url": detection_url, "source_path": str(source),
         "source_hash": hashlib.sha256(source.read_bytes()).hexdigest(), "note": "Verified against surviving circular and archived annexure"}]}}
    reviewed = plan_migration(corpus, evidence=evidence)
    assert not reviewed["conflicts"]
    apply_migration(reviewed, tmp_path / "backups", repair_indexes=lambda manifest: None)
    with sqlite3.connect(corpus) as db:
        row = db.execute("SELECT id,circular_id,local_path,is_vectorized FROM attachments").fetchone()
        assert row == (new_attachment, new, str(archived), 0)
        ledger = db.execute("SELECT id,source_id,logical_document_id,status FROM semantic_index_sources").fetchone()
        assert ledger == (ledger_row_id(f"attachment:{new_attachment}"), new_attachment, new, "stale")
    assert archived.read_bytes() == b"%PDF original archive"
