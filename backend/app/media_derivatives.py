from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Never

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra import database as database_module
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewItemModel,
    ReviewVersionModel,
    utcnow,
)
from backend.app.modules.review_media.service import MediaProbeResult, probe_media
from backend.app.safe_files import (
    UnsafeFilePathError,
    contained_path,
    ensure_private_directory,
    pin_managed_root,
    pin_regular_file,
    unlink_regular_file_if_identity,
)
from backend.app.settings import Settings, get_database_settings
from backend.app.telemetry_metrics import (
    observe_media_task,
    start_worker_metrics_server,
)

LOGGER = logging.getLogger(__name__)
SUPPORTED_KINDS = frozenset({"playback_faststart", "thumbnail"})
MEDIA_WORKER_BATCH_SIZE = 1
MEDIA_WORKER_MAX_SECONDS = 86_400
PROCESS_POLL_SECONDS = 1.0
PROCESS_TERMINATE_GRACE_SECONDS = 1.0
HASH_CHUNK_BYTES = 1024 * 1024
HASH_LEASE_REFRESH_SECONDS = 30.0
WORKER_HEARTBEAT_REFRESH_SECONDS = 30.0


class MediaDerivativeError(RuntimeError):
    def __init__(self, code: str, stage: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.retryable = retryable


class MediaDerivativeLeaseLost(MediaDerivativeError):
    def __init__(self) -> None:
        super().__init__("MEDIA_DERIVATIVE_LEASE_LOST", "lease", retryable=False)


@dataclass(frozen=True, slots=True)
class MediaDerivativeClaim:
    task_id: str
    lease_id: str
    kind: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    source_file_id: str
    source_storage_path: str
    source_filename: str
    source_mime_type: str
    source_size: int
    source_sha256: str
    source_owner_id: str
    source_owner_kind: str
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int


@dataclass(frozen=True, slots=True)
class PreparedDerivative:
    task_id: str
    lease_id: str
    kind: str
    output_file_id: str
    storage_path: Path
    staging_path: Path | None
    sha256: str
    size: int
    device: int
    inode: int
    ctime_ns: int
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    mime_type: str
    published_new: bool = False


@dataclass(slots=True)
class _ProcessOutputState:
    limit: int
    total: int = 0
    too_large: bool = False
    failed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@contextmanager
def _worker_session() -> Iterator[Session]:
    with database_module.SessionLocal() as session:
        database_module.require_runtime_participant_session(session)
        yield session


def _output_file_id(task_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"media-derivative-v1\0{task_id}\0{kind}".encode()).hexdigest()
    return f"media_{digest[:32]}"


def _output_extension(kind: str) -> str:
    if kind == "playback_faststart":
        return ".mp4"
    if kind == "thumbnail":
        return ".jpg"
    raise MediaDerivativeError("MEDIA_DERIVATIVE_KIND_INVALID", "claim", retryable=False)


def _hash_descriptor(
    descriptor: int,
    renew_lease: Callable[[], bool] | None = None,
) -> str:
    digest = hashlib.sha256()
    offset = 0
    next_renewal = time.monotonic() + HASH_LEASE_REFRESH_SECONDS
    while True:
        chunk = os.pread(descriptor, HASH_CHUNK_BYTES, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
        if renew_lease is not None and time.monotonic() >= next_renewal:
            if not renew_lease():
                raise MediaDerivativeLeaseLost()
            next_renewal = time.monotonic() + HASH_LEASE_REFRESH_SECONDS
    return digest.hexdigest()


def _read_box_header(descriptor: int, offset: int, file_size: int) -> tuple[int, bytes]:
    header = os.pread(descriptor, 16, offset)
    if len(header) < 8:
        raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)
    short_size = int.from_bytes(header[0:4], "big")
    box_type = header[4:8]
    if short_size == 0:
        box_size = file_size - offset
    elif short_size == 1:
        if len(header) < 16:
            raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)
        box_size = int.from_bytes(header[8:16], "big")
        if box_size < 16:
            raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)
    else:
        box_size = short_size
        if box_size < 8:
            raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)
    if box_size > file_size - offset:
        raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)
    return box_size, box_type


def _assert_faststart(descriptor: int) -> None:
    file_size = os.fstat(descriptor).st_size
    offset = 0
    moov_offset: int | None = None
    mdat_offset: int | None = None
    while offset < file_size:
        box_size, box_type = _read_box_header(descriptor, offset, file_size)
        if box_type == b"moov" and moov_offset is None:
            moov_offset = offset
        elif box_type == b"mdat" and mdat_offset is None:
            mdat_offset = offset
        offset += box_size
    if offset != file_size or moov_offset is None or mdat_offset is None or moov_offset >= mdat_offset:
        raise MediaDerivativeError("MEDIA_FASTSTART_INVALID", "validate", retryable=False)


_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _jpeg_dimensions(descriptor: int) -> tuple[int, int]:
    file_size = os.fstat(descriptor).st_size
    if file_size < 12 or os.pread(descriptor, 2, 0) != b"\xff\xd8":
        raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
    if os.pread(descriptor, 2, file_size - 2) != b"\xff\xd9":
        raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
    offset = 2
    while offset + 4 <= file_size:
        prefix = os.pread(descriptor, 2, offset)
        if len(prefix) != 2 or prefix[0] != 0xFF:
            raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
        marker = prefix[1]
        offset += 2
        while marker == 0xFF:
            marker_bytes = os.pread(descriptor, 1, offset)
            if not marker_bytes:
                raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
            marker = marker_bytes[0]
            offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length_bytes = os.pread(descriptor, 2, offset)
        if len(length_bytes) != 2:
            raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
        segment_length = int.from_bytes(length_bytes, "big")
        if segment_length < 2 or offset + segment_length > file_size:
            raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
        if marker in _JPEG_SOF_MARKERS:
            dimensions = os.pread(descriptor, 5, offset + 2)
            if len(dimensions) != 5:
                raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
            height = int.from_bytes(dimensions[1:3], "big")
            width = int.from_bytes(dimensions[3:5], "big")
            if width <= 0 or height <= 0:
                raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)
            return width, height
        if marker == 0xDA:
            break
        offset += segment_length
    raise MediaDerivativeError("MEDIA_THUMBNAIL_INVALID", "validate", retryable=False)


