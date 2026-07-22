from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

EXPECTED_MAINTENANCE_BATCH_SIZE = 100


class _OrderedDirectoryScan:
    def __init__(self, names: list[str]) -> None:
        self._entries = iter(SimpleNamespace(name=name) for name in names)
        self.next_calls = 0
        self.closed = False

    def __iter__(self) -> _OrderedDirectoryScan:
        return self

    def __next__(self) -> SimpleNamespace:
        self.next_calls += 1
        return next(self._entries)

    def close(self) -> None:
        self.closed = True


def _use_ordered_directory_scan(
    monkeypatch: pytest.MonkeyPatch,
    maintenance: Any,
    names: list[str],
) -> _OrderedDirectoryScan:
    scan = _OrderedDirectoryScan(names)
    monkeypatch.setattr(maintenance, "_open_directory_scan", lambda _fd: scan)
    return scan


def test_maintenance_cleanup_batch_limits_are_fixed_at_100() -> None:
    from backend.app.maintenance_cleanup import (
        DIRECTORY_SCAN_BATCH_SIZE,
        PACKAGE_CLEANUP_BATCH_SIZE,
        UPLOAD_CLEANUP_BATCH_SIZE,
    )

    assert DIRECTORY_SCAN_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    assert PACKAGE_CLEANUP_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    assert UPLOAD_CLEANUP_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE


