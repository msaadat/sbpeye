from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from .models import Attachment, Circular


class Document(TypedDict):
    doc_id: str
    doc_type: Literal["circular", "attachment"]
    doc_label: str
    text: str
    file_type: str
    local_path: NotRequired[str]


def document_from_circular(circular: Circular) -> Document:
    text = circular.content_text or ""
    cache_file = Path(__file__).resolve().parents[2] / "cache" / "html" / f"{circular.id}.html"
    if cache_file.is_file():
        from .scraper.clean_html import extract_sbp_text

        refreshed_text = extract_sbp_text(cache_file.read_bytes())
        if refreshed_text:
            text = refreshed_text
    return {
        "doc_id": circular.id,
        "doc_type": "circular",
        "doc_label": (
            f"{circular.department or 'SBP'} - {circular.reference or circular.title}"
        ),
        "text": text,
        "file_type": "html",
    }


def document_from_attachment(attachment: Attachment) -> Document:
    document: Document = {
        "doc_id": attachment.id,
        "doc_type": "attachment",
        "doc_label": attachment.filename,
        "text": attachment.content_text or "",
        "file_type": attachment.file_type or "",
    }
    if local_path := getattr(attachment, "local_path", None):
        path = Path(local_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        document["local_path"] = str(path)
    return document


def build_corpus(circular: Circular) -> list[Document]:
    documents: list[Document] = []
    circular_document = document_from_circular(circular)
    if circular_document["text"].strip():
        documents.append(circular_document)

    for attachment in sorted(circular.attachments, key=lambda item: item.filename):
        if attachment.content_text and attachment.content_text.strip():
            documents.append(document_from_attachment(attachment))

    return documents
