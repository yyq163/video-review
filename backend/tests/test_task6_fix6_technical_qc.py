from __future__ import annotations

import errno
import importlib
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def _sandbox() -> Any:
    return importlib.import_module("backend.app.technical_qc_sandbox")


def test_sandbox_fd_budget_preserves_headroom_under_nofile64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    sandbox = _sandbox()
    assert sandbox.SANDBOX_NOFILE_LIMIT == 64
    assert sandbox.SANDBOX_FD_BUDGET == (
        sandbox.SANDBOX_BASE_FD_RESERVE
        + sandbox.MAX_SCAN_CONNECTIONS
        + sandbox.MAX_SANDBOX_CONCURRENCY * sandbox.ACTIVE_SCAN_EXTRA_FDS
        + sandbox.MAX_PENDING_HEALTH_CONNECTIONS
    )
    assert sandbox.SANDBOX_NOFILE_LIMIT - sandbox.SANDBOX_FD_BUDGET >= 8


def test_worker_async_fatal_hard_exits_real_process_within_one_second(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "heartbeat"
    status = tmp_path / "status"
    script = f"""
import os, threading, time
from pathlib import Path
import backend.app.technical_qc as qc

def cycle(**callbacks):
    def fatal():
        time.sleep(0.05)
        callbacks['on_fatal'](qc.TechnicalQcFatalError('TECHNICAL_QC_REAP_FAILED', retryable=False))
    threading.Thread(target=fatal, daemon=True).start()
    return {{'complete': 0, 'failed': 0, 'skipped': 0}}

qc.process_pending_technical_qc = cycle
qc.run_technical_qc_worker_loop(
    60,
    heartbeat_path=Path({str(heartbeat)!r}),
    status_path=Path({str(status)!r}),
    hard_exit=os._exit,
)
"""
    started = time.monotonic()
    completed = subprocess.run(
        [str(ROOT / "backend/.venv/bin/python"), "-c", script],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(ROOT),
            "LC_ALL": "C",
            "DATABASE_URL": "sqlite:///:memory:",
            "ALLOW_SQLITE_FOR_TESTS": "true",
            "STORAGE_ROOT": str(tmp_path / "storage"),
            "PACKAGE_ROOT": str(tmp_path / "packages"),
        },
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 70, completed.stderr.decode(errors="replace")
    assert time.monotonic() - started < 1
    assert status.read_text(encoding="ascii") == "fatal"


def test_late_fatal_cannot_be_overwritten_by_normal_stop_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    heartbeat = tmp_path / "heartbeat"
    status = tmp_path / "status"
    callback: list[Any] = []
    stopped_write_entered = threading.Event()
    fatal_callback_done = threading.Event()
    exit_codes: list[int] = []
    original_write = qc._atomic_worker_state

    def cycle(**callbacks: Any) -> dict[str, int]:
        callback.append(callbacks["on_fatal"])
        qc._WORKER_STOP.set()
        return {"complete": 0, "failed": 0, "skipped": 0}

    def controlled_write(path: Path, value: str) -> None:
        if path == status and value == "stopped":
            stopped_write_entered.set()
            fatal_callback_done.wait(0.15)
        original_write(path, value)

    def trigger() -> None:
        assert stopped_write_entered.wait(1)
        callback[0](
            qc.TechnicalQcFatalError(
                "TECHNICAL_QC_REAP_FAILED",
                retryable=False,
            ),
        )
        fatal_callback_done.set()

    monkeypatch.setattr(qc, "process_pending_technical_qc", cycle)
    monkeypatch.setattr(qc, "_atomic_worker_state", controlled_write)
    thread = threading.Thread(target=trigger, daemon=True)
    thread.start()
    qc.run_technical_qc_worker_loop(
        1,
        heartbeat_path=heartbeat,
        status_path=status,
        hard_exit=exit_codes.append,
    )
    thread.join(timeout=1)
    assert exit_codes == [70]
    assert qc._WORKER_FATAL.is_set()
    assert qc._WORKER_STOP.is_set()
    assert status.read_text(encoding="ascii") == "fatal"
    qc._WORKER_FATAL.clear()
    qc._WORKER_STOP.clear()


def test_normal_stop_remains_stopped_without_hard_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _qc()
    status = tmp_path / "status"
    exits: list[int] = []

    def stop_cycle(**_callbacks: Any) -> dict[str, int]:
        qc._WORKER_STOP.set()
        return {"complete": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(qc, "process_pending_technical_qc", stop_cycle)
    qc.run_technical_qc_worker_loop(
        1,
        heartbeat_path=tmp_path / "heartbeat",
        status_path=status,
        hard_exit=exits.append,
    )
    assert exits == []
    assert status.read_text(encoding="ascii") == "stopped"
    assert not qc._WORKER_STOP.is_set()


def test_health_accept_emfile_marks_fatal_and_exits_instead_of_staying_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix6-health-emfile-{time.time_ns()}.sock"
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    status_path = socket_path.with_name("sandbox.status")
    original_accept = socket.socket.accept
    fallback_stop = threading.Event()

    def exhausted_accept(listener: socket.socket) -> Any:
        if listener.getsockname() == str(health_path):
            raise OSError(errno.EMFILE, "controlled health descriptor exhaustion")
        return original_accept(listener)

    def fallback() -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not health_path.exists():
            time.sleep(0.01)
        if health_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(health_path))
            except OSError:
                pass
        time.sleep(0.2)
        if not sandbox._FATAL.is_set():
            sandbox._STOP.set()
        fallback_stop.set()

    monkeypatch.setattr(socket.socket, "accept", exhausted_accept)
    threading.Thread(target=fallback, daemon=True).start()
    try:
        with pytest.raises(SystemExit) as caught:
            sandbox.serve_forever(socket_path, concurrency=1)
        assert caught.value.code == 70
        assert status_path.read_text(encoding="ascii") == "fatal"
        assert sandbox._FATAL.is_set()
        assert sandbox._STOP.is_set()
        assert sandbox._ACTIVE_CONNECTIONS == set()
    finally:
        fallback_stop.wait(1)
        sandbox._FATAL.clear()
        sandbox._STOP.clear()
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)


