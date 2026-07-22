from __future__ import annotations

import asyncio
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.safe_files import UnsafeFilePathError, exclusive_file_writer, pin_regular_file

from .conftest import upload_init_request


def _stage_upload_for_finalization(client: Any, label: str) -> tuple[str, bytes]:
    from .conftest import api_data, tiny_video_bytes

    blob = tiny_video_bytes(label.encode())
    initialized = upload_init_request(
        client,
        json={
            "original_filename": f"{label}.mp4",
            "mime_type": "video/mp4",
            "file_size": len(blob),
            "sha256": "0" * 64,
        },
    )
    upload_id = api_data(initialized)["upload_id"]
    uploaded = client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=blob)
    assert uploaded.status_code == 200, uploaded.text
    return upload_id, blob


def test_exclusive_file_writer_is_exclusive_private_and_durable(tmp_path: Path) -> None:
    managed_root = tmp_path / "uploads"
    destination = managed_root / "nested" / "part.bin"
    destination.parent.mkdir(parents=True)

    with exclusive_file_writer(destination, managed_root) as handle:
        handle.write(b"part-data")

    assert destination.read_bytes() == b"part-data"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        with exclusive_file_writer(destination, managed_root):
            pass


def test_exclusive_file_writer_cleanup_uses_pinned_parent_after_symlink_replacement(tmp_path: Path) -> None:
    managed_root = tmp_path / "uploads"
    original_parent = managed_root / "nested"
    moved_parent = managed_root / "nested-pinned"
    outside = tmp_path / "outside"
    original_parent.mkdir(parents=True)
    outside.mkdir()
    destination = original_parent / "part.bin"

    with pytest.raises(RuntimeError, match="forced stream failure"):
        with exclusive_file_writer(destination, managed_root) as handle:
            original_parent.rename(moved_parent)
            original_parent.symlink_to(outside, target_is_directory=True)
            handle.write(b"must-not-escape")
            raise RuntimeError("forced stream failure")

    assert not (moved_parent / destination.name).exists()
    assert not (outside / destination.name).exists()


def test_exclusive_file_writer_rejects_parent_replaced_before_open(tmp_path: Path) -> None:
    managed_root = tmp_path / "uploads"
    original_parent = managed_root / "nested"
    moved_parent = managed_root / "nested-original"
    outside = tmp_path / "outside"
    original_parent.mkdir(parents=True)
    outside.mkdir()
    destination = original_parent / "part.bin"
    original_parent.rename(moved_parent)
    original_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        with exclusive_file_writer(destination, managed_root) as handle:
            handle.write(b"must-not-escape")

    assert not (moved_parent / destination.name).exists()
    assert not (outside / destination.name).exists()