def _probe_matches_source(probe: MediaProbeResult, claim: MediaDerivativeClaim) -> bool:
    return (
        probe.width == claim.width
        and probe.height == claim.height
        and probe.fps_num == claim.fps_num
        and probe.fps_den == claim.fps_den
        and abs(probe.duration_ms - claim.duration_ms) <= 1_000
    )


def _ffmpeg_command(
    claim: MediaDerivativeClaim,
    settings: Settings,
    source_descriptor: int,
    output_path: Path,
) -> list[str]:
    common = [
        settings.media_transform_command,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-protocol_whitelist",
        "fd,file",
        "-fd",
        str(source_descriptor),
        "-i",
        "fd:",
    ]
    if claim.kind == "playback_faststart":
        return [
            *common,
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output_path),
        ]
    if claim.kind == "thumbnail":
        width = settings.media_thumbnail_width
        return [
            *common,
            "-map",
            "0:v:0",
            "-vf",
            f"select=eq(n\\,0),scale=min({width}\\,iw):-2",
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-q:v",
            "2",
            "-f",
            "image2",
            str(output_path),
        ]
    raise MediaDerivativeError("MEDIA_DERIVATIVE_KIND_INVALID", "transform", retryable=False)


def _signal_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _read_process_output(
    stream: BinaryIO,
    process: subprocess.Popen[bytes],
    state: _ProcessOutputState,
) -> None:
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            with state.lock:
                state.total += len(chunk)
                if state.total > state.limit:
                    state.too_large = True
                    _signal_process_group(process, signal.SIGKILL)
                    return
    except (OSError, ValueError):
        with state.lock:
            state.failed = True
        _signal_process_group(process, signal.SIGKILL)


