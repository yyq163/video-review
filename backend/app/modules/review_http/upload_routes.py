from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.app.modules.final_cut_review.application.context import ExecutionContext
from backend.app.modules.final_cut_review.application.command_handlers import canonical_request_hash, principal_fingerprint
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.final_cut_review.infra.database import SessionLocal
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import IdempotencyRecordModel, UploadSessionModel
from backend.app.modules.review_access.policies import ConfiguredWriteGuardAdapter, WriteGuardSessionSigner
from backend.app.modules.review_contracts.generated import UploadInitRequest
from backend.app.modules.review_media.service import (
    LocalMediaService,
    PreparedUploadAbort,
    PreparedUploadCompletion,
    PreparedUploadPart,
    UploadFinalizationClaim,
)
from backend.app.safe_files import exclusive_file_writer, unlink_regular_file
from backend.app.settings import Settings, get_settings
from backend.app.upload_parts import UPLOAD_PART_ADMISSION_LIMITER, new_upload_part_path, upload_identity_from_candidate_name

from .context_dependencies import build_context, get_request_id, session_dependency
from .envelope import ok

router = APIRouter(prefix="/api/v1/files")
LOGGER = logging.getLogger(__name__)
_UPLOAD_IO_EXECUTORS: dict[int, ThreadPoolExecutor] = {}
_UPLOAD_IO_EXECUTORS_LOCK = threading.Lock()
_UPLOAD_BACKGROUND_CLEANUPS: set[asyncio.Task[None]] = set()
_TERMINAL_FINALIZATION_ERROR_CODES = {"FILE_HASH_MISMATCH", "FILE_TYPE_NOT_ALLOWED", "UPLOAD_INCOMPLETE"}


class UploadBodyTimeout(ReviewError):
    def __init__(self, cleanup_task: asyncio.Task[None]) -> None:
        super().__init__("RESOURCE_STATE_CONFLICT", "上传分片读取超时，请重试")
        self.cleanup_task = cleanup_task


class UploadBodyCancelled(asyncio.CancelledError):
    def __init__(self, cleanup_task: asyncio.Task[None]) -> None:
        super().__init__()
        self.cleanup_task = cleanup_task


def _upload_io_executor(workers: int) -> ThreadPoolExecutor:
    with _UPLOAD_IO_EXECUTORS_LOCK:
        executor = _UPLOAD_IO_EXECUTORS.get(workers)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"upload-io-{workers}")
            _UPLOAD_IO_EXECUTORS[workers] = executor
        return executor


async def _run_upload_io[T](workers: int, operation: Callable[..., T], *args: object) -> T:
    future = asyncio.get_running_loop().run_in_executor(
        _upload_io_executor(workers),
        partial(operation, *args),
    )
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await future
        raise


async def _finish_timed_out_body(
    body_task: asyncio.Task[int],
    destination: FilePath,
    root: FilePath,
    io_workers: int,
) -> None:
    try:
        await asyncio.shield(body_task)
    except asyncio.CancelledError:
        try:
            await body_task
        except BaseException:
            pass
    except BaseException:
        pass
    try:
        await _run_upload_io(io_workers, unlink_regular_file, destination, root)
    except OSError:
        LOGGER.warning("upload_part_timeout_cleanup_pending")


def _track_background_cleanup(task: asyncio.Task[None]) -> None:
    _UPLOAD_BACKGROUND_CLEANUPS.add(task)
    task.add_done_callback(_UPLOAD_BACKGROUND_CLEANUPS.discard)


def staging_part_path(upload_root: FilePath, upload_id: str, part_no: int) -> FilePath:
    try:
        return new_upload_part_path(upload_root, upload_id, part_no)
    except (OSError, ValueError) as exc:
        raise ReviewError("VALIDATION_ERROR", "上传分片路径非法") from exc


