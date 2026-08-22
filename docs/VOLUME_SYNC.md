# Syncing `files/` to the Railway volume

Operator runbook for `scripts/sync_volume.py`.

**This is the fallback path now.** SBP is reachable from the deployment again
(`DEPLOYMENT_PLAN.md` §2.1), so `circulars` and `cache` are cheaper for the app to fetch itself
from the admin console's Sync tab (§2.8) than to push from here. Use this tool for:

- **`files/laws`, always and only.** The archive cannot be re-fetched — SBP replaces law PDFs
  in place and keeps no history, and two superseded editions exist nowhere else (invariant 3.7).
  A console sync can never reproduce it, so this is the sole route.
- **Rebuilding a volume** from a known-good local tree.
- **Bulk backfill**, when pushing bytes beats waiting on a long unattended sync.

The `files/` tree is 5251 files, 963 MB. It is gitignored and not in the image, so for the laws
archive upload remains the only route.

`railway volume files upload` cannot do this on its own. Read §"Why not the CLI directly"
below before reaching for it — the failure is silent, not loud.

---

## Quick reference

Run everything from the repo root: the tool resolves `files/` relative to the working
directory.

```bash
python scripts/sync_volume.py status
```

```bash
python scripts/sync_volume.py push laws --apply
```

| Command | Does | Mutates |
|---|---|---|
| `status [SUBTREE]` | Diff local against the volume, by path **and size** | no |
| `fix-nesting [--apply]` | Move a nested directory's contents up one level | volume |
| `prune-duplicates [--apply]` | Remove nested leftovers whose correct copy matches | volume |
| `push [SUBTREE] [--apply]` | Tar what is missing, chunk it, upload, extract, re-verify | volume |
| `cleanup --apply` | Remove the staging directory | volume |

Every mutating phase is a dry run unless `--apply` is passed.

## The normal path

Push one subtree at a time, **laws first** — it is the archive that cannot be re-fetched
(`DEPLOYMENT_PLAN.md` §3.7). The rest is re-fetchable given an unblocked IP, so it
is the cheaper thing to lose.

```bash
python scripts/sync_volume.py push laws --apply
python scripts/sync_volume.py push cache/parses --apply
python scripts/sync_volume.py push cache/html --apply
python scripts/sync_volume.py push circulars --apply
```

Then confirm, and redeploy so the app opens what you uploaded:

```bash
python scripts/sync_volume.py status
```

Expect `missing 0` and `wrong size 0`.

### If it dies partway

**Run the identical command again.** Chunks already on the volume at the right size are
skipped, so a re-run continues rather than restarting. This is the whole reason the tool
exists.

The only state a failure can leave behind is chunks in `/data/_upload`, which the next
successful run of that subtree consumes. `cleanup --apply` removes them if you abandon a
subtree instead.

## How it works

1. **Diff.** The remote manifest comes from `find` over `railway ssh`, in one call, and is
   compared to the local tree by relative path and size.
2. **Pack.** Only missing or size-mismatched files go into one `tar.gz` (compresslevel 1 —
   the html cache compresses well, the PDFs not at all).
3. **Chunk.** Split into 64 MB parts named `<subtree>.part.NNNN`, uploaded one at a time.
4. **Extract.** `cat /data/_upload/<subtree>.part.* | tar xzf - -C /data/files` over ssh,
   then the chunks are deleted.
5. **Verify.** The diff is re-run and the remaining count reported.

Size is what decides a re-send, not mere presence: a file interrupted mid-upload is present
and short, and treating existence as success would leave it truncated permanently.

## Why not the CLI directly

Three properties of `railway volume files`, all measured against CLI 5.41.2. The full write-up
is `DEPLOYMENT_PLAN.md` §2.5.

**A directory upload onto an existing path nests.** `upload ./files/laws /files/laws` when
`/files/laws` already exists writes `/files/laws/laws/` — `cp -r` semantics. It does not
merge, skip, or error, and `--overwrite` does not change it. Re-running a timed-out upload
is therefore not a resume; it is a second copy in the wrong place. This has already happened
once here, putting 2662 files one level too deep.

**`list` truncates without saying so.** It reported 5 files in a directory holding 2561. Do
not verify with it. Use:

```bash
railway ssh -s sbpeye -- sh -c "find /data/files -type f | wc -l"
```

**Each invocation costs ~6.5 s.** Uploading 2664 files individually is ~4.8 hours; walking
1985 directories with `list` to diff them is ~3.6 hours. Hence tar-and-chunk.

## Recovering from a nested upload

If someone runs a raw directory upload and nests a tree:

```bash
python scripts/sync_volume.py status
```

Nested directories are listed at the end of the report. Then:

```bash
python scripts/sync_volume.py fix-nesting --apply
```

This is a `mv` inside the container, so an already-uploaded tree is put right in seconds
rather than re-sent. `mv -n` is used, so a nested copy never clobbers a correctly placed
file; anything it refuses to move stays behind and is reported. Clear that residue with:

```bash
python scripts/sync_volume.py prune-duplicates --apply
```

which removes a leftover **only** when the correctly placed copy is the same size as both it
and the local file. Anything else is left for `push` to settle.

## Configuration

| Variable | Default |
|---|---|
| `RAILWAY_BIN` | auto-detected (`railway.cmd` on Windows) |
| `SBPEYE_VOLUME` | `sbpeye-volume-76qt` |
| `SBPEYE_SERVICE` | `sbpeye` |
| `SBPEYE_LOCAL_ROOT` | `files` |

`--chunk-mb` defaults to 64. `--work` defaults to a directory under the system temp — kept
out of the repo deliberately, because the archive plus its chunks is ~1.4 GB and verification
item 11.6 checks that `git status` stays clean.

## Gotchas

- **Run from the repo root.** `files/` is resolved relative to the working directory.
- **Free space.** `push` needs roughly twice the payload size in temp: the archive plus its
  chunks.
- **Git Bash mangles remote paths.** `list /` becomes `list D:/Progs/Git/`. Export
  `MSYS_NO_PATHCONV=1` for raw `railway` commands carrying a remote path. The tool is
  unaffected — `subprocess` does no such rewriting, which is part of why it is Python.
- **The volume is only reachable through a running container.** Both `railway ssh` and
  `railway volume files` proxy through it, so the service cannot be stopped first
  (`DEPLOYMENT_PLAN.md` §2.4).
- **Do not push while the app is being used.** The extract writes into the tree the app
  reads. For `files/` the risk is low — a reader gets a file that appears mid-request — but
  the same is emphatically not true of `chroma_db/`, which must never be written under a
  live process (`DEPLOYMENT_PLAN.md` §2.4).
- **Uploaded files are root-owned**, since the container runs as root. Noted as production
  hardening in `DEPLOYMENT_PLAN.md` §1.3, harmless for a test deploy.
