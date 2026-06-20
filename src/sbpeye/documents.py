from typing import Literal, TypedDict

from .models import Attachment, Circular


class Document(TypedDict):
    doc_id: str
    doc_type: Literal["circular", "attachment"]
    doc_label: str
    text: str
    file_type: str


def document_from_circular(circular: Circular) -> Document:
    return {
        "doc_id": circular.id,
        "doc_type": "circular",
        "doc_label": (
            f"{circular.department} - {circular.reference or circular.title}"
        ),
        "text": circular.content_text or "",
        "file_type": "html",
    }


def document_from_attachment(attachment: Attachment) -> Document:
    return {
        "doc_id": attachment.id,
        "doc_type": "attachment",
        "doc_label": attachment.filename,
        "text": attachment.content_text or "",
        "file_type": attachment.file_type or "",
    }


def build_corpus(circular: Circular) -> list[Document]:
    documents: list[Document] = []
    circular_document = document_from_circular(circular)
    if circular_document["text"].strip():
        documents.append(circular_document)

    for attachment in sorted(circular.attachments, key=lambda item: item.filename):
        if attachment.content_text and attachment.content_text.strip():
            documents.append(document_from_attachment(attachment))

    return documents
