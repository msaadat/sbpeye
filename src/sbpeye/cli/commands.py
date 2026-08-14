import click
import os
import sys
import time
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from sbpeye.database import PROJECT_ROOT, engine, Base, SessionLocal
from sbpeye.models import Attachment, CachedDocument, Circular, CircularEntity, CircularRelationship
from sbpeye.ai import get_ai_client, is_rate_limit_error
from sbpeye.link_routing import resolve_reference_in_context
from sbpeye.supersession import apply_annexure_supersession, apply_blanket_supersession


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
@click.option("--full-listing", is_flag=True, help="Crawl every listing page instead of stopping at the latest local circular date")
@click.option("--workers", type=click.IntRange(1), default=1, show_default=True, help="Concurrent circular downloads")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
def sync(dept, year, limit, skip_llm, force_fetch, force_download, no_attachments, full_listing, workers, verbose):
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
            full_listing=full_listing,
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


def _run_attachment_dedupe(db, verbose=False):
    """Merge attachment rows that share a (circular_id, filename) identity.

    SBP's site sometimes links the same annexure under two different asset paths
    that serve identical content, so the scraper's own url-based dedup can miss
    them. Within each duplicate group, keep the row with usable extracted content
    (falling back to the earliest row), and drop the rest along with their
    downloaded files and any indexed vector chunks.
    """
    from sbpeye.scraper.circulars import _delete_document_chunks

    complete_statuses = {"extracted", "scanned"}
    groups: dict[tuple[str, str], list[Attachment]] = {}
    for attachment in db.query(Attachment).order_by(Attachment.created_at).all():
        key = (attachment.circular_id, attachment.filename.casefold())
        groups.setdefault(key, []).append(attachment)

    removed = 0
    affected_groups = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        affected_groups += 1
        keeper = next(
            (
                row
                for row in rows
                if row.extraction_status in complete_statuses and row.content_text
            ),
            rows[0],
        )
        for row in rows:
            if row is keeper:
                continue
            if row.local_path:
                local_file = PROJECT_ROOT / row.local_path
                local_file.unlink(missing_ok=True)
            _delete_document_chunks(attachment_id_value=row.id)
            if verbose:
                print(f"  [DEDUPE] Removing {row.filename} ({row.id}) for circular {row.circular_id}")
            db.delete(row)
            removed += 1
        db.commit()

    return {"groups": affected_groups, "removed": removed}


@attachments.command("dedupe")
@click.option("--verbose", "-v", is_flag=True, help="Print each removed attachment")
def attachments_dedupe(verbose):
    """Merge duplicate attachments that share a circular and filename."""
    db = SessionLocal()
    try:
        summary = _run_attachment_dedupe(db, verbose=verbose)
        print(
            f"Dedupe complete: {summary['groups']} duplicate group(s), "
            f"{summary['removed']} row(s) removed"
        )
    finally:
        db.close()


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


@circulars.command()
@click.argument("circular_id", required=False)
@click.option("--dept", "-d", multiple=True, help="Only process circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only process circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Re-extract even if values already exist")
@click.option("--verbose", "-v", is_flag=True, help="Print the value count per circular")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def entities(circular_id, dept, year, limit, refresh, verbose, delay):
    """Extract structured regulatory values (ratios, thresholds, limits, dates).

    With CIRCULAR_ID, extracts values for that single circular. Without it, processes
    all circulars that have no extracted values yet — use --refresh to redo existing
    ones and --limit to process them in batches.
    """
    db = SessionLocal()
    try:
        client = get_ai_client(db)
        _run_entities(db, client, circular_id, dept, year, limit, refresh, verbose, delay)
    finally:
        db.close()


