import click
import sys
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from sbpeye.database import PROJECT_ROOT, engine, Base, SessionLocal
from sbpeye.models import Attachment, Circular, CircularRelationship
from sbpeye.ai import get_ai_client
from sbpeye.link_routing import resolve_reference_in_context
from sbpeye.supersession import apply_blanket_supersession


@click.group()
def cli():
    """SBPEye - Independent SBP Circulars & Data Indexer"""
    pass


@cli.group()
def circulars():
    """Manage SBP circulars — sync, summarize, tag, etc."""
    pass


@circulars.command()
@click.option("--dept", "-d", multiple=True, help="Filter by department name substring (e.g., bprd, epd)")
@click.option("--year", "-y", multiple=True, help="Filter by year (e.g., 2025, 2024)")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--skip-llm", is_flag=True, help="Skip LLM relationship extraction")
@click.option("--force-fetch", is_flag=True, help="Re-fetch circular HTML and update existing rows")
@click.option("--force-download", is_flag=True, help="Re-download attachment files")
@click.option("--no-attachments", is_flag=True, help="Skip attachment discovery and download")
@click.option("--workers", type=click.IntRange(1), default=1, show_default=True, help="Concurrent circular downloads")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
def sync(dept, year, limit, skip_llm, force_fetch, force_download, no_attachments, workers, verbose):
    """Scrape circulars from SBP website."""
    from sbpeye.scraper.circulars import scrape_circulars

    db = SessionLocal()
    try:
        scrape_circulars(
            db,
            departments=list(dept) if dept else None,
            years=list(year) if year else None,
            limit=limit,
            skip_llm=skip_llm,
            verbose=verbose,
            force_fetch=force_fetch,
            force_download=force_download,
            include_attachments=not no_attachments,
            workers=workers,
        )
    finally:
        db.close()

    _show_stats()


@circulars.command()
@click.option("--url", required=True, help="URL of the circular page to process")
@click.option("--dept", "-d", default="Unknown", help="Department name for this circular")
@click.option("--skip-llm", is_flag=True, help="Skip LLM relationship extraction")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
def process_url(url, dept, skip_llm, verbose):
    """Process a single circular by its URL."""
    from sbpeye.scraper.circulars import process_circular

    db = SessionLocal()
    try:
        existing = db.query(Circular).filter(Circular.url == url).first()
        if existing:
            print(f"Circular already exists in DB: {existing.title}")
            return

        print(f"Processing: {url}")
        process_circular(
            db,
            title=url.rsplit("/", 1)[-1] or url,
            url=url,
            department=dept,
            skip_llm=skip_llm,
            verbose=verbose,
        )
    finally:
        db.close()

    _show_stats()


@circulars.command("reextract")
@click.option("--url", help="Re-extract a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Filter by department substring")
@click.option("--year", "-y", multiple=True, help="Filter by year")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars (0=unlimited)")
@click.option("--reindex", is_flag=True, help="Rebuild Chroma chunks after extraction")
@click.option("--verbose", "-v", is_flag=True, help="Print extraction and indexing details")
def reextract(url, dept, year, limit, reindex, verbose):
    """Re-extract circular HTML and PDFs from local cache files."""
    from sbpeye.scraper.circulars import reextract_circular_from_cache

    db = SessionLocal()
    try:
        query = _year_filter(_dept_filter(_url_filter(db.query(Circular), url), dept), year)
        circular_items = query.order_by(Circular.date.desc()).all()
        if limit > 0:
            circular_items = circular_items[:limit]
        print(f"Re-extracting {len(circular_items)} circulars from local caches...")
        totals = {"changed": 0, "errors": 0, "indexed": 0}
        for index, circular in enumerate(circular_items, start=1):
            result = reextract_circular_from_cache(
                db,
                circular,
                reindex=reindex,
                verbose=verbose,
            )
            for key in totals:
                totals[key] += result[key]
            print(
                f"  [{index}/{len(circular_items)}] "
                f"{circular.reference or circular.title[:60]}: "
                f"changed={result['changed']} errors={result['errors']} "
                f"indexed={result['indexed']}"
            )
        print(
            "Re-extraction complete: "
            f"changed={totals['changed']}, errors={totals['errors']}, "
            f"indexed={totals['indexed']}"
        )
    finally:
        db.close()


@cli.group()
def attachments():
    """Fetch and vectorize circular attachments."""
    pass


