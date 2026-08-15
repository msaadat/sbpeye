"""LLM analysis over the laws & regulations corpus.

The laws-side counterpart to `circular_ai.py`. See docs/LAWS_AI_PLAN.md — phases B and C:
turning a `RegDocument` into the document list the extractors already understand, running
summary/tags/checklist over it, and storing the result against the *edition* it describes.

`law_corpus` mirrors `checklist.build_checklist_corpus`: `(documents, gaps)`. Gaps are how a
result stays honest — a checklist over a container that holds no text of its own is not an
empty checklist, it is a checklist of nothing, and the difference has to survive into the
payload.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .ai import get_ai_client
from .database import SessionLocal
from .llm_debug import emit_event, trace_operation
from .laws_links import (
    find_law_references,
    identifying_names,
    law_label,
    mention_snippets,
)
from .models import (
    AIGenerationJob,
    CircularEntity,
    RegDocument,
    RegDocumentRelationship,
    RegDocumentVersion,
)


# Consolidation has no meaning here: it folds an amendment *chain* of circulars, and laws
# have no chain. Their equivalent is version diffing, which the plan defers.
LAW_GENERATION_FEATURES = ("summary", "tags", "checklist", "entities", "relationships")
LAW_GENERATION_ACTIONS = (*LAW_GENERATION_FEATURES, "all")
# What "all" actually runs. The checklist is the most expensive feature here — one LLM
# call per chunk, over documents that run to hundreds of pages — and is wanted far less
# often than the rest, so it is opt-in: ask for `checklist` by name to get one.
LAW_BULK_FEATURES = tuple(
    item for item in LAW_GENERATION_FEATURES if item != "checklist"
)


# Why a document yielded no analysable text. The first five are structural — the corpus is
# built this way on purpose and the row is not a failure — while the last three are.
GAP_CIRCULAR_BACKED = "law_backed_by_circular"
GAP_EXTERNAL = "law_external"
GAP_NO_CURRENT_VERSION = "law_no_current_version"
GAP_MANIFEST = "law_manifest"
GAP_UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
GAP_MISSING_TEXT = "missing_text"
GAP_MISSING_FILE = "missing_file"
GAP_DELISTED = "law_delisted"

# A gap that no amount of re-running fixes: the document is outside the corpus by design,
# and the UI should say so rather than offering a Generate button.
STRUCTURAL_GAPS = frozenset({
    GAP_CIRCULAR_BACKED,
    GAP_EXTERNAL,
    GAP_MANIFEST,
    GAP_UNSUPPORTED_FILE_TYPE,
})

# Docling reads PDFs and Markdown. A law version of any other file type has no path to a
# parse: `_convert_document` would hand `converter.convert()` a format it never registered
# and raise, reporting a crash where the truth is "we do not read spreadsheets".
PARSEABLE_LAW_FILE_TYPES = frozenset({"pdf", "html"})


def _gap(
    document: RegDocument,
    reason: str,
    version: RegDocumentVersion | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "doc_id": version.id if version is not None else document.id,
        "doc_type": "law",
        "doc_label": law_label(document),
        "reason": reason,
        **({"error": error} if error else {}),
    }


def law_corpus(document: RegDocument) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The analysable corpus for one law/regulation, and why anything was left out.

    At most one document comes back: the edition in force. Parts are *not* pulled in for a
    container — a chapter of the FE Manual is its own `RegDocument` with its own analysis,
    and rolling 26 of them into one prompt would produce a checklist that cites nothing
    usable. That is the plan's "the container is the document, the part is the unit of
    change", applied to analysis.

    Superseded editions are never analysed. Their text is still in SQLite and archived on
    disk, but an obligation extracted from wording SBP no longer publishes is worse than
    no obligation at all — the same rule the search index already follows.
    """
    from .scraper.laws import law_document

    if document.circular_id:
        # The instrument is already in the circular corpus. Analysing it here would produce
        # a second, drifting copy of the same document's analysis.
        return [], [_gap(document, GAP_CIRCULAR_BACKED)]
    if document.is_external:
        return [], [_gap(document, GAP_EXTERNAL)]
    if document.delisted_at is not None:
        return [], [_gap(document, GAP_DELISTED)]

    version = document.current_version
    if version is None:
        # A stub, or a listing row whose file SBP has never served us.
        return [], [_gap(document, GAP_NO_CURRENT_VERSION)]

    file_type = (version.file_type or "").lower()
    if file_type not in PARSEABLE_LAW_FILE_TYPES:
        # `manifest` is the container case and is bookkeeping rather than text; anything
        # else here is a real file we cannot read, and the two deserve different words.
        reason = GAP_MANIFEST if file_type == "manifest" else GAP_UNSUPPORTED_FILE_TYPE
        return [], [_gap(document, reason, version)]

    payload = law_document(document, version)
    local_path = payload.get("local_path")
    if local_path and not Path(local_path).is_file():
        # The row says we archived it and the archive disagrees. Falling back to the
        # extracted text would silently produce a parse with no page numbers, so say it.
        return [], [_gap(document, GAP_MISSING_FILE, version, error=local_path)]
    if not local_path and not payload["text"].strip():
        return [], [
            _gap(document, GAP_MISSING_TEXT, version, error=version.extraction_error)
        ]

    return [payload], []


