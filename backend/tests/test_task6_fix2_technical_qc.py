from __future__ import annotations

import importlib
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from .conftest import create_item, create_project_item, upload_video
from .test_task6_fix1_technical_qc import (
    _insert_second_ready_version_for_same_item,
    _seed_large_ready_backlog,
)

from .test_task6_technical_qc import _file_claim, _write_executable


def _qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def _db() -> Any:
    return importlib.import_module("backend.app.modules.final_cut_review.infra.database")


def _models() -> Any:
    return importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )


def test_ffprobe_checks_cancel_while_running_and_releases_slot(tmp_path: Path) -> None:
    qc = _qc()
    probe = _write_executable(
        tmp_path / "slow-probe.py",
        "import time\ntime.sleep(5)\n",
    )
    media = tmp_path / "probe-input.mp4"
    media.write_bytes(b"probe-input")
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    started = time.monotonic()
    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.probe_technical_qc_media(
            handle,
            ffprobe_command=str(probe),
            deadline=time.monotonic() + 2,
            output_limit_bytes=4_096,
            should_cancel=cancel,
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_CANCELLED"
    assert checks >= 3
    assert time.monotonic() - started < 0.5


def test_ffprobe_incremental_output_limit_kills_before_process_finishes(tmp_path: Path) -> None:
    qc = _qc()
    probe = _write_executable(
        tmp_path / "noisy-probe.py",
        "import sys,time\n"
        "for _ in range(1000):\n"
        " sys.stdout.write('x' * 256); sys.stdout.flush(); time.sleep(0.002)\n",
    )
    media = tmp_path / "noisy-input.mp4"
    media.write_bytes(b"probe-input")
    started = time.monotonic()
    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.probe_technical_qc_media(
            handle,
            ffprobe_command=str(probe),
            deadline=time.monotonic() + 3,
            output_limit_bytes=1_024,
        )
    assert caught.value.code == "TECHNICAL_QC_OUTPUT_LIMIT"
    # The production TERM/KILL/reap budget is one second; keep enough scheduler
    # headroom while still proving overflow stops well before the 3s deadline.
    assert time.monotonic() - started < 1.5


def test_spawn_timeout_ownership_reaps_late_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    qc = _qc()
    actions: list[str] = []
    process_ready = threading.Event()

    class LateProcess:
        pid = 4242
        stdout = None
        stderr = None
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            actions.append("term")
            self.returncode = -15

        def kill(self) -> None:
            actions.append("kill")
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            actions.append(f"wait:{timeout}")
            return self.returncode or 0

    late = LateProcess()

    def delayed_popen(*_args: Any, **_kwargs: Any) -> LateProcess:
        time.sleep(0.12)
        process_ready.set()
        return late

    monkeypatch.setattr(subprocess, "Popen", delayed_popen)
    monkeypatch.setattr(
        qc,
        "_signal_process_group",
        lambda process, signum: process.terminate() if signum == 15 else process.kill(),
    )
    with pytest.raises(qc.TechnicalQcError) as caught:
        qc._popen_with_deadline(
            ["fake"],
            {},
            deadline=time.monotonic() + 0.02,
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_TIMEOUT"
    assert process_ready.wait(1)
    deadline = time.monotonic() + 1
    while not actions and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "term" in actions
    assert any(action.startswith("wait:") for action in actions)


def test_spawn_handoff_barrier_cannot_lose_late_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    created = threading.Event()
    release_publish = threading.Event()
    reaped = threading.Event()

    class LateProcess:
        pid = 4444
        stdout = None
        stderr = None
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            reaped.set()
            return self.returncode or 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: LateProcess())
    monkeypatch.setattr(
        qc,
        "_signal_process_group",
        lambda process, signum: process.terminate() if signum == 15 else process.kill(),
    )

    def before_publish(_process: Any) -> None:
        created.set()
        assert release_publish.wait(1)

    with pytest.raises(qc.TechnicalQcError) as caught:
        qc._popen_with_deadline(
            ["fake"],
            {},
            deadline=time.monotonic() + 0.03,
            before_publish=before_publish,
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_TIMEOUT"
    assert created.is_set()
    release_publish.set()
    assert reaped.wait(1)


def test_late_unreapable_child_invokes_fatal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    fatal = threading.Event()

    class Unreapable:
        pid = 4343
        stdout = None
        stderr = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("fake", timeout)

    def delayed_popen(*_args: Any, **_kwargs: Any) -> Unreapable:
        time.sleep(0.06)
        return Unreapable()

    monkeypatch.setattr(subprocess, "Popen", delayed_popen)
    monkeypatch.setattr(qc, "_signal_process_group", lambda *_args: None)
    with pytest.raises(qc.TechnicalQcError) as caught:
        qc._popen_with_deadline(
            ["fake"],
            {},
            deadline=time.monotonic() + 0.01,
            on_late_fatal=lambda _error: fatal.set(),
        )
    assert caught.value.code == "TECHNICAL_QC_SCAN_TIMEOUT"
    assert fatal.wait(2)


def _sandbox_exchange(
    tmp_path: Path,
    messages: list[dict[str, Any]],
) -> tuple[Any, Any]:
    qc = _qc()
    sandbox = importlib.import_module("backend.app.technical_qc_sandbox")
    media = tmp_path / f"ipc-{time.time_ns()}.mp4"
    media.write_bytes(b"ipc-input")
    claim = _file_claim(qc, media, total_ms=160, frames=4)
    socket_path = Path("/tmp") / f"tqc-fix2-{time.time_ns()}.sock"

    def server() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            listener.listen(1)
            connection, _ = listener.accept()
            with connection:
                request, descriptor = sandbox._receive_request_and_fd(connection)
                try:
                    for message in messages:
                        payload = dict(message)
                        if payload.get("request_id") == "$REQUEST":
                            payload["request_id"] = request["request_id"]
                        connection.sendall(sandbox._json_line(payload))
                finally:
                    os.close(descriptor)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    sandbox.wait_for_socket(socket_path, timeout_seconds=1)
    try:
        with media.open("rb") as handle:
            result = sandbox.scan_via_sandbox(
                socket_path,
                claim,
                handle,
                timeout_seconds=1,
                output_limit_bytes=16 * 1024,
            )
        return result, None
    except qc.TechnicalQcError as exc:
        return None, exc
    finally:
        thread.join(timeout=1)
        socket_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "messages",
    [
        [
            {
                "type": "result",
                "request_id": "$REQUEST",
                "frames_processed": 4,
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
                "processed_ms": 160,
                "intervals": [],
            },
        ],
        [
            {
                "type": "probe",
                "request_id": "$REQUEST",
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
            },
            {
                "type": "probe",
                "request_id": "$REQUEST",
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
            },
        ],
        [
            {
                "type": "probe",
                "request_id": "$REQUEST",
                "video_frame_count": 4,
                "video_duration_ms": 160,
                "total_ms": 160,
            },
            {"type": "progress", "request_id": "$REQUEST", "processed_ms": 100, "total_ms": 160},
            {"type": "progress", "request_id": "$REQUEST", "processed_ms": 50, "total_ms": 160},
        ],
        [
            {
                "type": "probe",
                "request_id": "$REQUEST",
                "video_frame_count": 4,
                "video_duration_ms": 160,
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
                "intervals": [
                    {
                        "start_frame": 999_999,
                        "end_frame": 999_999,
                        "start_ms": 999_999_999,
                        "end_ms": 999_999_999,
                        "reason_codes": ["not_a_qc_rule"],
                        "values": {"arbitrary": "payload"},
                    },
                ],
            },
        ],
    ],
)
def test_supervisor_rejects_out_of_order_or_unbounded_sandbox_messages(
    tmp_path: Path,
    messages: list[dict[str, Any]],
) -> None:
    _result, error = _sandbox_exchange(tmp_path, messages)
    assert error is not None
    assert error.code == "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID"


def test_supervisor_maps_sandbox_error_retryability_locally(tmp_path: Path) -> None:
    _result, error = _sandbox_exchange(
        tmp_path,
        [
            {
                "type": "error",
                "code": "TECHNICAL_QC_MEDIA_INVALID",
            },
        ],
    )
    assert error is not None
    assert error.code == "TECHNICAL_QC_MEDIA_INVALID"
    assert error.retryable is False


def test_discovery_fairly_creates_small_project_task_before_old_backlog_drains(client: Any) -> None:
    del client
    qc = _qc()
    db = _db()
    models = _models()
    with db.SessionLocal.begin() as session:
        large_project = _seed_large_ready_backlog(session, count=200, include_tasks=False)
        small_project = _seed_large_ready_backlog(session, count=1, include_tasks=False)
        assert qc.discover_ready_technical_qc_tasks(session, batch_size=2) == 2
        discovered = set(
            session.scalars(select(models.TechnicalQcTaskModel.project_ref_id)),
        )
    assert discovered == {large_project, small_project}


def test_same_item_versions_cannot_fill_sql_window_and_hide_next_episode(client: Any) -> None:
    qc = _qc()
    db = _db()
    project, first_item = create_project_item(client, code="TASK6-FIX2-WINDOW")
    second_item = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, seed=b"second-episode"),
        item_code="EP-2",
        episode_no=2,
    )
    with db.SessionLocal.begin() as session:
        for _index in range(9):
            _insert_second_ready_version_for_same_item(session, first_item["id"])
        assert qc.discover_ready_technical_qc_tasks(session, batch_size=12) == 11
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=2,
            per_project_limit=2,
            lease_seconds=60,
            candidate_scan_limit=16,
        )
    assert {claim.review_item_id for claim in claims} == {
        first_item["id"],
        second_item["id"],
    }