@attachments.command("fetch")
@click.option("--dept", "-d", multiple=True, help="Filter by department substring")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to scan (0=unlimited)")
@click.option("--rescan", is_flag=True, help="Include previously scanned circulars")
@click.option("--force-fetch", is_flag=True, help="Re-fetch circular HTML")
@click.option("--force-download", is_flag=True, help="Re-download attachment files")
@click.option("--delay", type=float, default=0.5, help="Delay between circulars")
@click.option("--workers", type=click.IntRange(1), default=4, show_default=True, help="Concurrent circular downloads")
@click.option("--verbose", "-v", is_flag=True, help="Print per-attachment progress")
def attachments_fetch(dept, limit, rescan, force_fetch, force_download, delay, workers, verbose):
    """Discover and download attachments for existing circulars."""
    from sbpeye.scraper.circulars import fetch_attachments_for_circular

    db = SessionLocal()
    try:
        query = db.query(Circular)
        query = _dept_filter(query, dept)
        circular_items = query.order_by(Circular.date.desc()).all()
        if not rescan and not force_fetch and not force_download:
            circular_items = [
                circular
                for circular in circular_items
                if circular.attachments_scanned_at is None
                or any(
                    attachment.extraction_status in {"pending", "error"}
                    or not attachment.local_path
                    or not (PROJECT_ROOT / attachment.local_path).exists()
                    for attachment in circular.attachments
                )
            ]
        if limit > 0:
            circular_items = circular_items[:limit]

        circular_ids = [circular.id for circular in circular_items]
        print(
            f"Scanning {len(circular_ids)} circulars with {workers} worker(s)..."
        )

        def fetch_one(circular_id):
            worker_db = SessionLocal()
            try:
                circular = worker_db.get(Circular, circular_id)
                processed = fetch_attachments_for_circular(
                    worker_db,
                    circular,
                    force_fetch=force_fetch,
                    force_download=force_download,
                    verbose=verbose,
                )
                result = len(processed), circular.reference or circular.title[:50]
                if delay > 0:
                    time.sleep(delay)
                return result
            finally:
                worker_db.close()

        found = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(fetch_one, circular_id): index
                for index, circular_id in enumerate(circular_ids, start=1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    count, label = future.result()
                    found += count
                    print(
                        f"  [{index}/{len(circular_ids)}] {label}: "
                        f"{count} attachment(s)"
                    )
                except Exception as exc:
                    errors += 1
                    print(f"  [ERROR] circular #{index}: {exc}")
        print(f"Attachment scan complete: {found} found, {errors} errors")
    finally:
        db.close()


def _run_attachment_vectorize(
    db, dept=(), limit=0, verbose=False, attachment_id=None
):
    from sbpeye.scraper.circulars import vectorize_attachment

    query = db.query(Attachment).join(Circular).filter(
        Attachment.content_text.is_not(None),
        Attachment.content_text != "",
    )
    if attachment_id:
        query = query.filter(Attachment.id == attachment_id)
    else:
        query = query.filter(Attachment.is_vectorized == 0)
    if dept:
        from sqlalchemy import or_

        query = query.filter(
            or_(*[Circular.department.ilike(f"%{item}%") for item in dept])
        )
    attachment_items = query.order_by(Attachment.created_at).all()
    if limit > 0:
        attachment_items = attachment_items[:limit]

    print(f"Vectorizing {len(attachment_items)} attachments...")
    indexed = sum(
        vectorize_attachment(db, item, verbose=verbose)
        for item in attachment_items
    )
    print(f"Vectorized {indexed}/{len(attachment_items)} attachments")
    return indexed


@attachments.command("vectorize")
@click.option("--id", "attachment_id", help="Vectorize one attachment by exact ID")
@click.option("--dept", "-d", multiple=True, help="Filter by department substring")
@click.option("--limit", "-l", type=int, default=0, help="Max attachments (0=unlimited)")
@click.option("--verbose", "-v", is_flag=True, help="Print per-attachment progress")
def attachments_vectorize(attachment_id, dept, limit, verbose):
    """Index extracted attachment text into ChromaDB."""
    db = SessionLocal()
    try:
        _run_attachment_vectorize(
            db,
            dept=dept,
            limit=limit,
            verbose=verbose,
            attachment_id=attachment_id,
        )
    finally:
        db.close()


@circulars.command()
@click.option("--url", help="Process a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Only summarize circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only summarize circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Overwrite existing summaries")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def summarize(url, dept, year, limit, refresh, verbose, delay):
    """Generate AI summaries for circulars."""
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        _run_summarize(db, client, url, dept, year, limit, refresh, verbose, delay)
    finally:
        db.close()


@circulars.command()
@click.option("--url", help="Process a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Only tag circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only tag circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Overwrite existing tags")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def tags(url, dept, year, limit, refresh, verbose, delay):
    """Generate AI tags for circulars."""
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        _run_tags(db, client, url, dept, year, limit, refresh, verbose, delay)
    finally:
        db.close()


@circulars.command()
@click.option("--id", "attachment_id", help="Run a diagnostic checklist for one PDF attachment ID")
@click.option("--url", help="Process a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Only process circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only process circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Overwrite existing checklists")
@click.option("--verbose", "-v", is_flag=True, help="Print source metadata, parsed items, prompts, and each LLM response")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def checklist(attachment_id, url, dept, year, limit, refresh, verbose, delay):
    """Generate AI compliance checklists for circulars."""
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        if attachment_id:
            _run_attachment_checklist(
                db,
                client,
                attachment_id=attachment_id,
                verbose=verbose,
                delay=delay,
            )
        else:
            _run_checklist(db, client, url, dept, year, limit, refresh, verbose, delay)
    finally:
        db.close()


@circulars.command()
@click.option("--url", help="Process a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Only process circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only process circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Overwrite existing relationships")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.5, help="Delay between API calls in seconds")
def relationships(url, dept, year, limit, refresh, verbose, delay):
    """Extract relationships between circulars using AI."""
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        _run_relationships(db, client, url, dept, year, limit, refresh, verbose, delay)
    finally:
        db.close()