@circulars.command()
@click.argument("circular_id", required=False)
@click.option("--limit", "-l", type=int, default=0, help="Max chains to process (0=unlimited)")
@click.option("--force", is_flag=True, help="Regenerate chains that already have a fresh consolidation")
@click.option("--verbose", "-v", is_flag=True, help="Print chain members and per-chain progress")
@click.option("--delay", type=float, default=1.0, help="Delay between chains in seconds")
def consolidate(circular_id, limit, force, verbose, delay):
    """Consolidate amendment chains into merged requirement views.

    With CIRCULAR_ID, consolidates that circular's chain. Without it, processes
    every resolved amendment chain that has no consolidation yet or whose
    consolidation is stale; use --force to also regenerate fresh ones.
    """
    from sbpeye.consolidation import (
        CHAIN_RELATIONSHIP_TYPES,
        generate_consolidation,
        resolve_chain,
    )
    from sbpeye.models import CircularConsolidation

    db = SessionLocal()
    try:
        client = get_ai_client(db)
        if circular_id:
            seed = db.get(Circular, circular_id)
            if not seed:
                raise click.ClickException(f"Circular not found: {circular_id}")
            seeds = [seed]
        else:
            member_ids = set()
            for rel in db.query(CircularRelationship).filter(
                CircularRelationship.type.in_(CHAIN_RELATIONSHIP_TYPES),
                CircularRelationship.target_id.isnot(None),
            ):
                member_ids.add(rel.source_id)
                member_ids.add(rel.target_id)
            seeds = db.query(Circular).filter(Circular.id.in_(member_ids)).all()

        processed_chains: set[str] = set()
        generated = skipped = failed = 0
        for seed in seeds:
            members = resolve_chain(db, seed.id)
            if len(members) < 2:
                continue
            base_id = members[0].id
            if base_id in processed_chains:
                continue
            processed_chains.add(base_id)
            if limit and generated + failed >= limit:
                break

            existing = db.get(CircularConsolidation, base_id)
            current_ids = json.dumps([item.id for item in members])
            fresh = (
                existing is not None
                and not existing.stale
                and existing.member_ids == current_ids
            )
            if fresh and not force and not circular_id:
                skipped += 1
                continue

            label = f"{members[0].display_name} … {members[-1].display_name} ({len(members)} circulars)"
            if verbose:
                click.echo(f"Consolidating chain: {label}")
                for member in members:
                    click.echo(f"  - {member.display_name} ({member.date:%Y-%m-%d})" if member.date else f"  - {member.display_name}")
            try:
                generate_consolidation(db, client, seed)
                db.commit()
                generated += 1
                click.echo(f"  consolidated: {label}")
            except Exception as exc:
                db.rollback()
                failed += 1
                click.echo(f"  FAILED {label}: {exc}", err=True)
                if is_rate_limit_error(exc):
                    raise click.ClickException("Rate limited by the AI provider; stopping.") from exc
            time.sleep(delay)
        click.echo(
            f"Done. {generated} chain(s) consolidated, {skipped} already fresh, {failed} failed."
        )
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

                    for rel in apply_annexure_supersession(db, circular, rels):
                        new_rels.append((rel.type, rel.target_reference, rel.target))

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
                    db.rollback()
                    if is_rate_limit_error(exc):
                        raise click.ClickException(
                            f"Rate limit (HTTP 429) hit on {label}: {exc}. "
                            f"Stopping graph expansion after {processed} processed."
                        )
                    print(f"  [error] {depth_tag}{label}: {exc}")

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
@click.option("--full-listing", is_flag=True, help="Crawl every listing page instead of stopping at the latest local circular date")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
@click.option("--workers", type=click.IntRange(1), default=4, show_default=True, help="Concurrent circular downloads")
def run_all(dept, year, limit, skip_llm, no_attachment_vectorize, full_listing, verbose, delay, workers):
    """Run the complete circular and attachment processing pipeline."""
    from sbpeye.scraper.circulars import scrape_circulars

    print("=" * 60)
    print("SBPEye Full Pipeline")
    print("=" * 60)

    print("\n[1/8] Syncing circulars...")
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
            full_listing=full_listing,
        )
    finally:
        db.close()

    if not no_attachment_vectorize:
        print("\n[2/8] Vectorizing attachments...")
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
        ("Regulatory values", _run_entities),
    ], start=3):
        print(f"\n[{step}/8] {task_name}...")
        db = SessionLocal()
        try:
            task_fn(db, client, None, dept, year, limit, False, verbose, delay)
        finally:
            db.close()

    print(f"\n[8/8] Computing statuses...")
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
    """Test the hybrid search engine (FTS5 + vector)."""
    from sbpeye.search import backfill_fts, search_engine

    # The lexical arm reads the persistent FTS index; build it once if this is a
    # fresh process/DB that hasn't been backfilled yet (no-op when populated).
    db = SessionLocal()
    try:
        backfill_fts(db)
    finally:
        db.close()

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


def _require_exclusive_vector_store() -> None:
    """Refuse to rebuild while another process has the vector store open.

    Chroma's PersistentClient assumes one process. Two writers leave the HNSW segment
    and the SQLite metadata disagreeing, and the store then segfaults the interpreter on
    the next read — recoverable only by rebuilding from scratch.

    The whole store directory has to be checked, not just ``chroma.sqlite3``: a running
    server keeps the HNSW segment files (``data_level0.bin``, ``link_lists.bin``) open
    while holding no SQLite handle at all, and those segment files are exactly what a
    concurrent rebuild destroys.
    """
    from sbpeye.database import CHROMA_DB_DIR

    if not CHROMA_DB_DIR.exists():
        return
    try:
        import psutil
    except ImportError:
        return  # Cannot check; the caller is on their own.

    store_root = str(CHROMA_DB_DIR.resolve())
    self_pid = os.getpid()
    holders: dict[int, str] = {}
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.info["pid"] == self_pid:
            continue
        try:
            for handle in process.open_files():
                if handle.path.startswith(store_root):
                    holders[process.info["pid"]] = " ".join(
                        process.info["cmdline"] or [process.info["name"] or "?"]
                    )[:80]
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue

    if holders:
        listed = "; ".join(f"pid {pid}: {cmd}" for pid, cmd in sorted(holders.items()))
        raise click.ClickException(
            "another process has the vector store open — concurrent access corrupts "
            f"the Chroma index and segfaults later reads.\n  {listed}\n"
            "Stop it and retry."
        )