def law_analysis_label(document: RegDocument) -> str:
    """What the extractors are told they are reading.

    The raw title, not the display title: "(Updated till July 16, 2026)" is exactly the
    context a model should have when it reads an obligation, even though the reader UI
    splits it off as state rather than name.
    """
    return law_label(document)


# Why a document cannot be analysed, in words a reader can act on. The endpoint returns
# these verbatim, so a refusal explains itself rather than saying "no content".
GAP_MESSAGES = {
    GAP_CIRCULAR_BACKED: (
        "SBP lists this among the regulations, but the document itself is a circular. "
        "Analyse it from the circular instead."
    ),
    GAP_EXTERNAL: "This document is published outside SBP, so we hold no copy to analyse.",
    GAP_DELISTED: "SBP has removed this document from its listing.",
    GAP_NO_CURRENT_VERSION: (
        "SBP's link to this file is broken, so nothing has been archived to analyse. "
        "We retry on every sync."
    ),
    GAP_MANIFEST: (
        "This is a collection with no text of its own. Analyse its parts individually."
    ),
    GAP_UNSUPPORTED_FILE_TYPE: "This document is a spreadsheet, which we cannot read.",
    GAP_MISSING_FILE: "The archived file for this edition is missing from disk.",
    GAP_MISSING_TEXT: "No text could be extracted from this document.",
}


def is_container(document: RegDocument) -> bool:
    """A document whose content is its parts — the FE Manual and six others.

    Its own version is a manifest hashed over its children, so it has no text to analyse.
    What it can have is a rollup of what its parts say.
    """
    return bool(document.children)


# What a container can be asked for. A checklist or a regulatory value has to come from
# wording, and a manifest has none; its parts are analysed in their own right and carry
# those. A summary and its tags can be rolled up, and the circular half of `relationships`
# reads the *circulars'* text, so it works on a container unchanged.
CONTAINER_FEATURES = ("summary", "tags", "relationships")


def rollup_sources(document: RegDocument) -> list[tuple[str, str]]:
    """(part label, summary) for each analysed part, in reading order.

    Chapter order, not alphabetical, and only parts that have actually been summarised —
    a rollup over three of twenty-six chapters would read as a summary of the manual.
    """
    parts = sorted(
        document.children, key=lambda c: (c.part_order is None, c.part_order or 0)
    )
    sources: list[tuple[str, str]] = []
    for part in parts:
        version = part.current_version
        if version is not None and (version.summary or "").strip():
            sources.append((part.part_label or part.title, version.summary))
    return sources


def gap_message(gaps: list[dict[str, Any]]) -> str:
    if not gaps:
        return "This document has no extracted content to analyze."
    reason = gaps[0].get("reason", "")
    return GAP_MESSAGES.get(reason, "This document has no extracted content to analyze.")


# --------------------------------------------------------------------- generation


def _summary_windows(text: str, limit: int) -> list[str]:
    """Split a document into the fewest prompt-sized windows, on paragraph boundaries.

    Word-window splitting rather than the Docling blocks the checklist uses: a summary
    needs no page-accurate provenance, and going through Docling would make summarising a
    law cost a multi-minute parse it does not need.
    """
    paragraphs = [para for para in re.split(r"\n\s*\n", text) if para.strip()]
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        # A single paragraph over the limit still has to go somewhere; it rides alone and
        # is clipped by the model's own context handling rather than silently dropped.
        if current and size + len(paragraph) > limit:
            windows.append("\n\n".join(current))
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph)
    if current:
        windows.append("\n\n".join(current))
    return windows or [text]


