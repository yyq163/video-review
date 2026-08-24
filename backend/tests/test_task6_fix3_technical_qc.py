from __future__ import annotations

import importlib
import os
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .conftest import create_item, create_project_item, upload_video
from .test_task6_fix1_technical_qc import (
    _insert_second_ready_version_for_same_item,
    _seed_large_ready_backlog,
)
from .test_task6_fix2_technical_qc import _sandbox_exchange


ROOT = Path(__file__).resolve().parents[2]
RUNNING_ITEM_INDEX = "uq_technical_qc_tasks_running_item"
SQLITE_RUNNING_ITEM_MESSAGE = (
    "UNIQUE constraint failed: technical_qc_tasks.review_item_id"
)


def _qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def _db() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.database",
    )


def _models() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError(
        "UPDATE technical_qc_tasks SET status='running'",
        {},
        sqlite3.IntegrityError(message),
    )


def _seed_three_ready_items(client: Any, *, code: str) -> tuple[str, list[str]]:
    project, first = create_project_item(client, code=code)
    project_id = project["project_ref_id"]
    items = [first["id"]]
    for episode_no in (2, 3):
        item = create_item(
            client,
            project_id,
            upload_video(client, seed=f"{code}-{episode_no}".encode()),
            item_code=f"EP-{episode_no}",
            episode_no=episode_no,
        )
        items.append(item["id"])
    with _db().SessionLocal.begin() as session:
        assert _qc().discover_ready_technical_qc_tasks(session, batch_size=8) == 3
    return project_id, items


def test_running_item_integrity_classifier_is_exact_for_postgres_and_sqlite() -> None:
    qc = _qc()

    class Diagnostic:
        def __init__(self, constraint_name: str) -> None:
            self.constraint_name = constraint_name

    class PostgresUniqueViolation(Exception):
        def __init__(self, constraint_name: str) -> None:
            self.diag = Diagnostic(constraint_name)

    postgres_target = IntegrityError(None, None, PostgresUniqueViolation(RUNNING_ITEM_INDEX))
    postgres_other = IntegrityError(None, None, PostgresUniqueViolation("uq_unrelated"))
    sqlite_target = _integrity_error(SQLITE_RUNNING_ITEM_MESSAGE)
    sqlite_other = _integrity_error(
        "UNIQUE constraint failed: technical_qc_tasks.id",
    )

    assert qc._is_running_item_unique_conflict(postgres_target) is True
    assert qc._is_running_item_unique_conflict(sqlite_target) is True
    assert qc._is_running_item_unique_conflict(postgres_other) is False
    assert qc._is_running_item_unique_conflict(sqlite_other) is False


