from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from starlette.types import Message, Scope

if TYPE_CHECKING:
    from backend.app.modules.review_http.query_routes import PackageDownloadLeaseHeartbeat


def _create_package_with_lease(
    package_id: str,
    lease_id: str,
    lease_expires_at: datetime,
) -> tuple[Path, str]:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ProjectRefModel,
        utcnow,
    )
    from backend.app.settings import get_settings

    settings = get_settings()
    project_id = f"prj_{uuid.uuid4().hex}"
    package_path = settings.package_root / f"{package_id}.zip"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_bytes(b"package-download-lease")
    with SessionLocal() as session:
        session.add(
            ProjectRefModel(
                id=project_id,
                project_code=f"LEASE-{uuid.uuid4().hex[:12]}",
                project_name="Package download lease test",
            )
        )
        session.flush()
        session.add(
            FinalCutPackageSnapshotModel(
                id=package_id,
                project_ref_id=project_id,
                status="ready",
                package_filename=package_path.name,
                storage_path=str(package_path),
                expires_at=utcnow() - timedelta(seconds=1),
                total_bytes=package_path.stat().st_size,
                storage_bytes=package_path.stat().st_size,
                sha256="a" * 64,
                download_lease_id=lease_id,
                download_lease_expires_at=lease_expires_at,
                items=[],
            )
        )
        session.commit()
    return package_path, project_id


def _heartbeat(renew: Callable[[], bool | None]) -> PackageDownloadLeaseHeartbeat:
    from backend.app.modules.review_http.query_routes import PackageDownloadLeaseHeartbeat

    return PackageDownloadLeaseHeartbeat(renew, interval_seconds=0.01)


def test_runtime_writer_lock_serializes_shared_connection_probes() -> None:
    from backend.app.modules.final_cut_review.infra.database import RuntimeWriterLock

    class ProbeConnection:
        closed = False

        def __init__(self) -> None:
            self.active_calls = 0
            self.max_active_calls = 0
            self.guard = threading.Lock()

        def scalar(self, statement: object, _parameters: object = None) -> int | bool:
            with self.guard:
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
            try:
                time.sleep(0.01)
                return 4242 if str(statement).strip() == "SELECT pg_backend_pid()" else True
            finally:
                with self.guard:
                    self.active_calls -= 1

    connection = ProbeConnection()
    writer_lock = RuntimeWriterLock(
        connection=cast(Any, connection),
        writer_key=1,
        contract_key=2,
        fence_key=3,
        backend_pid=4242,
    )
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def probe() -> None:
        start.wait()
        try:
            writer_lock.assert_held()
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=probe) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(worker.is_alive() is False for worker in workers)
    assert errors == []
    assert connection.max_active_calls == 1


def _run_response(
    response: StreamingResponse,
    *,
    fail_during_send: bool = False,
    fail_on_final_send: bool = False,
) -> None:
    async def receive() -> Message:
        raise AssertionError("ASGI receive must not be called for spec 2.4 streaming")

    async def send(message: Message) -> None:
        if fail_during_send and message["type"] == "http.response.body":
            raise OSError("synthetic client disconnect")
        if fail_on_final_send and message["type"] == "http.response.body" and not message.get("more_body", False):
            raise OSError("synthetic final response failure")

    scope = cast(Scope, {"type": "http", "asgi": {"spec_version": "2.4"}})
    asyncio.run(response(scope, receive, send))


def test_download_heartbeat_extends_lease_past_original_expiry_and_blocks_cleanup(
    client: TestClient,
) -> None:
    from backend.app.maintenance_cleanup import _claim_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository, aware
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.settings import get_settings

    package_id = f"pkg_{uuid.uuid4().hex}"
    lease_id = uuid.uuid4().hex
    original_expiry = utcnow() + timedelta(milliseconds=20)
    package_path, _project_id = _create_package_with_lease(package_id, lease_id, original_expiry)
    renewed = threading.Event()

    def renew() -> bool:
        with SessionLocal() as session:
            result = SqlAlchemyReviewRepository(session, get_settings()).renew_package_download_lease(
                package_id,
                lease_id,
            )
            session.commit()
        renewed.set()
        return result

    heartbeat = _heartbeat(renew)
    heartbeat.start()
    try:
        assert renewed.wait(timeout=1)
    finally:
        heartbeat.stop()

    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        assert package.download_lease_id == lease_id
        assert package.download_lease_expires_at is not None
        assert aware(package.download_lease_expires_at) > original_expiry
        claims = _claim_expired_packages(session, original_expiry + timedelta(seconds=1))
        assert claims == []
    assert package_path.exists()


