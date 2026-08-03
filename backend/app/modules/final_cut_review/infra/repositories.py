from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, cast

from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from backend.app.modules.final_cut_review.application.context import ExecutionContext
from backend.app.modules.final_cut_review.domain import invariants
from backend.app.modules.final_cut_review.domain.errors import ReviewError, not_found
from backend.app.modules.review_contracts.generated import (
    DOMAIN_EVENT_TYPES,
    FinalizationDTO,
    OriginalMediaSnapshotDTO,
    PackageSnapshotDTO,
    PackageSnapshotItemDTO,
    ProjectDTO,
    ProjectSummaryBulkDeleteDTO,
    ProjectSummaryCurrentVersionDTO,
    ProjectSummaryDTO,
    ProjectSummaryFinalizationDTO,
    ProjectSummaryItemDTO,
    ReviewAnnotationSetDTO,
    ReviewAnnotationShape,
    ReviewIssueDTO,
    ReviewIssueRevisionDTO,
    ReviewItemDTO,
    ReviewPlaybackTarget,
    ThreadMessageDTO,
    ReviewVersionDTO,
    validate_domain_event_payload,
)
from backend.app.modules.review_access.policies import PackageDownloadTokenSigner
from backend.app.safe_files import (
    UnsafeFilePathError,
    contained_path,
    pin_managed_root,
    pin_regular_file,
    unlink_regular_file,
    unlink_regular_file_if_identity,
    write_private_file,
)
from backend.app.settings import Settings, estimate_zip_stored_storage_bytes

from .sqlalchemy_models import (
    FileObjectModel,
    FinalCutPackageSnapshotModel,
    FinalizationPackageInvalidationModel,
    FinalizationRecordModel,
    IdempotencyRecordModel,
    OperationLogModel,
    OutboxEventModel,
    ProjectRefModel,
    MediaDerivativeTaskModel,
    ReviewAnnotationSetModel,
    ReviewIssueModel,
    ReviewIssueRevisionModel,
    ReviewItemModel,
    ReviewThreadMessageModel,
    ReviewVersionModel,
    UploadSessionModel,
    utcnow,
)

POST_COMMIT_FILE_DELETIONS_KEY = "final_cut_review_post_commit_file_deletions"
POST_COMMIT_PACKAGE_DELETIONS_KEY = "final_cut_review_post_commit_package_deletions"
AUDIT_EXECUTION_IDENTITY_KEY = "final_cut_review_audit_execution_identity"
PACKAGE_QUEUE_ADVISORY_LOCK_KEY = 5064946991358883141
STORAGE_ADMISSION_ADVISORY_LOCK_KEY = int.from_bytes(b"FCRSTOR1", "big")
PACKAGE_BUILD_STAGING_RE = re.compile(
    r"^(pkg_[0-9a-f]{32})\.(build_[0-9a-f]{32})\.staging\.zip$"
)
PACKAGE_IO_SYNC_INTERVAL_BYTES = 64 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageBuildSource:
    file_id: str | None
    storage_path: str | None
    expected_hash: str | None
    expected_size: int | None
    archive_name: str | None


@dataclass(frozen=True)
class PackageBuildClaim:
    package_id: str
    project_ref_id: str
    lease_id: str
    storage_path: str
    staging_path: str
    expected_source_bytes: int
    reserved_storage_bytes: int
    sources: tuple[PackageBuildSource, ...]


@dataclass(frozen=True)
class PackageBuildArtifact:
    package_id: str
    lease_id: str
    storage_path: str
    canonical_path: str
    sha256: str
    storage_bytes: int
    device: int
    inode: int


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def storage_roots_share_filesystem(settings: Settings) -> bool:
    try:
        with (
            pin_managed_root(settings.storage_root) as (_storage_root, storage_fd),
            pin_managed_root(settings.package_root) as (_package_root, package_fd),
        ):
            return os.fstat(storage_fd).st_dev == os.fstat(package_fd).st_dev
    except (OSError, UnsafeFilePathError) as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "无法检查共享存储可用空间") from exc


def active_package_storage_reservation_bytes(session: Session) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(FinalCutPackageSnapshotModel.storage_bytes), 0)).where(
                FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None)
            )
        )
        or 0
    )


def pending_package_storage_reservation_bytes(
    session: Session,
    *,
    exclude_package_id: str | None = None,
) -> int:
    statement = select(func.coalesce(func.sum(FinalCutPackageSnapshotModel.storage_bytes), 0)).where(
        FinalCutPackageSnapshotModel.status == "preparing",
        FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
    )
    if exclude_package_id is not None:
        statement = statement.where(FinalCutPackageSnapshotModel.id != exclude_package_id)
    return int(session.scalar(statement) or 0)


def active_upload_storage_reservation_bytes(session: Session) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0)).where(
                UploadSessionModel.parts_cleanup_confirmed_at.is_(None)
            )
        )
        or 0
    )


def ensure_package_filesystem_headroom(
    session: Session,
    settings: Settings,
    *,
    package_future_reserved_bytes: int,
    requested_bytes: int,
) -> None:
    shared_filesystem = storage_roots_share_filesystem(settings)
    upload_reserved_bytes = active_upload_storage_reservation_bytes(session) if shared_filesystem else 0
    try:
        free_bytes = shutil.disk_usage(settings.package_root).free
    except OSError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "无法检查临时包存储可用空间") from exc
    low_watermark = settings.upload_storage_low_watermark_bytes if shared_filesystem else 0
    conservatively_available = (
        free_bytes
        - package_future_reserved_bytes
        - upload_reserved_bytes
        - requested_bytes
    )
    if conservatively_available < low_watermark:
        raise ReviewError("STORAGE_UNAVAILABLE", "临时包存储已达到低水位保护线")


def package_build_staging_path(package_root: Path, package_id: str, lease_id: str) -> Path:
    staging_name = f"{package_id}.{lease_id}.staging.zip"
    if PACKAGE_BUILD_STAGING_RE.fullmatch(staging_name) is None:
        raise ReviewError("STORAGE_UNAVAILABLE", "打包租约标识不符合存储合同")
    return contained_path(Path(staging_name), package_root)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def stable_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def user_agent_fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def release_file_cache(descriptor: int, length: int) -> None:
    posix_fadvise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if (
        length <= 0
        or not callable(posix_fadvise)
        or not isinstance(dontneed, int)
    ):
        return
    try:
        posix_fadvise(descriptor, 0, length, dontneed)
    except OSError:
        # Cache advice is best-effort and never part of package correctness.
        LOGGER.debug("package_io_cache_release_unavailable")


class SequentialDigestWriter:
    """Non-seekable ZIP target that hashes once and bounds dirty page cache."""

    def __init__(self, handle: Any, sync_interval_bytes: int = PACKAGE_IO_SYNC_INTERVAL_BYTES) -> None:
        self._handle = handle
        self._digest = hashlib.sha256()
        self._written = 0
        self._last_synced = 0
        self._sync_interval_bytes = sync_interval_bytes

    def write(self, data: bytes | bytearray | memoryview) -> int:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = self._handle.write(view[offset:])
            if not isinstance(written, int) or written <= 0:
                raise OSError("package output write made no progress")
            self._digest.update(view[offset : offset + written])
            offset += written
            self._written += written
        if self._written - self._last_synced >= self._sync_interval_bytes:
            self.sync()
        return len(view)

    def tell(self) -> int:
        return self._written

    def flush(self) -> None:
        self._handle.flush()

    def seek(self, *_args: object, **_kwargs: object) -> int:
        raise OSError("sequential package writer is not seekable")

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def sync(self) -> None:
        self._handle.flush()
        durable_sync = getattr(os, "fdatasync", os.fsync)
        durable_sync(self._handle.fileno())
        release_file_cache(self._handle.fileno(), self._written)
        self._last_synced = self._written

    def finalize(self) -> tuple[str, int]:
        self.sync()
        return self._digest.hexdigest(), self._written


def add_file_to_archive(
    zf: zipfile.ZipFile,
    path: Path,
    root: Path,
    arcname: str,
    expected_size: int,
) -> str:
    digest = hashlib.sha256()
    try:
        with pin_regular_file(path, root) as pinned:
            if pinned is None or not pinned.exists:
                raise ReviewError("PACKAGE_SOURCE_MISSING", "包源文件缺失")
            if pinned.size != expected_size:
                raise ReviewError("FILE_HASH_MISMATCH", "定稿原片大小不匹配")
            with pinned.open_readonly() as handle, zf.open(arcname, "w", force_zip64=True) as archive_handle:
                bytes_read = 0
                last_cache_release = 0
                while bytes_read < expected_size:
                    chunk = handle.read(min(1024 * 1024, expected_size - bytes_read))
                    if not chunk:
                        raise ReviewError("FILE_HASH_MISMATCH", "定稿原片大小不匹配")
                    bytes_read += len(chunk)
                    digest.update(chunk)
                    archive_handle.write(chunk)
                    if bytes_read - last_cache_release >= PACKAGE_IO_SYNC_INTERVAL_BYTES:
                        release_file_cache(handle.fileno(), bytes_read)
                        last_cache_release = bytes_read
                release_file_cache(handle.fileno(), bytes_read)
                if handle.read(1):
                    raise ReviewError("FILE_HASH_MISMATCH", "定稿原片大小不匹配")
                final_metadata = os.fstat(handle.fileno())
                if (
                    bytes_read != expected_size
                    or final_metadata.st_size != expected_size
                    or final_metadata.st_mtime_ns != pinned.mtime_ns
                ):
                    raise ReviewError("FILE_HASH_MISMATCH", "定稿原片大小不匹配")
    except UnsafeFilePathError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "包源文件路径非法") from exc
    return digest.hexdigest()


def regular_file_sha256(path: Path, root: Path) -> str:
    digest = hashlib.sha256()
    try:
        with pin_regular_file(path, root) as pinned:
            if pinned is None or not pinned.exists:
                raise ReviewError("PACKAGE_SOURCE_MISSING", "定稿源文件缺失")
            with pinned.open_readonly() as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
    except UnsafeFilePathError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "定稿源文件路径非法") from exc
    return digest.hexdigest()


def estimate_package_storage_bytes(total_bytes: int, archive_names: list[str]) -> int:
    """Reserve a conservative ZIP_STORED upper bound before the worker runs."""
    encoded_name_bytes = sum(len(name.encode("utf-8")) for name in archive_names)
    return estimate_zip_stored_storage_bytes(
        total_bytes,
        len(archive_names),
        encoded_name_bytes,
    )


def validate_package_limits(
    finalizations: list[FinalizationRecordModel],
    settings: Settings,
) -> int:
    """Validate active finalization metadata before any archive path is created."""
    if len(finalizations) > settings.max_package_files:
        raise ReviewError("FILE_TOO_LARGE", "项目定稿原片数量超出打包限制")
    total_bytes = sum(finalization.file_size for finalization in finalizations)
    if total_bytes > settings.max_package_bytes:
        raise ReviewError("FILE_TOO_LARGE", "项目定稿原片总大小超出打包限制")
    return total_bytes


MANAGED_FILE_ID_PATTERN = re.compile(r"(?:file|media)_[0-9a-f]{32}")


def contained_storage_path(path_value: str, root: Path, file_id: str | None = None) -> Path:
    try:
        path = contained_path(path_value, root)
    except UnsafeFilePathError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "文件路径越界") from exc
    if file_id is not None:
        if MANAGED_FILE_ID_PATTERN.fullmatch(file_id) is None:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件标识非法")
        expected = contained_path(Path("files") / file_id, root)
        if path != expected:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件路径不符合存储合同")
    return path


def safe_archive_component(filename: str) -> str:
    basename = Path(filename.replace("\\", "/")).name.strip()
    return basename or "original.bin"


def _media_from_version(version: ReviewVersionModel) -> OriginalMediaSnapshotDTO:
    return OriginalMediaSnapshotDTO(
        original_file_id=version.original_file_id,
        original_filename=version.original_filename,
        mime_type=version.mime_type,
        file_size=version.file_size,
        sha256=version.sha256,
        duration_ms=version.duration_ms,
        width=version.width,
        height=version.height,
        fps_num=version.fps_num,
        fps_den=version.fps_den,
        media_probe_version=version.media_probe_version,
    )


def _media_from_file(file: FileObjectModel) -> OriginalMediaSnapshotDTO:
    return OriginalMediaSnapshotDTO(
        original_file_id=file.id,
        original_filename=file.original_filename,
        mime_type=file.mime_type,
        file_size=file.file_size,
        sha256=file.sha256,
        duration_ms=file.duration_ms,
        width=file.width,
        height=file.height,
        fps_num=file.fps_num,
        fps_den=file.fps_den,
        media_probe_version=file.media_probe_version,
    )