@cli.command("reindex")
@click.option("--dry-run", is_flag=True, help="Show what would be indexed without writing")
def reindex_cmd(dry_run):
    """Re-index all circulars, attachments, and laws/regulations into ChromaDB."""
    from sbpeye.database import embedding_backend, embedding_config, reset_collection
    from sbpeye.checklist import prepare_index_chunks
    from sbpeye.models import RegDocument
    from sbpeye.scraper.circulars import (
        attachment_chunk_metadata,
        attachment_document,
        circular_chunk_metadata,
        circular_document,
    )
    from sbpeye.scraper.laws import index_pending_laws
    from sbpeye.search import NON_TEXT_LAW_FILE_TYPES, backfill_fts, backfill_laws_fts

    BATCH_SIZE = 50

    def _law_is_searchable(document) -> bool:
        version = document.current_version
        return (
            version is not None
            and version.file_type not in NON_TEXT_LAW_FILE_TYPES
            and bool((version.content_text or "").strip())
        )

    db = SessionLocal()
    try:
        circulars = db.query(Circular).all()
        total = len(circulars)
        law_documents = db.query(RegDocument).all()
        searchable_laws = sum(1 for d in law_documents if _law_is_searchable(d))
        print(
            f"Found {total} circulars and {len(law_documents)} law documents "
            f"({searchable_laws} searchable) in database"
        )

        if dry_run:
            total_chunks = 0
            for c in circulars:
                total_chunks += len(prepare_index_chunks(circular_document(c)))
                for attachment in c.attachments:
                    if attachment.content_text and attachment.content_text.strip():
                        total_chunks += len(
                            prepare_index_chunks(attachment_document(attachment))
                        )
            print(
                f"Would create {total_chunks} circular/attachment chunks "
                f"(avg {total_chunks/max(total, 1):.1f} per circular)"
            )
            print(f"Would re-index {searchable_laws} law/regulation documents")
            return

        print(f"Embedding backend: {embedding_config.provider} ({embedding_config.model})")
        probe = embedding_backend.embed_queries(["SBP circular search"])
        print(f"Embedding dimensions: {len(probe[0])}")

        # Chroma's PersistentClient is single-process. A dev server holding the same
        # store while this runs is what corrupts the HNSW segment, so fail loudly
        # rather than produce an index that segfaults on the next read.
        _require_exclusive_vector_store()
        col = reset_collection()
        print("Reset the shared collection")

        indexed = 0
        total_chunks = 0
        errors = 0
        indexed_attachments = 0

        batch_docs: list[str] = []
        batch_stored: list[str] = []
        batch_ids: list[str] = []
        batch_metas: list[dict] = []

        def flush_batch():
            nonlocal total_chunks
            if not batch_docs:
                return
            embeddings = embedding_backend.embed_documents(list(batch_docs))
            col.add(
                documents=list(batch_stored),
                embeddings=embeddings,
                ids=list(batch_ids),
                metadatas=list(batch_metas),
            )
            total_chunks += len(batch_docs)
            batch_docs.clear()
            batch_stored.clear()
            batch_ids.clear()
            batch_metas.clear()

        for i, c in enumerate(circulars):
            try:
                chunks = prepare_index_chunks(circular_document(c))
                for ci, chunk in enumerate(chunks):
                    batch_docs.append(chunk["embed_text"])
                    batch_stored.append(chunk["text"])
                    batch_ids.append(f"{c.id}__chunk_{ci}")
                    batch_metas.append(circular_chunk_metadata(c, chunk, ci))

                for attachment in c.attachments:
                    if not attachment.content_text or not attachment.content_text.strip():
                        attachment.is_vectorized = 0
                        continue
                    attachment_chunks = prepare_index_chunks(attachment_document(attachment))
                    for ci, chunk in enumerate(attachment_chunks):
                        batch_docs.append(chunk["embed_text"])
                        batch_stored.append(chunk["text"])
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

        # Laws share this collection. Rebuilding circulars alone left the law arm empty
        # while `is_vectorized` still read 1, so `index_pending_laws` skipped it on every
        # later sync and laws stayed lexically findable but semantically invisible.
        # `force=True` re-embeds regardless of that flag; the function applies the same
        # currency and manifest rules as a normal sync.
        indexed_laws = index_pending_laws(db, force=True)
        print(f"Laws re-indexed: {indexed_laws}")

        # Rebuild the persistent FTS5 lexical indexes alongside the vector store.
        backfill_fts(db, force=True)
        backfill_laws_fts(db, force=True)
        print("Rebuilt FTS5 lexical indexes (circulars + laws)")

        # The circular/attachment arm writes chunks in batches rather than through
        # `_replace_document_chunks`, so it has no per-source ledger hook. Reconciling
        # here is both cheaper and stricter: it checks what actually landed in the store.
        from sbpeye.database import collection as live_collection
        from sbpeye.inventory.ledger import reconcile

        ledger_report = reconcile(db, live_collection, embedding_config, write=True)
        print(
            f"Ledger: {ledger_report.status_counts.get('indexed', 0)} indexed, "
            f"{ledger_report.status_counts.get('stale', 0)} stale, "
            f"{ledger_report.orphan_chunks} orphan chunk(s)"
        )

        print(f"\nRe-indexing complete:")
        print(f"  Circulars indexed: {indexed}")
        print(f"  Attachments indexed: {indexed_attachments}")
        print(f"  Laws indexed:      {indexed_laws} of {searchable_laws} searchable")
        print(f"  Circular chunks:   {total_chunks}")
        print(f"  Avg chunks/doc:    {total_chunks/max(indexed,1):.1f}")
        print(f"  Errors:            {errors}")
        print(f"  Collection count:  {col.count()}")

    finally:
        db.close()