def test_background_lease_renewal_binds_writer_fence_in_native_thread(client: TestClient) -> None:
    from backend.app.modules.review_http.query_routes import renew_package_download_lease
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import utcnow

    package_id = f"pkg_{uuid.uuid4().hex}"
    lease_id = uuid.uuid4().hex
    _create_package_with_lease(package_id, lease_id, utcnow() + timedelta(minutes=5))
    fenced_thread_ids: list[int] = []
    results: list[bool | None] = []

    class WriterLock:
        @staticmethod
        def assert_transaction_held(_session: object) -> None:
            fenced_thread_ids.append(threading.get_ident())

    worker = threading.Thread(
        target=lambda: results.append(
            renew_package_download_lease(package_id, lease_id, cast(Any, WriterLock()))
        )
    )
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert results == [True]
    assert fenced_thread_ids == [worker.ident]


def test_streaming_response_close_stops_download_heartbeat(tmp_path: Path) -> None:
    from backend.app.modules.review_http.query_routes import regular_file_response

    source = tmp_path / "package.zip"
    source.write_bytes(b"package-body")
    renewed = threading.Event()
    renewal_count = 0

    def renew() -> bool:
        nonlocal renewal_count
        renewal_count += 1
        renewed.set()
        return True

    heartbeat = _heartbeat(renew)
    heartbeat.start()
    assert renewed.wait(timeout=1)
    stream_completed = threading.Event()
    response_completions: list[bool] = []
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_close=heartbeat.stop,
        on_stream_complete=stream_completed.set,
        on_response_complete=response_completions.append,
    )

    _run_response(response)

    assert heartbeat.is_alive is False
    assert stream_completed.is_set()
    assert response_completions == [True]
    stopped_count = renewal_count
    time.sleep(0.03)
    assert renewal_count == stopped_count


def test_streaming_response_send_error_stops_download_heartbeat(tmp_path: Path) -> None:
    from backend.app.modules.review_http.query_routes import regular_file_response

    source = tmp_path / "package.zip"
    source.write_bytes(b"package-body")
    renewed = threading.Event()

    def renew() -> bool:
        renewed.set()
        return True

    heartbeat = _heartbeat(renew)
    heartbeat.start()
    assert renewed.wait(timeout=1)
    stream_completed = threading.Event()
    response_completions: list[bool] = []
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_close=heartbeat.stop,
        on_stream_complete=stream_completed.set,
        on_response_complete=response_completions.append,
    )

    with pytest.raises(ClientDisconnect):
        _run_response(response, fail_during_send=True)

    assert heartbeat.is_alive is False
    assert not stream_completed.is_set()
    assert response_completions == [False]


def test_stream_completion_requires_successful_final_asgi_send(tmp_path: Path) -> None:
    from backend.app.modules.review_http.query_routes import regular_file_response

    source = tmp_path / "package.zip"
    source.write_bytes(b"package-body")
    interval_completed = threading.Event()
    response_completions: list[bool] = []
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_stream_complete=interval_completed.set,
        on_response_complete=response_completions.append,
    )

    with pytest.raises(ClientDisconnect):
        _run_response(response, fail_on_final_send=True)

    assert interval_completed.is_set()
    assert response_completions == [False]


def test_download_heartbeat_failure_aborts_stream_and_closes_resources(tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.review_http.query_routes import regular_file_response

    source = tmp_path / "package.zip"
    source.write_bytes(b"package-body")
    renewal_attempted = threading.Event()

    def renew() -> bool:
        renewal_attempted.set()
        return False

    heartbeat = _heartbeat(renew)
    heartbeat.start()
    assert renewal_attempted.wait(timeout=1)
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_close=heartbeat.stop,
        stream_guard=heartbeat.assert_healthy,
    )

    with pytest.raises(ReviewError, match="下载租约续期失败"):
        _run_response(response)

    assert heartbeat.is_alive is False