class SqlAlchemyReviewRepository:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _post_commit_file_deletions(
        self,
    ) -> list[tuple[Path, Path, int | None, int | None, int | None, int | None]]:
        pending = self.session.info.setdefault(POST_COMMIT_FILE_DELETIONS_KEY, [])
        return pending

    def _post_commit_package_deletions(self) -> list[tuple[str, Path, int, int]]:
        pending = self.session.info.setdefault(POST_COMMIT_PACKAGE_DELETIONS_KEY, [])
        return pending

    def run_post_commit_file_deletions(self) -> None:
        pending = list(self.session.info.pop(POST_COMMIT_FILE_DELETIONS_KEY, []))
        for storage_path, tombstone, device, inode, ctime_ns, size in pending:
            try:
                if device is None or inode is None:
                    with pin_regular_file(storage_path, self.settings.storage_root) as pinned:
                        if pinned is not None and pinned.exists:
                            raise UnsafeFilePathError("pending delete target appeared after its identity was captured")
                else:
                    unlink_regular_file_if_identity(
                        storage_path,
                        self.settings.storage_root,
                        device=device,
                        inode=inode,
                        ctime_ns=ctime_ns,
                        size=size,
                    )
            except OSError:
                LOGGER.warning("post_commit_file_delete_pending", extra={"tombstone": tombstone.name})
                continue
            try:
                unlink_regular_file(tombstone, self.settings.storage_root)
            except OSError:
                LOGGER.warning("post_commit_file_delete_tombstone_retained", extra={"tombstone": tombstone.name})

        reclaimed_package_ids: list[str] = []
        package_pending = list(self.session.info.pop(POST_COMMIT_PACKAGE_DELETIONS_KEY, []))
        for package_id, package_path, device, inode in package_pending:
            try:
                unlink_regular_file_if_identity(
                    package_path,
                    self.settings.package_root,
                    device=device,
                    inode=inode,
                )
            except OSError:
                LOGGER.warning("post_commit_package_delete_pending", extra={"package_id": package_id})
                continue
            reclaimed_package_ids.append(package_id)
        if reclaimed_package_ids:
            now = utcnow()
            try:
                self.session.execute(
                    update(FinalCutPackageSnapshotModel)
                    .where(
                        FinalCutPackageSnapshotModel.id.in_(reclaimed_package_ids),
                        FinalCutPackageSnapshotModel.status == "failed",
                        FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
                    )
                    .values(storage_bytes=0, storage_reclaimed_at=now, updated_at=now)
                )
                self.session.commit()
            except Exception:
                self.session.rollback()
                LOGGER.warning(
                    "post_commit_package_reclaim_accounting_pending",
                    extra={"package_count": len(reclaimed_package_ids)},
                )

    def discard_post_commit_file_deletions(self) -> None:
        pending = self.session.info.pop(POST_COMMIT_FILE_DELETIONS_KEY, [])
        self.session.info.pop(POST_COMMIT_PACKAGE_DELETIONS_KEY, None)
        for _storage_path, tombstone, _device, _inode, _ctime_ns, _size in pending:
            try:
                unlink_regular_file(tombstone, self.settings.storage_root)
            except OSError:
                LOGGER.warning("post_commit_file_delete_tombstone_discard_failed", extra={"tombstone": tombstone.name})

    def preserve_post_commit_file_deletions(self) -> None:
        pending = self.session.info.pop(POST_COMMIT_FILE_DELETIONS_KEY, [])
        if pending:
            LOGGER.warning("post_commit_file_delete_outcome_unknown", extra={"pending_count": len(pending)})
        package_pending = self.session.info.pop(POST_COMMIT_PACKAGE_DELETIONS_KEY, [])
        if package_pending:
            LOGGER.warning("post_commit_package_delete_outcome_unknown", extra={"pending_count": len(package_pending)})

    def _queue_post_commit_file_deletion(self, storage_path: Path, file_id: str) -> None:
        tombstone, device, inode, ctime_ns, size = self._write_pending_file_delete(
            storage_path,
            file_id,
        )
        self._post_commit_file_deletions().append(
            (storage_path, tombstone, device, inode, ctime_ns, size)
        )

    def _queue_post_commit_package_deletion(
        self,
        package_id: str,
        package_path: Path,
        *,
        device: int,
        inode: int,
    ) -> None:
        self._post_commit_package_deletions().append((package_id, package_path, device, inode))

    def _write_pending_file_delete(
        self,
        storage_path: Path,
        file_id: str,
    ) -> tuple[Path, int | None, int | None, int | None, int | None]:
        try:
            with pin_regular_file(storage_path, self.settings.storage_root) as pinned:
                device = pinned.device if pinned is not None and pinned.exists else None
                inode = pinned.inode if pinned is not None and pinned.exists else None
                ctime_ns = pinned.ctime_ns if pinned is not None and pinned.exists else None
                size = pinned.size if pinned is not None and pinned.exists else None
        except UnsafeFilePathError:
            device = None
            inode = None
            ctime_ns = None
            size = None
        content = json.dumps(
            {
                "file_id": file_id,
                "storage_path": str(storage_path),
                "created_at": iso(utcnow()),
                "device": device,
                "inode": inode,
                "ctime_ns": ctime_ns,
                "size": size,
            },
            separators=(",", ":"),
        ).encode()
        tombstone = write_private_file(
            self.settings.storage_root,
            "pending-deletes",
            f"{uuid.uuid4().hex}.json",
            content,
        )
        return tombstone, device, inode, ctime_ns, size

    def get_idempotency(self, key: str) -> tuple[str, dict[str, Any]] | None:
        record = self.session.get(IdempotencyRecordModel, key)
        if not record:
            return None
        return record.request_hash, record.response_json

    def reserve_idempotency(self, key: str, command_type: str, request_hash: str, principal_hash: str) -> dict[str, Any] | None:
        existing = self.session.get(IdempotencyRecordModel, key)
        if existing:
            return self._idempotency_replay(existing, command_type, request_hash, principal_hash)

        values = {
            "key": key,
            "command_type": command_type,
            "request_hash": request_hash,
            "principal_hash": principal_hash,
            "response_json": {},
            "status_code": 102,
        }
        if self.session.get_bind().dialect.name == "postgresql":
            inserted_key = self.session.execute(
                postgresql_insert(IdempotencyRecordModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[IdempotencyRecordModel.key])
                .returning(IdempotencyRecordModel.key)
            ).scalar_one_or_none()
            if inserted_key is not None:
                return None
        else:
            try:
                with self.session.begin_nested():
                    self.session.add(IdempotencyRecordModel(**values))
                    self.session.flush()
                return None
            except IntegrityError:
                pass

        self.session.expire_all()
        existing = self.session.get(IdempotencyRecordModel, key, populate_existing=True)
        if not existing:
            raise ReviewError("IDEMPOTENCY_CONFLICT", "Idempotency reservation disappeared")
        return self._idempotency_replay(existing, command_type, request_hash, principal_hash)

    def _idempotency_replay(
        self,
        existing: IdempotencyRecordModel,
        command_type: str,
        request_hash: str,
        principal_hash: str,
    ) -> dict[str, Any] | None:
        if existing.principal_hash != principal_hash:
            raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "Idempotency-Key 不属于当前主体")
        if existing.command_type != command_type or existing.request_hash != request_hash:
            raise ReviewError("IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 对应不同请求体")
        if existing.status_code == 200:
            if command_type == "PrepareFinalizedPackage":
                package_id = existing.response_json.get("id")
                project_ref_id = existing.response_json.get("project_ref_id")
                if not isinstance(package_id, str) or not isinstance(project_ref_id, str):
                    raise ReviewError("IDEMPOTENCY_CONFLICT", "项目包幂等记录不完整")
                self._assert_project_visible(self._get_project(project_ref_id))
                package = self.session.scalar(
                    select(FinalCutPackageSnapshotModel).where(
                        FinalCutPackageSnapshotModel.id == package_id,
                        FinalCutPackageSnapshotModel.project_ref_id == project_ref_id,
                    )
                )
                if package is None:
                    raise not_found()
                if aware(package.expires_at) < utcnow():
                    raise ReviewError("PACKAGE_EXPIRED", "临时包已过期")
                if package.status == "ready":
                    integrity_error, _expected_path, _actual_size, _identity = self._ready_package_reuse_integrity(package)
                    if integrity_error is not None:
                        existing.response_json = {}
                        existing.status_code = 102
                        self.session.flush()
                        return None
                return self.package_dto(package)
            return existing.response_json
        raise ReviewError("IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 请求仍在处理")

    def complete_idempotency(self, key: str, response: dict[str, Any]) -> None:
        record = self.session.get(IdempotencyRecordModel, key)
        if not record:
            raise ReviewError("IDEMPOTENCY_CONFLICT", "Idempotency reservation missing")
        persisted_response = dict(response)
        if record.command_type == "PrepareFinalizedPackage":
            persisted_response.pop("download_token", None)
            persisted_response.pop("download_token_expires_at", None)
        record.response_json = persisted_response
        record.status_code = 200
        self.session.flush()

    def execute(self, command_type: str, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        handlers = {
            "CreateProject": self.create_project,
            "UpdateProject": self.update_project,
            "ArchiveProject": self.archive_project,
            "RestoreProject": self.restore_project,
            "SoftDeleteProject": self.soft_delete_project,
            "CreateReviewItem": self.create_review_item,
            "UpdateReviewItem": self.update_review_item,
            "DeleteReviewItem": self.delete_review_item,
            "UploadReviewVersion": self.upload_review_version,
            "StartReview": self.start_review,
            "CreateReviewIssue": self.create_review_issue,
            "UpdateReviewIssue": self.update_review_issue,
            "AddReviewMessage": self.add_review_message,
            "ResolveReviewIssue": self.resolve_issue,
            "ReopenReviewIssue": self.reopen_issue,
            "SoftDeleteReviewIssue": self.soft_delete_issue,
            "FinalizeVersion": self.finalize_version,
            "RevokeFinalization": self.revoke_finalization,
            "PrepareFinalizedPackage": self.prepare_package,
        }
        self.session.info[AUDIT_EXECUTION_IDENTITY_KEY] = stable_hash(
            {
                "command_type": command_type,
                "payload": payload,
                "expected_version": expected_version,
                "principal_kind": context.principal.kind,
                "principal_id": context.principal.id,
            }
        )
        try:
            result = handlers[command_type](payload, context, expected_version)
            self.session.flush()
            return result
        except KeyError as exc:
            raise ReviewError("VALIDATION_ERROR", f"未知命令 {command_type}") from exc
        except StaleDataError as exc:
            raise ReviewError("OPTIMISTIC_LOCK_CONFLICT", "乐观锁冲突") from exc
        except IntegrityError as exc:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "数据库约束拒绝该操作") from exc

    def _operation_identity_hash(
        self,
        context: ExecutionContext,
        *,
        command_type: str,
        idempotency_key: str | None,
        resource_type: str,
        resource_id: str | None,
    ) -> str:
        command_identity: object
        if idempotency_key is not None:
            command_identity = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        else:
            command_identity = self.session.info.get(AUDIT_EXECUTION_IDENTITY_KEY)
            if not isinstance(command_identity, str) or len(command_identity) != 64:
                command_identity = hashlib.sha256(context.request_id.encode("utf-8")).hexdigest()
        return stable_hash(
            {
                "command_identity": command_identity,
                "command_type": command_type,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "principal_kind": context.principal.kind,
                "principal_id": context.principal.id,
            }
        )

    def _lock_operation_outcome_identity(self, operation_identity_hash: str) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        lock_key = int.from_bytes(
            bytes.fromhex(operation_identity_hash)[:8],
            byteorder="big",
            signed=True,
        )
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    def add_operation_log(
        self,
        context: ExecutionContext,
        capability: str | None,
        result: str,
        error_code: str | None = None,
        *,
        command_type: str,
        idempotency_key: str | None = None,
        resource_type: str = "request",
        resource_id: str | None = None,
        failure_stage: str | None = None,
        operation_identity_hash: str | None = None,
    ) -> None:
        def bounded(value: str | None, limit: int) -> str | None:
            if value is None:
                return None
            cleaned = "".join(character if character.isprintable() else "?" for character in str(value))
            return cleaned[:limit]

        entry_source = context.entry_source if context.entry_source in {"edit", "review", "embedded", "unspecified"} else "unspecified"
        resolved_operation_identity_hash = operation_identity_hash or self._operation_identity_hash(
            context,
            command_type=command_type,
            idempotency_key=idempotency_key,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        if result == "ok":
            self._lock_operation_outcome_identity(resolved_operation_identity_hash)
        self.session.add(
            OperationLogModel(
                request_id=bounded(context.request_id, 64) or "unknown",
                entry_source=entry_source,
                command_type=bounded(command_type, 128) or "UnknownCommand",
                capability=bounded(capability, 128),
                principal_kind=bounded(context.principal.kind, 32) or "anonymous",
                principal_id=bounded(context.principal.id, 128),
                client_ip=bounded(context.remote_addr, 64),
                user_agent=user_agent_fingerprint(context.user_agent),
                idempotency_key_hash=(hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest() if idempotency_key is not None else None),
                operation_identity_hash=resolved_operation_identity_hash,
                resource_type=bounded(resource_type, 32) or "request",
                resource_id=bounded(resource_id, 128),
                result=result,
                error_code=bounded(error_code, 64),
                failure_stage=bounded(failure_stage, 32),
            )
        )

    def persist_failed_operation_log(
        self,
        context: ExecutionContext,
        capability: str | None,
        error_code: str,
        *,
        command_type: str,
        idempotency_key: str | None,
        resource_type: str,
        resource_id: str | None,
        failure_stage: str,
    ) -> bool:
        try:
            operation_identity_hash = self._operation_identity_hash(
                context,
                command_type=command_type,
                idempotency_key=idempotency_key,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            with Session(bind=self.session.get_bind(), expire_on_commit=False) as audit_session:
                audit_repo = SqlAlchemyReviewRepository(audit_session, self.settings)
                audit_repo.add_operation_log(
                    context,
                    capability,
                    "error",
                    error_code,
                    command_type=command_type,
                    idempotency_key=idempotency_key,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    failure_stage=failure_stage,
                    operation_identity_hash=operation_identity_hash,
                )
                audit_session.commit()
            return True
        except Exception as exc:
            LOGGER.warning(
                "operation_audit_write_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return False

    def persist_uncertain_operation_log(
        self,
        context: ExecutionContext,
        capability: str | None,
        *,
        command_type: str,
        idempotency_key: str | None,
        resource_type: str,
        resource_id: str | None,
        failure_stage: str,
    ) -> bool:
        """Record an unresolved commit only when no committed outcome is visible."""
        try:
            operation_identity_hash = self._operation_identity_hash(
                context,
                command_type=command_type,
                idempotency_key=idempotency_key,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            with Session(bind=self.session.get_bind(), expire_on_commit=False) as audit_session:
                audit_repo = SqlAlchemyReviewRepository(audit_session, self.settings)
                audit_repo._lock_operation_outcome_identity(operation_identity_hash)
                existing = audit_session.scalar(
                    select(OperationLogModel.id)
                    .where(
                        OperationLogModel.operation_identity_hash == operation_identity_hash,
                        OperationLogModel.result == "ok",
                    )
                    .order_by(OperationLogModel.id)
                    .limit(1)
                )
                if existing is not None:
                    return False
                audit_repo.add_operation_log(
                    context,
                    capability,
                    "unknown",
                    "COMMIT_OUTCOME_UNKNOWN",
                    command_type=command_type,
                    idempotency_key=idempotency_key,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    failure_stage=failure_stage,
                    operation_identity_hash=operation_identity_hash,
                )
                audit_session.commit()
            return True
        except IntegrityError:
            return False
        except Exception as exc:
            LOGGER.warning(
                "operation_audit_uncertain_write_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return False

    def _event(
        self,
        event_type: str,
        context: ExecutionContext,
        project_ref_id: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int = 1,
        review_item_id: str | None = None,
        version_id: str | None = None,
        issue_id: str | None = None,
        finalization_id: str | None = None,
        package_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if event_type not in DOMAIN_EVENT_TYPES:
            raise ValueError(f"Unknown domain event type: {event_type}")
        event_payload = payload or {}
        validate_domain_event_payload(event_type, event_payload)
        max_persisted_sequence = self._next_outbox_sequence_base()
        max_pending_sequence = max((event.sequence for event in self.session.new if isinstance(event, OutboxEventModel)), default=0)
        sequence = max(max_persisted_sequence, max_pending_sequence) + 1
        self.session.add(
            OutboxEventModel(
                event_id=new_id("evt"),
                event_type=event_type,
                event_version=1,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                aggregate_version=aggregate_version,
                sequence=sequence,
                project_ref_id=project_ref_id,
                review_item_id=review_item_id,
                version_id=version_id,
                issue_id=issue_id,
                finalization_id=finalization_id,
                package_id=package_id,
                correlation_id=context.request_id,
                causation_id=None,
                metadata_json={
                    "entry_source": context.entry_source,
                    "principal_kind": context.principal.kind,
                    "principal_id": context.principal.id,
                    "request_id": context.request_id,
                },
                payload=event_payload,
            )
        )

    def _next_outbox_sequence_base(self) -> int:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(text("LOCK TABLE outbox_events IN EXCLUSIVE MODE"))
        return self.session.scalar(select(func.max(OutboxEventModel.sequence))) or 0

    def project_dto(self, project: ProjectRefModel) -> dict[str, Any]:
        item_count = self.session.scalar(select(func.count()).select_from(ReviewItemModel).where(ReviewItemModel.project_ref_id == project.id)) or 0
        finalized_count = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewItemModel)
                .where(ReviewItemModel.project_ref_id == project.id, ReviewItemModel.workflow_status == "finalized")
            )
            or 0
        )
        return self._project_dto(project, item_count=int(item_count), finalized_count=int(finalized_count))

    def _project_dto(
        self,
        project: ProjectRefModel,
        *,
        item_count: int,
        finalized_count: int,
    ) -> dict[str, Any]:
        completion = "empty" if item_count == 0 else "completed" if finalized_count == item_count else "in_progress"
        return ProjectDTO(
            project_ref_id=project.id,
            project_code=project.project_code,
            project_name=project.project_name,
            description=project.description,
            source=project.source,  # type: ignore[arg-type]
            external_project_id=project.external_project_id,
            lifecycle_status=project.lifecycle_status,  # type: ignore[arg-type]
            completion_status=completion,  # type: ignore[arg-type]
            deleted_at=iso(project.deleted_at) if project.deleted_at else None,
            lock_version=project.lock_version,
            created_at=iso(project.created_at),
            updated_at=iso(project.updated_at),
        ).model_dump(mode="json")

    def version_dto(self, version: ReviewVersionModel) -> dict[str, Any]:
        derivative_tasks = {
            task.kind: task
            for task in self.session.scalars(
                select(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.project_ref_id == version.project_ref_id,
                    MediaDerivativeTaskModel.review_item_id == version.review_item_id,
                    MediaDerivativeTaskModel.version_id == version.id,
                )
            ).all()
        }
        asset_ids = {
            asset_id
            for asset_id in (version.playback_asset_id, version.thumbnail_asset_id)
            if asset_id
        }
        files = {
            file.id: file
            for file in self.session.scalars(
                select(FileObjectModel).where(FileObjectModel.id.in_(asset_ids))
            ).all()
        } if asset_ids else {}
        return self._version_dto(
            version,
            playback_status=self._version_derivative_status(
                version,
                "playback_faststart",
                derivative_tasks.get("playback_faststart"),
                files.get(version.playback_asset_id or ""),
            ),
            thumbnail_status=self._version_derivative_status(
                version,
                "thumbnail",
                derivative_tasks.get("thumbnail"),
                files.get(version.thumbnail_asset_id or ""),
            ),
        )

    def _version_dto(
        self,
        version: ReviewVersionModel,
        *,
        playback_status: str,
        thumbnail_status: str,
    ) -> dict[str, Any]:
        return ReviewVersionDTO(
            id=version.id,
            project_ref_id=version.project_ref_id,
            review_item_id=version.review_item_id,
            previous_version_id=version.previous_version_id,
            version_no=version.version_no,
            version_label=version.version_label,
            is_current=version.is_current,
            original_media=_media_from_version(version),
            playback_status=playback_status,  # type: ignore[arg-type]
            playback_asset_id=version.playback_asset_id,
            thumbnail_status=thumbnail_status,  # type: ignore[arg-type]
            thumbnail_asset_id=version.thumbnail_asset_id,
            version_note=version.version_note,
            change_summary=version.change_summary,
            lock_version=version.lock_version,
            created_at=iso(version.created_at),
        ).model_dump(mode="json")

    def _version_derivative_status(
        self,
        version: ReviewVersionModel,
        kind: str,
        task: MediaDerivativeTaskModel | None,
        file: FileObjectModel | None,
    ) -> str:
        asset_id = (
            version.playback_asset_id
            if kind == "playback_faststart"
            else version.thumbnail_asset_id
        )
        if task is not None:
            if task.status in {"queued", "running"}:
                return "processing"
            if (
                task.status != "ready"
                or task.output_file_id is None
                or task.output_file_id != asset_id
            ):
                return "failed"
        if asset_id is None or file is None or file.id != asset_id:
            return "failed"
        return "ready" if self._playback_file_is_ready(file) else "failed"

    def _playback_status(self, version: ReviewVersionModel) -> str:
        playback_file = self.session.get(FileObjectModel, version.playback_asset_id) if version.playback_asset_id else None
        return self._playback_file_status(playback_file)

    def _playback_file_status(self, playback_file: FileObjectModel | None) -> str:
        return "ready" if self._playback_file_is_ready(playback_file) else "failed"

    def _playback_asset_is_ready(self, playback_asset_id: str | None) -> bool:
        if not playback_asset_id:
            return False
        return self._playback_file_is_ready(self.session.get(FileObjectModel, playback_asset_id))

    def _playback_file_is_ready(self, file: FileObjectModel | None) -> bool:
        if file is None:
            return False
        try:
            storage_path = contained_storage_path(file.storage_path, self.settings.storage_root, file.id)
            with pin_regular_file(storage_path, self.settings.storage_root) as pinned:
                if pinned is None or not pinned.exists:
                    return False
                with pinned.open_readonly():
                    return True
        except (OSError, ReviewError):
            return False

    def item_dto(self, item: ReviewItemModel) -> dict[str, Any]:
        current = self._get_version(item.project_ref_id, item.id, item.current_version_id or "")
        unresolved = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewIssueModel)
                .where(
                    ReviewIssueModel.project_ref_id == item.project_ref_id,
                    ReviewIssueModel.review_item_id == item.id,
                    ReviewIssueModel.version_id == item.current_version_id,
                    ReviewIssueModel.status == "unresolved",
                    ReviewIssueModel.deleted_at.is_(None),
                )
            )
            or 0
        )
        resolved = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewIssueModel)
                .where(
                    ReviewIssueModel.project_ref_id == item.project_ref_id,
                    ReviewIssueModel.review_item_id == item.id,
                    ReviewIssueModel.version_id == item.current_version_id,
                    ReviewIssueModel.status == "resolved",
                    ReviewIssueModel.deleted_at.is_(None),
                )
            )
            or 0
        )
        history = self.session.scalar(select(func.count()).select_from(ReviewVersionModel).where(ReviewVersionModel.review_item_id == item.id)) or 0
        return self._item_dto(
            item,
            current=current,
            unresolved=int(unresolved),
            resolved=int(resolved),
            history=int(history),
        )

    def _item_dto(
        self,
        item: ReviewItemModel,
        *,
        current: ReviewVersionModel,
        unresolved: int,
        resolved: int,
        history: int,
    ) -> dict[str, Any]:
        ui_status = item.workflow_status
        if item.workflow_status == "pending_review" and current.version_no > 1:
            ui_status = "pending_re_review"
        return ReviewItemDTO(
            id=item.id,
            project_ref_id=item.project_ref_id,
            item_code=item.item_code,
            episode_no=item.episode_no,
            title=item.title,
            workflow_status=item.workflow_status,  # type: ignore[arg-type]
            current_version_id=item.current_version_id or "",
            current_version_no=current.version_no,
            ui_status=ui_status,
            active_finalization_id=item.active_finalization_id,
            unresolved_current_version_count=unresolved,
            resolved_current_version_count=resolved,
            historical_version_count=max(0, history - 1),
            is_finalized=item.workflow_status == "finalized",
            lock_version=item.lock_version,
            created_at=iso(item.created_at),
            updated_at=iso(item.updated_at),
        ).model_dump(mode="json")

    def issue_dto(self, issue: ReviewIssueModel) -> dict[str, Any]:
        revision = self._get_revision(issue.id, issue.current_revision_id or "")
        annotation = self._get_annotation(issue.project_ref_id, issue.review_item_id, issue.version_id, issue.id, revision.annotation_set_id)
        return self._issue_dto(issue, revision=revision, annotation=annotation)

    def _issue_dto(
        self,
        issue: ReviewIssueModel,
        *,
        revision: ReviewIssueRevisionModel,
        annotation: ReviewAnnotationSetModel | None,
    ) -> dict[str, Any]:
        revision_dto = ReviewIssueRevisionDTO(
            id=revision.id,
            project_ref_id=revision.project_ref_id,
            review_item_id=revision.review_item_id,
            version_id=revision.version_id,
            issue_id=revision.issue_id,
            revision_no=revision.revision_no,
            content=revision.content,
            annotation_set_id=revision.annotation_set_id,
            created_at=iso(revision.created_at),
        )
        annotation_dto = None
        if annotation:
            annotation_dto = ReviewAnnotationSetDTO(
                id=annotation.id,
                project_ref_id=annotation.project_ref_id,
                review_item_id=annotation.review_item_id,
                version_id=annotation.version_id,
                issue_id=annotation.issue_id,
                timestamp_ms=annotation.timestamp_ms,
                frame_number=annotation.frame_number,
                canvas_width=annotation.canvas_width,
                canvas_height=annotation.canvas_height,
                video_width=annotation.video_width,
                video_height=annotation.video_height,
                shapes=[ReviewAnnotationShape.model_validate(shape) for shape in annotation.shapes],
                created_at=iso(annotation.created_at),
            )
        playback_target = ReviewPlaybackTarget(
            project_ref_id=issue.project_ref_id,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
            revision_id=revision.id,
            annotation_set_id=revision.annotation_set_id,
            timestamp_ms=issue.timestamp_ms,
            frame_number=issue.frame_number,
        )
        return ReviewIssueDTO(
            id=issue.id,
            project_ref_id=issue.project_ref_id,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_no=issue.issue_no,
            status=issue.status,  # type: ignore[arg-type]
            current_revision_id=issue.current_revision_id,
            timestamp_ms=issue.timestamp_ms,
            frame_number=issue.frame_number,
            playback_target=playback_target,
            current_revision=revision_dto,
            current_annotation_set=annotation_dto,
            deleted_at=iso(issue.deleted_at) if issue.deleted_at else None,
            lock_version=issue.lock_version,
            created_at=iso(issue.created_at),
            updated_at=iso(issue.updated_at),
        ).model_dump(mode="json")

    def _get_project(self, project_ref_id: str, *, for_update: bool = False) -> ProjectRefModel:
        statement = select(ProjectRefModel).where(ProjectRefModel.id == project_ref_id)
        if for_update:
            statement = statement.with_for_update()
        project = self.session.scalar(statement)
        if not project:
            raise not_found()
        return project

    def _get_item(self, project_ref_id: str, review_item_id: str, *, for_update: bool = False) -> ReviewItemModel:
        statement = select(ReviewItemModel).where(
            ReviewItemModel.id == review_item_id,
            ReviewItemModel.project_ref_id == project_ref_id,
        )
        if for_update:
            statement = statement.with_for_update()
        item = self.session.scalar(statement)
        if not item:
            raise not_found()
        return item

    def _get_version(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        *,
        for_update: bool = False,
    ) -> ReviewVersionModel:
        statement = select(ReviewVersionModel).where(
                ReviewVersionModel.id == version_id,
                ReviewVersionModel.project_ref_id == project_ref_id,
                ReviewVersionModel.review_item_id == review_item_id,
            )
        if for_update:
            statement = statement.with_for_update()
        version = self.session.scalar(statement)
        if not version:
            raise not_found()
        return version

    def _get_issue(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, *, include_deleted: bool = False) -> ReviewIssueModel:
        issue = self.session.scalar(
            select(ReviewIssueModel).where(
                ReviewIssueModel.id == issue_id,
                ReviewIssueModel.project_ref_id == project_ref_id,
                ReviewIssueModel.review_item_id == review_item_id,
                ReviewIssueModel.version_id == version_id,
            )
        )
        if not issue:
            raise not_found()
        if issue.deleted_at is not None and not include_deleted:
            raise not_found()
        return issue

    def _get_revision(self, issue_id: str, revision_id: str) -> ReviewIssueRevisionModel:
        revision = self.session.scalar(
            select(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.id == revision_id, ReviewIssueRevisionModel.issue_id == issue_id)
        )
        if not revision:
            raise not_found()
        return revision

    def _get_annotation(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        issue_id: str,
        annotation_set_id: str | None,
    ) -> ReviewAnnotationSetModel | None:
        if not annotation_set_id:
            return None
        annotation = self.session.scalar(
            select(ReviewAnnotationSetModel).where(
                ReviewAnnotationSetModel.id == annotation_set_id,
                ReviewAnnotationSetModel.project_ref_id == project_ref_id,
                ReviewAnnotationSetModel.review_item_id == review_item_id,
                ReviewAnnotationSetModel.version_id == version_id,
                ReviewAnnotationSetModel.issue_id == issue_id,
            )
        )
        if not annotation:
            raise not_found()
        return annotation

    def _expect_lock(self, model: Any, expected_version: int | None) -> None:
        if expected_version is not None and model.lock_version != expected_version:
            raise ReviewError("OPTIMISTIC_LOCK_CONFLICT", "乐观锁冲突")

    def _get_ready_file(self, file_id: str, context: ExecutionContext) -> FileObjectModel:
        file = self.session.get(FileObjectModel, file_id)
        if not file:
            raise ReviewError("VERSION_FILE_NOT_READY", "文件尚未完成上传")
        if not context.principal.id or file.owner_principal_id != context.principal.id or file.owner_principal_kind != context.principal.kind:
            raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "当前 principal 无权使用该文件")
        return file

    def _assert_playback_ready(self, version: ReviewVersionModel) -> None:
        if not self._playback_asset_is_ready(version.playback_asset_id):
            raise ReviewError("PLAYBACK_NOT_READY", "播放文件尚未就绪")

    def _assert_project_active(self, project: ProjectRefModel) -> None:
        if project.deleted_at is not None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目已删除")
        if project.lifecycle_status != "active":
            raise ReviewError("RESOURCE_STATE_CONFLICT", "归档项目只读")

    def _assert_project_visible(self, project: ProjectRefModel) -> None:
        if project.deleted_at is not None:
            raise not_found()

    def create_project(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        del expected_version
        project = ProjectRefModel(
            id=new_id("prj"),
            project_code=payload["project_code"],
            project_name=payload["project_name"],
            description=payload.get("description", ""),
            source="host" if payload.get("external_project_id") else "local",
            external_project_id=payload.get("external_project_id"),
        )
        self.session.add(project)
        self.session.flush()
        self._event("review.project.created", context, project.id, "project", project.id, project.lock_version, payload={"project_code": project.project_code})
        return self.project_dto(project)

    def update_project(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        self._expect_lock(project, expected_version)
        if "project_code" in payload and payload["project_code"] not in (None, project.project_code):
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目编号创建后不可修改")
        project.project_name = payload["project_name"]
        project.description = payload["description"]
        project.lock_version += 1
        project.updated_at = utcnow()
        self._event("review.project.updated", context, project.id, "project", project.id, project.lock_version)
        return self.project_dto(project)

    def archive_project(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._expect_lock(project, expected_version)
        if project.deleted_at is not None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目已删除")
        if project.lifecycle_status == "archived":
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目已归档")
        project.lifecycle_status = "archived"
        project.lock_version += 1
        project.updated_at = utcnow()
        self._event("review.project.archived", context, project.id, "project", project.id, project.lock_version)
        return self.project_dto(project)

    def restore_project(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._expect_lock(project, expected_version)
        if project.deleted_at is not None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目已删除")
        if project.lifecycle_status != "archived":
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目未归档")
        project.lifecycle_status = "active"
        project.lock_version += 1
        project.updated_at = utcnow()
        self._event("review.project.restored", context, project.id, "project", project.id, project.lock_version)
        return self.project_dto(project)

    def soft_delete_project(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        self._expect_lock(project, expected_version)
        project.deleted_at = utcnow()
        project.lock_version += 1
        project.updated_at = utcnow()
        self._event("review.project.deleted", context, project.id, "project", project.id, project.lock_version)
        return self.project_dto(project)

    def create_review_item(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        del expected_version
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        file = self._get_ready_file(payload["original_file_id"], context)
        item = ReviewItemModel(
            id=new_id("item"),
            project_ref_id=project.id,
            item_code=payload["item_code"],
            episode_no=payload.get("episode_no"),
            title=payload["title"],
            workflow_status="pending_review",
        )
        self.session.add(item)
        self.session.flush()
        version = self._create_version(
            project.id, item.id, file, previous_version_id=None, version_no=1, version_note=payload.get("version_note"), change_summary=None
        )
        item.current_version_id = version.id
        item.updated_at = utcnow()
        self._event("review.item.created", context, project.id, "review_item", item.id, item.lock_version, review_item_id=item.id)
        self._event(
            "review.version.uploaded", context, project.id, "review_version", version.id, version.lock_version, review_item_id=item.id, version_id=version.id
        )
        return self.item_dto(item)

    def update_review_item(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        if not any(field in payload for field in ("episode_no", "title")):
            raise ReviewError("VALIDATION_ERROR", "成片元数据更新至少需要 title 或 episode_no")
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        invariants.ensure_not_finalized(item.workflow_status)
        self._expect_lock(item, expected_version)
        for field in ("episode_no", "title"):
            if field in payload and payload[field] is not None:
                setattr(item, field, payload[field])
        item.lock_version += 1
        item.updated_at = utcnow()
        self._event("review.item.updated", context, project.id, "review_item", item.id, item.lock_version, review_item_id=item.id)
        return self.item_dto(item)

    def delete_review_item(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "删除分集必须二次确认")
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        self._expect_lock(item, expected_version)
        versions = list(
            self.session.scalars(
                select(ReviewVersionModel).where(
                    ReviewVersionModel.project_ref_id == project.id,
                    ReviewVersionModel.review_item_id == item.id,
                )
            )
        )
        issue_count = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewIssueModel)
                .where(
                    ReviewIssueModel.project_ref_id == project.id,
                    ReviewIssueModel.review_item_id == item.id,
                )
            )
            or 0
        )
        finalization_count = (
            self.session.scalar(
                select(func.count())
                .select_from(FinalizationRecordModel)
                .where(
                    FinalizationRecordModel.project_ref_id == project.id,
                    FinalizationRecordModel.review_item_id == item.id,
                )
            )
            or 0
        )
        if item.workflow_status != "pending_review" or len(versions) != 1 or issue_count > 0 or finalization_count > 0 or item.active_finalization_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "审核已开始，不能删除分集")
        derivative_tasks = list(
            self.session.scalars(
                select(MediaDerivativeTaskModel)
                .where(
                    MediaDerivativeTaskModel.project_ref_id == project.id,
                    MediaDerivativeTaskModel.review_item_id == item.id,
                )
                .with_for_update()
            )
        )
        file_ids = {
            file_id
            for version in versions
            for file_id in (
                version.original_file_id,
                version.playback_asset_id,
                version.thumbnail_asset_id,
            )
            if file_id
        }
        file_ids.update(task.output_file_id for task in derivative_tasks if task.output_file_id)
        item.lock_version += 1
        item.updated_at = utcnow()
        deleted = self.item_dto(item)
        self._event("review.item.deleted", context, project.id, "review_item", item.id, item.lock_version)
        self.session.execute(
            update(OutboxEventModel)
            .where(
                OutboxEventModel.project_ref_id == project.id,
                OutboxEventModel.review_item_id == item.id,
            )
            .values(
                review_item_id=None,
                version_id=None,
                issue_id=None,
                finalization_id=None,
            )
        )
        item.current_version_id = None
        self.session.flush()
        for task in derivative_tasks:
            self.session.delete(task)
        self.session.flush()
        for version in versions:
            self.session.delete(version)
        self.session.flush()
        self.session.delete(item)
        self.session.flush()
        for file_id in file_ids:
            self._delete_file_object_if_unreferenced(file_id)
        return deleted

    def _delete_file_object_if_unreferenced(self, file_id: str) -> None:
        file = self.session.get(FileObjectModel, file_id)
        if file is None:
            return
        version_refs = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewVersionModel)
                .where(
                    (ReviewVersionModel.original_file_id == file_id)
                    | (ReviewVersionModel.playback_asset_id == file_id)
                    | (ReviewVersionModel.thumbnail_asset_id == file_id)
                )
            )
            or 0
        )
        finalization_refs = (
            self.session.scalar(select(func.count()).select_from(FinalizationRecordModel).where(FinalizationRecordModel.original_file_id == file_id)) or 0
        )
        if version_refs or finalization_refs:
            return
        storage_path = contained_storage_path(file.storage_path, self.settings.storage_root, file_id)
        uploads = list(self.session.scalars(select(UploadSessionModel).where(UploadSessionModel.file_id == file_id).with_for_update()))
        for upload in uploads:
            upload.file_id = None
            if upload.parts_cleanup_confirmed_at is not None and not upload.received_parts:
                self.session.delete(upload)
            else:
                if upload.received_parts:
                    upload.parts_cleanup_confirmed_at = None
                upload.updated_at = utcnow()
        self.session.flush()
        self.session.delete(file)
        self.session.flush()
        self._queue_post_commit_file_deletion(storage_path, file_id)

    def _create_version(
        self,
        project_ref_id: str,
        review_item_id: str,
        file: FileObjectModel,
        previous_version_id: str | None,
        version_no: int,
        version_note: str | None,
        change_summary: str | None,
    ) -> ReviewVersionModel:
        version = ReviewVersionModel(
            id=new_id("ver"),
            project_ref_id=project_ref_id,
            review_item_id=review_item_id,
            previous_version_id=previous_version_id,
            version_no=version_no,
            version_label=f"V{version_no}",
            is_current=True,
            original_file_id=file.id,
            original_filename=file.original_filename,
            mime_type=file.mime_type,
            file_size=file.file_size,
            sha256=file.sha256,
            duration_ms=file.duration_ms,
            width=file.width,
            height=file.height,
            fps_num=file.fps_num,
            fps_den=file.fps_den,
            media_probe_version=file.media_probe_version,
            playback_asset_id=None,
            thumbnail_asset_id=None,
            version_note=version_note,
            change_summary=change_summary,
        )
        self.session.add(version)
        self.session.flush()
        now = utcnow()
        self.session.add_all(
            [
                MediaDerivativeTaskModel(
                    id=new_id("mdt"),
                    project_ref_id=project_ref_id,
                    review_item_id=review_item_id,
                    version_id=version.id,
                    kind=kind,
                    status="queued",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
                for kind in ("playback_faststart", "thumbnail")
            ]
        )
        self.session.flush()
        return version

    def upload_review_version(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        self._expect_lock(item, expected_version)
        current = self._get_version(project.id, item.id, item.current_version_id or "")
        current_issue_count = (
            self.session.scalar(
                select(func.count())
                .select_from(ReviewIssueModel)
                .where(
                    ReviewIssueModel.review_item_id == item.id,
                    ReviewIssueModel.version_id == current.id,
                    ReviewIssueModel.deleted_at.is_(None),
                )
            )
            or 0
        )
        invariants.ensure_can_upload_version(item.workflow_status, int(current_issue_count))
        file = self._get_ready_file(payload["original_file_id"], context)
        item.current_version_id = None
        self.session.flush()
        self.session.execute(
            update(ReviewVersionModel).where(ReviewVersionModel.review_item_id == item.id, ReviewVersionModel.is_current.is_(True)).values(is_current=False)
        )
        version_no = current.version_no + 1
        version = self._create_version(
            project.id,
            item.id,
            file,
            previous_version_id=current.id,
            version_no=version_no,
            version_note=payload.get("version_note"),
            change_summary=payload.get("change_summary") or payload.get("supersede_reason"),
        )
        item.current_version_id = version.id
        item.workflow_status = "pending_review"
        item.lock_version += 1
        item.updated_at = utcnow()
        self._event(
            "review.version.uploaded", context, project.id, "review_version", version.id, version.lock_version, review_item_id=item.id, version_id=version.id
        )
        return self.version_dto(version)

    def start_review(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        version = self._get_version(item.project_ref_id, item.id, item.current_version_id or "")
        self._assert_playback_ready(version)
        self._expect_lock(item, expected_version)
        invariants.ensure_not_finalized(item.workflow_status)
        if item.workflow_status == "pending_review":
            item.workflow_status = "in_review"
            item.lock_version += 1
            item.updated_at = utcnow()
            self._event(
                "review.session.started",
                context,
                item.project_ref_id,
                "review_item",
                item.id,
                item.lock_version,
                review_item_id=item.id,
                version_id=item.current_version_id,
            )
        return self.item_dto(item)

    def create_review_issue(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        self._expect_lock(item, expected_version)
        version = self._get_version(item.project_ref_id, item.id, payload["version_id"])
        invariants.ensure_current_version(version.id, item.current_version_id or "")
        self._assert_playback_ready(version)
        invariants.ensure_can_create_issue(item.workflow_status)
        started_review = item.workflow_status == "pending_review"
        if item.workflow_status == "pending_review":
            item.workflow_status = "in_review"
        issue_no = (self.session.scalar(select(func.max(ReviewIssueModel.issue_no)).where(ReviewIssueModel.review_item_id == item.id)) or 0) + 1
        issue_id = new_id("iss")
        revision_id = new_id("rev")
        issue = ReviewIssueModel(
            id=issue_id,
            project_ref_id=item.project_ref_id,
            review_item_id=item.id,
            version_id=version.id,
            issue_no=issue_no,
            status="unresolved",
            current_revision_id=revision_id,
            timestamp_ms=payload["timestamp_ms"],
            frame_number=payload["frame_number"],
        )
        self.session.add(issue)
        self.session.flush()
        annotation_id = self._create_annotation_if_present(issue, payload.get("annotation"))
        revision = ReviewIssueRevisionModel(
            id=revision_id,
            project_ref_id=issue.project_ref_id,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
            revision_no=1,
            content=payload["content"],
            annotation_set_id=annotation_id,
        )
        self.session.add(revision)
        self.session.flush()
        self._sync_item_workflow_for_current_issues(item)
        if started_review:
            self._event(
                "review.session.started", context, item.project_ref_id, "review_item", item.id, item.lock_version, review_item_id=item.id, version_id=version.id
            )
        self._event(
            "review.issue.created",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return self.issue_dto(issue)

    def _sync_item_workflow_for_current_issues(self, item: ReviewItemModel) -> None:
        """Synchronize the authoritative item state while holding the item row lock."""
        if item.workflow_status == "finalized":
            raise ReviewError("REVIEW_ITEM_FINALIZED", "已定稿条目不可修改意见状态")
        current_version_id = item.current_version_id
        if not current_version_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "审阅条目缺少当前版本")
        unresolved = int(
            self.session.scalar(
                select(func.count())
                .select_from(ReviewIssueModel)
                .where(
                    ReviewIssueModel.review_item_id == item.id,
                    ReviewIssueModel.version_id == current_version_id,
                    ReviewIssueModel.status == "unresolved",
                    ReviewIssueModel.deleted_at.is_(None),
                )
            )
            or 0
        )
        item.workflow_status = "changes_requested" if unresolved > 0 else "in_review"
        item.lock_version += 1
        item.updated_at = utcnow()

    def _create_annotation_if_present(self, issue: ReviewIssueModel, annotation: dict[str, Any] | None) -> str | None:
        if not annotation:
            return None
        annotation_id = new_id("ann")
        shapes = [shape.model_dump(mode="json") if hasattr(shape, "model_dump") else shape for shape in annotation.get("shapes", [])]
        self.session.add(
            ReviewAnnotationSetModel(
                id=annotation_id,
                project_ref_id=issue.project_ref_id,
                review_item_id=issue.review_item_id,
                version_id=issue.version_id,
                issue_id=issue.id,
                timestamp_ms=issue.timestamp_ms,
                frame_number=issue.frame_number,
                canvas_width=annotation["canvas_width"],
                canvas_height=annotation["canvas_height"],
                video_width=annotation["video_width"],
                video_height=annotation["video_height"],
                shapes=shapes,
            )
        )
        self.session.flush()
        return annotation_id

    def update_review_issue(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        issue = self._get_issue(project.id, item.id, payload["version_id"], payload["issue_id"])
        invariants.ensure_issue_writable(item.workflow_status)
        invariants.ensure_current_version(issue.version_id, item.current_version_id or "")
        self._expect_lock(issue, expected_version)
        current_revision = self._get_revision(issue.id, issue.current_revision_id or "")
        annotation_id = self._create_annotation_if_present(issue, payload.get("annotation"))
        revision = ReviewIssueRevisionModel(
            id=new_id("rev"),
            project_ref_id=issue.project_ref_id,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
            revision_no=current_revision.revision_no + 1,
            content=payload.get("content") or current_revision.content,
            annotation_set_id=annotation_id if annotation_id is not None else current_revision.annotation_set_id,
        )
        self.session.add(revision)
        self.session.flush()
        issue.current_revision_id = revision.id
        issue.lock_version += 1
        issue.updated_at = utcnow()
        self._event(
            "review.issue.updated",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return self.issue_dto(issue)

    def add_review_message(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        del expected_version
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        issue = self._get_issue(project.id, item.id, payload["version_id"], payload["issue_id"])
        invariants.ensure_issue_writable(item.workflow_status)
        invariants.ensure_current_version(issue.version_id, item.current_version_id or "")
        message = ReviewThreadMessageModel(
            id=new_id("msg"),
            project_ref_id=issue.project_ref_id,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
            content=payload["content"],
        )
        self.session.add(message)
        self.session.flush()
        self._event(
            "review.issue.message_added",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return ThreadMessageDTO(
            id=message.id,
            project_ref_id=message.project_ref_id,
            review_item_id=message.review_item_id,
            version_id=message.version_id,
            issue_id=message.issue_id,
            content=message.content,
            created_at=iso(message.created_at),
        ).model_dump(mode="json")

    def resolve_issue(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        issue = self._get_issue(project.id, item.id, payload["version_id"], payload["issue_id"])
        invariants.ensure_issue_writable(item.workflow_status)
        invariants.ensure_current_version(issue.version_id, item.current_version_id or "")
        self._expect_lock(issue, expected_version)
        if issue.status == "resolved":
            return self.issue_dto(issue)
        issue.status = "resolved"
        issue.lock_version += 1
        issue.updated_at = utcnow()
        self.session.flush()
        self._sync_item_workflow_for_current_issues(item)
        self._event(
            "review.issue.resolved",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return self.issue_dto(issue)

    def reopen_issue(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        issue = self._get_issue(project.id, item.id, payload["version_id"], payload["issue_id"])
        invariants.ensure_issue_writable(item.workflow_status)
        invariants.ensure_current_version(issue.version_id, item.current_version_id or "")
        self._expect_lock(issue, expected_version)
        if issue.status == "unresolved":
            return self.issue_dto(issue)
        issue.status = "unresolved"
        issue.lock_version += 1
        issue.updated_at = utcnow()
        self.session.flush()
        self._sync_item_workflow_for_current_issues(item)
        self._event(
            "review.issue.reopened",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return self.issue_dto(issue)

    def soft_delete_issue(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        issue = self._get_issue(
            project.id,
            item.id,
            payload["version_id"],
            payload["issue_id"],
            include_deleted=True,
        )
        invariants.ensure_issue_writable(item.workflow_status)
        invariants.ensure_current_version(issue.version_id, item.current_version_id or "")
        self._expect_lock(issue, expected_version)
        if issue.deleted_at is not None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "意见已删除")
        issue.deleted_at = utcnow()
        issue.lock_version += 1
        issue.updated_at = utcnow()
        self.session.flush()
        self._sync_item_workflow_for_current_issues(item)
        self._event(
            "review.issue.deleted",
            context,
            issue.project_ref_id,
            "review_issue",
            issue.id,
            issue.lock_version,
            review_item_id=issue.review_item_id,
            version_id=issue.version_id,
            issue_id=issue.id,
        )
        return self.issue_dto(issue)

    def finalize_version(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "定稿必须 confirmed=true")
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        version = self._get_version(item.project_ref_id, item.id, payload["version_id"])
        invariants.ensure_current_version(version.id, item.current_version_id or "")
        self._assert_playback_ready(version)
        self._expect_lock(item, expected_version)
        invariants.ensure_finalizable(item.workflow_status)
        incomplete_revocation_cleanup = self.session.scalar(
            select(FinalizationPackageInvalidationModel.id)
            .where(
                FinalizationPackageInvalidationModel.project_ref_id == project.id,
                FinalizationPackageInvalidationModel.review_item_id == item.id,
                FinalizationPackageInvalidationModel.cleanup_status != "complete",
            )
            .order_by(FinalizationPackageInvalidationModel.created_at)
            .limit(1)
            .with_for_update()
        )
        if incomplete_revocation_cleanup is not None:
            raise ReviewError(
                "RESOURCE_STATE_CONFLICT",
                "旧定稿包尚未完成物理清理",
            )
        if item.active_finalization_id:
            raise ReviewError("REVIEW_ITEM_FINALIZED", "当前版本已存在 active finalization")
        file = self.session.get(FileObjectModel, version.original_file_id)
        if not file:
            raise ReviewError("PACKAGE_SOURCE_MISSING", "定稿源文件缺失")
        storage_path = contained_storage_path(file.storage_path, self.settings.storage_root, file.id)
        if regular_file_sha256(storage_path, self.settings.storage_root) != version.sha256:
            raise ReviewError("FILE_HASH_MISMATCH", "定稿源文件 hash 不匹配")
        finalization = FinalizationRecordModel(
            id=new_id("fin"),
            project_ref_id=item.project_ref_id,
            review_item_id=item.id,
            version_id=version.id,
            version_no=version.version_no,
            original_file_id=version.original_file_id,
            original_filename=version.original_filename,
            mime_type=version.mime_type,
            file_size=version.file_size,
            sha256=version.sha256,
            duration_ms=version.duration_ms,
            width=version.width,
            height=version.height,
            fps_num=version.fps_num,
            fps_den=version.fps_den,
            media_probe_version=version.media_probe_version,
            status="active",
        )
        self.session.add(finalization)
        self.session.flush()
        item.workflow_status = "finalized"
        item.active_finalization_id = finalization.id
        item.lock_version += 1
        item.updated_at = utcnow()
        self._event(
            "review.version.finalized",
            context,
            item.project_ref_id,
            "review_item",
            item.id,
            item.lock_version,
            review_item_id=item.id,
            version_id=version.id,
            finalization_id=finalization.id,
        )
        return self.finalization_dto(finalization)

    def revoke_finalization(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "撤销定稿必须 confirmed=true")
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        item = self._get_item(project.id, payload["review_item_id"], for_update=True)
        self._expect_lock(item, expected_version)
        if item.workflow_status != "finalized" or not item.active_finalization_id:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "审阅条目当前未定稿")
        finalization = self.session.scalar(
            select(FinalizationRecordModel)
            .where(
                FinalizationRecordModel.id == item.active_finalization_id,
                FinalizationRecordModel.project_ref_id == project.id,
                FinalizationRecordModel.review_item_id == item.id,
                FinalizationRecordModel.status == "active",
            )
            .with_for_update()
        )
        if finalization is None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "定稿指针与权威记录不一致")

        now = utcnow()
        # Existing database freeze triggers permit the exact item transition first;
        # the same transaction then revokes the child finalization and packages.
        item.workflow_status = "in_review"
        item.active_finalization_id = None
        item.lock_version += 1
        item.updated_at = now
        self.session.flush()

        packages = list(
            self.session.scalars(
                select(FinalCutPackageSnapshotModel)
                .where(
                    FinalCutPackageSnapshotModel.project_ref_id == project.id,
                    FinalCutPackageSnapshotModel.status.in_(
                        ("preparing", "ready", "failed", "expired", "invalidated")
                    ),
                    FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
                )
                .order_by(FinalCutPackageSnapshotModel.created_at, FinalCutPackageSnapshotModel.id)
                .with_for_update()
            )
        )
        invalidated_package_ids: list[str] = []
        for package in packages:
            if not any(
                isinstance(package_item, dict)
                and package_item.get("finalization_id") == finalization.id
                for package_item in package.items
            ):
                continue
            if package.status != "invalidated":
                package.status = "invalidated"
                package.expires_at = now - timedelta(microseconds=1)
                package.next_build_attempt_at = None
                package.build_lease_id = None
                package.build_lease_expires_at = None
                package.download_session_hash = None
                package.download_session_expires_at = None
                package.download_lease_id = None
                package.download_lease_expires_at = None
                package.failure_details = {
                    "error_code": "PACKAGE_INVALIDATED_BY_FINALIZATION_REVOKE",
                    "finalization_id": finalization.id,
                }
                package.updated_at = now
            invalidated_package_ids.append(package.id)
            self.session.add(
                FinalizationPackageInvalidationModel(
                    id=new_id("fpi"),
                    project_ref_id=project.id,
                    review_item_id=item.id,
                    finalization_id=finalization.id,
                    package_id=package.id,
                    cleanup_status="pending",
                )
            )

        finalization.status = "revoked"
        finalization.revoked_at = now
        self.session.flush()
        self._event(
            "review.finalization.revoked",
            context,
            item.project_ref_id,
            "review_item",
            item.id,
            item.lock_version,
            review_item_id=item.id,
            version_id=finalization.version_id,
            finalization_id=finalization.id,
        )
        return {
            "finalization": self.finalization_dto(finalization),
            "review_item": self.item_dto(item),
            "cleanup_status": "pending" if invalidated_package_ids else "complete",
            "invalidated_package_ids": invalidated_package_ids,
        }

    def finalization_dto(self, finalization: FinalizationRecordModel) -> dict[str, Any]:
        return FinalizationDTO(
            id=finalization.id,
            project_ref_id=finalization.project_ref_id,
            review_item_id=finalization.review_item_id,
            version_id=finalization.version_id,
            version_no=finalization.version_no,
            original_media=OriginalMediaSnapshotDTO(
                original_file_id=finalization.original_file_id,
                original_filename=finalization.original_filename,
                mime_type=finalization.mime_type,
                file_size=finalization.file_size,
                sha256=finalization.sha256,
                duration_ms=finalization.duration_ms,
                width=finalization.width,
                height=finalization.height,
                fps_num=finalization.fps_num,
                fps_den=finalization.fps_den,
                media_probe_version=finalization.media_probe_version,
            ),
            status=finalization.status,  # type: ignore[arg-type]
            finalized_at=iso(finalization.finalized_at),
            revoked_at=iso(finalization.revoked_at) if finalization.revoked_at else None,
        ).model_dump(mode="json")

    def _ready_package_reuse_integrity(
        self,
        snapshot: FinalCutPackageSnapshotModel,
    ) -> tuple[str | None, Path | None, int | None, tuple[int, int] | None]:
        package_root = self.settings.package_root
        if not re.fullmatch(r"pkg_[0-9a-f]{32}", snapshot.id):
            return "STORAGE_UNAVAILABLE", None, None, None
        try:
            expected_path = contained_path(Path(f"{snapshot.id}.zip"), package_root)
            with pin_regular_file(expected_path, package_root) as pinned:
                if pinned is None or not pinned.exists:
                    return "PACKAGE_SOURCE_MISSING", expected_path, None, None
                actual_size = pinned.size
                identity = (pinned.device, pinned.inode) if pinned.device is not None and pinned.inode is not None else None
                stored_path = contained_path(snapshot.storage_path, package_root)
                if stored_path != expected_path or snapshot.storage_reclaimed_at is not None:
                    return "STORAGE_UNAVAILABLE", expected_path, actual_size, identity
                if (
                    actual_size is None
                    or snapshot.storage_bytes != actual_size
                    or not isinstance(snapshot.sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", snapshot.sha256) is None
                ):
                    return "FILE_HASH_MISMATCH", expected_path, actual_size, identity

                digest = hashlib.sha256()
                bytes_read = 0
                with pinned.open_readonly() as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                        bytes_read += len(chunk)
                    final_metadata = os.fstat(handle.fileno())
                if (
                    bytes_read != actual_size
                    or final_metadata.st_size != actual_size
                    or final_metadata.st_mtime_ns != pinned.mtime_ns
                    or not hmac.compare_digest(digest.hexdigest(), snapshot.sha256)
                ):
                    return "FILE_HASH_MISMATCH", expected_path, final_metadata.st_size, identity
                return None, expected_path, actual_size, identity
        except OSError:
            return "STORAGE_UNAVAILABLE", None, None, None

    def _fail_ready_package_reuse(
        self,
        snapshot: FinalCutPackageSnapshotModel,
        context: ExecutionContext,
        *,
        error_code: str,
        expected_path: Path | None,
        actual_size: int | None,
        identity: tuple[int, int] | None,
    ) -> None:
        now = utcnow()
        snapshot.status = "failed"
        snapshot.sha256 = None
        snapshot.next_build_attempt_at = None
        snapshot.download_session_hash = None
        snapshot.download_session_expires_at = None
        snapshot.download_lease_id = None
        snapshot.download_lease_expires_at = None
        snapshot.failure_details = {"error_code": error_code}
        snapshot.expires_at = min(aware(snapshot.expires_at), now)
        snapshot.updated_at = now
        if expected_path is not None:
            snapshot.storage_path = str(expected_path)
        if actual_size is None and error_code == "PACKAGE_SOURCE_MISSING":
            snapshot.storage_bytes = 0
            snapshot.storage_reclaimed_at = now
        else:
            if actual_size is not None:
                snapshot.storage_bytes = actual_size
            snapshot.storage_reclaimed_at = None
            if expected_path is not None and identity is not None:
                self._queue_post_commit_package_deletion(
                    snapshot.id,
                    expected_path,
                    device=identity[0],
                    inode=identity[1],
                )
        self.session.flush()
        self._event(
            "review.package.failed",
            context,
            snapshot.project_ref_id,
            "package",
            snapshot.id,
            1,
            package_id=snapshot.id,
            payload={"error_code": error_code},
        )
        self.session.flush()

    def prepare_package(self, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]:
        del expected_version
        project = self._get_project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        existing_preparing = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.project_ref_id == project.id,
                FinalCutPackageSnapshotModel.status == "preparing",
            )
            .order_by(FinalCutPackageSnapshotModel.created_at, FinalCutPackageSnapshotModel.id)
            .limit(1)
        )
        if existing_preparing is not None:
            return self.package_dto(existing_preparing)
        finalizations = list(
            self.session.scalars(
                select(FinalizationRecordModel)
                .where(
                    FinalizationRecordModel.project_ref_id == project.id,
                    FinalizationRecordModel.status == "active",
                )
                .order_by(FinalizationRecordModel.review_item_id, FinalizationRecordModel.id)
            )
        )
        if not finalizations:
            raise ReviewError("PACKAGE_NO_FINALIZED_FILES", "项目无定稿原片")
        total_bytes = validate_package_limits(finalizations, self.settings)
        items: list[dict[str, Any]] = []
        package_id = new_id("pkg")
        package_filename = f"{project.project_code}_{project.project_name}_定稿原片_{utcnow().strftime('%Y%m%d-%H%M')}.zip"
        package_path = contained_path(Path(f"{package_id}.zip"), self.settings.package_root)
        archive_names: set[str] = set()
        for finalization in finalizations:
            item = self._get_item(finalization.project_ref_id, finalization.review_item_id)
            version = self._get_version(finalization.project_ref_id, finalization.review_item_id, finalization.version_id)
            original_name = safe_archive_component(finalization.original_filename)
            base_arcname = "_".join(
                [
                    safe_archive_component(item.item_code),
                    safe_archive_component(item.title),
                    safe_archive_component(version.version_label),
                    original_name,
                ]
            )
            arcname = base_arcname
            if arcname in archive_names:
                arcname = "_".join(
                    [
                        safe_archive_component(item.item_code),
                        safe_archive_component(item.title),
                        item.id[-8:],
                        safe_archive_component(version.version_label),
                        original_name,
                    ]
                )
            suffix = 2
            while arcname in archive_names:
                stem = Path(original_name).stem or "original"
                suffix_name = f"{stem}_{suffix}{Path(original_name).suffix}"
                arcname = "_".join(
                    [
                        safe_archive_component(item.item_code),
                        safe_archive_component(item.title),
                        item.id[-8:],
                        safe_archive_component(version.version_label),
                        safe_archive_component(suffix_name),
                    ]
                )
                suffix += 1
            archive_names.add(arcname)
            items.append(
                {
                    "review_item_id": finalization.review_item_id,
                    "version_id": finalization.version_id,
                    "finalization_id": finalization.id,
                    "original_file_id": finalization.original_file_id,
                    "original_filename": finalization.original_filename,
                    "sha256": finalization.sha256,
                    "archive_name": arcname,
                }
            )
        estimated_storage_bytes = estimate_package_storage_bytes(
            total_bytes,
            [str(item["archive_name"]) for item in items],
        )
        reusable_packages = self.session.scalars(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.project_ref_id == project.id,
                FinalCutPackageSnapshotModel.status == "ready",
                FinalCutPackageSnapshotModel.expires_at > utcnow(),
            )
            .order_by(FinalCutPackageSnapshotModel.created_at.desc())
            .limit(self.settings.max_pending_package_builds)
        )
        scanned_reusable_packages: list[
            tuple[
                str,
                tuple[Any, ...],
                tuple[str | None, Path | None, int | None, tuple[int, int] | None],
            ]
        ] = []

        def reusable_fingerprint(snapshot: FinalCutPackageSnapshotModel) -> tuple[Any, ...]:
            return (
                snapshot.status,
                snapshot.expires_at,
                snapshot.total_bytes,
                json.dumps(snapshot.items, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                snapshot.storage_path,
                snapshot.storage_bytes,
                snapshot.storage_reclaimed_at,
                snapshot.sha256,
                snapshot.updated_at,
            )

        for reusable in reusable_packages:
            if reusable.total_bytes != total_bytes or reusable.items != items:
                continue
            sources_available = True
            for reusable_item in reusable.items:
                file_id = reusable_item.get("original_file_id") if isinstance(reusable_item, dict) else None
                file = self.session.get(FileObjectModel, file_id) if isinstance(file_id, str) else None
                if file is None:
                    sources_available = False
                    break
                try:
                    with pin_regular_file(file.storage_path, self.settings.storage_root) as pinned:
                        if pinned is None or not pinned.exists:
                            sources_available = False
                            break
                except OSError:
                    sources_available = False
                    break
            if not sources_available:
                continue
            scanned_reusable_packages.append(
                (
                    reusable.id,
                    reusable_fingerprint(reusable),
                    self._ready_package_reuse_integrity(reusable),
                )
            )
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": STORAGE_ADMISSION_ADVISORY_LOCK_KEY},
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": PACKAGE_QUEUE_ADVISORY_LOCK_KEY},
            )
        existing_preparing = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.project_ref_id == project.id,
                FinalCutPackageSnapshotModel.status == "preparing",
            )
            .order_by(FinalCutPackageSnapshotModel.created_at, FinalCutPackageSnapshotModel.id)
            .limit(1)
            .with_for_update()
        )
        if existing_preparing is not None:
            return self.package_dto(existing_preparing)
        for reusable_id, scanned_fingerprint, integrity in scanned_reusable_packages:
            current_reusable = self.session.scalar(
                select(FinalCutPackageSnapshotModel)
                .where(FinalCutPackageSnapshotModel.id == reusable_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                current_reusable is None
                or current_reusable.status != "ready"
                or aware(current_reusable.expires_at) <= utcnow()
                or current_reusable.total_bytes != total_bytes
                or current_reusable.items != items
                or reusable_fingerprint(current_reusable) != scanned_fingerprint
            ):
                continue
            integrity_error, expected_path, actual_size, identity = integrity
            if integrity_error is None:
                return self.package_dto(current_reusable)
            self._fail_ready_package_reuse(
                current_reusable,
                context,
                error_code=integrity_error,
                expected_path=expected_path,
                actual_size=actual_size,
                identity=identity,
            )
        pending_count = (
            self.session.scalar(select(func.count()).select_from(FinalCutPackageSnapshotModel).where(FinalCutPackageSnapshotModel.status == "preparing")) or 0
        )
        if pending_count >= self.settings.max_pending_package_builds:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "打包队列繁忙，请稍后重试")
        reserved_bytes = active_package_storage_reservation_bytes(self.session)
        if reserved_bytes + estimated_storage_bytes > self.settings.max_package_storage_bytes:
            raise ReviewError("FILE_TOO_LARGE", "临时包存储配额不足")
        pending_reserved_bytes = pending_package_storage_reservation_bytes(self.session)
        ensure_package_filesystem_headroom(
            self.session,
            self.settings,
            package_future_reserved_bytes=pending_reserved_bytes,
            requested_bytes=estimated_storage_bytes,
        )
        self._event("review.package.requested", context, project.id, "package", package_id, 1, package_id=package_id)
        snapshot = FinalCutPackageSnapshotModel(
            id=package_id,
            project_ref_id=project.id,
            status="preparing",
            package_filename=package_filename,
            storage_path=str(package_path),
            expires_at=utcnow() + timedelta(seconds=self.settings.package_ttl_seconds),
            items=items,
            total_bytes=total_bytes,
            storage_bytes=estimated_storage_bytes,
            storage_reclaimed_at=None,
            build_attempts=0,
            next_build_attempt_at=None,
            failure_details=None,
        )
        self.session.add(snapshot)
        self.session.flush()
        return self.package_dto(snapshot)

    def build_prepared_package(self, claim: PackageBuildClaim) -> PackageBuildArtifact:
        package_root = self.settings.package_root
        package_path = contained_path(claim.storage_path, package_root)
        expected_path = contained_path(Path(f"{claim.package_id}.zip"), package_root)
        staging_path = contained_path(claim.staging_path, package_root)
        expected_staging_path = package_build_staging_path(package_root, claim.package_id, claim.lease_id)
        created_identity: tuple[int, int] | None = None
        try:
            if package_path != expected_path or staging_path != expected_staging_path:
                raise ReviewError("STORAGE_UNAVAILABLE", "打包输出路径不符合存储合同")
            if (
                not isinstance(claim.expected_source_bytes, int)
                or isinstance(claim.expected_source_bytes, bool)
                or claim.expected_source_bytes <= 0
                or not isinstance(claim.reserved_storage_bytes, int)
                or isinstance(claim.reserved_storage_bytes, bool)
                or claim.reserved_storage_bytes < claim.expected_source_bytes
                or sum(
                    source.expected_size
                    for source in claim.sources
                    if isinstance(source.expected_size, int)
                    and not isinstance(source.expected_size, bool)
                    and source.expected_size > 0
                )
                != claim.expected_source_bytes
            ):
                raise ReviewError("PACKAGE_SOURCE_MISSING", "包快照字节数非法")
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
            with pin_managed_root(package_root) as (_root_path, directory_fd):
                descriptor = os.open(staging_path.name, flags, 0o600, dir_fd=directory_fd)
                output_metadata = os.fstat(descriptor)
                created_identity = (output_metadata.st_dev, output_metadata.st_ino)
                with os.fdopen(descriptor, "wb", buffering=0) as package_handle:
                    package_writer = SequentialDigestWriter(package_handle)
                    with zipfile.ZipFile(
                        cast(BinaryIO, package_writer),
                        "w",
                        compression=zipfile.ZIP_STORED,
                    ) as zf:
                        for source in claim.sources:
                            if (
                                not isinstance(source.file_id, str)
                                or not isinstance(source.storage_path, str)
                                or not isinstance(source.expected_hash, str)
                                or len(source.expected_hash) != 64
                                or not isinstance(source.expected_size, int)
                                or isinstance(source.expected_size, bool)
                                or source.expected_size <= 0
                                or not isinstance(source.archive_name, str)
                                or Path(source.archive_name).name != source.archive_name
                            ):
                                raise ReviewError("PACKAGE_SOURCE_MISSING", "包快照条目非法")
                            source_path = contained_storage_path(
                                source.storage_path,
                                self.settings.storage_root,
                                source.file_id,
                            )
                            actual_hash = add_file_to_archive(
                                zf,
                                source_path,
                                self.settings.storage_root,
                                source.archive_name,
                                source.expected_size,
                            )
                            if actual_hash != source.expected_hash:
                                raise ReviewError("FILE_HASH_MISMATCH", "定稿原片 hash 不匹配")
                            if package_writer.tell() > claim.reserved_storage_bytes:
                                raise ReviewError("FILE_TOO_LARGE", "临时包实际大小超出预留空间")
                    package_sha256, hashed_storage_bytes = package_writer.finalize()
                    actual_storage_bytes = os.fstat(package_handle.fileno()).st_size
                    if actual_storage_bytes != hashed_storage_bytes:
                        raise ReviewError("FILE_HASH_MISMATCH", "临时包顺序摘要字节数不一致")
                    if actual_storage_bytes > claim.reserved_storage_bytes:
                        raise ReviewError("FILE_TOO_LARGE", "临时包实际大小超出预留空间")
                os.fsync(directory_fd)
            if created_identity is None:
                raise ReviewError("STORAGE_UNAVAILABLE", "打包输出缺少文件身份")
            return PackageBuildArtifact(
                package_id=claim.package_id,
                lease_id=claim.lease_id,
                storage_path=str(staging_path),
                canonical_path=claim.storage_path,
                sha256=package_sha256,
                storage_bytes=actual_storage_bytes,
                device=created_identity[0],
                inode=created_identity[1],
            )
        except Exception:
            if created_identity is not None:
                try:
                    unlink_regular_file_if_identity(
                        staging_path,
                        package_root,
                        device=created_identity[0],
                        inode=created_identity[1],
                    )
                except (OSError, UnsafeFilePathError):
                    pass
            raise

    def claim_package_build(
        self,
        package_id: str,
        context: ExecutionContext,
    ) -> tuple[str, PackageBuildClaim | None]:
        snapshot = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.status == "preparing",
            )
            .with_for_update()
        )
        if snapshot is None:
            return "skipped", None
        now = utcnow()
        if snapshot.build_lease_id is not None and snapshot.build_lease_expires_at is not None and aware(snapshot.build_lease_expires_at) > now:
            return "skipped", None
        if snapshot.next_build_attempt_at is not None and aware(snapshot.next_build_attempt_at) > now:
            return "skipped", None
        expired_lease_failure: dict[str, Any] | None = None
        if (
            snapshot.build_lease_id is not None
            and snapshot.build_lease_expires_at is not None
            and aware(snapshot.build_lease_expires_at) <= now
        ):
            # A dead worker cannot report its root cause. Persist the lease expiry
            # that this process can prove, without mislabelling it as an OOM or IO
            # failure. Host/container telemetry remains the authority for that
            # deeper diagnosis.
            expired_lease_failure = {
                "error_code": "PACKAGE_BUILD_LEASE_EXPIRED",
                "attempt": snapshot.build_attempts,
                "retryable": snapshot.build_attempts < self.settings.package_worker_max_attempts,
                "storage_reclaimed": False,
            }
        if snapshot.build_attempts >= self.settings.package_worker_max_attempts:
            last_failure = expired_lease_failure or snapshot.failure_details
            snapshot.status = "failed"
            snapshot.sha256 = None
            snapshot.next_build_attempt_at = None
            snapshot.build_lease_id = None
            snapshot.build_lease_expires_at = None
            snapshot.failure_details = {
                "error_code": "PACKAGE_BUILD_INTERRUPTED",
                "last_failure": last_failure,
            }
            snapshot.updated_at = now
            self.session.flush()
            self._event(
                "review.package.failed",
                context,
                snapshot.project_ref_id,
                "package",
                snapshot.id,
                1,
                package_id=snapshot.id,
                payload={"error_code": "PACKAGE_BUILD_INTERRUPTED"},
            )
            return "failed", None
        if expired_lease_failure is not None:
            snapshot.failure_details = {
                "error_code": "PACKAGE_BUILD_RETRY_SCHEDULED",
                "last_failure": expired_lease_failure,
            }
        lease_id = f"build_{uuid.uuid4().hex}"
        lease_expires_at = now + timedelta(seconds=self.settings.package_worker_retry_delay_seconds)
        snapshot.build_attempts += 1
        snapshot.next_build_attempt_at = lease_expires_at
        snapshot.build_lease_id = lease_id
        snapshot.build_lease_expires_at = lease_expires_at
        snapshot.updated_at = now
        sources: list[PackageBuildSource] = []
        for item in snapshot.items:
            file_id = item.get("original_file_id") if isinstance(item, dict) else None
            finalization_id = item.get("finalization_id") if isinstance(item, dict) else None
            file = self.session.get(FileObjectModel, file_id) if isinstance(file_id, str) else None
            finalization = (
                self.session.get(FinalizationRecordModel, finalization_id)
                if isinstance(finalization_id, str)
                else None
            )
            expected_size = (
                finalization.file_size
                if finalization is not None
                and finalization.project_ref_id == snapshot.project_ref_id
                and finalization.original_file_id == file_id
                and finalization.review_item_id == item.get("review_item_id")
                and finalization.version_id == item.get("version_id")
                else None
            )
            sources.append(
                PackageBuildSource(
                    file_id=file_id if isinstance(file_id, str) else None,
                    storage_path=file.storage_path if file is not None else None,
                    expected_hash=(item.get("sha256") if isinstance(item, dict) and isinstance(item.get("sha256"), str) else None),
                    expected_size=expected_size,
                    archive_name=(item.get("archive_name") if isinstance(item, dict) and isinstance(item.get("archive_name"), str) else None),
                )
            )
        self.session.flush()
        return (
            "claimed",
            PackageBuildClaim(
                package_id=snapshot.id,
                project_ref_id=snapshot.project_ref_id,
                lease_id=lease_id,
                storage_path=snapshot.storage_path,
                staging_path=str(
                    package_build_staging_path(
                        self.settings.package_root,
                        snapshot.id,
                        lease_id,
                    )
                ),
                expected_source_bytes=snapshot.total_bytes,
                reserved_storage_bytes=snapshot.storage_bytes,
                sources=tuple(sources),
            ),
        )

    def publish_prepared_package(
        self,
        artifact: PackageBuildArtifact,
        context: ExecutionContext,
    ) -> str:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": STORAGE_ADMISSION_ADVISORY_LOCK_KEY},
            )
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": PACKAGE_QUEUE_ADVISORY_LOCK_KEY},
            )
        snapshot = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == artifact.package_id,
                FinalCutPackageSnapshotModel.status == "preparing",
            )
            .with_for_update()
        )
        if snapshot is None or snapshot.build_lease_id != artifact.lease_id:
            return "skipped"
        expected_path = contained_path(Path(f"{snapshot.id}.zip"), self.settings.package_root)
        artifact_path = contained_path(artifact.storage_path, self.settings.package_root)
        artifact_canonical_path = contained_path(
            artifact.canonical_path,
            self.settings.package_root,
        )
        if artifact_canonical_path != expected_path:
            return "artifact_invalid"
        expected_staging_path = package_build_staging_path(
            self.settings.package_root,
            snapshot.id,
            artifact.lease_id,
        )
        if artifact_path != expected_staging_path:
            return "artifact_invalid"
        try:
            with pin_regular_file(artifact_path, self.settings.package_root) as pinned:
                if (
                    pinned is None
                    or not pinned.exists
                    or pinned.device != artifact.device
                    or pinned.inode != artifact.inode
                    or pinned.size != artifact.storage_bytes
                ):
                    return "artifact_invalid"
        except (OSError, UnsafeFilePathError):
            return "artifact_invalid"
        other_reserved_bytes = (
            self.session.scalar(
                select(func.coalesce(func.sum(FinalCutPackageSnapshotModel.storage_bytes), 0)).where(
                    FinalCutPackageSnapshotModel.id != snapshot.id,
                    FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
                )
            )
            or 0
        )
        if int(other_reserved_bytes) + artifact.storage_bytes > self.settings.max_package_storage_bytes:
            return "quota_exceeded"
        pending_reserved_bytes = pending_package_storage_reservation_bytes(
            self.session,
            exclude_package_id=snapshot.id,
        )
        try:
            ensure_package_filesystem_headroom(
                self.session,
                self.settings,
                package_future_reserved_bytes=pending_reserved_bytes,
                requested_bytes=0,
            )
        except ReviewError as exc:
            return "storage_unavailable" if exc.code == "STORAGE_UNAVAILABLE" else "quota_exceeded"
        try:
            with pin_managed_root(self.settings.package_root) as (_package_root, package_root_fd):
                staging_metadata = os.stat(
                    artifact_path.name,
                    dir_fd=package_root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(staging_metadata.st_mode)
                    or staging_metadata.st_dev != artifact.device
                    or staging_metadata.st_ino != artifact.inode
                    or staging_metadata.st_size != artifact.storage_bytes
                ):
                    return "artifact_invalid"
                try:
                    canonical_metadata = os.stat(
                        expected_path.name,
                        dir_fd=package_root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(canonical_metadata.st_mode):
                        return "artifact_invalid"
                os.replace(
                    artifact_path.name,
                    expected_path.name,
                    src_dir_fd=package_root_fd,
                    dst_dir_fd=package_root_fd,
                )
                published_metadata = os.stat(
                    expected_path.name,
                    dir_fd=package_root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(published_metadata.st_mode)
                    or published_metadata.st_dev != artifact.device
                    or published_metadata.st_ino != artifact.inode
                    or published_metadata.st_size != artifact.storage_bytes
                ):
                    raise OSError("published package identity changed during atomic replacement")
                os.fsync(package_root_fd)
        except (OSError, UnsafeFilePathError):
            return "artifact_invalid"
        snapshot.sha256 = artifact.sha256
        snapshot.storage_bytes = artifact.storage_bytes
        snapshot.storage_reclaimed_at = None
        snapshot.status = "ready"
        snapshot.next_build_attempt_at = None
        snapshot.build_lease_id = None
        snapshot.build_lease_expires_at = None
        snapshot.updated_at = utcnow()
        self.session.flush()
        self._event(
            "review.package.ready",
            context,
            snapshot.project_ref_id,
            "package",
            snapshot.id,
            1,
            package_id=snapshot.id,
        )
        return "ready"

    def record_package_build_failure(
        self,
        package_id: str,
        context: ExecutionContext,
        error_code: str,
        *,
        lease_id: str,
        retryable: bool,
        storage_reclaimed: bool,
    ) -> str:
        snapshot = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.status == "preparing",
            )
            .with_for_update()
        )
        if snapshot is None or snapshot.build_lease_id != lease_id:
            return "skipped"
        now = utcnow()
        snapshot.updated_at = now
        attempt_failure = {
            "error_code": error_code,
            "attempt": snapshot.build_attempts,
            "retryable": retryable,
            "storage_reclaimed": storage_reclaimed,
        }
        if retryable and snapshot.build_attempts < self.settings.package_worker_max_attempts:
            snapshot.next_build_attempt_at = now + timedelta(seconds=self.settings.package_worker_retry_delay_seconds)
            snapshot.build_lease_id = None
            snapshot.build_lease_expires_at = None
            snapshot.failure_details = {
                "error_code": "PACKAGE_BUILD_RETRY_SCHEDULED",
                "last_failure": attempt_failure,
            }
            self.session.flush()
            return "retry"
        snapshot.status = "failed"
        if storage_reclaimed:
            snapshot.storage_bytes = 0
            snapshot.storage_reclaimed_at = now
        snapshot.sha256 = None
        snapshot.next_build_attempt_at = None
        snapshot.build_lease_id = None
        snapshot.build_lease_expires_at = None
        snapshot.failure_details = attempt_failure
        self.session.flush()
        self._event(
            "review.package.failed",
            context,
            snapshot.project_ref_id,
            "package",
            snapshot.id,
            1,
            package_id=snapshot.id,
            payload={"error_code": error_code},
        )
        return "failed"

    def package_dto(self, package: FinalCutPackageSnapshotModel) -> dict[str, Any]:
        return PackageSnapshotDTO(
            id=package.id,
            project_ref_id=package.project_ref_id,
            status=package.status,  # type: ignore[arg-type]
            package_filename=package.package_filename,
            expires_at=iso(package.expires_at),
            file_count=len(package.items),
            total_bytes=package.total_bytes,
            sha256=package.sha256,
            created_at=iso(package.created_at),
            updated_at=iso(package.updated_at),
            failure_details=package.failure_details,
            download_token=(PackageDownloadTokenSigner(self.settings).issue(package.project_ref_id, package.id) if package.status == "ready" else None),
            download_token_expires_at=(
                iso(
                    min(
                        aware(package.expires_at),
                        utcnow() + timedelta(seconds=self.settings.package_download_token_ttl_seconds),
                    )
                )
                if package.status == "ready"
                else None
            ),
            items=[PackageSnapshotItemDTO.model_validate(item) for item in package.items],
        ).model_dump(mode="json", exclude_none=True)

    def authorize_package_download_session(self, project_ref_id: str, package_id: str) -> tuple[str, datetime]:
        self._assert_project_visible(self._get_project(project_ref_id))
        package = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.project_ref_id == project_ref_id,
            )
            .with_for_update()
        )
        if package is None:
            raise not_found()
        now = utcnow()
        if aware(package.expires_at) <= now:
            raise ReviewError("PACKAGE_EXPIRED", "临时包已过期")
        if package.status != "ready":
            raise ReviewError("PACKAGE_NOT_READY", "临时包尚未就绪")
        if package.sha256 is None:
            raise ReviewError("STORAGE_UNAVAILABLE", "临时包缺少完整性摘要")
        token = secrets.token_urlsafe(32)
        expires_at = min(
            aware(package.expires_at),
            now + timedelta(seconds=self.settings.package_download_session_ttl_seconds),
        )
        package.download_session_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        package.download_session_expires_at = expires_at
        package.updated_at = now
        self.session.flush()
        return token, expires_at

    def begin_package_download(self, project_ref_id: str, package_id: str, session_token: str) -> dict[str, Any]:
        self._assert_project_visible(self._get_project(project_ref_id))
        package = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.project_ref_id == project_ref_id,
            )
            .with_for_update()
        )
        if package is None:
            raise not_found()
        now = utcnow()
        if aware(package.expires_at) <= now:
            raise ReviewError("PACKAGE_EXPIRED", "临时包已过期")
        candidate_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        if (
            package.download_session_hash is None
            or package.download_session_expires_at is None
            or aware(package.download_session_expires_at) <= now
            or not hmac.compare_digest(package.download_session_hash, candidate_hash)
        ):
            raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "包下载 session 无效或已过期")
        if package.download_lease_id is not None and package.download_lease_expires_at is not None and aware(package.download_lease_expires_at) > now:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "同一临时包已有下载进行中")
        if (
            package.last_download_finished_at is not None
            and aware(package.last_download_finished_at) + timedelta(seconds=self.settings.package_download_cooldown_seconds) > now
        ):
            raise ReviewError("RESOURCE_STATE_CONFLICT", "临时包下载过于频繁，请稍后重试")
        if package.status != "ready" or package.sha256 is None:
            raise ReviewError("PACKAGE_NOT_READY", "临时包尚未就绪")
        lease_id = secrets.token_hex(32)
        package.download_session_hash = None
        package.download_session_expires_at = None
        package.download_lease_id = lease_id
        package.download_lease_expires_at = now + timedelta(seconds=self.settings.package_download_lease_seconds)
        package.updated_at = now
        self.session.flush()
        return {
            "storage_path": package.storage_path,
            "package_filename": package.package_filename,
            "sha256": package.sha256,
            "lease_id": lease_id,
        }

    def renew_package_download_lease(self, package_id: str, lease_id: str) -> bool:
        package = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.download_lease_id == lease_id,
                FinalCutPackageSnapshotModel.status == "ready",
                FinalCutPackageSnapshotModel.storage_reclaimed_at.is_(None),
            )
            .with_for_update()
        )
        if package is None:
            return False
        now = utcnow()
        package.download_lease_expires_at = now + timedelta(seconds=self.settings.package_download_lease_seconds)
        package.updated_at = now
        self.session.flush()
        return True

    def release_package_download_lease(self, package_id: str, lease_id: str) -> bool:
        package = self.session.scalar(
            select(FinalCutPackageSnapshotModel)
            .where(
                FinalCutPackageSnapshotModel.id == package_id,
                FinalCutPackageSnapshotModel.download_lease_id == lease_id,
            )
            .with_for_update()
        )
        if package is None:
            return False
        now = utcnow()
        package.download_lease_id = None
        package.download_lease_expires_at = None
        package.last_download_finished_at = now
        package.updated_at = now
        self.session.flush()
        return True

    @staticmethod
    def _page_offset(page: int, page_size: int) -> int:
        if page < 1 or page_size < 1:
            raise ValueError("page and page_size must be positive")
        return (page - 1) * page_size

    def get_project_summary(self, project_ref_id: str) -> dict[str, Any]:
        """Return the project card state with a fixed number of batched queries."""
        project = self._get_project(project_ref_id)
        self._assert_project_visible(project)
        items = list(
            self.session.scalars(
                select(ReviewItemModel)
                .where(ReviewItemModel.project_ref_id == project_ref_id)
                .order_by(ReviewItemModel.created_at.desc(), ReviewItemModel.id)
            )
        )
        if not items:
            return ProjectSummaryDTO(
                project=ProjectDTO.model_validate(
                    self._project_dto(
                        project,
                        item_count=0,
                        finalized_count=0,
                    )
                ),
                items=[],
            ).model_dump(mode="json")

        item_ids = [item.id for item in items]
        current_version_ids = [
            item.current_version_id for item in items if item.current_version_id is not None
        ]
        current_versions = {
            version.id: version
            for version in self.session.scalars(
                select(ReviewVersionModel).where(
                    ReviewVersionModel.project_ref_id == project_ref_id,
                    ReviewVersionModel.id.in_(current_version_ids),
                )
            )
        }
        if len(current_versions) != len(current_version_ids):
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目当前版本引用不完整")

        issue_counts = {
            (review_item_id, status): int(count)
            for review_item_id, status, count in self.session.execute(
                select(
                    ReviewIssueModel.review_item_id,
                    ReviewIssueModel.status,
                    func.count(),
                )
                .where(
                    ReviewIssueModel.project_ref_id == project_ref_id,
                    ReviewIssueModel.review_item_id.in_(item_ids),
                    ReviewIssueModel.version_id.in_(current_version_ids),
                    ReviewIssueModel.deleted_at.is_(None),
                )
                .group_by(ReviewIssueModel.review_item_id, ReviewIssueModel.status)
            ).all()
        }
        issue_history_counts = {
            review_item_id: int(count)
            for review_item_id, count in self.session.execute(
                select(ReviewIssueModel.review_item_id, func.count())
                .where(
                    ReviewIssueModel.project_ref_id == project_ref_id,
                    ReviewIssueModel.review_item_id.in_(item_ids),
                )
                .group_by(ReviewIssueModel.review_item_id)
            ).all()
        }
        version_counts = {
            review_item_id: int(count)
            for review_item_id, count in self.session.execute(
                select(ReviewVersionModel.review_item_id, func.count())
                .where(
                    ReviewVersionModel.project_ref_id == project_ref_id,
                    ReviewVersionModel.review_item_id.in_(item_ids),
                )
                .group_by(ReviewVersionModel.review_item_id)
            ).all()
        }
        derivative_tasks = {
            (task.version_id, task.kind): task
            for task in self.session.scalars(
                select(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.project_ref_id == project_ref_id,
                    MediaDerivativeTaskModel.version_id.in_(current_version_ids),
                )
            )
        }
        asset_ids = {
            asset_id
            for version in current_versions.values()
            for asset_id in (version.playback_asset_id, version.thumbnail_asset_id)
            if asset_id is not None
        }
        files = {
            file.id: file
            for file in self.session.scalars(
                select(FileObjectModel).where(FileObjectModel.id.in_(asset_ids))
            )
        }

        finalizations = list(
            self.session.scalars(
                select(FinalizationRecordModel)
                .where(
                    FinalizationRecordModel.project_ref_id == project_ref_id,
                    FinalizationRecordModel.review_item_id.in_(item_ids),
                )
                .order_by(
                    FinalizationRecordModel.review_item_id,
                    case((FinalizationRecordModel.status == "active", 0), else_=1),
                    FinalizationRecordModel.finalized_at.desc(),
                    FinalizationRecordModel.id.desc(),
                )
            )
        )
        selected_finalizations: dict[str, FinalizationRecordModel] = {}
        finalization_counts: dict[str, int] = {}
        for finalization in finalizations:
            finalization_counts[finalization.review_item_id] = (
                finalization_counts.get(finalization.review_item_id, 0) + 1
            )
            selected_finalizations.setdefault(finalization.review_item_id, finalization)

        selected_revoked_ids = [
            finalization.id
            for finalization in selected_finalizations.values()
            if finalization.status == "revoked"
        ]
        invalidation_statuses: dict[str, list[str]] = {}
        if selected_revoked_ids:
            for finalization_id, cleanup_status in self.session.execute(
                select(
                    FinalizationPackageInvalidationModel.finalization_id,
                    FinalizationPackageInvalidationModel.cleanup_status,
                ).where(
                    FinalizationPackageInvalidationModel.finalization_id.in_(
                        selected_revoked_ids
                    )
                )
            ).all():
                invalidation_statuses.setdefault(finalization_id, []).append(
                    cleanup_status
                )

        summary_items: list[ProjectSummaryItemDTO] = []
        for item in items:
            current_version_id = item.current_version_id
            if current_version_id is None:
                raise ReviewError("RESOURCE_STATE_CONFLICT", "审阅条目缺少当前版本")
            version = current_versions[current_version_id]
            playback_status = self._summary_derivative_status(
                version,
                "playback_faststart",
                derivative_tasks.get((version.id, "playback_faststart")),
                files.get(version.playback_asset_id or ""),
            )
            thumbnail_status = self._summary_derivative_status(
                version,
                "thumbnail",
                derivative_tasks.get((version.id, "thumbnail")),
                files.get(version.thumbnail_asset_id or ""),
            )
            selected_finalization = selected_finalizations.get(item.id)
            cleanup_status = self._revocation_cleanup_status(
                selected_finalization,
                invalidation_statuses,
            )
            bulk_delete_reason = self._bulk_delete_reason(
                project,
                item,
                version_count=version_counts.get(item.id, 0),
                issue_count=issue_history_counts.get(item.id, 0),
                finalization_count=finalization_counts.get(item.id, 0),
            )
            playback_url = (
                f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{item.id}"
                f"/versions/{version.id}/stream"
                if playback_status == "ready"
                else None
            )
            thumbnail_url = (
                f"/api/v1/final-cut-review/projects/{project_ref_id}/items/{item.id}"
                f"/versions/{version.id}/thumbnail"
                if thumbnail_status == "ready"
                else None
            )
            summary_items.append(
                ProjectSummaryItemDTO(
                    id=item.id,
                    project_ref_id=item.project_ref_id,
                    item_code=item.item_code,
                    episode_no=item.episode_no,
                    title=item.title,
                    workflow_state=item.workflow_status,  # type: ignore[arg-type]
                    lock_version=item.lock_version,
                    current_version_id=version.id,
                    current_version=ProjectSummaryCurrentVersionDTO(
                        id=version.id,
                        version_no=version.version_no,
                        version_label=version.version_label,
                        duration_ms=version.duration_ms,
                        file_size=version.file_size,
                        playback_status=playback_status,  # type: ignore[arg-type]
                        playback_url=playback_url,
                        thumbnail_status=thumbnail_status,  # type: ignore[arg-type]
                        thumbnail_url=thumbnail_url,
                    ),
                    unresolved_current_version_count=issue_counts.get(
                        (item.id, "unresolved"), 0
                    ),
                    finalization=(
                        ProjectSummaryFinalizationDTO(
                            id=selected_finalization.id,
                            version_id=selected_finalization.version_id,
                            version_no=selected_finalization.version_no,
                            status=selected_finalization.status,  # type: ignore[arg-type]
                            finalized_at=iso(selected_finalization.finalized_at),
                            revoked_at=(
                                iso(selected_finalization.revoked_at)
                                if selected_finalization.revoked_at is not None
                                else None
                            ),
                        )
                        if selected_finalization is not None
                        else None
                    ),
                    revocation_cleanup_status=cleanup_status,  # type: ignore[arg-type]
                    bulk_delete=ProjectSummaryBulkDeleteDTO(
                        eligible=bulk_delete_reason is None,
                        locked=bulk_delete_reason is not None,
                        reason=bulk_delete_reason,
                    ),
                )
            )
        return ProjectSummaryDTO(
            project=ProjectDTO.model_validate(
                self._project_dto(
                    project,
                    item_count=len(items),
                    finalized_count=sum(
                        item.workflow_status == "finalized" for item in items
                    ),
                ),
            ),
            items=summary_items,
        ).model_dump(mode="json")

    def _summary_derivative_status(
        self,
        version: ReviewVersionModel,
        kind: str,
        task: MediaDerivativeTaskModel | None,
        file: FileObjectModel | None,
    ) -> str:
        """Report persisted derivative state without touching remote storage.

        Summary is a project-card aggregation path. Physical file verification
        belongs to the authenticated stream/thumbnail endpoints, which remain
        fail-closed via ``_playback_file_is_ready``.
        """
        asset_id = (
            version.playback_asset_id
            if kind == "playback_faststart"
            else version.thumbnail_asset_id
        )
        if task is None:
            # Versions created before the derivative-task migration may already
            # reference a valid compatibility asset. Preserve that authoritative
            # persisted state without scheduling a bulk media conversion or
            # adding physical file I/O to the project aggregation query.
            return (
                "ready"
                if asset_id is not None and file is not None and file.id == asset_id
                else "failed"
            )
        if task.status in {"queued", "running"}:
            return "pending"
        if (
            task.status != "ready"
            or task.output_file_id is None
            or task.output_file_id != asset_id
            or file is None
            or file.id != asset_id
        ):
            return "failed"
        return "ready"

    @staticmethod
    def _revocation_cleanup_status(
        finalization: FinalizationRecordModel | None,
        invalidation_statuses: dict[str, list[str]],
    ) -> str:
        if finalization is None or finalization.status == "active":
            return "none"
        statuses = invalidation_statuses.get(finalization.id, [])
        if not statuses or all(status == "complete" for status in statuses):
            return "complete"
        if any(status == "failed" for status in statuses):
            return "failed"
        return "pending"

    @staticmethod
    def _bulk_delete_reason(
        project: ProjectRefModel,
        item: ReviewItemModel,
        *,
        version_count: int,
        issue_count: int,
        finalization_count: int,
    ) -> str | None:
        if project.lifecycle_status != "active":
            return "project_read_only"
        if item.workflow_status != "pending_review" or item.active_finalization_id:
            return "workflow_locked"
        if version_count != 1:
            return "version_history"
        if issue_count:
            return "issue_history"
        if finalization_count:
            return "finalization_history"
        return None

    def list_projects_page(
        self,
        *,
        page: int,
        page_size: int,
        allowed_project_ref_ids: tuple[str, ...] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = [ProjectRefModel.deleted_at.is_(None)]
        if allowed_project_ref_ids is not None:
            filters.append(ProjectRefModel.id.in_(allowed_project_ref_ids))
        total = self.session.scalar(select(func.count()).select_from(ProjectRefModel).where(*filters)) or 0
        projects = self.session.scalars(
            select(ProjectRefModel)
            .where(*filters)
            .order_by(ProjectRefModel.created_at.desc(), ProjectRefModel.id)
            .offset(self._page_offset(page, page_size))
            .limit(page_size)
        ).all()
        project_ids = [project.id for project in projects]
        counts: dict[str, tuple[int, int]] = {}
        if project_ids:
            rows = self.session.execute(
                select(
                    ReviewItemModel.project_ref_id,
                    func.count(),
                    func.sum(case((ReviewItemModel.workflow_status == "finalized", 1), else_=0)),
                )
                .where(ReviewItemModel.project_ref_id.in_(project_ids))
                .group_by(ReviewItemModel.project_ref_id)
            ).all()
            counts = {project_ref_id: (int(item_count), int(finalized_count or 0)) for project_ref_id, item_count, finalized_count in rows}
        return [
            self._project_dto(
                project,
                item_count=counts.get(project.id, (0, 0))[0],
                finalized_count=counts.get(project.id, (0, 0))[1],
            )
            for project in projects
        ], int(total)

    def list_items_page(
        self,
        project_ref_id: str,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self._assert_project_visible(self._get_project(project_ref_id))
        filters = [ReviewItemModel.project_ref_id == project_ref_id]
        total = self.session.scalar(select(func.count()).select_from(ReviewItemModel).where(*filters)) or 0
        items = self.session.scalars(
            select(ReviewItemModel)
            .where(*filters)
            .order_by(ReviewItemModel.created_at.desc(), ReviewItemModel.id)
            .offset(self._page_offset(page, page_size))
            .limit(page_size)
        ).all()
        if not items:
            return [], int(total)

        item_ids = [item.id for item in items]
        current_version_ids = [item.current_version_id for item in items if item.current_version_id]
        current_versions = {
            version.id: version for version in self.session.scalars(select(ReviewVersionModel).where(ReviewVersionModel.id.in_(current_version_ids))).all()
        }
        issue_counts = {
            (review_item_id, status): int(count)
            for review_item_id, status, count in self.session.execute(
                select(ReviewIssueModel.review_item_id, ReviewIssueModel.status, func.count())
                .where(
                    ReviewIssueModel.project_ref_id == project_ref_id,
                    ReviewIssueModel.review_item_id.in_(item_ids),
                    ReviewIssueModel.version_id.in_(current_version_ids),
                    ReviewIssueModel.deleted_at.is_(None),
                )
                .group_by(ReviewIssueModel.review_item_id, ReviewIssueModel.status)
            ).all()
        }
        history_counts = {
            review_item_id: int(count)
            for review_item_id, count in self.session.execute(
                select(ReviewVersionModel.review_item_id, func.count())
                .where(ReviewVersionModel.review_item_id.in_(item_ids))
                .group_by(ReviewVersionModel.review_item_id)
            ).all()
        }

        data: list[dict[str, Any]] = []
        for item in items:
            current = current_versions.get(item.current_version_id or "")
            if current is None:
                raise not_found()
            data.append(
                self._item_dto(
                    item,
                    current=current,
                    unresolved=issue_counts.get((item.id, "unresolved"), 0),
                    resolved=issue_counts.get((item.id, "resolved"), 0),
                    history=history_counts.get(item.id, 0),
                )
            )
        return data, int(total)

    def list_versions_page(
        self,
        project_ref_id: str,
        review_item_id: str,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_item(project_ref_id, review_item_id)
        filters = [
            ReviewVersionModel.project_ref_id == project_ref_id,
            ReviewVersionModel.review_item_id == review_item_id,
        ]
        total = self.session.scalar(select(func.count()).select_from(ReviewVersionModel).where(*filters)) or 0
        versions = self.session.scalars(
            select(ReviewVersionModel).where(*filters).order_by(ReviewVersionModel.version_no).offset(self._page_offset(page, page_size)).limit(page_size)
        ).all()
        version_ids = [version.id for version in versions]
        derivative_tasks = {
            (task.version_id, task.kind): task
            for task in self.session.scalars(
                select(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.version_id.in_(version_ids)
                )
            ).all()
        } if version_ids else {}
        asset_ids = {
            asset_id
            for version in versions
            for asset_id in (version.playback_asset_id, version.thumbnail_asset_id)
            if asset_id
        }
        files = {
            file.id: file
            for file in self.session.scalars(
                select(FileObjectModel).where(FileObjectModel.id.in_(asset_ids))
            ).all()
        } if asset_ids else {}
        return [
            self._version_dto(
                version,
                playback_status=self._version_derivative_status(
                    version,
                    "playback_faststart",
                    derivative_tasks.get((version.id, "playback_faststart")),
                    files.get(version.playback_asset_id or ""),
                ),
                thumbnail_status=self._version_derivative_status(
                    version,
                    "thumbnail",
                    derivative_tasks.get((version.id, "thumbnail")),
                    files.get(version.thumbnail_asset_id or ""),
                ),
            )
            for version in versions
        ], int(total)

    def list_issues_page(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_version(project_ref_id, review_item_id, version_id)
        filters = [
            ReviewIssueModel.project_ref_id == project_ref_id,
            ReviewIssueModel.review_item_id == review_item_id,
            ReviewIssueModel.version_id == version_id,
            ReviewIssueModel.deleted_at.is_(None),
        ]
        total = self.session.scalar(select(func.count()).select_from(ReviewIssueModel).where(*filters)) or 0
        issues = self.session.scalars(
            select(ReviewIssueModel)
            .where(*filters)
            .order_by(
                case((ReviewIssueModel.status == "unresolved", 0), else_=1),
                ReviewIssueModel.timestamp_ms,
                ReviewIssueModel.issue_no,
            )
            .offset(self._page_offset(page, page_size))
            .limit(page_size)
        ).all()
        revision_ids = [issue.current_revision_id for issue in issues]
        revisions = {
            revision.id: revision
            for revision in self.session.scalars(select(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.id.in_(revision_ids))).all()
        }
        annotation_ids = [revision.annotation_set_id for revision in revisions.values() if revision.annotation_set_id is not None]
        annotations = {
            annotation.id: annotation
            for annotation in self.session.scalars(select(ReviewAnnotationSetModel).where(ReviewAnnotationSetModel.id.in_(annotation_ids))).all()
        }
        data: list[dict[str, Any]] = []
        for issue in issues:
            revision = revisions.get(issue.current_revision_id)
            if revision is None:
                raise not_found()
            data.append(
                self._issue_dto(
                    issue,
                    revision=revision,
                    annotation=annotations.get(revision.annotation_set_id or ""),
                )
            )
        return data, int(total)

    def list_revisions_page(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        issue_id: str,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_issue(project_ref_id, review_item_id, version_id, issue_id)
        filters = [ReviewIssueRevisionModel.issue_id == issue_id]
        total = self.session.scalar(select(func.count()).select_from(ReviewIssueRevisionModel).where(*filters)) or 0
        revisions = self.session.scalars(
            select(ReviewIssueRevisionModel)
            .where(*filters)
            .order_by(ReviewIssueRevisionModel.revision_no)
            .offset(self._page_offset(page, page_size))
            .limit(page_size)
        ).all()
        return [
            ReviewIssueRevisionDTO(
                id=revision.id,
                project_ref_id=revision.project_ref_id,
                review_item_id=revision.review_item_id,
                version_id=revision.version_id,
                issue_id=revision.issue_id,
                revision_no=revision.revision_no,
                content=revision.content,
                annotation_set_id=revision.annotation_set_id,
                created_at=iso(revision.created_at),
            ).model_dump(mode="json")
            for revision in revisions
        ], int(total)

    def list_messages_page(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        issue_id: str,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_issue(project_ref_id, review_item_id, version_id, issue_id)
        filters = [ReviewThreadMessageModel.issue_id == issue_id]
        total = self.session.scalar(select(func.count()).select_from(ReviewThreadMessageModel).where(*filters)) or 0
        messages = self.session.scalars(
            select(ReviewThreadMessageModel)
            .where(*filters)
            .order_by(ReviewThreadMessageModel.created_at, ReviewThreadMessageModel.id)
            .offset(self._page_offset(page, page_size))
            .limit(page_size)
        ).all()
        return [
            ThreadMessageDTO(
                id=message.id,
                project_ref_id=message.project_ref_id,
                review_item_id=message.review_item_id,
                version_id=message.version_id,
                issue_id=message.issue_id,
                content=message.content,
                created_at=iso(message.created_at),
            ).model_dump(mode="json")
            for message in messages
        ], int(total)

    def list_projects(self) -> list[dict[str, Any]]:
        return [
            self.project_dto(project)
            for project in self.session.scalars(select(ProjectRefModel).where(ProjectRefModel.deleted_at.is_(None)).order_by(ProjectRefModel.created_at.desc()))
        ]

    def list_items(self, project_ref_id: str) -> list[dict[str, Any]]:
        self._assert_project_visible(self._get_project(project_ref_id))
        return [
            self.item_dto(item)
            for item in self.session.scalars(
                select(ReviewItemModel).where(ReviewItemModel.project_ref_id == project_ref_id).order_by(ReviewItemModel.created_at.desc())
            )
        ]

    def list_versions(self, project_ref_id: str, review_item_id: str) -> list[dict[str, Any]]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_item(project_ref_id, review_item_id)
        return [
            self.version_dto(version)
            for version in self.session.scalars(
                select(ReviewVersionModel)
                .where(ReviewVersionModel.project_ref_id == project_ref_id, ReviewVersionModel.review_item_id == review_item_id)
                .order_by(ReviewVersionModel.version_no)
            )
        ]

    def list_issues(self, project_ref_id: str, review_item_id: str, version_id: str) -> list[dict[str, Any]]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_version(project_ref_id, review_item_id, version_id)
        return [
            self.issue_dto(issue)
            for issue in self.session.scalars(
                select(ReviewIssueModel)
                .where(
                    ReviewIssueModel.project_ref_id == project_ref_id,
                    ReviewIssueModel.review_item_id == review_item_id,
                    ReviewIssueModel.version_id == version_id,
                    ReviewIssueModel.deleted_at.is_(None),
                )
                .order_by(
                    case((ReviewIssueModel.status == "unresolved", 0), else_=1),
                    ReviewIssueModel.timestamp_ms,
                    ReviewIssueModel.issue_no,
                )
            )
        ]

    def list_revisions(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str) -> list[dict[str, Any]]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_issue(project_ref_id, review_item_id, version_id, issue_id)
        return [
            ReviewIssueRevisionDTO(
                id=revision.id,
                project_ref_id=revision.project_ref_id,
                review_item_id=revision.review_item_id,
                version_id=revision.version_id,
                issue_id=revision.issue_id,
                revision_no=revision.revision_no,
                content=revision.content,
                annotation_set_id=revision.annotation_set_id,
                created_at=iso(revision.created_at),
            ).model_dump(mode="json")
            for revision in self.session.scalars(
                select(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.issue_id == issue_id).order_by(ReviewIssueRevisionModel.revision_no)
            )
        ]

    def list_messages(self, project_ref_id: str, review_item_id: str, version_id: str, issue_id: str) -> list[dict[str, Any]]:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_issue(project_ref_id, review_item_id, version_id, issue_id)
        return [
            ThreadMessageDTO(
                id=msg.id,
                project_ref_id=msg.project_ref_id,
                review_item_id=msg.review_item_id,
                version_id=msg.version_id,
                issue_id=msg.issue_id,
                content=msg.content,
                created_at=iso(msg.created_at),
            ).model_dump(mode="json")
            for msg in self.session.scalars(
                select(ReviewThreadMessageModel).where(ReviewThreadMessageModel.issue_id == issue_id).order_by(ReviewThreadMessageModel.created_at)
            )
        ]

    def get_finalization(self, project_ref_id: str, review_item_id: str) -> dict[str, Any] | None:
        self._assert_project_visible(self._get_project(project_ref_id))
        self._get_item(project_ref_id, review_item_id)
        finalization = self.session.scalar(
            select(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project_ref_id,
                FinalizationRecordModel.review_item_id == review_item_id,
                FinalizationRecordModel.status == "active",
            )
        )
        return self.finalization_dto(finalization) if finalization else None

    def get_file_for_version(self, project_ref_id: str, review_item_id: str, version_id: str) -> FileObjectModel:
        self._assert_project_visible(self._get_project(project_ref_id))
        version = self._get_version(project_ref_id, review_item_id, version_id)
        return self._get_ready_derivative_file(version, "playback_faststart")

    def get_thumbnail_file_for_current_version(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
    ) -> FileObjectModel:
        self._assert_project_visible(self._get_project(project_ref_id))
        item = self._get_item(project_ref_id, review_item_id)
        version = self._get_version(project_ref_id, review_item_id, version_id)
        invariants.ensure_current_version(version.id, item.current_version_id or "")
        return self._get_ready_derivative_file(version, "thumbnail")

    def _get_ready_derivative_file(
        self,
        version: ReviewVersionModel,
        kind: str,
    ) -> FileObjectModel:
        asset_id = (
            version.playback_asset_id
            if kind == "playback_faststart"
            else version.thumbnail_asset_id
        )
        task = self.session.scalar(
            select(MediaDerivativeTaskModel).where(
                MediaDerivativeTaskModel.project_ref_id == version.project_ref_id,
                MediaDerivativeTaskModel.review_item_id == version.review_item_id,
                MediaDerivativeTaskModel.version_id == version.id,
                MediaDerivativeTaskModel.kind == kind,
            )
        )
        if task is not None and (
            task.status != "ready"
            or task.output_file_id is None
            or task.output_file_id != asset_id
        ):
            raise ReviewError("PLAYBACK_NOT_READY", "媒体衍生文件尚未就绪")
        if asset_id is None:
            raise ReviewError("PLAYBACK_NOT_READY", "媒体衍生文件尚未就绪")
        file = self.session.get(FileObjectModel, asset_id)
        if file is None or not self._playback_file_is_ready(file):
            raise ReviewError("PLAYBACK_NOT_READY", "媒体衍生文件尚未就绪")
        return file

    def requeue_missing_ready_derivative(
        self,
        project_ref_id: str,
        review_item_id: str,
        version_id: str,
        kind: str,
    ) -> bool:
        """Idempotently repair DB-ready/physical-missing derivative drift.

        Keep the immutable version pointer in place (including on finalized
        items), but return the task to the worker queue.  Locks follow the
        project -> item -> version -> task order shared by upload/delete and
        media publication.
        """

        self._get_project(project_ref_id, for_update=True)
        self._get_item(project_ref_id, review_item_id, for_update=True)
        version = self._get_version(
            project_ref_id,
            review_item_id,
            version_id,
            for_update=True,
        )
        task = self.session.scalar(
            select(MediaDerivativeTaskModel)
            .where(
                MediaDerivativeTaskModel.project_ref_id == project_ref_id,
                MediaDerivativeTaskModel.review_item_id == review_item_id,
                MediaDerivativeTaskModel.version_id == version_id,
                MediaDerivativeTaskModel.kind == kind,
            )
            .with_for_update()
        )
        asset_id = (
            version.playback_asset_id
            if kind == "playback_faststart"
            else version.thumbnail_asset_id
        )
        if (
            task is None
            or task.status != "ready"
            or asset_id is None
            or task.output_file_id != asset_id
        ):
            return False
        file = self.session.get(FileObjectModel, asset_id)
        if self._playback_file_is_ready(file):
            return False
        now = utcnow()
        task.status = "queued"
        task.attempts = 0
        task.next_attempt_at = now
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = "MEDIA_DERIVATIVE_ARTIFACT_MISSING"
        task.failure_details = {"stage": "authenticated_delivery_repair"}
        task.updated_at = now
        self.session.flush()
        return True

    def get_file_for_finalization(self, project_ref_id: str, review_item_id: str) -> tuple[FinalizationRecordModel, FileObjectModel]:
        self._assert_project_visible(self._get_project(project_ref_id))
        finalization = self.session.scalar(
            select(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project_ref_id,
                FinalizationRecordModel.review_item_id == review_item_id,
                FinalizationRecordModel.status == "active",
            )
        )
        if not finalization:
            raise not_found()
        file = self.session.get(FileObjectModel, finalization.original_file_id)
        if not file:
            raise ReviewError("PACKAGE_SOURCE_MISSING", "定稿源文件缺失")
        return finalization, file

    def record_finalized_original_download_requested(self, context: ExecutionContext, finalization: FinalizationRecordModel) -> None:
        self._event(
            "review.finalized_original.download_requested",
            context,
            finalization.project_ref_id,
            "finalization",
            finalization.id,
            1,
            review_item_id=finalization.review_item_id,
            version_id=finalization.version_id,
            finalization_id=finalization.id,
        )
        self.session.flush()

    def get_package(self, project_ref_id: str, package_id: str) -> FinalCutPackageSnapshotModel:
        self._assert_project_visible(self._get_project(project_ref_id))
        package = self.session.scalar(
            select(FinalCutPackageSnapshotModel).where(
                FinalCutPackageSnapshotModel.id == package_id, FinalCutPackageSnapshotModel.project_ref_id == project_ref_id
            )
        )
        if not package:
            raise not_found()
        if aware(package.expires_at) < utcnow():
            raise ReviewError("PACKAGE_EXPIRED", "临时包已过期")
        if package.status != "ready":
            return package
        try:
            with pin_regular_file(package.storage_path, self.settings.package_root) as pinned:
                if pinned is None or not pinned.exists:
                    raise ReviewError("PACKAGE_SOURCE_MISSING", "包文件缺失")
        except UnsafeFilePathError as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "包文件路径非法") from exc
        return package