async def write_limited_body(
    request: Request,
    destination: FilePath,
    max_bytes: int,
    *,
    managed_root: FilePath | None = None,
    read_timeout_seconds: float = 120.0,
    io_workers: int = 4,
) -> int:
    root = managed_root or destination.parent

    async def write_body() -> int:
        total = 0
        writer = exclusive_file_writer(destination, root)
        try:
            handle = await _run_upload_io(io_workers, writer.__enter__)
        except asyncio.CancelledError as exc:
            await _run_upload_io(io_workers, writer.__exit__, type(exc), exc, exc.__traceback__)
            raise
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_bytes:
                    raise ReviewError("FILE_TOO_LARGE", "上传分片过大")
                await _run_upload_io(io_workers, handle.write, chunk)
        except BaseException as exc:
            await _run_upload_io(io_workers, writer.__exit__, type(exc), exc, exc.__traceback__)
            raise
        await _run_upload_io(io_workers, writer.__exit__, None, None, None)
        return total

    body_task = asyncio.create_task(write_body())
    try:
        done, _pending = await asyncio.wait({body_task}, timeout=read_timeout_seconds)
    except asyncio.CancelledError:
        body_task.cancel()
        cleanup_task = asyncio.create_task(_finish_timed_out_body(body_task, destination, root, io_workers))
        _track_background_cleanup(cleanup_task)
        raise UploadBodyCancelled(cleanup_task) from None
    if done:
        return body_task.result()
    body_task.cancel()
    cleanup_task = asyncio.create_task(_finish_timed_out_body(body_task, destination, root, io_workers))
    _track_background_cleanup(cleanup_task)
    raise UploadBodyTimeout(cleanup_task)


def _part_commit_outcome(prepared: PreparedUploadPart) -> bool | None:
    identity = upload_identity_from_candidate_name(prepared.candidate_path.name)
    if identity is None:
        return False
    upload_id, part_no = identity
    try:
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            if upload is None or not isinstance(upload.received_parts, dict):
                return False
            part = upload.received_parts.get(str(part_no))
            return isinstance(part, dict) and part.get("path") == str(prepared.candidate_path)
    except Exception:
        LOGGER.warning("upload_part_commit_outcome_unknown")
        return None


def _completion_commit_outcome(upload_id: str, prepared: PreparedUploadCompletion) -> bool | None:
    if prepared.final_path is None:
        return True
    try:
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            return upload is not None and upload.status == "completed" and upload.file_id == prepared.final_path.name
    except Exception:
        LOGGER.warning("upload_complete_commit_outcome_unknown")
        return None


def _init_commit_outcome(
    idempotency_key: str,
    request_hash: str,
    principal_hash: str,
    upload_id: str,
) -> dict[str, object] | None:
    try:
        with SessionLocal() as observer:
            record = observer.get(IdempotencyRecordModel, idempotency_key)
            upload = observer.get(UploadSessionModel, upload_id)
            if (
                record is None
                or record.command_type != "InitUpload"
                or record.request_hash != request_hash
                or record.principal_hash != principal_hash
                or record.status_code != 200
                or record.response_json.get("upload_id") != upload_id
                or upload is None
            ):
                return None
            return dict(record.response_json)
    except Exception:
        LOGGER.warning("upload_init_commit_outcome_unknown")
        return None


def _completion_claim_commit_outcome(
    claim: UploadFinalizationClaim,
    idempotency_key: str,
    request_hash: str,
    principal_hash: str,
) -> bool | None:
    try:
        with SessionLocal() as observer:
            record = observer.get(IdempotencyRecordModel, idempotency_key)
            upload = observer.get(UploadSessionModel, claim.upload_id)
            return bool(
                record is not None
                and record.command_type == "CompleteUpload"
                and record.request_hash == request_hash
                and record.principal_hash == principal_hash
                and record.status_code == 102
                and upload is not None
                and upload.status == "finalizing"
                and upload.finalization_lease_id == claim.lease_id
                and upload.finalization_file_id == claim.file_id
            )
    except Exception:
        LOGGER.warning("upload_completion_claim_commit_outcome_unknown")
        return None


def commit_prepared_part(session: Session, service: LocalMediaService, prepared: PreparedUploadPart) -> None:
    try:
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            LOGGER.warning("upload_part_commit_rollback_failed")
        outcome = _part_commit_outcome(prepared)
        if outcome is True:
            try:
                service.discard_superseded_part(prepared)
            except OSError:
                LOGGER.warning("upload_part_superseded_cleanup_pending")
            return
        if outcome is False:
            try:
                service.discard_uncommitted_part(prepared)
            except OSError:
                LOGGER.warning("upload_part_commit_rollback_cleanup_pending")
        raise
    try:
        service.discard_superseded_part(prepared)
    except OSError:
        LOGGER.warning("upload_part_superseded_cleanup_pending")


def cleanup_committed_parts(
    service: LocalMediaService,
    prepared: PreparedUploadAbort | PreparedUploadCompletion,
) -> bool:
    try:
        service.cleanup_part_paths(prepared.part_paths)
    except OSError:
        LOGGER.warning("upload_committed_part_cleanup_pending")
        return False
    return True


def media(session: Session, context: ExecutionContext) -> LocalMediaService:
    return LocalMediaService(session, get_settings(), context)


