"""Standalone identity dry run, without SQLAlchemy, Chroma or application bootstrap."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sbpeye.identity_migration import plan_migration


def main():
    root = Path(os.environ.get("SBPEYE_DATA_DIR") or Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=root / "sbpeye.db")
    parser.add_argument("--app", type=Path, default=Path(os.environ.get("SBPEYE_APP_DB") or root / "sbpeye_app.db"))
    parser.add_argument("--output", type=Path, help="Write a new manifest file (never overwrite)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--evidence", type=Path, help="Reviewed, fingerprinted provenance evidence keyed by old ID")
    parser.add_argument("--listing-snapshot", type=Path, help="Optional saved JSON listing descriptors; never re-fetches")
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.manifest or not args.backup_dir:
                parser.error("--apply requires --manifest and --backup-dir")
            from sbpeye.identity_migration import apply_migration
            from sbpeye.storage_preflight import require_exclusive_store
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            data_root = Path(manifest["corpus_path"]).parent
            if Path(manifest["corpus_path"]).name != "sbpeye.db":
                parser.error("Apply requires a corpus named sbpeye.db for index bootstrap")
            require_exclusive_store(data_root / "chroma_db")
            require_exclusive_store(Path(manifest["corpus_path"]))
            if manifest.get("app_path"):
                require_exclusive_store(Path(manifest["app_path"]))
            os.environ["SBPEYE_DATA_DIR"] = str(data_root)
            if manifest.get("app_path"):
                os.environ["SBPEYE_APP_DB"] = manifest["app_path"]
            def repair(reviewed):
                from sbpeye.mirror_repairs import repair_migration_indexes
                repair_migration_indexes(reviewed)
            result = apply_migration(manifest, args.backup_dir, repair_indexes=repair)
            print(json.dumps(result, indent=2))
            return 0
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
        snapshot = json.loads(args.listing_snapshot.read_text(encoding="utf-8")) if args.listing_snapshot else None
        report = plan_migration(args.corpus, args.app if args.app.exists() else None, evidence=evidence, listing_snapshot=snapshot)
        payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as target:
                target.write(payload)
        else:
            print(payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"Identity preflight failed: {exc}\n")
    return 2 if report["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
