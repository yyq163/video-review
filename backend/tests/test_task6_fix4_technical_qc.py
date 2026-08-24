from __future__ import annotations

import importlib
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from .conftest import create_item, create_project_item, upload_video
from .test_task6_fix1_technical_qc import _settings


def _qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def _sandbox() -> Any:
    return importlib.import_module("backend.app.technical_qc_sandbox")


def _db() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.database",
    )


def _models() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )


def _wait_ready(socket_path: Path, status_path: Path) -> None:
    _sandbox().wait_for_socket(socket_path, timeout_seconds=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if status_path.is_file() and status_path.read_text(encoding="ascii") == "ready":
            return
        time.sleep(0.01)
    raise TimeoutError("sandbox did not report ready")


def test_health_remains_available_when_all_scan_slots_are_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix4-full-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    release = threading.Event()
    occupied = threading.Event()
    lock = threading.Lock()
    active = 0
    observed: dict[str, Any] = {}

    def blocking_scan(connection: socket.socket) -> None:
        nonlocal active
        with connection, lock:
            active += 1
            if active == 2:
                occupied.set()
        release.wait(2)

    monkeypatch.setattr(sandbox, "_handle_connection", blocking_scan)

    def exercise() -> None:
        clients: list[socket.socket] = []
        try:
            _wait_ready(socket_path, status_path)
            for _index in range(2):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(socket_path))
                clients.append(client)
            assert occupied.wait(1)
            started = time.monotonic()
            observed["healthy"] = sandbox.sandbox_healthcheck(
                socket_path,
                timeout_seconds=0.4,
            )
            observed["elapsed"] = time.monotonic() - started
        finally:
            release.set()
            for client in clients:
                client.close()
            sandbox._STOP.set()

    coordinator = threading.Thread(target=exercise, daemon=True)
    coordinator.start()
    try:
        sandbox.serve_forever(socket_path, concurrency=2)
    finally:
        release.set()
        coordinator.join(timeout=2)
        socket_path.unlink(missing_ok=True)
        socket_path.with_suffix(".health.sock").unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
    assert observed["healthy"] is True
    assert observed["elapsed"] < 0.4


def test_slow_health_connections_do_not_exhaust_health_capacity() -> None:
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix4-slow-{time.time_ns()}.sock"
    health_path = socket_path.with_suffix(".health.sock")
    status_path = socket_path.with_name("sandbox.status")
    observed: dict[str, Any] = {}

    def exercise() -> None:
        clients: list[socket.socket] = []
        try:
            _wait_ready(socket_path, status_path)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not health_path.exists():
                time.sleep(0.01)
            observed["health_socket_exists"] = health_path.exists()
            if health_path.exists():
                for _index in range(8):
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.connect(str(health_path))
                    clients.append(client)
                observed["healthy"] = sandbox.sandbox_healthcheck(
                    socket_path,
                    timeout_seconds=0.5,
                )
        finally:
            for client in clients:
                client.close()
            sandbox._STOP.set()

    coordinator = threading.Thread(target=exercise, daemon=True)
    coordinator.start()
    try:
        sandbox.serve_forever(socket_path, concurrency=1)
    finally:
        coordinator.join(timeout=2)
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
    assert observed == {"health_socket_exists": True, "healthy": True}


def test_late_fatal_during_idle_wakes_worker_and_exits_before_next_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    heartbeat = tmp_path / "heartbeat"
    status = tmp_path / "status"
    cycle_count = 0
    fatal_at: list[float] = []
    trigger_done = threading.Event()

    def idle_cycle(**callbacks: Any) -> dict[str, int]:
        nonlocal cycle_count
        cycle_count += 1
        if cycle_count == 1:
            def trigger() -> None:
                time.sleep(0.05)
                fatal_at.append(time.monotonic())
                callbacks["on_fatal"](
                    qc.TechnicalQcFatalError(
                        "TECHNICAL_QC_REAP_FAILED",
                        retryable=False,
                    ),
                )
                trigger_done.set()

            threading.Thread(target=trigger, daemon=True).start()
        return {"complete": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(qc, "process_pending_technical_qc", idle_cycle)
    with pytest.raises(qc.TechnicalQcFatalError):
        qc.run_technical_qc_worker_loop(
            1,
            heartbeat_path=heartbeat,
            status_path=status,
        )
    exited_at = time.monotonic()
    assert trigger_done.wait(1)
    assert cycle_count == 1
    assert exited_at - fatal_at[0] < 0.3
    assert qc._WORKER_STOP.is_set()
    assert status.read_text(encoding="ascii") == "fatal"


def test_fatal_before_batch_does_not_claim_or_mark_task_cancelled(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    db = _db()
    models = _models()
    create_project_item(client, code="TASK6-FIX4-PRE-BATCH")
    with db.SessionLocal.begin() as session:
        assert qc.discover_ready_technical_qc_tasks(session) == 1
        task_id = session.scalar(select(models.TechnicalQcTaskModel.id))
    scan_called = False

    def cancelled_scan(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal scan_called
        scan_called = True
        raise qc.TechnicalQcError("TECHNICAL_QC_SCAN_CANCELLED", retryable=False)

    monkeypatch.setattr(qc, "scan_technical_qc_claim", cancelled_scan)
    qc._WORKER_FATAL.set()
    qc._WORKER_STOP.set()
    raised = False
    try:
        qc.process_technical_qc_batch(_settings(concurrency=1, per_project=1), runtime_binding=None)
    except qc.TechnicalQcFatalError:
        raised = True
    finally:
        qc._WORKER_FATAL.clear()
        qc._WORKER_STOP.clear()
    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, task_id)
        assert task is not None
        state = (task.status, task.error_code, task.claim_id)
    assert raised is True
    assert scan_called is False
    assert state == ("queued", None, None)


def test_late_fatal_after_future_completion_blocks_refill_and_submit(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    db = _db()
    models = _models()
    project, _first = create_project_item(client, code="TASK6-FIX4-REFILL")
    create_item(
        client,
        project["project_ref_id"],
        upload_video(client, seed=b"fix4-refill-second"),
        item_code="EP-2",
        episode_no=2,
    )
    processed: list[str] = []

    def late_fatal_process(claim: Any, *_args: Any) -> str:
        processed.append(claim.task_id)
        if len(processed) == 1:
            qc._WORKER_FATAL.set()
            qc._WORKER_STOP.set()
        return "complete"

    monkeypatch.setattr(qc, "_process_technical_qc_claim", late_fatal_process)
    raised = False
    try:
        qc.process_technical_qc_batch(_settings(concurrency=1, per_project=2), runtime_binding=None)
    except qc.TechnicalQcFatalError:
        raised = True
    finally:
        qc._WORKER_FATAL.clear()
        qc._WORKER_STOP.clear()
    with db.SessionLocal() as session:
        statuses = list(
            session.scalars(
                select(models.TechnicalQcTaskModel.status).order_by(
                    models.TechnicalQcTaskModel.created_at,
                ),
            ),
        )
    assert raised is True
    assert len(processed) == 1
    assert sorted(statuses) == ["queued", "running"]