def summarize_law(client, label: str, text: str, subject: str, progress_callback=None) -> str:
    """Summarise one law, mapping over windows and reducing when it does not fit in one.

    `AIClient.summarize` alone would hand the model `_truncate_context`'s clip of the text,
    which on the default 4,000 budget is the first page of a 200-page manual. Windows are
    sized from the context budget so the single-window case — every circular and the
    median law — stays exactly one call.
    """
    budget_chars = max(4_000, client.resolve_context_budget() * 4)
    # `summarize` clips its input at `max_context_tokens` characters (the unit mismatch is
    # pre-existing); staying under it means the clip can never silently eat a window.
    limit = min(budget_chars, max(4_000, client.config.max_context_tokens))
    windows = _summary_windows(text, limit)

    if progress_callback:
        progress_callback(0, len(windows) + (1 if len(windows) > 1 else 0))
    if len(windows) == 1:
        summary = client.summarize(label, windows[0], subject=subject)
        if progress_callback:
            progress_callback(1, 1)
        return summary

    parts: list[str] = []
    for index, window in enumerate(windows, 1):
        parts.append(client.summarize(label, window, subject=subject))
        if progress_callback:
            progress_callback(index, len(windows) + 1)
    reduced = client.reduce_summaries(label, parts, subject=subject)
    if progress_callback:
        progress_callback(len(windows) + 1, len(windows) + 1)
    return reduced


def _circular_link_candidates(document: RegDocument) -> list[dict[str, Any]]:
    """Circulars that merely *name* this regulation, with the wording that names it.

    Only `name_match` edges are offered for typing. A `url_scan` edge is a hyperlink into
    the document — already the strongest evidence we have — and `listing` is SBP's own
    grouping; neither is improved by a model's opinion.

    A candidate with no quotable mention is dropped rather than sent. Measured on the live
    corpus, 110 of 794 name-matched edges produce no snippet — the name matches in the
    canonical form the index uses but not in the circular's text as written. Asking a model
    to classify a relationship it cannot see is asking it to guess, and the deterministic
    `references` those edges already carry is the more honest answer.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    names = identifying_names(document.normalized_title or document.title)
    for link in document.circular_links:
        circular = link.circular
        if circular is None or link.detected_via != "name_match":
            continue
        if circular.id in seen:
            continue
        seen.add(circular.id)
        snippets: list[str] = []
        for name in names:
            snippets.extend(mention_snippets(circular.content_text, name, limit=2))
            if snippets:
                break
        if not snippets:
            continue
        candidates.append({
            "id": circular.id,
            "label": f"{circular.reference or circular.title} — {circular.title}",
            "snippets": snippets,
            "link": link,
        })
    return candidates


def _pack_candidates(
    candidates: list[dict[str, Any]], budget_chars: int
) -> list[list[dict[str, Any]]]:
    """Group candidates into prompt-sized batches.

    The Banking Companies Ordinance is named by 314 circulars — the most-cited statute in
    the corpus — and one prompt carrying all of them is several hundred thousand
    characters. Packing is the same idea the checklist arm uses, applied to a different
    kind of item; a single oversized candidate rides alone rather than being dropped.
    """
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for candidate in candidates:
        cost = len(candidate.get("label") or candidate.get("title") or "") + sum(
            len(snippet) for snippet in candidate["snippets"]
        )
        if current and size + cost > budget_chars:
            batches.append(current)
            current, size = [], 0
        current.append(candidate)
        size += cost
    if current:
        batches.append(current)
    return batches


def _generate_relationships(
    client, db, document: RegDocument, progress_callback=None
) -> dict[str, int]:
    """Deterministic edges first, then one call each to say what they mean.

    Two questions get answered here, and they are not the same question. What this law
    says about other laws is in its own text. What a *circular* does to this law is in the
    circular's text — and that is the direction a reader of a regulation actually asks
    about, since a law is amended by circulars and never mentions them itself.
    """
    counts = {"law_edges": 0, "typed_circular_links": 0}
    references = find_law_references(db, document)
    circulars = _circular_link_candidates(document)
    budget_chars = max(4_000, client.resolve_context_budget() * 4)
    reference_batches = _pack_candidates(
        [
            {"id": ref.target_id, "title": ref.target_title, "snippets": ref.snippets}
            for ref in references
        ],
        budget_chars,
    )
    circular_batches = _pack_candidates(circulars, budget_chars)
    total = len(reference_batches) + len(circular_batches)
    completed = 0
    if progress_callback:
        progress_callback(0, total)

    # Rewritten wholesale: a regeneration must not leave an edge behind that the current
    # edition's text no longer supports.
    db.query(RegDocumentRelationship).filter(
        RegDocumentRelationship.source_document_id == document.id
    ).delete(synchronize_session=False)

    if references:
        classified: dict[str, dict[str, Any]] = {}
        for batch in reference_batches:
            classified.update(
                client.classify_law_references(law_analysis_label(document), batch)
            )
            completed += 1
            if progress_callback:
                progress_callback(completed, total)
        for reference in references:
            # A candidate the model did not classify still happened — the name is in the
            # text. It keeps the deterministic reading, which claims only a mention.
            verdict = classified.get(reference.target_id)
            db.add(RegDocumentRelationship(
                source_document_id=document.id,
                target_document_id=reference.target_id,
                target_reference=reference.target_title,
                type=verdict["type"] if verdict else "references",
                confidence=verdict["confidence"] if verdict else None,
                detected_via="ai" if verdict else "name_match",
            ))
            counts["law_edges"] += 1

    for batch in circular_batches:
        actions = client.classify_circular_law_actions(
            law_analysis_label(document),
            [{k: v for k, v in candidate.items() if k != "link"} for candidate in batch],
        )
        for candidate in batch:
            verdict = actions.get(candidate["id"])
            if not verdict:
                continue
            link = candidate["link"]
            link.link_type = verdict["type"]
            link.detected_via = "ai"
            link.confidence = verdict["confidence"]
            counts["typed_circular_links"] += 1
        completed += 1
        if progress_callback:
            progress_callback(completed, total)

    return counts


def _requested_features(
    feature: str,
    available: tuple[str, ...],
    bulk: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Expand `all` to `bulk`, and refuse a name nothing will act on.

    `bulk` is narrower than `available` where a feature is too expensive to run unasked:
    it stays individually requestable without riding along on every `all`.

    Without this an unrecognised feature runs every branch, matches none, and returns no
    outputs — which a batch caller cannot tell apart from a document that legitimately
    produced nothing. That is exactly how `sbpeye laws summarize` first reported two
    documents generated while writing nothing at all: the command is named `summarize`
    and the feature is `summary`.
    """
    if feature == "all":
        return bulk if bulk is not None else available
    if feature not in available:
        raise ValueError(
            f"Unknown feature {feature!r}. Expected one of: {', '.join(available)}."
        )
    return (feature,)


