from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.types import Receive, Scope, Send

from backend.app.modules.final_cut_review.application.context import ExecutionContext
from backend.app.modules.final_cut_review.application.query_services import FinalCutReviewQueryService
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.final_cut_review.infra.database import (
    RuntimeWriterLock,
    SessionLocal,
    bind_runtime_writer_lock,
    current_runtime_writer_lock,
)
from backend.app.modules.review_access.policies import NoAccountAuthorizationAdapter, PackageDownloadTokenSigner
from backend.app.modules.review_contracts.generated import CONTRACT_VERSION, MODULE_MANIFEST
from backend.app.modules.review_media.service import parse_range_header
from backend.app.safe_files import UnsafeFilePathError, contained_path, pin_regular_file
from backend.app.settings import get_settings

from .context_dependencies import build_context, get_request_id, session_dependency, trusted_request_scheme
from .envelope import ok

router = APIRouter(prefix="/api/v1/final-cut-review")
LOGGER = logging.getLogger(__name__)
PACKAGE_DOWNLOAD_HEARTBEAT_MAX_INTERVAL_SECONDS = 2.0
PROTECTED_MEDIA_INTERNAL_PREFIX = "/_protected_media"


class PackageDownloadLeaseHeartbeat:
    def __init__(self, renew: Callable[[], bool | None], *, interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("package download heartbeat interval must be positive")
        self._renew = renew
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("package download heartbeat already started")
        thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="package-download-lease-heartbeat",
        )
        thread.start()
        self._thread = thread

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                renewed = self._renew()
            except Exception as exc:
                LOGGER.error(
                    "package_download_lease_heartbeat_failed",
                    extra={"error_type": type(exc).__name__},
                )
                self._failed.set()
                return
            if renewed is not True:
                self._failed.set()
                return

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def assert_healthy(self) -> None:
        if self._failed.is_set():
            raise ReviewError("STORAGE_UNAVAILABLE", "下载租约续期失败")


class LifecycleStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: Iterator[bytes],
        *,
        status_code: int,
        media_type: str,
        headers: dict[str, str],
        on_close: Callable[[], None],
        on_response_complete: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(content, status_code=status_code, media_type=media_type, headers=headers)
        self._on_close = on_close
        self._on_response_complete = on_response_complete

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        completed = False
        try:
            await super().__call__(scope, receive, send)
            completed = True
        finally:
            self._on_close()
            if self._on_response_complete is not None:
                self._on_response_complete(completed)


def query_service(session: Session) -> FinalCutReviewQueryService:
    return FinalCutReviewQueryService(SqlAlchemyReviewRepository(session, get_settings()))


def query_context(request: Request, request_id: str) -> ExecutionContext:
    return build_context("review", request, request_id)


def authorize_query(context: ExecutionContext, capability: str, project_ref_id: str | None = None) -> None:
    NoAccountAuthorizationAdapter().authorize(context, capability, {"project_ref_id": project_ref_id})


def paginated_ok(
    data: list[Any],
    total_count: int,
    request_id: str,
    page: int,
    page_size: int,
) -> dict[str, object]:
    return {
        "data": data,
        "meta": {
            "request_id": request_id,
            "contract_version": CONTRACT_VERSION,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        },
    }


def allowed_project_ref_ids(context: ExecutionContext) -> tuple[str, ...] | None:
    if not context.principal.id or "*" in context.principal.project_ref_ids:
        return None
    return tuple(context.principal.project_ref_ids)


MEDIA_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
THUMBNAIL_CACHE_CONTROL = "private, no-cache, max-age=0, must-revalidate"


