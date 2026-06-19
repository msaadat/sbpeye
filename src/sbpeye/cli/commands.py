import click
import sys
import time
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from sbpeye.database import engine, Base, SessionLocal
from sbpeye.models import Circular, CircularRelationship
from sbpeye.ai import get_ai_client


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
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
def sync(dept, year, limit, skip_llm, verbose):
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
@click.option("--url", help="Process a single circular by URL")
@click.option("--dept", "-d", multiple=True, help="Only process circulars in these departments")
@click.option("--year", "-y", multiple=True, help="Only process circulars from these years")
@click.option("--limit", "-l", type=int, default=0, help="Max circulars to process (0=unlimited)")
@click.option("--refresh", is_flag=True, help="Overwrite existing checklists")
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def checklist(url, dept, year, limit, refresh, verbose, delay):
    """Generate AI compliance checklists for circulars."""
    db = SessionLocal()
    try:
        client = get_ai_client(db)
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
@click.option("--verbose", "-v", is_flag=True, help="Print extra details")
@click.option("--delay", type=float, default=1.0, help="Delay between API calls in seconds")
def run_all(dept, year, limit, skip_llm, verbose, delay):
    """Run full pipeline: sync -> summarize -> tags -> checklist -> relationships -> status."""
    from sbpeye.scraper.circulars import scrape_circulars

    print("=" * 60)
    print("SBPEye Full Pipeline")
    print("=" * 60)

    print("\n[1/6] Syncing circulars...")
    db = SessionLocal()
    try:
        scrape_circulars(
            db,
            departments=list(dept) if dept else None,
            years=list(year) if year else None,
            limit=limit,
            skip_llm=True,
            verbose=verbose,
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
    ], start=2):
        print(f"\n[{step}/6] {task_name}...")
        db = SessionLocal()
        try:
            task_fn(db, client, None, dept, year, limit, False, verbose, delay)
        finally:
            db.close()

    print(f"\n[6/6] Computing statuses...")
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
    """Re-index all circulars into ChromaDB."""
    from sbpeye.database import chroma_client, embedding_backend, embedding_config
    from sbpeye.search import prepare_chunks

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
                chunks = prepare_chunks(c.title or "", c.content_text or "")
                total_chunks += len(chunks)
            print(f"Would create {total_chunks} chunks (avg {total_chunks/total:.1f} per circular)")
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
                chunks = prepare_chunks(c.title or "", c.content_text or "")

                for ci, chunk in enumerate(chunks):
                    batch_docs.append(chunk)
                    batch_ids.append(f"{c.id}__chunk_{ci}")
                    batch_metas.append({
                        "circular_id": c.id,
                        "title": c.title or "",
                        "url": c.url or "",
                        "department": c.department or "",
                        "chunk_index": ci,
                    })

                if len(batch_docs) >= BATCH_SIZE:
                    flush_batch()

                indexed += 1

                if (i + 1) % 100 == 0:
                    print(f"  [{i+1}/{total}] indexed {indexed} circulars, {total_chunks + len(batch_docs)} chunks so far...")

            except Exception as e:
                errors += 1
                print(f"  [ERROR] {c.id}: {e}")

        flush_batch()

        print(f"\nRe-indexing complete:")
        print(f"  Circulars indexed: {indexed}")
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


def _resolve_reference(db, ref_text: str) -> Circular | None:
    ref_lower = ref_text.lower().strip()
    if not ref_lower:
        return None
    from sqlalchemy import or_
    matches = db.query(Circular).filter(
        or_(
            Circular.reference.ilike(f"%{ref_lower}%"),
            Circular.title.ilike(f"%{ref_lower}%"),
        )
    ).limit(5).all()
    if len(matches) == 1:
        return matches[0]
    for m in matches:
        if m.reference and m.reference.lower() == ref_lower:
            return m
    return None