def validate_upload_before_stream(
    context: ExecutionContext,
    upload_id: str,
    part_no: int,
) -> int:
    settings = get_settings()
    with SessionLocal() as validation_session:
        max_stream_bytes = LocalMediaService(validation_session, settings, context).claim_part_stream(
            upload_id,
            part_no,
        )
        validation_session.commit()
        return max_stream_bytes


def bind_and_commit_part(
    session: Session,
    settings: Settings,
    context: ExecutionContext,
    upload_id: str,
    part_no: int,
    staged_path: FilePath,
    size: int,
) -> PreparedUploadPart:
    service = LocalMediaService(session, settings, context)
    try:
        locked_upload = service.ensure_can_receive_part(upload_id, part_no)
        prepared = service.put_part_file(upload_id, part_no, staged_path, size, locked_upload=locked_upload)
    except BaseException:
        try:
            session.rollback()
        except Exception:
            LOGGER.warning("upload_part_prepare_rollback_failed")
        try:
            unlink_regular_file(staged_path, settings.storage_root / "uploads")
        except OSError:
            LOGGER.warning("upload_part_prepare_cleanup_pending")
        raise
    commit_prepared_part(session, service, prepared)
    return prepared


def exclusive_upload_operation(upload_id: str) -> Iterator[None]:
    lease = UPLOAD_PART_ADMISSION_LIMITER.try_acquire_exclusive(upload_id)
    if lease is None:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片仍在处理中，请稍后重试")
    try:
        yield
    finally:
        lease.release()


def confirm_cleanup(
    context: ExecutionContext,
    upload_id: str,
    expected_status: str,
) -> None:
    settings = get_settings()
    with SessionLocal() as cleanup_session:
        service = LocalMediaService(cleanup_session, settings, context)
        service.confirm_part_cleanup(upload_id, expected_status)
        cleanup_session.commit()


def cleanup_and_confirm(
    service: LocalMediaService,
    prepared: PreparedUploadAbort | PreparedUploadCompletion,
    context: ExecutionContext,
    upload_id: str,
    expected_status: str,
) -> None:
    if not cleanup_committed_parts(service, prepared):
        return
    try:
        confirm_cleanup(context, upload_id, expected_status)
    except Exception:
        LOGGER.warning("upload_part_cleanup_confirmation_pending")


def reserve_completion_idempotency(
    repo: SqlAlchemyReviewRepository,
    key: str,
    request_hash: str,
    principal_hash: str,
    *,
    allow_pending_recovery: bool,
) -> dict[str, object] | None:
    existing = repo.session.scalar(select(IdempotencyRecordModel).where(IdempotencyRecordModel.key == key).with_for_update())
    if existing is None:
        return repo.reserve_idempotency(key, "CompleteUpload", request_hash, principal_hash)
    if existing.principal_hash != principal_hash:
        raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "Idempotency-Key 不属于当前主体")
    if existing.request_hash != request_hash:
        raise ReviewError("IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 对应不同请求体")
    if existing.status_code == 200:
        return existing.response_json
    if allow_pending_recovery and existing.status_code == 102:
        return None
    raise ReviewError("IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 请求仍在处理")


def is_pending_completion_replay(
    repo: SqlAlchemyReviewRepository,
    key: str,
    request_hash: str,
    principal_hash: str,
) -> bool:
    existing = repo.session.get(IdempotencyRecordModel, key)
    if existing is None:
        return False
    if existing.principal_hash != principal_hash:
        raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "Idempotency-Key 不属于当前主体")
    if existing.command_type != "CompleteUpload" or existing.request_hash != request_hash:
        raise ReviewError("IDEMPOTENCY_CONFLICT", "相同 Idempotency-Key 对应不同请求体")
    if existing.status_code == 200:
        return False
    if existing.status_code == 102:
        return True
    raise ReviewError("IDEMPOTENCY_CONFLICT", "上传完成幂等记录状态非法")


def release_failed_completion(
    context: ExecutionContext,
    claim: UploadFinalizationClaim,
    idempotency_key: str,
    request_hash: str,
    principal_hash: str,
    *,
    abort: bool,
) -> PreparedUploadAbort | None:
    settings = get_settings()
    with SessionLocal() as recovery_session:
        service = LocalMediaService(recovery_session, settings, context)
        prepared_abort = service.release_failed_finalization(claim, abort=abort)
        record = recovery_session.scalar(select(IdempotencyRecordModel).where(IdempotencyRecordModel.key == idempotency_key).with_for_update())
        if record is not None and record.status_code == 102 and record.request_hash == request_hash and record.principal_hash == principal_hash:
            recovery_session.delete(record)
        recovery_session.commit()
        return prepared_abort