@cli.command("dry-run")
@click.option("--dept", "-d", multiple=True, help="Filter by department")
@click.option("--year", "-y", multiple=True, help="Filter by year")
@click.option("--verbose", "-v", is_flag=True, help="Print each discovered circular's details")
@click.option("--max-pages", type=int, default=0, help="Limit listing pages crawled (0 = all)")
def dry_run_cmd(dept, year, verbose, max_pages):
    """Discover and list circulars without downloading."""
    from sbpeye.scraper.circulars import discover_circulars, _matches_department

    circulars = discover_circulars(max_pages=max_pages, verbose=True)
    if dept:
        circulars = [c for c in circulars if _matches_department(c, list(dept))]
    if year:
        circulars = [c for c in circulars if c.get("year") in year]
    if dept or year:
        print(f"\nFiltered to {len(circulars)} circular(s)")

    if verbose:
        for c in circulars:
            ref, date = c.get("reference", ""), c.get("date", "")
            prefix = f"[{ref}] {date} - " if ref or date else ""
            print(f"  {prefix}{c['title'][:80]}")
            print(f"    {c['url']}  ({c.get('department','')} / {c.get('category','')})")

    print(f"\nTotal circulars discovered: {len(circulars)}")


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
            db.rollback()
            if is_rate_limit_error(e):
                raise click.ClickException(
                    f"Rate limit (HTTP 429) hit on {c.title[:60]}: {e}. "
                    f"Stopping after {processed} processed."
                )
            print(f"  [ERROR] {c.title[:60]}: {e}")
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
        all_rels.extend(apply_annexure_supersession(db, c, rels))

        c.relationships_generated_at = datetime.utcnow()
        return _BatchOutcome(detail=f"{len(all_rels)} relationships", count=len(all_rels))

    print(f"Extracting relationships for {len(circulars)} circulars...")
    processed, total_rels = _run_ai_batch(
        db, circulars, process=process,
        skip=lambda c: not c.content_text, verbose=verbose, delay=delay,
    )
    print(f"\nExtracted {total_rels} relationships from {processed}/{len(circulars)} circulars.")


def _run_entities(db, client, circular_id, dept, year, limit, refresh, verbose, delay):
    if circular_id:
        seed = db.get(Circular, circular_id)
        if not seed:
            print(f"No circular found with ID: {circular_id}")
            return
        circulars = [seed]
    else:
        query = _year_filter(_dept_filter(db.query(Circular), dept), year)
        if not refresh:
            query = query.filter(Circular.entities_generated_at.is_(None))
        circulars = query.order_by(Circular.date.desc()).all()
        if limit > 0:
            circulars = circulars[:limit]

    if not circulars:
        print("No circulars to process.")
        return

    def skip(c):
        has_pdf_text = any(
            (a.file_type or "").lower() == "pdf"
            and a.extraction_status == "extracted"
            and bool(a.content_text)
            for a in c.attachments
        )
        if not c.content_text and not has_pdf_text:
            if verbose:
                print(f"  [SKIP] No content: {c.title[:60]}")
            return True
        return False

    def process(c):
        extracted = client.extract_entities(c, delay=delay)
        db.query(CircularEntity).filter(
            CircularEntity.circular_id == c.id
        ).delete(synchronize_session=False)
        for entity in extracted:
            db.add(CircularEntity(circular_id=c.id, **entity))
        c.entities_generated_at = datetime.utcnow()
        return _BatchOutcome(detail=f"{len(extracted)} values", count=len(extracted))

    print(f"Extracting regulatory values for {len(circulars)} circular(s)...")
    # extract_entities handles its own per-block delay, so the driver does not sleep.
    processed, total = _run_ai_batch(
        db, circulars, process=process, skip=skip,
        verbose=verbose, delay=delay, sleep_in_loop=False,
    )
    print(f"\nExtracted {total} values from {processed}/{len(circulars)} circular(s).")


def _show_stats():
    db = SessionLocal()
    try:
        circ_count = db.query(Circular).count()
        rel_count = db.query(CircularRelationship).count()

        summarized = db.query(Circular).filter(Circular.summary != None, Circular.summary != "").count()  # noqa: E711
        tagged = db.query(Circular).filter(Circular.tags != None, Circular.tags != "").count()
        checklist_count = db.query(Circular).filter(Circular.compliance_checklist != None, Circular.compliance_checklist != "").count()
        entity_circ_count = db.query(Circular).filter(Circular.entities_generated_at != None).count()  # noqa: E711
        entity_count = db.query(CircularEntity).count()

        print(f"\n--- Database Stats ---")
        print(f"  Circulars:         {circ_count}")
        print(f"  Summarized:        {summarized}/{circ_count}")
        print(f"  Tagged:            {tagged}/{circ_count}")
        print(f"  Checklists:        {checklist_count}/{circ_count}")
        print(f"  Relationships:     {rel_count}")
        print(f"  Regulatory values: {entity_count} ({entity_circ_count}/{circ_count} circulars)")

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