def test_rlimit64_health_fault_subprocess_exits_nonzero_without_false_ready(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"tqc-fix6-rlimit-{time.time_ns()}.sock"
    status_path = socket_path.with_name("sandbox.status")
    script = f"""
import errno, resource, socket, threading, time
from pathlib import Path
import backend.app.technical_qc_sandbox as sandbox

resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
scan_path = Path({str(socket_path)!r})
health_path = sandbox.sandbox_health_socket_path(scan_path)
original_accept = socket.socket.accept
def fail_health(listener):
    if listener.getsockname() == str(health_path):
        raise OSError(errno.EMFILE, 'rlimit64 controlled health fault')
    return original_accept(listener)
socket.socket.accept = fail_health
def trigger_health():
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not health_path.exists():
        time.sleep(0.01)
    if health_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(health_path))
        except OSError:
            pass
threading.Thread(target=trigger_health, daemon=True).start()
sandbox.serve_forever(scan_path, concurrency=1)
"""
    completed = subprocess.run(
        [str(ROOT / "backend/.venv/bin/python"), "-c", script],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": str(ROOT),
            "LC_ALL": "C",
            "DATABASE_URL": "sqlite:///:memory:",
            "ALLOW_SQLITE_FOR_TESTS": "true",
            "STORAGE_ROOT": str(tmp_path / "storage"),
            "PACKAGE_ROOT": str(tmp_path / "packages"),
        },
        check=False,
        capture_output=True,
        timeout=2,
    )
    assert completed.returncode == 70, completed.stderr.decode(errors="replace")
    assert status_path.read_text(encoding="ascii") == "fatal"
    socket_path.unlink(missing_ok=True)
    socket_path.with_suffix(".health.sock").unlink(missing_ok=True)
    status_path.unlink(missing_ok=True)