@circulars.command("resolve-targets")
@click.option("--verbose", "-v", is_flag=True, help="Print each resolved relationship")
def resolve_targets(verbose):
    """Fill blank target_ids in existing relationships using the current resolver."""
    db = SessionLocal()
    try:
        pending = (
            db.query(CircularRelationship)
            .filter(
                CircularRelationship.target_id.is_(None),
                CircularRelationship.target_reference.isnot(None),
            )
            .all()
        )
        click.echo(f"Found {len(pending)} relationship(s) with no target_id.")
        resolved = 0
        for rel in pending:
            source = db.get(Circular, rel.source_id)
            if not source:
                continue
            target = resolve_reference_in_context(db, source, rel.target_reference)
            if target:
                rel.target_id = target.id
                resolved += 1
                if verbose:
                    click.echo(f"  resolved: [{rel.id}] {rel.target_reference!r} -> {target.id}")
        db.commit()
        click.echo(f"Resolved {resolved} / {len(pending)} relationship(s).")
    finally:
        db.close()


@circulars.command()
@click.argument("circular_id", required=False)
@click.option("--depth", type=int, default=0, help="Max hops from seed (0=unlimited)")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Re-extract even if relationships already exist")
@click.option("--verbose", "-v", is_flag=True, help="Print each extracted relationship")
@click.option("--delay", type=float, default=1.5, help="Delay between API calls in seconds")
def graph(circular_id, depth, limit, refresh, verbose, delay):
    """Generate relationships for circular graph expansion.

    With CIRCULAR_ID, runs BFS from that seed, extracting relationships at each hop
    and expanding to newly discovered neighbors until the full reachable graph is
    covered (or --depth hops are exhausted).

    Without CIRCULAR_ID, processes all circulars that have not already had
    relationships generated. Use --limit to process them in batches.
    """
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        if circular_id:
            seed = db.get(Circular, circular_id)
            if not seed:
                raise click.ClickException(f"Circular not found: {circular_id}")
            seeds = [seed]
            print(f"Graph expansion from: {seed.display_name}")
        else:
            query = db.query(Circular).filter(
                Circular.content_text.is_not(None),
                Circular.content_text != "",
            )
            if not refresh:
                query = query.filter(Circular.relationships_generated_at.is_(None))
            seeds = query.order_by(Circular.date.desc()).all()
            print(f"Graph expansion for {len(seeds)} circular(s)")
            if not refresh:
                print("Mode: ungraphed only")
        if depth:
            print(f"Max depth: {depth}")
        if limit:
            print(f"Process limit: {limit}")

        queue: list[tuple[str, int]] = [(seed.id, 0) for seed in seeds]
        visited: set[str] = set()
        processed = 0
        skipped = 0
        total_rels = 0

        while queue:
            if limit > 0 and processed >= limit:
                break

            current_id, current_depth = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            if depth and current_depth > depth:
                continue

            circular = db.get(Circular, current_id)
            if not circular:
                continue

            depth_tag = f"[d{current_depth}] " if current_depth else ""
            label = circular.reference or circular.title[:60]

            already_done = circular.relationships_generated_at is not None

            if already_done and not refresh:
                skipped += 1
                if verbose:
                    print(f"  [skip] {depth_tag}{label}")
            elif not circular.content_text:
                if verbose:
                    print(f"  [no-content] {depth_tag}{label}")
            else:
                try:
                    if refresh and already_done:
                        db.query(CircularRelationship).filter(
                            CircularRelationship.source_id == current_id
                        ).delete(synchronize_session=False)

                    rels = client.extract_relationships(
                        circular.title, circular.reference or "", circular.content_text
                    )
                    new_rels: list[tuple[str, str, Circular | None]] = []
                    for rel_type in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
                        for target_ref in rels.get(rel_type, []):
                            target = resolve_reference_in_context(db, circular, target_ref)
                            db.add(CircularRelationship(
                                source_id=circular.id,
                                target_id=target.id if target else None,
                                target_reference=target_ref,
                                type=rel_type,
                            ))
                            new_rels.append((rel_type, target_ref, target))

                    for target in apply_blanket_supersession(db, client, circular, rels):
                        new_rels.append(("supersedes", "(all previous on subject)", target))

                    circular.relationships_generated_at = datetime.utcnow()
                    db.commit()
                    total_rels += len(new_rels)
                    processed += 1
                    print(f"  [{processed}] {depth_tag}{label}: {len(new_rels)} relationship(s)")
                    if verbose:
                        for rel_type, ref, target in new_rels:
                            resolved = f"→ {target.reference or target.id}" if target else "(unresolved)"
                            print(f"       {rel_type}: {ref!r} {resolved}")
                    time.sleep(delay)
                except Exception as exc:
                    print(f"  [error] {depth_tag}{label}: {exc}")
                    db.rollback()

            # Expand neighbors from relationships now in DB
            if not depth or current_depth < depth:
                out_ids = [
                    r.target_id
                    for r in db.query(CircularRelationship).filter(
                        CircularRelationship.source_id == current_id,
                        CircularRelationship.target_id.isnot(None),
                    )
                ]
                in_ids = [
                    r.source_id
                    for r in db.query(CircularRelationship).filter(
                        CircularRelationship.target_id == current_id,
                    )
                ]
                for nid in out_ids + in_ids:
                    if nid and nid not in visited:
                        queue.append((nid, current_depth + 1))

        from sbpeye.circular_ai import _recompute_statuses
        _recompute_statuses(db)
        db.commit()

        if circular_id:
            print(f"\nDone. Seed: {seeds[0].display_name}")
        else:
            print("\nDone. Batch graph expansion complete.")
        print(f"  Graph nodes visited:   {len(visited)}")
        print(f"  Circulars processed:   {processed}")
        print(f"  Skipped (cached):      {skipped}")
        print(f"  Total relationships:   {total_rels}")
    finally:
        db.close()