def _run_summarize(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _url_filter(db.query(Circular), url)
    query = _dept_filter(query, dept)
    query = _year_filter(query, year)
    if not refresh:
        query = query.filter((Circular.summary == None) | (Circular.summary == ""))  # noqa: E711
    circulars = query.order_by(Circular.date.desc()).all()

    if url and not circulars:
        print(f"No circular found with URL: {url}")
        return

    if limit > 0:
        circulars = circulars[:limit]

    print(f"Summarizing {len(circulars)} circulars...")
    processed = 0
    for c in circulars:
        if not c.content_text:
            if verbose:
                print(f"  [SKIP] No content: {c.title[:60]}")
            continue
        try:
            summary = client.summarize(c.title, c.content_text)
            c.summary = summary
            c.summary_generated_at = datetime.utcnow()
            db.commit()
            processed += 1
            if verbose:
                print(f"  [{processed}] {c.title[:60]}: {summary[:80]}...")
            else:
                print(f"  [{processed}/{len(circulars)}] {c.title[:60]}")
        except Exception as e:
            print(f"  [ERROR] {c.title[:60]}: {e}")
            db.rollback()
        time.sleep(delay)

    print(f"\nSummarized {processed}/{len(circulars)} circulars.")


def _run_tags(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _url_filter(db.query(Circular), url)
    query = _dept_filter(query, dept)
    query = _year_filter(query, year)
    if not refresh:
        query = query.filter((Circular.tags == None) | (Circular.tags == ""))  # noqa: E711
    circulars = query.order_by(Circular.date.desc()).all()

    if url and not circulars:
        print(f"No circular found with URL: {url}")
        return

    if limit > 0:
        circulars = circulars[:limit]

    print(f"Tagging {len(circulars)} circulars...")
    processed = 0
    for c in circulars:
        if not c.content_text:
            continue
        try:
            tag_list = client.generate_tags(c.title, c.content_text)
            c.tags = json.dumps(tag_list)
            c.tags_generated_at = datetime.utcnow()
            db.commit()
            processed += 1
            if verbose:
                print(f"  [{processed}] {c.title[:60]}: {tag_list}")
            else:
                print(f"  [{processed}/{len(circulars)}] {c.title[:60]}")
        except Exception as e:
            print(f"  [ERROR] {c.title[:60]}: {e}")
            db.rollback()
        time.sleep(delay)

    print(f"\nTagged {processed}/{len(circulars)} circulars.")


def _run_checklist(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _url_filter(db.query(Circular), url)
    query = _dept_filter(query, dept)
    query = _year_filter(query, year)
    if not refresh:
        query = query.filter((Circular.compliance_checklist == None) | (Circular.compliance_checklist == ""))  # noqa: E711
    circulars = query.order_by(Circular.date.desc()).all()

    if url and not circulars:
        print(f"No circular found with URL: {url}")
        return

    if limit > 0:
        circulars = circulars[:limit]

    print(f"Generating checklists for {len(circulars)} circulars...")
    processed = 0
    for c in circulars:
        if not c.content_text:
            continue
        try:
            checklist = client.generate_checklist(c.title, c.content_text)
            c.compliance_checklist = json.dumps(checklist)
            c.checklist_generated_at = datetime.utcnow()
            db.commit()
            processed += 1
            if verbose:
                print(f"  [{processed}] {c.title[:60]}: {len(checklist)} items")
            else:
                print(f"  [{processed}/{len(circulars)}] {c.title[:60]}")
        except Exception as e:
            print(f"  [ERROR] {c.title[:60]}: {e}")
            db.rollback()
        time.sleep(delay)

    print(f"\nGenerated checklists for {processed}/{len(circulars)} circulars.")


def _run_relationships(db, client, url, dept, year, limit, refresh, verbose, delay):
    query = _url_filter(db.query(Circular), url)
    query = _dept_filter(query, dept)
    query = _year_filter(query, year)

    all_matching = query.all()
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

    print(f"Extracting relationships for {len(circulars)} circulars...")
    processed = 0
    total_rels = 0
    for c in circulars:
        if not c.content_text:
            continue
        try:
            rels = client.extract_relationships(c.title, c.reference or "", c.content_text)
            all_rels = []
            for rel_type in ("amends", "supersedes", "cancels", "adds_to", "clarifies"):
                for target_ref in rels.get(rel_type, []):
                    target_circular = _resolve_reference(db, target_ref)
                    rel = CircularRelationship(
                        source_id=c.id,
                        target_id=target_circular.id if target_circular else None,
                        target_reference=target_ref,
                        type=rel_type,
                    )
                    db.add(rel)
                    all_rels.append(rel)

            c.relationships_generated_at = datetime.utcnow()
            db.commit()
            total_rels += len(all_rels)
            processed += 1
            if verbose:
                print(f"  [{processed}] {c.title[:60]}: {len(all_rels)} relationships")
            else:
                print(f"  [{processed}/{len(circulars)}] {c.title[:60]}")
        except Exception as e:
            print(f"  [ERROR] {c.title[:60]}: {e}")
            db.rollback()
        time.sleep(delay)

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