def _run_ffmpeg(
    command: list[str],
    settings: Settings,
    source_descriptor: int,
    output_descriptor: int,
    renew_lease: Callable[[], bool],
) -> None:
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            pass_fds=(source_descriptor, output_descriptor),
            start_new_session=True,
        )
    except OSError:
        raise MediaDerivativeError("MEDIA_TRANSFORM_UNAVAILABLE", "transform", retryable=True) from None
    if process.stdout is None or process.stderr is None:
        _signal_process_group(process, signal.SIGKILL)
        process.wait()
        raise MediaDerivativeError("MEDIA_TRANSFORM_UNAVAILABLE", "transform", retryable=True)

    state = _ProcessOutputState(limit=settings.media_transform_output_limit_bytes)
    readers = [
        threading.Thread(
            target=_read_process_output,
            args=(stream, process, state),
            daemon=True,
            name=f"media-transform-{name}",
        )
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + settings.media_transform_timeout_seconds
    renew_interval = max(1.0, min(30.0, settings.media_worker_lease_seconds / 3))
    next_renewal = time.monotonic() + renew_interval
    timed_out = False
    lease_lost = False
    termination_failed = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                _signal_process_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _signal_process_group(process, signal.SIGKILL)
                break
            if now >= next_renewal:
                if not renew_lease():
                    lease_lost = True
                    _signal_process_group(process, signal.SIGKILL)
                    break
                next_renewal = now + renew_interval
            try:
                process.wait(timeout=min(PROCESS_POLL_SECONDS, max(0.01, deadline - now)))
            except subprocess.TimeoutExpired:
                continue
        try:
            process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=PROCESS_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                termination_failed = True
    finally:
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            stream.close()
        for reader in readers:
            if reader.is_alive():
                reader.join(timeout=1)

    if lease_lost:
        raise MediaDerivativeLeaseLost()
    if termination_failed:
        raise MediaDerivativeError("MEDIA_TRANSFORM_TERMINATION_FAILED", "transform", retryable=True)
    if timed_out:
        raise MediaDerivativeError("MEDIA_TRANSFORM_TIMEOUT", "transform", retryable=True)
    if any(reader.is_alive() for reader in readers) or state.failed:
        raise MediaDerivativeError("MEDIA_TRANSFORM_OUTPUT_UNAVAILABLE", "transform", retryable=True)
    if state.too_large:
        raise MediaDerivativeError("MEDIA_TRANSFORM_OUTPUT_LIMIT", "transform", retryable=True)
    if process.returncode != 0:
        raise MediaDerivativeError("MEDIA_TRANSFORM_FAILED", "transform", retryable=True)


def _claim_task(
    session: Session,
    settings: Settings,
    task_id: str | None,
) -> tuple[str, MediaDerivativeClaim | None]:
    now = utcnow()
    eligible = or_(
        and_(
            MediaDerivativeTaskModel.status == "queued",
            or_(
                MediaDerivativeTaskModel.next_attempt_at.is_(None),
                MediaDerivativeTaskModel.next_attempt_at <= now,
            ),
        ),
        and_(
            MediaDerivativeTaskModel.status == "running",
            MediaDerivativeTaskModel.lease_expires_at <= now,
        ),
    )
    statement = (
        select(
            MediaDerivativeTaskModel.id,
            MediaDerivativeTaskModel.project_ref_id,
            MediaDerivativeTaskModel.review_item_id,
            MediaDerivativeTaskModel.version_id,
        )
        .where(eligible)
        .order_by(MediaDerivativeTaskModel.created_at, MediaDerivativeTaskModel.id)
        .limit(1)
    )
    if task_id is not None:
        statement = statement.where(MediaDerivativeTaskModel.id == task_id)
    candidate = session.execute(statement).one_or_none()
    if candidate is None:
        existing = session.get(MediaDerivativeTaskModel, task_id) if task_id is not None else None
        return (existing.status if existing is not None else "skipped"), None
    # Use the same project -> item -> version -> task lock order as upload V2,
    # item deletion, and derivative publication.  The candidate lookup is
    # intentionally unlocked; eligibility is checked again after the locks.
    project = session.scalar(
        select(ProjectRefModel)
        .where(ProjectRefModel.id == candidate.project_ref_id)
        .with_for_update()
    )
    item = session.scalar(
        select(ReviewItemModel)
        .where(
            ReviewItemModel.id == candidate.review_item_id,
            ReviewItemModel.project_ref_id == candidate.project_ref_id,
        )
        .with_for_update()
    )
    version = session.scalar(
        select(ReviewVersionModel)
        .where(
            ReviewVersionModel.id == candidate.version_id,
            ReviewVersionModel.project_ref_id == candidate.project_ref_id,
            ReviewVersionModel.review_item_id == candidate.review_item_id,
        )
        .with_for_update()
    )
    task = session.scalar(
        select(MediaDerivativeTaskModel)
        .where(MediaDerivativeTaskModel.id == candidate.id)
        .with_for_update()
    )
    if project is None or item is None or version is None or task is None:
        return "skipped", None
    task_is_eligible = (
        task.status == "queued"
        and not _lease_deadline_is_future(task.next_attempt_at, now)
    ) or (
        task.status == "running"
        and task.lease_expires_at is not None
        and not _lease_deadline_is_future(task.lease_expires_at, now)
    )
    if not task_is_eligible:
        return task.status, None
    if task.kind not in SUPPORTED_KINDS:
        task.status = "failed"
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = "MEDIA_DERIVATIVE_KIND_INVALID"
        task.failure_details = {"stage": "claim"}
        task.updated_at = now
        return "failed", None
    if task.attempts >= settings.media_worker_max_attempts:
        task.status = "failed"
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = "MEDIA_DERIVATIVE_ATTEMPTS_EXHAUSTED"
        task.failure_details = {"stage": "claim"}
        task.updated_at = now
        return "failed", None

    source = session.get(FileObjectModel, version.original_file_id) if version is not None else None
    if version is None or source is None:
        task.status = "failed"
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = "MEDIA_DERIVATIVE_SOURCE_MISSING"
        task.failure_details = {"stage": "claim"}
        task.updated_at = now
        return "failed", None
    if (
        version.sha256 != source.sha256
        or version.file_size != source.file_size
        or version.duration_ms != source.duration_ms
        or version.width != source.width
        or version.height != source.height
        or version.fps_num != source.fps_num
        or version.fps_den != source.fps_den
    ):
        task.status = "failed"
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = "MEDIA_DERIVATIVE_SOURCE_METADATA_MISMATCH"
        task.failure_details = {"stage": "claim"}
        task.updated_at = now
        return "failed", None
    if task.kind == "thumbnail":
        if (
            item is None
            or not version.is_current
            or item.current_version_id != version.id
        ):
            task.status = "failed"
            task.lease_id = None
            task.lease_expires_at = None
            task.error_code = "MEDIA_DERIVATIVE_VERSION_NOT_CURRENT"
            task.failure_details = {"stage": "claim"}
            task.updated_at = now
            return "failed", None

    lease_id = f"lease_{uuid.uuid4().hex}"
    task.status = "running"
    task.attempts += 1
    task.next_attempt_at = None
    task.lease_id = lease_id
    task.lease_expires_at = now + timedelta(seconds=settings.media_worker_lease_seconds)
    task.error_code = None
    task.failure_details = None
    task.updated_at = now
    session.flush()
    return (
        "claimed",
        MediaDerivativeClaim(
            task_id=task.id,
            lease_id=lease_id,
            kind=task.kind,
            project_ref_id=task.project_ref_id,
            review_item_id=task.review_item_id,
            version_id=task.version_id,
            source_file_id=source.id,
            source_storage_path=source.storage_path,
            source_filename=source.original_filename,
            source_mime_type=source.mime_type,
            source_size=source.file_size,
            source_sha256=source.sha256,
            source_owner_id=source.owner_principal_id,
            source_owner_kind=source.owner_principal_kind,
            duration_ms=source.duration_ms,
            width=source.width,
            height=source.height,
            fps_num=source.fps_num,
            fps_den=source.fps_den,
        ),
    )


def _renew_lease(claim: MediaDerivativeClaim, settings: Settings) -> bool:
    with _worker_session() as session:
        task = session.scalar(
            select(MediaDerivativeTaskModel)
            .where(MediaDerivativeTaskModel.id == claim.task_id)
            .with_for_update()
        )
        now = utcnow()
        if (
            task is None
            or task.status != "running"
            or task.lease_id != claim.lease_id
            or not _lease_deadline_is_future(task.lease_expires_at, now)
        ):
            session.rollback()
            return False
        if claim.kind == "thumbnail":
            version = session.get(ReviewVersionModel, claim.version_id)
            item = session.get(ReviewItemModel, claim.review_item_id)
            if (
                version is None
                or item is None
                or not version.is_current
                or item.current_version_id != version.id
            ):
                session.rollback()
                return False
        task.lease_expires_at = now + timedelta(seconds=settings.media_worker_lease_seconds)
        task.updated_at = now
        session.commit()
        return True


def _validate_derivative(
    descriptor: int,
    claim: MediaDerivativeClaim,
    settings: Settings,
    source_av_streams: tuple[tuple[str, str, int, int], ...],
) -> tuple[int, int, int, int, int]:
    if claim.kind == "playback_faststart":
        _assert_faststart(descriptor)
        if not _renew_lease(claim, settings):
            raise MediaDerivativeLeaseLost()
        try:
            probe = probe_media(descriptor, settings)
        except Exception as exc:
            code = getattr(exc, "code", "MEDIA_DERIVATIVE_PROBE_FAILED")
            raise MediaDerivativeError(str(code), "validate", retryable=True) from None
        if not _probe_matches_source(probe, claim):
            raise MediaDerivativeError("MEDIA_DERIVATIVE_PROBE_MISMATCH", "validate", retryable=False)
        if (
            not source_av_streams
            or sorted(probe.av_streams) != sorted(source_av_streams)
        ):
            raise MediaDerivativeError("MEDIA_DERIVATIVE_STREAM_MISMATCH", "validate", retryable=False)
        return probe.duration_ms, probe.width, probe.height, probe.fps_num, probe.fps_den
    width, height = _jpeg_dimensions(descriptor)
    if width > settings.media_thumbnail_width or height <= 0:
        raise MediaDerivativeError("MEDIA_THUMBNAIL_DIMENSIONS_INVALID", "validate", retryable=False)
    return claim.duration_ms, width, height, claim.fps_num, claim.fps_den


def _lease_deadline_is_future(deadline: datetime | None, now: datetime) -> bool:
    if deadline is None:
        return False
    deadline_value = deadline
    now_value = now
    if deadline_value.tzinfo is None and now_value.tzinfo is not None:
        now_value = now_value.replace(tzinfo=None)
    elif deadline_value.tzinfo is not None and now_value.tzinfo is None:
        deadline_value = deadline_value.replace(tzinfo=None)
    return bool(deadline_value > now_value)


def _canonical_source_path(claim: MediaDerivativeClaim, settings: Settings) -> Path:
    source_path = contained_path(claim.source_storage_path, settings.storage_root)
    expected = contained_path(Path("files") / claim.source_file_id, settings.storage_root)
    if source_path != expected:
        raise MediaDerivativeError("MEDIA_DERIVATIVE_SOURCE_PATH_INVALID", "source", retryable=False)
    return source_path


def _prepared_from_descriptor(
    descriptor: int,
    *,
    claim: MediaDerivativeClaim,
    settings: Settings,
    output_file_id: str,
    storage_path: Path,
    staging_path: Path | None,
    source_av_streams: tuple[tuple[str, str, int, int], ...],
) -> PreparedDerivative:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise MediaDerivativeError("MEDIA_DERIVATIVE_OUTPUT_INVALID", "validate", retryable=False)
    duration_ms, width, height, fps_num, fps_den = _validate_derivative(
        descriptor,
        claim,
        settings,
        source_av_streams,
    )
    return PreparedDerivative(
        task_id=claim.task_id,
        lease_id=claim.lease_id,
        kind=claim.kind,
        output_file_id=output_file_id,
        storage_path=storage_path,
        staging_path=staging_path,
        sha256=_hash_descriptor(descriptor, lambda: _renew_lease(claim, settings)),
        size=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        ctime_ns=metadata.st_ctime_ns,
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        mime_type="video/mp4" if claim.kind == "playback_faststart" else "image/jpeg",
    )


def _reuse_published_derivative(
    claim: MediaDerivativeClaim,
    settings: Settings,
    output_file_id: str,
    output_path: Path,
    source_av_streams: tuple[tuple[str, str, int, int], ...],
) -> PreparedDerivative | None:
    with pin_regular_file(output_path, settings.storage_root) as pinned:
        if pinned is None or not pinned.exists:
            return None
        with pinned.open_readonly() as handle:
            return _prepared_from_descriptor(
                handle.fileno(),
                claim=claim,
                settings=settings,
                output_file_id=output_file_id,
                storage_path=output_path,
                staging_path=None,
                source_av_streams=source_av_streams,
            )


def _prepare_derivative(claim: MediaDerivativeClaim, settings: Settings) -> PreparedDerivative:
    file_root = ensure_private_directory(settings.storage_root, "files")
    output_file_id = _output_file_id(claim.task_id, claim.kind)
    output_path = contained_path(Path("files") / output_file_id, settings.storage_root)
    source_path = _canonical_source_path(claim, settings)
    with pin_regular_file(source_path, settings.storage_root) as source:
        if source is None or not source.exists or source.size != claim.source_size:
            raise MediaDerivativeError("MEDIA_DERIVATIVE_SOURCE_MISSING", "source", retryable=False)
        with source.open_readonly() as source_handle:
            if (
                _hash_descriptor(
                    source_handle.fileno(),
                    lambda: _renew_lease(claim, settings),
                )
                != claim.source_sha256
            ):
                raise MediaDerivativeError("MEDIA_DERIVATIVE_SOURCE_HASH_MISMATCH", "source", retryable=False)
            source_av_streams: tuple[tuple[str, str, int, int], ...] = ()
            if claim.kind == "playback_faststart":
                try:
                    source_probe = probe_media(source_handle.fileno(), settings)
                except Exception:
                    raise MediaDerivativeError(
                        "MEDIA_DERIVATIVE_SOURCE_PROBE_FAILED",
                        "source",
                        retryable=True,
                    ) from None
                if not _probe_matches_source(source_probe, claim) or not source_probe.av_streams:
                    raise MediaDerivativeError(
                        "MEDIA_DERIVATIVE_SOURCE_METADATA_MISMATCH",
                        "source",
                        retryable=False,
                    )
                source_av_streams = source_probe.av_streams
            existing = _reuse_published_derivative(
                claim,
                settings,
                output_file_id,
                output_path,
                source_av_streams,
            )
            if existing is not None:
                if (
                    _hash_descriptor(
                        source_handle.fileno(),
                        lambda: _renew_lease(claim, settings),
                    )
                    != claim.source_sha256
                ):
                    raise MediaDerivativeError("MEDIA_DERIVATIVE_SOURCE_CHANGED", "source", retryable=False)
                return existing

            staging_name = f".{output_file_id}.staging{_output_extension(claim.kind)}.tmp"
            staging_path = contained_path(Path("files") / staging_name, settings.storage_root)
            with pin_managed_root(file_root) as (_root, root_fd):
                _cleanup_stale_staging(root_fd, staging_name)
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
                output_descriptor = os.open(staging_name, flags, 0o600, dir_fd=root_fd)
                try:
                    output_metadata = os.fstat(output_descriptor)
                    if not stat.S_ISREG(output_metadata.st_mode):
                        raise MediaDerivativeError("MEDIA_DERIVATIVE_OUTPUT_INVALID", "transform", retryable=False)
                    command = _ffmpeg_command(
                        claim,
                        settings,
                        source_handle.fileno(),
                        staging_path,
                    )
                    _run_ffmpeg(
                        command,
                        settings,
                        source_handle.fileno(),
                        output_descriptor,
                        lambda: _renew_lease(claim, settings),
                    )
                    named_metadata = os.stat(
                        staging_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                    descriptor_metadata = os.fstat(output_descriptor)
                    if (
                        not stat.S_ISREG(named_metadata.st_mode)
                        or named_metadata.st_dev != descriptor_metadata.st_dev
                        or named_metadata.st_ino != descriptor_metadata.st_ino
                    ):
                        raise MediaDerivativeError(
                            "MEDIA_DERIVATIVE_STAGING_IDENTITY_CHANGED",
                            "transform",
                            retryable=False,
                        )
                    os.fsync(output_descriptor)
                    prepared = _prepared_from_descriptor(
                        output_descriptor,
                        claim=claim,
                        settings=settings,
                        output_file_id=output_file_id,
                        storage_path=output_path,
                        staging_path=staging_path,
                        source_av_streams=source_av_streams,
                    )
                    if (
                        _hash_descriptor(
                            source_handle.fileno(),
                            lambda: _renew_lease(claim, settings),
                        )
                        != claim.source_sha256
                    ):
                        raise MediaDerivativeError("MEDIA_DERIVATIVE_SOURCE_CHANGED", "source", retryable=False)
                    return prepared
                except BaseException:
                    metadata = os.fstat(output_descriptor)
                    os.close(output_descriptor)
                    output_descriptor = -1
                    try:
                        unlink_regular_file_if_identity(
                            staging_path,
                            settings.storage_root,
                            device=metadata.st_dev,
                            inode=metadata.st_ino,
                            missing_ok=True,
                        )
                    except (OSError, UnsafeFilePathError):
                        LOGGER.error("media_derivative_staging_cleanup_failed")
                    raise
                finally:
                    if output_descriptor >= 0:
                        os.close(output_descriptor)


def _discard_staging(prepared: PreparedDerivative, settings: Settings) -> None:
    if prepared.staging_path is None:
        return
    try:
        unlink_regular_file_if_identity(
            prepared.staging_path,
            settings.storage_root,
            device=prepared.device,
            inode=prepared.inode,
            ctime_ns=prepared.ctime_ns,
            size=prepared.size,
            missing_ok=True,
        )
    except (OSError, UnsafeFilePathError):
        LOGGER.error("media_derivative_staging_cleanup_failed")


def _cleanup_stale_staging(
    root_fd: int,
    staging_name: str,
) -> None:
    try:
        metadata = os.stat(staging_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise MediaDerivativeError(
            "MEDIA_DERIVATIVE_STAGING_UNSAFE",
            "storage",
            retryable=False,
        )
    os.unlink(staging_name, dir_fd=root_fd)
    os.fsync(root_fd)


def _lease_is_current(claim: MediaDerivativeClaim) -> bool:
    with _worker_session() as session:
        task = session.get(MediaDerivativeTaskModel, claim.task_id)
        return bool(
            task is not None
            and task.status == "running"
            and task.lease_id == claim.lease_id
            and _lease_deadline_is_future(task.lease_expires_at, utcnow())
        )


def _publish_file(
    prepared: PreparedDerivative,
    settings: Settings,
    renew_lease: Callable[[], bool] | None = None,
) -> PreparedDerivative:
    if prepared.staging_path is None:
        return prepared
    if renew_lease is not None and not renew_lease():
        raise MediaDerivativeLeaseLost()
    file_root = ensure_private_directory(settings.storage_root, "files")
    source_name = prepared.staging_path.name
    destination_name = prepared.storage_path.name
    published_new = False
    published_ctime_ns = prepared.ctime_ns
    try:
        with pin_managed_root(file_root) as (_root, root_fd):
            try:
                os.link(
                    source_name,
                    destination_name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
                published_new = True
                linked_metadata = os.stat(
                    destination_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(linked_metadata.st_mode)
                    or linked_metadata.st_dev != prepared.device
                    or linked_metadata.st_ino != prepared.inode
                ):
                    raise MediaDerivativeError(
                        "MEDIA_DERIVATIVE_PUBLISH_CHANGED",
                        "publish",
                        retryable=False,
                    )
                published_ctime_ns = linked_metadata.st_ctime_ns
                os.fsync(root_fd)
            except FileExistsError:
                with pin_regular_file(prepared.storage_path, settings.storage_root) as existing:
                    if existing is None or not existing.exists:
                        raise MediaDerivativeError(
                            "MEDIA_DERIVATIVE_PUBLISH_FAILED",
                            "publish",
                            retryable=True,
                        ) from None
                    with existing.open_readonly() as handle:
                        if _hash_descriptor(handle.fileno(), renew_lease) != prepared.sha256:
                            raise MediaDerivativeError(
                                "MEDIA_DERIVATIVE_PUBLISH_COLLISION",
                                "publish",
                                retryable=False,
                            ) from None
            if renew_lease is not None and not renew_lease():
                raise MediaDerivativeLeaseLost()
            os.unlink(source_name, dir_fd=root_fd)
            linked_metadata = os.stat(
                destination_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(linked_metadata.st_mode)
                or linked_metadata.st_dev != prepared.device
                or linked_metadata.st_ino != prepared.inode
            ):
                raise MediaDerivativeError(
                    "MEDIA_DERIVATIVE_PUBLISH_CHANGED",
                    "publish",
                    retryable=False,
                )
            published_ctime_ns = linked_metadata.st_ctime_ns
            os.fsync(root_fd)
        with pin_regular_file(prepared.storage_path, settings.storage_root) as published:
            if published is None or not published.exists:
                raise MediaDerivativeError(
                    "MEDIA_DERIVATIVE_PUBLISH_FAILED",
                    "publish",
                    retryable=True,
                )
            with published.open_readonly() as handle:
                metadata = os.fstat(handle.fileno())
                published_ctime_ns = metadata.st_ctime_ns
                if _hash_descriptor(handle.fileno(), renew_lease) != prepared.sha256:
                    raise MediaDerivativeError(
                        "MEDIA_DERIVATIVE_PUBLISH_CHANGED",
                        "publish",
                        retryable=False,
                    )
    except Exception:
        if published_new:
            unlink_regular_file_if_identity(
                prepared.storage_path,
                settings.storage_root,
                device=prepared.device,
                inode=prepared.inode,
                ctime_ns=published_ctime_ns,
                missing_ok=True,
            )
        raise
    return PreparedDerivative(
        task_id=prepared.task_id,
        lease_id=prepared.lease_id,
        kind=prepared.kind,
        output_file_id=prepared.output_file_id,
        storage_path=prepared.storage_path,
        staging_path=None,
        sha256=prepared.sha256,
        size=prepared.size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        ctime_ns=metadata.st_ctime_ns,
        duration_ms=prepared.duration_ms,
        width=prepared.width,
        height=prepared.height,
        fps_num=prepared.fps_num,
        fps_den=prepared.fps_den,
        mime_type=prepared.mime_type,
        published_new=published_new,
    )


def _discard_uncommitted_publication(
    prepared: PreparedDerivative,
    settings: Settings,
) -> None:
    if not prepared.published_new:
        return
    unlink_regular_file_if_identity(
        prepared.storage_path,
        settings.storage_root,
        device=prepared.device,
        inode=prepared.inode,
        ctime_ns=prepared.ctime_ns,
        missing_ok=True,
    )


def _publish_database(
    claim: MediaDerivativeClaim,
    prepared: PreparedDerivative,
) -> str:
    with _worker_session() as session:
        project = session.scalar(
            select(ProjectRefModel)
            .where(ProjectRefModel.id == claim.project_ref_id)
            .with_for_update()
        )
        item = session.scalar(
            select(ReviewItemModel)
            .where(
                ReviewItemModel.id == claim.review_item_id,
                ReviewItemModel.project_ref_id == claim.project_ref_id,
            )
            .with_for_update()
        )
        version = session.scalar(
            select(ReviewVersionModel)
            .where(
                ReviewVersionModel.id == claim.version_id,
                ReviewVersionModel.project_ref_id == claim.project_ref_id,
                ReviewVersionModel.review_item_id == claim.review_item_id,
            )
            .with_for_update()
        )
        task = session.scalar(
            select(MediaDerivativeTaskModel)
            .where(MediaDerivativeTaskModel.id == claim.task_id)
            .with_for_update()
        )
        if project is None or item is None or version is None or task is None:
            session.rollback()
            return "skipped"
        if task.status == "ready" and task.output_file_id == prepared.output_file_id:
            session.rollback()
            return "ready"
        if (
            task.status != "running"
            or task.lease_id != claim.lease_id
            or not _lease_deadline_is_future(task.lease_expires_at, utcnow())
        ):
            session.rollback()
            return "skipped"
        if claim.kind == "thumbnail":
            if (
                not version.is_current
                or item.current_version_id != version.id
            ):
                session.rollback()
                return "skipped"
        file = session.get(FileObjectModel, prepared.output_file_id)
        if file is None:
            file = FileObjectModel(
                id=prepared.output_file_id,
                original_filename=f"{prepared.output_file_id}{_output_extension(claim.kind)}",
                mime_type=prepared.mime_type,
                file_size=prepared.size,
                sha256=prepared.sha256,
                storage_path=str(prepared.storage_path),
                owner_principal_id=claim.source_owner_id,
                owner_principal_kind=claim.source_owner_kind,
                duration_ms=prepared.duration_ms,
                width=prepared.width,
                height=prepared.height,
                fps_num=prepared.fps_num,
                fps_den=prepared.fps_den,
                media_probe_version="ffmpeg-derivative-v1",
            )
            session.add(file)
            session.flush()
        elif (
            file.sha256 != prepared.sha256
            or file.file_size != prepared.size
            or file.storage_path != str(prepared.storage_path)
            or file.mime_type != prepared.mime_type
            or file.owner_principal_id != claim.source_owner_id
            or file.owner_principal_kind != claim.source_owner_kind
            or file.duration_ms != prepared.duration_ms
            or file.width != prepared.width
            or file.height != prepared.height
            or file.fps_num != prepared.fps_num
            or file.fps_den != prepared.fps_den
        ):
            raise MediaDerivativeError("MEDIA_DERIVATIVE_FILE_ID_CONFLICT", "publish", retryable=False)
        now = utcnow()
        task.status = "ready"
        task.output_file_id = file.id
        task.lease_id = None
        task.lease_expires_at = None
        task.next_attempt_at = None
        task.error_code = None
        task.failure_details = None
        task.updated_at = now
        # Persist the ready task authority before publishing its pointer on a
        # finalized version.  The database freeze trigger permits only this
        # exact, task-backed derivative transition; every original-media field
        # and all unrelated version state remain immutable.
        session.flush()
        if claim.kind == "playback_faststart":
            version.playback_asset_id = file.id
        else:
            version.thumbnail_asset_id = file.id
        session.flush()
        session.commit()
        return "ready"


def _publication_database_state(
    claim: MediaDerivativeClaim,
    prepared: PreparedDerivative,
) -> bool | None:
    try:
        with _worker_session() as session:
            # Lock in the same order as the publishing transaction. If the
            # original COMMIT is still resolving server-side, this waits for
            # that transaction before declaring the publication uncommitted.
            project = session.scalar(
                select(ProjectRefModel)
                .where(ProjectRefModel.id == claim.project_ref_id)
                .with_for_update()
            )
            item = session.scalar(
                select(ReviewItemModel)
                .where(
                    ReviewItemModel.id == claim.review_item_id,
                    ReviewItemModel.project_ref_id == claim.project_ref_id,
                )
                .with_for_update()
            )
            version = session.scalar(
                select(ReviewVersionModel)
                .where(
                    ReviewVersionModel.id == claim.version_id,
                    ReviewVersionModel.project_ref_id == claim.project_ref_id,
                    ReviewVersionModel.review_item_id == claim.review_item_id,
                )
                .with_for_update()
            )
            task = session.scalar(
                select(MediaDerivativeTaskModel)
                .where(MediaDerivativeTaskModel.id == claim.task_id)
                .with_for_update()
            )
            file = session.scalar(
                select(FileObjectModel)
                .where(FileObjectModel.id == prepared.output_file_id)
                .with_for_update()
            )
            asset_id = (
                None
                if version is None
                else (
                    version.playback_asset_id
                    if claim.kind == "playback_faststart"
                    else version.thumbnail_asset_id
                )
            )
            return bool(
                project is not None
                and item is not None
                and version is not None
                and task is not None
                and task.status == "ready"
                and task.output_file_id == prepared.output_file_id
                and file is not None
                and file.sha256 == prepared.sha256
                and file.file_size == prepared.size
                and file.storage_path == str(prepared.storage_path)
                and asset_id == prepared.output_file_id
            )
    except Exception as exc:
        LOGGER.warning(
            "media_derivative_publish_reconciliation_unavailable",
            extra={"error_type": type(exc).__name__},
        )
        return None


def _publish_database_with_reconciliation(
    claim: MediaDerivativeClaim,
    prepared: PreparedDerivative,
    settings: Settings,
) -> str:
    try:
        return _publish_database(claim, prepared)
    except Exception:
        committed = _publication_database_state(claim, prepared)
        if committed is True:
            return "ready"
        if committed is False:
            _discard_uncommitted_publication(prepared, settings)
        # If the authority cannot be queried, retain the deterministic
        # content-addressed publication so a later retry can reconcile it.
        raise


def _record_failure(
    claim: MediaDerivativeClaim,
    settings: Settings,
    error: MediaDerivativeError,
) -> str:
    with _worker_session() as session:
        task = session.scalar(
            select(MediaDerivativeTaskModel)
            .where(MediaDerivativeTaskModel.id == claim.task_id)
            .with_for_update()
        )
        if (
            task is None
            or task.status != "running"
            or task.lease_id != claim.lease_id
            or not _lease_deadline_is_future(task.lease_expires_at, utcnow())
        ):
            session.rollback()
            return "skipped"
        now = utcnow()
        retry = error.retryable and task.attempts < settings.media_worker_max_attempts
        task.status = "queued" if retry else "failed"
        task.next_attempt_at = (
            now + timedelta(seconds=settings.media_worker_retry_delay_seconds) if retry else None
        )
        task.lease_id = None
        task.lease_expires_at = None
        task.error_code = error.code
        task.failure_details = {"stage": error.stage, "attempt": task.attempts}
        task.updated_at = now
        session.commit()
        return "queued" if retry else "failed"


def _process_media_derivative(task_id: str, settings: Settings) -> str:
    started_at = time.monotonic()
    with _worker_session() as claim_session:
        claim_status, claim = _claim_task(claim_session, settings, task_id)
        claim_session.commit()
    if claim_status != "claimed" or claim is None:
        return claim_status

    def observed(status: str, failure_code: str = "") -> str:
        observe_media_task(
            claim.kind,
            status,
            time.monotonic() - started_at,
            failure_code,
        )
        return status

    prepared: PreparedDerivative | None = None
    try:
        prepared = _prepare_derivative(claim, settings)
        if not _lease_is_current(claim):
            _discard_staging(prepared, settings)
            return observed("skipped")
        prepared = _publish_file(
            prepared,
            settings,
            lambda: _renew_lease(claim, settings),
        )
        publish_status = _publish_database_with_reconciliation(
            claim,
            prepared,
            settings,
        )
        if publish_status == "skipped":
            _discard_uncommitted_publication(prepared, settings)
        return observed(publish_status)
    except MediaDerivativeLeaseLost:
        if prepared is not None:
            _discard_staging(prepared, settings)
        return observed("skipped", "MEDIA_DERIVATIVE_LEASE_LOST")
    except MediaDerivativeError as exc:
        if prepared is not None:
            _discard_staging(prepared, settings)
        return observed(_record_failure(claim, settings, exc), exc.code)
    except (OSError, UnsafeFilePathError):
        if prepared is not None:
            _discard_staging(prepared, settings)
        error = MediaDerivativeError(
            "MEDIA_DERIVATIVE_STORAGE_FAILED",
            "storage",
            retryable=True,
        )
        return observed(
            _record_failure(
                claim,
                settings,
                error,
            ),
            error.code,
        )
    except Exception as exc:
        if prepared is not None:
            _discard_staging(prepared, settings)
        LOGGER.error("media_derivative_unexpected_failure", extra={"error_type": type(exc).__name__})
        error = MediaDerivativeError(
            "MEDIA_DERIVATIVE_FAILED",
            "worker",
            retryable=True,
        )
        return observed(
            _record_failure(claim, settings, error),
            error.code,
        )


def process_media_derivative(task_id: str) -> str:
    settings = get_database_settings()
    with database_module.runtime_participant_lease(database_module.engine, settings):
        return _process_media_derivative(task_id, settings)


def process_pending_media_derivatives(batch_size: int = MEDIA_WORKER_BATCH_SIZE) -> dict[str, int]:
    if not 1 <= batch_size <= MEDIA_WORKER_BATCH_SIZE:
        raise ValueError("media derivative batch size is outside the supported range")
    settings = get_database_settings()
    if settings.media_worker_concurrency < 1:
        raise RuntimeError("media worker concurrency is invalid")
    with database_module.runtime_participant_lease(database_module.engine, settings):
        with _worker_session() as session:
            now = utcnow()
            task_ids = list(
                session.scalars(
                    select(MediaDerivativeTaskModel.id)
                    .where(
                        or_(
                            and_(
                                MediaDerivativeTaskModel.status == "queued",
                                or_(
                                    MediaDerivativeTaskModel.next_attempt_at.is_(None),
                                    MediaDerivativeTaskModel.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                MediaDerivativeTaskModel.status == "running",
                                MediaDerivativeTaskModel.lease_expires_at <= now,
                            ),
                        )
                    )
                    .order_by(MediaDerivativeTaskModel.created_at, MediaDerivativeTaskModel.id)
                    .limit(batch_size)
                )
            )
        result = {"ready": 0, "failed": 0, "queued": 0, "skipped": 0}
        for task_id in task_ids:
            status = _process_media_derivative(task_id, settings)
            result[status if status in result else "skipped"] += 1
        return result


def _atomic_worker_state(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.write(descriptor, value.encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            LOGGER.error("media_worker_state_cleanup_failed")


def _pulse_worker_heartbeat(path: Path, stop: threading.Event) -> None:
    while not stop.wait(WORKER_HEARTBEAT_REFRESH_SECONDS):
        _atomic_worker_state(path, str(time.time_ns()))


def _sleep_with_worker_heartbeat(
    interval_seconds: int,
    heartbeat_path: Path,
    sleep: Callable[[float], None],
) -> None:
    remaining = float(interval_seconds)
    while remaining > 0:
        duration = min(remaining, WORKER_HEARTBEAT_REFRESH_SECONDS)
        sleep(duration)
        _atomic_worker_state(heartbeat_path, str(time.time_ns()))
        remaining -= duration


def run_media_worker_loop(
    interval_seconds: int,
    *,
    heartbeat_path: Path = Path("/tmp/fj-media-worker-heartbeat"),
    status_path: Path = Path("/tmp/fj-media-worker-status"),
    process: Callable[[], dict[str, int]] = process_pending_media_derivatives,
    sleep: Callable[[float], None] = time.sleep,
) -> Never:
    if not 1 <= interval_seconds <= MEDIA_WORKER_MAX_SECONDS:
        raise ValueError("interval_seconds is outside the supported range")
    _atomic_worker_state(heartbeat_path, str(time.time_ns()))
    _atomic_worker_state(status_path, "starting")
    while True:
        _atomic_worker_state(status_path, "running")
        stop = threading.Event()
        pulse = threading.Thread(
            target=_pulse_worker_heartbeat,
            args=(heartbeat_path, stop),
            daemon=True,
            name="media-worker-heartbeat",
        )
        pulse.start()
        try:
            result = process()
        except Exception as exc:
            _atomic_worker_state(status_path, "failed")
            _atomic_worker_state(heartbeat_path, str(time.time_ns()))
            print(
                json.dumps(
                    {"status": "failed", "error_type": type(exc).__name__},
                    sort_keys=True,
                ),
                flush=True,
            )
            raise RuntimeError("media worker cycle failed") from exc
        finally:
            stop.set()
            pulse.join(timeout=1)
        status = "degraded" if result["failed"] else "ok"
        _atomic_worker_state(status_path, status)
        _atomic_worker_state(heartbeat_path, str(time.time_ns()))
        print(json.dumps({"status": status, **result}, sort_keys=True), flush=True)
        _sleep_with_worker_heartbeat(
            interval_seconds,
            heartbeat_path,
            sleep,
        )


def _bounded_seconds(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MEDIA_WORKER_MAX_SECONDS:
        raise argparse.ArgumentTypeError(
            f"value must be between 1 and {MEDIA_WORKER_MAX_SECONDS}"
        )
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--interval-seconds", type=_bounded_seconds, default=2)
    args = parser.parse_args()
    if args.command == "once":
        print(json.dumps(process_pending_media_derivatives(), sort_keys=True), flush=True)
        return 0
    start_worker_metrics_server(9102)
    try:
        run_media_worker_loop(args.interval_seconds)
    except RuntimeError as exc:
        LOGGER.error(
            "media_worker_stopped",
            extra={
                "error_type": (
                    type(exc.__cause__).__name__
                    if exc.__cause__ is not None
                    else type(exc).__name__
                )
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