@circulars.command()
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
def status(verbose):
    """Recompute circular status from relationships (active/amended/superseded/cancelled)."""
    db = SessionLocal()
    try:
        rels = db.query(CircularRelationship).all()
        status_map = {}

        for rel in rels:
            if rel.target_id:
                target_status = "superseded" if rel.type == "supersedes" else "amended" if rel.type == "amends" else "cancelled" if rel.type == "cancels" else "amended"
                current = status_map.get(rel.target_id, "active")
                priority = {"cancelled": 3, "superseded": 2, "amended": 1, "active": 0}
                if priority.get(target_status, 0) > priority.get(current, 0):
                    status_map[rel.target_id] = target_status

        updated = 0
        all_circulars = db.query(Circular).all()
        for c in all_circulars:
            new_status = status_map.get(c.id, "active")
            if c.status != new_status:
                c.status = new_status
                updated += 1
                if verbose:
                    print(f"  {c.reference or c.id}: {c.status} -> {new_status}")

        db.commit()
        print(f"Updated {updated} circular statuses. Total: {len(status_map)} circulars affected.")
    finally:
        db.close()


@circulars.command("all")
@click.option("--dept", "-d", multiple=True, help="Filter by department")
@click.option("--year", "-y", multiple=True, help="Filter by year")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars per task (0=unlimited)")
@click.option("--skip-llm", is_flag=True, help="Skip LLM tasks (only sync circulars)")
@click.option("--no-attachment-vectorize", is_flag=True, help="Leave attachment text unindexed")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
@click.option("--workers", type=click.IntRange(1), default=4, show_default=True, help="Concurrent circular downloads")
def run_all(dept, year, limit, skip_llm, no_attachment_vectorize, verbose, delay, workers):
    """Run the complete circular and attachment processing pipeline."""
    from sbpeye.scraper.circulars import scrape_circulars

    print("=" * 60)
    print("SBPEye Full Pipeline")
    print("=" * 60)

    print("\n[1/7] Syncing circulars...")
    db = SessionLocal()
    try:
        scrape_circulars(
            db,
            departments=list(dept) if dept else None,
            years=list(year) if year else None,
            limit=limit,
            skip_llm=True,
            verbose=verbose,
            workers=workers,
        )
    finally:
        db.close()

    if not no_attachment_vectorize:
        print("\n[2/7] Vectorizing attachments...")
        db = SessionLocal()
        try:
            _run_attachment_vectorize(
                db, dept=dept, limit=limit, verbose=verbose
            )
        finally:
            db.close()

    if skip_llm:
        print("\nSkipping LLM tasks (--skip-llm).")
        return

    client = get_ai_client()

    for step, (task_name, task_fn) in enumerate([
        ("Summarize", _run_summarize),
        ("Tags", _run_tags),
        ("Checklist", _run_checklist),
        ("Relationships", _run_relationships),
    ], start=3):
        print(f"\n[{step}/7] {task_name}...")
        db = SessionLocal()
        try:
            task_fn(db, client, None, dept, year, limit, False, verbose, delay)
        finally:
            db.close()

    print(f"\n[7/7] Computing statuses...")
    ctx = click.Context(status)
    ctx.params = {"verbose": verbose}
    with ctx:
        status.invoke(ctx)

    print("\n" + "=" * 60)
    print("Pipeline complete!")
    print("=" * 60)
    _show_stats()


