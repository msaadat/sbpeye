"""Tests for the July-2026 SBP site-redesign scraper rebuild.

Covers reference-based identity, the new listing/detail/ecodata/news parsers, anchor
harvesting, and the LLM-preservation migration. All offline (inline HTML, in-memory DB).
"""

from datetime import datetime

from bs4 import BeautifulSoup

from sbpeye.link_routing import normalize_reference, harvest_reference_links
from sbpeye.scraper.clean_html import extract_sbp_text
from sbpeye.scraper.circulars import parse_circular_listing, circular_identity
from sbpeye.scraper.ecodata_index import parse_ecodata_index
from sbpeye.scraper.news import parse_news_cards
from sbpeye.migration import snapshot_llm_data, apply_llm_snapshot
from sbpeye.models import Circular, CircularEntity, CircularRelationship

from tests.conftest import make_circular


# --- reference normalization + identity -------------------------------------------------

def test_normalize_reference_is_site_independent():
    assert normalize_reference("BPRD Circular No. 04 of 2025") == "BPRD CIRCULAR NO 4 OF 2025"
    assert normalize_reference(" bprd  circular no 4 of 2025 ") == "BPRD CIRCULAR NO 4 OF 2025"
    assert normalize_reference("DMMD Circular Letter No. 03 of 2023") == "DMMD CIRCULAR LETTER NO 3 OF 2023"
    assert normalize_reference("Just a title") is None


def test_circular_identity_stable_across_urls():
    old = circular_identity("BPRD Circular No. 04 of 2025", "https://www.sbp.org.pk/bprd/2025/C4.htm")
    new = circular_identity("BPRD Circular No. 4 of 2025", "https://www.sbp.org.pk/circulars/bprd-circular-no-4-of-2025")
    assert old == new


def test_circular_identity_falls_back_to_url_when_unreferenced():
    a = circular_identity("", "https://www.sbp.org.pk/circulars/notice-a")
    b = circular_identity("", "https://www.sbp.org.pk/circulars/notice-b")
    assert a != b


# --- listing parser ---------------------------------------------------------------------

LISTING_HTML = """
<div class="publication-box-new">
  <h4 class="mb-2"><a href="https://www.sbp.org.pk/circulars/bprd-circular-no-4-of-2025">Prudential Regulations</a></h4>
  <p class="mb-3 date"> BPRD Circular No. 04 of 2025</p>
  <p class="date">January 15 2025 | <span class="dept">BPRD</span> | <span class="cat">Banking</span> | <span class="type">Circulars</span></p>
</div>
"""


def test_parse_circular_listing_extracts_all_fields():
    items = parse_circular_listing(BeautifulSoup(LISTING_HTML, "html.parser"))
    assert len(items) == 1
    item = items[0]
    assert item["reference"] == "BPRD Circular No. 04 of 2025"
    assert item["date"] == "January 15 2025"
    assert item["department"] == "BPRD"
    assert item["category"] == "Banking"
    assert item["doc_type"] == "Circulars"
    assert item["year"] == "2025"
    assert item["url"] == "https://www.sbp.org.pk/circulars/bprd-circular-no-4-of-2025"


# --- ecodata parser ---------------------------------------------------------------------

ECODATA_HTML = """
<h2 class="sector-heading">Monetary and Financial Sector</h2>
<h5 class="primary-color">Auction Profile And Results</h5>
<table class="table economic-data-table single-column-table"><tbody>
  <tr data-category="auction"><td>
    <div class="data-row-wrapper">
      <div class="left-content">
        <h6>Auction Result of CNY Loan Facility</h6>
        <small class="text-muted"><span class="pill-badge">Occasionally</span><span class="pill-badge">December 04, 2013</span></small>
      </div>
      <div class="right-content"><a href="https://www.sbp.org.pk/assets/document/CNY_1.pdf"><img/></a></div>
    </div>
  </td></tr>
</tbody></table>
"""


