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


def _run_response(response: StreamingResponse, *, fail_during_send: bool = False) -> None:
    async def receive() -> Message:
        raise AssertionError("ASGI receive must not be called for spec 2.4 streaming")

    async def send(message: Message) -> None:
        if fail_during_send and message["type"] == "http.response.body":
            raise OSError("synthetic client disconnect")

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
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_close=heartbeat.stop,
    )

    _run_response(response)

    assert heartbeat.is_alive is False
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
    response = regular_file_response(
        source,
        tmp_path,
        media_type="application/zip",
        on_close=heartbeat.stop,
    )

    with pytest.raises(ClientDisconnect):
        _run_response(response, fail_during_send=True)

    assert heartbeat.is_alive is False


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
        assert repository.release_package_download_lease(package_id, "stale-lease") is False
        session.commit()

    with SessionLocal() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        assert package.download_lease_id == current_lease_id
        assert package.download_lease_expires_at is not None
        assert aware(package.download_lease_expires_at) == current_expiry
        assert package.last_download_finished_at is None
