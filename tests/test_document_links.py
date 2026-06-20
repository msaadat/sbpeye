from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.database import Base
from sbpeye.link_routing import normalize_sbp_url, rewrite_document_links
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
