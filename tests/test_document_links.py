from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.database import Base
from sbpeye.link_routing import (
    normalize_sbp_url,
    resolve_reference_in_context,
    rewrite_document_links,
)
from sbpeye.models import Attachment, Circular


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_circular(circular_id: str, url: str) -> Circular:
    return Circular(
        id=circular_id,
        title="Test circular",
        department="BPRD",
        date=datetime(2025, 1, 1),
        url=url,
        content_text="Body",
    )


def test_normalize_sbp_url_restricts_hosts_and_credentials():
    assert normalize_sbp_url("http://www.sbp.org.pk/a.htm#top") == "https://www.sbp.org.pk/a.htm"
    assert normalize_sbp_url("https://files.sbp.org.pk/a.pdf") == "https://files.sbp.org.pk/a.pdf"

    for url in (
        "https://example.com/a.pdf",
        "https://sbp.org.pk.example.com/a.pdf",
        "https://user@sbp.org.pk/a.pdf",
        "file:///tmp/a.pdf",
    ):
        with pytest.raises(ValueError):
            normalize_sbp_url(url)


def test_source_links_become_internal_document_pills():
    db = make_session()
    current = make_circular("current", "https://www.sbp.org.pk/current.htm")
    known = make_circular("known", "https://www.sbp.org.pk/known.htm")
    attachment = Attachment(
        id="rules",
        circular_id=current.id,
        filename="rules.pdf",
        original_url="https://www.sbp.org.pk/files/rules.pdf",
        file_type="pdf",
        extraction_status="extracted",
    )
    db.add_all([current, known, attachment])
    db.commit()

    result = rewrite_document_links(
        """
        <body>
          <a href="https://www.sbp.org.pk/known.htm">Known circular</a>
          <a href="https://www.sbp.org.pk/new.htm">New circular</a>
          <a href="https://www.sbp.org.pk/files/rules.pdf">Rules</a>
          <a href="https://example.com/external">External</a>
        </body>
        """,
        current,
        db,
    )
    soup = BeautifulSoup(result, "html.parser")
    links = soup.find_all("a")

    assert links[0]["href"] == "/circulars/known"
    assert links[1]["href"] == "https://www.sbp.org.pk/new.htm"
    assert links[2]["href"] == "/documents/open?id=rules"
    assert "document-pill" in links[0].get("class", [])
    assert "document-pill" not in links[1].get("class", [])
    assert "document-pill" in links[2].get("class", [])
    assert links[3]["href"] == "https://example.com/external"
    assert "document-pill" not in links[3].get("class", [])


def _make_dmmd(circular_id: str, number: int, year: int) -> Circular:
    return Circular(
        id=circular_id,
        title=f"DMMD Circular No. {number}",
        department="DMMD",
        date=datetime(year, 11, 3),
        url=f"https://www.sbp.org.pk/dmmd/{year}/{circular_id}.htm",
        reference=f"DMMD Circular No. {number}",
        content_text="Body",
    )


def _grouped_reference_session():
    db = make_session()
    source = Circular(
        id="source",
        title="DMMD Circular Letter No. 01",
        department="DMMD",
        date=datetime(2012, 6, 29),
        url="https://www.sbp.org.pk/source.htm",
        reference="DMMD Circular Letter No. 01",
        content_text=(
            "Please refer to DMMD Circular no. 20, 21 and 22 dated November 03, 2011 "
            "regarding maintenance of CRR, SLR and Cash Reserves."
        ),
    )
    db.add_all(
        [
            source,
            _make_dmmd("c20-2011", 20, 2011),
            _make_dmmd("c20-2013", 20, 2013),  # same number, different year
            _make_dmmd("c21-2011", 21, 2011),
            _make_dmmd("c22-2011", 22, 2011),
            _make_dmmd("c200-2011", 200, 2011),  # prefix-substring decoy of "20"
        ]
    )
    db.commit()
    return db, source


def test_grouped_references_link_each_number_with_year_inference():
    db, source = _grouped_reference_session()

    result = rewrite_document_links(
        f"<p>{source.content_text}</p>", source, db
    )
    soup = BeautifulSoup(result, "html.parser")
    links = soup.find_all("a")

    # All three numbers in the group resolve, disambiguated to the 2011 records.
    assert [a["href"] for a in links] == [
        "/circulars/c20-2011",
        "/circulars/c21-2011",
        "/circulars/c22-2011",
    ]
    # The grouped numbers are linked as bare numbers; surrounding text is preserved.
    assert [a.string for a in links] == ["DMMD Circular no. 20", "21", "22"]
    assert "DMMD Circular no. 20, 21 and 22 dated November 03, 2011" in soup.get_text()


def test_resolve_reference_in_context_uses_content_year():
    db, source = _grouped_reference_session()

    # The bare reference (no year) is disambiguated by the nearby date in the source content,
    # and "No. 20" must not resolve to the "No. 200" decoy.
    assert resolve_reference_in_context(db, source, "DMMD Circular no. 20").id == "c20-2011"
    assert resolve_reference_in_context(db, source, "DMMD Circular no. 21").id == "c21-2011"
    assert resolve_reference_in_context(db, source, "DMMD Circular no. 22").id == "c22-2011"
