from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

EXPECTED_DIRECTORY_BATCH_SIZE = 100


def _new_file_target(storage_root: Path, content: bytes = b"removable") -> Path:
    target = storage_root / "files" / f"file_{uuid.uuid4().hex}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _write_tombstone(path: Path, target: Path, *, file_id: str | None = None) -> None:
    metadata = os.stat(target, follow_symlinks=False)
    path.write_text(
        json.dumps(
            {
                "file_id": file_id or target.name,
                "storage_path": str(target),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "ctime_ns": metadata.st_ctime_ns,
                "size": metadata.st_size,
            }
        ),
        encoding="utf-8",
    )


def test_pending_delete_cleanup_never_deletes_replacement_created_after_tombstone(
    client: TestClient,
    tmp_path: Path,
) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    target = _new_file_target(tmp_path, b"original")
    tombstone = pending_root / "target.json"
    _write_tombstone(tombstone, target)
    original = target.with_name(f"{target.name}.original")
    target.rename(original)
    target.write_bytes(b"replacement")

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (0, 1)
    assert target.read_bytes() == b"replacement"
    assert original.read_bytes() == b"original"
    assert tombstone.exists()


def test_pending_delete_cleanup_isolates_persistent_unlink_failure(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    blocked_target = _new_file_target(tmp_path, b"blocked")
    removable_target = _new_file_target(tmp_path)
    blocked_tombstone = pending_root / "blocked.json"
    removable_tombstone = pending_root / "removable.json"
    _write_tombstone(blocked_tombstone, blocked_target)
    _write_tombstone(removable_tombstone, removable_target)

    original_rename = os.rename

    def persistent_failure(source: str | Path, target: str | Path, **kwargs: Any) -> None:
        if Path(source).name == blocked_target.name and kwargs.get("src_dir_fd") is not None:
            raise OSError("forced persistent unlink failure")
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", persistent_failure)
    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (1, 1)
    assert blocked_target.exists()
    assert blocked_tombstone.exists()
    assert not removable_target.exists()
    assert not removable_tombstone.exists()
    quarantine = tmp_path / "files" / ".fcr-delete-quarantine"
    assert not quarantine.exists() or not list(quarantine.iterdir())


def test_pending_delete_cleanup_isolates_corrupt_tombstones(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    target = _new_file_target(tmp_path)
    (pending_root / "non-object.json").write_text("[]", encoding="utf-8")
    (pending_root / "invalid-utf8.json").write_bytes(b"\xff\xfe")
    removable_tombstone = pending_root / "removable.json"
    _write_tombstone(removable_tombstone, target)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (1, 2)
    assert not target.exists()
    assert not removable_tombstone.exists()
    assert (pending_root / "non-object.json").exists()
    assert (pending_root / "invalid-utf8.json").exists()


def test_pending_delete_scan_rotates_past_100_malformed_tombstones(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    assert maintenance.DIRECTORY_SCAN_BATCH_SIZE == EXPECTED_DIRECTORY_BATCH_SIZE
    blocker_names = [
        f"malformed-{index:03d}.json"
        for index in range(EXPECTED_DIRECTORY_BATCH_SIZE)
    ]
    for name in blocker_names:
        (pending_root / name).write_text("[]", encoding="utf-8")
    target = _new_file_target(tmp_path)
    cleanable_name = "zz-cleanable.json"
    _write_tombstone(pending_root / cleanable_name, target)

    class OrderedScan:
        def __init__(self, names: list[str]) -> None:
            self.entries = iter(SimpleNamespace(name=name) for name in names)
            self.next_calls = 0
            self.closed = False

        def __iter__(self) -> OrderedScan:
            return self

        def __next__(self) -> SimpleNamespace:
            self.next_calls += 1
            return next(self.entries)

        def close(self) -> None:
            self.closed = True

    scan = OrderedScan([*blocker_names, cleanable_name])
    monkeypatch.setattr(maintenance, "_open_directory_scan", lambda _fd: scan)

    with SessionLocal() as session:
        assert maintenance._cleanup_pending_deletes(session, tmp_path) == (
            0,
            EXPECTED_DIRECTORY_BATCH_SIZE,
        )
        assert scan.next_calls == EXPECTED_DIRECTORY_BATCH_SIZE
        assert target.exists()
        assert maintenance._cleanup_pending_deletes(session, tmp_path) == (1, 0)

    assert not target.exists()
    assert not (pending_root / cleanable_name).exists()
    assert scan.closed is True


def test_stale_referenced_tombstone_is_removed_without_deleting_live_file(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    target = _new_file_target(tmp_path, b"still-referenced")
    tombstone = pending_root / "rolled-back.json"
    tombstone.write_text(
        json.dumps(
            {
                "file_id": target.name,
                "storage_path": str(target),
                "created_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(maintenance, "_pending_delete_has_database_reference", lambda *_args: True)

    with SessionLocal() as session:
        removed, failed = maintenance._cleanup_pending_deletes(
            session,
            tmp_path,
            datetime.now(timezone.utc),
        )

    assert (removed, failed) == (1, 0)
    assert target.read_bytes() == b"still-referenced"
    assert not tombstone.exists()


def test_pending_delete_cleanup_rejects_symlinked_tombstone_directory(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    storage_root = tmp_path / "symlink-storage"
    outside_root = tmp_path / "outside"
    storage_root.mkdir()
    outside_root.mkdir()
    target = _new_file_target(storage_root)
    outside_tombstone = outside_root / "victim.json"
    _write_tombstone(outside_tombstone, target)
    (storage_root / "pending-deletes").symlink_to(outside_root, target_is_directory=True)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, storage_root)

    assert (removed, failed) == (0, 1)
    assert outside_tombstone.exists()


def test_pending_delete_cleanup_rejects_symlinked_tombstone_file(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    outside_root = tmp_path / "outside"
    pending_root.mkdir()
    outside_root.mkdir()
    target = _new_file_target(tmp_path, b"must-remain")
    outside_tombstone = outside_root / "external.json"
    _write_tombstone(outside_tombstone, target)
    (pending_root / "linked.json").symlink_to(outside_tombstone)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (0, 1)
    assert target.exists()
    assert outside_tombstone.exists()


def test_pending_delete_cleanup_rejects_fifo_without_blocking_batch(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    os.mkfifo(pending_root / "blocked.json")
    target = _new_file_target(tmp_path)
    _write_tombstone(pending_root / "removable.json", target)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (1, 1)
    assert (pending_root / "blocked.json").exists()
    assert not target.exists()


def test_pending_delete_cleanup_rejects_oversized_tombstone(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import MAX_TOMBSTONE_BYTES, _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    oversized = pending_root / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_TOMBSTONE_BYTES + 1))
    target = _new_file_target(tmp_path)
    _write_tombstone(pending_root / "removable.json", target)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (1, 1)
    assert oversized.exists()
    assert not target.exists()


def test_pending_delete_cleanup_isolates_json_integer_limit_error(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    pathological = pending_root / "pathological.json"
    pathological.write_text('{"storage_path":' + "9" * 5000 + "}", encoding="utf-8")
    target = _new_file_target(tmp_path)
    _write_tombstone(pending_root / "removable.json", target)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (1, 1)
    assert pathological.exists()
    assert not target.exists()


def test_pending_delete_cleanup_preserves_path_escape_tombstone(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    storage_root = tmp_path / "storage"
    pending_root = storage_root / "pending-deletes"
    pending_root.mkdir(parents=True)
    outside_target = tmp_path / "outside.mp4"
    outside_target.write_bytes(b"outside")
    escape_tombstone = pending_root / "escape.json"
    _write_tombstone(escape_tombstone, outside_target, file_id=f"file_{uuid.uuid4().hex}")
    valid_target = _new_file_target(storage_root, b"valid")
    _write_tombstone(pending_root / "valid.json", valid_target)

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, storage_root)

    assert (removed, failed) == (1, 1)
    assert outside_target.exists()
    assert escape_tombstone.exists()
    assert not valid_target.exists()


def test_pending_delete_cleanup_pins_parent_against_symlink_swap(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    storage_root = tmp_path / "storage"
    files_root = storage_root / "files"
    pending_root = storage_root / "pending-deletes"
    outside_root = tmp_path / "outside"
    files_root.mkdir(parents=True)
    pending_root.mkdir()
    outside_root.mkdir()
    file_id = f"file_{uuid.uuid4().hex}"
    inside_target = files_root / file_id
    outside_target = outside_root / file_id
    inside_target.write_bytes(b"inside")
    outside_target.write_bytes(b"outside")
    _write_tombstone(pending_root / "target.json", inside_target)
    original_reference_check = maintenance._pending_delete_has_database_reference

    def swap_parent(*args: Any, **kwargs: Any) -> bool:
        original_files_root = storage_root / "files-original"
        files_root.rename(original_files_root)
        files_root.symlink_to(outside_root, target_is_directory=True)
        return original_reference_check(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_pending_delete_has_database_reference", swap_parent)
    with SessionLocal() as session:
        removed, failed = maintenance._cleanup_pending_deletes(session, storage_root)

    assert (removed, failed) == (1, 0)
    assert outside_target.read_bytes() == b"outside"
    assert not (storage_root / "files-original" / file_id).exists()


def test_pending_delete_cleanup_keeps_scanned_root_identity_during_replacement(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    storage_root = tmp_path / "storage"
    pending_root = storage_root / "pending-deletes"
    pending_root.mkdir(parents=True)
    target = _new_file_target(storage_root, b"old-root")
    tombstone = pending_root / "target.json"
    _write_tombstone(tombstone, target)
    original_root = tmp_path / "storage-original"
    replacement_content = b"replacement-root"
    original_pin = maintenance._pin_regular_file_beneath
    swapped = False

    @contextmanager
    def swap_before_target_pin(root_fd: int, root: Path, relative_path: Path) -> Iterator[Any]:
        nonlocal swapped
        if relative_path.parts[0] == "files" and not swapped:
            storage_root.rename(original_root)
            (storage_root / "files").mkdir(parents=True)
            (storage_root / "files" / target.name).write_bytes(replacement_content)
            (storage_root / "pending-deletes").mkdir()
            swapped = True
        with original_pin(root_fd, root, relative_path) as pinned:
            yield pinned

    monkeypatch.setattr(maintenance, "_pin_regular_file_beneath", swap_before_target_pin)
    with SessionLocal() as session:
        assert maintenance._cleanup_pending_deletes(session, storage_root) == (1, 0)

    assert swapped is True
    assert (storage_root / "files" / target.name).read_bytes() == replacement_content
    assert not (original_root / "files" / target.name).exists()
    assert not (original_root / "pending-deletes" / tombstone.name).exists()


def test_pending_delete_cleanup_does_not_unlink_replaced_tombstone(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    target = _new_file_target(tmp_path, b"old-request")
    replacement_target = _new_file_target(tmp_path, b"new-request")
    tombstone = pending_root / "target.json"
    old_tombstone = pending_root / "target-old.json"
    _write_tombstone(tombstone, target)

    def replace_tombstone(*_args: Any, **_kwargs: Any) -> bool:
        tombstone.rename(old_tombstone)
        _write_tombstone(tombstone, replacement_target)
        return False

    monkeypatch.setattr(maintenance, "_pending_delete_has_database_reference", replace_tombstone)
    with SessionLocal() as session:
        assert maintenance._cleanup_pending_deletes(session, tmp_path) == (0, 1)

    assert not target.exists()
    assert replacement_target.exists()
    assert tombstone.exists()
    assert old_tombstone.exists()


def test_pending_delete_cleanup_rejects_invalid_field_contracts(client: TestClient, tmp_path: Path) -> None:
    from backend.app.maintenance import _cleanup_pending_deletes
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    pending_root = tmp_path / "pending-deletes"
    pending_root.mkdir()
    target = _new_file_target(tmp_path, b"must-remain")
    created_at = datetime.now(timezone.utc).isoformat()
    payloads = [
        {"file_id": None, "storage_path": str(target), "created_at": created_at},
        {"file_id": target.name, "storage_path": 42, "created_at": created_at},
        {"file_id": f"file_{uuid.uuid4().hex}", "storage_path": str(target), "created_at": created_at},
    ]
    for index, payload in enumerate(payloads):
        (pending_root / f"invalid-{index}.json").write_text(json.dumps(payload), encoding="utf-8")

    with SessionLocal() as session:
        removed, failed = _cleanup_pending_deletes(session, tmp_path)

    assert (removed, failed) == (0, len(payloads))
    assert target.read_bytes() == b"must-remain"


def test_private_file_write_and_unlink_use_durability_barriers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.safe_files import unlink_regular_file, write_private_file

    fsync_calls: list[int] = []
    rename_calls: list[tuple[str, str]] = []
    original_rename = os.rename

    monkeypatch.setattr(os, "fsync", lambda descriptor: fsync_calls.append(descriptor))

    def tracked_rename(source: str, target: str, **kwargs: Any) -> None:
        rename_calls.append((source, target))
        original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", tracked_rename)
    target = write_private_file(tmp_path, "pending-deletes", "durable.json", b"{}")

    assert target.read_bytes() == b"{}"
    assert rename_calls and rename_calls[0][1] == "durable.json"
    assert len(fsync_calls) >= 2
    before_unlink = len(fsync_calls)
    assert unlink_regular_file(target, tmp_path) is True
    assert len(fsync_calls) >= before_unlink + 2


def test_pinned_regular_file_rejects_leaf_replacement_before_unlink(tmp_path: Path) -> None:
    from backend.app.safe_files import UnsafeFilePathError, pin_regular_file

    target = tmp_path / "target.bin"
    target.write_bytes(b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")

    with pin_regular_file(target, tmp_path) as pinned:
        assert pinned is not None
        target.unlink()
        replacement.rename(target)
        with pytest.raises(UnsafeFilePathError, match="changed after it was pinned"):
            pinned.unlink()

    assert target.read_bytes() == b"replacement"


def test_pinned_regular_file_restores_replacement_swapped_during_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import safe_files
    from backend.app.safe_files import UnsafeFilePathError, pin_regular_file

    target = tmp_path / "target.bin"
    target.write_bytes(b"original")
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")
    original_rename = safe_files.os.rename
    swapped = False

    def swap_before_quarantine(source: str | Path, destination: str | Path, **kwargs: Any) -> None:
        nonlocal swapped
        if not swapped and Path(source).name == target.name and Path(destination).name == "target":
            swapped = True
            target.unlink()
            replacement.rename(target)
        original_rename(source, destination, **kwargs)

    monkeypatch.setattr(safe_files.os, "rename", swap_before_quarantine)
    with pin_regular_file(target, tmp_path) as pinned:
        assert pinned is not None
        with pytest.raises(UnsafeFilePathError, match="pinned file"):
            pinned.unlink()

    assert target.read_bytes() == b"replacement"
    quarantine = tmp_path / ".fcr-delete-quarantine"
    assert not quarantine.exists() or not list(quarantine.iterdir())


def test_pinned_regular_file_rejects_same_inode_content_change_after_quarantine_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import safe_files
    from backend.app.safe_files import UnsafeFilePathError, pin_regular_file

    original_content = b"original-content" * 4096
    changed_content = b"modified-content" * 4096
    assert len(changed_content) == len(original_content)
    target = tmp_path / "target.bin"
    target.write_bytes(original_content)
    original_rename = safe_files.os.rename

    def mutate_after_quarantine_move(source: str | Path, destination: str | Path, **kwargs: Any) -> None:
        original_rename(source, destination, **kwargs)
        descriptor = os.open(
            str(destination),
            os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=kwargs["dst_dir_fd"],
        )
        try:
            os.write(descriptor, changed_content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(safe_files.os, "rename", mutate_after_quarantine_move)
    with pin_regular_file(target, tmp_path) as pinned:
        assert pinned is not None
        with pytest.raises(UnsafeFilePathError, match="content changed"):
            pinned.unlink()

    assert target.read_bytes() == changed_content
    quarantine = tmp_path / ".fcr-delete-quarantine"
    assert not quarantine.exists() or not list(quarantine.iterdir())


def test_delete_quarantine_recovers_after_process_exit_between_rename_and_unlink(tmp_path: Path) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    target = tmp_path / "target.bin"
    target.write_bytes(b"crash-recoverable")
    repository_root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path

from backend.app import safe_files

root = Path(os.environ["FCR_TEST_DELETE_ROOT"])
target = root / "target.bin"
original_rename = safe_files.os.rename

def crash_after_rename(source, destination, **kwargs):
    original_rename(source, destination, **kwargs)
    os._exit(73)

safe_files.os.rename = crash_after_rename
with safe_files.pin_regular_file(target, root) as pinned:
    assert pinned is not None
    pinned.unlink()
""",
        ],
        cwd=repository_root,
        env={**os.environ, "FCR_TEST_DELETE_ROOT": str(tmp_path), "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
    )

    assert child.returncode == 73
    assert not target.exists()
    quarantine = tmp_path / ".fcr-delete-quarantine"
    assert len(list(quarantine.iterdir())) == 1
    assert recover_delete_quarantines(tmp_path) == (1, 0)
    assert not target.exists()
    assert not list(quarantine.iterdir())


def _create_crashed_delete_quarantine(root: Path, content: bytes) -> Path:
    target = root / "target.bin"
    target.write_bytes(content)
    repository_root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path

from backend.app import safe_files

root = Path(os.environ["FCR_TEST_DELETE_ROOT"])
target = root / "target.bin"
original_rename = safe_files.os.rename

def crash_after_rename(source, destination, **kwargs):
    original_rename(source, destination, **kwargs)
    os._exit(73)

safe_files.os.rename = crash_after_rename
with safe_files.pin_regular_file(target, root) as pinned:
    assert pinned is not None
    pinned.unlink()
""",
        ],
        cwd=repository_root,
        env={**os.environ, "FCR_TEST_DELETE_ROOT": str(root), "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 73
    entries = list((root / ".fcr-delete-quarantine").iterdir())
    assert len(entries) == 1
    return entries[0]


def _create_empty_delete_quarantine(root: Path) -> Path:
    quarantine_root = root / ".fcr-delete-quarantine"
    quarantine_root.mkdir(mode=0o700)
    entry = quarantine_root / ("0" * 32)
    entry.mkdir(mode=0o700)
    return entry


def test_delete_quarantine_recovery_retains_empty_entry_without_manifest_as_benign(
    tmp_path: Path,
) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    entry = _create_empty_delete_quarantine(tmp_path)

    assert recover_delete_quarantines(tmp_path) == (0, 0)
    assert entry.is_dir()


@pytest.mark.parametrize(
    "child_kind",
    ["file", "directory", "symlink", "manifest-next", "target"],
)
def test_delete_quarantine_recovery_preserves_manifestless_entry_with_child(
    tmp_path: Path,
    child_kind: str,
) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    entry = _create_empty_delete_quarantine(tmp_path)
    if child_kind == "directory":
        child = entry / "unexpected-directory"
        child.mkdir()
    elif child_kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        child = entry / "unexpected-symlink"
        child.symlink_to(outside)
    else:
        name = {
            "file": "unexpected-file",
            "manifest-next": "manifest.next",
            "target": "target",
        }[child_kind]
        child = entry / name
        child.write_bytes(b"must-remain")

    assert recover_delete_quarantines(tmp_path) == (0, 1)
    assert entry.exists()
    assert child.is_symlink() if child_kind == "symlink" else child.exists()
    if child_kind == "symlink":
        assert outside.read_bytes() == b"outside"


def test_delete_quarantine_recovery_preserves_child_that_appears_after_empty_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files

    entry = _create_empty_delete_quarantine(tmp_path)
    entry_identity = (entry.stat().st_dev, entry.stat().st_ino)
    injected_child = entry / "concurrent-child"
    original_scandir = safe_files.os.scandir

    class InjectChildOnClose:
        def __init__(self, iterator: Iterator[os.DirEntry[str]]) -> None:
            self._iterator = iterator

        def __iter__(self) -> InjectChildOnClose:
            return self

        def __next__(self) -> os.DirEntry[str]:
            return next(self._iterator)

        def close(self) -> None:
            safe_files._close_scandir(self._iterator)
            injected_child.write_bytes(b"must-remain")

    def inject_after_entry_scan(descriptor: int) -> Iterator[os.DirEntry[str]]:
        iterator = original_scandir(descriptor)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == entry_identity:
            return InjectChildOnClose(iterator)
        return iterator

    monkeypatch.setattr(safe_files.os, "scandir", inject_after_entry_scan)

    assert safe_files.recover_delete_quarantines(tmp_path) == (0, 0)
    assert entry.exists()
    assert injected_child.read_bytes() == b"must-remain"
    monkeypatch.setattr(safe_files.os, "scandir", original_scandir)
    assert safe_files.recover_delete_quarantines(tmp_path) == (0, 1)
    assert injected_child.read_bytes() == b"must-remain"


def test_delete_quarantine_recovery_does_not_delete_when_empty_entry_name_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files

    entry = _create_empty_delete_quarantine(tmp_path)
    quarantine_root = entry.parent
    opened_identity = (entry.stat().st_dev, entry.stat().st_ino)
    displaced = quarantine_root / ("f" * 32)
    entry_identity = opened_identity
    original_scandir = safe_files.os.scandir

    class SwapNameOnClose:
        def __init__(self, iterator: Iterator[os.DirEntry[str]]) -> None:
            self._iterator = iterator

        def __iter__(self) -> SwapNameOnClose:
            return self

        def __next__(self) -> os.DirEntry[str]:
            return next(self._iterator)

        def close(self) -> None:
            safe_files._close_scandir(self._iterator)
            entry.rename(displaced)
            entry.mkdir(mode=0o700)

    def swap_after_entry_scan(descriptor: int) -> Iterator[os.DirEntry[str]]:
        iterator = original_scandir(descriptor)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == entry_identity:
            return SwapNameOnClose(iterator)
        return iterator

    monkeypatch.setattr(safe_files.os, "scandir", swap_after_entry_scan)

    assert safe_files.recover_delete_quarantines(tmp_path) == (0, 0)
    monkeypatch.setattr(safe_files.os, "scandir", original_scandir)
    assert entry.is_dir()
    assert displaced.is_dir()
    assert (displaced.stat().st_dev, displaced.stat().st_ino) == opened_identity
    assert (entry.stat().st_dev, entry.stat().st_ino) != opened_identity
    assert all(
        len(candidate.name) == 32
        and all(character in "0123456789abcdef" for character in candidate.name)
        for candidate in quarantine_root.iterdir()
    )


def test_delete_quarantine_recovery_rejects_same_size_content_replacement(tmp_path: Path) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    entry = _create_crashed_delete_quarantine(tmp_path, b"original-content")
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    quarantined_target = entry / "target"
    quarantined_target.write_bytes(b"replaced-content")
    os.utime(
        quarantined_target,
        ns=(manifest["mtime_ns"], manifest["mtime_ns"]),
        follow_symlinks=False,
    )

    assert recover_delete_quarantines(tmp_path) == (0, 1)
    assert quarantined_target.read_bytes() == b"replaced-content"


def test_delete_quarantine_recovery_rejects_large_middle_content_replacement(tmp_path: Path) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    segment_size = 64 * 1024
    original = b"a" * segment_size + b"b" * segment_size + b"c" * segment_size
    entry = _create_crashed_delete_quarantine(tmp_path, original)
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    quarantined_target = entry / "target"
    replacement = b"a" * segment_size + b"x" * segment_size + b"c" * segment_size
    quarantined_target.write_bytes(replacement)
    os.utime(
        quarantined_target,
        ns=(manifest["mtime_ns"], manifest["mtime_ns"]),
        follow_symlinks=False,
    )

    assert recover_delete_quarantines(tmp_path) == (0, 1)
    assert quarantined_target.read_bytes() == replacement


def test_delete_quarantine_recovery_skips_active_delete_lock(tmp_path: Path) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    entry = _create_crashed_delete_quarantine(tmp_path, b"locked-content")
    descriptor = os.open(entry, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        assert recover_delete_quarantines(tmp_path) == (0, 0)
        assert (entry / "target").exists()
    finally:
        os.close(descriptor)

    assert recover_delete_quarantines(tmp_path) == (1, 0)
    assert not entry.exists()


def test_delete_quarantine_recovery_skips_lock_held_by_another_process(tmp_path: Path) -> None:
    from backend.app.safe_files import recover_delete_quarantines

    entry = _create_crashed_delete_quarantine(tmp_path, b"cross-process-lock")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import fcntl
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
fcntl.flock(descriptor, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
os.close(descriptor)
""",
            str(entry),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    try:
        assert recover_delete_quarantines(tmp_path) == (0, 0)
        assert (entry / "target").exists()
    finally:
        stdout, stderr = holder.communicate("\n", timeout=5)
        assert stdout == ""
        assert stderr == ""
        assert holder.returncode == 0

    assert recover_delete_quarantines(tmp_path) == (1, 0)
    assert not entry.exists()


def test_delete_quarantine_recovery_advances_past_failed_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files

    valid_entry = _create_crashed_delete_quarantine(tmp_path, b"eventually-recovered")
    valid_name = valid_entry.name
    quarantine_root = tmp_path / safe_files.DELETE_QUARANTINE_DIRECTORY
    failed_names = [f"{index:032x}" for index in range(safe_files.DELETE_QUARANTINE_BATCH_SIZE)]
    for name in failed_names:
        failed_entry = quarantine_root / name
        failed_entry.mkdir(mode=0o700)
        (failed_entry / "unexpected-child").write_bytes(b"must-remain")

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class OrderedScan:
        def __init__(self, names: list[str]) -> None:
            self._entries = iter(Entry(name) for name in names)

        def __iter__(self) -> OrderedScan:
            return self

        def __next__(self) -> Entry:
            return next(self._entries)

        def close(self) -> None:
            return None

    scan = OrderedScan([*failed_names, valid_name])
    original_scandir = safe_files.os.scandir
    quarantine_identity = (quarantine_root.stat().st_dev, quarantine_root.stat().st_ino)

    def ordered_root_scan(descriptor: int) -> Any:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == quarantine_identity:
            return scan
        return original_scandir(descriptor)

    monkeypatch.setattr(safe_files.os, "scandir", ordered_root_scan)

    assert safe_files.recover_delete_quarantines(tmp_path) == (0, safe_files.DELETE_QUARANTINE_BATCH_SIZE)
    assert (valid_entry / "target").exists()
    assert safe_files.recover_delete_quarantines(tmp_path) == (1, 0)
    assert not valid_entry.exists()


def test_delete_quarantine_scan_cursor_survives_root_descriptor_reopen(tmp_path: Path) -> None:
    import backend.app.safe_files as safe_files

    quarantine_root = tmp_path / safe_files.DELETE_QUARANTINE_DIRECTORY
    quarantine_root.mkdir(mode=0o700)
    expected_names = {f"{index:032x}" for index in range(safe_files.DELETE_QUARANTINE_BATCH_SIZE + 1)}
    for name in expected_names:
        (quarantine_root / name).mkdir(mode=0o700)

    batches: list[list[str]] = []
    for _index in range(2):
        with safe_files.pin_managed_root(tmp_path) as (_root_path, root_fd):
            quarantine_fd = os.open(
                safe_files.DELETE_QUARANTINE_DIRECTORY,
                safe_files.DIRECTORY_FLAGS,
                dir_fd=root_fd,
            )
            try:
                batches.append(safe_files._delete_quarantine_scan_batch(quarantine_fd, quarantine_root))
            finally:
                os.close(quarantine_fd)

    assert [len(batch) for batch in batches] == [safe_files.DELETE_QUARANTINE_BATCH_SIZE, 1]
    assert set(batches[0] + batches[1]) == expected_names
