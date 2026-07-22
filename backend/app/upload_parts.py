from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path

from backend.app.safe_files import UnsafeFilePathError, contained_path, ensure_private_directory, pin_managed_root

UPLOAD_ID_RE = re.compile(r"^upl_[0-9a-f]{32}$")
UNIQUE_PART_RE = re.compile(r"^(upl_[0-9a-f]{32})\.parts\.([1-9][0-9]*)\.([0-9a-f]{32})\.part$")
LEGACY_PART_RE = re.compile(r"^(upl_[0-9a-f]{32})\.parts\.([1-9][0-9]*)$")
LEGACY_STAGING_RE = re.compile(r"^(upl_[0-9a-f]{32})\.([1-9][0-9]*)\.([0-9a-f]{32})\.part$")


class UploadPartAdmissionLease:
    def __init__(self, limiter: UploadPartAdmissionLimiter, principal_key: str, upload_id: str) -> None:
        self._limiter = limiter
        self._principal_key = principal_key
        self._upload_id = upload_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release(self._principal_key, self._upload_id)


class UploadExclusiveOperationLease:
    def __init__(self, limiter: UploadPartAdmissionLimiter, upload_id: str) -> None:
        self._limiter = limiter
        self._upload_id = upload_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release_exclusive(self._upload_id)


class UploadPartAdmissionLimiter:
    """Bound in-flight, not-yet-bound upload candidates without touching the database."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_by_principal: dict[str, int] = {}
        self._active_by_upload: dict[str, int] = {}
        self._exclusive_uploads: set[str] = set()

    def try_acquire(
        self,
        principal_key: str,
        upload_id: str,
        *,
        max_per_principal: int,
        max_per_upload: int,
        max_total: int,
    ) -> UploadPartAdmissionLease | None:
        if not principal_key or min(max_per_principal, max_per_upload, max_total) < 1:
            raise ValueError("invalid upload admission limits")
        with self._lock:
            principal_count = self._active_by_principal.get(principal_key, 0)
            upload_count = self._active_by_upload.get(upload_id, 0)
            if (
                upload_id in self._exclusive_uploads
                or
                self._active_total >= max_total
                or principal_count >= max_per_principal
                or upload_count >= max_per_upload
            ):
                return None
            self._active_total += 1
            self._active_by_principal[principal_key] = principal_count + 1
            self._active_by_upload[upload_id] = upload_count + 1
        return UploadPartAdmissionLease(self, principal_key, upload_id)

    def try_acquire_exclusive(self, upload_id: str) -> UploadExclusiveOperationLease | None:
        if not upload_id:
            raise ValueError("invalid upload identity")
        with self._lock:
            if upload_id in self._exclusive_uploads or self._active_by_upload.get(upload_id, 0) > 0:
                return None
            self._exclusive_uploads.add(upload_id)
        return UploadExclusiveOperationLease(self, upload_id)

    def _release(self, principal_key: str, upload_id: str) -> None:
        with self._lock:
            principal_count = self._active_by_principal[principal_key]
            upload_count = self._active_by_upload[upload_id]
            if principal_count == 1:
                del self._active_by_principal[principal_key]
            else:
                self._active_by_principal[principal_key] = principal_count - 1
            if upload_count == 1:
                del self._active_by_upload[upload_id]
            else:
                self._active_by_upload[upload_id] = upload_count - 1
            self._active_total -= 1

    def _release_exclusive(self, upload_id: str) -> None:
        with self._lock:
            self._exclusive_uploads.remove(upload_id)


UPLOAD_PART_ADMISSION_LIMITER = UploadPartAdmissionLimiter()


def new_upload_part_path(upload_root: Path, upload_id: str, part_no: int) -> Path:
    if not UPLOAD_ID_RE.fullmatch(upload_id) or part_no < 1:
        raise ValueError("invalid upload part identity")
    ensured_root = ensure_private_directory(upload_root.parent, upload_root.name)
    with pin_managed_root(ensured_root) as (root, _root_fd):
        return root / f"{upload_id}.parts.{part_no}.{secrets.token_hex(16)}.part"


def validated_upload_part_path(
    path_value: str | Path,
    upload_root: Path,
    upload_id: str,
    temp_path: str | Path,
    part_no: int,
) -> Path:
    if not UPLOAD_ID_RE.fullmatch(upload_id) or part_no < 1:
        raise ValueError("invalid upload part identity")
    with pin_managed_root(upload_root) as (root, _root_fd):
        pass
    target = contained_path(path_value, root)
    expected_temp = contained_path(temp_path, root)
    if expected_temp.parent != root or expected_temp.name != f"{upload_id}.parts":
        raise UnsafeFilePathError("upload session temp path violates storage contract")
    legacy_path = Path(f"{expected_temp}.{part_no}")
    match = UNIQUE_PART_RE.fullmatch(target.name)
    unique_matches = bool(
        match
        and match.group(1) == upload_id
        and int(match.group(2)) == part_no
        and target.parent == root
    )
    if target != legacy_path and not unique_matches:
        raise UnsafeFilePathError("upload part path violates storage contract")
    return target


def is_managed_upload_candidate_name(filename: str) -> bool:
    return bool(
        UNIQUE_PART_RE.fullmatch(filename)
        or LEGACY_PART_RE.fullmatch(filename)
        or LEGACY_STAGING_RE.fullmatch(filename)
    )


def upload_identity_from_candidate_name(filename: str) -> tuple[str, int] | None:
    for pattern in (UNIQUE_PART_RE, LEGACY_PART_RE, LEGACY_STAGING_RE):
        match = pattern.fullmatch(filename)
        if match:
            return match.group(1), int(match.group(2))
    return None