@circulars.command("search")
@click.option("--query", "-q", multiple=True, help="Search query (repeatable; runs built-in test queries if omitted)")
@click.option("--limit", "-l", type=int, default=3, help="Max results per query")
def search_cmd(query, limit):
    """Test the hybrid search engine (BM25 + vector)."""
    from sbpeye.search import search_engine

    queries = list(query) if query else [
        "BPRD Circular No. 05 of 2024",
        "AML regulations",
        "cash reserve requirement",
        "KYC requirements for banks",
        "TT rebate",
        "T.T. rebate",
    ]

    for q in queries:
        db = SessionLocal()
        try:
            print(f"\n--- Query: '{q}' ---")
            results, total = search_engine.search(q, db, limit=limit)
            for i, r in enumerate(results):
                c = r["circular"]
                snippet = r["snippet"].replace("\n", " ")[:100] + "..." if r["snippet"] else ""
                print(f"{i+1}. [{c.reference}] {c.title}")
                print(f"   Date: {c.date.strftime('%Y-%m-%d')} | Dept: {c.department}")
                print(f"   Snippet: {snippet}")
        finally:
            db.close()


@cli.command("stats")
def stats_cmd():
    """Show database statistics."""
    _show_stats()


@cli.command("reindex")
@click.option("--dry-run", is_flag=True, help="Show what would be indexed without writing")
def reindex_cmd(dry_run):
    """Re-index all circulars and extracted attachments into ChromaDB."""
    from sbpeye.database import chroma_client, embedding_backend, embedding_config
    from sbpeye.checklist import prepare_reference_chunks
    from sbpeye.scraper.circulars import (
        attachment_chunk_metadata,
        attachment_document,
        circular_chunk_metadata,
        circular_document,
    )

    COLLECTION_NAME = "circulars"
    BATCH_SIZE = 50

    db = SessionLocal()
    try:
        circulars = db.query(Circular).all()
        total = len(circulars)
        print(f"Found {total} circulars in database")

        if dry_run:
            total_chunks = 0
            for c in circulars:
                total_chunks += len(prepare_reference_chunks(circular_document(c)))
                for attachment in c.attachments:
                    if attachment.content_text and attachment.content_text.strip():
                        total_chunks += len(
                            prepare_reference_chunks(attachment_document(attachment))
                        )
            print(
                f"Would create {total_chunks} chunks "
                f"(avg {total_chunks/max(total, 1):.1f} per circular)"
            )
            return

        print(f"Embedding backend: {embedding_config.provider} ({embedding_config.model})")
        probe = embedding_backend.embed_queries(["SBP circular search"])
        print(f"Embedding dimensions: {len(probe[0])}")

        try:
            chroma_client.delete_collection(COLLECTION_NAME)
            print(f"Deleted old '{COLLECTION_NAME}' collection")
        except Exception:
            print(f"No existing '{COLLECTION_NAME}' collection to delete")

        col = chroma_client.create_collection(name=COLLECTION_NAME, embedding_function=None)

        indexed = 0
        total_chunks = 0
        errors = 0
        indexed_attachments = 0

        batch_docs: list[str] = []
        batch_ids: list[str] = []
        batch_metas: list[dict] = []

        def flush_batch():
            nonlocal total_chunks
            if not batch_docs:
                return
            embeddings = embedding_backend.embed_documents(list(batch_docs))
            col.add(
                documents=list(batch_docs),
                embeddings=embeddings,
                ids=list(batch_ids),
                metadatas=list(batch_metas),
            )
            total_chunks += len(batch_docs)
            batch_docs.clear()
            batch_ids.clear()
            batch_metas.clear()

        for i, c in enumerate(circulars):
            try:
                chunks = prepare_reference_chunks(circular_document(c))
                for ci, chunk in enumerate(chunks):
                    batch_docs.append(chunk["text"])
                    batch_ids.append(f"{c.id}__chunk_{ci}")
                    batch_metas.append(circular_chunk_metadata(c, chunk, ci))

                for attachment in c.attachments:
                    if not attachment.content_text or not attachment.content_text.strip():
                        attachment.is_vectorized = 0
                        continue
                    attachment_chunks = prepare_reference_chunks(attachment_document(attachment))
                    for ci, chunk in enumerate(attachment_chunks):
                        batch_docs.append(chunk["text"])
                        batch_ids.append(f"{attachment.id}__chunk_{ci}")
                        batch_metas.append(attachment_chunk_metadata(attachment, chunk, ci))
                    attachment.is_vectorized = 1
                    indexed_attachments += 1

                if len(batch_docs) >= BATCH_SIZE:
                    flush_batch()

                indexed += 1

                if (i + 1) % 100 == 0:
                    print(f"  [{i+1}/{total}] indexed {indexed} circulars, {total_chunks + len(batch_docs)} chunks so far...")

            except Exception as e:
                errors += 1
                print(f"  [ERROR] {c.id}: {e}")

        flush_batch()
        db.commit()

        print(f"\nRe-indexing complete:")
        print(f"  Circulars indexed: {indexed}")
        print(f"  Attachments indexed: {indexed_attachments}")
        print(f"  Total chunks:      {total_chunks}")
        print(f"  Avg chunks/doc:    {total_chunks/max(indexed,1):.1f}")
        print(f"  Errors:            {errors}")
        print(f"  Collection count:  {col.count()}")

    finally:
        db.close()


