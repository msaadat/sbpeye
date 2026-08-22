"""Put the benchmark questions to a running SBPEye and capture the answers.

    python benchmarks/run_pilot.py --out results/2026-08-19-sbpeye/answers

Each question goes to `/api/chat` in a fresh session with no pre-selected circulars, so the
system has to find its own sources — the same position a user starts from. Answers are saved
one JSON per item, ready for `make_grading_pack.py`.

Questions are parsed from `pilot-v1-questions.md` rather than duplicated here, so the set
put to the system is always the set that was published.

Only SBPEye can be driven this way. Other systems under test are prompted by hand and their
answers saved into the same JSON shape: item, question, answer, elapsed_s.

`/api/chat` is behind the authentication boundary, so a run needs a session cookie: pass
`--cookie` or set `SBPEYE_SESSION_COOKIE`. Chat bills the *requesting user's* provider
credentials, so whichever account the cookie belongs to is the account that pays for the
round — and its configured chat model is the model under test. Record both alongside the
scores; a benchmark number means nothing without the model that produced it.
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SESSION_COOKIE = "sbpeye_session"

HERE = Path(__file__).parent
# "**P01.** text..." running until a blank line, or to end of file for the last item.
ITEM = re.compile(r"^\*\*(P\d+)\.\*\*\s*(.+?)(?=\n\s*\n|\Z)", re.MULTILINE | re.DOTALL)


def load_questions(path: Path) -> dict[str, str]:
    questions = {
        item: " ".join(text.split()) for item, text in ITEM.findall(path.read_text(encoding="utf-8"))
    }
    if not questions:
        raise SystemExit(f"{path}: no questions found (expected lines like '**P01.** ...')")
    return questions


def ask(url: str, item: str, question: str, out: Path, timeout: int, cookie: str) -> str:
    body = json.dumps({"message": question, "circular_ids": []}).encode()
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    request = urllib.request.Request(url, data=body, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        record = {
            "item": item,
            "question": question,
            "answer": payload.get("response", ""),
            "session_id": payload.get("session_id"),
            "elapsed_s": round(time.monotonic() - started, 1),
        }
    except urllib.error.HTTPError as exc:
        # 401 is a broken run, not a benchmark result: every remaining item would fail the
        # same way and the round would look like fifteen refusals from the system.
        if exc.code in (401, 403):
            raise SystemExit(
                f"{item}: {exc.code} from {url}. /api/chat is behind authentication — "
                "pass --cookie or set SBPEYE_SESSION_COOKIE."
            ) from exc
        record = {
            "item": item,
            "question": question,
            "answer": "",
            "error": f"HTTPError {exc.code}: {exc.reason}",
            "elapsed_s": round(time.monotonic() - started, 1),
        }
    except Exception as exc:
        # A failed turn is a benchmark result — record it and carry on to the next item.
        record = {
            "item": item,
            "question": question,
            "answer": "",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.monotonic() - started, 1),
        }
    (out / f"{item}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    status = "FAILED" if record.get("error") else f"{len(record['answer'])} chars"
    return f"{item} {record['elapsed_s']:>6.1f}s  {status}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default=str(HERE / "pilot-v1-questions.md"))
    parser.add_argument("--out", required=True, help="directory for the captured answers")
    parser.add_argument("--url", default="http://localhost:8000/api/chat")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--only", default="", help="comma-separated items, e.g. P03,P07")
    parser.add_argument(
        "--cookie",
        default=os.environ.get("SBPEYE_SESSION_COOKIE", ""),
        help=f"value of the {SESSION_COOKIE} cookie; defaults to $SBPEYE_SESSION_COOKIE",
    )
    args = parser.parse_args()

    questions = load_questions(Path(args.questions))
    if args.only:
        wanted = {item.strip().upper() for item in args.only.split(",")}
        questions = {k: v for k, v in questions.items() if k in wanted}
        if not questions:
            raise SystemExit(f"--only {args.only!r} matched none of {sorted(load_questions(Path(args.questions)))}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(questions)} question(s) -> {out}\n")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for line in pool.map(
            lambda kv: ask(args.url, kv[0], kv[1], out, args.timeout, args.cookie),
            questions.items(),
        ):
            print(line, flush=True)


if __name__ == "__main__":
    main()