def test_cleanup_loop_retries_without_logging_exception_details(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from backend.app.maintenance import run_cleanup_loop

    cleanup_calls = 0
    sleep_calls: list[float] = []
    sensitive_detail = "/private/runtime/do-not-log.mp4"

    def cleanup() -> dict[str, int]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise OSError(f"cannot unlink {sensitive_detail}")
        return {
            "removed_upload_parts": 0,
            "removed_packages": 0,
            "removed_pending_deletes": 1,
            "failed_pending_deletes": 0,
        }

    def stop_after_retry(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            raise RuntimeError("stop test loop")

    with pytest.raises(RuntimeError, match="stop test loop"):
        run_cleanup_loop(7, heartbeat_path=tmp_path / "heartbeat", cleanup=cleanup, sleep=stop_after_retry)

    output = capsys.readouterr().out
    assert cleanup_calls == 2
    assert sleep_calls == [7, 7]
    assert '"error_type": "OSError"' in output
    assert '"status": "ok"' in output
    assert sensitive_detail not in output


def test_cleanup_loop_bounds_error_retry_then_restores_normal_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance as maintenance

    cleanup_calls = 0
    sleep_intervals: list[int] = []

    def cleanup() -> dict[str, int]:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("transient startup race")
        return {}

    def record_sleep_interval(
        interval_seconds: int,
        _heartbeat_path: Path,
        _sleep: Any,
    ) -> None:
        sleep_intervals.append(interval_seconds)
        if len(sleep_intervals) == 2:
            raise RuntimeError("stop test loop")

    monkeypatch.setattr(maintenance, "_sleep_with_heartbeat", record_sleep_interval)

    with pytest.raises(RuntimeError, match="stop test loop"):
        maintenance.run_cleanup_loop(
            300,
            heartbeat_path=tmp_path / "heartbeat",
            cleanup=cleanup,
        )

    assert cleanup_calls == 2
    assert sleep_intervals == [maintenance.MAX_ERROR_RETRY_SECONDS, 300]
    assert sleep_intervals[0] <= 10


def test_cleanup_sleep_refreshes_heartbeat_during_long_intervals(tmp_path: Path) -> None:
    from backend.app.maintenance import HEARTBEAT_REFRESH_SECONDS, _sleep_with_heartbeat

    heartbeat = tmp_path / "heartbeat"
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    _sleep_with_heartbeat(HEARTBEAT_REFRESH_SECONDS * 2 + 5, heartbeat, fake_sleep)

    assert sleep_calls == [HEARTBEAT_REFRESH_SECONDS, HEARTBEAT_REFRESH_SECONDS, 5]
    assert heartbeat.exists()


def test_cleanup_loop_publishes_heartbeat_before_first_cycle(tmp_path: Path) -> None:
    from backend.app.maintenance import run_cleanup_loop

    heartbeat = tmp_path / "heartbeat"
    heartbeat_seen: list[bool] = []

    def cleanup() -> dict[str, int]:
        heartbeat_seen.append(heartbeat.exists())
        return {}

    def stop_after_first_cycle(_seconds: float) -> None:
        raise RuntimeError("stop test loop")

    with pytest.raises(RuntimeError, match="stop test loop"):
        run_cleanup_loop(1, heartbeat_path=heartbeat, cleanup=cleanup, sleep=stop_after_first_cycle)

    assert heartbeat_seen == [True]


def test_cleanup_result_reports_retryable_file_failures_as_degraded() -> None:
    from backend.app.maintenance import _cleanup_result_payload

    result = {
        "removed_upload_parts": 0,
        "failed_upload_parts": 1,
        "removed_packages": 0,
        "failed_packages": 0,
        "removed_pending_deletes": 0,
        "failed_pending_deletes": 0,
    }

    assert _cleanup_result_payload(result)["status"] == "degraded"


def test_cleanup_loop_publishes_degraded_health_status(tmp_path: Path) -> None:
    from backend.app.maintenance import run_cleanup_loop

    status = tmp_path / "status"

    def cleanup() -> dict[str, int]:
        return {"failed_pending_deletes": 1}

    def stop_after_first_cycle(_seconds: float) -> None:
        raise RuntimeError("stop test loop")

    with pytest.raises(RuntimeError, match="stop test loop"):
        run_cleanup_loop(
            1,
            heartbeat_path=tmp_path / "heartbeat",
            status_path=status,
            cleanup=cleanup,
            sleep=stop_after_first_cycle,
        )

    assert status.read_text(encoding="ascii") == "degraded"


def test_cleanup_loop_bounds_interval_and_exits_after_repeated_timeouts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from backend.app.maintenance import MAX_MAINTENANCE_SECONDS, run_cleanup_loop

    with pytest.raises(ValueError, match="supported range"):
        run_cleanup_loop(MAX_MAINTENANCE_SECONDS + 1)

    def slow_cleanup() -> dict[str, int]:
        time.sleep(2)
        return {}

    with pytest.raises(RuntimeError, match="failed repeatedly"):
        run_cleanup_loop(
            1,
            cycle_timeout_seconds=1,
            max_consecutive_errors=1,
            heartbeat_path=tmp_path / "timeout-heartbeat",
            cleanup=slow_cleanup,
        )

    output = capsys.readouterr().out
    assert '"error_type": "TimeoutError"' in output
    assert '"consecutive_errors": 1' in output
    assert (tmp_path / "timeout-heartbeat").exists()


def test_maintenance_cli_converts_repeated_failure_to_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.app.maintenance as maintenance

    def fail_loop(*_args: object, **_kwargs: object) -> None:
        raise maintenance.RepeatedMaintenanceError("maintenance cleanup failed repeatedly")

    monkeypatch.setattr(maintenance, "run_cleanup_loop", fail_loop)
    monkeypatch.setattr(sys, "argv", ["maintenance", "run"])

    assert maintenance.main() == 1


def test_maintenance_cli_cleanup_sanitizes_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import backend.app.maintenance as maintenance

    sensitive_detail = "/synthetic/private/path/do-not-log"

    def fail_cleanup() -> dict[str, int]:
        raise OSError(sensitive_detail)

    monkeypatch.setattr(maintenance, "cleanup_temporary_files", fail_cleanup)
    monkeypatch.setattr(sys, "argv", ["maintenance", "cleanup"])

    assert maintenance.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error_type": "OSError", "status": "error"}
    assert sensitive_detail not in captured.out
    assert sensitive_detail not in captured.err


def test_cleanup_removes_stale_empty_aborted_upload_session(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    original_updated_at = utcnow() - timedelta(days=2)
    with SessionLocal() as session:
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="aborted.mp4",
                mime_type="video/mp4",
                declared_size=1,
                expected_sha256="a" * 64,
                status="aborted",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={},
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=original_updated_at,
                updated_at=original_updated_at,
            )
        )
        session.commit()

    cleanup_temporary_files()
    cleanup_temporary_files()

    with SessionLocal() as session:
        assert session.get(UploadSessionModel, upload_id) is None


def test_cleanup_batches_make_progress_across_stale_aborted_uploads(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.maintenance_cleanup import UPLOAD_CLEANUP_BATCH_SIZE
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    stale = utcnow() - timedelta(days=2)
    upload_ids = [f"upl_{uuid.uuid4().hex}" for _ in range(UPLOAD_CLEANUP_BATCH_SIZE + 1)]
    with SessionLocal() as session:
        session.add_all(
            [
                UploadSessionModel(
                    id=upload_id,
                    original_filename="aborted.mp4",
                    mime_type="video/mp4",
                    declared_size=1,
                    expected_sha256="a" * 64,
                    status="aborted",
                    temp_path=str(settings.storage_root / "uploads" / upload_id),
                    owner_principal_id="maintenance-test",
                    owner_principal_kind="system",
                    received_parts={},
                    duration_ms=1,
                    width=1,
                    height=1,
                    fps_num=1,
                    fps_den=1,
                    created_at=stale,
                    updated_at=stale,
                )
                for upload_id in upload_ids
            ]
        )
        session.commit()

    cleanup_temporary_files()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(UploadSessionModel).where(UploadSessionModel.id.in_(upload_ids))) == 1
    cleanup_temporary_files()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(UploadSessionModel).where(UploadSessionModel.id.in_(upload_ids))) == 0