@cli.command("dry-run")
@click.option("--dept", "-d", multiple=True, help="Filter by department")
@click.option("--year", "-y", multiple=True, help="Filter by year")
@click.option("--verbose", "-v", is_flag=True, help="Print each discovered circular's details")
def dry_run_cmd(dept, year, verbose):
    """Discover and list circulars without downloading."""
    from sbpeye.scraper.circulars import discover_departments, discover_year_pages, discover_circulars_on_year_page

    all_depts = discover_departments(verbose=True)
    if dept:
        dept_lower = [d.lower() for d in dept]
        all_depts = [d for d in all_depts if any(f in d["name"].lower() for f in dept_lower)]
        print(f"\nFiltered to {len(all_depts)} department(s)")

    total = 0
    for d in all_depts:
        print(f"\n{'='*60}\n  {d['name']}\n  {d['url']}\n{'='*60}")
        year_pages = discover_year_pages(d["url"], verbose=True)
        if year:
            year_pages = [yp for yp in year_pages if yp["year"] in year]
        for yp in year_pages:
            circs = discover_circulars_on_year_page(yp["url"], d["name"], yp["year"], verbose=True)
            if verbose:
                for c in circs:
                    ref = c.get("reference", "")
                    date = c.get("date", "")
                    prefix = f"[{ref}] {date} - " if ref or date else ""
                    print(f"      {prefix}{c['title'][:80]}")
                    print(f"        {c['url']}")
            total += len(circs)

    print(f"\nTotal circulars discovered: {total}")


def _url_filter(query, url):
    if not url:
        return query
    return query.filter(Circular.url == url)


def _dept_filter(query, dept):
    from sqlalchemy import or_
    if not dept:
        return query
    clauses = [Circular.department.ilike(f"%{d}%") for d in dept]
    return query.filter(or_(*clauses))


def _year_filter(query, year):
    if not year:
        return query
    from sqlalchemy import extract
    return query.filter(extract("year", Circular.date).in_([int(y) for y in year]))


def _scope_query(db, url, dept, year):
    """Circular query filtered by the shared --url/--dept/--year batch options."""
    return _year_filter(_dept_filter(_url_filter(db.query(Circular), url), dept), year)


class _BatchOutcome:
    """What a per-circular batch step accomplished, for progress reporting."""

    __slots__ = ("detail", "count")

    def __init__(self, detail: str = "", count: int = 0):
        self.detail = detail  # verbose one-line summary of the result
        self.count = count    # extra items produced (e.g. relationships created)


def _run_ai_batch(db, circulars, *, process, skip=None,
                  verbose=False, delay=0.0, sleep_in_loop=True):
    """Drive an AI batch task over `circulars`: commit/rollback, progress, delay.

    `skip(c)` returns True to bypass a circular (and is responsible for any
    skip-specific logging). `process(c)` performs the work, mutates the row, and
    returns a `_BatchOutcome`; the driver commits on success and rolls back on error.
    Returns `(processed, total_count)`.
    """
    processed = 0
    total = 0
    for c in circulars:
        if skip is not None and skip(c):
            continue
        try:
            outcome = process(c)
            db.commit()
            processed += 1
            total += outcome.count
            if verbose:
                print(f"  [{processed}] {c.title[:60]}: {outcome.detail}")
            else:
                print(f"  [{processed}/{len(circulars)}] {c.title[:60]}")
        except Exception as e:
            print(f"  [ERROR] {c.title[:60]}: {e}")
            db.rollback()
        if sleep_in_loop:
            time.sleep(delay)
    return processed, total


def _select_circulars(query, url, limit):
    """Order by date desc, report an empty single-URL run, and apply --limit."""
    circulars = query.order_by(Circular.date.desc()).all()
    if url and not circulars:
        print(f"No circular found with URL: {url}")
        return None
    if limit > 0:
        circulars = circulars[:limit]
    return circulars