def test_exclusive_file_writer_removes_candidate_when_file_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files

    managed_root = tmp_path / "uploads"
    managed_root.mkdir()
    destination = managed_root / "part.bin"
    original_fsync = os.fsync
    failed = False

    def fail_file_fsync(descriptor: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed = True
            raise OSError("forced file fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(safe_files.os, "fsync", fail_file_fsync)

    with pytest.raises(OSError, match="forced file fsync failure"):
        with exclusive_file_writer(destination, managed_root) as handle:
            handle.write(b"part-data")

    assert failed
    assert not destination.exists()


def test_pin_regular_file_rejects_simulated_cross_device_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files

    managed_root = tmp_path / "storage"
    managed_root.mkdir()
    asset = managed_root / "asset.bin"
    asset.write_bytes(b"asset")
    original_stat = safe_files.os.stat

    def cross_device_stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        metadata = original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == asset.name and dir_fd is not None and follow_symlinks is False:
            values = list(metadata)
            values[2] = metadata.st_dev + 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(safe_files.os, "stat", cross_device_stat)

    with pytest.raises(UnsafeFilePathError, match="crosses configured filesystem"):
        with pin_regular_file(asset, managed_root):
            pass


def test_streaming_put_stages_body_before_lock_and_reuses_locked_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from backend.app.upload_parts import UploadPartAdmissionLimiter

    events: list[str] = []
    locked_upload = object()
    session = SimpleNamespace(rollback=lambda: events.append("rollback"))
    storage_root = tmp_path / "storage"
    upload_root = storage_root / "uploads"
    upload_root.mkdir(parents=True)
    destination = upload_root / "candidate.part"
    settings = SimpleNamespace(
        storage_root=storage_root,
        max_upload_parts=256,
        max_upload_part_bytes=64,
        upload_part_read_timeout_seconds=1.0,
        upload_part_io_workers=2,
        max_inflight_upload_parts_per_principal=2,
        max_inflight_upload_parts_per_session=2,
        max_inflight_upload_part_candidates=4,
    )
    expected_locked_upload = locked_upload

    class StubRequest:
        headers = {"content-length": "4"}

    class StubMediaService:
        def __init__(self, received_session: object, received_settings: object, _context: object) -> None:
            assert received_session is session
            assert received_settings is settings
            self.upload_root = upload_root

        def ensure_can_receive_part(self, upload_id: str, part_no: int) -> object:
            assert upload_id == "upl_test"
            assert part_no == 1
            events.append("lock")
            return locked_upload

        def put_part_file(
            self,
            upload_id: str,
            part_no: int,
            staged_path: Path,
            size: int,
            *,
            locked_upload: object | None = None,
        ) -> SimpleNamespace:
            assert (upload_id, part_no, staged_path, size) == ("upl_test", 1, destination, 4)
            assert locked_upload is expected_locked_upload
            events.append("metadata")
            return SimpleNamespace(response={"upload_id": upload_id})

    def fake_staging_path(_upload_root: Path, _upload_id: str, _part_no: int) -> Path:
        events.append("candidate")
        return destination

    async def fake_write_limited_body(
        _request: object,
        received_destination: Path,
        max_bytes: int,
        *,
        managed_root: Path | None = None,
        read_timeout_seconds: float,
        io_workers: int,
    ) -> int:
        assert received_destination == destination
        assert max_bytes == 4
        assert managed_root == upload_root
        assert read_timeout_seconds == 1.0
        assert io_workers == 2
        events.append("stream")
        return 4

    def fake_commit(received_session: object, _service: object, _prepared: object) -> None:
        assert received_session is session
        events.append("commit")

    def fake_preflight(_context: object, _upload_id: str, _part_no: int) -> int:
        events.append("preflight")
        return 4

    monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        upload_routes,
        "upload_context",
        lambda *_args: SimpleNamespace(principal=SimpleNamespace(kind="system", id="owner")),
    )
    monkeypatch.setattr(upload_routes, "UPLOAD_PART_ADMISSION_LIMITER", UploadPartAdmissionLimiter())
    monkeypatch.setattr(upload_routes, "LocalMediaService", StubMediaService)
    monkeypatch.setattr(
        upload_routes,
        "validate_upload_before_stream",
        fake_preflight,
    )
    monkeypatch.setattr(upload_routes, "staging_part_path", fake_staging_path)
    monkeypatch.setattr(upload_routes, "write_limited_body", fake_write_limited_body)
    monkeypatch.setattr(upload_routes, "commit_prepared_part", fake_commit)

    result = asyncio.run(
        upload_routes.put_part(
            "upl_test",
            1,
            StubRequest(),  # type: ignore[arg-type]
            session=session,  # type: ignore[arg-type]
            request_id="req_test",
            fj_write_guard=None,
            x_write_guard_verified=None,
        )
    )

    assert result["data"] == {"upload_id": "upl_test"}
    assert events == ["preflight", "candidate", "stream", "lock", "metadata", "commit"]


def test_streaming_put_closes_preflight_connection_before_body_and_rechecks_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session, sessionmaker

    from backend.app.upload_parts import UploadPartAdmissionLimiter

    engine = create_engine("sqlite:///:memory:")
    SessionFactory = sessionmaker(bind=engine)
    active_connections = 0
    events: list[str] = []
    upload_id = f"upl_{uuid.uuid4().hex}"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    settings = SimpleNamespace(
        storage_root=storage_root,
        max_upload_parts=256,
        max_upload_part_bytes=64,
        upload_part_read_timeout_seconds=1.0,
        upload_part_io_workers=2,
        max_inflight_upload_parts_per_principal=2,
        max_inflight_upload_parts_per_session=2,
        max_inflight_upload_part_candidates=4,
    )

    @event.listens_for(engine, "checkout")
    def count_checkout(*_args: object) -> None:
        nonlocal active_connections
        active_connections += 1
        events.append("checkout")

    @event.listens_for(engine, "checkin")
    def count_checkin(*_args: object) -> None:
        nonlocal active_connections
        active_connections -= 1
        events.append("checkin")

    with SessionFactory() as session:

        class InspectingRequest:
            headers = {"content-length": "4"}

            async def stream(self):
                assert not session.in_transaction()
                assert active_connections == 0
                events.append("body")
                yield b"data"

        class StubMediaService:
            def __init__(self, received_session: Session, _settings: object, _context: object) -> None:
                self.received_session = received_session

            def claim_part_stream(self, received_upload_id: str, part_no: int) -> int:
                assert (received_upload_id, part_no) == (upload_id, 1)
                self.received_session.execute(text("SELECT 1"))
                events.append("preflight")
                return 4

            def ensure_can_receive_part(self, received_upload_id: str, part_no: int) -> object:
                assert (received_upload_id, part_no) == (upload_id, 1)
                assert self.received_session is session
                self.received_session.execute(text("SELECT 1"))
                events.append("lock")
                return object()

            def put_part_file(
                self,
                received_upload_id: str,
                part_no: int,
                staged_path: Path,
                size: int,
                *,
                locked_upload: object | None = None,
            ) -> SimpleNamespace:
                assert (received_upload_id, part_no, size) == (upload_id, 1, 4)
                assert staged_path.parent == storage_root / "uploads"
                assert locked_upload is not None
                events.append("metadata")
                return SimpleNamespace(response={"upload_id": upload_id})

        def fake_commit(_session: object, _service: object, _prepared: object) -> None:
            session.commit()
            events.append("commit")

        monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)
        monkeypatch.setattr(
            upload_routes,
            "upload_context",
            lambda *_args: SimpleNamespace(principal=SimpleNamespace(kind="system", id="owner")),
        )
        monkeypatch.setattr(upload_routes, "UPLOAD_PART_ADMISSION_LIMITER", UploadPartAdmissionLimiter())
        monkeypatch.setattr(upload_routes, "LocalMediaService", StubMediaService)
        monkeypatch.setattr(upload_routes, "SessionLocal", SessionFactory)
        monkeypatch.setattr(upload_routes, "commit_prepared_part", fake_commit)

        result = asyncio.run(
            upload_routes.put_part(
                upload_id,
                1,
                InspectingRequest(),  # type: ignore[arg-type]
                session=session,
                request_id="req-no-checkout",
                fj_write_guard=None,
                x_write_guard_verified=None,
            )
        )

    engine.dispose()
    assert result["data"] == {"upload_id": upload_id}
    assert active_connections == 0
    assert events == [
        "checkout",
        "preflight",
        "checkin",
        "body",
        "checkout",
        "lock",
        "metadata",
        "checkin",
        "commit",
    ]


