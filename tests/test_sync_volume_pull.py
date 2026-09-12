"""Phase 5 of the laws uploads plan: `sync_volume.py pull`.

Railway is never contacted — `ssh` is replaced by a fake volume built from a local
directory, which is the only part of the real thing this logic depends on.

`pull` writes into `files/laws/`, the one tree in the system nothing may delete from
(see env.py), so the guards that stop it overwriting or escaping are what these cover.

See docs/LAWS_UPLOADS_PLAN.md §7 phase 5 and docs/VOLUME_SYNC.md.
"""

import base64
import importlib.util
import io
import sys
import tarfile
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_volume.py"


@pytest.fixture
def sync_volume(monkeypatch, tmp_path):
    """The script loaded as a module, with a fake volume backed by tmp_path."""
    spec = importlib.util.spec_from_file_location("sync_volume_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    local = tmp_path / "local"
    volume = tmp_path / "volume"
    local.mkdir()
    volume.mkdir()
    monkeypatch.setattr(module, "LOCAL_ROOT", local)

    def fake_ssh(command: str, timeout: int = 600) -> str:
        """Answer the two shapes of command `pull` issues, against tmp_path."""
        if command.startswith("find "):
            lines = []
            for path in sorted(volume.rglob("*")):
                if path.is_file():
                    lines.append(f"{path.stat().st_size}\t{path.relative_to(volume).as_posix()}")
            return "\n".join(lines) + ("\n" if lines else "")
        if command.startswith("tar czf -"):
            # Everything between the `-C <root>` and the pipe is the argv file list.
            import shlex as _shlex

            argv = _shlex.split(command.split("|")[0])
            names = argv[argv.index("-C") + 2 :]
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for name in names:
                    tar.add(volume / name, arcname=name)
            return base64.b64encode(buffer.getvalue()).decode()
        raise AssertionError(f"unexpected ssh command: {command}")

    monkeypatch.setattr(module, "ssh", fake_ssh)

    class Harness:
        def __init__(self):
            self.module = module
            self.local = local
            self.volume = volume

        def on_volume(self, rel: str, body: bytes) -> Path:
            path = volume / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            return path

        def here(self, rel: str, body: bytes) -> Path:
            path = local / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            return path

        def pull(self, subtree=None, apply=True) -> int:
            return module.cmd_pull(Namespace(subtree=subtree, apply=apply))

    return Harness()


def test_pull_fetches_an_upload_that_exists_only_on_the_volume(sync_volume, capsys):
    """The whole point: an admin uploaded through the console and the bytes are up there."""
    sync_volume.on_volume("laws/doc-1/b87a4f82-bco.pdf", b"%PDF-1.7 uploaded")
    sync_volume.here("laws/doc-2/aaaaaaaa-sme-prs.pdf", b"%PDF-1.7 already here")

    assert sync_volume.pull("laws") == 0

    fetched = sync_volume.local / "laws/doc-1/b87a4f82-bco.pdf"
    assert fetched.read_bytes() == b"%PDF-1.7 uploaded"
    assert "wrote 1 file(s)" in capsys.readouterr().out


def test_a_dry_run_reports_and_writes_nothing(sync_volume, capsys):
    sync_volume.on_volume("laws/doc-1/b87a4f82-bco.pdf", b"%PDF-1.7 uploaded")

    assert sync_volume.pull("laws", apply=False) == 0

    assert not (sync_volume.local / "laws/doc-1/b87a4f82-bco.pdf").exists()
    assert "dry run" in capsys.readouterr().out


def test_pull_never_overwrites_a_local_file(sync_volume, capsys):
    """Under files/laws a filename carries its content hash, so a same-size collision is
    the same bytes — and overwriting is how the local copy of an edition gets lost."""
    sync_volume.on_volume("laws/doc-1/b87a4f82-bco.pdf", b"volume copy")
    ours = sync_volume.here("laws/doc-1/b87a4f82-bco.pdf", b"local copy!")

    sync_volume.pull("laws")

    assert ours.read_bytes() == b"local copy!"


def test_a_size_mismatch_is_a_reported_conflict_rather_than_a_fetch(sync_volume, capsys):
    sync_volume.on_volume("laws/doc-1/b87a4f82-bco.pdf", b"volume copy, longer")
    ours = sync_volume.here("laws/doc-1/b87a4f82-bco.pdf", b"local")

    # Non-zero: an operator scripting this has to notice.
    assert sync_volume.pull("laws") == 1

    assert ours.read_bytes() == b"local"
    output = capsys.readouterr().out
    assert "differ in size and will NOT be touched" in output
    assert "laws/doc-1/b87a4f82-bco.pdf" in output


def test_pull_deletes_nothing_the_volume_no_longer_has(sync_volume):
    """Push's mirror-image would prune; the archive forbids it (env.py's invariant)."""
    ours = sync_volume.here("laws/doc-9/old-edition.pdf", b"superseded but ours")
    sync_volume.on_volume("laws/doc-1/b87a4f82-bco.pdf", b"uploaded")

    sync_volume.pull("laws")

    assert ours.is_file()


def test_a_subtree_scopes_the_fetch_on_directory_boundaries(sync_volume):
    sync_volume.on_volume("laws/doc-1/act.pdf", b"a law")
    sync_volume.on_volume("laws_old/doc-1/act.pdf", b"a sibling, not a subdirectory")
    sync_volume.on_volume("circulars/c-1/att.pdf", b"an attachment")

    sync_volume.pull("laws")

    assert (sync_volume.local / "laws/doc-1/act.pdf").is_file()
    assert not (sync_volume.local / "laws_old/doc-1/act.pdf").exists()
    assert not (sync_volume.local / "circulars/c-1/att.pdf").exists()


def test_a_member_addressing_outside_the_tree_is_refused(sync_volume, tmp_path):
    """The tar comes off a remote host and is not trusted to address our filesystem."""
    module = sync_volume.module
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        payload = b"escaped"
        info = tarfile.TarInfo("../escaped.pdf")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    written, skipped = module._safe_extract(buffer.getvalue(), sync_volume.local)

    assert written == 0
    assert "path escapes" in skipped[0]
    assert not (sync_volume.local.parent / "escaped.pdf").exists()


def test_an_oversized_pull_says_to_narrow_the_subtree(sync_volume, monkeypatch, capsys):
    monkeypatch.setattr(sync_volume.module, "PULL_MAX_BYTES", 8)
    sync_volume.on_volume("laws/doc-1/act.pdf", b"more than eight bytes")

    assert sync_volume.pull("laws") == 1

    assert not (sync_volume.local / "laws/doc-1/act.pdf").exists()
    assert "narrow the subtree" in capsys.readouterr().err


def test_many_files_are_fetched_in_batches(sync_volume, monkeypatch):
    """The remote tar takes its file list as argv, so the batch size is an ARG_MAX budget."""
    monkeypatch.setattr(sync_volume.module, "PULL_BATCH_FILES", 3)
    for index in range(7):
        sync_volume.on_volume(f"laws/doc-{index}/act.pdf", f"law {index}".encode())

    assert sync_volume.pull("laws") == 0

    for index in range(7):
        assert (sync_volume.local / f"laws/doc-{index}/act.pdf").read_bytes() == f"law {index}".encode()
