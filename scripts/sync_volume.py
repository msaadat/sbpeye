"""Incrementally sync a local file tree onto the Railway volume.

`railway volume files upload` has no resume and no skip-existing mode, and its
directory form has `cp -r` semantics: uploading ./files/laws to /files/laws when
that path already exists writes /files/laws/laws instead of merging. Re-running a
timed-out upload therefore nests it rather than continuing it.

This script avoids both problems:

  * the remote manifest comes from `find` over `railway ssh`, in one call, rather
    than from `railway volume files list` -- which caps its output and costs ~6.5s
    per directory (3.6 hours for this tree's 1985 directories);
  * only missing or size-mismatched files are sent, packed into one tar and split
    into chunks, so the transfer is a handful of single-file uploads instead of
    thousands of ~6.5s CLI invocations;
  * chunks already on the volume at the right size are skipped, so an interrupted
    run resumes by being run again.

Usage:
    python scripts/sync_volume.py status [SUBTREE]
    python scripts/sync_volume.py fix-nesting [--apply]
    python scripts/sync_volume.py prune-duplicates [--apply]
    python scripts/sync_volume.py push [SUBTREE] [--apply] [--chunk-mb 64]
    python scripts/sync_volume.py pull [SUBTREE] [--apply]
    python scripts/sync_volume.py cleanup --apply

SUBTREE limits the operation to one part of the tree, so it can go up in stages --
laws first, since it is the archive that cannot be re-fetched:

    python scripts/sync_volume.py push laws --apply
    python scripts/sync_volume.py push circulars --apply
    python scripts/sync_volume.py push cache/html --apply

`pull` is the other direction, and it exists for one reason: admin-uploaded law
documents (docs/LAWS_UPLOADS_PLAN.md). Everything else on the volume came from SBP and
can be re-fetched, but an upload's bytes exist only where the administrator put them --
and the deployment runbook re-uploads `sbpeye.db` wholesale, which would orphan any files
created on the volume since. So before a DB push:

    python scripts/sync_volume.py pull laws --apply

Pull never deletes and never overwrites a local file. A local file of a different size is
reported as a conflict and skipped, because under `files/laws/` a filename carries its
own content hash: same name, different size means something is wrong, not stale.

Every mutating phase is a dry run unless --apply is passed.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

def _railway_bin() -> str:
    """Resolve the CLI. On Windows the launcher is railway.cmd, which
    subprocess will not find under the bare name.
    """
    override = os.environ.get("RAILWAY_BIN")
    if override:
        return override
    for name in ("railway.cmd", "railway.exe", "railway"):
        found = shutil.which(name)
        if found:
            return found
    return "railway"


RAILWAY = _railway_bin()
VOLUME = os.environ.get("SBPEYE_VOLUME", "sbpeye-volume-76qt")
SERVICE = os.environ.get("SBPEYE_SERVICE", "sbpeye")

LOCAL_ROOT = Path(os.environ.get("SBPEYE_LOCAL_ROOT", "files"))
REMOTE_ROOT = "/data/files"      # absolute, as seen inside the container
STAGE = "/data/_upload"          # scratch dir for chunks, removed by `cleanup`

# Directories that a nested re-run would have created, mapped to where their
# contents belong. Checked by `fix-nesting`.
NESTED = [
    (f"{REMOTE_ROOT}/cache/html/html", f"{REMOTE_ROOT}/cache/html"),
    (f"{REMOTE_ROOT}/laws/laws", f"{REMOTE_ROOT}/laws"),
    (f"{REMOTE_ROOT}/circulars/circulars", f"{REMOTE_ROOT}/circulars"),
    (f"{REMOTE_ROOT}/cache/parses/parses", f"{REMOTE_ROOT}/cache/parses"),
    (f"{REMOTE_ROOT}/files", REMOTE_ROOT),
]


# --------------------------------------------------------------------------- #
# plumbing


def ssh(command: str, timeout: int = 600) -> str:
    """Run a shell command inside the running container and return its stdout.

    stdin is closed: the CLI wrapper blocks waiting on it otherwise.
    """
    proc = subprocess.run(
        [RAILWAY, "ssh", "-s", SERVICE, "--", "sh", "-c", command],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ssh failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def upload(local: Path, remote: str, overwrite: bool = False) -> None:
    args = [RAILWAY, "volume", "files", "-v", VOLUME, "upload", str(local), remote]
    if overwrite:
        args.append("--overwrite")
    proc = subprocess.run(args, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"upload of {local.name} failed ({proc.returncode})")


def manifest(root: str) -> dict[str, int]:
    """{relative path: size} for every file under a remote directory."""
    out = ssh(f"find {shlex.quote(root)} -type f -printf '%s\\t%P\\n' 2>/dev/null || true")
    found: dict[str, int] = {}
    for line in out.splitlines():
        size, _, rel = line.partition("\t")
        if rel and size.isdigit():
            found[rel.replace("\\", "/")] = int(size)
    return found


def local_manifest() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in LOCAL_ROOT.rglob("*"):
        if path.is_file():
            rel = path.relative_to(LOCAL_ROOT).as_posix()
            found[rel] = path.stat().st_size
    return found


def diff() -> tuple[dict[str, int], dict[str, int], list[str], list[str]]:
    local = local_manifest()
    remote = manifest(REMOTE_ROOT)
    missing = sorted(k for k in local if k not in remote)
    mismatched = sorted(k for k in local if k in remote and remote[k] != local[k])
    return local, remote, missing, mismatched


def in_subtree(rel: str, subtree: str | None) -> bool:
    """Match a relative path against a subtree, on directory boundaries.

    Plain string prefixing would let `laws` also select a sibling named `laws_old`.
    """
    if not subtree:
        return True
    subtree = subtree.strip("/")
    return rel == subtree or rel.startswith(f"{subtree}/")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- #
# phases


def cmd_status(args: argparse.Namespace) -> int:
    subtree = getattr(args, "subtree", None)
    local, remote, missing, mismatched = diff()
    stray = sorted(k for k in remote if k not in local)
    if subtree:
        local = {k: v for k, v in local.items() if in_subtree(k, subtree)}
        remote = {k: v for k, v in remote.items() if in_subtree(k, subtree)}
        missing = [k for k in missing if in_subtree(k, subtree)]
        mismatched = [k for k in mismatched if in_subtree(k, subtree)]
        stray = [k for k in stray if in_subtree(k, subtree)]
        print(f"scope: files/{subtree.strip('/')}\n")

    print(f"local   {len(local):>6} files  {human(sum(local.values()))}")
    print(f"remote  {len(remote):>6} files  {human(sum(remote.values()))}")
    print()
    print(f"missing      {len(missing):>6}  {human(sum(local[k] for k in missing))} to send")
    print(f"wrong size   {len(mismatched):>6}  (truncated by an interrupted upload)")
    print(f"not in local {len(stray):>6}  (nested by a re-run, or genuinely stale)")

    for label, items in (("missing", missing), ("wrong size", mismatched), ("stray", stray)):
        if items:
            print(f"\nfirst {label}:")
            for k in items[:5]:
                print(f"  {k}")

    nested = [src for src, _ in NESTED if remote_dir_exists(src)]
    if nested:
        print("\nnested directories needing `fix-nesting`:")
        for src in nested:
            print(f"  {src}")
    return 0


def remote_dir_exists(path: str) -> bool:
    out = ssh(f"test -d {shlex.quote(path)} && echo yes || echo no")
    return "yes" in out


def cmd_fix_nesting(args: argparse.Namespace) -> int:
    """Move contents of an accidentally nested directory up one level.

    This is a rename inside the container, so 225 MB already uploaded is put right
    in seconds rather than re-sent.
    """
    for src, dst in NESTED:
        if not remote_dir_exists(src):
            continue
        count = ssh(f"find {shlex.quote(src)} -mindepth 1 -maxdepth 1 | wc -l").strip()
        print(f"{src} -> {dst}  ({count} entries)")
        if not args.apply:
            continue
        # -n: never overwrite a correctly placed file with a nested duplicate.
        # Anything it refuses to move stays behind and is dealt with by
        # `prune-duplicates`, which checks the two copies match before removing.
        ssh(
            f"cd {shlex.quote(src)} && "
            f"find . -mindepth 1 -maxdepth 1 -exec mv -n {{}} {shlex.quote(dst)}/ \\; ; "
            f"rmdir {shlex.quote(src)} 2>/dev/null || true"
        )
        left = ssh(f"find {shlex.quote(src)} -type f 2>/dev/null | wc -l").strip()
        print(f"  moved; {left} left behind (destination already occupied)")
    if not args.apply:
        print("\ndry run -- pass --apply to move")
    return 0


def cmd_prune_duplicates(args: argparse.Namespace) -> int:
    """Remove nested leftovers, but only where the correctly placed copy is identical.

    `mv -n` refuses to clobber, so a nested file whose destination already existed
    stays put. Removing it is safe only if the copy that won is the same size --
    otherwise the file still carries bytes the correct path does not have, and it
    is `push`'s job to settle which is right.
    """
    remote = manifest(REMOTE_ROOT)
    local = local_manifest()
    removable: list[str] = []
    kept: list[str] = []

    for src, dst in NESTED:
        prefix = src[len(REMOTE_ROOT) + 1 :] + "/"
        target_prefix = dst[len(REMOTE_ROOT) + 1 :]
        for rel, size in remote.items():
            if not rel.startswith(prefix):
                continue
            settled = rel[len(prefix) :]
            if target_prefix:
                settled = f"{target_prefix}/{settled}"
            if remote.get(settled) == size == local.get(settled):
                removable.append(rel)
            else:
                kept.append(rel)

    print(f"{len(removable)} nested duplicates safe to remove")
    for rel in removable[:10]:
        print(f"  {rel}")
    if kept:
        print(f"\n{len(kept)} left alone -- no matching correct copy, `push` will settle these:")
        for rel in kept[:10]:
            print(f"  {rel}")

    if not removable:
        return 0
    if not args.apply:
        print("\ndry run -- pass --apply to remove")
        return 0

    for rel in removable:
        ssh(f"rm -f {shlex.quote(f'{REMOTE_ROOT}/{rel}')}")
    for src, _ in NESTED:
        ssh(f"find {shlex.quote(src)} -type d -empty -delete 2>/dev/null || true")
    print(f"removed {len(removable)}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    local, _remote, missing, mismatched = diff()
    todo = sorted(set(missing) | set(mismatched))
    todo = [rel for rel in todo if in_subtree(rel, args.subtree)]
    scope = args.subtree or "everything"
    if not todo:
        print(f"nothing to send under {scope} -- remote matches local")
        return 0

    total = sum(local[k] for k in todo)
    print(f"{scope}: {len(todo)} files, {human(total)} to send")
    if not args.apply:
        print("dry run -- pass --apply to build and upload")
        return 0

    # Chunks are labelled per subtree. Without this the extract glob would pick up
    # a previous subtree's leftover chunks and concatenate them into this tar.
    label = (args.subtree or "all").strip("/").replace("/", "_")

    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    archive = work / f"{label}.tgz"

    # compresslevel=1: the html cache compresses well and the PDFs barely at all,
    # so anything higher spends CPU on incompressible bytes for no gain.
    print(f"packing -> {archive}")
    with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
        for i, rel in enumerate(todo, 1):
            tar.add(LOCAL_ROOT / rel, arcname=rel)
            if i % 500 == 0:
                print(f"  {i}/{len(todo)}")
    print(f"packed {human(archive.stat().st_size)}")

    chunk_size = args.chunk_mb * 1024 * 1024
    chunks = split(archive, work, chunk_size, label)
    print(f"{len(chunks)} chunks of up to {args.chunk_mb} MB")

    ssh(f"mkdir -p {shlex.quote(STAGE)}")
    staged = manifest(STAGE)

    for chunk in chunks:
        if staged.get(chunk.name) == chunk.stat().st_size:
            print(f"  {chunk.name} already staged, skipping")
            continue
        print(f"  uploading {chunk.name} ({human(chunk.stat().st_size)})")
        upload(chunk, f"{STAGE}/{chunk.name}", overwrite=chunk.name in staged)

    # Verify every chunk arrived whole before extracting: a short chunk would make
    # tar fail partway through and leave the tree half written.
    staged = manifest(STAGE)
    for chunk in chunks:
        if staged.get(chunk.name) != chunk.stat().st_size:
            print(
                f"chunk {chunk.name} is {staged.get(chunk.name)} on the volume, "
                f"expected {chunk.stat().st_size} -- re-run to resume",
                file=sys.stderr,
            )
            return 1

    print("extracting")
    ssh(
        f"cat {STAGE}/{label}.part.* | tar xzf - -C {shlex.quote(REMOTE_ROOT)}",
        timeout=1800,
    )
    # This subtree's chunks are consumed; leaving them would stage the whole
    # payload twice on a volume that has to hold the tree as well.
    ssh(f"rm -f {STAGE}/{label}.part.*")

    _, _, missing_after, mismatched_after = diff()
    missing_after = [r for r in missing_after if in_subtree(r, args.subtree)]
    mismatched_after = [r for r in mismatched_after if in_subtree(r, args.subtree)]
    print(f"\nafter ({scope}): {len(missing_after)} missing, {len(mismatched_after)} wrong size")
    if not missing_after and not mismatched_after:
        print(f"complete -- run `cleanup --apply` to remove {STAGE}")
        return 0
    return 1


def split(archive: Path, work: Path, chunk_size: int, label: str) -> list[Path]:
    """Split into zero-padded parts so a lexical glob concatenates them in order."""
    for old in work.glob(f"{label}.part.*"):
        old.unlink()
    chunks: list[Path] = []
    with archive.open("rb") as src:
        index = 0
        while True:
            data = src.read(chunk_size)
            if not data:
                break
            chunk = work / f"{label}.part.{index:04d}"
            chunk.write_bytes(data)
            chunks.append(chunk)
            index += 1
    return chunks


# Files per `tar` invocation. The remote side takes its file list as argv, so the batch
# size is really an ARG_MAX budget; 200 archive paths is far inside it on any kernel.
PULL_BATCH_FILES = 200

# Refuse a pull bigger than this rather than push hundreds of MB through a base64 pipe.
# The intended payload is a handful of uploaded documents; anything at this scale means the
# subtree is wrong, and the operator should say which part they want.
PULL_MAX_BYTES = 256 * 1024 * 1024


def _safe_extract(payload: bytes, destination: Path) -> tuple[int, list[str]]:
    """Unpack a pulled tar under `destination`, refusing to clobber anything.

    Two guards, both because this writes into the archive:

      * a member whose path escapes `destination` (absolute, or containing `..`) is
        rejected outright -- the tar comes off a remote host and is not trusted to
        address our filesystem;
      * a member whose destination already exists is skipped. Pull is for recovering
        files we do not have; overwriting is how a local edition gets lost.

    Returns (written, skipped paths).
    """
    written = 0
    skipped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            target = (destination / member.name).resolve()
            if destination.resolve() not in target.parents:
                skipped.append(f"{member.name} (path escapes {destination})")
                continue
            if target.exists():
                skipped.append(f"{member.name} (already here)")
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            written += 1
    return written, skipped


def cmd_pull(args: argparse.Namespace) -> int:
    """Fetch files the volume has and we do not. The reverse of push, and never destructive."""
    local, remote, _missing, _mismatched = diff()
    scope = args.subtree or "everything"

    absent = sorted(
        rel for rel in remote if rel not in local and in_subtree(rel, args.subtree)
    )
    conflicts = sorted(
        rel
        for rel in remote
        if rel in local and remote[rel] != local[rel] and in_subtree(rel, args.subtree)
    )

    if conflicts:
        print(f"{len(conflicts)} file(s) differ in size and will NOT be touched:")
        for rel in conflicts[:10]:
            print(f"  {rel}  volume {human(remote[rel])}, local {human(local[rel])}")
        print("  resolve these by hand -- under files/laws a name carries its content hash,")
        print("  so one name with two sizes is a fault rather than a stale copy.\n")

    if not absent:
        print(f"nothing to fetch under {scope} -- local already has everything the volume does")
        return 1 if conflicts else 0

    total = sum(remote[rel] for rel in absent)
    print(f"{scope}: {len(absent)} file(s) on the volume and not here, {human(total)}")
    for rel in absent[:10]:
        print(f"  {rel}")
    if len(absent) > 10:
        print(f"  ... and {len(absent) - 10} more")

    if total > PULL_MAX_BYTES:
        print(
            f"\nthat is more than {human(PULL_MAX_BYTES)} -- narrow the subtree "
            "(e.g. `pull laws`) rather than pulling the whole tree this way",
            file=sys.stderr,
        )
        return 1

    if not args.apply:
        print("\ndry run -- pass --apply to fetch")
        return 0

    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped: list[str] = []
    for start in range(0, len(absent), PULL_BATCH_FILES):
        batch = absent[start : start + PULL_BATCH_FILES]
        print(f"fetching {start + 1}-{start + len(batch)} of {len(absent)}")
        # base64 because the only remote primitive here is `railway ssh`, whose stdout
        # this script captures as text -- there is no counterpart to `volume files upload`
        # that fetches, and raw bytes through that channel would not survive.
        paths = " ".join(shlex.quote(rel) for rel in batch)
        encoded = ssh(
            f"tar czf - -C {shlex.quote(REMOTE_ROOT)} {paths} | base64",
            timeout=1800,
        )
        batch_written, batch_skipped = _safe_extract(
            base64.b64decode("".join(encoded.split())), LOCAL_ROOT
        )
        written += batch_written
        skipped.extend(batch_skipped)

    print(f"\nwrote {written} file(s) into {LOCAL_ROOT}")
    if skipped:
        print(f"skipped {len(skipped)}:")
        for item in skipped[:10]:
            print(f"  {item}")

    _local_after, _remote_after, _m, _mm = diff()
    still = [
        rel
        for rel in _remote_after
        if rel not in _local_after and in_subtree(rel, args.subtree)
    ]
    print(f"after ({scope}): {len(still)} still only on the volume")
    return 0 if not still and not conflicts else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    if not args.apply:
        print(f"would remove {STAGE} from the volume -- pass --apply")
        return 0
    ssh(f"rm -rf {shlex.quote(STAGE)}")
    print(f"removed {STAGE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="diff local against the volume")
    status.add_argument("subtree", nargs="?", help="limit the report to one subtree of files/")
    status.set_defaults(func=cmd_status)

    fix = sub.add_parser("fix-nesting", help="move nested directories up one level")
    fix.add_argument("--apply", action="store_true")
    fix.set_defaults(func=cmd_fix_nesting)

    prune = sub.add_parser("prune-duplicates", help="remove nested leftovers that are already correctly placed")
    prune.add_argument("--apply", action="store_true")
    prune.set_defaults(func=cmd_prune_duplicates)

    push = sub.add_parser("push", help="send missing files as tar chunks")
    push.add_argument(
        "subtree",
        nargs="?",
        help="limit to one subtree of files/, e.g. laws, circulars, cache/html. "
        "Omit to send everything outstanding.",
    )
    push.add_argument("--apply", action="store_true")
    push.add_argument("--chunk-mb", type=int, default=64)
    # Outside the repo: the archive and its chunks are ~1.4 GB, and item 11.6 of
    # the deployment plan checks that `git status` stays clean.
    push.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "sbpeye_sync"))
    push.set_defaults(func=cmd_push)

    pull = sub.add_parser(
        "pull", help="fetch files the volume has and this checkout does not"
    )
    pull.add_argument(
        "subtree",
        nargs="?",
        help="limit to one subtree of files/. `laws` is the one that matters: it is "
        "where admin-uploaded documents land, and they exist nowhere else.",
    )
    pull.add_argument("--apply", action="store_true")
    pull.set_defaults(func=cmd_pull)

    clean = sub.add_parser("cleanup", help="remove the staging directory")
    clean.add_argument("--apply", action="store_true")
    clean.set_defaults(func=cmd_cleanup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
