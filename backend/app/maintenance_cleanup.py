from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra import database as database_module
from backend.app.modules.final_cut_review.infra.database import SessionLocal
from backend.app.modules.final_cut_review.infra.repositories import PACKAGE_BUILD_STAGING_RE
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    FinalCutPackageSnapshotModel,
    FinalizationRecordModel,
    ReviewVersionModel,
    UploadSessionModel,
    utcnow,
)
from backend.app.safe_files import (
    PinnedRegularFile,
    UnsafeFilePathError,
    contained_path,
    ensure_private_directory,
    pin_managed_root,
    recover_delete_quarantines,
    unlink_regular_file,
)
from backend.app.settings import Settings, get_database_settings
from backend.app.upload_parts import (
    upload_identity_from_candidate_name,
    validated_upload_part_path,
)

MAX_TOMBSTONE_BYTES = 16_384
FILE_ID_RE = re.compile(r"^file_[0-9a-f]{32}$")
PACKAGE_FILE_RE = re.compile(r"^(pkg_[0-9a-f]{32})\.zip$")
UPLOAD_CLEANUP_BATCH_SIZE = 100
UPLOAD_CLEANUP_RETRY_SECONDS = 300
DIRECTORY_SCAN_BATCH_SIZE = 100
PACKAGE_CLEANUP_BATCH_SIZE = 100
PACKAGE_CLEANUP_RETRY_SECONDS = 300
MAX_UPLOAD_PARTS_PER_SESSION = 256

_directory_scan_lock = threading.Lock()
_directory_scan_states: dict[tuple[str, str], tuple[tuple[int, int], Iterator[os.DirEntry[str]]]] = {}


def _open_directory_scan(directory_fd: int) -> Iterator[os.DirEntry[str]]:
    return os.scandir(directory_fd)


def _close_directory_scan(iterator: Iterator[os.DirEntry[str]]) -> None:
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


def _directory_scan_batch(directory_fd: int, directory: Path, scan_class: str) -> list[os.DirEntry[str]]:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("maintenance scan root is not a directory")
    identity = (metadata.st_dev, metadata.st_ino)
    key = (str(directory), scan_class)
    with _directory_scan_lock:
        state = _directory_scan_states.get(key)
        if state is not None and state[0] != identity:
            _close_directory_scan(state[1])
            state = None
            _directory_scan_states.pop(key, None)
        if state is None:
            state = (identity, _open_directory_scan(directory_fd))
            _directory_scan_states[key] = state
        try:
            entries = list(islice(state[1], DIRECTORY_SCAN_BATCH_SIZE))
        except BaseException:
            _close_directory_scan(state[1])
            _directory_scan_states.pop(key, None)
            raise
        if len(entries) < DIRECTORY_SCAN_BATCH_SIZE:
            _close_directory_scan(state[1])
            _directory_scan_states.pop(key, None)
        return entries


@contextmanager
def _pin_regular_file_beneath(
    root_fd: int,
    root: Path,
    relative_path: Path,
) -> Iterator[PinnedRegularFile | None]:
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise UnsafeFilePathError("maintenance target is not a safe relative path")
    current_fd = os.dup(root_fd)
    root_device = os.fstat(root_fd).st_dev
    try:
        for part in relative_path.parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_fd)
            except FileNotFoundError:
                yield None
                return
            if os.fstat(next_fd).st_dev != root_device:
                os.close(next_fd)
                raise UnsafeFilePathError("maintenance target crosses configured filesystem")
            os.close(current_fd)
            current_fd = next_fd
        name = relative_path.parts[-1]
        try:
            metadata = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        except FileNotFoundError:
            yield None
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != root_device:
            raise UnsafeFilePathError("maintenance target is not a regular file on the configured filesystem")
        yield PinnedRegularFile(
            path=root / relative_path,
            quarantine_parent_fd=root_fd,
            parent_fd=current_fd,
            name=name,
            exists=True,
            mtime_ns=metadata.st_mtime_ns,
            size=metadata.st_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            ctime_ns=metadata.st_ctime_ns,
        )
    finally:
        os.close(current_fd)