def _run_summarize(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _scope_query(db, url, dept, year)
    if not refresh:
        query = query.filter((Circular.summary == None) | (Circular.summary == ""))  # noqa: E711
    circulars = _select_circulars(query, url, limit)
    if circulars is None:
        return

    def skip(c):
        if not c.content_text:
            if verbose:
                print(f"  [SKIP] No content: {c.title[:60]}")
            return True
        return False

    def process(c):
        summary = client.summarize(c.title, c.content_text)
        c.summary = summary
        c.summary_generated_at = datetime.utcnow()
        return _BatchOutcome(detail=f"{summary[:80]}...")

    print(f"Summarizing {len(circulars)} circulars...")
    processed, _ = _run_ai_batch(
        db, circulars, process=process, skip=skip,
        verbose=verbose, delay=delay,
    )
    print(f"\nSummarized {processed}/{len(circulars)} circulars.")


def _run_tags(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _scope_query(db, url, dept, year)
    if not refresh:
        query = query.filter((Circular.tags == None) | (Circular.tags == ""))  # noqa: E711
    circulars = _select_circulars(query, url, limit)
    if circulars is None:
        return

    def skip(c):
        has_pdf_text = any(
            (attachment.file_type or "").lower() == "pdf"
            and attachment.extraction_status == "extracted"
            and bool(attachment.content_text)
            for attachment in c.attachments
        )
        return not c.content_text and not has_pdf_text

    def process(c):
        tag_list = client.generate_tags(c.title, c.content_text)
        c.tags = json.dumps(tag_list)
        c.tags_generated_at = datetime.utcnow()
        return _BatchOutcome(detail=f"{tag_list}")

    print(f"Tagging {len(circulars)} circulars...")
    processed, _ = _run_ai_batch(
        db, circulars, process=process, skip=skip,
        verbose=verbose, delay=delay,
    )
    print(f"\nTagged {processed}/{len(circulars)} circulars.")


def _diagnostic_trace_printer():
    llm_section_started = False

    def trace(event, payload):
        nonlocal llm_section_started
        if event == "document":
            document = payload["document"]
            click.echo("\n=== 1. RAW CONTENT ===")
            click.echo(f"Document ID: {document['doc_id']}")
            click.echo(f"Document: {document['doc_label']}")
            click.echo(f"Type: {document['file_type']}")
            click.echo("\n" + document["text"])
            return
        if event == "parsing":
            units = payload["units"]
            click.echo(f"\n=== 2. DOCLING ITEMS ({len(units)} units) ===")
            for index, unit in enumerate(units, start=1):
                page = unit.page_start if unit.page_start is not None else "HTML"
                headings = " > ".join(unit.heading_path) or "None"
                click.echo(
                    f"\n--- Segment {index}/{len(units)} ---\n"
                    f"Unit ID: {unit.unit_id}\n"
                    f"Reference: {unit.ref}\n"
                    f"Page: {page}\n"
                    f"Offsets: {unit.start_offset}-{unit.end_offset}\n"
                    f"Heading path: {headings}\n"
                    f"Oversized: {'yes' if unit.oversized else 'no'}\n"
                    f"Source text:\n{unit.source_text}"
                )
            return
        if event == "analysis_blocks":
            blocks = payload["blocks"]
            click.echo(f"\n=== 3. ANALYSIS BLOCKS ({len(blocks)} blocks) ===")
            for index, block in enumerate(blocks, start=1):
                click.echo(
                    f"\n--- Block {index}/{len(blocks)} ---\n"
                    f"Block ID: {block.block_id}\n"
                    f"Reference: {block.ref}\n"
                    f"Type: {block.block_type}\n"
                    f"Pages: {block.page_start or 'HTML'}-{block.page_end or block.page_start or 'HTML'}\n"
                    f"Source units: {', '.join(block.source_unit_ids)}"
                )
            return
        if event == "llm_input":
            if not llm_section_started:
                click.echo("\n=== 4. LLM EXTRACTION ===")
                llm_section_started = True
            block = payload["block"]
            click.echo(
                f"\n--- LLM INPUT: {block.ref} ({block.block_id}) ---\n"
                f"SYSTEM PROMPT:\n{payload['system_prompt']}\n\n"
                f"USER PROMPT:\n{payload['user_prompt']}"
            )
            return
        if event == "llm_output":
            block = payload["block"]
            click.echo(
                f"\n--- RAW LLM OUTPUT: {block.ref} ({block.block_id}) ---\n"
                f"{payload['raw_response']}"
            )
            return
        if event == "normalized_block":
            click.echo(
                f"\n--- NORMALIZED BLOCK "
                f"{payload['completed']}/{payload['total']} ---\n"
                + json.dumps(payload["items"], ensure_ascii=False, indent=2)
            )

    return trace


def _run_attachment_checklist(db, client, attachment_id, verbose, delay):
    from sbpeye.documents import document_from_attachment

    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        raise click.ClickException(f"Attachment not found: {attachment_id}")
    if (attachment.file_type or "").lower() != "pdf":
        raise click.ClickException(
            f"Attachment {attachment_id} is not a PDF ({attachment.file_type or 'unknown'})."
        )
    document = document_from_attachment(attachment)
    has_cached_file = bool(
        document.get("local_path") and Path(document["local_path"]).is_file()
    )
    if not has_cached_file and not document["text"].strip():
        raise click.ClickException(
            f"Attachment {attachment_id} has neither a cached PDF nor extracted text."
        )

    click.echo(
        f"Diagnostic checklist run for {attachment.filename} ({attachment.id}).\n"
        "This attachment-only result will not overwrite the circular checklist."
    )
    result = client.generate_checklist(
        attachment.circular,
        delay=delay,
        trace_callback=_diagnostic_trace_printer() if verbose else None,
        documents=[document],
        gaps=[],
    )
    click.echo("\n=== 5. FINAL CHECKLIST ===")
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _run_checklist(db, client, url, dept, year, limit, refresh, verbose, delay):
    circulars = _scope_query(db, url, dept, year).order_by(Circular.date.desc()).all()
    if not refresh:
        def has_current_checklist(circular):
            try:
                value = json.loads(circular.compliance_checklist or "null")
            except json.JSONDecodeError:
                return False
            return (
                isinstance(value, dict)
                and value.get("schema_version") == 2
                and isinstance(value.get("checklist_items"), list)
            )

        circulars = [item for item in circulars if not has_current_checklist(item)]

    if url and not circulars:
        print(f"No circular found with URL: {url}")
        return

    if limit > 0:
        circulars = circulars[:limit]

    def skip(c):
        has_pdf_file = any(
            (attachment.file_type or "").lower() == "pdf"
            and bool(attachment.local_path)
            and (PROJECT_ROOT / attachment.local_path).is_file()
            for attachment in c.attachments
        )
        return not c.content_text and not has_pdf_file

    def process(c):
        checklist = client.generate_checklist(c, delay=delay)
        c.compliance_checklist = json.dumps(checklist)
        c.checklist_generated_at = datetime.utcnow()
        items = checklist["checklist_items"]
        required_count = sum(item.get("classification") == "required" for item in items)
        return _BatchOutcome(
            detail=f"{len(items)} checklist items, {required_count} required, "
            f"status={checklist['status']}"
        )

    print(f"Generating checklists for {len(circulars)} circulars...")
    # The per-call delay is handled inside generate_checklist, so the driver does not sleep.
    processed, _ = _run_ai_batch(
        db, circulars, process=process,
        skip=skip, verbose=verbose, delay=delay, sleep_in_loop=False,
    )
    print(f"\nGenerated checklists for {processed}/{len(circulars)} circulars.")


def _run_relationships(db, client, url, dept, year, limit, refresh, verbose, delay):
    all_matching = _scope_query(db, url, dept, year).all()
    if url and not all_matching:
        print(f"No circular found with URL: {url}")
        return

    if refresh:
        target_ids = [c.id for c in all_matching]
        if target_ids:
            db.query(CircularRelationship).filter(
                CircularRelationship.source_id.in_(target_ids)
            ).delete(synchronize_session=False)
            db.commit()
        circulars = all_matching
    else:
        existing_source_ids = db.query(CircularRelationship.source_id).distinct().all()
        existing_ids = {sid[0] for sid in existing_source_ids}
        circulars = [c for c in all_matching if c.id not in existing_ids]

    if limit > 0:
        circulars = circulars[:limit]

    def process(c):
        rels = client.extract_relationships(c.title, c.reference or "", c.content_text)
        all_rels = []
        for rel_type in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
            for target_ref in rels.get(rel_type, []):
                target_circular = resolve_reference_in_context(db, c, target_ref)
                rel = CircularRelationship(
                    source_id=c.id,
                    target_id=target_circular.id if target_circular else None,
                    target_reference=target_ref,
                    type=rel_type,
                )
                db.add(rel)
                all_rels.append(rel)

        all_rels.extend(apply_blanket_supersession(db, client, c, rels))

        c.relationships_generated_at = datetime.utcnow()
        return _BatchOutcome(detail=f"{len(all_rels)} relationships", count=len(all_rels))

    print(f"Extracting relationships for {len(circulars)} circulars...")
    processed, total_rels = _run_ai_batch(
        db, circulars, process=process,
        skip=lambda c: not c.content_text, verbose=verbose, delay=delay,
    )
    print(f"\nExtracted {total_rels} relationships from {processed}/{len(circulars)} circulars.")


def _show_stats():
    db = SessionLocal()
    try:
        circ_count = db.query(Circular).count()
        rel_count = db.query(CircularRelationship).count()

        summarized = db.query(Circular).filter(Circular.summary != None, Circular.summary != "").count()  # noqa: E711
        tagged = db.query(Circular).filter(Circular.tags != None, Circular.tags != "").count()
        checklist_count = db.query(Circular).filter(Circular.compliance_checklist != None, Circular.compliance_checklist != "").count()

        print(f"\n--- Database Stats ---")
        print(f"  Circulars:         {circ_count}")
        print(f"  Summarized:        {summarized}/{circ_count}")
        print(f"  Tagged:            {tagged}/{circ_count}")
        print(f"  Checklists:        {checklist_count}/{circ_count}")
        print(f"  Relationships:     {rel_count}")

        if circ_count > 0:
            latest = db.query(Circular).order_by(Circular.date.desc()).first()
            print(f"  Latest circular:   {latest.title[:60]} ({latest.date})")

            from sqlalchemy import func
            dept_counts = db.query(Circular.department, func.count(Circular.id)).group_by(Circular.department).all()
            if dept_counts:
                print(f"\n  By department:")
                for dept, count in sorted(dept_counts, key=lambda x: -x[1]):
                    print(f"    {dept:<50s} {count:>4}")
        print()
    finally:
        db.close()


def main():
    cli()


if __name__ == "__main__":
    main()
