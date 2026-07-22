#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from backend.app.safe_files import UnsafeFilePathError, contained_path, pin_regular_file

FILE_ID_PATTERN = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def verify_storage_blob(storage_root: Path, file_id: str, expected_size: int, expected_sha256: str) -> None:
    if not FILE_ID_PATTERN.fullmatch(file_id):
        raise ValueError("storage file identity is invalid")
    if expected_size < 0 or not SHA256_PATTERN.fullmatch(expected_sha256):
        raise ValueError("storage blob expectation is invalid")
    target = contained_path(Path("files") / file_id, storage_root)
    digest = hashlib.sha256()
    observed_size = 0
    try:
        with pin_regular_file(target, storage_root) as pinned:
            if pinned is None or not pinned.exists or pinned.size != expected_size:
                raise RuntimeError("storage blob size mismatch")
            with pinned.open_readonly() as handle:
                while chunk := handle.read(1024 * 1024):
                    observed_size += len(chunk)
                    digest.update(chunk)
    except UnsafeFilePathError as exc:
        raise RuntimeError("storage blob path is unsafe") from exc
    if observed_size != expected_size or digest.hexdigest() != expected_sha256:
        raise RuntimeError("storage blob digest mismatch")


def main() -> int:
    try:
        expected_size = int(os.environ.get("VERIFY_BLOB_SIZE", ""))
    except ValueError as exc:
        raise SystemExit("storage blob size is invalid") from exc
    verify_storage_blob(
        Path(os.environ.get("STORAGE_ROOT", "/data/storage")),
        os.environ.get("VERIFY_FILE_ID", ""),
        expected_size,
        os.environ.get("VERIFY_BLOB_SHA256", ""),
    )
    print("storage blob verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