def _pending_delete_has_database_reference(session: Session, payload: dict[str, object], target: Path) -> bool:
    file_id = payload.get("file_id")
    if isinstance(file_id, str) and file_id:
        file_refs = session.scalar(select(func.count()).select_from(FileObjectModel).where(FileObjectModel.id == file_id)) or 0
        upload_refs = session.scalar(select(func.count()).select_from(UploadSessionModel).where(UploadSessionModel.file_id == file_id)) or 0
        version_refs = session.scalar(select(func.count()).select_from(ReviewVersionModel).where(ReviewVersionModel.original_file_id == file_id)) or 0
        finalization_refs = (
            session.scalar(select(func.count()).select_from(FinalizationRecordModel).where(FinalizationRecordModel.original_file_id == file_id)) or 0
        )
        if file_refs or upload_refs or version_refs or finalization_refs:
            return True
    storage_values = {str(target)}
    raw_storage_path = payload.get("storage_path")
    if isinstance(raw_storage_path, str):
        storage_values.add(raw_storage_path)
    storage_refs = session.scalar(select(func.count()).select_from(FileObjectModel).where(FileObjectModel.storage_path.in_(storage_values))) or 0
    return storage_refs > 0


def _read_tombstone(pinned: PinnedRegularFile) -> object:
    if pinned.size is None or pinned.size > MAX_TOMBSTONE_BYTES:
        raise OSError("invalid pending-delete tombstone")
    with pinned.open_readonly() as handle:
        return json.load(handle)


def _validated_tombstone_target(payload: object, storage_root: Path) -> tuple[dict[str, object], Path]:
    if not isinstance(payload, dict):
        raise ValueError("pending-delete tombstone must be an object")
    file_id = payload.get("file_id")
    storage_path = payload.get("storage_path")
    created_at = payload.get("created_at")
    device = payload.get("device")
    inode = payload.get("inode")
    ctime_ns = payload.get("ctime_ns")
    size = payload.get("size")
    if not isinstance(file_id, str) or not FILE_ID_RE.fullmatch(file_id):
        raise ValueError("pending-delete tombstone has invalid file_id")
    if not isinstance(storage_path, str) or not storage_path:
        raise ValueError("pending-delete tombstone has invalid storage_path")
    if not isinstance(created_at, str):
        raise ValueError("pending-delete tombstone has invalid created_at")
    if (device is None) != (inode is None):
        raise ValueError("pending-delete tombstone has incomplete file identity")
    if device is not None and (not isinstance(device, int) or isinstance(device, bool) or device < 0):
        raise ValueError("pending-delete tombstone has invalid device")
    if inode is not None and (not isinstance(inode, int) or isinstance(inode, bool) or inode < 0):
        raise ValueError("pending-delete tombstone has invalid inode")
    if (ctime_ns is None) != (size is None):
        raise ValueError("pending-delete tombstone has incomplete strong file identity")
    if ctime_ns is not None and (not isinstance(ctime_ns, int) or isinstance(ctime_ns, bool) or ctime_ns < 0):
        raise ValueError("pending-delete tombstone has invalid ctime_ns")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise ValueError("pending-delete tombstone has invalid size")
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        raise ValueError("pending-delete tombstone created_at must include a timezone")
    target = contained_path(storage_path, storage_root)
    expected = contained_path(Path("files") / file_id, storage_root)
    if target != expected:
        raise ValueError("pending-delete tombstone does not match file storage contract")
    return payload, target