def test_parse_ecodata_index():
    entries = parse_ecodata_index(BeautifulSoup(ECODATA_HTML, "html.parser"))
    assert len(entries) == 1
    e = entries[0]
    assert e["section"] == "Monetary and Financial Sector"
    assert e["subsection"] == "Auction Profile And Results"
    assert e["description"] == "Auction Result of CNY Loan Facility"
    assert e["frequency"] == "Occasionally"
    assert e["last_update"] == "December 04, 2013"
    assert e["format_type"] == "pdf"
    assert e["format_url"].endswith("/assets/document/CNY_1.pdf")


# --- news parser ------------------------------------------------------------------------

NEWS_HTML = """
<div class="what-new">
  <div class="news-card"><a href="https://www.sbp.org.pk/assets/documents/press-release/Pr-1.pdf">
    <p class="date">June 30 2026</p><p class="title">SBP Unveils New Website</p></a></div>
  <div class="news-card"><a href="https://www.sbp.org.pk/circulars/bprd-circular-no-4-of-2025">
    <p class="date">June 24 2026</p><p class="title">Prudential Regulations</p></a></div>
</div>
"""


def test_parse_news_cards():
    cards = parse_news_cards(BeautifulSoup(NEWS_HTML, "html.parser"))
    assert [c["title"] for c in cards] == ["SBP Unveils New Website", "Prudential Regulations"]
    assert cards[0]["date"] == "June 30 2026"
    press = [c for c in cards if "press-release" in c["href"].lower()]
    assert len(press) == 1


# --- anchor harvesting ------------------------------------------------------------------

def test_harvest_reference_links_resolves_anchors(db_factory):
    db = db_factory()
    source = make_circular("src", reference="DMMD Circular Letter No. 9 of 2026",
                           url="https://www.sbp.org.pk/circulars/dmmd-circular-letter-no-9-of-2026",
                           new_url="https://www.sbp.org.pk/circulars/dmmd-circular-letter-no-9-of-2026")
    target = make_circular("tgt", reference="BSD Circular No. 18 of 2001",
                           url="https://www.sbp.org.pk/circulars/bsd-circular-no-18",
                           new_url="https://www.sbp.org.pk/circulars/bsd-circular-no-18")
    db.add_all([source, target])
    db.commit()

    html = (
        '<div class="circular-body">See '
        '<a href="https://www.sbp.org.pk/circulars/bsd-circular-no-18">BSD Circular No. 18</a> and '
        '<a href="https://www.sbp.org.pk/index.php?/circulars/P30">next</a>.</div>'
    )
    targets = harvest_reference_links(html, db, source)
    assert [t.id for t in targets] == ["tgt"]


# --- migration roundtrip ----------------------------------------------------------------