def _compute_container_outputs(
    client, db, document: RegDocument, feature: str, progress_callback=None
) -> dict[str, Any]:
    """Summarise a container from its parts, since it has no wording of its own."""
    features = _requested_features(feature, CONTAINER_FEATURES)
    label = law_analysis_label(document)
    subject = document.doc_type or "regulation"
    outputs: dict[str, Any] = {}

    for item in features:
        if item == "summary":
            sources = rollup_sources(document)
            if not sources:
                raise ValueError(
                    "None of this collection's parts have been summarised yet. "
                    "Analyse the parts first, then roll them up."
                )
            if progress_callback:
                progress_callback(0, 1)
            outputs["summary"] = client.reduce_summaries(
                label,
                [f"{part_label}: {summary}" for part_label, summary in sources],
                subject=subject,
            )
            if progress_callback:
                progress_callback(1, 1)
        elif item == "tags":
            basis = outputs.get("summary") or document.summary
            if not basis:
                raise ValueError("Summarise this collection before tagging it.")
            outputs["tags"] = client.generate_tags(label, basis, subject=subject)
        elif item == "relationships":
            outputs["relationships"] = _generate_relationships(
                client, db, document, progress_callback=progress_callback
            )
    return outputs


def _compute_outputs(
    client,
    db,
    document: RegDocument,
    version: RegDocumentVersion,
    documents: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    feature: str,
    progress_callback=None,
) -> dict[str, Any]:
    features = _requested_features(
        feature, LAW_GENERATION_FEATURES, bulk=LAW_BULK_FEATURES
    )
    label = law_analysis_label(document)
    subject = document.doc_type or "regulation"
    outputs: dict[str, Any] = {}

    # Tags read the summary rather than the raw text: tags drawn from the first window of
    # a 200-page manual describe its preface. When both are requested the summary is
    # already computed; when only tags are, it is worth the one extra call.
    for item in features:
        if item == "summary":
            summary = summarize_law(
                client, label, version.content_text or "", subject,
                progress_callback=progress_callback,
            )
            if not summary:
                raise ValueError("The model returned an empty summary.")
            outputs["summary"] = summary
        elif item == "tags":
            basis = outputs.get("summary") or version.summary
            if not basis:
                basis = summarize_law(
                    client, label, version.content_text or "", subject
                )
            outputs["tags"] = client.generate_tags(label, basis, subject=subject)
        elif item == "checklist":
            outputs["checklist"] = client.generate_checklist(
                label=label,
                documents=documents,
                gaps=gaps,
                progress_callback=progress_callback,
            )
        elif item == "entities":
            outputs["entities"] = client.extract_entities(
                label=label,
                documents=documents,
                progress_callback=progress_callback,
            )
        elif item == "relationships":
            # Writes its own edges rather than returning them: one half updates existing
            # `RegDocumentLink` rows in place, which is not an output to persist.
            outputs["relationships"] = _generate_relationships(
                client, db, document, progress_callback=progress_callback
            )
    return outputs