def test_streaming_put_database_phase_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from backend.app.upload_parts import UploadPartAdmissionLimiter

    upload_id = f"upl_{uuid.uuid4().hex}"
    storage_root = tmp_path / "storage"
    upload_root = storage_root / "uploads"
    upload_root.mkdir(parents=True)
    staged_path = upload_root / f"{upload_id}.parts.1.{uuid.uuid4().hex}.part"
    settings = SimpleNamespace(
        storage_root=storage_root,
        max_upload_parts=256,
        max_upload_part_bytes=64,
        upload_part_read_timeout_seconds=1.0,
        upload_part_io_workers=2,
        max_inflight_upload_parts_per_principal=2,
        max_inflight_upload_parts_per_session=2,
        max_inflight_upload_part_candidates=4,
    )
    database_started = threading.Event()
    release_database = threading.Event()

    class StubRequest:
        headers = {"content-length": "4"}

    async def fake_write(*_args: object, **_kwargs: object) -> int:
        staged_path.write_bytes(b"data")
        return 4

    def blocking_bind(*_args: object, **_kwargs: object) -> SimpleNamespace:
        database_started.set()
        assert release_database.wait(timeout=2)
        return SimpleNamespace(response={"upload_id": upload_id})

    monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        upload_routes,
        "upload_context",
        lambda *_args: SimpleNamespace(principal=SimpleNamespace(kind="system", id="owner")),
    )
    monkeypatch.setattr(upload_routes, "UPLOAD_PART_ADMISSION_LIMITER", UploadPartAdmissionLimiter())
    monkeypatch.setattr(upload_routes, "validate_upload_before_stream", lambda *_args: 4)
    monkeypatch.setattr(upload_routes, "staging_part_path", lambda *_args: staged_path)
    monkeypatch.setattr(upload_routes, "write_limited_body", fake_write)
    monkeypatch.setattr(upload_routes, "bind_and_commit_part", blocking_bind)

    async def exercise() -> dict[str, object]:
        heartbeat_ticks = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            while not release_database.is_set():
                heartbeat_ticks += 1
                await asyncio.sleep(0.002)

        heartbeat_task = asyncio.create_task(heartbeat())
        upload_task = asyncio.create_task(
            upload_routes.put_part(
                upload_id,
                1,
                StubRequest(),  # type: ignore[arg-type]
                session=SimpleNamespace(),  # type: ignore[arg-type]
                request_id="req-db-heartbeat",
                fj_write_guard=None,
                x_write_guard_verified=None,
            )
        )
        assert await asyncio.to_thread(database_started.wait, 1)
        await asyncio.sleep(0.02)
        assert heartbeat_ticks >= 2
        release_database.set()
        result = await upload_task
        await heartbeat_task
        return result

    assert asyncio.run(exercise())["data"] == {"upload_id": upload_id}


def test_write_limited_body_timeout_removes_candidate(tmp_path: Path) -> None:
    from backend.app.modules.review_http.upload_routes import UploadBodyTimeout, write_limited_body

    class SlowRequest:
        async def stream(self):
            yield b"partial"
            await asyncio.Event().wait()

    staged_path = tmp_path / "timed-out.part"

    async def exercise() -> UploadBodyTimeout:
        with pytest.raises(UploadBodyTimeout) as exc_info:
            await write_limited_body(
                SlowRequest(),  # type: ignore[arg-type]
                staged_path,
                max_bytes=64,
                read_timeout_seconds=0.01,
            )
        await exc_info.value.cleanup_task
        return exc_info.value

    error = asyncio.run(exercise())
    assert error.code == "RESOURCE_STATE_CONFLICT"
    assert not staged_path.exists()