def _cleanup_pending_deletes(
    session: Session,
    storage_root: Path,
    referenced_tombstone_grace_before: datetime | None = None,
) -> tuple[int, int]:
    removed = 0
    failed = 0
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        with pin_managed_root(storage_root) as (resolved_storage_root, storage_fd):
            try:
                pending_fd = os.open("pending-deletes", directory_flags, dir_fd=storage_fd)
            except FileNotFoundError:
                return 0, 0
            try:
                pending_root = resolved_storage_root / "pending-deletes"
                tombstone_names = [
                    entry.name
                    for entry in _directory_scan_batch(
                        pending_fd,
                        pending_root,
                        "pending-deletes",
                    )
                ]
                for tombstone_name in tombstone_names:
                    if not tombstone_name.endswith(".json"):
                        continue
                    try:
                        with _pin_regular_file_beneath(pending_fd, pending_root, Path(tombstone_name)) as tombstone:
                            if tombstone is None:
                                continue
                            payload, target = _validated_tombstone_target(_read_tombstone(tombstone), resolved_storage_root)
                            file_id = str(payload["file_id"])
                            with _pin_regular_file_beneath(
                                storage_fd,
                                resolved_storage_root,
                                Path("files") / file_id,
                            ) as pinned:
                                if _pending_delete_has_database_reference(session, payload, target):
                                    if referenced_tombstone_grace_before is not None:
                                        created_at = datetime.fromisoformat(str(payload["created_at"]))
                                        if created_at < referenced_tombstone_grace_before and tombstone.unlink(missing_ok=True):
                                            removed += 1
                                    continue
                                if pinned is not None:
                                    expected_device = payload.get("device")
                                    expected_inode = payload.get("inode")
                                    expected_ctime_ns = payload.get("ctime_ns")
                                    expected_size = payload.get("size")
                                    if (
                                        not isinstance(expected_device, int)
                                        or not isinstance(expected_inode, int)
                                        or not isinstance(expected_ctime_ns, int)
                                        or not isinstance(expected_size, int)
                                    ):
                                        raise UnsafeFilePathError(
                                            "pending-delete tombstone lacks the original strong file identity"
                                        )
                                    if (
                                        pinned.device != expected_device
                                        or pinned.inode != expected_inode
                                        or pinned.ctime_ns != expected_ctime_ns
                                        or pinned.size != expected_size
                                    ):
                                        raise UnsafeFilePathError(
                                            "pending-delete target no longer identifies the original file"
                                        )
                                    pinned.unlink(missing_ok=True)
                                tombstone.unlink(missing_ok=True)
                    except (OSError, UnicodeError, ValueError):
                        failed += 1
                        continue
                    removed += 1
            finally:
                os.close(pending_fd)
    except (OSError, ValueError):
        return removed, failed + 1
    return removed, failed


