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


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    return importlib.import_module("backend.app.technical_qc_sandbox")


@pytest.mark.parametrize("operation", ["receive", "settimeout", "sendall"])
def test_health_runtime_resource_error_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    sandbox = _sandbox(tmp_path, monkeypatch)
    socket_path = Path("/tmp") / f"tqc-fix8-{operation}-{time.time_ns()}.sock"
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    status_path = socket_path.with_name("sandbox.status")
    original_receive = sandbox._receive_request_and_fd
    original_settimeout = socket.socket.settimeout
    original_sendall = socket.socket.sendall

    if operation == "receive":
        def failing_receive(connection: socket.socket, *, allow_health: bool = False) -> Any:
            if allow_health:
                raise OSError(errno.ENOMEM, "controlled health receive resource error")
            return original_receive(connection, allow_health=allow_health)

        monkeypatch.setattr(sandbox, "_receive_request_and_fd", failing_receive)
    elif operation == "settimeout":
        def failing_settimeout(connection: socket.socket, value: float | None) -> None:
            if connection.getsockname() == str(health_path) and value == sandbox.HEALTH_REQUEST_TIMEOUT_SECONDS:
                raise OSError(errno.EMFILE, "controlled health timeout resource error")
            original_settimeout(connection, value)

        monkeypatch.setattr(socket.socket, "settimeout", failing_settimeout)
    else:
        def failing_sendall(connection: socket.socket, payload: bytes) -> None:
            if connection.getsockname() == str(health_path):
                raise OSError(errno.ENOSPC, "controlled health send resource error")
            original_sendall(connection, payload)

        monkeypatch.setattr(socket.socket, "sendall", failing_sendall)

    def trigger() -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not health_path.exists():
            time.sleep(0.01)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(health_path))
                client.sendall(b'{"request_id":"00000000000000000000000000000000","type":"health"}\n')
                client.recv(1)
        except OSError:
            pass

    def fallback() -> None:
        time.sleep(0.5)
        if not sandbox._FATAL.is_set():
            sandbox._STOP.set()

    threading.Thread(target=trigger, daemon=True).start()
    threading.Thread(target=fallback, daemon=True).start()
    try:
        with pytest.raises(SystemExit) as caught:
            sandbox.serve_forever(socket_path, concurrency=1)
        assert caught.value.code == 70
        assert sandbox._FATAL.is_set()
        assert sandbox._STOP.is_set()
        assert status_path.read_text(encoding="ascii") == "fatal"
    finally:
        sandbox._FATAL.clear()
        sandbox._STOP.clear()
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)


def test_health_runtime_broken_pipe_is_connection_local_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _sandbox(tmp_path, monkeypatch)
    assert sandbox._is_fatal_health_os_error(OSError(errno.EPIPE, "disconnect")) is False
    assert sandbox._is_fatal_health_os_error(OSError(errno.ECONNRESET, "reset")) is False
    assert sandbox._is_fatal_health_os_error(OSError(errno.ENOMEM, "resource")) is True


def test_sandbox_fatal_status_write_failure_cannot_override_exit70(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"tqc-fix8-double-{time.time_ns()}.sock"
    script = f"""
import errno, socket, threading, time
from pathlib import Path
import backend.app.technical_qc_sandbox as sandbox
scan_path = Path({str(socket_path)!r})
health_path = sandbox.sandbox_health_socket_path(scan_path)
original_accept = socket.socket.accept
def fail_health_accept(listener):
    if listener.getsockname() == str(health_path):
        raise OSError(errno.EMFILE, 'controlled health resource failure')
    return original_accept(listener)
socket.socket.accept = fail_health_accept
original_write = sandbox._atomic_worker_state
def fail_fatal_write(path, value):
    if value == 'fatal':
        raise OSError(errno.ENOSPC, 'controlled persistent fatal status failure')
    original_write(path, value)
sandbox._atomic_worker_state = fail_fatal_write
def trigger():
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not health_path.exists():
        time.sleep(0.01)
    if health_path.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(health_path))
        except OSError:
            pass
threading.Thread(target=trigger, daemon=True).start()
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
    assert not socket_path.exists()
    assert not socket_path.with_suffix(".health.sock").exists()