def test_upload_cleanup_defers_100_failed_claims_before_reaching_101st_session(client: TestClient) -> None:
    from backend.app.maintenance_cleanup import (
        UPLOAD_CLEANUP_BATCH_SIZE,
        _claim_terminal_or_stale_uploads,
        _cleanup_claimed_upload_parts,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    stale = utcnow() - timedelta(days=2)
    assert UPLOAD_CLEANUP_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    blocker_ids = [f"upl_{index:032x}" for index in range(EXPECTED_MAINTENANCE_BATCH_SIZE)]
    cleanable_id = f"upl_{EXPECTED_MAINTENANCE_BATCH_SIZE:032x}"
    with SessionLocal() as session:
        session.add_all(
            [
                UploadSessionModel(
                    id=upload_id,
                    original_filename="blocked.mp4",
                    mime_type="video/mp4",
                    declared_size=1,
                    expected_sha256="a" * 64,
                    status="aborted",
                    temp_path=str(settings.storage_root / "uploads" / upload_id),
                    owner_principal_id="maintenance-test",
                    owner_principal_kind="system",
                    received_parts={"invalid": {"path": "invalid", "size": 1}},
                    duration_ms=1,
                    width=1,
                    height=1,
                    fps_num=1,
                    fps_den=1,
                    created_at=stale,
                    updated_at=stale,
                )
                for upload_id in blocker_ids
            ]
        )
        session.add(
            UploadSessionModel(
                id=cleanable_id,
                original_filename="cleanable.mp4",
                mime_type="video/mp4",
                declared_size=1,
                expected_sha256="b" * 64,
                status="aborted",
                temp_path=str(settings.storage_root / "uploads" / cleanable_id),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={},
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=stale,
                updated_at=stale,
            )
        )
        session.commit()

        first_claims = _claim_terminal_or_stale_uploads(session, utcnow() - timedelta(days=1))
        assert first_claims == blocker_ids
        assert _cleanup_claimed_upload_parts(session, first_claims, settings.storage_root / "uploads") == (
            0,
            UPLOAD_CLEANUP_BATCH_SIZE,
        )

    with SessionLocal() as session:
        second_claims = _claim_terminal_or_stale_uploads(session, utcnow() - timedelta(days=1))
        assert second_claims == [cleanable_id]
        assert _cleanup_claimed_upload_parts(session, second_claims, settings.storage_root / "uploads") == (0, 0)
        assert session.get(UploadSessionModel, cleanable_id) is None
        assert all(session.get(UploadSessionModel, upload_id) is not None for upload_id in blocker_ids)


def test_sqlite_completed_detached_upload_cleanup_deletes_session_and_releases_reservation(client: TestClient) -> None:
    from backend.app.maintenance_cleanup import (
        _claim_terminal_or_stale_uploads,
        _cleanup_claimed_upload_parts,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings
    from backend.app.upload_parts import new_upload_part_path

    settings = get_database_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    part_path = new_upload_part_path(settings.storage_root / "uploads", upload_id, 1)
    part_path.write_bytes(b"retry-cleanup")
    now = utcnow() - timedelta(minutes=10)
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "sqlite"
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="completed.mp4",
                mime_type="video/mp4",
                declared_size=len(b"retry-cleanup"),
                expected_sha256="a" * 64,
                status="completed",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={"1": {"path": str(part_path), "size": len(b"retry-cleanup")}},
                reserved_bytes=len(b"retry-cleanup") * 2,
                parts_cleanup_confirmed_at=None,
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

        claims = _claim_terminal_or_stale_uploads(session, now - timedelta(days=1))
        assert claims == [upload_id]
        assert _cleanup_claimed_upload_parts(
            session,
            claims,
            settings.storage_root / "uploads",
        ) == (1, 0)
        assert session.get(UploadSessionModel, upload_id) is None
        active_count, active_bytes = session.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(UploadSessionModel.parts_cleanup_confirmed_at.is_(None))
        ).one()
        assert (active_count, active_bytes) == (0, 0)
        assert not part_path.exists()


def test_sqlite_completed_file_linked_upload_is_retained_after_part_cleanup(client: TestClient) -> None:
    from backend.app.maintenance_cleanup import (
        _claim_terminal_or_stale_uploads,
        _cleanup_claimed_upload_parts,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel, UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings
    from backend.app.upload_parts import new_upload_part_path

    settings = get_database_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    file_id = f"file_{uuid.uuid4().hex}"
    payload = b"linked-cleanup"
    part_path = new_upload_part_path(settings.storage_root / "uploads", upload_id, 1)
    part_path.write_bytes(payload)
    file_path = settings.storage_root / "files" / file_id
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(payload)
    now = utcnow() - timedelta(minutes=10)
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "sqlite"
        session.add(
            FileObjectModel(
                id=file_id,
                original_filename="linked.mp4",
                mime_type="video/mp4",
                file_size=len(payload),
                sha256="b" * 64,
                storage_path=str(file_path),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
            )
        )
        session.flush()
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="linked.mp4",
                mime_type="video/mp4",
                declared_size=len(payload),
                expected_sha256="b" * 64,
                status="completed",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={"1": {"path": str(part_path), "size": len(payload)}},
                reserved_bytes=len(payload) * 2,
                parts_cleanup_confirmed_at=None,
                file_id=file_id,
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

        claims = _claim_terminal_or_stale_uploads(session, now - timedelta(days=1))
        assert claims == [upload_id]
        assert _cleanup_claimed_upload_parts(session, claims, settings.storage_root / "uploads") == (1, 0)
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.file_id == file_id
        assert upload.received_parts == {}
        assert upload.parts_cleanup_confirmed_at is not None
        assert session.get(FileObjectModel, file_id) is not None
        assert not part_path.exists()


def test_sqlite_completed_detached_upload_cleanup_failure_keeps_reservation_and_retries(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings
    from backend.app.upload_parts import new_upload_part_path

    settings = get_database_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    payload = b"retry-detached-cleanup"
    reservation = len(payload) * 2
    part_path = new_upload_part_path(settings.storage_root / "uploads", upload_id, 1)
    part_path.write_bytes(payload)
    stale = utcnow() - timedelta(minutes=10)
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "sqlite"
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="detached.mp4",
                mime_type="video/mp4",
                declared_size=len(payload),
                expected_sha256="c" * 64,
                status="completed",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={"1": {"path": str(part_path), "size": len(payload)}},
                reserved_bytes=reservation,
                parts_cleanup_confirmed_at=None,
                file_id=None,
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=stale,
                updated_at=stale,
            )
        )
        session.commit()

        original_unlink = maintenance.unlink_regular_file
        attempts = 0

        def fail_first_cleanup(path: Path, root: Path) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("forced detached cleanup failure")
            return original_unlink(path, root)

        monkeypatch.setattr(maintenance, "unlink_regular_file", fail_first_cleanup)
        claims = maintenance._claim_terminal_or_stale_uploads(session, stale - timedelta(days=1))
        assert claims == [upload_id]
        assert maintenance._cleanup_claimed_upload_parts(session, claims, settings.storage_root / "uploads") == (0, 1)

        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.file_id is None
        assert upload.received_parts == {"1": {"path": str(part_path), "size": len(payload)}}
        assert upload.parts_cleanup_confirmed_at is None
        assert upload.reserved_bytes == reservation
        active_count, active_bytes = session.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(
                UploadSessionModel.id == upload_id,
                UploadSessionModel.parts_cleanup_confirmed_at.is_(None),
            )
        ).one()
        assert (active_count, active_bytes) == (1, reservation)
        assert part_path.exists()

        upload.updated_at = utcnow() - timedelta(seconds=maintenance.UPLOAD_CLEANUP_RETRY_SECONDS + 1)
        session.commit()
        retry_claims = maintenance._claim_terminal_or_stale_uploads(session, stale - timedelta(days=1))
        assert retry_claims == [upload_id]
        assert maintenance._cleanup_claimed_upload_parts(session, retry_claims, settings.storage_root / "uploads") == (1, 0)
        assert session.get(UploadSessionModel, upload_id) is None
        active_count, active_bytes = session.execute(
            select(
                func.count(UploadSessionModel.id),
                func.coalesce(func.sum(UploadSessionModel.reserved_bytes), 0),
            ).where(UploadSessionModel.parts_cleanup_confirmed_at.is_(None))
        ).one()
        assert (active_count, active_bytes) == (0, 0)
        assert not part_path.exists()


