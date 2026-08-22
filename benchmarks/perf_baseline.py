"""Server-side read cost for the endpoints the SPA waits on.

Calls the same serializers and search engine the routes call, in-process: no HTTP,
no auth, no LLM. The numbers are therefore the cost of a request *minus* framework
overhead, which is the part `docs/PERFORMANCE_PLAN.md` is about.

    .venv/bin/python benchmarks/perf_baseline.py

Every figure is the **minimum** of N runs after a warm-up, so it reports the cost
with the page cache warm — the optimistic case. A cold container is slower.

`--section` limits the run: assets | laws | search | chroma | status | detail.
"""
from __future__ import annotations

import argparse
import gzip
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SPA_DIR = ROOT / "src" / "sbpeye" / "static" / "spa"
SPA_ASSETS = SPA_DIR / "assets"

# The route whose first paint is being costed. `/` redirects here.
LANDING_VIEW = "CircularsView"

QUERIES = [
    "capital adequacy",
    "foreign exchange remittance",
    "AML CFT",
    "minimum capital requirement banks",
    "know your customer",
    "islamic banking mudarabah",
    "cyber security incident reporting",
]


def bench(fn, repeat: int = 3) -> float:
    """Minimum wall time over `repeat` runs, after one warm-up."""
    fn()
    timings = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        timings.append(time.perf_counter() - started)
    return min(timings)


def report(label: str, seconds: float, width: int = 52) -> None:
    print(f"{label:<{width}} {seconds * 1000:8.1f} ms")


# --------------------------------------------------------------------------
# P1 — what the browser downloads before first paint
# --------------------------------------------------------------------------

def _landing_payload() -> list[Path]:
    """The files a browser fetches before the landing route paints.

    Read out of the build rather than hardcoded, because the set changes whenever a
    component moves between chunks. Vite emits the entry into `index.html`, and for each
    lazy route a `__vite__mapDeps([…])` index list into the shared name table `d=[…]` at
    the top of the entry chunk — so the route's dependency closure is recoverable
    exactly, without downloading anything or guessing from filename prefixes.
    """
    import re

    entry_names = re.findall(
        r'(?:src|href)="/spa/assets/([^"]+)"', (SPA_DIR / "index.html").read_text()
    )
    entry_js = next(name for name in entry_names if name.endswith(".js"))
    source = (SPA_ASSETS / entry_js).read_text()

    table = re.search(r"d=\(m\.f\|\|\(m\.f=\[(.*?)\]\)\)", source, re.S)
    names = re.findall(r'"([^"]+)"', table.group(1)) if table else []

    # The list whose lead chunk is the landing view: that first entry is the route's own
    # chunk, the rest are what it pulls in.
    for match in re.findall(r"__vite__mapDeps\(\[([0-9,]*)\]\)", source):
        indices = [int(i) for i in match.split(",") if i]
        if indices and names[indices[0]].startswith(f"assets/{LANDING_VIEW}"):
            route = [SPA_ASSETS / Path(names[i]).name for i in indices]
            break
    else:
        route = []

    return [SPA_ASSETS / name for name in entry_names] + route


def section_assets() -> None:
    print(f"\n### initial payload for GET /circulars  ({LANDING_VIEW})")
    if not SPA_ASSETS.is_dir():
        print("  (no build in src/sbpeye/static/spa/assets — run `npm run build`)")
        return

    raw = compressed = 0
    files = _landing_payload()
    for path in files:
        size = path.stat().st_size
        gz = len(gzip.compress(path.read_bytes(), 6))
        raw += size
        compressed += gz
        print(f"  {path.name:<64} {size:>7}  gz {gz:>7}")
    print(f"  {'TOTAL (' + str(len(files)) + ' requests)':<64} {raw:>7}  gz {compressed:>7}")
    print(f"  raw {raw // 1024} KB   gzipped {compressed // 1024} KB "
          f"({100 - compressed * 100 // raw}% smaller)")


def section_wire(base_url: str) -> None:
    """What a running server actually puts on the wire — P1 and P7 verification.

    The section above costs the *files*; this costs the **responses**, which is the thing
    a tester waits for. Needs a server (`.claude/launch.json` → `sbpeye-perf`), and only
    touches `/spa/assets`, which is public — no auth, no corpus.
    """
    import urllib.request

    print(f"\n### over the wire from {base_url}")

    def fetch(path: str, encoding: str | None):
        request = urllib.request.Request(f"{base_url}{path}")
        request.add_header("Accept-Encoding", encoding or "identity")
        with urllib.request.urlopen(request, timeout=10) as response:
            return len(response.read()), dict(response.headers)

    raw_total = wire_total = 0
    cache_control = set()
    files = _landing_payload()
    print(f"  {'file':<64} {'identity':>9} {'gzip':>9}")
    for path in files:
        url = f"/spa/assets/{path.name}"
        raw, _ = fetch(url, None)
        wire, headers = fetch(url, "gzip")
        raw_total += raw
        wire_total += wire
        cache_control.add(headers.get("cache-control"))
        print(f"  {path.name:<64} {raw:>9} {wire:>9}")

    print("  " + "-" * 84)
    print(f"  {'TOTAL (' + str(len(files)) + ' requests)':<64} "
          f"{raw_total:>9} {wire_total:>9}")
    print(f"  raw {raw_total // 1024} KB  ->  on the wire {wire_total // 1024} KB "
          f"({100 - wire_total * 100 // raw_total}% smaller)")
    print(f"  Cache-Control served: {cache_control.pop() if len(cache_control) == 1 else cache_control}")

    # P7's real payoff is the second load: a revalidation that costs a round trip each.
    _, headers = fetch(f"/spa/assets/{files[0].name}", "gzip")
    conditional = urllib.request.Request(f"{base_url}/spa/assets/{files[0].name}")
    conditional.add_header("If-None-Match", headers["etag"])
    try:
        urllib.request.urlopen(conditional, timeout=10)
        print("  conditional GET: 200 (unexpected — etag did not match)")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            print(f"  conditional GET: 304, Cache-Control: "
                  f"{exc.headers.get('cache-control')!r} "
                  f"(with max-age the browser skips this request entirely)")