def test_target_running_item_competition_refills_available_slots(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    db = _db()
    _project_id, item_ids = _seed_three_ready_items(client, code="TASK6-FIX3-REFILL")
    session = db.SessionLocal()
    original_flush = session.flush
    injected = False

    def competing_flush(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        running_dirty = any(
            isinstance(value, _models().TechnicalQcTaskModel)
            and value.status == "running"
            for value in session.dirty
        )
        if running_dirty and not injected:
            injected = True
            raise _integrity_error(SQLITE_RUNNING_ITEM_MESSAGE)
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", competing_flush)
    try:
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=2,
            per_project_limit=2,
            lease_seconds=60,
            candidate_scan_limit=8,
        )
        session.commit()
    finally:
        session.close()

    assert injected is True
    assert [claim.review_item_id for claim in claims] == item_ids[1:]


def test_non_target_integrity_error_is_not_silently_treated_as_competition(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    db = _db()
    _seed_three_ready_items(client, code="TASK6-FIX3-NONTARGET")
    session = db.SessionLocal()
    original_flush = session.flush
    injected = False

    def broken_flush(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        running_dirty = any(
            isinstance(value, _models().TechnicalQcTaskModel)
            and value.status == "running"
            for value in session.dirty
        )
        if running_dirty and not injected:
            injected = True
            raise _integrity_error(
                "UNIQUE constraint failed: technical_qc_tasks.id",
            )
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", broken_flush)
    try:
        with pytest.raises(IntegrityError):
            qc.claim_technical_qc_tasks(
                session,
                limit=2,
                per_project_limit=2,
                lease_seconds=60,
                candidate_scan_limit=8,
            )
    finally:
        session.rollback()
        session.close()
    assert injected is True


def test_repeated_target_competition_stops_at_candidate_bound(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    db = _db()
    _seed_three_ready_items(client, code="TASK6-FIX3-BOUNDED")
    session = db.SessionLocal()
    original_flush = session.flush
    conflict_attempts = 0

    def always_competing_flush(*args: Any, **kwargs: Any) -> Any:
        nonlocal conflict_attempts
        running_dirty = any(
            isinstance(value, _models().TechnicalQcTaskModel)
            and value.status == "running"
            for value in session.dirty
        )
        if running_dirty:
            conflict_attempts += 1
            raise _integrity_error(SQLITE_RUNNING_ITEM_MESSAGE)
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", always_competing_flush)
    try:
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=3,
            per_project_limit=3,
            lease_seconds=60,
            candidate_scan_limit=2,
        )
    finally:
        session.rollback()
        session.close()
    assert claims == []
    assert conflict_attempts == 2


def test_postgresql_real_partial_unique_race_refills_another_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL technical QC race gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    if db.engine.dialect.name != "postgresql":
        pytest.fail("PostgreSQL technical QC race gate requires a PostgreSQL DATABASE_URL")

    qc = _qc()
    models = _models()
    with db.SessionLocal.begin() as setup:
        project_id = _seed_large_ready_backlog(setup, count=3, include_tasks=True)
        first_item_id = setup.scalar(
            select(models.ReviewItemModel.id)
            .where(models.ReviewItemModel.project_ref_id == project_id)
            .order_by(models.ReviewItemModel.episode_no),
        )
        assert first_item_id is not None
        _insert_second_ready_version_for_same_item(setup, first_item_id)
        assert qc.discover_ready_technical_qc_tasks(setup, batch_size=8) == 1

    candidates_selected = threading.Event()
    competitor_committed = threading.Event()
    original_candidates = qc._claim_candidates
    claimed: list[Any] = []
    failure: list[BaseException] = []

    def paused_candidates(*args: Any, **kwargs: Any) -> Any:
        rows = original_candidates(*args, **kwargs)
        candidates_selected.set()
        assert competitor_committed.wait(5)
        return rows

    monkeypatch.setattr(qc, "_claim_candidates", paused_candidates)

    def claimant() -> None:
        try:
            with db.SessionLocal.begin() as session:
                claimed.extend(
                    qc.claim_technical_qc_tasks(
                        session,
                        limit=2,
                        per_project_limit=2,
                        lease_seconds=60,
                        candidate_scan_limit=8,
                    ),
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    thread = threading.Thread(target=claimant, daemon=True)
    thread.start()
    try:
        assert candidates_selected.wait(5)
        with db.SessionLocal.begin() as competitor:
            first_tasks = list(
                competitor.scalars(
                    select(models.TechnicalQcTaskModel)
                    .where(models.TechnicalQcTaskModel.review_item_id == first_item_id)
                    .order_by(models.TechnicalQcTaskModel.created_at),
                ),
            )
            assert len(first_tasks) == 2
            competing_task = first_tasks[1]
            competing_task.status = "running"
            competing_task.claim_id = "fix3-pg-competitor"
            competing_task.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            competing_task.attempts = 1
        competitor_committed.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert failure == []
        assert len(claimed) == 2
        assert first_item_id not in {claim.review_item_id for claim in claimed}
    finally:
        competitor_committed.set()
        thread.join(timeout=2)
        with db.SessionLocal.begin() as cleanup:
            project = cleanup.get(models.ProjectRefModel, project_id)
            if project is not None:
                cleanup.delete(project)


def _result_messages(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "probe",
            "request_id": "$REQUEST",
            "video_frame_count": 4,
            "video_duration_ms": 160,
            "total_ms": 160,
        },
        {
            "type": "progress",
            "request_id": "$REQUEST",
            "processed_ms": 160,
            "total_ms": 160,
        },
        {
            "type": "result",
            "request_id": "$REQUEST",
            "frames_processed": 4,
            "video_frame_count": 4,
            "video_duration_ms": 160,
            "total_ms": 160,
            "processed_ms": 160,
            "intervals": intervals,
        },
    ]


@pytest.mark.parametrize(
    ("reason", "start_frame", "end_frame", "start_ms", "end_ms", "values"),
    [
        ("short_shot", 0, 0, 0, 40, {"frame_count": 1.0}),
        ("short_shot", 0, 0, 0, 40, {"frame_count": 2}),
        ("short_shot", 0, 0, 0, 40, {"frame_count": 10**100}),
        ("silence", 0, 0, 0, 40, {"duration_ms": 40.0}),
        ("silence", 0, 0, 0, 40, {"duration_ms": 39}),
        ("silence", 0, 0, 0, 40, {"duration_ms": 10**100}),
    ],
)
def test_supervisor_rejects_untrusted_interval_counts_across_full_ipc(
    tmp_path: Path,
    reason: str,
    start_frame: int,
    end_frame: int,
    start_ms: int,
    end_ms: int,
    values: dict[str, Any],
) -> None:
    interval = {
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "reason_codes": [reason],
        "values": values,
    }
    result, error = _sandbox_exchange(tmp_path, _result_messages([interval]))
    assert result is None
    assert error is not None
    assert error.code == "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID"


def test_supervisor_accepts_exact_integer_interval_counts(tmp_path: Path) -> None:
    intervals = [
        {
            "start_frame": 0,
            "end_frame": 0,
            "start_ms": 0,
            "end_ms": 40,
            "reason_codes": ["short_shot"],
            "values": {"frame_count": 1},
        },
        {
            "start_frame": 1,
            "end_frame": 1,
            "start_ms": 40,
            "end_ms": 80,
            "reason_codes": ["silence"],
            "values": {"duration_ms": 40},
        },
    ]
    result, error = _sandbox_exchange(tmp_path, _result_messages(intervals))
    assert error is None
    assert result is not None
    assert len(result.intervals) == 2


def _sandbox_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "PYTHONPATH": str(ROOT),
        "LC_ALL": "C",
        "DATABASE_URL": "sqlite:///:memory:",
        "ALLOW_SQLITE_FOR_TESTS": "true",
        "STORAGE_ROOT": str(tmp_path / "sandbox-storage"),
        "PACKAGE_ROOT": str(tmp_path / "sandbox-packages"),
    }


def _sandbox_health_command(socket_path: Path, timeout: float = 0.5) -> list[str]:
    return [
        str(ROOT / "backend/.venv/bin/python"),
        "-m",
        "backend.app.technical_qc_sandbox",
        "--socket-path",
        str(socket_path),
        "--healthcheck",
        "--health-timeout-seconds",
        str(timeout),
    ]


def test_sandbox_health_requires_ready_status_and_real_unix_round_trip(
    tmp_path: Path,
) -> None:
    sandbox = importlib.import_module("backend.app.technical_qc_sandbox")
    socket_path = Path("/tmp") / f"tqc-fix3-health-{os.getpid()}-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    process = subprocess.Popen(
        [
            str(ROOT / "backend/.venv/bin/python"),
            "-m",
            "backend.app.technical_qc_sandbox",
            "--socket-path",
            str(socket_path),
            "--concurrency",
            "1",
        ],
        cwd=ROOT,
        env=_sandbox_environment(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        sandbox.wait_for_socket(socket_path, timeout_seconds=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            not status_path.is_file()
            or status_path.read_text(encoding="ascii") != "ready"
        ):
            time.sleep(0.01)
        healthy = subprocess.run(
            _sandbox_health_command(socket_path),
            cwd=ROOT,
            env=_sandbox_environment(tmp_path),
            check=False,
            capture_output=True,
            timeout=3,
        )
        assert healthy.returncode == 0, healthy.stderr.decode(errors="replace")

        status_path.write_text("fatal", encoding="ascii")
        fatal = subprocess.run(
            _sandbox_health_command(socket_path),
            cwd=ROOT,
            env=_sandbox_environment(tmp_path),
            check=False,
            capture_output=True,
            timeout=3,
        )
        assert fatal.returncode != 0
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
        socket_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)


def test_sandbox_health_rejects_ready_socket_without_protocol_responder(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"tqc-fix3-dead-{os.getpid()}-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        listener.listen(1)
        status_path.write_text("ready", encoding="ascii")
        started = time.monotonic()
        checked = subprocess.run(
            _sandbox_health_command(socket_path, timeout=0.1),
            cwd=ROOT,
            env=_sandbox_environment(tmp_path),
            check=False,
            capture_output=True,
            timeout=2,
        )
    socket_path.unlink(missing_ok=True)
    status_path.unlink(missing_ok=True)
    assert checked.returncode != 0
    assert time.monotonic() - started < 1


def test_worker_heartbeat_changes_only_on_service_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    heartbeat = tmp_path / "worker-heartbeat"
    status = tmp_path / "worker-status"
    observed: dict[str, bool] = {}

    def controlled_cycle(**callbacks: Any) -> dict[str, int]:
        initial = heartbeat.stat().st_mtime_ns
        time.sleep(0.08)
        observed["unchanged_without_event"] = heartbeat.stat().st_mtime_ns == initial
        event = callbacks.get("on_service_event")
        observed["callback_present"] = callable(event)
        if callable(event):
            event()
        observed["changed_after_event"] = heartbeat.stat().st_mtime_ns > initial
        qc._WORKER_STOP.set()
        return {"complete": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(qc, "WORKER_HEARTBEAT_REFRESH_SECONDS", 0.02)
    monkeypatch.setattr(qc, "process_pending_technical_qc", controlled_cycle)
    qc.run_technical_qc_worker_loop(
        1,
        heartbeat_path=heartbeat,
        status_path=status,
    )
    assert observed == {
        "unchanged_without_event": True,
        "callback_present": True,
        "changed_after_event": True,
    }
    assert status.read_text(encoding="ascii") == "stopped"


def test_active_probe_lease_progress_and_completion_emit_worker_service_events(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    create_project_item(client, code="TASK6-FIX3-HEALTH-EVENTS")
    emitted: list[int] = []

    def controlled_scan(claim: Any, **callbacks: Any) -> Any:
        probe = qc.TechnicalQcMediaProbe(4, 160, 160)
        callbacks["on_probe"](probe)
        assert callbacks["on_heartbeat"]() is True
        callbacks["on_progress"](80)
        return qc.TechnicalQcScanResult(
            intervals=(),
            processed_ms=160,
            frames_processed=4,
            video_frame_count=4,
            video_duration_ms=160,
            total_ms=160,
        )

    monkeypatch.setattr(qc, "scan_technical_qc_claim", controlled_scan)
    settings = SimpleNamespace(
        storage_root=Path("/unused-by-controlled-scan"),
        technical_qc_sandbox_socket=Path("/unused-by-controlled-scan/sandbox.sock"),
        technical_qc_ffmpeg_command="/usr/bin/false",
        technical_qc_timeout_seconds=5.0,
        technical_qc_output_limit_bytes=64 * 1024,
        technical_qc_worker_concurrency=1,
        technical_qc_per_project_concurrency=1,
        technical_qc_lease_seconds=60,
        technical_qc_worker_max_attempts=3,
        technical_qc_worker_retry_delay_seconds=30,
    )
    result = qc.process_technical_qc_batch(
        settings,
        runtime_binding=None,
        on_service_event=lambda: emitted.append(time.time_ns()),
    )
    assert result == {"complete": 1, "failed": 0, "skipped": 0}
    assert len(emitted) >= 5


def test_worker_fatal_status_is_atomic_and_not_overwritten_by_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    heartbeat = tmp_path / "worker-heartbeat"
    status = tmp_path / "worker-status"

    def fatal_cycle(**callbacks: Any) -> dict[str, int]:
        fatal = callbacks.get("on_fatal")
        if callable(fatal):
            fatal(qc.TechnicalQcFatalError("TECHNICAL_QC_REAP_FAILED", retryable=False))
        qc._WORKER_FATAL.set()
        return {"complete": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(qc, "process_pending_technical_qc", fatal_cycle)
    with pytest.raises(qc.TechnicalQcFatalError):
        qc.run_technical_qc_worker_loop(
            1,
            heartbeat_path=heartbeat,
            status_path=status,
        )
    assert status.read_text(encoding="ascii") == "fatal"
    assert qc.technical_qc_worker_healthcheck(
        heartbeat_path=heartbeat,
        status_path=status,
        max_age_seconds=90,
    ) is False


def test_compose_healthchecks_consume_service_state_and_protocol() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    worker_health = compose["services"]["technical-qc-worker"]["healthcheck"]["test"]
    sandbox_health = compose["services"]["technical-qc-sandbox"]["healthcheck"]["test"]
    assert worker_health == [
        "CMD",
        "python",
        "-m",
        "backend.app.technical_qc",
        "health",
    ]
    assert sandbox_health == [
        "CMD",
        "python",
        "-m",
        "backend.app.technical_qc_sandbox",
        "--socket-path",
        "/run/fj-technical-qc/sandbox.sock",
        "--healthcheck",
    ]