def _claim_terminal_or_stale_uploads(session: Session, stale_before: datetime) -> list[str]:
    now = utcnow()
    retry_before = now - timedelta(seconds=UPLOAD_CLEANUP_RETRY_SECONDS)
    uploads = list(
        session.scalars(
            select(UploadSessionModel)
            .where(
                or_(
                    and_(
                        UploadSessionModel.status.in_(("initiated", "receiving")),
                        UploadSessionModel.updated_at < stale_before,
                    ),
                    and_(
                        UploadSessionModel.status == "finalizing",
                        UploadSessionModel.updated_at < stale_before,
                        UploadSessionModel.finalization_lease_expires_at <= now,
                    ),
                    and_(
                        UploadSessionModel.status == "aborted",
                        or_(
                            and_(
                                UploadSessionModel.parts_cleanup_confirmed_at.is_(None),
                                UploadSessionModel.updated_at < retry_before,
                            ),
                            and_(
                                UploadSessionModel.parts_cleanup_confirmed_at.is_not(None),
                                UploadSessionModel.updated_at < stale_before,
                            ),
                        ),
                    ),
                    and_(
                        UploadSessionModel.status == "completed",
                        UploadSessionModel.parts_cleanup_confirmed_at.is_(None),
                        UploadSessionModel.updated_at < retry_before,
                    ),
                )
            )
            .order_by(UploadSessionModel.updated_at, UploadSessionModel.id)
            .limit(UPLOAD_CLEANUP_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    )
    claimed_ids = [upload.id for upload in uploads]
    for upload in uploads:
        if upload.status in {"initiated", "receiving", "finalizing"}:
            upload.status = "aborted"
            upload.finalization_lease_id = None
            upload.finalization_lease_expires_at = None
            upload.finalization_file_id = None
            upload.finalization_idempotency_key_hash = None
            upload.finalization_request_hash = None
        upload.updated_at = now
    session.commit()
    return claimed_ids


def _validated_upload_part_path(upload: UploadSessionModel, part_number: object, part: object, upload_root: Path) -> Path:
    if not isinstance(part_number, str) or not part_number.isdigit() or int(part_number) < 1:
        raise ValueError("upload part number is invalid")
    if not isinstance(part, dict):
        raise ValueError("upload part metadata is invalid")
    path_value = part.get("path")
    size = part.get("size")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("upload part path is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("upload part size is invalid")
    return validated_upload_part_path(path_value, upload_root, upload.id, upload.temp_path, int(part_number))


def _cleanup_orphan_upload_parts(session: Session, upload_root: Path, stale_before: datetime) -> tuple[int, int]:
    cutoff_ns = int(stale_before.timestamp() * 1_000_000_000)
    removed = 0
    failed = 0
    try:
        with pin_managed_root(upload_root) as (root, root_fd):
            entries = _directory_scan_batch(root_fd, root, "orphan-upload-parts")
            for entry in entries:
                identity = upload_identity_from_candidate_name(entry.name)
                if identity is None:
                    continue
                path = root / entry.name
                was_removed = False
                try:
                    with _pin_regular_file_beneath(root_fd, root, Path(entry.name)) as pinned:
                        if pinned is None or not pinned.exists:
                            continue
                        if pinned.mtime_ns is None or pinned.mtime_ns >= cutoff_ns:
                            continue
                        upload_id, _part_number = identity
                        session.rollback()
                        upload = session.scalar(select(UploadSessionModel).where(UploadSessionModel.id == upload_id).with_for_update(skip_locked=True))
                        if upload is None:
                            upload_exists = session.scalar(select(UploadSessionModel.id).where(UploadSessionModel.id == upload_id))
                            if upload_exists is not None:
                                session.rollback()
                                continue
                        elif not isinstance(upload.received_parts, dict) or len(upload.received_parts) > MAX_UPLOAD_PARTS_PER_SESSION:
                            session.rollback()
                            continue
                        else:
                            referenced = False
                            invalid_metadata = False
                            for part_number, part in upload.received_parts.items():
                                try:
                                    target = _validated_upload_part_path(upload, part_number, part, root)
                                except (OSError, TypeError, ValueError):
                                    invalid_metadata = True
                                    break
                                if target == path:
                                    referenced = True
                                    break
                            if referenced or invalid_metadata:
                                session.rollback()
                                continue
                        was_removed = pinned.unlink(missing_ok=True)
                        session.commit()
                except OSError:
                    session.rollback()
                    failed += 1
                    continue
                if was_removed:
                    removed += 1
    except FileNotFoundError:
        return 0, 0
    except (OSError, ValueError):
        return removed, failed + 1
    return removed, failed


def _cleanup_orphan_managed_files(
    session: Session,
    root: Path,
    stale_before: datetime,
    *,
    kind: str,
) -> tuple[int, int]:
    cutoff_ns = int(stale_before.timestamp() * 1_000_000_000)
    removed = 0
    failed = 0
    try:
        with pin_managed_root(root) as (resolved_root, root_fd):
            entries = _directory_scan_batch(root_fd, resolved_root, f"orphan-managed-{kind}")
            for entry in entries:
                if kind == "file":
                    record_id = entry.name if FILE_ID_RE.fullmatch(entry.name) else None
                else:
                    match = PACKAGE_FILE_RE.fullmatch(entry.name)
                    record_id = match.group(1) if match else None
                if record_id is None:
                    continue
                try:
                    with _pin_regular_file_beneath(root_fd, resolved_root, Path(entry.name)) as pinned:
                        if pinned is None or not pinned.exists or pinned.mtime_ns is None or pinned.mtime_ns >= cutoff_ns:
                            continue
                        if kind == "file":
                            referenced = session.get(FileObjectModel, record_id) is not None
                            if not referenced:
                                referenced = (
                                    session.scalar(
                                        select(UploadSessionModel.id)
                                        .where(
                                            UploadSessionModel.status.in_(("finalizing", "completed")),
                                            or_(
                                                UploadSessionModel.finalization_file_id == record_id,
                                                UploadSessionModel.file_id == record_id,
                                            ),
                                        )
                                        .limit(1)
                                    )
                                    is not None
                                )
                        else:
                            referenced = session.get(FinalCutPackageSnapshotModel, record_id) is not None
                        if referenced:
                            continue
                        if pinned.unlink(missing_ok=True):
                            removed += 1
                except OSError:
                    failed += 1
    except (OSError, ValueError):
        return removed, failed + 1
    return removed, failed


def _cleanup_stale_package_staging(
    session: Session,
    package_root: Path,
    stale_before: datetime,
) -> tuple[int, int]:
    cutoff_ns = int(stale_before.timestamp() * 1_000_000_000)
    removed = 0
    failed = 0
    try:
        with pin_managed_root(package_root) as (resolved_package_root, package_root_fd):
            entries = _directory_scan_batch(
                package_root_fd,
                resolved_package_root,
                "stale-package-staging",
            )
            for entry in entries:
                match = PACKAGE_BUILD_STAGING_RE.fullmatch(entry.name)
                if match is None:
                    continue
                package_id, lease_id = match.groups()
                try:
                    with _pin_regular_file_beneath(
                        package_root_fd,
                        resolved_package_root,
                        Path(entry.name),
                    ) as pinned:
                        if (
                            pinned is None
                            or not pinned.exists
                            or pinned.mtime_ns is None
                            or pinned.mtime_ns >= cutoff_ns
                        ):
                            continue
                        package = session.scalar(
                            select(FinalCutPackageSnapshotModel)
                            .where(FinalCutPackageSnapshotModel.id == package_id)
                            .with_for_update()
                        )
                        if (
                            package is not None
                            and package.status == "preparing"
                            and package.build_lease_id == lease_id
                        ):
                            continue
                        if pinned.unlink(missing_ok=True):
                            removed += 1
                except OSError:
                    failed += 1
    except (OSError, ValueError):
        return removed, failed + 1
    return removed, failed


def _confirm_or_remove_cleaned_upload(session: Session, upload: UploadSessionModel) -> None:
    if upload.file_id is None:
        session.delete(upload)
        return
    confirmed_at = utcnow()
    upload.received_parts = {}
    upload.parts_cleanup_confirmed_at = confirmed_at
    upload.updated_at = confirmed_at


def _cleanup_claimed_upload_parts(session: Session, upload_ids: list[str], upload_root: Path) -> tuple[int, int]:
    removed = 0
    failed = 0
    for upload_id in upload_ids:
        session.expire_all()
        upload = session.scalar(
            select(UploadSessionModel)
            .where(
                UploadSessionModel.id == upload_id,
                UploadSessionModel.status.in_(("aborted", "completed")),
            )
            .with_for_update(skip_locked=True)
        )
        if upload is None:
            session.rollback()
            continue
        if not isinstance(upload.received_parts, dict):
            failed += 1
            session.rollback()
            continue
        if len(upload.received_parts) > MAX_UPLOAD_PARTS_PER_SESSION:
            failed += 1
            session.rollback()
            continue
        if not upload.received_parts:
            _confirm_or_remove_cleaned_upload(session, upload)
            session.commit()
            continue
        remaining_parts: dict[str, object] = {}
        for part_number, part in upload.received_parts.items():
            try:
                target = _validated_upload_part_path(upload, part_number, part, upload_root)
                was_removed = unlink_regular_file(target, upload_root)
            except (KeyError, OSError, TypeError, ValueError):
                remaining_parts[str(part_number)] = part
                failed += 1
                continue
            if was_removed:
                removed += 1
        if remaining_parts != upload.received_parts:
            if remaining_parts:
                upload.received_parts = remaining_parts
                upload.updated_at = utcnow()
            else:
                _confirm_or_remove_cleaned_upload(session, upload)
            session.commit()
        else:
            session.rollback()
    return removed, failed


def _claim_expired_packages(session: Session, now: datetime) -> list[tuple[str, str]]:
    packages = list(
        session.scalars(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.expires_at < now,
                FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
                or_(
                    FinalCutPackageSnapshotModel.download_lease_id.is_(None),
                    FinalCutPackageSnapshotModel.download_lease_expires_at <= now,
                ),
            )
            .order_by(
                FinalCutPackageSnapshotModel.updated_at,
                FinalCutPackageSnapshotModel.expires_at,
                FinalCutPackageSnapshotModel.id,
            )
            .limit(PACKAGE_CLEANUP_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    )
    claims: list[tuple[str, str]] = []
    lease_expires_at = now + timedelta(seconds=PACKAGE_CLEANUP_RETRY_SECONDS)
    for package in packages:
        claim_id = f"cleanup_{uuid.uuid4().hex}"
        package.download_lease_id = claim_id
        package.download_lease_expires_at = lease_expires_at
        package.updated_at = now
        claims.append((package.id, claim_id))
    session.commit()
    return claims


def _cleanup_claimed_expired_packages(
    session: Session,
    claims: list[tuple[str, str]],
    package_root: Path,
) -> tuple[int, int]:
    removed = 0
    failed = 0
    for package_id, claim_id in claims:
        session.expire_all()
        package = session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.download_lease_id == claim_id,
                FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        if package is None:
            session.rollback()
            continue
        try:
            with pin_managed_root(package_root) as (resolved_package_root, package_root_fd):
                package_path = contained_path(package.storage_path, resolved_package_root)
                expected_path = contained_path(Path(f"{package.id}.zip"), resolved_package_root)
                if package_path != expected_path:
                    raise ValueError("package storage path does not match package identity")
                with _pin_regular_file_beneath(
                    package_root_fd,
                    resolved_package_root,
                    Path(expected_path.name),
                ) as pinned:
                    was_removed = pinned.unlink(missing_ok=True) if pinned is not None else False
        except (OSError, ValueError):
            failed += 1
            session.rollback()
            continue
        if was_removed:
            removed += 1
        package.status = "expired"
        package.download_session_hash = None
        package.download_session_expires_at = None
        package.download_lease_id = None
        package.download_lease_expires_at = None
        package.storage_bytes = 0
        package.storage_reclaimed_at = utcnow()
        package.updated_at = package.storage_reclaimed_at
        session.commit()
    return removed, failed


def cleanup_temporary_files() -> dict[str, int]:
    settings = get_database_settings()
    with database_module.runtime_participant_lease(database_module.engine, settings):
        return _cleanup_temporary_files(settings)


def _cleanup_temporary_files(settings: Settings) -> dict[str, int]:
    upload_root = ensure_private_directory(settings.storage_root, "uploads")
    file_root = ensure_private_directory(settings.storage_root, "files")
    pending_root = ensure_private_directory(settings.storage_root, "pending-deletes")
    package_root = settings.package_root
    resolved_delete_quarantines = 0
    failed_delete_quarantines = 0
    for quarantine_parent in (
        settings.storage_root,
        upload_root,
        file_root,
        pending_root,
        package_root,
    ):
        try:
            resolved, failed = recover_delete_quarantines(quarantine_parent)
        except (OSError, ValueError):
            failed_delete_quarantines += 1
            continue
        resolved_delete_quarantines += resolved
        failed_delete_quarantines += failed
    with SessionLocal() as session:
        database_module.require_runtime_participant_session(session)
        stale_before = utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds)
        claimed_upload_ids = _claim_terminal_or_stale_uploads(session, stale_before)
        removed_upload_parts, failed_upload_parts = _cleanup_claimed_upload_parts(session, claimed_upload_ids, upload_root)
        removed_orphan_upload_parts, failed_orphan_upload_parts = _cleanup_orphan_upload_parts(session, upload_root, stale_before)
        removed_orphan_files, failed_orphan_files = _cleanup_orphan_managed_files(
            session,
            file_root,
            stale_before,
            kind="file",
        )
        removed_orphan_packages, failed_orphan_packages = _cleanup_orphan_managed_files(
            session,
            package_root,
            stale_before,
            kind="package",
        )
        removed_package_staging, failed_package_staging = _cleanup_stale_package_staging(
            session,
            package_root,
            stale_before,
        )
        package_claims = _claim_expired_packages(session, utcnow())
        removed_packages, failed_packages = _cleanup_claimed_expired_packages(session, package_claims, package_root)
        removed_pending_deletes, failed_pending_deletes = _cleanup_pending_deletes(
            session,
            settings.storage_root,
            stale_before,
        )
    return {
        "resolved_delete_quarantines": resolved_delete_quarantines,
        "failed_delete_quarantines": failed_delete_quarantines,
        "removed_upload_parts": removed_upload_parts,
        "failed_upload_parts": failed_upload_parts,
        "removed_orphan_upload_parts": removed_orphan_upload_parts,
        "failed_orphan_upload_parts": failed_orphan_upload_parts,
        "removed_orphan_files": removed_orphan_files,
        "failed_orphan_files": failed_orphan_files,
        "removed_orphan_packages": removed_orphan_packages,
        "failed_orphan_packages": failed_orphan_packages,
        "removed_package_staging": removed_package_staging,
        "failed_package_staging": failed_package_staging,
        "removed_packages": removed_packages,
        "failed_packages": failed_packages,
        "removed_pending_deletes": removed_pending_deletes,
        "failed_pending_deletes": failed_pending_deletes,
    }