def test_snapshot_and_reattach_preserves_llm_data(db_factory):
    db = db_factory()
    a = make_circular("old-a", reference="BPRD Circular No. 4 of 2025",
                      url="https://www.sbp.org.pk/bprd/2025/C4.htm", summary="Summary A")
    b = make_circular("old-b", reference="BSD Circular No. 18 of 2001",
                      url="https://www.sbp.org.pk/bsd/2001/C18.htm")
    db.add_all([a, b])
    db.flush()
    db.add(CircularRelationship(source_id=a.id, target_id=b.id,
                                target_reference="BSD Circular No. 18 of 2001",
                                type="amends", confidence=0.9))
    db.add(CircularEntity(circular_id=a.id, entity_type="ratio", metric="CAR",
                          value_numeric=8.0, unit="%"))
    db.commit()

    snapshot = snapshot_llm_data(db)
    assert set(snapshot["circulars"]) == {"BPRD CIRCULAR NO 4 OF 2025", "BSD CIRCULAR NO 18 OF 2001"}

    # Simulate the from-scratch rebuild: drop old rows, re-add with reference-based ids.
    db.query(CircularRelationship).delete()
    db.query(CircularEntity).delete()
    db.query(Circular).delete()
    db.commit()
    na = make_circular(circular_identity("BPRD Circular No. 04 of 2025", "u1"),
                       reference="BPRD Circular No. 04 of 2025",
                       url="https://www.sbp.org.pk/circulars/bprd-circular-no-04-of-2025",
                       new_url="https://www.sbp.org.pk/circulars/bprd-circular-no-04-of-2025",
                       summary=None)
    nb = make_circular(circular_identity("BSD Circular No. 18 of 2001", "u2"),
                       reference="BSD Circular No. 18 of 2001",
                       url="https://www.sbp.org.pk/circulars/bsd-circular-no-18-of-2001",
                       new_url="https://www.sbp.org.pk/circulars/bsd-circular-no-18-of-2001")
    db.add_all([na, nb])
    db.commit()

    stats = apply_llm_snapshot(db, snapshot)
    assert stats["matched"] == 2
    assert stats["unmatched_snapshot"] == []

    na = db.query(Circular).filter(Circular.id == na.id).first()
    assert na.summary == "Summary A"
    assert na.old_url == "https://www.sbp.org.pk/bprd/2025/C4.htm"
    assert db.query(CircularEntity).count() == 1

    rel = db.query(CircularRelationship).one()
    assert rel.type == "amends"
    assert rel.target_id == nb.id
    assert rel.confidence == 0.9
    assert db.query(Circular).filter(Circular.id == nb.id).one().status == "amended"


# --- detail-page text extraction --------------------------------------------------------

def _detail_html(*, chrome_no_print: bool) -> bytes:
    """A new-site circular detail page.

    Mirrors the real structure: the <h2> heading and <h5> date sit in sibling chrome divs
    outside div.border-box, and SBP inconsistently tags those chrome divs `no-print`. The
    letter body + signature live inside div.border-box regardless.
    """
    chrome_cls = " no-print" if chrome_no_print else ""
    return f"""
    <div class="pdfcontenttodownload">
      <div class="col-12 mb-5{chrome_cls}"><h2>Discontinuation of the Scheme</h2></div>
      <div class="col-12 d-flex{chrome_cls}">
        <h5 class="mb-0">July 02, 2026</h5>
        <div class="text-end no-print"><button id="downloadPdfBtn">Download PDF</button></div>
      </div>
      <div class="col-12">
        <div class="border-box bg-white p-3">
          <span id="automationPathHolder" style="display:none;"></span><font color="#fff">38894</font>
          <div class="circular-body">
            <div class="row"><div class="col-lg-12 mx-auto">
              <p><b>The Presidents/Chief Executives</b></p>
              <p>Dear Sir/Madam,</p>
              <p><b><u>Discontinuation of the Scheme</u></b></p>
              <p>Attention is invited to the subject scheme.</p>
            </div></div>
          </div>
          <div class="bottom mt-2 text-end"><div class="row"><div class="col-12"><div class="w-max ms-auto">
            <p>Yours sincerely,</p>
            <p>Dr. Asif Ali Director</p>
          </div></div></div></div>
        </div>
      </div>
    </div>
    """.encode()


def test_extract_sbp_text_excludes_heading_and_date_regardless_of_no_print():
    tagged = extract_sbp_text(_detail_html(chrome_no_print=True))
    untagged = extract_sbp_text(_detail_html(chrome_no_print=False))

    # The `no-print` tagging on the heading/date chrome is inconsistent upstream, but
    # scoping to div.border-box makes extraction identical either way.
    assert tagged == untagged

    # Body-only: starts at the salutation, keeps the signature, drops the <h2> heading and
    # <h5> date (both stored separately as Circular.title / Circular.date). The in-body
    # subject line survives — only the leading heading/date chrome is removed.
    assert tagged.startswith("The Presidents/Chief Executives")
    assert "Yours sincerely," in tagged
    assert "Dr. Asif Ali" in tagged
    assert not tagged.startswith("Discontinuation of the Scheme")
    assert "July 02, 2026" not in tagged
    assert "Download PDF" not in tagged
    assert "38894" not in tagged
