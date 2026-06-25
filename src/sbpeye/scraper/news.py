"""Scrape the SBP homepage for press releases and "What's New" items."""

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from ..models import Circular
from .circulars import HEADERS

SBP_BASE = "https://www.sbp.org.pk"


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def scrape_sbp_news(db: Session | None = None) -> dict:
    """Return up to 5 press releases and 5 "What's New" links from the SBP homepage.

    Links that already exist as indexed circulars are rewritten to in-app view URLs.
    """
    resp = requests.get(f"{SBP_BASE}/index.html", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    press_releases = []
    whats_new = []

    pr_div = soup.find("div", id="PressRelease3")
    if pr_div:
        for li in pr_div.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            title = _normalize_title(a.get_text(strip=True))
            href = a.get("href", "")
            if not title or not href:
                continue
            if title.lower() in ("more", "clarifications/rebuttals"):
                continue
            if href.endswith("-U.pdf") or "urdu" in title.lower():
                continue
            url = urljoin(SBP_BASE + "/", href)
            if db and db.query(Circular).filter(Circular.url == url).first():
                url = f"/view_circular?cir={url}"
            press_releases.append({"title": title, "url": url})
            if len(press_releases) >= 5:
                break

    for table in soup.find_all("table"):
        table_text = table.get_text()[:200].lower()
        if "what" in table_text and "new" in table_text:
            box = table.find("div", class_="box")
            if not box:
                continue
            for li in box.find_all("li"):
                a = li.find("a")
                if not a:
                    continue
                title = _normalize_title(a.get_text(strip=True))
                href = a.get("href", "")
                if not title or not href:
                    continue
                if title.lower() == "more":
                    continue
                url = urljoin(SBP_BASE + "/", href)
                if db and db.query(Circular).filter(Circular.url == url).first():
                    url = f"/view_circular?cir={url}"
                whats_new.append({"title": title, "url": url})
                if len(whats_new) >= 5:
                    break
            break

    return {"press_releases": press_releases, "whats_new": whats_new}