def _persist_outputs(
    db, document: RegDocument, version: RegDocumentVersion, outputs: dict[str, Any]
) -> None:
    generated_at = datetime.utcnow()
    # A container's rollup describes the collection, not the manifest row that happens to
    # represent it, and the manifest's hash changes whenever any part changes — which would
    # throw the rollup away for a reason that has nothing to do with it. Everything else is
    # version-level, per §4.
    subject = document if is_container(document) else version
    if "summary" in outputs:
        subject.summary = outputs["summary"]
        subject.summary_generated_at = generated_at
    if "tags" in outputs:
        subject.tags = json.dumps(outputs["tags"])
        subject.tags_generated_at = generated_at
    if "checklist" in outputs:
        version.compliance_checklist = json.dumps(outputs["checklist"])
        version.checklist_generated_at = generated_at
    if "entities" in outputs:
        # Scoped to this version, not to the document: a previous edition's values stay
        # attached to the edition that stated them, which is what makes "this limit moved
        # between the 2024 and 2026 editions" answerable at all.
        db.query(CircularEntity).filter(
            CircularEntity.version_id == version.id
        ).delete(synchronize_session=False)
        for entity in outputs["entities"]:
            db.add(CircularEntity(
                subject_kind="law",
                document_id=document.id,
                version_id=version.id,
                **entity,
            ))
        version.entities_generated_at = generated_at
    if "relationships" in outputs:
        # The edges were written by `_generate_relationships`; this is only the stamp
        # that lets the detail payload distinguish "none found" from "never run".
        version.relationships_generated_at = generated_at


def _run_law_generation_job(job_id: str) -> None:
    """Background worker for a law analysis job. Mirrors `circular_ai.run_generation_job`."""
    db = SessionLocal()
    try:
        job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        document = (
            db.query(RegDocument).filter(RegDocument.id == job.document_id).first()
        )
        if document is None:
            raise ValueError("This document no longer exists.")

        # Resolved once, before the run: the analysis is stored against the edition it
        # actually describes, and never against whatever is current when it finishes.
        version = document.current_version
        client = get_ai_client(db)

        def update_progress(completed: int, total: int) -> None:
            job.progress_completed = completed
            job.progress_total = total
            db.commit()

        if is_container(document):
            if job.feature != "all" and job.feature not in CONTAINER_FEATURES:
                raise ValueError(
                    f"A collection has no text of its own, so it cannot produce a "
                    f"{job.feature}. Analyse its parts instead."
                )
            outputs = _compute_container_outputs(
                client, db, document, job.feature, progress_callback=update_progress
            )
        else:
            documents, gaps = law_corpus(document)
            if not documents:
                raise ValueError(gap_message(gaps))
            outputs = _compute_outputs(
                client, db, document, version, documents, gaps, job.feature,
                progress_callback=update_progress,
            )
        _persist_outputs(db, document, version, outputs)
        job.status = "succeeded"
        if "checklist" in outputs:
            job.result_status = outputs["checklist"].get("status")
        job.completed_at = datetime.utcnow()
        db.commit()
        emit_event("normalized_result", outputs, stage="generation.result")
        emit_event("persisted_result", {
            "persisted": True, "job_id": job.id, "target_id": document.id,
            "version_id": version.id if version else None,
            "features": list(outputs),
        }, stage="generation.persist")
    except Exception as exc:
        db.rollback()
        job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
        if job:
            job.status = "failed"
            job.error = str(exc)
            job.completed_at = datetime.utcnow()
            db.commit()
        raise
    finally:
        db.close()


def run_law_generation_job(job_id: str) -> None:
    """Run one law web job inside a durable, correlated logical trace."""
    db = SessionLocal()
    try:
        job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
        if not job:
            return
        operation = f"law.{job.feature}"
        target_id = job.document_id
        metadata = {"feature": job.feature}
    finally:
        db.close()
    try:
        with trace_operation(
            operation, "web_generation", job_id=job_id,
            target_kind="law", target_id=target_id, metadata=metadata,
        ):
            _run_law_generation_job(job_id)
    except Exception:
        return