@cli.group()
def migrate():
    """Rebuild the circular corpus from the redesigned site, preserving LLM outputs."""
    pass


@migrate.command("snapshot")
@click.option("--out", "out_path", default="llm_snapshot.json", show_default=True,
              help="File to write the preserved LLM data to")
def migrate_snapshot(out_path):
    """Export summaries/tags/checklists/relationships/entities keyed by reference."""
    from sbpeye.migration import snapshot_llm_data

    db = SessionLocal()
    try:
        snapshot = snapshot_llm_data(db)
    finally:
        db.close()
    Path(out_path).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"Snapshotted {len(snapshot['circulars'])} circular(s) to {out_path}")
    if snapshot["unkeyed"]:
        print(f"  {len(snapshot['unkeyed'])} circular(s) had no parseable reference "
              f"and cannot be reattached by reference.")


@migrate.command("reattach")
@click.option("--in", "in_path", default="llm_snapshot.json", show_default=True,
              help="Snapshot file produced by 'migrate snapshot'")
def migrate_reattach(in_path):
    """Reattach a snapshot's LLM data to the freshly scraped circulars, matched by reference."""
    from sbpeye.migration import apply_llm_snapshot

    snapshot = json.loads(Path(in_path).read_text(encoding="utf-8"))
    db = SessionLocal()
    try:
        stats = apply_llm_snapshot(db, snapshot)
    finally:
        db.close()
    print(f"Reattached LLM data to {stats['matched']} circular(s); "
          f"rebuilt {stats['relationships']} relationship(s), {stats['entities']} entity(ies)")
    if stats["unmatched_snapshot"]:
        print(f"  {len(stats['unmatched_snapshot'])} snapshot reference(s) had no match "
              f"in the rebuilt corpus:")
        for key in stats["unmatched_snapshot"][:50]:
            print(f"    - {key}")


@migrate.command("rebuild")
@click.option("--snapshot-file", default="llm_snapshot.json", show_default=True)
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to scrape (0 = all)")
@click.option("--workers", type=click.IntRange(1), default=1, show_default=True)
@click.option("--yes", is_flag=True, help="Skip the destructive-wipe confirmation")
@click.option("--verbose", "-v", is_flag=True)
def migrate_rebuild(snapshot_file, limit, workers, yes, verbose):
    """Full one-shot rebuild: snapshot -> wipe -> re-scrape -> reattach.

    Run 'sbpeye reindex' afterwards to rebuild the vector store.
    """
    from sbpeye.migration import snapshot_llm_data, apply_llm_snapshot, wipe_circular_data
    from sbpeye.scraper.circulars import scrape_circulars

    if not yes:
        click.confirm(
            "This deletes ALL circulars, relationships, entities, attachments and vectors, "
            "then re-scrapes from the new site. Continue?",
            abort=True,
        )

    db = SessionLocal()
    try:
        # snapshot = snapshot_llm_data(db)
        # Path(snapshot_file).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        snapshot = json.loads(Path(snapshot_file).read_text(encoding="utf-8"))
        # print(f"[1/4] Snapshotted {len(snapshot['circulars'])} circular(s) -> {snapshot_file}")

        wipe_circular_data(db)
        print("[2/4] Wiped old circular data and vectors")

        scrape_circulars(
            db,
            limit=limit,
            skip_llm=True,
            verbose=verbose,
            workers=workers,
            full_listing=True,
        )
        print("[3/4] Re-scraped circulars from the new site")

        stats = apply_llm_snapshot(db, snapshot)
        print(f"[4/4] Reattached LLM data to {stats['matched']} circular(s) "
              f"({len(stats['unmatched_snapshot'])} unmatched)")
    finally:
        db.close()
    print("Done. Run 'sbpeye reindex' to rebuild the vector store.")


@cli.group()
def cache():
    """Reconcile the on-disk HTML/attachment cache against the database."""
    pass


