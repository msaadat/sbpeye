"""Tests for acronym/expansion-aware metric matching in regulatory-value search.

`resolve_metric_terms` bridges the gap between how a user types a metric ("CRR")
and how the LLM stored it ("Cash Reserve Requirement (average)"), reusing the
existing SBP SYNONYMS dictionary.
"""

from datetime import datetime

from sbpeye.models import CircularEntity
from sbpeye.search import resolve_metric_terms

from conftest import make_circular


def test_acronym_resolves_to_expansion():
    metrics = [
        "Cash Reserve Requirement (average)",
        "Cash Reserve Requirement (daily minimum)",
        "Capital Adequacy Ratio",
    ]
    matched = resolve_metric_terms("CRR", metrics)
    assert matched == [
        "Cash Reserve Requirement (average)",
        "Cash Reserve Requirement (daily minimum)",
    ]
    assert "Capital Adequacy Ratio" not in matched


def test_expansion_resolves_to_acronym():
    # Reverse direction: full name should find a metric stored as the acronym.
    assert resolve_metric_terms("cash reserve requirement", ["CRR", "CAR", "LCR"]) == ["CRR"]


def test_car_matches_capital_adequacy_ratio():
    assert resolve_metric_terms("CAR", ["Capital Adequacy Ratio", "CRR"]) == ["Capital Adequacy Ratio"]


def test_partial_phrase_matches_both_forms():
    matched = resolve_metric_terms("capital adequacy", ["CAR", "Capital Adequacy Ratio", "CCB"])
    assert set(matched) == {"CAR", "Capital Adequacy Ratio"}
    assert "CCB" not in matched


def test_unknown_term_returns_empty_for_substring_fallback():
    # No synonym group and no substring hit → empty, so callers fall back to ilike
    # (i.e. never fewer results than the old plain-substring behavior).
    assert resolve_metric_terms("totally novel metric", ["CAR", "LCR"]) == []


def test_substring_match_is_preserved():
    # A plain substring still matches even without a synonym entry.
    matched = resolve_metric_terms("exposure", ["Exposure Limit", "Single Party Exposure", "CAR"])
    assert set(matched) == {"Exposure Limit", "Single Party Exposure"}


def _seed_entity(db, circular_id, metric, **overrides):
    fields = dict(
        circular_id=circular_id,
        entity_type="ratio",
        metric=metric,
        comparator="min",
        value_numeric=5.0,
        unit="%",
        value_text="5%",
        created_at=datetime(2025, 1, 1),
    )
    fields.update(overrides)
    db.add(CircularEntity(**fields))


def test_entity_query_endpoint_resolves_acronym(client):
    test_client, db_factory = client
    db = db_factory()
    try:
        db.add(make_circular(circular_id="c1"))
        db.flush()
        _seed_entity(db, "c1", "Cash Reserve Requirement (average)")
        _seed_entity(db, "c1", "Capital Adequacy Ratio")
        db.commit()
    finally:
        db.close()

    resp = test_client.get("/api/circulars/entities/query", params={"metric": "CRR"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["results"][0]["metric"] == "Cash Reserve Requirement (average)"