def test_stream_rejects_file_identity_change_after_download_lease_is_granted(tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.review_http.query_routes import regular_file_response

    source = tmp_path / "package.zip"
    source.write_bytes(b"original-body")
    original = source.stat()
    expected_identity = (
        original.st_dev,
        original.st_ino,
        original.st_ctime_ns,
        original.st_mtime_ns,
        original.st_size,
    )
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(b"tampered-body")
    replacement.replace(source)

    with pytest.raises(ReviewError, match="完整性校验失败"):
        regular_file_response(
            source,
            tmp_path,
            media_type="application/zip",
            expected_file_identity=expected_identity,
        )


def test_old_download_lease_cannot_renew_or_release_current_lease(client: TestClient) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository, aware
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.settings import get_settings

    package_id = f"pkg_{uuid.uuid4().hex}"
    current_lease_id = uuid.uuid4().hex
    current_expiry = utcnow() + timedelta(minutes=5)
    _create_package_with_lease(package_id, current_lease_id, current_expiry)

    with SessionLocal() as session:
        repository = SqlAlchemyReviewRepository(session, get_settings())
        assert repository.renew_package_download_lease(package_id, "stale-lease") is False
        assert repository.release_package_download_lease(package_id, "stale-lease", completed=False) is False
        session.commit()

    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        assert package.download_lease_id == current_lease_id
        assert package.download_lease_expires_at is not None
        assert aware(package.download_lease_expires_at) == current_expiry
        assert package.last_download_finished_at is None


def test_current_download_lease_release_without_completion_clears_lease_without_cooldown(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.settings import get_settings

    package_id = f"pkg_{uuid.uuid4().hex}"
    current_lease_id = uuid.uuid4().hex
    _create_package_with_lease(
        package_id,
        current_lease_id,
        utcnow() + timedelta(minutes=5),
    )

    with SessionLocal() as session:
        repository = SqlAlchemyReviewRepository(session, get_settings())
        assert repository.release_package_download_lease(
            package_id,
            current_lease_id,
            completed=False,
        ) is True
        session.commit()

    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        assert package.download_lease_id is None
        assert package.download_lease_expires_at is None
        assert package.last_download_finished_at is None


def test_early_eof_marks_stream_and_response_incomplete_and_releases_without_cooldown(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.domain.errors import ReviewError
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
    from backend.app.modules.review_http.query_routes import regular_file_response
    from backend.app.settings import get_settings

    package_id = f"pkg_{uuid.uuid4().hex}"
    current_lease_id = uuid.uuid4().hex
    package_path, _project_id = _create_package_with_lease(
        package_id,
        current_lease_id,
        utcnow() + timedelta(minutes=5),
    )
    stream_completed = threading.Event()
    response_completions: list[bool] = []
    lease_releases: list[bool] = []

    def release_current_lease(response_completed: bool) -> None:
        response_completions.append(response_completed)
        with SessionLocal() as session:
            released = SqlAlchemyReviewRepository(
                session,
                get_settings(),
            ).release_package_download_lease(
                package_id,
                current_lease_id,
                completed=response_completed and stream_completed.is_set(),
            )
            if released:
                session.commit()
            else:
                session.rollback()
            lease_releases.append(released)

    response = regular_file_response(
        package_path,
        get_settings().package_root,
        media_type="application/zip",
        on_stream_complete=stream_completed.set,
        on_response_complete=release_current_lease,
    )
    package_path.write_bytes(b"")

    with pytest.raises(ReviewError, match="文件读取提前结束"):
        _run_response(response)

    assert stream_completed.is_set() is False
    assert response_completions == [False]
    assert lease_releases == [True]
    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        assert package.download_lease_id is None
        assert package.download_lease_expires_at is None
        assert package.last_download_finished_at is None
