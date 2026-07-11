"""Chain discovery, amendment folding, and consolidation API tests."""

import json
from datetime import datetime

from sbpeye.consolidation import (
    _apply_changes,
    generate_consolidation,
    mark_stale,
    resolve_chain,
    value_supported,
)
from sbpeye.models import Circular, CircularConsolidation, CircularRelationship

from conftest import make_circular


def amends(source_id: str, target_id: str | None) -> CircularRelationship:
    return CircularRelationship(
        source_id=source_id, target_id=target_id, type="amends"
    )


def adds_to(source_id: str, target_id: str | None) -> CircularRelationship:
    return CircularRelationship(
        source_id=source_id, target_id=target_id, type="adds_to"
    )


def seed_chain(db, count: int = 3) -> list[Circular]:
    """base <- c1 <- c2 ... each amending the previous one."""
    members = [
        make_circular(
            f"chain-{index}",
            reference=f"DMMD Circular No. {index + 1} of 2025",
            date=datetime(2025, 1 + index, 1),
            content_text=f"Policy rate shall be {10 + index}.50%.",
        )
        for index in range(count)
    ]
    db.add_all(members)
    for newer, older in zip(members[1:], members):
        db.add(amends(newer.id, older.id))
    db.commit()
    return members


class TestResolveChain:
    def test_resolves_same_chain_from_any_member(self, db_factory):
        db = db_factory()
        members = seed_chain(db)
        expected = [item.id for item in members]
        for member in members:
            chain = resolve_chain(db, member.id)
            assert [item.id for item in chain] == expected

    def test_single_circular_has_no_chain(self, db_factory):
        db = db_factory()
        db.add(make_circular("lonely"))
        db.commit()
        assert len(resolve_chain(db, "lonely")) == 1

    def test_unresolved_targets_are_excluded(self, db_factory):
        db = db_factory()
        db.add(make_circular("solo"))
        db.add(amends("solo", None))
        db.commit()
        assert len(resolve_chain(db, "solo")) == 1

    def test_survives_cycles(self, db_factory):
        db = db_factory()
        members = seed_chain(db, count=2)
        # Bogus back-edge: the base "amends" its own amender.
        db.add(amends(members[0].id, members[1].id))
        db.commit()
        chain = resolve_chain(db, members[0].id)
        assert [item.id for item in chain] == [item.id for item in members]

    def test_orders_by_date_oldest_first(self, db_factory):
        db = db_factory()
        newer = make_circular("newer", date=datetime(2025, 6, 1))
        older = make_circular("older", date=datetime(2024, 1, 1))
        db.add_all([newer, older])
        db.add(amends("newer", "older"))
        db.commit()
        chain = resolve_chain(db, "newer")
        assert [item.id for item in chain] == ["older", "newer"]

    def test_multi_target_amender_sees_all_its_targets(self, db_factory):
        db = db_factory()
        a = make_circular("a", date=datetime(2024, 1, 1))
        b = make_circular("b", date=datetime(2024, 2, 1))
        c = make_circular("c", date=datetime(2024, 3, 1))
        db.add_all([a, b, c])
        db.add(amends("c", "a"))
        db.add(amends("c", "b"))
        db.commit()
        assert [item.id for item in resolve_chain(db, "c")] == ["a", "b", "c"]

    def test_sibling_rulebooks_sharing_an_amender_stay_separate(self, db_factory):
        """From a base circular, a shared amender's OTHER targets don't join."""
        db = db_factory()
        a = make_circular("a", date=datetime(2024, 1, 1))
        b = make_circular("b", date=datetime(2024, 2, 1))
        c = make_circular("c", date=datetime(2024, 3, 1))
        db.add_all([a, b, c])
        db.add(amends("c", "a"))
        db.add(amends("c", "b"))
        db.commit()
        assert [item.id for item in resolve_chain(db, "a")] == ["a", "c"]
        assert [item.id for item in resolve_chain(db, "b")] == ["b", "c"]

    def test_adds_to_relationships_are_chain_members(self, db_factory):
        db = db_factory()
        base = make_circular("base", date=datetime(2024, 1, 1))
        addendum = make_circular("addendum", date=datetime(2024, 2, 1))
        amendment = make_circular("amendment", date=datetime(2024, 3, 1))
        db.add_all([base, addendum, amendment])
        db.add(adds_to(addendum.id, base.id))
        db.add(amends(amendment.id, addendum.id))
        db.commit()

        expected = ["base", "addendum", "amendment"]
        assert [item.id for item in resolve_chain(db, "base")] == expected
        assert [item.id for item in resolve_chain(db, "addendum")] == expected


