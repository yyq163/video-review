from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.scripts.verify_storage_blob import verify_storage_blob


def test_verify_storage_blob_checks_pinned_size_and_digest(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    files = storage_root / "files"
    files.mkdir(parents=True)
    payload = b"backup-and-persistence-sentinel"
    target = files / "file_test_sentinel"
    target.write_bytes(payload)

    verify_storage_blob(storage_root, target.name, len(payload), hashlib.sha256(payload).hexdigest())

    target.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="size mismatch|digest mismatch"):
        verify_storage_blob(storage_root, target.name, len(payload), hashlib.sha256(payload).hexdigest())


def test_verify_storage_blob_rejects_missing_and_symlink_targets(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    files = storage_root / "files"
    files.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (files / "file_symlink").symlink_to(outside)

    with pytest.raises(RuntimeError, match="size mismatch"):
        verify_storage_blob(storage_root, "file_missing", 1, hashlib.sha256(b"x").hexdigest())
    with pytest.raises(RuntimeError, match="unsafe"):
        verify_storage_blob(storage_root, "file_symlink", len(b"outside"), hashlib.sha256(b"outside").hexdigest())
