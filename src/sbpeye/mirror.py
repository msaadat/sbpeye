"""Listing capture, pure reconciliation, and atomic audit publication."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import re

from .circular_identity import circular_identity, _reference_parts, reference_conflicts


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


from .sbp_urls import normalize_sbp_url as normalized_url


def descriptor_date(descriptor):
    from .circular_dates import parse_listing_date
    raw = descriptor.get("date") or descriptor.get("listing_date") or ""
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return parse_listing_date(raw, str(descriptor.get("year") or ""))


def descriptor_year(descriptor):
    parts = _reference_parts(descriptor.get("reference"))
    value = (parts or {}).get("year") or descriptor.get("year")
    return int(value) if re.fullmatch(r"\d{4}", str(value or "")) else None


def _compatible(descriptor, local):
    a, b = _reference_parts(descriptor.get("reference")), _reference_parts(local.get("reference"))
    if not a or not b:
        return True
    return all(a[key] == b[key] for key in ("prefix", "number", "is_letter")) and (
        not a["year"] or not b["year"] or a["year"] == b["year"])


def reconcile(snapshot, local_rows):
    """Reconcile retained listing occurrences against a consistent local snapshot."""
    groups = defaultdict(list)
    for occurrence in snapshot:
        item = dict(occurrence)
        item["url"] = normalized_url(item["url"])
        groups[circular_identity(item.get("reference"), item["url"])].append(item)
    locals_by_id = {row["id"]: row for row in local_rows}
    urls = defaultdict(set)
    for row in local_rows:
        for field in ("url", "new_url", "old_url"):
            if row.get(field):
                try:
                    urls[normalized_url(row[field])].add(row["id"])
                except ValueError:
                    pass
    counts = Counter({name: 0 for name in ("matched", "drifted", "missing", "ambiguous", "unlisted_local", "identity_collision_groups", "listing_duplicate_groups", "duplicate_entries")})
    items, claimed, uncertain = [], set(), set()
    coverage = defaultdict(Counter)
    for identity, variants in sorted(groups.items()):
        def preference(item):
            year = descriptor_year(item)
            preferred = bool(year and item["url"].rstrip("/").endswith(f"-of-{year}"))
            return (not preferred, item["url"], item.get("page", 0), item.get("position", 0))
        variants.sort(key=preference)
        descriptor = variants[0]
        candidates = set().union(*(urls.get(item["url"], set()) for item in variants))
        if identity in locals_by_id:
            candidates.add(identity)
        conflicts = any(reference_conflicts(item.get("reference")) or not _compatible(descriptor, item) for item in variants)
        metadata_conflict = len({re.sub(r"\s+", " ", item.get("title", "")).casefold() for item in variants}) > 1
        diagnostics = []
        if conflicts:
            counts["identity_collision_groups"] += 1
            diagnostics.append("identity_collision")
        if len(variants) > 1:
            counts["duplicate_entries"] += len(variants) - 1
            if not conflicts:
                counts["listing_duplicate_groups"] += 1
                diagnostics.append("listing_duplicate")
        if conflicts or metadata_conflict or len(candidates) > 1 or any(not _compatible(descriptor, locals_by_id[candidate]) for candidate in candidates):
            bucket, matched_id = "ambiguous", None
            uncertain.update(candidates)
        elif candidates:
            matched_id = next(iter(candidates))
            bucket = "matched" if matched_id == identity else "drifted"
            claimed.add(matched_id)
        else:
            bucket, matched_id = "missing", None
        year = descriptor_year(descriptor)
        counts[bucket] += 1
        coverage[str(year or "unknown")][bucket] += 1
        items.append({"item_key": f"listing:{identity}", "identity": identity, "bucket": bucket,
                      "matched_id": matched_id, "descriptor": descriptor, "variants": variants,
                      "department": descriptor.get("department"), "year": year,
                      "sort_date": descriptor_date(descriptor),
                      "evidence": {"candidate_ids": sorted(candidates), "diagnostics": diagnostics,
                                   "metadata_conflict": metadata_conflict}})
    for identity, row in sorted(locals_by_id.items()):
        if identity not in claimed and identity not in uncertain:
            counts["unlisted_local"] += 1
            items.append({"item_key": f"local:{identity}", "identity": identity, "bucket": "unlisted_local",
                          "matched_id": identity, "descriptor": row, "variants": [],
                          "department": row.get("department"), "year": descriptor_year(row),
                          "sort_date": descriptor_date(row), "evidence": {}})
    return {"items": items, "counts": dict(counts), "coverage": dict(coverage),
            "raw_total": len(snapshot), "distinct_total": len(groups), "local_total": len(local_rows)}


def crawl_listing(options, fetch, progress=lambda value: None, baseline=None):
    """Capture all advertised pages, retain diagnostics, and reject unsafe publication."""
    from .scraper.circulars import parse_circular_listing, LISTING_PAGE_SIZE, CIRCULARS_LISTING_URL_FIRST, CIRCULARS_LISTING_URL
    def parse(soup, page):
        pager = soup.select_one(".pagination-custom[data-total-pages]")
        total_box = soup.select_one("#total_all_records")
        total_text = total_box.get("value", "") if total_box else ""
        total = int(total_text) if total_text.isdigit() else None
        page_text = pager.get("data-total-pages", "") if pager else ""
        pages = int(page_text) if page_text.isdigit() else (-(-total // LISTING_PAGE_SIZE) if total is not None else None)
        if not pages or pages > 10000:
            raise ValueError("invalid_listing: missing or invalid pagination")
        if total is not None and pages != max(1, -(-total // LISTING_PAGE_SIZE)):
            raise ValueError("invalid_listing: pagination count disagreement")
        rows = parse_circular_listing(soup)
        boxes = len(soup.select("div.publication-box-new"))
        if len(rows) != boxes or not rows or (page < pages - 1 and len(rows) != LISTING_PAGE_SIZE):
            raise ValueError("invalid_listing: empty page, rejected entries, or unexpected yield")
        return pages, total, [{**row, "page": page, "position": position} for position, row in enumerate(rows)]
    for restart in range(2):
        diagnostics, entries = [], []
        try:
            pages, advertised, first = parse(fetch(CIRCULARS_LISTING_URL_FIRST), 0)
        except Exception as exc:
            return {"status": "failed", "error_code": "invalid_listing" if isinstance(exc, ValueError) else "page_zero_failed", "error": str(exc), "entries": [], "diagnostics": [{"page": 0, "error": str(exc)}], "pages_total": 0}
        entries.extend(first)
        fingerprints = {tuple(item["url"] for item in first): 0}
        diagnostics.append({"page": 0, "yield": len(first), "urls": list(next(iter(fingerprints)))})
        invalid = False
        with ThreadPoolExecutor(max_workers=options.workers) as executor:
            tasks = {executor.submit(fetch, CIRCULARS_LISTING_URL.format(offset=page * LISTING_PAGE_SIZE)): page for page in range(1, pages)}
            for future in as_completed(tasks):
                page = tasks[future]
                try:
                    count, total, rows = parse(future.result(), page)
                    fingerprint = tuple(item["url"] for item in rows)
                    if count != pages or (advertised is not None and total != advertised) or fingerprint in fingerprints:
                        raise ValueError("invalid_listing: inconsistent or repeated page")
                    fingerprints[fingerprint] = page
                    entries.extend(rows)
                    diagnostics.append({"page": page, "yield": len(rows), "urls": list(fingerprint)})
                except Exception as exc:
                    invalid |= isinstance(exc, ValueError)
                    diagnostics.append({"page": page, "error": str(exc)})
                progress({"pages_total": pages, "pages_completed": len(diagnostics)})
        status, code, error = "success", None, None
        failures = sum("error" in item for item in diagnostics)
        if failures:
            status, code = ("failed", "invalid_listing") if invalid else ("partial", "failed_pages")
        if status == "success" and advertised is not None and len(entries) != advertised:
            status, code = "failed", "invalid_listing"
        if baseline and len(entries) < baseline["raw_total"] * 0.9 and not failures:
            status, code = "failed", "listing_collapse"
        try:
            end_pages, end_total, end_first = parse(fetch(CIRCULARS_LISTING_URL_FIRST), 0)
            changed = end_pages != pages or end_total != advertised or end_first != first
        except Exception as exc:
            changed, error = True, str(exc)
        if changed and restart == 0:
            continue
        if changed:
            status, code = "partial", "listing_changed"
        return {"status": status, "error_code": code, "error": error, "entries": sorted(entries, key=lambda row: (row["page"], row["position"])),
                "diagnostics": sorted(diagnostics, key=lambda row: row["page"]), "pages_total": pages}


def publish_audit(session, audit_id, report, capture):
    """Publish immutable findings and additive queue changes in one transaction.

    A complete capture is authoritative: it can reconcile every existing gap.  An
    incomplete capture is not authoritative enough to close, hold, or otherwise
    reinterpret an existing gap, but a listing row that it did observe and cannot
    find locally is still a useful, actionable backfill candidate.  Publish those
    missing rows additively so an operator can recover them and run a later full
    audit to reconcile the rest of the queue.
    """
    from .models import Circular
    from .mirror_models import MirrorAudit, MirrorAuditItem, MirrorGap
    timestamp = now()
    audit = session.get(MirrorAudit, audit_id)
    if audit is None or audit.status not in {"queued", "running"}:
        raise ValueError("Audit is missing or already published")
    audit.status = capture["status"]
    audit.completed_at = timestamp
    audit.local_observed_at = report.get("local_observed_at", timestamp)
    audit.error_code, audit.error = capture.get("error_code"), capture.get("error")
    audit.pages_total = capture["pages_total"]
    audit.pages_failed = sum("error" in row for row in capture["diagnostics"])
    audit.pages_completed = len(capture["diagnostics"]) - audit.pages_failed
    for key in ("raw_total", "distinct_total", "local_total"):
        setattr(audit, key, report[key])
    audit.counts, audit.coverage, audit.diagnostics = dumps(report["counts"]), dumps(report["coverage"]), dumps(capture["diagnostics"])
    findings = {}
    for item in report["items"]:
        data = dict(item)
        for key in ("descriptor", "variants", "evidence"):
            data[key] = dumps(data[key])
        session.add(MirrorAuditItem(audit_id=audit_id, **data))
        if item["bucket"] != "unlisted_local":
            findings[item["identity"]] = item
    gaps = {row.id: row for row in session.query(MirrorGap).all()}
    for identity, finding in findings.items():
        if finding["bucket"] != "missing":
            continue
        gap = gaps.get(identity)
        if gap is None:
            gap = MirrorGap(id=identity, descriptor="{}", first_seen_at=timestamp, status="pending")
            session.add(gap)
            gaps[identity] = gap
        if gap.active_attempt_id:
            continue
        gap.eligibility_checked_at = timestamp
        gap.eligible = True
        gap.eligibility_reason = "missing" if audit.status == "success" else "missing_incomplete_audit"
        gap.eligibility_source = "audit" if audit.status == "success" else "incomplete_audit"
        gap.last_seen_at, gap.last_audit_id = timestamp, audit_id
        gap.descriptor, gap.variants = dumps(finding["descriptor"]), dumps(finding["variants"])
        gap.department, gap.year, gap.sort_date = finding["department"], finding["year"], finding["sort_date"]
        gap.raw_date = str(finding["descriptor"].get("date", ""))
        # A locally absent circular cannot remain resolved.  Preserve an explicit
        # operator skip, however; it can be requeued from the UI when wanted.
        if gap.status == "resolved":
            gap.status, gap.resolution_id, gap.resolved_at = "pending", None, None

    if audit.status == "success":
        for identity, gap in gaps.items():
            if gap.active_attempt_id:
                continue
            finding = findings.get(identity)
            gap.eligibility_checked_at = timestamp
            gap.eligible = bool(finding and finding["bucket"] == "missing")
            gap.eligibility_reason = "missing" if gap.eligible else (finding["bucket"] if finding else "unlisted")
            gap.eligibility_source = "audit"
            if finding:
                gap.last_seen_at, gap.last_audit_id = timestamp, audit_id
                gap.descriptor, gap.variants = dumps(finding["descriptor"]), dumps(finding["variants"])
                gap.department, gap.year, gap.sort_date = finding["department"], finding["year"], finding["sort_date"]
                gap.raw_date = str(finding["descriptor"].get("date", ""))
                if finding["bucket"] in {"matched", "drifted"}:
                    gap.status, gap.resolution_id, gap.resolved_at = "resolved", finding["matched_id"], timestamp
            if gap.status == "resolved" and (not gap.resolution_id or session.get(Circular, gap.resolution_id) is None):
                gap.resolution_id, gap.resolved_at = None, None
                gap.status = "pending" if gap.eligible else "failed"
    session.commit()