class TestValueSupported:
    def test_matches_ignoring_whitespace_and_case(self):
        assert value_supported("11.50%", "rate will be at 11.50 % p.a.")

    def test_rejects_absent_value(self):
        assert not value_supported("12.75%", "rate will be at 11.50% p.a.")

    def test_empty_value_is_vacuously_supported(self):
        assert value_supported("", "anything")


class TestApplyChanges:
    def make_state(self):
        return [{
            "req_id": "r1",
            "section": "Policy Rate",
            "text": "Policy rate shall be 10.50%.",
            "value": "10.50%",
            "applies_to": "All banks",
            "status": "unchanged",
            "introduced_by": "base",
            "last_changed_by": None,
            "old_text": None,
            "old_value": None,
            "removed_by": None,
            "confidence": "high",
            "history": [],
        }]

    def amender(self, text="Policy rate shall be 11.50%.") -> Circular:
        return make_circular("amender-1", content_text=text)

    def test_modify_keeps_old_value_from_state(self, ):
        state = self.make_state()
        _apply_changes(state, [{
            "action": "modify", "req_id": "r1",
            "requirement": "Policy rate shall be 11.50%.", "value": "11.50%",
        }], self.amender())
        item = state[0]
        assert item["status"] == "modified"
        assert item["value"] == "11.50%"
        assert item["old_value"] == "10.50%"
        assert item["last_changed_by"] == "amender-1"
        assert item["confidence"] == "high"
        assert item["history"][-1]["new_value"] == "11.50%"

    def test_unsupported_new_value_demotes_confidence(self):
        state = self.make_state()
        _apply_changes(state, [{
            "action": "modify", "req_id": "r1",
            "requirement": "Policy rate shall be 12.75%.", "value": "12.75%",
        }], self.amender())
        assert state[0]["confidence"] == "low"

    def test_later_verifiable_change_restores_confidence(self):
        state = self.make_state()
        state[0]["confidence"] = "low"
        _apply_changes(state, [{
            "action": "modify", "req_id": "r1",
            "requirement": "Policy rate shall be 11.50%.", "value": "11.50%",
        }], self.amender())
        assert state[0]["confidence"] == "high"

    def test_add_and_remove(self):
        state = self.make_state()
        _apply_changes(state, [
            {"action": "add", "requirement": "Banks shall report weekly.",
             "section": "Reporting", "value": ""},
            {"action": "remove", "req_id": "r1"},
        ], self.amender())
        assert state[0]["status"] == "removed"
        assert state[0]["removed_by"] == "amender-1"
        added = state[1]
        assert added["status"] == "added"
        assert added["req_id"] == "r2"
        assert added["introduced_by"] == "amender-1"

    def test_unknown_req_id_is_ignored(self):
        state = self.make_state()
        _apply_changes(state, [{"action": "modify", "req_id": "r99", "requirement": "x" * 20}], self.amender())
        assert state[0]["status"] == "unchanged"


class FakeConsolidationClient:
    """Extracts one requirement from the base, modifies it per amendment."""

    def extract_requirements(self, title, reference, content_text):
        return [{
            "requirement": content_text,
            "section": "Policy Rate",
            "value": content_text.split("shall be ")[1].rstrip("."),
            "applies_to": "All banks",
        }]

    def align_requirements(self, *, current_requirements, amending_reference,
                           amending_title, amending_text):
        return [{
            "action": "modify",
            "req_id": current_requirements[0]["req_id"],
            "requirement": amending_text,
            "value": amending_text.split("shall be ")[1].rstrip("."),
        }]