def test_expired_finalizing_upload_enters_ttl_cleanup(client: TestClient) -> None:
    from backend.app.maintenance_cleanup import _claim_terminal_or_stale_uploads
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    stale = utcnow() - timedelta(days=2)
    with SessionLocal() as session:
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="expired-finalizing.mp4",
                mime_type="video/mp4",
                declared_size=1,
                expected_sha256="b" * 64,
                status="finalizing",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id="maintenance-test",
                owner_principal_kind="system",
                received_parts={},
                reserved_bytes=2,
                parts_cleanup_confirmed_at=None,
                finalization_lease_id="lease_expired",
                finalization_lease_expires_at=stale + timedelta(hours=1),
                finalization_file_id=f"file_{uuid.uuid4().hex}",
                finalization_idempotency_key_hash="a" * 64,
                finalization_request_hash="b" * 64,
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=stale,
                updated_at=stale,
            )
        )
        session.commit()

        assert _claim_terminal_or_stale_uploads(session, utcnow() - timedelta(days=1)) == [upload_id]
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.status == "aborted"
        assert upload.finalization_lease_id is None
        assert upload.finalization_lease_expires_at is None
        assert upload.finalization_file_id is None


def test_put_preflight_activity_renewal_blocks_maintenance_during_body_window(
    client: TestClient,
) -> None:
    from backend.app.maintenance_cleanup import _claim_terminal_or_stale_uploads
    from backend.app.modules.final_cut_review.application.context import (
        ExecutionContext,
        PrincipalRef,
        WriteGuardState,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import UploadSessionModel, utcnow
    from backend.app.modules.review_media.service import LocalMediaService
    from backend.app.settings import get_settings

    settings = get_settings()
    upload_id = f"upl_{uuid.uuid4().hex}"
    owner_id = f"principal_{uuid.uuid4().hex}"
    stale = utcnow() - timedelta(seconds=settings.upload_session_ttl_seconds - 1)
    with SessionLocal() as session:
        session.add(
            UploadSessionModel(
                id=upload_id,
                original_filename="active-put.mp4",
                mime_type="video/mp4",
                declared_size=10,
                expected_sha256="c" * 64,
                status="receiving",
                temp_path=str(settings.storage_root / "uploads" / f"{upload_id}.parts"),
                owner_principal_id=owner_id,
                owner_principal_kind="system",
                received_parts={},
                reserved_bytes=20,
                parts_cleanup_confirmed_at=None,
                duration_ms=1,
                width=1,
                height=1,
                fps_num=1,
                fps_den=1,
                created_at=stale,
                updated_at=stale,
            )
        )
        session.commit()

    context = ExecutionContext(
        entry_source="edit",
        request_id=f"req_{uuid.uuid4().hex}",
        principal=PrincipalRef(kind="system", id=owner_id, project_ref_ids=("*",)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )
    with SessionLocal() as session:
        assert LocalMediaService(session, settings, context).claim_part_stream(upload_id, 1) == 10
        session.commit()
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        renewed_at = upload.updated_at

    body_completion_time = renewed_at + timedelta(seconds=settings.upload_part_read_timeout_seconds)
    stale_before_during_body = body_completion_time - timedelta(seconds=settings.upload_session_ttl_seconds)
    with SessionLocal() as session:
        assert _claim_terminal_or_stale_uploads(session, stale_before_during_body) == []

    with SessionLocal() as session:
        assert _claim_terminal_or_stale_uploads(
            session,
            renewed_at + timedelta(seconds=1),
        ) == [upload_id]


def test_orphan_directory_scan_is_bounded_and_advances_next_cycle(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.maintenance_cleanup import DIRECTORY_SCAN_BATCH_SIZE
    from backend.app.settings import get_settings
    from backend.app.upload_parts import new_upload_part_path

    settings = get_settings()
    upload_root = settings.storage_root / "uploads"
    paths = [new_upload_part_path(upload_root, f"upl_{uuid.uuid4().hex}", 1) for _ in range(DIRECTORY_SCAN_BATCH_SIZE + 1)]
    stale_time = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    for path in paths:
        path.write_bytes(b"orphan")
        os.utime(path, (stale_time, stale_time))

    first = cleanup_temporary_files()
    assert first["removed_orphan_upload_parts"] == DIRECTORY_SCAN_BATCH_SIZE
    assert sum(path.exists() for path in paths) == 1
    second = cleanup_temporary_files()
    assert second["removed_orphan_upload_parts"] == 1
    assert not any(path.exists() for path in paths)


def test_cleanup_recovers_storage_root_delete_quarantine(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.settings import get_settings

    settings = get_settings()
    target = settings.storage_root / "root-delete-recovery.bin"
    target.write_bytes(b"recover-root-quarantine")
    repository_root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path

from backend.app import safe_files

root = Path(os.environ["FCR_TEST_DELETE_ROOT"])
target = root / "root-delete-recovery.bin"
original_rename = safe_files.os.rename

def crash_after_rename(source, destination, **kwargs):
    original_rename(source, destination, **kwargs)
    os._exit(73)

safe_files.os.rename = crash_after_rename
with safe_files.pin_regular_file(target, root) as pinned:
    assert pinned is not None
    pinned.unlink()
""",
        ],
        cwd=repository_root,
        env={**os.environ, "FCR_TEST_DELETE_ROOT": str(settings.storage_root), "PYTHONPATH": "."},
        check=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 73

    result = cleanup_temporary_files()

    assert result["resolved_delete_quarantines"] == 1
    assert result["failed_delete_quarantines"] == 0
    assert not target.exists()
    assert not list((settings.storage_root / ".fcr-delete-quarantine").iterdir())


def test_orphan_upload_scan_rotates_past_100_malformed_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.upload_parts import new_upload_part_path

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    assert maintenance.DIRECTORY_SCAN_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    blockers = [f"malformed-{index:03d}" for index in range(EXPECTED_MAINTENANCE_BATCH_SIZE)]
    cleanable = new_upload_part_path(upload_root, f"upl_{uuid.uuid4().hex}", 1)
    cleanable.write_bytes(b"orphan")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(cleanable, (stale.timestamp(), stale.timestamp()))
    scan = _use_ordered_directory_scan(monkeypatch, maintenance, [*blockers, cleanable.name])

    class EmptySession:
        def rollback(self) -> None:
            pass

        def scalar(self, _statement: object) -> None:
            return None

        def commit(self) -> None:
            pass

    session = cast(Session, EmptySession())
    assert maintenance._cleanup_orphan_upload_parts(session, upload_root, datetime.now(timezone.utc)) == (0, 0)
    assert scan.next_calls == EXPECTED_MAINTENANCE_BATCH_SIZE
    assert cleanable.exists()
    assert maintenance._cleanup_orphan_upload_parts(session, upload_root, datetime.now(timezone.utc)) == (1, 0)
    assert not cleanable.exists()
    assert scan.closed is True


@pytest.mark.parametrize(
    "received_parts",
    [
        [],
        {"invalid": {"path": "invalid", "size": 1}},
    ],
)
def test_orphan_upload_cleanup_preserves_candidates_when_reference_metadata_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    received_parts: object,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.upload_parts import new_upload_part_path

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    upload_id = f"upl_{uuid.uuid4().hex}"
    candidate = new_upload_part_path(upload_root, upload_id, 1)
    candidate.write_bytes(b"must-remain")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(candidate, (stale.timestamp(), stale.timestamp()))
    _use_ordered_directory_scan(monkeypatch, maintenance, [candidate.name])
    upload = SimpleNamespace(id=upload_id, temp_path=str(upload_root / f"{upload_id}.parts"), received_parts=received_parts)

    class InvalidReferenceSession:
        def rollback(self) -> None:
            pass

        def scalar(self, _statement: object) -> object:
            return upload

        def commit(self) -> None:
            raise AssertionError("invalid reference metadata must not reach deletion commit")

    session = cast(Session, InvalidReferenceSession())
    assert maintenance._cleanup_orphan_upload_parts(session, upload_root, datetime.now(timezone.utc)) == (0, 0)
    assert candidate.read_bytes() == b"must-remain"


@pytest.mark.parametrize(
    ("kind", "name_template"),
    [
        ("file", "file_{value}"),
        ("package", "pkg_{value}.zip"),
    ],
)
def test_managed_directory_scans_rotate_past_100_referenced_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    name_template: str,
) -> None:
    import backend.app.maintenance_cleanup as maintenance

    root = tmp_path / kind
    root.mkdir()
    assert maintenance.DIRECTORY_SCAN_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    blocker_names = [name_template.format(value=f"{index:032x}") for index in range(EXPECTED_MAINTENANCE_BATCH_SIZE)]
    cleanable_name = name_template.format(value=f"{EXPECTED_MAINTENANCE_BATCH_SIZE:032x}")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    for name in [*blocker_names, cleanable_name]:
        path = root / name
        path.write_bytes(b"managed")
        os.utime(path, (stale.timestamp(), stale.timestamp()))
    blocker_ids = {name if kind == "file" else name.removesuffix(".zip") for name in blocker_names}
    scan = _use_ordered_directory_scan(monkeypatch, maintenance, [*blocker_names, cleanable_name])

    class ReferencedSession:
        def get(self, _model: object, record_id: str) -> object | None:
            return object() if record_id in blocker_ids else None

        def scalar(self, _statement: object) -> None:
            return None

    session = cast(Session, ReferencedSession())
    assert maintenance._cleanup_orphan_managed_files(
        session,
        root,
        datetime.now(timezone.utc),
        kind=kind,
    ) == (0, 0)
    assert scan.next_calls == EXPECTED_MAINTENANCE_BATCH_SIZE
    assert (root / cleanable_name).exists()
    assert maintenance._cleanup_orphan_managed_files(
        session,
        root,
        datetime.now(timezone.utc),
        kind=kind,
    ) == (1, 0)
    assert not (root / cleanable_name).exists()
    assert scan.closed is True


def test_managed_directory_cleanup_uses_scanned_root_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance

    root = tmp_path / "files"
    root.mkdir()
    file_id = f"file_{uuid.uuid4().hex}"
    original = root / file_id
    original.write_bytes(b"original-root")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(original, (stale.timestamp(), stale.timestamp()))
    _use_ordered_directory_scan(monkeypatch, maintenance, [file_id])
    moved_root = tmp_path / "files-original"
    replacement_content = b"replacement-root"
    original_pin = maintenance._pin_regular_file_beneath
    swapped = False

    @contextmanager
    def swap_before_pin(root_fd: int, pinned_root: Path, relative_path: Path) -> Iterator[Any]:
        nonlocal swapped
        if not swapped:
            root.rename(moved_root)
            root.mkdir()
            (root / file_id).write_bytes(replacement_content)
            swapped = True
        with original_pin(root_fd, pinned_root, relative_path) as pinned:
            yield pinned

    class EmptySession:
        def get(self, _model: object, _record_id: str) -> None:
            return None

        def scalar(self, _statement: object) -> None:
            return None

    monkeypatch.setattr(maintenance, "_pin_regular_file_beneath", swap_before_pin)
    session = cast(Session, EmptySession())
    assert maintenance._cleanup_orphan_managed_files(
        session,
        root,
        datetime.now(timezone.utc),
        kind="file",
    ) == (1, 0)
    assert swapped is True
    assert not (moved_root / file_id).exists()
    assert (root / file_id).read_bytes() == replacement_content


def test_orphan_file_cleanup_preserves_upload_session_file_references(tmp_path: Path) -> None:
    import backend.app.maintenance_cleanup as maintenance

    root = tmp_path / "files"
    root.mkdir()
    file_id = f"file_{uuid.uuid4().hex}"
    target = root / file_id
    target.write_bytes(b"active-finalization")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(target, (stale.timestamp(), stale.timestamp()))

    class UploadReferencedSession:
        def get(self, _model: object, _record_id: str) -> None:
            return None

        def scalar(self, statement: object) -> str:
            sql = str(statement)
            assert "upload_sessions.finalization_file_id" in sql
            assert "upload_sessions.file_id" in sql
            return "upl_active"

    session = cast(Session, UploadReferencedSession())
    assert maintenance._cleanup_orphan_managed_files(
        session,
        root,
        datetime.now(timezone.utc),
        kind="file",
    ) == (0, 0)
    assert target.read_bytes() == b"active-finalization"


def test_package_unlink_failure_does_not_starve_pending_deletes(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ProjectRefModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    project_id = f"prj_{uuid.uuid4().hex}"
    failed_package_id = f"pkg_{uuid.uuid4().hex}"
    removed_package_id = f"pkg_{uuid.uuid4().hex}"
    package_path = settings.package_root / f"{failed_package_id}.zip"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_bytes(b"package")
    removable_package_path = settings.package_root / f"{removed_package_id}.zip"
    removable_package_path.write_bytes(b"removable-package")
    with SessionLocal() as session:
        session.add(
            ProjectRefModel(
                id=project_id,
                project_code=f"MAINT-{uuid.uuid4().hex[:12]}",
                project_name="Maintenance package cleanup test",
            )
        )
        session.flush()
        session.add(
            FinalCutPackageSnapshotModel(
                id=failed_package_id,
                project_ref_id=project_id,
                status="ready",
                package_filename=package_path.name,
                storage_path=str(package_path),
                expires_at=utcnow() - timedelta(seconds=1),
                total_bytes=package_path.stat().st_size,
                sha256="a" * 64,
                items=[],
            )
        )
        session.add(
            FinalCutPackageSnapshotModel(
                id=removed_package_id,
                project_ref_id=project_id,
                status="ready",
                package_filename=removable_package_path.name,
                storage_path=str(removable_package_path),
                expires_at=utcnow() - timedelta(seconds=1),
                total_bytes=removable_package_path.stat().st_size,
                sha256="b" * 64,
                items=[],
            )
        )
        session.commit()

    pending_root = settings.storage_root / "pending-deletes"
    pending_root.mkdir(parents=True, exist_ok=True)
    pending_file_id = f"file_{uuid.uuid4().hex}"
    pending_target = settings.storage_root / "files" / pending_file_id
    pending_target.parent.mkdir(parents=True, exist_ok=True)
    pending_target.write_bytes(b"pending")
    pending_identity = pending_target.stat()
    pending_tombstone = pending_root / "independent-pending-delete.json"
    pending_tombstone.write_text(
        json.dumps(
            {
                "file_id": pending_file_id,
                "storage_path": str(pending_target),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "device": pending_identity.st_dev,
                "inode": pending_identity.st_ino,
                "ctime_ns": pending_identity.st_ctime_ns,
                "size": pending_identity.st_size,
            }
        ),
        encoding="utf-8",
    )

    original_rename = os.rename

    def fail_package_unlink(source: str | Path, target: str | Path, **kwargs: Any) -> None:
        if Path(source).name == package_path.name and kwargs.get("src_dir_fd") is not None:
            raise OSError("forced package unlink failure")
        return original_rename(source, target, **kwargs)

    monkeypatch.setattr(os, "rename", fail_package_unlink)
    result = cleanup_temporary_files()

    assert result["failed_packages"] == 1
    assert result["removed_packages"] == 1
    assert result["removed_pending_deletes"] == 1
    assert package_path.exists()
    assert not removable_package_path.exists()
    assert not pending_target.exists()
    assert not pending_tombstone.exists()
    with SessionLocal() as session:
        failed_package = session.get(FinalCutPackageSnapshotModel, failed_package_id)
        removed_package = session.get(FinalCutPackageSnapshotModel, removed_package_id)
        assert failed_package is not None and failed_package.status == "ready"
        assert removed_package is not None and removed_package.status == "expired"


def test_expired_package_cleanup_defers_100_failures_before_reaching_101st_package(client: TestClient) -> None:
    from backend.app.maintenance_cleanup import (
        PACKAGE_CLEANUP_BATCH_SIZE,
        PACKAGE_CLEANUP_RETRY_SECONDS,
        _claim_expired_packages,
        _cleanup_claimed_expired_packages,
    )
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ProjectRefModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    project_id = f"prj_{uuid.uuid4().hex}"
    assert PACKAGE_CLEANUP_BATCH_SIZE == EXPECTED_MAINTENANCE_BATCH_SIZE
    blocker_ids = [f"pkg_{index:032x}" for index in range(EXPECTED_MAINTENANCE_BATCH_SIZE)]
    cleanable_id = f"pkg_{EXPECTED_MAINTENANCE_BATCH_SIZE:032x}"
    cleanable_path = settings.package_root / f"{cleanable_id}.zip"
    cleanable_path.parent.mkdir(parents=True, exist_ok=True)
    cleanable_path.write_bytes(b"cleanable-package")
    expired_at = utcnow() - timedelta(days=2)
    with SessionLocal() as session:
        session.add(
            ProjectRefModel(
                id=project_id,
                project_code=f"MAINT-{uuid.uuid4().hex[:12]}",
                project_name="Expired package starvation test",
            )
        )
        session.flush()
        session.add_all(
            [
                FinalCutPackageSnapshotModel(
                    id=package_id,
                    project_ref_id=project_id,
                    status="expired",
                    package_filename=f"{package_id}.zip",
                    storage_path=str(settings.package_root / f"wrong-{package_id}.zip"),
                    expires_at=expired_at,
                    total_bytes=1,
                    sha256=None,
                    items=[],
                )
                for package_id in blocker_ids
            ]
        )
        session.add(
            FinalCutPackageSnapshotModel(
                id=cleanable_id,
                project_ref_id=project_id,
                status="expired",
                package_filename=cleanable_path.name,
                storage_path=str(cleanable_path),
                expires_at=expired_at,
                total_bytes=cleanable_path.stat().st_size,
                storage_bytes=cleanable_path.stat().st_size,
                sha256=None,
                items=[],
            )
        )
        session.commit()

        first_claimed_at = utcnow()
        first_claims = _claim_expired_packages(session, first_claimed_at)
        assert [package_id for package_id, _claim_id in first_claims] == blocker_ids
        assert _cleanup_claimed_expired_packages(session, first_claims, settings.package_root) == (
            0,
            PACKAGE_CLEANUP_BATCH_SIZE,
        )
        session.expire_all()
        for package_id in blocker_ids:
            blocker = session.get(FinalCutPackageSnapshotModel, package_id)
            assert blocker is not None and blocker.download_lease_id is not None

    with SessionLocal() as session:
        second_claims = _claim_expired_packages(
            session,
            first_claimed_at + timedelta(seconds=PACKAGE_CLEANUP_RETRY_SECONDS + 1),
        )
        assert len(second_claims) == EXPECTED_MAINTENANCE_BATCH_SIZE
        assert second_claims[0][0] == cleanable_id
        assert _cleanup_claimed_expired_packages(session, second_claims, settings.package_root) == (
            1,
            EXPECTED_MAINTENANCE_BATCH_SIZE - 1,
        )
        assert not cleanable_path.exists()
        cleanable = session.get(FinalCutPackageSnapshotModel, cleanable_id)
        assert cleanable is not None and cleanable.storage_reclaimed_at is not None
        assert cleanable.storage_bytes == 0


def test_cleanup_retries_expired_package_file_removal(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ProjectRefModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    package_id = f"pkg_{uuid.uuid4().hex}"
    package_path = settings.package_root / f"{package_id}.zip"
    package_path.write_bytes(b"legacy-expired-package")
    project_id = f"prj_{uuid.uuid4().hex}"
    with SessionLocal() as session:
        session.add(
            ProjectRefModel(
                id=project_id,
                project_code=f"MAINT-{uuid.uuid4().hex[:12]}",
                project_name="Expired package retry test",
            )
        )
        session.flush()
        session.add(
            FinalCutPackageSnapshotModel(
                id=package_id,
                project_ref_id=project_id,
                status="expired",
                package_filename=package_path.name,
                storage_path=str(package_path),
                expires_at=utcnow() - timedelta(seconds=1),
                total_bytes=package_path.stat().st_size,
                sha256=None,
                items=[],
            )
        )
        session.commit()

    result = cleanup_temporary_files()

    assert result["removed_packages"] == 1
    assert result["failed_packages"] == 0
    assert not package_path.exists()


def test_cleanup_rejects_package_path_bound_to_another_package(client: TestClient) -> None:
    from backend.app.maintenance import cleanup_temporary_files
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ProjectRefModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    settings = get_database_settings()
    expired_id = f"pkg_{uuid.uuid4().hex}"
    live_id = f"pkg_{uuid.uuid4().hex}"
    live_path = settings.package_root / f"{live_id}.zip"
    live_path.write_bytes(b"live-package")
    project_id = f"prj_{uuid.uuid4().hex}"
    with SessionLocal() as session:
        session.add(
            ProjectRefModel(
                id=project_id,
                project_code=f"MAINT-{uuid.uuid4().hex[:12]}",
                project_name="Package binding cleanup test",
            )
        )
        session.flush()
        session.add_all(
            [
                FinalCutPackageSnapshotModel(
                    id=expired_id,
                    project_ref_id=project_id,
                    status="expired",
                    package_filename=f"{expired_id}.zip",
                    storage_path=str(live_path),
                    expires_at=utcnow() - timedelta(seconds=1),
                    total_bytes=live_path.stat().st_size,
                    sha256=None,
                    items=[],
                ),
                FinalCutPackageSnapshotModel(
                    id=live_id,
                    project_ref_id=project_id,
                    status="ready",
                    package_filename=f"{live_id}.zip",
                    storage_path=str(live_path),
                    expires_at=utcnow() + timedelta(hours=1),
                    total_bytes=live_path.stat().st_size,
                    sha256="a" * 64,
                    items=[],
                ),
            ]
        )
        session.commit()

    result = cleanup_temporary_files()

    assert result["failed_packages"] == 1
    assert live_path.read_bytes() == b"live-package"
