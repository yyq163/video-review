from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.safe_files import UnsafeFilePathError

from .conftest import api_data, command, create_project_item
from .test_api_contract_runtime import finalize, prepare_ready_package


def _package_id(character: str = "a") -> str:
    return "pkg_" + character * 32


def _claim_id(character: str = "b") -> str:
    return "cleanup_" + character * 32


def _private_package_root(tmp_path: Path) -> Path:
    root = tmp_path / "packages"
    root.mkdir(mode=0o700)
    return root


def _crash_in_prepared_state(
    package_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    package_id: str,
    claim_id: str,
    sha256: str,
) -> None:
    original_replace_manifest = package_files._replace_manifest

    def crash_before_moved_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic crash after package move")

    monkeypatch.setattr(package_files, "_replace_manifest", crash_before_moved_manifest)
    with pytest.raises(OSError, match="synthetic crash"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=claim_id,
            expected_sha256=sha256,
        )
    monkeypatch.setattr(package_files, "_replace_manifest", original_replace_manifest)


def _crash_in_moved_state(
    package_files: Any,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    package_id: str,
    claim_id: str,
    sha256: str,
) -> None:
    original_unlink = os.unlink

    def crash_before_target_unlink(path: str | bytes, *args: object, **kwargs: object) -> None:
        if path == package_files.PACKAGE_DELETE_TARGET and kwargs.get("dir_fd") is not None:
            raise OSError("synthetic crash after moved manifest")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(package_files.os, "unlink", crash_before_target_unlink)
    with pytest.raises(OSError, match="synthetic crash"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=claim_id,
            expected_sha256=sha256,
        )
    monkeypatch.setattr(package_files.os, "unlink", original_unlink)


def _quarantine_target(root: Path, package_id: str) -> Path:
    return root / ".fcr-package-delete-quarantine" / package_id / "target"


def _quarantine_manifest(root: Path, package_id: str) -> Path:
    return root / ".fcr-package-delete-quarantine" / package_id / "manifest.json"


def _create_legacy_package_journal(
    root: Path,
    package_id: str,
    content: bytes,
    *,
    state: str,
    entry_character: str = "d",
    move_target: bool = True,
) -> Path:
    import backend.app.safe_files as safe_files

    canonical = root / f"{package_id}.zip"
    canonical.write_bytes(content)
    before = canonical.stat()
    quarantine_root = root / safe_files.DELETE_QUARANTINE_DIRECTORY
    quarantine_root.mkdir(mode=0o700, exist_ok=True)
    entry = quarantine_root / (entry_character * 32)
    entry.mkdir(mode=0o700)
    target = entry / safe_files.DELETE_QUARANTINE_TARGET
    if move_target:
        os.rename(canonical, target)
        identity = target.stat() if state == "moved" else before
    else:
        identity = before
    manifest = {
        "schema": 3,
        "state": state,
        "created_at_ns": max(1, before.st_ctime_ns),
        "name": canonical.name,
        "device": identity.st_dev,
        "inode": identity.st_ino,
        "ctime_ns": identity.st_ctime_ns,
        "mtime_ns": identity.st_mtime_ns,
        "size": identity.st_size,
        "fingerprint_sha256": hashlib.sha256(content).hexdigest(),
    }
    (entry / safe_files.DELETE_QUARANTINE_MANIFEST).write_text(
        json.dumps(manifest, separators=(",", ":")),
        encoding="utf-8",
    )
    return entry