def upload_context(request: Request, request_id: str, fj_write_guard: str | None, x_write_guard_verified: str | None) -> ExecutionContext:
    settings = get_settings()
    context = build_context("edit", request, request_id, fj_write_guard, x_write_guard_verified)
    ConfiguredWriteGuardAdapter(settings, WriteGuardSessionSigner(settings)).assert_write_allowed(context)
    return context


@router.post("/uploads/init")
def init_upload(
    payload: UploadInitRequest,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=256)] = None,
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> dict[str, object]:
    if not idempotency_key:
        raise ReviewError("VALIDATION_ERROR", "缺少 Idempotency-Key")
    context = upload_context(request, request_id, fj_write_guard, x_write_guard_verified)
    settings = get_settings()
    body = payload.model_dump(mode="json")
    request_hash = canonical_request_hash(
        "InitUpload",
        {
            "payload": body,
            "principal": {"kind": context.principal.kind, "id": context.principal.id},
        },
    )
    principal_hash = principal_fingerprint(context)
    repo = SqlAlchemyReviewRepository(session, settings)
    replay = repo.reserve_idempotency(idempotency_key, "InitUpload", request_hash, principal_hash)
    if replay is not None:
        session.rollback()
        return ok(replay, request_id)
    result = LocalMediaService(session, settings, context).init_upload(body)
    repo.complete_idempotency(idempotency_key, result)
    try:
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            LOGGER.warning("upload_init_commit_rollback_failed")
        committed = _init_commit_outcome(
            idempotency_key,
            request_hash,
            principal_hash,
            str(result["upload_id"]),
        )
        if committed is not None:
            return ok(committed, request_id)
        raise
    return ok(result, request_id)


@router.put("/uploads/{upload_id}/parts/{part_no}")
async def put_part(
    upload_id: str,
    part_no: Annotated[int, Path(ge=1, le=256)],
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> dict[str, object]:
    settings = get_settings()
    if part_no > settings.max_upload_parts:
        raise ReviewError("VALIDATION_ERROR", "上传分片编号超出部署限制")
    content_length = request.headers.get("content-length")
    parsed_content_length: int | None = None
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise ReviewError("VALIDATION_ERROR", "Content-Length 非法")
            if parsed_content_length > settings.max_upload_part_bytes:
                raise ReviewError("FILE_TOO_LARGE", "上传分片过大")
        except ValueError as exc:
            raise ReviewError("VALIDATION_ERROR", "Content-Length 非法") from exc
    context = upload_context(request, request_id, fj_write_guard, x_write_guard_verified)
    if not context.principal.id:
        raise ReviewError("PRINCIPAL_AUTHENTICATION_REQUIRED", "缺少可信 principal 上下文")
    max_stream_bytes = await run_in_threadpool(validate_upload_before_stream, context, upload_id, part_no)
    if parsed_content_length is not None and parsed_content_length > max_stream_bytes:
        raise ReviewError("FILE_TOO_LARGE", "上传分片超过文件声明剩余大小")
    lease = UPLOAD_PART_ADMISSION_LIMITER.try_acquire(
        f"{context.principal.kind}:{context.principal.id}",
        upload_id,
        max_per_principal=settings.max_inflight_upload_parts_per_principal,
        max_per_upload=settings.max_inflight_upload_parts_per_session,
        max_total=settings.max_inflight_upload_part_candidates,
    )
    if lease is None:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "上传分片并发请求过多，请稍后重试")
    release_lease = True
    try:
        staged_path = await run_in_threadpool(
            staging_part_path,
            settings.storage_root / "uploads",
            upload_id,
            part_no,
        )
        try:
            size = await write_limited_body(
                request,
                staged_path,
                max_stream_bytes,
                managed_root=settings.storage_root / "uploads",
                read_timeout_seconds=settings.upload_part_read_timeout_seconds,
                io_workers=settings.upload_part_io_workers,
            )
        except (UploadBodyTimeout, UploadBodyCancelled) as exc:
            exc.cleanup_task.add_done_callback(lambda _task: lease.release())
            release_lease = False
            raise
        except BaseException:
            try:
                await _run_upload_io(
                    settings.upload_part_io_workers,
                    unlink_regular_file,
                    staged_path,
                    settings.storage_root / "uploads",
                )
            except OSError:
                LOGGER.warning("upload_part_prepare_cleanup_pending")
            raise
        prepared = await run_in_threadpool(
            bind_and_commit_part,
            session,
            settings,
            context,
            upload_id,
            part_no,
            staged_path,
            size,
        )
        return ok(prepared.response, request_id)
    finally:
        if release_lease:
            lease.release()