def contained_existing_path(
    path_value: str,
    root: Path,
    missing_code: str = "PACKAGE_SOURCE_MISSING",
    expected_path: Path | None = None,
) -> Path:
    try:
        path = contained_path(path_value, root)
    except UnsafeFilePathError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "文件路径越界") from exc
    if expected_path is not None:
        try:
            expected = contained_path(expected_path, root)
        except UnsafeFilePathError as exc:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件存储合同非法") from exc
        if path != expected:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件路径不符合存储合同")
    try:
        with pin_regular_file(path, root) as pinned:
            if pinned is None or not pinned.exists:
                raise ReviewError(missing_code, "文件不存在")
    except UnsafeFilePathError as exc:
        raise ReviewError("STORAGE_UNAVAILABLE", "文件路径非法") from exc
    return path


def verify_open_handle_sha256(
    handle: BinaryIO,
    expected_sha256: str,
    stream_guard: Callable[[], None] | None = None,
) -> None:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        if stream_guard is not None:
            stream_guard()
        digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
        raise ReviewError("FILE_HASH_MISMATCH", "文件完整性校验失败")


def regular_file_response(
    path: Path,
    root: Path,
    *,
    media_type: str,
    filename: str | None = None,
    start: int = 0,
    end: int | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    expected_sha256: str | None = None,
    on_close: Callable[[], None] | None = None,
    on_response_complete: Callable[[bool], None] | None = None,
    on_stream_complete: Callable[[], None] | None = None,
    on_sha256_verified: Callable[[tuple[int, int, int, int, int]], None] | None = None,
    force_sha256_verification: bool = False,
    expected_file_identity: tuple[int, int, int, int, int] | None = None,
    stream_guard: Callable[[], None] | None = None,
) -> StreamingResponse:
    pinned_context = pin_regular_file(path, root)
    handle_context = None
    pinned_entered = False
    handle_entered = False
    closed = False
    close_lock = threading.Lock()

    def close_once() -> None:
        nonlocal closed
        with close_lock:
            if closed:
                return
            closed = True
        try:
            if handle_entered and handle_context is not None:
                handle_context.__exit__(None, None, None)
        finally:
            try:
                if pinned_entered:
                    pinned_context.__exit__(None, None, None)
            finally:
                if on_close is not None:
                    on_close()

    try:
        pinned = pinned_context.__enter__()
        pinned_entered = True
        if pinned is None or not pinned.exists or pinned.size is None:
            raise ReviewError("PACKAGE_SOURCE_MISSING", "文件不存在")
        final_end = pinned.size - 1 if end is None else end
        handle_context = pinned.open_readonly()
        handle = handle_context.__enter__()
        handle_entered = True
        opened_metadata = os.fstat(handle.fileno())
        opened_identity = (
            opened_metadata.st_dev,
            opened_metadata.st_ino,
            opened_metadata.st_ctime_ns,
            opened_metadata.st_mtime_ns,
            opened_metadata.st_size,
        )
        if expected_file_identity is not None and opened_identity != expected_file_identity:
            raise ReviewError("FILE_HASH_MISMATCH", "文件完整性校验失败")
        # Only a package session whose current file identity has already passed
        # a full digest may serve a partial response without rehashing the file.
        if expected_sha256 is not None and (force_sha256_verification or (status_code == 200 and start == 0)):
            verify_open_handle_sha256(handle, expected_sha256, stream_guard)
            final_metadata = os.fstat(handle.fileno())
            verified_identity = (
                final_metadata.st_dev,
                final_metadata.st_ino,
                final_metadata.st_ctime_ns,
                final_metadata.st_mtime_ns,
                final_metadata.st_size,
            )
            expected_identity = (
                pinned.device,
                pinned.inode,
                pinned.ctime_ns,
                pinned.mtime_ns,
                pinned.size,
            )
            if verified_identity != expected_identity:
                raise ReviewError("FILE_HASH_MISMATCH", "文件完整性校验失败")
            if on_sha256_verified is not None:
                on_sha256_verified(verified_identity)
        if start < 0 or final_end < start or final_end >= pinned.size:
            raise ReviewError("VALIDATION_ERROR", "文件读取范围越界")
    except Exception:
        close_once()
        if on_response_complete is not None:
            on_response_complete(False)
        raise

    def iterator():
        try:
            handle.seek(start)
            remaining = final_end - start + 1
            while remaining > 0:
                if stream_guard is not None:
                    stream_guard()
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ReviewError("STORAGE_UNAVAILABLE", "文件读取提前结束")
                remaining -= len(chunk)
                yield chunk
            if remaining == 0 and on_stream_complete is not None:
                on_stream_complete()
        finally:
            close_once()

    response_headers = {"Content-Length": str(final_end - start + 1), **MEDIA_RESPONSE_HEADERS, **(headers or {})}
    if filename is not None:
        response_headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
    return LifecycleStreamingResponse(
        iterator(),
        status_code=status_code,
        media_type=media_type,
        headers=response_headers,
        on_close=close_once,
        on_response_complete=on_response_complete,
    )


