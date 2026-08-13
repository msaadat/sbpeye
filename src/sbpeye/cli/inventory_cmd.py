"""CLI adapter for inventory search. Transport only — no search logic lives here."""

import json

import click

from sbpeye.database import SessionLocal


@click.group("inventory")
def inventory():
    """Exhaustive regulatory inventory search (see docs/INVENTORY_SEARCH_PLAN.md)."""


@inventory.command("status")
def inventory_status():
    """Show what the semantic index can and cannot currently search."""
    from sbpeye.database import collection, embedding_config
    from sbpeye.inventory.ledger import reconcile, snapshot_id

    db = SessionLocal()
    try:
        report = reconcile(db, collection, embedding_config, write=False)
        click.echo(f"Snapshot:          {snapshot_id(db)[:24]}...")
        click.echo(f"Embedding:         {embedding_config.model}")
        click.echo(f"Chunker:           {report.chunker_version}")
        click.echo(f"Searchable sources:{report.searchable_sources:>7}")
        click.echo(f"  indexed:         {report.status_counts.get('indexed', 0):>7}")
        click.echo(f"  stale/missing:   {report.status_counts.get('stale', 0):>7}")
        click.echo(f"Expected chunks:   {report.expected_chunks:>7}")
        click.echo(f"Stored chunks:     {report.indexed_chunks:>7}")
        click.echo(f"Orphan chunks:     {report.orphan_chunks:>7}")
        if report.unsearchable:
            click.echo("Unsearchable sources:")
            for status, count in sorted(report.unsearchable.items()):
                click.echo(f"  {status:<18}{count:>7}")
        if report.excluded_by_design:
            click.echo("Excluded by design:")
            for reason, count in sorted(report.excluded_by_design.items()):
                click.echo(f"  {reason:<18}{count:>7}")
        click.echo(f"Complete:          {report.is_complete}")
    finally:
        db.close()


@inventory.command("index")
@click.option("--audit", is_flag=True, help="Reconcile the ledger without re-indexing")
@click.option("--repair", is_flag=True, help="Re-index every stale or missing source")
def inventory_index(audit, repair):
    """Reconcile the semantic index ledger, and optionally repair the index."""
    from sbpeye.database import collection, embedding_config
    from sbpeye.inventory.ledger import reconcile

    if not audit and not repair:
        raise click.UsageError("pass --audit or --repair")

    db = SessionLocal()
    try:
        report = reconcile(db, collection, embedding_config, write=True)
        click.echo(
            f"Audit: {report.status_counts.get('indexed', 0)} indexed, "
            f"{report.status_counts.get('stale', 0)} stale, "
            f"{report.orphan_chunks} orphan chunk(s)"
        )
        if not repair:
            return

        repaired, failed = _repair(db, report)
        click.echo(f"Repaired {repaired} source(s), {failed} failure(s)")
        report = reconcile(db, collection, embedding_config, write=True)
        click.echo(
            f"After repair: {report.status_counts.get('indexed', 0)} indexed, "
            f"{report.status_counts.get('stale', 0)} stale"
        )
    finally:
        db.close()


def _repair(db, report) -> tuple[int, int]:
    """Re-index stale sources through the normal paired indexing functions."""
    from sbpeye.models import Attachment, Circular, RegDocument
    from sbpeye.scraper.circulars import _index_circular, vectorize_attachment
    from sbpeye.scraper.laws import index_law_document

    repaired = failed = 0
    with click.progressbar(report.stale_sources, label="Repairing") as sources:
        for source in sources:
            try:
                if source.source_kind == "circular":
                    circular = db.query(Circular).get(source.source_id)
                    if circular is not None:
                        _index_circular(circular)
                elif source.source_kind == "attachment":
                    attachment = db.query(Attachment).get(source.source_id)
                    if attachment is not None:
                        vectorize_attachment(db, attachment)
                else:
                    document = db.query(RegDocument).get(source.logical_document_id)
                    if document is not None:
                        index_law_document(db, document)
                repaired += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                click.echo(f"  [ERROR] {source.ledger_id}: {exc}")
    db.commit()
    return repaired, failed


@inventory.command("search")
@click.argument("query")
@click.option("--source", type=click.Choice(["all", "circulars", "laws"]), default="all")
@click.option("--band", type=int, default=300, show_default=True,
              help="Semantic supplement size")
@click.option("--max-candidates", type=int, default=1200, show_default=True)
@click.option("--no-llm", is_flag=True,
              help="Disable term generation, HyDE, adjudication, and extraction")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]),
              default="text")
@click.option("--strict", is_flag=True, help="Fail if index coverage is incomplete")
def inventory_search(query, source, band, max_candidates, no_llm, output_format, strict):
    """Find every regulatory document that discusses QUERY."""
    from sbpeye.database import collection, embedding_backend, embedding_config
    from sbpeye.inventory.schemas import InventoryError, InventorySearchRequest
    from sbpeye.inventory.service import InventorySearchService

    sources = ["circulars", "laws"] if source == "all" else [source]
    request = InventorySearchRequest(
        query=query,
        sources=sources,
        semantic_band=band,
        max_candidates=max_candidates,
        generate_terms=not no_llm,
        use_hyde=not no_llm,
        skip_adjudication=no_llm,
        extract_spans=not no_llm,
        require_complete_coverage=strict,
    )

    llm = None
    if not no_llm:
        from sbpeye.inventory.llm import load_llm

        db_for_llm = SessionLocal()
        try:
            llm = load_llm(db_for_llm)
        finally:
            db_for_llm.close()

    db = SessionLocal()
    try:
        service = InventorySearchService(
            collection, embedding_backend, embedding_config, llm=llm
        )
        response = service.search(request, db)
    except InventoryError as exc:
        raise click.ClickException(f"[{exc.code}] {exc}") from exc
    finally:
        db.close()

    if output_format == "json":
        click.echo(json.dumps(response.to_dict(), indent=2, ensure_ascii=False))
        return
    _render(response)


def _render(response) -> None:
    coverage = response.coverage
    click.echo(f"Query:      {response.query}")
    click.echo(f"Snapshot:   {response.snapshot_id[:24]}...")
    click.echo(f"Terms:      {', '.join(response.retrieval_policy.term_set)}")
    click.echo(
        f"Coverage:   {coverage.candidates_lexical} lexical + "
        f"{coverage.candidates_semantic} semantic -> "
        f"{coverage.candidates_union} candidates -> "
        f"{coverage.adjudicated_included} included"
    )
    click.echo(
        f"            {coverage.source_units_indexed}/{coverage.source_units_expected} "
        f"sources indexed, complete={coverage.is_complete}"
    )
    for warning in coverage.warnings:
        click.echo(f"  [warn] {warning}")
    click.echo()
    click.echo(f"{response.matched_documents} matching document(s):")
    click.echo()

    for result in response.results:
        reference = result.reference or result.title
        click.echo(f"- {reference}")
        if result.reference:
            click.echo(f"  {result.title}")
        for evidence in result.evidence:
            locator = _locator_label(evidence)
            text = (evidence.extracted_text or evidence.passage).strip()
            mark = "" if evidence.extraction_verified else "  [unverified span]"
            click.echo(f"  {locator}: {text[:400]}{mark}")
        click.echo()


def _locator_label(evidence) -> str:
    if evidence.locator_kind == "page" and evidence.page_start is not None:
        label = f"p.{evidence.page_start}"
    elif evidence.locator_kind == "offset" and evidence.source_start is not None:
        label = f"@{evidence.source_start}"
    else:
        label = evidence.source_ref or "chunk"
    return f"{evidence.source_label or evidence.source_kind} {label}".strip()