def _run_cache_check_stale(db, prune=False, verbose=False):
    """Find disk cache files with no matching DB row, and DB rows whose
    local_path file is missing, for the HTML cache and attachments dir."""
    import uuid
    from sbpeye.scraper.circulars import HTML_CACHE_DIR, ATTACHMENTS_DIR

    # --- HTML cache: cache/html/<uuid5(url)>.html ---
    expected_html_names = set()
    for (url,) in db.query(Circular.url).all():
        if url:
            expected_html_names.add(f"{uuid.uuid5(uuid.NAMESPACE_URL, url)}.html")
    for (old_url,) in db.query(Circular.old_url).all():
        if old_url:
            expected_html_names.add(f"{uuid.uuid5(uuid.NAMESPACE_URL, old_url)}.html")

    html_orphans = []
    if HTML_CACHE_DIR.is_dir():
        for f in HTML_CACHE_DIR.iterdir():
            if f.is_file() and f.name not in expected_html_names:
                html_orphans.append(f)

    # --- Attachments: attachments/<circular_id>/<attachment_id>.<ext> ---
    expected_attachment_paths = {}  # resolved path -> (kind, id, local_path)
    missing_attachment_refs = []
    for att_id, local_path in db.query(Attachment.id, Attachment.local_path).all():
        if not local_path:
            continue
        resolved = (PROJECT_ROOT / local_path).resolve()
        expected_attachment_paths[resolved] = ("attachment", att_id, local_path)
        if not resolved.is_file():
            missing_attachment_refs.append(("attachment", att_id, local_path))
    for doc_id, local_path in db.query(CachedDocument.id, CachedDocument.local_path).all():
        if not local_path:
            continue
        resolved = (PROJECT_ROOT / local_path).resolve()
        expected_attachment_paths[resolved] = ("cached_document", doc_id, local_path)
        if not resolved.is_file():
            missing_attachment_refs.append(("cached_document", doc_id, local_path))

    attachment_orphans = []
    if ATTACHMENTS_DIR.is_dir():
        for f in ATTACHMENTS_DIR.rglob("*"):
            if f.is_file() and f.resolve() not in expected_attachment_paths:
                attachment_orphans.append(f)

    print("\n--- Cache Reconciliation Report ---")
    html_bytes = sum(f.stat().st_size for f in html_orphans)
    print(f"  Orphaned HTML cache files:      {len(html_orphans)} ({html_bytes / 1024:.1f} KB)")
    attachment_bytes = sum(f.stat().st_size for f in attachment_orphans)
    print(f"  Orphaned attachment files:      {len(attachment_orphans)} ({attachment_bytes / 1024 / 1024:.1f} MB)")
    print(f"  DB rows with missing local file: {len(missing_attachment_refs)}")

    if verbose:
        for f in html_orphans:
            print(f"    [ORPHAN HTML] {f.relative_to(PROJECT_ROOT)}")
        for f in attachment_orphans:
            print(f"    [ORPHAN ATTACHMENT] {f.relative_to(PROJECT_ROOT)}")
        for kind, ref_id, local_path in missing_attachment_refs:
            print(f"    [MISSING FILE] {kind} {ref_id} -> {local_path}")

    removed = 0
    if prune:
        for f in html_orphans + attachment_orphans:
            f.unlink(missing_ok=True)
            removed += 1
        print(f"  Pruned {removed} orphaned file(s).")
    print()

    return {
        "html_orphans": len(html_orphans),
        "attachment_orphans": len(attachment_orphans),
        "missing_refs": len(missing_attachment_refs),
        "pruned": removed,
    }


@cache.command("check-stale")
@click.option("--prune", is_flag=True, help="Delete orphaned cache files (never touches DB rows)")
@click.option("--verbose", "-v", is_flag=True, help="List every orphan/missing reference")
def cache_check_stale(prune, verbose):
    """Report cache files with no matching DB row, and DB rows pointing at missing files."""
    db = SessionLocal()
    try:
        _run_cache_check_stale(db, prune=prune, verbose=verbose)
    finally:
        db.close()


@cli.group()
def laws():
    """Manage SBP laws & regulations — sync the /laws-regulations listing."""
    pass


def _run_laws_sync(
    db, doc_types, limit, force, delay, verbose, skip_subpages=False, skip_indexing=False
) -> dict:
    """Run a laws sync, recording it in SyncStatus so runs are auditable.

    The row is tagged `kind="laws"` — the circular sync UI filters on that, so a laws
    run never shows up as the circular sync's status (see models.circular_sync_only).
    """
    from sbpeye.models import SyncStatus
    from sbpeye.scraper.laws import sync_laws

    job = SyncStatus(
        job_id=str(uuid.uuid4()),
        kind="laws",
        status="running",
        started_at=datetime.utcnow(),
        parameters=json.dumps({
            "kind": "laws",
            "doc_types": list(doc_types),
            "limit": limit,
            "force": force,
            "skip_subpages": skip_subpages,
        }),
    )
    db.add(job)
    db.commit()

    try:
        counts = sync_laws(
            db,
            doc_types=list(doc_types) or None,
            limit=limit,
            force=force,
            delay=delay,
            verbose=verbose,
            skip_subpages=skip_subpages,
            skip_indexing=skip_indexing,
        )
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.completed_at = datetime.utcnow()
        db.commit()
        raise

    job.status = "success"
    job.completed_at = datetime.utcnow()
    job.last_sync_date = job.completed_at
    job.processed_count = counts["new_versions"]
    job.skipped_count = counts["unchanged"] + counts["stubs"] + counts["external"]
    job.error_count = counts["errors"]
    db.commit()
    return counts


@laws.command("sync")
@click.option(
    "--type", "doc_types", multiple=True,
    type=click.Choice(["law", "regulation", "guideline", "gazette", "licensing"]),
    help="Only sync these document types (repeatable)",
)
@click.option("--limit", "-l", type=int, default=0, help="Max listing rows to process (0=unlimited)")
@click.option("--force", is_flag=True, help="Re-fetch the listing and re-download every file")
@click.option("--delay", type=float, default=0.5, show_default=True, help="Delay between downloads in seconds")
@click.option("--skip-subpages", is_flag=True, help="Don't follow container pages into their parts")
@click.option("--skip-indexing", is_flag=True, help="Don't update the search indexes (run 'laws reindex' later)")
@click.option("--verbose", "-v", is_flag=True, help="Print per-row progress")
def laws_sync(doc_types, limit, force, delay, skip_subpages, skip_indexing, verbose):
    """Scrape SBP's laws & regulations listing, versioning by content hash."""
    db = SessionLocal()
    try:
        _run_laws_sync(
            db, doc_types, limit, force, delay, verbose, skip_subpages, skip_indexing
        )
    finally:
        db.close()

    _show_laws_status()