def protected_media_response(
    file: Any,
    *,
    range_header: str | None,
    cache_control: str = "no-store",
) -> Response:
    settings = get_settings()
    if settings.protected_media_direct_stream_enabled:
        path = contained_existing_path(
            file.storage_path,
            settings.storage_root,
            "PLAYBACK_NOT_READY",
            Path("files") / file.id,
        )
        start, end, status_code = parse_range_header(range_header, file.file_size)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": cache_control}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{file.file_size}"
        return regular_file_response(
            path,
            settings.storage_root,
            media_type=file.mime_type,
            start=start,
            end=end,
            status_code=status_code,
            headers=headers,
        )
    if not isinstance(file.id, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", file.id) is None:
        raise ReviewError("STORAGE_UNAVAILABLE", "媒体文件标识非法")
    return Response(
        status_code=200,
        media_type=file.mime_type,
        headers={
            **MEDIA_RESPONSE_HEADERS,
            "Cache-Control": cache_control,
            "Accept-Ranges": "bytes",
            "X-Accel-Redirect": f"{PROTECTED_MEDIA_INTERNAL_PREFIX}/{quote(file.id, safe='')}",
        },
    )


def package_download_cookie_name(package_id: str) -> str:
    return f"fj_pkg_{package_id}"


def package_download_heartbeat_interval(lease_seconds: int) -> float:
    return max(1.0, min(PACKAGE_DOWNLOAD_HEARTBEAT_MAX_INTERVAL_SECONDS, lease_seconds / 3))


def renew_package_download_lease(
    package_id: str,
    lease_id: str,
    runtime_writer_lock: RuntimeWriterLock,
) -> bool | None:
    try:
        with bind_runtime_writer_lock(runtime_writer_lock):
            with SessionLocal() as lease_session:
                renewed = SqlAlchemyReviewRepository(lease_session, get_settings()).renew_package_download_lease(
                    package_id,
                    lease_id,
                )
                if renewed:
                    lease_session.commit()
                else:
                    lease_session.rollback()
                return renewed
    except Exception as exc:
        LOGGER.error("package_download_lease_renewal_failed", extra={"error_type": type(exc).__name__})
        return None


def release_package_download_lease(
    package_id: str,
    lease_id: str,
    runtime_writer_lock: RuntimeWriterLock,
    *,
    completed: bool,
) -> bool:
    try:
        with bind_runtime_writer_lock(runtime_writer_lock):
            with SessionLocal() as lease_session:
                released = SqlAlchemyReviewRepository(lease_session, get_settings()).release_package_download_lease(
                    package_id,
                    lease_id,
                    completed=completed,
                )
                if released:
                    lease_session.commit()
                else:
                    lease_session.rollback()
                return released
    except Exception as exc:
        LOGGER.error("package_download_lease_release_failed", extra={"error_type": type(exc).__name__})
        return False


def mark_package_download_session_verified(
    package_id: str,
    lease_id: str,
    session_token: str,
    expected_sha256: str,
    file_identity: tuple[int, int, int, int, int],
    runtime_writer_lock: RuntimeWriterLock,
) -> bool:
    try:
        with bind_runtime_writer_lock(runtime_writer_lock):
            with SessionLocal() as lease_session:
                verified = SqlAlchemyReviewRepository(lease_session, get_settings()).mark_package_download_session_verified(
                    package_id,
                    lease_id,
                    session_token,
                    expected_sha256,
                    file_identity,
                )
                if verified:
                    lease_session.commit()
                else:
                    lease_session.rollback()
                return verified
    except Exception as exc:
        LOGGER.error("package_download_session_verification_failed", extra={"error_type": type(exc).__name__})
        return False


@router.get("/module-manifest")
def module_manifest(request_id: str = Depends(get_request_id)) -> dict[str, object]:
    return ok(MODULE_MANIFEST, request_id)


@router.get("/projects")
def list_projects(
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    context = query_context(request, request_id)
    authorize_query(context, "review.project.read")
    projects, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_projects_page(
        page=page,
        page_size=page_size,
        allowed_project_ref_ids=allowed_project_ref_ids(context),
    )
    return paginated_ok(projects, total_count, request_id, page, page_size)


@router.get("/responsible-editors")
def list_responsible_editors(
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> dict[str, object]:
    context = query_context(request, request_id)
    authorize_query(context, "review.project.read")
    return ok(SqlAlchemyReviewRepository(session, get_settings()).list_responsible_editors(), request_id)


@router.get("/projects/{project_ref_id}")
def get_project(
    project_ref_id: str, request: Request, session: Session = Depends(session_dependency), request_id: str = Depends(get_request_id)
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.project.read", project_ref_id)
    return ok(query_service(session).get_project(project_ref_id), request_id)


@router.get("/projects/{project_ref_id}/summary")
def get_project_summary(
    project_ref_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.project.read", project_ref_id)
    return ok(
        SqlAlchemyReviewRepository(session, get_settings()).get_project_summary(
            project_ref_id
        ),
        request_id,
    )


@router.get("/projects/{project_ref_id}/technical-qc-progress")
def get_project_technical_qc_progress(
    project_ref_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.project.read", project_ref_id)
    return ok(
        query_service(session).get_project_technical_qc_progress(project_ref_id),
        request_id,
    )


@router.get("/projects/{project_ref_id}/items")
def list_items(
    project_ref_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.item.read", project_ref_id)
    items, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_items_page(
        project_ref_id,
        page=page,
        page_size=page_size,
    )
    return paginated_ok(items, total_count, request_id, page, page_size)


@router.get("/projects/{project_ref_id}/items/{review_item_id}")
def get_item(
    project_ref_id: str, review_item_id: str, request: Request, session: Session = Depends(session_dependency), request_id: str = Depends(get_request_id)
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.item.read", project_ref_id)
    return ok(query_service(session).get_item(project_ref_id, review_item_id), request_id)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions")
def list_versions(
    project_ref_id: str,
    review_item_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.version.read", project_ref_id)
    versions, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_versions_page(
        project_ref_id,
        review_item_id,
        page=page,
        page_size=page_size,
    )
    return paginated_ok(versions, total_count, request_id, page, page_size)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}")
def get_version(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.version.read", project_ref_id)
    return ok(query_service(session).get_version(project_ref_id, review_item_id, version_id), request_id)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues")
def list_issues(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.issue.read", project_ref_id)
    issues, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_issues_page(
        project_ref_id,
        review_item_id,
        version_id,
        page=page,
        page_size=page_size,
    )
    return paginated_ok(issues, total_count, request_id, page, page_size)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}")
def get_issue(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    issue_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.issue.read", project_ref_id)
    return ok(query_service(session).get_issue(project_ref_id, review_item_id, version_id, issue_id), request_id)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/revisions")
def list_revisions(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    issue_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.issue.read", project_ref_id)
    revisions, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_revisions_page(
        project_ref_id,
        review_item_id,
        version_id,
        issue_id,
        page=page,
        page_size=page_size,
    )
    return paginated_ok(revisions, total_count, request_id, page, page_size)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages")
def list_messages(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    issue_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.issue.read", project_ref_id)
    messages, total_count = SqlAlchemyReviewRepository(session, get_settings()).list_messages_page(
        project_ref_id,
        review_item_id,
        version_id,
        issue_id,
        page=page,
        page_size=page_size,
    )
    return paginated_ok(messages, total_count, request_id, page, page_size)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/finalization")
def get_finalization(
    project_ref_id: str, review_item_id: str, request: Request, session: Session = Depends(session_dependency), request_id: str = Depends(get_request_id)
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.finalization.read", project_ref_id)
    return ok(query_service(session).get_finalization(project_ref_id, review_item_id), request_id)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/stream")
def stream_version(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    authorize_query(query_context(request, request_id), "review.version.read", project_ref_id)
    repo = SqlAlchemyReviewRepository(session, get_settings())
    try:
        file = repo.get_file_for_version(project_ref_id, review_item_id, version_id)
    except ReviewError as exc:
        if exc.code == "PLAYBACK_NOT_READY" and repo.requeue_missing_ready_derivative(
            project_ref_id,
            review_item_id,
            version_id,
            "playback_faststart",
        ):
            session.commit()
        raise
    return protected_media_response(file, range_header=range_header)


@router.get("/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/thumbnail")
def get_review_version_thumbnail(
    project_ref_id: str,
    review_item_id: str,
    version_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
) -> Response:
    authorize_query(query_context(request, request_id), "review.version.read", project_ref_id)
    repo = SqlAlchemyReviewRepository(session, get_settings())
    try:
        file = repo.get_thumbnail_file_for_current_version(
            project_ref_id,
            review_item_id,
            version_id,
        )
    except ReviewError as exc:
        if exc.code == "PLAYBACK_NOT_READY" and repo.requeue_missing_ready_derivative(
            project_ref_id,
            review_item_id,
            version_id,
            "thumbnail",
        ):
            session.commit()
        raise
    return protected_media_response(
        file,
        range_header=None,
        cache_control=THUMBNAIL_CACHE_CONTROL,
    )


@router.get("/projects/{project_ref_id}/items/{review_item_id}/finalized-original/download")
def download_finalized_original(
    project_ref_id: str,
    review_item_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    range_header: str | None = Header(default=None, alias="Range"),
) -> StreamingResponse:
    context = query_context(request, request_id)
    authorize_query(context, "review.download.finalized_original", project_ref_id)
    settings = get_settings()
    repo = SqlAlchemyReviewRepository(session, settings)
    finalization, file = repo.get_file_for_finalization(project_ref_id, review_item_id)
    path = contained_existing_path(
        file.storage_path,
        settings.storage_root,
        expected_path=Path("files") / file.id,
    )
    repo.record_finalized_original_download_requested(context, finalization)
    session.commit()
    start, end, status_code = parse_range_header(range_header, file.file_size)
    headers = {"Accept-Ranges": "bytes"}
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file.file_size}"
    return regular_file_response(
        path,
        settings.storage_root,
        media_type=finalization.mime_type,
        filename=finalization.original_filename,
        start=start,
        end=end,
        status_code=status_code,
        headers=headers,
        expected_sha256=finalization.sha256,
    )


@router.get("/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}")
def get_package(
    project_ref_id: str, package_id: str, request: Request, session: Session = Depends(session_dependency), request_id: str = Depends(get_request_id)
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.package.read", project_ref_id)
    repo = SqlAlchemyReviewRepository(session, get_settings())
    return ok(repo.package_dto(repo.get_package(project_ref_id, package_id)), request_id)


@router.post("/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download-session")
def authorize_package_download(
    project_ref_id: str,
    package_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    package_download_token: str = Header(alias="X-Package-Download-Token", min_length=1),
) -> dict[str, object]:
    authorize_query(query_context(request, request_id), "review.package.download", project_ref_id)
    settings = get_settings()
    secure_cookie = trusted_request_scheme(request, settings) == "https"
    if not PackageDownloadTokenSigner(settings).verify(package_download_token, project_ref_id, package_id):
        raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "包下载 token 无效或已过期")
    session_token, _expires_at = SqlAlchemyReviewRepository(session, settings).authorize_package_download_session(
        project_ref_id,
        package_id,
    )
    session.commit()
    download_path = f"/api/v1/final-cut-review/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download"
    response.set_cookie(
        package_download_cookie_name(package_id),
        session_token,
        max_age=settings.package_download_session_ttl_seconds,
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        path=download_path,
    )
    return ok({"status": "ready"}, request_id)


@router.get("/review/projects/{project_ref_id}/finalized-originals/packages/{package_id}/download")
def download_package(
    project_ref_id: str,
    package_id: str,
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    range_header: str | None = Header(default=None, alias="Range"),
) -> StreamingResponse:
    authorize_query(query_context(request, request_id), "review.package.download", project_ref_id)
    settings = get_settings()
    cookie_name = package_download_cookie_name(package_id)
    candidate_token = request.cookies.get(cookie_name)
    if not candidate_token:
        raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "包下载 session 缺失")
    repo = SqlAlchemyReviewRepository(session, settings)
    package_storage_bytes = repo.lock_package_download_storage_bytes(project_ref_id, package_id)
    start, end, status_code = parse_range_header(range_header, package_storage_bytes)
    if start == 0 and end == package_storage_bytes - 1:
        status_code = 200
    lease = repo.begin_package_download(
        project_ref_id,
        package_id,
        candidate_token,
        is_resume_request=status_code == 206 and start > 0,
    )
    if lease["storage_bytes"] != package_storage_bytes:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "临时包大小在下载准备期间发生变化")
    session.commit()
    runtime_writer_lock = current_runtime_writer_lock()
    heartbeat = PackageDownloadLeaseHeartbeat(
        lambda: renew_package_download_lease(package_id, lease["lease_id"], runtime_writer_lock),
        interval_seconds=package_download_heartbeat_interval(settings.package_download_lease_seconds),
    )

    stream_completed = threading.Event()
    release_lock = threading.Lock()
    lease_released = False

    def release_lease(response_completed: bool = False) -> None:
        nonlocal lease_released
        with release_lock:
            if lease_released:
                return
            lease_released = True
        heartbeat.stop()
        release_package_download_lease(
            package_id,
            lease["lease_id"],
            runtime_writer_lock,
            completed=response_completed and stream_completed.is_set(),
        )

    def mark_integrity_verified(file_identity: tuple[int, int, int, int, int]) -> None:
        if not mark_package_download_session_verified(
            package_id,
            lease["lease_id"],
            candidate_token,
            lease["sha256"],
            file_identity,
            runtime_writer_lock,
        ):
            raise ReviewError("STORAGE_UNAVAILABLE", "下载 session 完整性状态更新失败")

    try:
        heartbeat.start()
        path = contained_existing_path(
            lease["storage_path"],
            settings.package_root,
            expected_path=Path(f"{package_id}.zip"),
        )
        headers = {"Accept-Ranges": "bytes"}
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{lease['storage_bytes']}"
        response = regular_file_response(
            path,
            settings.package_root,
            media_type="application/zip",
            filename=lease["package_filename"],
            start=start,
            end=end,
            status_code=status_code,
            headers=headers,
            expected_sha256=lease["sha256"],
            on_response_complete=release_lease,
            on_stream_complete=stream_completed.set,
            on_sha256_verified=(None if lease["integrity_verified"] else mark_integrity_verified),
            force_sha256_verification=not lease["integrity_verified"],
            expected_file_identity=lease["file_identity"],
            stream_guard=heartbeat.assert_healthy,
        )
    except Exception:
        release_lease(False)
        raise
    return response