# --------------------------------------------------------------------------
# P5 — /api/laws blob load
# --------------------------------------------------------------------------

def section_laws() -> None:
    """P5. Reported in queries and bytes as well as milliseconds.

    Wall time understates this one: the benchmark machine has the page cache hot and an
    NVMe under it, so 4.88 MB of pointless reads costs ~18 ms here and a good deal more on
    a container that has to go to disk for them.
    """
    from sqlalchemy import event, inspect as sa_inspect
    from sbpeye.database import SessionLocal, engine
    from sbpeye.models import RegDocument
    from sbpeye.api.serializers import _law_summary, law_summary_load_options

    print("\n### GET /api/laws  (LawsView.loadCorpus — the whole corpus, 100 per page)")

    def page(number, preloaded):
        db = SessionLocal()
        try:
            query = db.query(RegDocument).filter(RegDocument.delisted_at.is_(None))
            if preloaded:
                query = query.options(*law_summary_load_options())
            query = query.order_by(RegDocument.doc_type, RegDocument.title)
            documents = query.offset((number - 1) * 100).limit(100).all()
            payload = [_law_summary(d) for d in documents]
            # Only what SQLAlchemy actually materialised — reading it any other way would
            # trigger the very loads being measured.
            blob = sum(
                len(version.content_text or "")
                for document in documents
                if "versions" not in sa_inspect(document).unloaded
                for version in document.versions
                if "content_text" not in sa_inspect(version).unloaded
            )
            return payload, blob
        finally:
            db.close()

    def count_statements(fn):
        state = {"n": 0}

        def bump(*args):
            state["n"] += 1

        event.listen(engine, "before_cursor_execute", bump)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", bump)
        return state["n"]

    db = SessionLocal()
    pages = max(1, -(-db.query(RegDocument).filter(
        RegDocument.delisted_at.is_(None)).count() // 100))
    db.close()

    print(f"  {'':<26} {'time':>9} {'SQL stmts':>10} {'law text read':>16}")
    for label, preloaded in (("as written", False), ("with load options", True)):
        elapsed = statements = blob = 0
        for number in range(1, pages + 1):
            elapsed += bench(lambda n=number, p=preloaded: page(n, p))
            statements += count_statements(lambda n=number, p=preloaded: page(n, p))
            blob += page(number, preloaded)[1]
        print(f"  {label:<26} {elapsed*1000:>8.1f}ms {statements:>10} "
              f"{blob/1048576:>13.2f} MB")

    identical = all(page(n, False)[0] == page(n, True)[0] for n in range(1, pages + 1))
    print(f"  payload identical across all {pages} pages: {identical}")


# --------------------------------------------------------------------------
# P2 + P3 — the search engine
# --------------------------------------------------------------------------

def section_search() -> None:
    from sbpeye.database import SessionLocal
    from sbpeye.search import search_engine

    print("\n### GET /api/circulars/search")
    total = 0.0
    for query in QUERIES:
        def run(q=query):
            db = SessionLocal()
            try:
                return search_engine.search(q, db, offset=0, limit=20)
            finally:
                db.close()
        elapsed = bench(run)
        total += elapsed
        report(f"q={query!r}", elapsed)
    report("MEAN", total / len(QUERIES))


def section_chroma() -> None:
    """P3 in isolation: what the metadata pre-filter costs, and the over-fetch's margin.

    The two arms landed differently and this section shows why: circulars are the bulk of
    the collection so they can be filtered in Python off an unfiltered over-fetch, while
    laws are too sparse for that to yield a full arm and keep their pre-filter.
    """
    from sbpeye.database import collection, embedding_backend
    from sbpeye.search import search_engine

    need = search_engine.CANDIDATE_COUNT
    overfetch = need * search_engine.VECTOR_OVERFETCH

    print("\n### Chroma query strategies (n_results=50 unless noted)")
    print(f"  collection holds {collection.count():,} chunks; "
          f"arms need {need}, over-fetch asks {overfetch}")

    # Deliberately hostile to the over-fetch: law-flavoured phrasing pulls the circular
    # arm's neighbourhood toward the corpus it has to filter out.
    queries = ["anti money laundering customer due diligence",
               "microfinance institutions ordinance",
               "state bank of pakistan act"]
    embeddings = {q: embedding_backend.embed_queries([q]) for q in queries}

    def worst(**kwargs):
        return max(
            bench(lambda e=e, k=kwargs: collection.query(
                query_embeddings=e,
                include=["metadatas", "documents", "distances"], **k))
            for e in embeddings.values()
        )

    print("  -- circular arm --")
    report('$in ["circular","attachment"]  (before P3)',
           worst(n_results=need, where={"doc_type": {"$in": ["circular", "attachment"]}}))
    report('$ne "law"', worst(n_results=need, where={"doc_type": {"$ne": "law"}}))
    report("no filter, n=50  (floor: no filtering at all)", worst(n_results=need))
    report(f"no filter, n={overfetch}  (landed)", worst(n_results=overfetch))

    print("  -- law arm --")
    report('where={"kind": "law"}  (landed — over-fetch starves here)',
           worst(n_results=need, where={"kind": "law"}))

    # The margin is the whole argument for the over-fetch, so print it rather than assert
    # it in prose: this is the number that decides VECTOR_OVERFETCH.
    print("  -- over-fetch yield, worst of the queries above --")
    for label, predicate in (
        ("circular/attachment", lambda m: m.get("doc_type") in ("circular", "attachment")),
        ("law", lambda m: m.get("kind") == "law"),
    ):
        yields = [
            sum(1 for m in collection.query(query_embeddings=e, n_results=overfetch,
                                            include=["metadatas"])["metadatas"][0]
                if predicate(m))
            for e in embeddings.values()
        ]
        low = min(yields)
        print(f"  {label:<24} worst yield {low:>4} of {overfetch}  "
              f"-> {low / need:.1f}x margin"
              f"{'' if low >= need else '   STARVED, needs the pre-filter'}")


# --------------------------------------------------------------------------
# P6 — /api/app/status, /api/circulars/{id}
# --------------------------------------------------------------------------

def section_status() -> None:
    from datetime import datetime, timezone
    from sqlalchemy import func
    from sbpeye.database import SessionLocal
    from sbpeye.models import Circular

    print("\n### aggregate endpoints")

    def status():
        db = SessionLocal()
        try:
            db.query(func.count(Circular.id)).scalar()
            db.query(func.count(func.distinct(Circular.department))).filter(
                Circular.department.isnot(None)).scalar()
            db.query(func.count(Circular.id)).filter(
                func.date(Circular.indexed_at)
                == datetime.now(timezone.utc).date()).scalar()
        finally:
            db.close()

    def departments():
        db = SessionLocal()
        try:
            return (db.query(Circular.department, func.count(Circular.id))
                    .group_by(Circular.department)
                    .order_by(Circular.department.asc()).all())
        finally:
            db.close()

    report("GET /api/app/status  (3 aggregate scans)", bench(status))
    report("GET /api/circulars/departments", bench(departments))


def section_detail() -> None:
    from sqlalchemy import func
    from sbpeye.database import SessionLocal
    from sbpeye.models import Circular, CircularRelationship

    db = SessionLocal()
    worst = (db.query(CircularRelationship.source_id, func.count())
             .group_by(CircularRelationship.source_id)
             .order_by(func.count().desc()).first())
    db.close()
    if not worst:
        return
    worst_id, edges = worst

    print(f"\n### GET /api/circulars/{{id}}  (worst case: {edges} outgoing edges)")

    def detail():
        db = SessionLocal()
        try:
            circular = db.query(Circular).filter(Circular.id == worst_id).first()
            outgoing = db.query(CircularRelationship).filter(
                CircularRelationship.source_id == worst_id).all()
            incoming = db.query(CircularRelationship).filter(
                CircularRelationship.target_id == worst_id).all()
            # The N+1 inside rel_dict: two point queries per edge.
            for rel in list(outgoing) + list(incoming):
                if rel.source_id:
                    db.query(Circular).filter(Circular.id == rel.source_id).first()
                if rel.target_id:
                    db.query(Circular).filter(Circular.id == rel.target_id).first()
            # `has_text` reads the whole extracted blob to test emptiness.
            return [bool(a.content_text) for a in circular.attachments]
        finally:
            db.close()

    report("as written", bench(detail))


SECTIONS = {
    "assets": section_assets,
    "laws": section_laws,
    "search": section_search,
    "chroma": section_chroma,
    "status": section_status,
    "detail": section_detail,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", choices=sorted(SECTIONS) + ["wire"],
                        action="append",
                        help="run only these sections (repeatable)")
    parser.add_argument("--server", default="http://localhost:8124",
                        help="base URL for the `wire` section")
    args = parser.parse_args()

    print("=" * 70)
    print("SBPEye server-side read cost — warm cache, minimum of 3 runs")
    print("=" * 70)
    for name in (args.section or SECTIONS):
        if name == "wire":
            section_wire(args.server)
        else:
            SECTIONS[name]()
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