def test_normal_package_delete_does_not_hash_the_whole_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id()
    artifact = root / f"{package_id}.zip"
    content = b"small-package"
    artifact.write_bytes(content)

    def forbidden_pread(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("normal package deletion must not perform a full raw hash")

    monkeypatch.setattr(package_files.os, "pread", forbidden_pread)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=_claim_id(),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not artifact.exists()
    assert not (root / ".fcr-package-delete-quarantine" / package_id).exists()


def test_package_delete_manifest_writes_are_complete_under_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("9")
    content = b"short-manifest-write"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    original_write = package_files.os.write
    short_writes = 0

    def short_write(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal short_writes
        short_writes += 1
        chunk = bytes(data[: max(1, len(data) // 2)])
        return original_write(descriptor, chunk)

    monkeypatch.setattr(package_files.os, "write", short_write)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=_claim_id("9"),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert short_writes > 2
    assert not artifact.exists()


def test_package_delete_rejects_a_target_that_no_longer_matches_persisted_identity(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("1")
    content = b"persisted-package-identity"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    identity = artifact.stat()

    with pytest.raises(UnsafeFilePathError, match="captured identity"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("1"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
            expected_device=identity.st_dev,
            expected_inode=identity.st_ino + 1,
            expected_ctime_ns=identity.st_ctime_ns,
            expected_size=identity.st_size,
        )

    assert artifact.read_bytes() == content


def test_package_delete_manifest_zero_write_fails_before_moving_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("5")
    content = b"zero-manifest-write"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    original_write = package_files.os.write
    monkeypatch.setattr(package_files.os, "write", lambda *_args, **_kwargs: 0)

    with pytest.raises(OSError, match="made no progress"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("5"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert artifact.read_bytes() == content
    monkeypatch.setattr(package_files.os, "write", original_write)
    assert (
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("5"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
        == package_files.PackageDeleteOutcome.REMOVED
    )
    assert not artifact.exists()


def test_package_delete_partial_initial_manifest_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("4")
    content = b"partial-manifest-write"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    original_write = package_files.os.write
    writes = 0

    def interrupt_after_partial_write(descriptor: int, data: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return original_write(descriptor, bytes(data[: max(1, len(data) // 2)]))
        raise OSError("synthetic interrupted manifest write")

    monkeypatch.setattr(package_files.os, "write", interrupt_after_partial_write)
    with pytest.raises(OSError, match="synthetic interrupted"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("4"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert artifact.read_bytes() == content

    monkeypatch.setattr(package_files.os, "write", original_write)
    assert (
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("4"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
        == package_files.PackageDeleteOutcome.REMOVED
    )
    assert not artifact.exists()


def test_package_delete_rejects_a_recreated_entry_generation(tmp_path: Path) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    quarantine = root / package_files.PACKAGE_DELETE_QUARANTINE_DIRECTORY
    quarantine.mkdir(mode=0o700)
    package_id = _package_id("7")
    entry = quarantine / package_id
    entry.mkdir(mode=0o700)
    quarantine_fd = os.open(quarantine, package_files.DIRECTORY_FLAGS)
    stale_entry_fd = os.open(package_id, package_files.DIRECTORY_FLAGS, dir_fd=quarantine_fd)
    try:
        fcntl.flock(stale_entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.rmdir(package_id, dir_fd=quarantine_fd)
        os.mkdir(package_id, mode=0o700, dir_fd=quarantine_fd)
        with pytest.raises(UnsafeFilePathError, match="generation changed"):
            package_files._locked_entry_metadata(
                quarantine_fd,
                package_id,
                stale_entry_fd,
            )
    finally:
        os.close(stale_entry_fd)
        os.close(quarantine_fd)


def test_package_delete_lock_contention_fails_without_touching_artifact(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("8")
    content = b"locked-package"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    quarantine = root / package_files.PACKAGE_DELETE_QUARANTINE_DIRECTORY
    quarantine.mkdir(mode=0o700)
    entry = quarantine / package_id
    entry.mkdir(mode=0o700)
    entry_fd = os.open(entry, package_files.DIRECTORY_FLAGS)
    try:
        fcntl.flock(entry_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            package_files.delete_claimed_package_artifact(
                root,
                package_id=package_id,
                cleanup_claim_id=_claim_id("8"),
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )
    finally:
        os.close(entry_fd)
    assert artifact.read_bytes() == content


def test_package_delete_pre_rmdir_generation_check_preserves_replacement_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("6")
    content = b"generation-race-package"
    artifact = root / f"{package_id}.zip"
    artifact.write_bytes(content)
    quarantine = root / package_files.PACKAGE_DELETE_QUARANTINE_DIRECTORY
    quarantine.mkdir(mode=0o700)
    entry = quarantine / package_id
    entry.mkdir(mode=0o700)
    original_inode = entry.stat().st_ino
    original_check = package_files._locked_entry_metadata
    generation_checks = 0

    def replace_before_rmdir(parent_fd: int, name: str, entry_fd: int) -> os.stat_result:
        nonlocal generation_checks
        generation_checks += 1
        if generation_checks == 2:
            os.rmdir(name, dir_fd=parent_fd)
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        return original_check(parent_fd, name, entry_fd)

    monkeypatch.setattr(package_files, "_locked_entry_metadata", replace_before_rmdir)
    with pytest.raises(UnsafeFilePathError, match="generation changed"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("6"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )

    assert generation_checks == 2
    assert entry.is_dir()
    assert entry.stat().st_ino != original_inode
    assert artifact.read_bytes() == content


def test_package_delete_rejects_symlink_hardlink_and_noncanonical_identity(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")

    symlink_id = _package_id("c")
    (root / f"{symlink_id}.zip").symlink_to(outside)
    with pytest.raises(UnsafeFilePathError, match="regular file"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=symlink_id,
            cleanup_claim_id=_claim_id("c"),
            expected_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
        )
    assert outside.read_bytes() == b"outside"

    hardlink_id = _package_id("d")
    hardlink = root / f"{hardlink_id}.zip"
    hardlink.write_bytes(b"linked")
    sibling = tmp_path / "hardlink-sibling.zip"
    os.link(hardlink, sibling)
    with pytest.raises(UnsafeFilePathError, match="hard-link count"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=hardlink_id,
            cleanup_claim_id=_claim_id("d"),
            expected_sha256=hashlib.sha256(b"linked").hexdigest(),
        )
    assert hardlink.exists() and sibling.exists()

    with pytest.raises(UnsafeFilePathError, match="package identity"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id="../not-canonical",
            cleanup_claim_id=_claim_id("e"),
            expected_sha256="e" * 64,
        )


def test_prepared_crash_recovery_hashes_once_then_removes_matching_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("f")
    claim_id = _claim_id("f")
    content = b"prepared-recovery"
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    assert json.loads(_quarantine_manifest(root, package_id).read_text())["state"] == "prepared"

    original_raw_sha256 = package_files._raw_sha256
    raw_hash_calls = 0

    def count_raw_hash(descriptor: int, size: int) -> str:
        nonlocal raw_hash_calls
        raw_hash_calls += 1
        return original_raw_sha256(descriptor, size)

    monkeypatch.setattr(package_files, "_raw_sha256", count_raw_hash)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=claim_id,
        expected_sha256=sha256,
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert raw_hash_calls == 1
    assert not _quarantine_target(root, package_id).exists()


def test_prepared_recovery_preserves_same_size_sha_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("1")
    claim_id = _claim_id("1")
    content = b"original-content"
    replacement = b"replacement-data"
    assert len(replacement) == len(content)
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    manifest = json.loads(_quarantine_manifest(root, package_id).read_text())
    target = _quarantine_target(root, package_id)
    target.write_bytes(replacement)
    os.utime(
        target,
        ns=(manifest["identity"]["mtime_ns"], manifest["identity"]["mtime_ns"]),
    )

    with pytest.raises(UnsafeFilePathError, match="content identity"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=claim_id,
            expected_sha256=sha256,
        )
    assert target.read_bytes() == replacement
    assert json.loads(_quarantine_manifest(root, package_id).read_text())["state"] == "prepared"


def test_prepared_recovery_rejects_a_replaced_inode_even_with_same_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("0")
    claim_id = _claim_id("0")
    content = b"prepared-replaced-inode"
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    manifest = json.loads(_quarantine_manifest(root, package_id).read_text())
    target = _quarantine_target(root, package_id)
    replacement = root / "replacement.tmp"
    replacement.write_bytes(content)
    os.utime(
        replacement,
        ns=(manifest["identity"]["mtime_ns"], manifest["identity"]["mtime_ns"]),
    )
    os.replace(replacement, target)

    with pytest.raises(UnsafeFilePathError, match="identity changed"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=claim_id,
            expected_sha256=sha256,
        )
    assert target.exists()


def test_moved_crash_recovery_does_not_rehash_and_removes_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("2")
    claim_id = _claim_id("2")
    content = b"moved-recovery"
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_moved_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    assert json.loads(_quarantine_manifest(root, package_id).read_text())["state"] == "moved"

    def forbidden_pread(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("moved recovery must not rehash the artifact")

    monkeypatch.setattr(package_files.os, "pread", forbidden_pread)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=claim_id,
        expected_sha256=sha256,
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not _quarantine_target(root, package_id).exists()


def test_moved_recovery_removes_a_reappeared_canonical_before_reporting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("8")
    claim_id = _claim_id("8")
    content = b"moved-recovery-with-reappeared-canonical"
    sha256 = hashlib.sha256(content).hexdigest()
    canonical = root / f"{package_id}.zip"
    canonical.write_bytes(content)
    _crash_in_moved_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    canonical.write_bytes(content)

    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=claim_id,
        expected_sha256=sha256,
    )

    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not canonical.exists()
    assert not _quarantine_target(root, package_id).exists()


def test_prepared_journal_with_no_target_or_canonical_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("b")
    claim_id = _claim_id("b")
    content = b"ambiguous-prepared-delete"
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    _quarantine_target(root, package_id).unlink()

    with pytest.raises(UnsafeFilePathError, match="no durable target"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=claim_id,
            expected_sha256=sha256,
        )
    assert _quarantine_manifest(root, package_id).exists()


def test_moved_journal_with_target_already_absent_is_durable_delete_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("c")
    claim_id = _claim_id("c")
    content = b"moved-delete-already-finished"
    sha256 = hashlib.sha256(content).hexdigest()
    (root / f"{package_id}.zip").write_bytes(content)
    _crash_in_moved_state(
        package_files,
        monkeypatch,
        root,
        package_id=package_id,
        claim_id=claim_id,
        sha256=sha256,
    )
    _quarantine_target(root, package_id).unlink()

    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=claim_id,
        expected_sha256=sha256,
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not _quarantine_manifest(root, package_id).exists()


def test_legacy_package_journal_is_recovered_without_reading_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("d")
    claim_id = _claim_id("d")
    content = b"legacy-moved-package"
    entry = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="moved",
    )

    def forbidden_pread(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("legacy package recovery must not read the whole artifact")

    monkeypatch.setattr(package_files.os, "pread", forbidden_pread)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=claim_id,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )

    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not entry.exists()
    assert not (root / f"{package_id}.zip").exists()


def test_legacy_prepared_package_journal_hashes_once_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("a")
    content = b"legacy-prepared-package"
    entry = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="prepared",
    )
    original_hash = package_files._raw_sha256
    calls = 0

    def count_hash(descriptor: int, size: int) -> str:
        nonlocal calls
        calls += 1
        return original_hash(descriptor, size)

    monkeypatch.setattr(package_files, "_raw_sha256", count_hash)
    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=_claim_id("a"),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert calls == 1
    assert not entry.exists()


def test_legacy_prepared_journal_without_target_or_canonical_fails_closed(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("e")
    content = b"legacy-ambiguous-prepared"
    entry = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="prepared",
    )
    (entry / package_files.LEGACY_DELETE_TARGET).unlink()

    with pytest.raises(UnsafeFilePathError, match="no durable target"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("e"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert entry.exists()


def test_legacy_target_and_reappeared_canonical_are_both_removed(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("6")
    content = b"legacy-target-and-canonical"
    entry = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="moved",
    )
    canonical = root / f"{package_id}.zip"
    canonical.write_bytes(content)

    outcome = package_files.delete_claimed_package_artifact(
        root,
        package_id=package_id,
        cleanup_claim_id=_claim_id("6"),
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert outcome == package_files.PackageDeleteOutcome.REMOVED
    assert not entry.exists()
    assert not canonical.exists()


def test_duplicate_legacy_package_journals_fail_closed(
    tmp_path: Path,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("7")
    content = b"duplicate-legacy-journal"
    first = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="moved",
        entry_character="1",
    )
    second = first.parent / ("2" * 32)
    second.mkdir(mode=0o700)
    for child in first.iterdir():
        if child.name == package_files.LEGACY_DELETE_TARGET:
            continue
        (second / child.name).write_bytes(child.read_bytes())

    with pytest.raises(UnsafeFilePathError, match="duplicate"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("7"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert first.exists() and second.exists()


@pytest.mark.parametrize("legacy_schema", [1, 2])
def test_unsupported_matching_legacy_schema_fails_closed(
    tmp_path: Path,
    legacy_schema: int,
) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    package_id = _package_id("4")
    content = b"unsupported-legacy-schema"
    entry = _create_legacy_package_journal(
        root,
        package_id,
        content,
        state="moved",
    )
    manifest_path = entry / package_files.LEGACY_DELETE_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema"] = legacy_schema
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(UnsafeFilePathError, match="contract"):
        package_files.delete_claimed_package_artifact(
            root,
            package_id=package_id,
            cleanup_claim_id=_claim_id("4"),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert entry.exists()


def test_legacy_scan_bound_fails_closed_without_claiming_absence(tmp_path: Path) -> None:
    import backend.app.package_cleanup_files as package_files

    root = _private_package_root(tmp_path)
    legacy_root = root / package_files.LEGACY_DELETE_QUARANTINE_DIRECTORY
    legacy_root.mkdir(mode=0o700)
    for index in range(package_files.LEGACY_DELETE_SCAN_MAX_ENTRIES + 1):
        (legacy_root / f"{index:032x}").mkdir(mode=0o700)

    with pytest.raises(UnsafeFilePathError, match="scan is incomplete"):
        package_files.legacy_package_delete_journal_exists(
            root,
            package_id=_package_id("3"),
        )


def _revoked_package_for_maintenance(
    client: TestClient,
    *,
    code: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project, item = create_project_item(client, code)
    finalization = finalize(client, project["project_ref_id"], item)
    package, _ = prepare_ready_package(client, project["project_ref_id"])
    current = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    return package, finalization


def _bind_cleanup_claim(package_id: str, claim_id: str) -> str:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )

    with SessionLocal.begin() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        package.download_lease_id = claim_id
        package.download_lease_expires_at = utcnow() + timedelta(minutes=5)
        invalidation = session.scalar(select(FinalizationPackageInvalidationModel).where(FinalizationPackageInvalidationModel.package_id == package_id))
        assert invalidation is not None
        invalidation.cleanup_status = "pending"
        invalidation.last_error_code = None
        return invalidation.id


def test_maintenance_defers_prepared_package_recovery_to_package_worker(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.package_builds import process_pending_packages
    from backend.app.settings import get_database_settings

    package, finalization = _revoked_package_for_maintenance(
        client,
        code="PKGWORKERPREPARED",
    )
    settings = get_database_settings()
    canonical = settings.package_root / f"{package['id']}.zip"
    content = canonical.read_bytes()
    entry = _create_legacy_package_journal(
        settings.package_root,
        package["id"],
        content,
        state="prepared",
        entry_character="9",
    )

    def forbidden_hash(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("60-second maintenance must not hash package artifacts")

    original_hash = package_files._raw_sha256
    monkeypatch.setattr(package_files, "_raw_sha256", forbidden_hash)
    maintenance_result = cleanup_temporary_files()
    assert maintenance_result["removed_packages"] == 0
    assert maintenance_result["failed_packages"] == 0
    assert entry.exists()

    hash_calls = 0

    def count_hash(descriptor: int, size: int) -> str:
        nonlocal hash_calls
        hash_calls += 1
        return original_hash(descriptor, size)

    monkeypatch.setattr(package_files, "_raw_sha256", count_hash)
    worker_result = process_pending_packages()
    assert worker_result["removed_packages"] == 1
    assert worker_result["failed_package_cleanups"] == 0
    assert hash_calls == 1
    assert not entry.exists()
    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.scalar(
            select(FinalizationPackageInvalidationModel).where(FinalizationPackageInvalidationModel.finalization_id == finalization["id"])
        )
        assert package_row is not None and package_row.storage_reclaimed_at is not None
        assert invalidation is not None and invalidation.cleanup_status == "complete"


def test_package_cleanup_lease_heartbeat_renews_only_the_matching_claim(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra import database as database_module
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        utcnow,
    )
    from backend.app.modules.final_cut_review.infra.repositories import aware
    from backend.app.package_builds import _pulse_cleanup_lease
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGLEASEHEARTBEAT",
    )
    claim_id = _claim_id("5")
    _bind_cleanup_claim(package["id"], claim_id)
    with SessionLocal.begin() as session:
        row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert row is not None
        row.download_lease_expires_at = utcnow() + timedelta(seconds=1)

    class OnePulse:
        calls = 0

        def wait(self, _seconds: float) -> bool:
            self.calls += 1
            return self.calls > 1

    with database_module.runtime_participant_lease(
        database_module.engine,
        get_database_settings(),
    ) as runtime_binding:
        thread = threading.Thread(
            target=_pulse_cleanup_lease,
            args=(
                package["id"],
                claim_id,
                OnePulse(),
                runtime_binding,
            ),
        )
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    with SessionLocal() as session:
        row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert row is not None and row.download_lease_expires_at is not None
        assert row.download_lease_id == claim_id
        assert aware(row.download_lease_expires_at) > utcnow() + timedelta(hours=1)


def test_cleanup_worker_prepared_recovery_uses_database_sha_once_and_completes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files
    from backend.app.maintenance_cleanup import _cleanup_claimed_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGPREPAREDRECOVER",
    )
    settings = get_database_settings()
    claim_id = _claim_id("3")
    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.sha256 is not None
        database_sha256 = package_row.sha256
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        settings.package_root,
        package_id=package["id"],
        claim_id=claim_id,
        sha256=database_sha256,
    )
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)
    original_raw_sha256 = package_files._raw_sha256
    calls: list[int] = []
    concurrent_db_write_succeeded = False

    def count_hash(descriptor: int, size: int) -> str:
        nonlocal concurrent_db_write_succeeded
        calls.append(size)
        with SessionLocal.begin() as concurrent_session:
            concurrent_package = concurrent_session.get(
                FinalCutPackageSnapshotModel,
                package["id"],
            )
            assert concurrent_package is not None
            concurrent_package.download_lease_expires_at = (concurrent_package.download_lease_expires_at or utcnow()) + timedelta(seconds=1)
        concurrent_db_write_succeeded = True
        return original_raw_sha256(descriptor, size)

    monkeypatch.setattr(package_files, "_raw_sha256", count_hash)
    with SessionLocal() as session:
        assert _cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (1, 0)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None
        assert package_row.storage_reclaimed_at is not None
        assert package_row.storage_bytes == 0
        assert package_row.download_lease_id is None
        assert invalidation is not None
        assert invalidation.cleanup_status == "complete"
        assert invalidation.last_error_code is None
    assert len(calls) == 1
    assert concurrent_db_write_succeeded is True


def test_cleanup_worker_sha_mismatch_preserves_artifact_and_never_marks_complete(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files
    from backend.app.maintenance_cleanup import _cleanup_claimed_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGSHAFAILRECOVER",
    )
    settings = get_database_settings()
    claim_id = _claim_id("4")
    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.sha256 is not None
        database_sha256 = package_row.sha256
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        settings.package_root,
        package_id=package["id"],
        claim_id=claim_id,
        sha256=database_sha256,
    )
    manifest = json.loads(_quarantine_manifest(settings.package_root, package["id"]).read_text())
    target = _quarantine_target(settings.package_root, package["id"])
    original = target.read_bytes()
    replacement = bytes([original[0] ^ 0x01]) + original[1:]
    target.write_bytes(replacement)
    os.utime(
        target,
        ns=(manifest["identity"]["mtime_ns"], manifest["identity"]["mtime_ns"]),
    )
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)

    with SessionLocal() as session:
        assert _cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (0, 1)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is None
        assert invalidation is not None
        assert invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == "PACKAGE_DELETE_IDENTITY_MISMATCH"
    assert target.read_bytes() == replacement


def test_cleanup_worker_timeout_preserves_prepared_journal_and_reserved_bytes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files
    from backend.app.maintenance_cleanup import _cleanup_claimed_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGPREPAREDTIMEOUT",
    )
    settings = get_database_settings()
    claim_id = _claim_id("f")
    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.sha256 is not None
        database_sha256 = package_row.sha256
        original_storage_bytes = package_row.storage_bytes
    _crash_in_prepared_state(
        package_files,
        monkeypatch,
        settings.package_root,
        package_id=package["id"],
        claim_id=claim_id,
        sha256=database_sha256,
    )
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)

    def timeout_hash(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError("synthetic long package hash timeout")

    monkeypatch.setattr(package_files, "_raw_sha256", timeout_hash)
    with SessionLocal() as session:
        assert _cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (0, 1)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is None
        assert package_row.storage_bytes == original_storage_bytes
        assert invalidation is not None and invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == "PACKAGE_DELETE_TIMEOUT"
    assert _quarantine_manifest(settings.package_root, package["id"]).exists()
    assert _quarantine_target(settings.package_root, package["id"]).exists()


def test_maintenance_moved_recovery_completes_without_raw_hash(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.package_cleanup_files as package_files
    from backend.app.maintenance_cleanup import _cleanup_claimed_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGMOVEDRECOVER",
    )
    settings = get_database_settings()
    claim_id = _claim_id("5")
    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.sha256 is not None
        database_sha256 = package_row.sha256
    _crash_in_moved_state(
        package_files,
        monkeypatch,
        settings.package_root,
        package_id=package["id"],
        claim_id=claim_id,
        sha256=database_sha256,
    )
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)

    def forbidden_pread(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("moved recovery must not hash")

    monkeypatch.setattr(package_files.os, "pread", forbidden_pread)
    with SessionLocal() as session:
        assert _cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (1, 0)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is not None
        assert invalidation is not None and invalidation.cleanup_status == "complete"


def test_maintenance_rejects_noncanonical_package_storage_binding_before_delete(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGNONCANONICAL",
    )
    settings = get_database_settings()
    canonical_path = settings.package_root / f"{package['id']}.zip"
    assert canonical_path.exists()
    claim_id = _claim_id("7")
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_row.storage_path = str(settings.package_root / "wrong-package.zip")

    def forbidden_delete(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("noncanonical storage binding must be rejected before file deletion")

    monkeypatch.setattr(maintenance, "delete_claimed_package_artifact", forbidden_delete)
    with SessionLocal() as session:
        assert maintenance._cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (0, 1)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is None
        assert invalidation is not None
        assert invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == "PACKAGE_DELETE_IDENTITY_MISMATCH"
    assert canonical_path.exists()


def test_sha_less_package_with_legacy_journal_never_reports_reclaimed(
    client: TestClient,
) -> None:
    from backend.app.maintenance_cleanup import _cleanup_claimed_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGSHALESSLEGACY",
    )
    settings = get_database_settings()
    canonical = settings.package_root / f"{package['id']}.zip"
    content = canonical.read_bytes()
    entry = _create_legacy_package_journal(
        settings.package_root,
        package["id"],
        content,
        state="moved",
    )
    claim_id = _claim_id("2")
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_row.sha256 = None

    with SessionLocal() as session:
        assert _cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            settings.package_root,
        ) == (0, 1)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is None
        assert package_row.storage_bytes > 0
        assert invalidation is not None and invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == "PACKAGE_DELETE_IDENTITY_MISMATCH"
    assert entry.exists()


@pytest.mark.parametrize(
    ("failure", "expected_error_code"),
    [
        (TimeoutError("sensitive-timeout-detail-/private/package.zip"), "PACKAGE_DELETE_TIMEOUT"),
        (PermissionError("sensitive-permission-detail-/private/package.zip"), "PACKAGE_DELETE_PERMISSION_DENIED"),
        (UnsafeFilePathError("sensitive-identity-detail-/private/package.zip"), "PACKAGE_DELETE_IDENTITY_MISMATCH"),
        (OSError("sensitive-io-detail-/private/package.zip"), "PACKAGE_DELETE_IO_RETRY_PENDING"),
    ],
)
def test_maintenance_classifies_package_delete_failures_without_exposing_details(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected_error_code: str,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code=f"PKGERROR{expected_error_code[-8:]}",
    )
    claim_id = _claim_id("6")
    invalidation_id = _bind_cleanup_claim(package["id"], claim_id)

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(maintenance, "delete_claimed_package_artifact", fail_delete)
    with SessionLocal() as session:
        assert maintenance._cleanup_claimed_expired_packages(
            session,
            [(package["id"], claim_id)],
            get_database_settings().package_root,
        ) == (0, 1)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.storage_reclaimed_at is None
        assert invalidation is not None
        assert invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == expected_error_code
        assert str(failure) not in str(invalidation.last_error_code)


def test_stale_cleanup_failure_never_overwrites_a_newer_claim_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    package, _finalization = _revoked_package_for_maintenance(
        client,
        code="PKGSTALECLAIMFAIL",
    )
    stale_claim_id = _claim_id("9")
    newer_claim_id = _claim_id("a")
    invalidation_id = _bind_cleanup_claim(package["id"], stale_claim_id)

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("stale cleanup timed out")

    monkeypatch.setattr(maintenance, "delete_claimed_package_artifact", fail_delete)
    with SessionLocal() as session:
        original_rollback = session.rollback
        advanced_claim = False

        def rollback_then_advance_claim() -> None:
            nonlocal advanced_claim
            original_rollback()
            if advanced_claim:
                return
            advanced_claim = True
            with SessionLocal.begin() as concurrent_session:
                package_row = concurrent_session.get(
                    FinalCutPackageSnapshotModel,
                    package["id"],
                )
                invalidation = concurrent_session.get(
                    FinalizationPackageInvalidationModel,
                    invalidation_id,
                )
                assert package_row is not None and invalidation is not None
                package_row.download_lease_id = newer_claim_id
                package_row.download_lease_expires_at = utcnow() + timedelta(minutes=5)
                invalidation.cleanup_status = "pending"
                invalidation.last_error_code = None

        monkeypatch.setattr(session, "rollback", rollback_then_advance_claim)
        assert maintenance._cleanup_claimed_expired_packages(
            session,
            [(package["id"], stale_claim_id)],
            get_database_settings().package_root,
        ) == (0, 0)

    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and invalidation is not None
        assert package_row.download_lease_id == newer_claim_id
        assert invalidation.cleanup_status == "pending"
        assert invalidation.last_error_code is None
