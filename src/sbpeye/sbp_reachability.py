"""Measure whether *this host* can reach SBP, and how reliably.

`DEPLOYMENT_PLAN.md` §2.1 records the deployment as blocked at SBP's edge. What it
cannot record is a rate: the block is applied by Cloudflare per request, so from a
datacenter range some requests are refused and others are not, and a single `curl` from
a shell answers the wrong question — it tells you what happened once. Anything you plan
to do about an intermittent block (retry, cache-first, run the sync elsewhere) depends on
knowing the success *rate*, so that is what this measures: N attempts across the URLs the
scrapers actually use, with the Cloudflare ray IDs to hand to anyone who can look at the
other side of it.

Three things make the answer trustworthy rather than anecdotal:

* **A control host.** Every run first fetches an egress-IP service. If that fails too the
  finding is "no outbound HTTP", not "SBP blocks us", and the two have different fixes.
* **200 is not success.** A Cloudflare interstitial is served as `200 text/html` with a
  challenge body. Counting it as reachable is how a probe reports green while every
  scrape returns nothing usable, so bodies are checked for challenge markers.
* **Four client arms**, each named for the library that issues the request, so the output
  never needs a legend. `cloudscraper` is what the app does today — a fresh
  `create_scraper()` per call. `cloudscraper-reuse` keeps one across attempts, `requests`
  is the bare control, and `curl_cffi` presents Chrome's real TLS and HTTP/2
  fingerprints. If the arms differ, the block is partly a client problem and the fix is
  in our code; if they all fail alike, it is IP reputation and no client change will help.

  The `curl_cffi` arm is the one worth watching. `cloudscraper` sends a Chrome
  User-Agent over a Python TLS stack: measured against a fingerprinting service, its JA4
  is `t13d1713h1_...` — an OpenSSL handshake over HTTP/1.1 — while real Chrome 120 is
  `t13d1516h2_...` over HTTP/2. `curl_cffi` reproduces the latter exactly. Cloudflare
  scores UA/fingerprint disagreement, so the two arms are asking different questions of
  the edge, and only a run from the blocked address says whether that is what tips it.

Run it from inside the environment being questioned — the egress path is the subject, so
`railway run` (which executes locally) proves nothing:

    railway ssh -- python -m sbpeye.sbp_reachability --attempts 20

Imports stay to the HTTP clients on purpose: this must run in a container whose database
or volume may be exactly what is broken.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import cloudscraper
import requests

# The pages the scrapers hit, one per code path that fails when the block is on. Kept as
# a literal list rather than imported from `scraper.*` so the probe still runs when an
# import in that package is itself the problem.
#
# Each carries the size a real response clears. Cloudflare's block pages are a few KB of
# HTML and a truncated 200 is a failure that no header reports, so a floor is the only
# way to catch either — but it has to be per-target, because SBP serves legitimately
# small pages too and a single global threshold calls them blocked. The archive is
# addressed at `/index.html` rather than `/` for the same reason: the root is a 942-byte
# JavaScript redirect stub, reachable but indistinguishable by size from an error.
TARGETS: dict[str, str] = {
    "homepage": "https://www.sbp.org.pk/",            # scraper/news.py
    "circulars": "https://www.sbp.org.pk/circulars/",  # scraper/circulars.py listing
    "economic-data": "https://www.sbp.org.pk/economic-data",  # scraper/ecodata_index.py
    "archive": "https://archive.sbp.org.pk/index.html",  # ARCHIVE_BASE_URL fallbacks
}

# Measured medians from a clean connection, used only to estimate a run's duration. Wrong
# by a second either way costs nothing; the point is telling someone "four minutes" rather
# than leaving them to guess whether it has hung.
TYPICAL_SECONDS: dict[str, float] = {
    "homepage": 2.3,
    "circulars": 2.8,
    "economic-data": 5.3,
    "archive": 0.9,
}

# Floors, not expected sizes: set well under what each page actually returns so that
# ordinary content changes never trip them.
MIN_BYTES: dict[str, int] = {
    "homepage": 20_000,
    "circulars": 20_000,
    "economic-data": 10_000,
    "archive": 20_000,
}

# Same UA the scrapers send, so a difference in outcome is a difference in network
# position and not in what we claimed to be.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Unrelated to SBP: it answers "does this host have outbound HTTPS at all", which is the
# question you have to rule out before blaming the destination.
EGRESS_IP_URL = "https://api.ipify.org?format=json"

# `curl_cffi` is optional. It carries a statically linked libcurl-impersonate, so an
# image that has it needs no system packages — but the currently deployed image predates
# this arm, and a probe that cannot start is worse than one that reports three arms.
try:  # pragma: no cover - presence depends on the environment being probed
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

# Named after the library that actually issues the request, so every line of output says
# which client produced it without a legend:
#
#   cloudscraper        a new `create_scraper()` per request — exactly what every call
#                       site in `scraper/` does today, and the arm to compare against
#   cloudscraper-reuse  one scraper reused across requests
#   requests            plain `requests`, the control: if it matches cloudscraper,
#                       cloudscraper is contributing nothing
#   curl_cffi           Chrome's real TLS and HTTP/2 fingerprint
ARMS = ("cloudscraper", "cloudscraper-reuse", "requests", "curl_cffi")

# Only circulars by default. Probing all four targets is 4x the wall clock to answer the
# same question — the block is applied per request at the edge, not per page — and a full
# run took long enough that it read as a hang. The others stay available via `-t` for
# confirming a finding is site-wide rather than one path.
DEFAULT_TARGETS = ("circulars",)

# Which Chrome curl_cffi presents. Pinned rather than left to the library default so a
# result stays comparable across runs after an upgrade moves the default forward.
IMPERSONATE_TARGET = "chrome131"

# Cloudflare serves its interstitial and its block page as normal HTML. These are the
# markers that distinguish one from a real SBP page; matched lowercase against the first
# few KB, which is where they all appear.
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "cf-browser-verification",
    "cf_chl_opt",
    "enable javascript and cookies to continue",
    "sorry, you have been blocked",
)


@dataclass
class Attempt:
    """One request. `ok` is the honest verdict, not the status line."""

    target: str
    arm: str
    attempt: int
    ok: bool
    status: int | None = None
    elapsed_ms: int | None = None
    bytes: int | None = None
    cf_ray: str | None = None
    cf_mitigated: str | None = None
    server: str | None = None
    challenge: bool = False
    error: str | None = None


@dataclass
class Summary:
    target: str
    arm: str
    attempts: int
    ok: int
    forbidden: int
    challenged: int
    short: int
    errors: int
    median_ms: int | None
    rays: list[str] = field(default_factory=list)

    @property
    def ok_rate(self) -> float:
        return self.ok / self.attempts if self.attempts else 0.0


def _looks_like_challenge(body: bytes) -> bool:
    head = body[:8192].decode("utf-8", errors="ignore").lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


def _egress_ip(timeout: int) -> dict:
    """Identify the address SBP sees, and prove outbound HTTPS works at all."""
    started = time.monotonic()
    try:
        resp = requests.get(EGRESS_IP_URL, timeout=timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}", "elapsed_ms": elapsed}
        return {"ok": True, "ip": resp.json().get("ip"), "elapsed_ms": elapsed}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def _client(arm: str, cache: dict):
    """Return the client for an arm, creating per-call or reusing as the arm intends."""
    if arm == "cloudscraper":
        # Deliberately not cached: reproducing `cloudscraper.create_scraper().get(...)`
        # at every call site in `scraper/` is the whole point of this arm.
        return cloudscraper.create_scraper()
    if arm == "cloudscraper-reuse":
        if arm not in cache:
            cache[arm] = cloudscraper.create_scraper()
        return cache[arm]
    if arm == "requests":
        if arm not in cache:
            cache[arm] = requests.Session()
        return cache[arm]
    if arm == "curl_cffi":
        if curl_requests is None:
            raise RuntimeError("curl_cffi is not installed in this environment")
        if arm not in cache:
            cache[arm] = curl_requests.Session(impersonate=IMPERSONATE_TARGET)
        return cache[arm]
    raise ValueError(f"Unknown arm: {arm}")


def _probe_once(target: str, url: str, arm: str, index: int, timeout: int, cache: dict) -> Attempt:
    started = time.monotonic()
    try:
        client = _client(arm, cache)
        # No headers on the curl_cffi arm: it sends Chrome's real header set in Chrome's
        # real order, and overriding the User-Agent from `HEADERS` would put a
        # hand-written header back into the profile the arm exists to reproduce.
        kwargs = {"timeout": timeout} if arm == "curl_cffi" else {"headers": HEADERS, "timeout": timeout}
        resp = client.get(url, **kwargs)
        elapsed = int((time.monotonic() - started) * 1000)
        body = resp.content or b""
        challenge = _looks_like_challenge(body)
        return Attempt(
            target=target,
            arm=arm,
            attempt=index,
            # A challenge body is a failure however it was numbered, and a 200 that came
            # back short is an error page or a truncated one.
            ok=resp.status_code == 200 and not challenge and len(body) >= MIN_BYTES.get(target, 1024),
            status=resp.status_code,
            elapsed_ms=elapsed,
            bytes=len(body),
            cf_ray=resp.headers.get("cf-ray"),
            cf_mitigated=resp.headers.get("cf-mitigated"),
            server=resp.headers.get("server"),
            challenge=challenge,
        )
    except Exception as exc:
        return Attempt(
            target=target,
            arm=arm,
            attempt=index,
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def _summarize(attempts: list[Attempt]) -> list[Summary]:
    grouped: dict[tuple[str, str], list[Attempt]] = {}
    for attempt in attempts:
        grouped.setdefault((attempt.target, attempt.arm), []).append(attempt)

    summaries = []
    for (target, arm), rows in grouped.items():
        latencies = [r.elapsed_ms for r in rows if r.elapsed_ms is not None]
        summaries.append(
            Summary(
                target=target,
                arm=arm,
                attempts=len(rows),
                ok=sum(1 for r in rows if r.ok),
                forbidden=sum(1 for r in rows if r.status == 403),
                challenged=sum(1 for r in rows if r.challenge),
                # Answered 200, not a challenge, still too small to be the real page.
                short=sum(
                    1 for r in rows
                    if r.status == 200 and not r.challenge and not r.ok
                ),
                errors=sum(1 for r in rows if r.error),
                median_ms=int(statistics.median(latencies)) if latencies else None,
                # Enough rays to look up on the other side without pasting a whole run.
                rays=[r.cf_ray for r in rows if r.cf_ray][:5],
            )
        )
    return summaries


def _verdict(egress: dict, summaries: list[Summary]) -> str:
    """One line an operator can act on, derived from the arms rather than guessed."""
    if not egress.get("ok"):
        return "no-outbound-http"
    if not summaries:
        return "not-probed"

    total = sum(s.attempts for s in summaries)
    ok = sum(s.ok for s in summaries)
    if ok == total:
        return "reachable"
    if ok == 0:
        return "blocked"

    # Partial success. Whether the arms agree decides who owns the fix: a spread means
    # the client is doing something wrong, a flat rate across arms means the edge is
    # sampling us and only backoff or a different egress helps.
    by_arm = {arm: (0, 0) for arm in ARMS}
    for s in summaries:
        prev_ok, prev_n = by_arm.get(s.arm, (0, 0))
        by_arm[s.arm] = (prev_ok + s.ok, prev_n + s.attempts)
    rates = [o / n for o, n in by_arm.values() if n]
    if rates and max(rates) - min(rates) >= 0.34:
        return "intermittent-client-dependent"
    return "intermittent"


def plan_size(attempts: int, targets: list[str], arms: list[str]) -> int:
    return attempts * len(targets) * len(arms)


def estimate_seconds(attempts: int, targets: list[str], arms: list[str], delay: float) -> int:
    """Roughly how long a run will take, for a caller that has to decide to wait for it.

    Uses measured per-target medians rather than one average: `economic-data` is six
    times slower than `archive`, so an average would mislead by minutes on a narrowed
    run, which is exactly when someone is watching the clock.
    """
    return int(sum(TYPICAL_SECONDS.get(t, 3.0) * len(arms) * attempts for t in targets)
               + plan_size(attempts, targets, arms) * delay)


def run_probe(
    attempts: int = 3,
    targets: list[str] | None = None,
    arms: list[str] | None = None,
    timeout: int = 30,
    delay: float = 0.5,
    on_attempt=None,
) -> dict:
    """Probe SBP `attempts` times per target per arm and return the full record.

    Serial by design. Firing concurrently would measure how the edge treats a burst,
    which is not how the scrapers behave and not the question being asked — but it also
    makes a full run minutes long, so `on_attempt` is called with each `Attempt` and the
    running total as it completes. Without it the CLI sits silent for the whole run and
    looks hung, which is indistinguishable from the network failure being investigated.
    """
    chosen_targets = targets or list(DEFAULT_TARGETS)
    chosen_arms = arms or list(ARMS)
    unknown = [t for t in chosen_targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"Unknown target(s): {', '.join(unknown)}")
    unknown_arms = [a for a in chosen_arms if a not in ARMS]
    if unknown_arms:
        raise ValueError(f"Unknown arm(s): {', '.join(unknown_arms)}")

    skipped_arms = []
    if "curl_cffi" in chosen_arms and curl_requests is None:
        # Dropped rather than run: without the library every attempt would record the
        # same ImportError, and a column of errors reads like a network finding.
        chosen_arms = [a for a in chosen_arms if a != "curl_cffi"]
        skipped_arms.append("curl_cffi (not installed in this environment)")

    started_at = datetime.now(timezone.utc)
    egress = _egress_ip(timeout)

    cache: dict = {}
    records: list[Attempt] = []
    total = plan_size(attempts, chosen_targets, chosen_arms)
    for index in range(1, attempts + 1):
        for target in chosen_targets:
            for arm in chosen_arms:
                record = _probe_once(target, TARGETS[target], arm, index, timeout, cache)
                records.append(record)
                if on_attempt is not None:
                    on_attempt(record, len(records), total)
                # No sleep after the final request: it is a gap between requests, and
                # trailing it just makes the run look slower than it was.
                if delay and len(records) < total:
                    time.sleep(delay)

    summaries = _summarize(records)
    finished_at = datetime.now(timezone.utc)
    return {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 1),
        "egress": egress,
        "attempts_per_cell": attempts,
        "targets": {name: TARGETS[name] for name in chosen_targets},
        "arms": chosen_arms,
        "skipped_arms": skipped_arms,
        "impersonate_target": IMPERSONATE_TARGET if "curl_cffi" in chosen_arms else None,
        "verdict": _verdict(egress, summaries),
        "summary": [asdict(s) | {"ok_rate": round(s.ok_rate, 3)} for s in summaries],
        "attempts": [asdict(r) for r in records],
    }


def format_report(result: dict) -> str:
    lines = []
    egress = result["egress"]
    if egress.get("ok"):
        lines.append(f"egress IP   {egress['ip']}  ({egress['elapsed_ms']} ms)")
    else:
        lines.append(f"egress IP   UNAVAILABLE — {egress.get('error')}")
    lines.append(f"verdict     {result['verdict']}")
    lines.append(f"duration    {result['duration_s']}s")
    if result.get("impersonate_target"):
        lines.append(f"impersonate {result['impersonate_target']}")
    for skipped in result.get("skipped_arms", []):
        lines.append(f"skipped     {skipped}")
    lines.append("")
    lines.append(
        f"{'target':<15} {'arm':<19} {'ok':>7} {'403':>5} {'chal':>5} {'short':>6} {'err':>5} {'p50':>8}"
    )
    lines.append("-" * 77)
    for row in sorted(result["summary"], key=lambda r: (r["target"], r["arm"])):
        p50 = f"{row['median_ms']}ms" if row["median_ms"] is not None else "-"
        lines.append(
            f"{row['target']:<15} {row['arm']:<19} "
            f"{row['ok']}/{row['attempts']:<5} {row['forbidden']:>5} "
            f"{row['challenged']:>5} {row['short']:>6} {row['errors']:>5} {p50:>8}"
        )

    rays = [r for row in result["summary"] for r in row["rays"]]
    if rays:
        lines.append("")
        lines.append("cf-ray samples: " + ", ".join(rays[:8]))
    return "\n".join(lines)


def _progress_line(record: Attempt, done: int, total: int) -> str:
    if record.error:
        outcome = record.error.split(":")[0]
    elif record.challenge:
        outcome = f"CHALLENGE {record.status}"
    elif record.ok:
        outcome = f"ok {record.status}"
    else:
        outcome = f"FAIL {record.status}"
    elapsed = f"{record.elapsed_ms}ms" if record.elapsed_ms is not None else "-"
    return f"[{done:>3}/{total}] {record.target:<14} {record.arm:<19} {outcome:<14} {elapsed:>8}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sbpeye.sbp_reachability",
        description="Measure this host's success rate reaching sbp.org.pk.",
    )
    parser.add_argument("-n", "--attempts", type=int, default=3,
                        help="attempts per target per arm (default: 3)")
    parser.add_argument("-t", "--target", action="append", dest="targets",
                        choices=sorted(TARGETS), help="add a target (repeatable; default: circulars only)")
    parser.add_argument("-a", "--arm", action="append", dest="arms",
                        choices=list(ARMS), help="restrict to a client arm (repeatable)")
    parser.add_argument("--timeout", type=int, default=30, help="per-request timeout (default: 30)")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="seconds between requests (default: 0.5)")
    parser.add_argument("--json", action="store_true", help="emit the full record as JSON")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress the per-request progress log on stderr")
    args = parser.parse_args(argv)

    targets = args.targets or list(DEFAULT_TARGETS)
    arms = args.arms or [a for a in ARMS if a != "curl_cffi" or curl_requests is not None]

    # Progress goes to stderr so `--json > file` still yields clean JSON, and so a run
    # over `railway ssh` shows something within a couple of seconds. A full run is 48
    # serial requests; announcing the size up front is what lets someone narrow it with
    # -t/-a instead of waiting out a scope they did not want.
    def report_progress(record, done, total):
        print(_progress_line(record, done, total), file=sys.stderr, flush=True)

    if not args.quiet:
        seconds = estimate_seconds(args.attempts, targets, arms, args.delay)
        print(
            f"probing {plan_size(args.attempts, targets, arms)} requests "
            f"({len(targets)} targets x {len(arms)} arms x {args.attempts}) "
            f"— roughly {seconds // 60}m{seconds % 60:02d}s, serial by design",
            file=sys.stderr, flush=True,
        )

    result = run_probe(
        attempts=args.attempts,
        targets=args.targets,
        arms=args.arms,
        timeout=args.timeout,
        delay=args.delay,
        on_attempt=None if args.quiet else report_progress,
    )

    if not args.quiet:
        print("", file=sys.stderr, flush=True)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))

    # Exit non-zero when nothing got through, so a scheduled run of this is a usable
    # alarm rather than something a human has to read.
    return 0 if result["verdict"] in {"reachable", "intermittent", "intermittent-client-dependent"} else 1


if __name__ == "__main__":
    sys.exit(main())