def test_blocked_upload_fsync_does_not_block_event_loop_and_timeout_cleans_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.safe_files as safe_files
    from backend.app.modules.review_http.upload_routes import UploadBodyTimeout, write_limited_body

    fsync_started = threading.Event()
    release_fsync = threading.Event()
    original_fsync = safe_files.os.fsync

    def blocking_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode) and not fsync_started.is_set():
            fsync_started.set()
            assert release_fsync.wait(timeout=2)
        original_fsync(descriptor)

    monkeypatch.setattr(safe_files.os, "fsync", blocking_fsync)
    staged_path = tmp_path / "blocked-fsync.part"

    class OneChunkRequest:
        async def stream(self):
            yield b"data"

    async def exercise() -> None:
        heartbeat_ticks = 0

        async def heartbeat() -> None:
            nonlocal heartbeat_ticks
            while not release_fsync.is_set():
                heartbeat_ticks += 1
                await asyncio.sleep(0.002)

        heartbeat_task = asyncio.create_task(heartbeat())
        upload_task = asyncio.create_task(
            write_limited_body(
                OneChunkRequest(),  # type: ignore[arg-type]
                staged_path,
                max_bytes=64,
                read_timeout_seconds=0.02,
                io_workers=1,
            )
        )
        assert await asyncio.to_thread(fsync_started.wait, 1)
        await asyncio.sleep(0.04)
        assert heartbeat_ticks >= 2
        assert upload_task.done()
        with pytest.raises(UploadBodyTimeout) as exc_info:
            await upload_task
        assert exc_info.value.code == "RESOURCE_STATE_CONFLICT"
        assert staged_path.exists()
        release_fsync.set()
        await exc_info.value.cleanup_task
        await heartbeat_task

    asyncio.run(exercise())
    assert not staged_path.exists()


def test_expired_finalization_lease_without_takeover_can_publish(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, UploadSessionModel, utcnow

    from .conftest import api_data

    upload_id, _blob = _stage_upload_for_finalization(client, "expired-without-takeover")
    original_finalize = service_module.LocalMediaService.finalize_claim

    def expire_before_publish(
        service: service_module.LocalMediaService,
        claim: service_module.UploadFinalizationClaim,
    ) -> service_module.FinalizedUploadFile:
        finalized = original_finalize(service, claim)
        with SessionLocal() as session:
            upload = session.get(UploadSessionModel, upload_id)
            assert upload is not None and upload.finalization_lease_id == claim.lease_id
            upload.finalization_lease_expires_at = utcnow() - timedelta(seconds=1)
            session.commit()
        return finalized

    monkeypatch.setattr(service_module.LocalMediaService, "finalize_claim", expire_before_publish)
    completed = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"expired-without-takeover-{upload_id}"},
    )

    assert completed.status_code == 200, completed.text
    file_id = api_data(completed)["file_id"]
    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None and upload.status == "completed"
        assert upload.file_id == file_id
        assert session.get(FileObjectModel, file_id) is not None


