from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.safe_files import UnsafeFilePathError
from backend.scripts.backup_workdir import _open_dump_descriptor, cleanup, create, refresh


def test_backup_workdir_create_refresh_and_identity_bound_cleanup(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    dump = work / "test-db.dump"
    dump.write_bytes(b"logical-backup")
    refreshed = refresh(str(work), (*created[:3], *created[3:5]))

    cleanup(str(work), refreshed)

    assert not work.exists()


def test_backup_workdir_cleanup_refuses_replacement_dump(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    dump = work / "test-db.dump"
    dump.write_bytes(b"logical-backup")
    refreshed = refresh(str(work), (*created[:3], *created[3:5]))
    replacement = tmp_path / "replacement.dump"
    replacement.write_bytes(b"replacement")
    dump.unlink()
    replacement.rename(dump)

    with pytest.raises(UnsafeFilePathError, match="dump identity changed"):
        cleanup(str(work), refreshed)

    assert dump.read_bytes() == b"replacement"


def test_backup_workdir_cleanup_refuses_replaced_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    moved = tmp_path / "moved"
    work.rename(moved)
    work.mkdir(mode=0o700)
    (work / "test-db.dump").write_bytes(b"replacement")

    with pytest.raises(UnsafeFilePathError, match="directory identity changed"):
        cleanup(str(work), created)

    assert (work / "test-db.dump").read_bytes() == b"replacement"
    assert os.path.isdir(moved)


def test_backup_workdir_writer_refuses_replacement_before_truncation(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    dump = work / "test-db.dump"
    replacement = tmp_path / "replacement.dump"
    replacement.write_bytes(b"must-survive")
    dump.unlink()
    replacement.rename(dump)

    with pytest.raises(UnsafeFilePathError, match="identity changed"):
        with _open_dump_descriptor(str(work), created[:5], write=True):
            pytest.fail("replacement dump must not be opened for writing")

    assert dump.read_bytes() == b"must-survive"


def test_backup_workdir_cli_writes_through_validated_descriptor(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "backend/scripts/backup_workdir.py",
            "write",
            str(work),
            *(str(value) for value in created[:5]),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'logical-backup')",
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        check=False,
        capture_output=True,
        text=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    refreshed = refresh(str(work), (*created[:3], *created[3:5]))
    assert refreshed[-1] == len(b"logical-backup")
    assert (work / "test-db.dump").read_bytes() == b"logical-backup"


def test_backup_workdir_cleanup_preserves_file_injected_after_quarantine_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.scripts.backup_workdir as backup_workdir

    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    created = create(str(work))
    dump = work / "test-db.dump"
    dump.write_bytes(b"logical-backup")
    refreshed = refresh(str(work), (*created[:3], *created[3:5]))
    original_rename = backup_workdir.os.rename

    def inject_after_move(source: str, destination: str, **kwargs: int) -> None:
        original_rename(source, destination, **kwargs)
        dump.write_bytes(b"replacement-must-survive")

    monkeypatch.setattr(backup_workdir.os, "rename", inject_after_move)

    with pytest.raises(OSError):
        cleanup(str(work), refreshed)

    assert dump.read_bytes() == b"replacement-must-survive"