@laws.command("backlink")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to scan (0=unlimited)")
@click.option("--rescan", is_flag=True, help="Include circulars that already have scanned links")
@click.option("--request-refetch", is_flag=True, help="Also flag every linked document for the next sync")
@click.option("--verbose", "-v", is_flag=True, help="Print each link created")
def laws_backlink(limit, rescan, request_refetch, verbose):
    """Scan circulars for references to laws & regulations and record the links."""
    from sbpeye.laws_links import backlink_circulars

    db = SessionLocal()
    try:
        totals = backlink_circulars(
            db, limit=limit, rescan=rescan,
            request_refetch=request_refetch, verbose=verbose,
        )
        print(
            f"\nScanned {totals['scanned']} circular(s); {totals['linked']} carry "
            f"references. Links: {totals['url_scan']} by URL, "
            f"{totals['name_match']} by name."
        )
    finally:
        db.close()


@laws.command("reindex")
@click.option("--force", is_flag=True, help="Re-embed every document, not just stale ones")
@click.option("--verbose", "-v", is_flag=True, help="Print per-document progress")
def laws_reindex(force, verbose):
    """Rebuild the laws search indexes (FTS5 + ChromaDB) from stored text."""
    from sbpeye.scraper.laws import index_pending_laws
    from sbpeye.search import backfill_laws_fts

    db = SessionLocal()
    try:
        if force:
            # Drop the lexical index first so documents that lost their text (delisted,
            # or whose current version became a manifest) don't linger as stale rows.
            rows = backfill_laws_fts(db, force=True)
            print(f"Rebuilt laws_fts: {rows} row(s)")
        indexed = index_pending_laws(db, force=force, verbose=verbose)
        print(f"Indexed {indexed} document(s) into ChromaDB + FTS")
    finally:
        db.close()

    _show_laws_status()


@laws.command("status")
def laws_status():
    """Show laws & regulations counts by type, versions, and pending extractions."""
    _show_laws_status()


def _show_laws_status():
    from sqlalchemy import func

    from sbpeye.models import RegDocument, RegDocumentVersion

    db = SessionLocal()
    try:
        documents = db.query(RegDocument).count()
        containers = db.query(RegDocument).filter(
            RegDocument.children.any()
        ).count()
        parts = db.query(RegDocument).filter(RegDocument.parent_id.isnot(None)).count()
        versions = db.query(RegDocumentVersion).count()
        current = db.query(RegDocumentVersion).filter(RegDocumentVersion.is_current == 1).count()
        pending = db.query(RegDocumentVersion).filter(
            RegDocumentVersion.effective_from.isnot(None),
            RegDocumentVersion.is_current == 0,
            RegDocumentVersion.effective_from > datetime.utcnow(),
        ).count()
        delisted = db.query(RegDocument).filter(RegDocument.delisted_at.isnot(None)).count()
        external = db.query(RegDocument).filter(RegDocument.is_external == 1).count()
        linked = db.query(RegDocument).filter(RegDocument.circular_id.isnot(None)).count()
        # Documents still waiting for content: no versions, and not deliberately
        # content-free (external links and rows that resolved to a circular).
        stubs = db.query(RegDocument).filter(
            ~RegDocument.versions.any(),
            RegDocument.is_external == 0,
            RegDocument.circular_id.is_(None),
        ).count()

        print(f"\n--- Laws & Regulations ---")
        print(f"  Documents:         {documents} ({containers} container(s), {parts} part(s))")
        print(f"  Versions:          {versions} ({current} current)")
        if pending:
            print(f"  Not yet in force:  {pending}")
        print(f"  Are circulars:     {linked}")
        print(f"  Awaiting content:  {stubs} stub(s), {external} external")
        if delisted:
            print(f"  Delisted:          {delisted}")

        type_counts = (
            db.query(RegDocument.doc_type, func.count(RegDocument.id))
            .group_by(RegDocument.doc_type)
            .all()
        )
        if type_counts:
            print(f"\n  By type:")
            for doc_type, count in sorted(type_counts, key=lambda row: -row[1]):
                print(f"    {doc_type or 'unknown':<20s} {count:>4}")

        status_counts = (
            db.query(RegDocumentVersion.extraction_status, func.count(RegDocumentVersion.id))
            .group_by(RegDocumentVersion.extraction_status)
            .all()
        )
        if status_counts:
            print(f"\n  Extraction:")
            for status, count in sorted(status_counts, key=lambda row: -row[1]):
                print(f"    {status or 'unknown':<20s} {count:>4}")
        print()
    finally:
        db.close()


# --------------------------------------------------------------- laws AI enrichment