class TestGenerateConsolidation:
    def test_folds_chain_and_persists_row(self, db_factory):
        db = db_factory()
        members = seed_chain(db, count=3)
        progress = []
        row = generate_consolidation(
            db, FakeConsolidationClient(), members[1],
            progress_callback=lambda done, total: progress.append((done, total)),
        )
        db.commit()

        assert row.chain_id == members[0].id
        assert row.as_of_circular_id == members[-1].id
        assert json.loads(row.member_ids) == [item.id for item in members]
        requirements = json.loads(row.requirements)
        assert len(requirements) == 1
        item = requirements[0]
        assert item["status"] == "modified"
        assert item["value"] == "12.50%"
        assert item["old_value"] == "11.50%"
        assert item["last_changed_by"] == members[-1].id
        assert [entry["circular_id"] for entry in item["history"]] == [
            members[1].id, members[2].id,
        ]
        assert progress[-1] == (3, 3)

    def test_requires_a_chain(self, db_factory):
        db = db_factory()
        db.add(make_circular("solo"))
        db.commit()
        try:
            generate_consolidation(db, FakeConsolidationClient(), db.get(Circular, "solo"))
        except ValueError as exc:
            assert "amendment chain" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_regeneration_upserts_the_same_row(self, db_factory):
        db = db_factory()
        members = seed_chain(db, count=2)
        generate_consolidation(db, FakeConsolidationClient(), members[0])
        db.commit()
        generate_consolidation(db, FakeConsolidationClient(), members[1])
        db.commit()
        assert db.query(CircularConsolidation).count() == 1


class TestMarkStale:
    def test_flags_chains_touching_affected_circulars(self, db_factory):
        db = db_factory()
        members = seed_chain(db, count=2)
        generate_consolidation(db, FakeConsolidationClient(), members[0])
        db.commit()
        mark_stale(db, {members[1].id})
        db.commit()
        assert db.query(CircularConsolidation).first().stale == 1

    def test_ignores_unrelated_circulars(self, db_factory):
        db = db_factory()
        members = seed_chain(db, count=2)
        generate_consolidation(db, FakeConsolidationClient(), members[0])
        db.commit()
        mark_stale(db, {"unrelated"})
        db.commit()
        assert db.query(CircularConsolidation).first().stale == 0


class TestConsolidationRoute:
    def test_not_found(self, client):
        test_client, _ = client
        assert test_client.get("/api/circulars/nope/consolidation").status_code == 404

    def test_unchained_circular_reports_unavailable(self, client):
        test_client, db_factory = client
        db = db_factory()
        db.add(make_circular("solo"))
        db.commit()
        body = test_client.get("/api/circulars/solo/consolidation").json()
        assert body == {"available": False, "chain": [], "consolidation": None}

    def test_chain_without_consolidation_reports_chain_only(self, client):
        test_client, db_factory = client
        db = db_factory()
        members = seed_chain(db)
        body = test_client.get(f"/api/circulars/{members[0].id}/consolidation").json()
        assert body["available"] is True
        assert body["chain_id"] == members[0].id
        assert [item["id"] for item in body["chain"]] == [item.id for item in members]
        assert body["consolidation"] is None

    def test_every_member_returns_the_stored_consolidation(self, client):
        test_client, db_factory = client
        db = db_factory()
        members = seed_chain(db)
        generate_consolidation(db, FakeConsolidationClient(), members[0])
        db.commit()
        for member in members:
            body = test_client.get(f"/api/circulars/{member.id}/consolidation").json()
            consolidation = body["consolidation"]
            assert consolidation is not None
            assert consolidation["as_of_circular_id"] == members[-1].id
            assert consolidation["requirements"][0]["value"] == "12.50%"
            assert consolidation["stale"] is False

    def test_grown_chain_reads_as_stale(self, client):
        test_client, db_factory = client
        db = db_factory()
        members = seed_chain(db)
        generate_consolidation(db, FakeConsolidationClient(), members[0])
        db.commit()
        newest = make_circular(
            "chain-99", date=datetime(2026, 1, 1),
            content_text="Policy rate shall be 13.50%.",
        )
        db.add(newest)
        db.add(amends(newest.id, members[-1].id))
        db.commit()
        body = test_client.get(f"/api/circulars/{members[0].id}/consolidation").json()
        assert body["consolidation"]["stale"] is True
