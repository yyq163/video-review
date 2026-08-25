from __future__ import annotations

import errno
import importlib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

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


def _wait_ready(socket_path: Path) -> None:
    sandbox = _sandbox()
    sandbox.wait_for_socket(socket_path, timeout_seconds=2)
    status_path = socket_path.with_name("sandbox.status")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if status_path.is_file() and status_path.read_text(encoding="ascii") == "ready":
            return
        time.sleep(0.01)
    raise TimeoutError("sandbox did not report ready")


def test_scan_active_plus_pending_connections_are_bounded_and_closed_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix5-capacity-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    release = threading.Event()
    first_started = threading.Event()
    submitted = 0
    observed: dict[str, Any] = {}

    class CountingExecutor(ThreadPoolExecutor):
        def submit(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal submitted
            submitted += 1
            return super().submit(*args, **kwargs)

    def blocking_scan(connection: socket.socket, *_args: Any) -> None:
        first_started.set()
        release.wait(2)
        connection.close()

    monkeypatch.setattr(sandbox, "ThreadPoolExecutor", CountingExecutor)
    monkeypatch.setattr(sandbox, "_handle_connection", blocking_scan)

    def exercise() -> None:
        clients: list[socket.socket] = []
        try:
            _wait_ready(socket_path)
            for _index in range(20):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(str(socket_path))
                client.setblocking(False)
                clients.append(client)
            assert first_started.wait(1)
            time.sleep(0.1)
            observed["submitted"] = submitted
            observed["health"] = sandbox.sandbox_healthcheck(
                socket_path,
                timeout_seconds=0.5,
            )
            closed = 0
            for client in clients:
                try:
                    if client.recv(1) == b"":
                        closed += 1
                except BlockingIOError:
                    pass
            observed["closed_before_stop"] = closed
            sandbox._STOP.set()
        finally:
            release.set()
            for client in clients:
                client.close()

    coordinator = threading.Thread(target=exercise, daemon=True)
    coordinator.start()
    started = time.monotonic()
    try:
        sandbox.serve_forever(socket_path, concurrency=1)
    finally:
        release.set()
        coordinator.join(timeout=2)
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
    assert observed["submitted"] <= 8
    assert observed["closed_before_stop"] >= 12
    assert observed["health"] is True
    assert time.monotonic() - started < 2
    assert sandbox._ACTIVE_CONNECTIONS == set()


def test_scan_accept_resource_error_fails_closed_with_fatal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix5-emfile-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    original_accept = socket.socket.accept

    def exhausted_accept(listener: socket.socket) -> Any:
        if listener.getsockname() == str(socket_path):
            raise OSError(errno.EMFILE, "controlled file descriptor exhaustion")
        return original_accept(listener)

    monkeypatch.setattr(socket.socket, "accept", exhausted_accept)
    try:
        with pytest.raises(SystemExit) as caught:
            sandbox.serve_forever(socket_path, concurrency=1)
        assert caught.value.code == 70
        assert status_path.read_text(encoding="ascii") == "fatal"
    finally:
        sandbox._STOP.clear()
        sandbox._FATAL.clear()
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)


@pytest.mark.parametrize("fatal", [False, True])
def test_stop_or_fatal_cancellation_preserves_running_lease_for_recovery(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    fatal: bool,
) -> None:
    qc = _qc()
    db = _db()
    models = _models()
    create_project_item(client, code=f"TASK6-FIX5-RECOVER-{fatal}")
    with db.SessionLocal.begin() as session:
        assert qc.discover_ready_technical_qc_tasks(session) == 1
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            lease_seconds=60,
        )[0]

    def cancelled_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise qc.TechnicalQcError("TECHNICAL_QC_SCAN_CANCELLED", retryable=False)

    monkeypatch.setattr(qc, "scan_technical_qc_claim", cancelled_scan)
    qc._WORKER_STOP.set()
    if fatal:
        qc._WORKER_FATAL.set()
    try:
        outcome = qc._process_technical_qc_claim(
            claim,
            _settings(concurrency=1, per_project=1),
            None,
        )
    finally:
        qc._WORKER_STOP.clear()
        qc._WORKER_FATAL.clear()
    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert task is not None
        state = (
            task.status,
            task.claim_id,
            task.lease_expires_at is not None,
            task.error_code,
            task.next_attempt_at,
        )
    assert outcome == "skipped"
    assert state == ("running", claim.claim_id, True, None, None)


def test_batch_wait_polls_fatal_and_returns_without_waiting_for_next_future(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    project, _first = create_project_item(client, code="TASK6-FIX5-WAIT")
    create_item(
        client,
        project["project_ref_id"],
        upload_video(client, seed=b"fix5-wait-second"),
        item_code="EP-2",
        episode_no=2,
    )
    both_started = threading.Event()
    release_slow = threading.Event()
    fatal_set = threading.Event()
    done = threading.Event()
    lock = threading.Lock()
    started_count = 0
    failure: list[BaseException] = []

    def controlled_process(_claim: Any, *_args: Any) -> str:
        nonlocal started_count
        with lock:
            started_count += 1
            current = started_count
            if started_count == 2:
                both_started.set()
        assert both_started.wait(1)
        if current == 1:
            def trigger_fatal() -> None:
                time.sleep(0.05)
                qc._WORKER_FATAL.set()
                qc._WORKER_STOP.set()
                fatal_set.set()

            threading.Thread(target=trigger_fatal, daemon=True).start()
            return "complete"
        release_slow.wait(2)
        return "skipped"

    monkeypatch.setattr(qc, "_process_technical_qc_claim", controlled_process)

    def run_batch() -> None:
        try:
            qc.process_technical_qc_batch(
                _settings(concurrency=2, per_project=2),
                runtime_binding=None,
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run_batch, daemon=True)
    thread.start()
    try:
        assert fatal_set.wait(1)
        assert done.wait(0.3)
        assert len(failure) == 1
        assert isinstance(failure[0], qc.TechnicalQcFatalError)
    finally:
        release_slow.set()
        thread.join(timeout=2)
        qc._WORKER_FATAL.clear()
        qc._WORKER_STOP.clear()