@router.get("/uploads/{upload_id}")
def get_upload(
    upload_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> dict[str, object]:
    return ok(media(session, upload_context(request, request_id, fj_write_guard, x_write_guard_verified)).get_upload(upload_id), request_id)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(
    upload_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    _exclusive_upload: None = Depends(exclusive_upload_operation),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=256)] = None,
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> dict[str, object]:
    if not idempotency_key:
        raise ReviewError("VALIDATION_ERROR", "缺少 Idempotency-Key")
    context = upload_context(request, request_id, fj_write_guard, x_write_guard_verified)
    settings = get_settings()
    service = LocalMediaService(session, settings, context)
    repo = SqlAlchemyReviewRepository(session, settings)
    request_hash = canonical_request_hash(
        "CompleteUpload",
        {
            "upload_id": upload_id,
            "principal": {"kind": context.principal.kind, "id": context.principal.id},
        },
    )
    principal_hash = principal_fingerprint(context)
    try:
        pending_replay = is_pending_completion_replay(
            repo,
            idempotency_key,
            request_hash,
            principal_hash,
        )
        claimed = service.claim_completion(
            upload_id,
            idempotency_key_hash=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            request_hash=request_hash,
            allow_active_lease_resume=pending_replay,
        )
        replay = reserve_completion_idempotency(
            repo,
            idempotency_key,
            request_hash,
            principal_hash,
            allow_pending_recovery=(
                isinstance(claimed, PreparedUploadCompletion)
                or claimed.recovered_expired_lease
                or claimed.resumed_active_lease
            ),
        )
    except Exception:
        session.rollback()
        raise
    if replay is not None:
        session.rollback()
        if isinstance(claimed, PreparedUploadCompletion):
            cleanup_and_confirm(service, claimed, context, upload_id, "completed")
        return ok(replay, request_id)
    if isinstance(claimed, PreparedUploadCompletion):
        try:
            repo.complete_idempotency(idempotency_key, claimed.response)
            session.commit()
        except Exception:
            session.rollback()
            raise
        cleanup_and_confirm(service, claimed, context, upload_id, "completed")
        return ok(claimed.response, request_id)

    claim = claimed
    try:
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            LOGGER.warning("upload_completion_claim_commit_rollback_failed")
        if _completion_claim_commit_outcome(
            claim,
            idempotency_key,
            request_hash,
            principal_hash,
        ) is not True:
            raise
    session.close()

    try:
        finalized = service.finalize_claim(claim)
    except Exception as exc:
        abort = isinstance(exc, ReviewError) and exc.code in _TERMINAL_FINALIZATION_ERROR_CODES
        try:
            aborted = release_failed_completion(
                context,
                claim,
                idempotency_key,
                request_hash,
                principal_hash,
                abort=abort,
            )
            if aborted is not None:
                cleanup_and_confirm(service, aborted, context, upload_id, "aborted")
        except Exception:
            LOGGER.warning("upload_finalization_failure_recovery_pending")
        raise

    prepared: PreparedUploadCompletion | None = None
    try:
        with SessionLocal() as publish_session:
            publish_service = LocalMediaService(publish_session, settings, context)
            prepared = publish_service.publish_completion(claim, finalized)
            SqlAlchemyReviewRepository(publish_session, settings).complete_idempotency(
                idempotency_key,
                prepared.response,
            )
            publish_session.commit()
    except Exception:
        if prepared is not None and _completion_commit_outcome(upload_id, prepared) is True:
            cleanup_and_confirm(service, prepared, context, upload_id, "completed")
            return ok(prepared.response, request_id)
        raise
    if prepared is None:
        raise RuntimeError("upload completion publish returned no result")
    cleanup_and_confirm(service, prepared, context, upload_id, "completed")
    return ok(prepared.response, request_id)


@router.post("/uploads/{upload_id}/abort")
def abort_upload(
    upload_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    _exclusive_upload: None = Depends(exclusive_upload_operation),
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> dict[str, object]:
    context = upload_context(request, request_id, fj_write_guard, x_write_guard_verified)
    service = media(session, context)
    prepared = service.prepare_abort(upload_id)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    cleanup_and_confirm(service, prepared, context, upload_id, "aborted")
    return ok(prepared.response, request_id)