def test_unreconciled_expired_running_item_never_crashes_partial_unique_claim(client: Any) -> None:
    qc = _qc()
    db = _db()
    models = _models()
    project, first_item = create_project_item(client, code="TASK6-FIX2-RECONCILE")
    second_item = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, seed=b"reconcile-second"),
        item_code="EP-2",
        episode_no=2,
    )
    now = datetime.now(timezone.utc)
    with db.SessionLocal.begin() as session:
        _insert_second_ready_version_for_same_item(session, first_item["id"])
        qc.discover_ready_technical_qc_tasks(session, batch_size=4)
        tasks = list(session.scalars(select(models.TechnicalQcTaskModel)))
        first_tasks = [task for task in tasks if task.review_item_id == first_item["id"]]
        second_task = next(task for task in tasks if task.review_item_id == second_item["id"])
        first_tasks[0].status = "running"
        first_tasks[0].claim_id = "expired-first-item"
        first_tasks[0].lease_expires_at = now - timedelta(seconds=1)
        first_tasks[0].attempts = 1
        second_task.status = "running"
        second_task.claim_id = "expired-second-item"
        second_task.lease_expires_at = now - timedelta(seconds=2)
        second_task.attempts = 1
        session.flush()

    with db.SessionLocal.begin() as session:
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=60,
            candidate_scan_limit=1,
            now=now,
        )
    assert all(claim.review_item_id != first_item["id"] for claim in claims)


def test_sandbox_fatal_reap_path_exits_service_pid1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    sandbox = importlib.import_module("backend.app.technical_qc_sandbox")
    socket_path = Path("/tmp") / f"tqc-fatal-{time.time_ns()}.sock"

    def fatal_handler(connection: socket.socket) -> None:
        connection.close()
        sandbox._mark_fatal(
            qc.TechnicalQcFatalError("TECHNICAL_QC_REAP_FAILED", retryable=False),
        )

    monkeypatch.setattr(sandbox, "_handle_connection", fatal_handler)

    def connect() -> None:
        sandbox.wait_for_socket(socket_path, timeout_seconds=1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))

    connector = threading.Thread(target=connect, daemon=True)
    connector.start()
    try:
        with pytest.raises(SystemExit) as caught:
            sandbox.serve_forever(socket_path, concurrency=1)
        assert caught.value.code == 70
    finally:
        connector.join(timeout=1)
        socket_path.unlink(missing_ok=True)


def test_supervisor_fatal_stops_health_pulse_and_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    heartbeat = tmp_path / "worker-heartbeat"
    status = tmp_path / "worker-status"

    def fatal_cycle(**_callbacks: Any) -> dict[str, int]:
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
