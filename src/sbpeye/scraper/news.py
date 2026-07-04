"""Scrape the SBP homepage for press releases and "What's New" items."""

import re
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ..models import Circular
from .circulars import HEADERS

SBP_BASE = "https://www.sbp.org.pk"


MAX_ITEMS = 8


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _localize_url(url: str, db: Session | None) -> str:
    """Rewrite a link that matches an indexed circular to its in-app view URL."""
    if not db:
        return url
    match = (
        db.query(Circular)
        .filter((Circular.url == url) | (Circular.new_url == url))
        .first()
    )
    return f"/view_circular?cir={url}" if match else url


def parse_news_cards(soup: BeautifulSoup) -> list[dict]:
    """Parse the homepage "What's New" feed into {title, date, href} items.

    On the redesigned site each item is a ``.what-new .news-card`` wrapping a single
    anchor with a ``p.date`` and ``p.title``.
    """
    section = soup.select_one(".what-new") or soup
    items: list[dict] = []
    for card in section.select(".news-card"):
        anchor = card.find("a", href=True)
        if not anchor:
            continue
        title_el = card.select_one(".title")
        date_el = card.select_one(".date")
        title = _normalize_title(title_el.get_text(strip=True) if title_el else anchor.get_text(strip=True))
        href = anchor["href"].strip()
        if not title or not href:
            continue
        if href.lower().endswith("-u.pdf") or "urdu" in title.lower():
            continue
        items.append({
            "title": title,
            "date": _normalize_title(date_el.get_text(strip=True)) if date_el else "",
            "href": href,
        })
    return items


def scrape_sbp_news(db: Session | None = None) -> dict:
    """Return recent press releases and other "What's New" items from the SBP homepage.

    Items whose link is an indexed circular are rewritten to in-app view URLs. Press
    releases (documents under the press-release store) and other new items are returned
    in separate lists to preserve the existing API shape.
    """
    resp = cloudscraper.create_scraper().get(f"{SBP_BASE}/", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    press_releases: list[dict] = []
    whats_new: list[dict] = []
    for card in parse_news_cards(soup):
        url = urljoin(SBP_BASE + "/", card["href"])
        item = {"title": card["title"], "date": card["date"], "url": _localize_url(url, db)}
        bucket = press_releases if "press-release" in card["href"].lower() else whats_new
        if len(bucket) < MAX_ITEMS:
            bucket.append(item)

    return {"press_releases": press_releases, "whats_new": whats_new}
