from pathlib import Path
from urllib.parse import urldefrag, urlencode, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import Attachment, Circular


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}


def is_allowed_sbp_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and (hostname == "sbp.org.pk" or hostname.endswith(".sbp.org.pk"))
        and parsed.username is None
        and parsed.password is None
    )


def normalize_sbp_url(url: str) -> str:
    normalized = urldefrag(url.strip())[0]
    if normalized.startswith("http://"):
        normalized = "https://" + normalized[7:]
    if not is_allowed_sbp_url(normalized):
        raise ValueError("Only HTTPS links on sbp.org.pk are supported.")
    return normalized


def attachment_info(url: str) -> dict:
    path = Path(urlparse(url).path)
    extension = path.suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS:
        raise ValueError("This SBP link is not a supported document.")
    return {"url": url, "filename": path.name or f"document{extension}", "file_type": extension.lstrip(".")}


def rewrite_document_links(html: str, circular: Circular, db: Session) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        try:
            url = normalize_sbp_url(anchor.get("href", ""))
        except ValueError:
            continue
        known = db.query(Circular.id).filter(func.lower(Circular.url) == url.lower()).first()
        attachment = db.query(Attachment.id).filter(
            func.lower(Attachment.original_url) == url.lower()
        ).first()
        if known:
            target, kind = f"/circulars/{known[0]}", "circular"
        elif attachment:
            target = f"/documents/open?{urlencode({'id': attachment[0]})}"
            kind = Path(urlparse(url).path).suffix.lstrip(".").upper() or "document"
        else:
            continue
        anchor["href"] = target
        anchor["class"] = list(dict.fromkeys([*(anchor.get("class") or []), "document-pill"]))
        anchor["data-document-link"] = "true"
        anchor["data-document-kind"] = kind
        anchor.attrs.pop("target", None)
        anchor.attrs.pop("rel", None)
    return str(soup)