def _law_feature_done_at(document, feature: str):
    """When `feature` was last generated for this document, or None.

    Reads through the same rule the API does: a container's summary and tags are its
    rollup on the document row, everything else belongs to the edition in force. A new
    capture therefore reads as ungenerated, which is exactly what should trigger a rerun.
    """
    from sbpeye.laws_ai import is_container

    if feature in ("summary", "tags") and is_container(document):
        return getattr(document, f"{feature}_generated_at", None)
    version = document.current_version
    if version is None:
        return None
    return getattr(version, f"{feature}_generated_at", None)


def _run_laws_feature(
    feature: str, doc_type, document_id, limit, force, delay, verbose
) -> None:
    """Batch one analysis feature over the laws corpus.

    Deliberately sequential and resumable: each document is committed on its own, so an
    interrupted run keeps everything it finished, and a rerun without `--force` picks up
    where it stopped. Docling parses are cached on content hash, so the expensive half of
    a repeat run costs nothing.
    """
    import time as _time

    from sbpeye.laws_ai import (
        CONTAINER_FEATURES,
        _compute_container_outputs,
        _compute_outputs,
        _persist_outputs,
        gap_message,
        is_container,
        law_corpus,
        rollup_sources,
    )
    from sbpeye.models import RegDocument

    db = SessionLocal()
    try:
        query = db.query(RegDocument)
        if document_id:
            query = query.filter(RegDocument.id == document_id)
        if doc_type:
            query = query.filter(RegDocument.doc_type == doc_type)
        documents = query.order_by(RegDocument.title).all()
        # Containers last, so a rollup sees the part summaries this same run produced.
        # In title order the Draft White Label ATM Guidelines sort before their only
        # chapter, and the collection was skipped as "no part summaries yet" on a run
        # that went on to summarise the very part it needed.
        documents.sort(key=lambda document: (bool(document.children), document.title or ""))

        client = get_ai_client(db)
        processed = skipped = failed = 0
        reasons: dict[str, int] = {}

        def note(reason: str) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1

        for document in documents:
            if limit and processed >= limit:
                break
            label = document.title[:60]

            if not force and _law_feature_done_at(document, feature) is not None:
                skipped += 1
                note("already generated")
                continue

            container = is_container(document)
            if container and feature not in CONTAINER_FEATURES:
                skipped += 1
                note("collection has no text of its own")
                continue
            if container and feature == "summary" and not rollup_sources(document):
                skipped += 1
                note("no part summaries to roll up yet")
                continue
            if not container:
                corpus, gaps = law_corpus(document)
                if not corpus:
                    skipped += 1
                    note(gap_message(gaps))
                    continue

            try:
                if container:
                    outputs = _compute_container_outputs(client, db, document, feature)
                else:
                    outputs = _compute_outputs(
                        client, db, document, document.current_version,
                        corpus, gaps, feature,
                    )
                _persist_outputs(db, document, document.current_version, outputs)
                db.commit()
                processed += 1
                if verbose:
                    print(f"  [OK] {label}")
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"  [FAIL] {label}: {exc}")

            if delay > 0:
                _time.sleep(delay)

        print(f"\n{feature}: {processed} generated, {skipped} skipped, {failed} failed")
        for reason, count in sorted(reasons.items(), key=lambda row: -row[1]):
            print(f"  {count:>4} skipped — {reason}")
    finally:
        db.close()


def _laws_feature_command(name: str, feature: str, help_text: str):
    """Register one `sbpeye laws <name>` batch command running `feature`.

    The five features take identical options and differ only in which one they run, so
    the command surface is generated rather than copied five times. Name and feature are
    separate arguments because they are not always the same word — the command is
    `summarize` to match `sbpeye circulars summarize`, while the feature is `summary`.
    """

    # `help=` rather than a docstring: Click reads the docstring at decoration time, so
    # assigning `__doc__` afterwards leaves every generated command unhelped.
    @laws.command(name, help=help_text)
    @click.option("--id", "document_id", help="Process a single document by id")
    @click.option("--doc-type", help="Only process this doc_type (law/regulation/guideline)")
    @click.option("--limit", "-l", type=int, default=0, help="Max documents to process (0=unlimited)")
    @click.option("--force", is_flag=True, help="Regenerate documents that already have output")
    @click.option("--delay", type=float, default=1.0, help="Delay between documents in seconds")
    @click.option("--verbose", "-v", is_flag=True, help="Print each document processed")
    def command(document_id, doc_type, limit, force, delay, verbose):
        _run_laws_feature(feature, doc_type, document_id, limit, force, delay, verbose)

    return command


laws_summarize = _laws_feature_command(
    "summarize", "summary",
    "Generate AI summaries for laws & regulations (collections roll up their parts).",
)
laws_tags = _laws_feature_command(
    "tags", "tags", "Assign taxonomy tags to laws & regulations."
)
laws_checklist = _laws_feature_command(
    "checklist", "checklist",
    "Extract cited compliance checklists from laws & regulations.",
)
laws_entities = _laws_feature_command(
    "entities", "entities",
    "Extract structured regulatory values from laws & regulations.",
)
laws_relationships = _laws_feature_command(
    "relationships", "relationships",
    "Type law-to-law edges and the circulars that act on each law.",
)


from sbpeye.cli.inventory_cmd import inventory  # noqa: E402  (command registration)

cli.add_command(inventory)


def main():
    cli()


if __name__ == "__main__":
    main()
