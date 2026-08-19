"""A whole chat session as a standalone markdown file.

The per-message download in the chat view flattens every citation to its plain label,
which is right for text going back into the app: the reader has the pills a click away.
A transcript leaves that context — it is emailed, filed against a compliance note, read
on a machine that never runs SBPEye — so here each citation becomes a link to the
document on SBP's own site. That is the whole reason this is server-side: the frontend
holds labels, and only the database holds the URLs behind them.

Nothing is rendered that cannot be stood behind. A citation whose id no longer resolves
degrades to its label as ordinary prose rather than shipping a dead link, which is the
same bargain :mod:`sbpeye.citation_handles` strikes when it expands a handle.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .citation_handles import TOKEN_PATTERN, UUID_PATTERN, strip_bare_uuids
from .models import Attachment, ChatMessage, Circular, RegDocument, RegDocumentVersion

#: Display text for a token that arrived without a label and resolves to nothing.
_KIND_FALLBACK = {"circular": "Circular", "law": "Regulation", "attachment": "Attachment"}

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

MAX_FILENAME_STEM = 60


def _circular_url(circular: Circular) -> str | None:
    """The address a reader outside the app should be sent to.

    ``new_url`` first: after the redesign it is the live page, while ``old_url`` is the
    frozen archive.sbp.org.pk mirror — still correct, but the wrong thing to hand someone
    as the current text when a live page exists.
    """
    return circular.new_url or circular.url or circular.old_url


def _document_url(document: RegDocument, version_url: str | None) -> str | None:
    """A regulation's landing page, or the file itself when the page is unknown.

    Containers (a manual split into chapters) carry the page and no file; a flat law is
    often the reverse, so both halves are needed to cover the corpus.
    """
    return document.source_url or version_url


def _markdown_link(label: str, url: str | None) -> str:
    if not url:
        return label
    text = label.replace("[", r"\[").replace("]", r"\]")
    # Angle brackets are the only form that survives a URL carrying spaces or parentheses,
    # and SBP filenames carry both often enough to matter.
    target = f"<{url}>" if re.search(r"[\s()]", url) else url
    return f"[{text}]({target})"


class CitationIndex:
    """Everything the export needs to turn a citation id into a link, fetched in bulk.

    Built from the whole conversation at once: a long thread cites the same handful of
    circulars on nearly every turn, so resolving per token would repeat the same lookups
    dozens of times over one export.
    """

    def __init__(
        self,
        urls: dict[tuple[str, str], str],
        labels: dict[tuple[str, str], str],
    ) -> None:
        self._urls = urls
        self._labels = labels

    def url(self, kind: str, identifier: str) -> str | None:
        return self._urls.get((kind, identifier.lower()))

    def label(self, kind: str, identifier: str) -> str | None:
        return self._labels.get((kind, identifier.lower()))

    @classmethod
    def build(cls, db: Session, contents: list[str | None]) -> "CitationIndex":
        wanted: dict[str, set[str]] = {"circular": set(), "law": set(), "attachment": set()}
        for content in contents:
            for match in TOKEN_PATTERN.finditer(content or ""):
                identifier = match.group(2).strip()
                if identifier:
                    wanted[match.group(1)].add(identifier)
            # A bare uuid in stored text predates citation handles. It is a circular id or
            # it is nothing, so it is looked up with the rest and dropped if absent.
            wanted["circular"].update(UUID_PATTERN.findall(content or ""))

        urls: dict[tuple[str, str], str] = {}
        labels: dict[tuple[str, str], str] = {}

        if wanted["circular"]:
            for circular in db.query(Circular).filter(
                Circular.id.in_(sorted(wanted["circular"]))
            ).all():
                key = ("circular", circular.id.lower())
                labels[key] = circular.display_name
                url = _circular_url(circular)
                if url:
                    urls[key] = url

        if wanted["law"]:
            ids = sorted(wanted["law"])
            version_url = {
                version.document_id: version.file_url
                for version in db.query(RegDocumentVersion).filter(
                    RegDocumentVersion.document_id.in_(ids),
                    RegDocumentVersion.is_current == 1,
                ).all()
            }
            for document in db.query(RegDocument).filter(RegDocument.id.in_(ids)).all():
                key = ("law", document.id.lower())
                labels[key] = document.title
                url = _document_url(document, version_url.get(document.id))
                if url:
                    urls[key] = url

        if wanted["attachment"]:
            # Attachments reach the model by id from one tool and by filename from another,
            # so both are looked up and a miss on both is simply a label with no link.
            identifiers = sorted(wanted["attachment"])
            for attachment in db.query(Attachment).filter(
                or_(Attachment.id.in_(identifiers), Attachment.filename.in_(identifiers))
            ).all():
                labels[("attachment", attachment.id.lower())] = attachment.filename
                if attachment.original_url:
                    for identifier in (attachment.id, attachment.filename):
                        urls.setdefault(
                            ("attachment", identifier.lower()), attachment.original_url
                        )

        return cls(urls, labels)


def linkify_citations(content: str | None, index: CitationIndex, *, is_assistant: bool) -> str:
    """Rewrite one message's citation tokens as markdown links to sbp.org.pk.

    ``is_assistant`` gates the bare-uuid sweep. A stray uuid in an answer is a leftover
    from before handles and means nothing to a reader; in a question it is something the
    user typed, and the export has no business editing that.
    """
    if not content:
        return ""

    def replace_token(match: re.Match[str]) -> str:
        kind, identifier = match.group(1), match.group(2).strip()
        label = (match.group(3) or "").strip()
        label = label or index.label(kind, identifier) or _KIND_FALLBACK[kind]
        return _markdown_link(label, index.url(kind, identifier))

    linked = TOKEN_PATTERN.sub(replace_token, content)

    if is_assistant:
        # A bare uuid that resolves is a citation the model wrote without markup; the rest
        # is fabricated by construction (see citation_handles) and leaves with the
        # punctuation around it, so the sentence closes up.
        def replace_uuid(match: re.Match[str]) -> str:
            identifier = match.group(0)
            label = index.label("circular", identifier)
            return _markdown_link(label, index.url("circular", identifier)) if label else identifier

        linked = strip_bare_uuids(UUID_PATTERN.sub(replace_uuid, linked))

    return linked.strip()


def _format_timestamp(value: datetime | None) -> str | None:
    # Chat rows are written with datetime.utcnow(), so the zone is stated rather than
    # implied — a transcript is read somewhere else, often much later.
    return f"{value.strftime('%d %b %Y, %H:%M')} UTC" if value else None


def render_session_markdown(
    db: Session,
    title: str | None,
    messages: list[ChatMessage],
    *,
    exported_at: datetime | None = None,
) -> str:
    """Render a whole conversation, both sides, as markdown."""
    index = CitationIndex.build(db, [message.content for message in messages])
    stamp = (exported_at or datetime.utcnow()).strftime("%d %b %Y")

    lines = [
        f"# {title or 'SBPEye chat'}",
        "",
        f"*Exported from SBPEye on {stamp}. Citations link to the source documents on "
        "the State Bank of Pakistan's website.*",
    ]

    rendered = 0
    for message in messages:
        is_assistant = message.role == "assistant"
        body = linkify_citations(message.content, index, is_assistant=is_assistant)
        if not body:
            continue
        rendered += 1
        lines.extend(["", "---", "", f"## {'Assistant' if is_assistant else 'You'}"])
        timestamp = _format_timestamp(message.created_at)
        if timestamp:
            lines.extend(["", f"*{timestamp}*"])
        lines.extend(["", body])

    if not rendered:
        lines.extend(["", "---", "", "*This conversation has no messages yet.*"])

    return "\n".join(lines) + "\n"


def session_filename(title: str | None, *, exported_at: datetime | None = None) -> str:
    """A download name that survives every filesystem, derived from the session title."""
    stem = _UNSAFE_FILENAME.sub("-", (title or "").strip()).strip("-._")[:MAX_FILENAME_STEM]
    stamp = (exported_at or datetime.utcnow()).strftime("%Y-%m-%d")
    return f"sbpeye-chat-{stem.strip('-._') or 'session'}-{stamp}.md"