def test_expired_finalization_lease_only_allows_matching_idempotency_identity(client: Any) -> None:
    import backend.app.modules.review_media.service as service_module

    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    upload_id, _blob = _stage_upload_for_finalization(client, "expired-identity")
    key_hash = "a" * 64
    request_hash = "b" * 64
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        context = ExecutionContext(
            entry_source="edit",
            request_id=uuid.uuid4().hex,
            principal=PrincipalRef(
                kind=upload.owner_principal_kind,
                id=upload.owner_principal_id,
                project_ref_ids=("*",),
            ),
            write_guard=WriteGuardState(mode="none", verified=True),
        )

    with SessionLocal() as claimant:
        original = service_module.LocalMediaService(claimant, get_settings(), context).claim_completion(
            upload_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        assert isinstance(original, service_module.UploadFinalizationClaim)
        claimant.commit()

    with SessionLocal() as expiry:
        upload = expiry.get(UploadSessionModel, upload_id)
        assert upload is not None
        upload.finalization_lease_expires_at = utcnow() - timedelta(seconds=1)
        expiry.commit()

    for wrong_key_hash, wrong_request_hash in (("c" * 64, request_hash), (key_hash, "d" * 64)):
        with SessionLocal() as rejected_session:
            with pytest.raises(ReviewError) as caught:
                service_module.LocalMediaService(rejected_session, get_settings(), context).claim_completion(
                    upload_id,
                    idempotency_key_hash=wrong_key_hash,
                    request_hash=wrong_request_hash,
                )
            assert caught.value.code == "IDEMPOTENCY_CONFLICT"
            rejected_session.rollback()
        with SessionLocal() as observer:
            upload = observer.get(UploadSessionModel, upload_id)
            assert upload is not None
            assert upload.finalization_lease_id == original.lease_id
            assert upload.finalization_idempotency_key_hash == key_hash
            assert upload.finalization_request_hash == request_hash

    with SessionLocal() as takeover_session:
        replacement = service_module.LocalMediaService(takeover_session, get_settings(), context).claim_completion(
            upload_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        assert isinstance(replacement, service_module.UploadFinalizationClaim)
        assert replacement.recovered_expired_lease is True
        assert replacement.file_id == original.file_id
        assert replacement.lease_id != original.lease_id
        takeover_session.commit()


def test_expired_finalization_lease_rejects_old_worker_after_takeover(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.application.context import ExecutionContext
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    from .conftest import api_error

    upload_id, _blob = _stage_upload_for_finalization(client, "expired-after-takeover")
    original_finalize = service_module.LocalMediaService.finalize_claim
    takeover: dict[str, object] = {}

    def takeover_before_old_publish(
        service: service_module.LocalMediaService,
        old_claim: service_module.UploadFinalizationClaim,
    ) -> service_module.FinalizedUploadFile:
        finalized = original_finalize(service, old_claim)
        with SessionLocal() as expiry_session:
            upload = expiry_session.get(UploadSessionModel, upload_id)
            assert upload is not None and upload.finalization_lease_id == old_claim.lease_id
            upload.finalization_lease_expires_at = utcnow() - timedelta(seconds=1)
            key_hash = upload.finalization_idempotency_key_hash
            request_hash = upload.finalization_request_hash
            assert key_hash is not None and request_hash is not None
            expiry_session.commit()
        with SessionLocal() as takeover_session:
            replacement = service_module.LocalMediaService(
                takeover_session,
                get_settings(),
                service.context,
            ).claim_completion(
                upload_id,
                idempotency_key_hash=key_hash,
                request_hash=request_hash,
            )
            assert isinstance(replacement, service_module.UploadFinalizationClaim)
            assert replacement.recovered_expired_lease is True
            assert replacement.file_id == old_claim.file_id
            assert replacement.lease_id != old_claim.lease_id
            takeover_session.commit()
        takeover["claim"] = replacement
        takeover["finalized"] = finalized
        takeover["context"] = service.context
        return finalized

    monkeypatch.setattr(service_module.LocalMediaService, "finalize_claim", takeover_before_old_publish)
    rejected = client.post(
        f"/api/v1/files/uploads/{upload_id}/complete",
        headers={"Idempotency-Key": f"expired-after-takeover-{upload_id}"},
    )

    assert rejected.status_code == 409
    assert api_error(rejected)["code"] == "RESOURCE_STATE_CONFLICT"
    replacement = takeover["claim"]
    finalized = takeover["finalized"]
    context = takeover["context"]
    assert isinstance(replacement, service_module.UploadFinalizationClaim)
    assert isinstance(finalized, service_module.FinalizedUploadFile)
    assert isinstance(context, ExecutionContext)
    with SessionLocal() as publish_session:
        prepared = service_module.LocalMediaService(
            publish_session,
            get_settings(),
            context,
        ).publish_completion(replacement, finalized)
        publish_session.commit()
    assert prepared.response["status"] == "completed"


def test_failed_finalizer_cleanup_never_removes_canonical_takeover_file(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module

    from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_settings

    upload_id, blob = _stage_upload_for_finalization(client, "failed-finalizer-cleanup")
    key_hash = "e" * 64
    request_hash = "f" * 64
    with SessionLocal() as observer:
        upload = observer.get(UploadSessionModel, upload_id)
        assert upload is not None
        context = ExecutionContext(
            entry_source="edit",
            request_id=uuid.uuid4().hex,
            principal=PrincipalRef(
                kind=upload.owner_principal_kind,
                id=upload.owner_principal_id,
                project_ref_ids=("*",),
            ),
            write_guard=WriteGuardState(mode="none", verified=True),
        )

    with SessionLocal() as claimant:
        old_claim = service_module.LocalMediaService(claimant, get_settings(), context).claim_completion(
            upload_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        assert isinstance(old_claim, service_module.UploadFinalizationClaim)
        claimant.commit()

    original_unlink = service_module.os.unlink
    failed = False

    def fail_first_staging_unlink(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal failed
        if path.startswith(f".{old_claim.file_id}.") and not failed:
            failed = True
            raise OSError("synthetic post-link failure")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(service_module.os, "unlink", fail_first_staging_unlink)
    with SessionLocal() as worker_session:
        with pytest.raises(OSError, match="synthetic post-link failure"):
            service_module.LocalMediaService(worker_session, get_settings(), context).finalize_claim(old_claim)

    canonical = get_settings().storage_root / "files" / old_claim.file_id
    assert failed is True
    assert canonical.read_bytes() == blob

    with SessionLocal() as expiry:
        upload = expiry.get(UploadSessionModel, upload_id)
        assert upload is not None
        upload.finalization_lease_expires_at = utcnow() - timedelta(seconds=1)
        expiry.commit()
    with SessionLocal() as replacement_session:
        replacement_service = service_module.LocalMediaService(replacement_session, get_settings(), context)
        replacement_claim = replacement_service.claim_completion(
            upload_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        assert isinstance(replacement_claim, service_module.UploadFinalizationClaim)
        replacement_session.commit()
    with SessionLocal() as publish_session:
        publish_service = service_module.LocalMediaService(publish_session, get_settings(), context)
        finalized = publish_service.finalize_claim(replacement_claim)
        prepared = publish_service.publish_completion(replacement_claim, finalized)
        publish_session.commit()

    assert prepared.response["status"] == "completed"
    assert canonical.read_bytes() == blob


def test_upload_part_admission_bounds_principal_session_and_total() -> None:
    from backend.app.upload_parts import UploadPartAdmissionLimiter

    limiter = UploadPartAdmissionLimiter()
    first = limiter.try_acquire(
        "system:owner-a",
        "upl_a",
        max_per_principal=2,
        max_per_upload=1,
        max_total=3,
    )
    assert first is not None
    assert (
        limiter.try_acquire(
            "system:owner-b",
            "upl_a",
            max_per_principal=2,
            max_per_upload=1,
            max_total=3,
        )
        is None
    )
    second = limiter.try_acquire(
        "system:owner-a",
        "upl_b",
        max_per_principal=2,
        max_per_upload=1,
        max_total=3,
    )
    assert second is not None
    assert (
        limiter.try_acquire(
            "system:owner-a",
            "upl_c",
            max_per_principal=2,
            max_per_upload=1,
            max_total=3,
        )
        is None
    )
    third = limiter.try_acquire(
        "system:owner-c",
        "upl_c",
        max_per_principal=2,
        max_per_upload=1,
        max_total=3,
    )
    assert third is not None
    assert (
        limiter.try_acquire(
            "system:owner-d",
            "upl_d",
            max_per_principal=2,
            max_per_upload=1,
            max_total=3,
        )
        is None
    )
    first.release()
    second.release()
    third.release()

    active = limiter.try_acquire(
        "system:owner-a",
        "upl_exclusive",
        max_per_principal=2,
        max_per_upload=1,
        max_total=3,
    )
    assert active is not None
    assert limiter.try_acquire_exclusive("upl_exclusive") is None
    active.release()
    exclusive = limiter.try_acquire_exclusive("upl_exclusive")
    assert exclusive is not None
    assert (
        limiter.try_acquire(
            "system:owner-a",
            "upl_exclusive",
            max_per_principal=2,
            max_per_upload=1,
            max_total=3,
        )
        is None
    )
    exclusive.release()


def test_shared_principal_holds_ten_distinct_upload_admissions() -> None:
    from backend.app.upload_parts import UploadPartAdmissionLimiter

    limiter = UploadPartAdmissionLimiter()
    leases = []
    for index in range(10):
        lease = limiter.try_acquire(
            "system:shared-lan-principal",
            f"upl_{index:032x}",
            max_per_principal=16,
            max_per_upload=1,
            max_total=64,
        )
        assert lease is not None
        leases.append(lease)

    assert (
        limiter.try_acquire(
            "system:shared-lan-principal",
            "upl_00000000000000000000000000000000",
            max_per_principal=16,
            max_per_upload=1,
            max_total=64,
        )
        is None
    )

    for lease in leases:
        lease.release()


def test_compose_keeps_process_local_upload_limiter_single_worker_and_replica() -> None:
    import yaml  # type: ignore[import-untyped]

    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    backend = compose["services"]["backend"]
    command = " ".join(backend["command"])
    environment = backend["environment"]

    assert "container_name" not in backend
    assert backend["deploy"]["replicas"] == 1
    assert "uvicorn backend.app.main:app" in command
    assert "--workers 1" in command
    assert environment["UPLOAD_RUNTIME_WORKER_COUNT"] == 1
    assert "MAX_INFLIGHT_UPLOAD_PART_CANDIDATES" in environment
    assert "MAX_ACTIVE_UPLOAD_SESSIONS_GLOBAL" in environment
    assert "MAX_RESERVED_UPLOAD_BYTES_GLOBAL" in environment


def test_competing_slow_body_is_rejected_before_lock_and_leaves_one_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from backend.app.upload_parts import UploadPartAdmissionLimiter

    upload_id = f"upl_{uuid.uuid4().hex}"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    upload_root = storage_root / "uploads"
    settings = SimpleNamespace(
        storage_root=storage_root,
        max_upload_parts=256,
        max_upload_part_bytes=64,
        upload_part_read_timeout_seconds=1.0,
        upload_part_io_workers=2,
        max_inflight_upload_parts_per_principal=16,
        max_inflight_upload_parts_per_session=1,
        max_inflight_upload_part_candidates=64,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    class BlockingRequest:
        headers = {"content-length": "4"}

        async def stream(self):
            started.set()
            await release.wait()
            yield b"data"

    class FastRequest:
        headers = {"content-length": "4"}

        async def stream(self):
            events.append("unexpected-second-stream")
            yield b"evil"

    class StubMediaService:
        def __init__(self, _session: object, _settings: object, _context: object) -> None:
            events.append("service")
            self.upload_root = upload_root

        def ensure_can_receive_part(self, received_upload_id: str, part_no: int) -> object:
            assert (received_upload_id, part_no) == (upload_id, 1)
            events.append("lock")
            return object()

        def put_part_file(
            self,
            received_upload_id: str,
            part_no: int,
            staged_path: Path,
            size: int,
            *,
            locked_upload: object | None = None,
        ) -> SimpleNamespace:
            assert (received_upload_id, part_no, size) == (upload_id, 1, 4)
            assert staged_path.parent == upload_root
            assert locked_upload is not None
            events.append("metadata")
            return SimpleNamespace(response={"upload_id": upload_id}, candidate_path=staged_path)

    def fake_commit(_session: object, _service: object, _prepared: object) -> None:
        events.append("commit")

    monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        upload_routes,
        "upload_context",
        lambda *_args: SimpleNamespace(principal=SimpleNamespace(kind="system", id="owner")),
    )
    monkeypatch.setattr(upload_routes, "UPLOAD_PART_ADMISSION_LIMITER", UploadPartAdmissionLimiter())
    monkeypatch.setattr(upload_routes, "LocalMediaService", StubMediaService)
    monkeypatch.setattr(upload_routes, "validate_upload_before_stream", lambda *_args: 4)
    monkeypatch.setattr(upload_routes, "commit_prepared_part", fake_commit)

    async def exercise() -> dict[str, object]:
        first = asyncio.create_task(
            upload_routes.put_part(
                upload_id,
                1,
                BlockingRequest(),  # type: ignore[arg-type]
                session=SimpleNamespace(rollback=lambda: None),  # type: ignore[arg-type]
                request_id="req-first",
                fj_write_guard=None,
                x_write_guard_verified=None,
            )
        )
        await started.wait()
        assert events == []
        with pytest.raises(ReviewError) as exc_info:
            await upload_routes.put_part(
                upload_id,
                1,
                FastRequest(),  # type: ignore[arg-type]
                session=SimpleNamespace(rollback=lambda: None),  # type: ignore[arg-type]
                request_id="req-second",
                fj_write_guard=None,
                x_write_guard_verified=None,
            )
        assert exc_info.value.code == "RESOURCE_STATE_CONFLICT"
        release.set()
        return await first

    result = asyncio.run(exercise())

    assert result["data"] == {"upload_id": upload_id}
    assert events == ["service", "lock", "metadata", "commit"]
    candidates = list(upload_root.glob(f"{upload_id}.parts.1.*.part"))
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == b"data"


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_put_part_commit_failure_discards_candidate_and_preserves_previous_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_fails: bool,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes
    from backend.app.modules.review_media.service import PreparedUploadPart

    previous = tmp_path / "previous.part"
    candidate = tmp_path / "candidate.part"
    previous.write_bytes(b"committed")
    candidate.write_bytes(b"replacement")
    prepared = PreparedUploadPart(
        response={"upload_id": "upl_test"},
        candidate_path=candidate,
        superseded_path=previous,
    )
    rolled_back = False

    class FailingSession:
        def commit(self) -> None:
            raise RuntimeError("synthetic commit failure")

        def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True
            if rollback_fails:
                raise RuntimeError("synthetic rollback failure")

    class StubService:
        def discard_uncommitted_part(self, received: PreparedUploadPart) -> None:
            received.candidate_path.unlink()

        def discard_superseded_part(self, _received: PreparedUploadPart) -> None:
            raise AssertionError("previous part must survive a failed commit")

    monkeypatch.setattr(upload_routes, "_part_commit_outcome", lambda _prepared: False)

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        upload_routes.commit_prepared_part(
            FailingSession(),  # type: ignore[arg-type]
            StubService(),  # type: ignore[arg-type]
            prepared,
        )

    assert rolled_back
    assert not candidate.exists()
    assert previous.read_bytes() == b"committed"


def test_put_part_file_reuses_locked_model_without_second_select(
    tmp_path: Path,
) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.upload_parts import new_upload_part_path

    upload_id = f"upl_{uuid.uuid4().hex}"
    upload_root = tmp_path / "storage" / "uploads"
    upload_root.mkdir(parents=True)
    candidate = new_upload_part_path(upload_root, upload_id, 1)
    with exclusive_file_writer(candidate, upload_root) as handle:
        handle.write(b"data")
    locked_upload = UploadSessionModel(
        id=upload_id,
        original_filename="locked.mp4",
        mime_type="video/mp4",
        declared_size=4,
        expected_sha256="0" * 64,
        status="initiated",
        temp_path=str(upload_root / f"{upload_id}.parts"),
        owner_principal_id="owner",
        owner_principal_kind="system",
        received_parts={},
        duration_ms=1000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
    )
    service: Any = object.__new__(service_module.LocalMediaService)
    service.settings = SimpleNamespace(max_upload_parts=256, max_upload_part_bytes=64, max_upload_bytes=64)
    service.upload_root = upload_root
    service.context = SimpleNamespace(principal=SimpleNamespace(id="owner", kind="system"))
    service.session = SimpleNamespace(flush=lambda: None)

    def fail_second_select(_upload_id: str) -> UploadSessionModel:
        raise AssertionError("locked upload row was selected twice")

    service._get_upload_for_update = fail_second_select  # type: ignore[method-assign]

    prepared = service.put_part_file(upload_id, 1, candidate, 4, locked_upload=locked_upload)

    assert prepared.candidate_path == candidate
    assert locked_upload.received_parts["1"] == {"size": 4, "path": str(candidate)}


def test_put_part_file_rechecks_declared_remaining_bytes_under_lock(tmp_path: Path) -> None:
    import backend.app.modules.review_media.service as service_module
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel
    from backend.app.upload_parts import new_upload_part_path

    upload_id = f"upl_{uuid.uuid4().hex}"
    upload_root = tmp_path / "storage" / "uploads"
    upload_root.mkdir(parents=True)
    existing = new_upload_part_path(upload_root, upload_id, 1)
    existing.write_bytes(b"abc")
    candidate = new_upload_part_path(upload_root, upload_id, 2)
    candidate.write_bytes(b"de")
    locked_upload = UploadSessionModel(
        id=upload_id,
        original_filename="locked-remaining.mp4",
        mime_type="video/mp4",
        declared_size=4,
        expected_sha256="0" * 64,
        status="receiving",
        temp_path=str(upload_root / f"{upload_id}.parts"),
        owner_principal_id="owner",
        owner_principal_kind="system",
        received_parts={"1": {"size": 3, "path": str(existing)}},
        duration_ms=1000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
    )
    service: Any = object.__new__(service_module.LocalMediaService)
    service.settings = SimpleNamespace(max_upload_parts=256, max_upload_part_bytes=64, max_upload_bytes=64)
    service.upload_root = upload_root
    service.context = SimpleNamespace(principal=SimpleNamespace(id="owner", kind="system"))
    service.session = SimpleNamespace(flush=lambda: None)

    with pytest.raises(ReviewError) as exc_info:
        service.put_part_file(upload_id, 2, candidate, 2, locked_upload=locked_upload)

    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert candidate.read_bytes() == b"de"


def test_streaming_put_rejects_content_length_over_declared_remaining_before_staging(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes

    from .conftest import api_data, api_error

    init = upload_init_request(
        client,
        json={
            "original_filename": "declared-remaining.mp4",
            "mime_type": "video/mp4",
            "file_size": 10,
            "sha256": "a" * 64,
        },
    )
    upload_id = api_data(init)["upload_id"]
    assert client.put(f"/api/v1/files/uploads/{upload_id}/parts/1", content=b"123456").status_code == 200
    staging_called = False

    def reject_staging(*_args: object) -> Path:
        nonlocal staging_called
        staging_called = True
        raise AssertionError("staging must not start when Content-Length exceeds declared remaining bytes")

    monkeypatch.setattr(upload_routes, "staging_part_path", reject_staging)
    response = client.put(f"/api/v1/files/uploads/{upload_id}/parts/2", content=b"12345")

    assert response.status_code == 413
    assert api_error(response)["code"] == "FILE_TOO_LARGE"
    assert staging_called is False


def test_streaming_put_rejects_negative_content_length_before_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_http.upload_routes as upload_routes

    class StubRequest:
        headers = {"content-length": "-1"}

    settings = SimpleNamespace(storage_root=tmp_path, max_upload_parts=256, max_upload_part_bytes=64)
    monkeypatch.setattr(upload_routes, "get_settings", lambda: settings)

    with pytest.raises(ReviewError) as exc_info:
        asyncio.run(
            upload_routes.put_part(
                "upl_test",
                1,
                StubRequest(),  # type: ignore[arg-type]
                session=object(),  # type: ignore[arg-type]
                request_id="req_test",
                fj_write_guard=None,
                x_write_guard_verified=None,
            )
        )

    assert exc_info.value.code == "VALIDATION_ERROR"


def test_local_media_put_part_uses_safe_writer_and_reuses_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.review_media.service as service_module

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    destination = upload_root / "upl_test.parts.1.candidate.part"
    service: Any = object.__new__(service_module.LocalMediaService)
    service.settings = SimpleNamespace(max_upload_part_bytes=64)
    service.upload_root = upload_root
    locked_upload = SimpleNamespace(id="upl_test")
    events: list[str] = []

    def lock_upload(upload_id: str, part_no: int) -> object:
        assert (upload_id, part_no) == ("upl_test", 1)
        events.append("lock")
        return locked_upload

    def prepare_part(
        upload_id: str,
        part_no: int,
        staged_path: Path,
        size: int,
        *,
        locked_upload: object | None = None,
    ) -> service_module.PreparedUploadPart:
        assert (upload_id, part_no, staged_path, size) == ("upl_test", 1, destination, 4)
        assert locked_upload is expected_locked_upload
        events.append("metadata")
        return service_module.PreparedUploadPart(
            response={"upload_id": upload_id},
            candidate_path=staged_path,
            superseded_path=None,
        )

    expected_locked_upload = locked_upload
    service.ensure_can_receive_part = lock_upload  # type: ignore[method-assign]
    service.put_part_file = prepare_part  # type: ignore[method-assign]
    monkeypatch.setattr(service_module, "new_upload_part_path", lambda *_args: destination)
    original_writer = service_module.exclusive_file_writer

    @contextmanager
    def recording_writer(path: Path, root: Path):
        assert (path, root) == (destination, upload_root)
        events.append("writer")
        with original_writer(path, root) as handle:
            yield handle

    monkeypatch.setattr(service_module, "exclusive_file_writer", recording_writer)

    assert service.put_part("upl_test", 1, b"data") == {"upload_id": "upl_test"}
    assert destination.read_bytes() == b"data"
    assert events == ["lock", "writer", "metadata"]
