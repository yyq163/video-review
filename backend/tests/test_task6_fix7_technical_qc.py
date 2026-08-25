from __future__ import annotations

import errno
import importlib
import selectors
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _sandbox() -> Any:
    return importlib.import_module("backend.app.technical_qc_sandbox")


def test_fatal_status_write_error_still_hard_exits_with_blocked_nondaemon_thread(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status"
    script = f"""
import os, threading, time
from pathlib import Path
import backend.app.technical_qc as qc

blocked = threading.Event()
threading.Thread(target=blocked.wait, daemon=False).start()
original_write = qc._atomic_worker_state
def failing_write(path, value):
    if value == 'fatal':
        raise OSError(28, 'controlled fatal status write failure')
    original_write(path, value)
qc._atomic_worker_state = failing_write
def cycle(**callbacks):
    def fatal():
        time.sleep(0.05)
        callbacks['on_fatal'](qc.TechnicalQcFatalError('TECHNICAL_QC_REAP_FAILED', retryable=False))
    threading.Thread(target=fatal, daemon=True).start()
    return {{'complete': 0, 'failed': 0, 'skipped': 0}}
qc.process_pending_technical_qc = cycle
qc.run_technical_qc_worker_loop(
    60,
    heartbeat_path=Path({str(tmp_path / 'heartbeat')!r}),
    status_path=Path({str(status_path)!r}),
    hard_exit=os._exit,
)
"""
    process = subprocess.Popen(
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        return_code = process.wait(timeout=0.8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
        return_code = None
    assert return_code == 70
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("failure_point", ["setblocking", "unregister"])
def test_health_connection_resource_error_cleans_all_and_exits_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    sandbox = _sandbox()
    socket_path = Path("/tmp") / f"tqc-fix7-{failure_point}-{time.time_ns()}.sock"
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    status_path = socket_path.with_name("sandbox.status")
    original_setblocking = socket.socket.setblocking
    original_selector_factory = selectors.DefaultSelector
    accepted_setblocking_calls = 0

    if failure_point == "setblocking":
        def failing_setblocking(connection: socket.socket, value: bool) -> None:
            nonlocal accepted_setblocking_calls
            if connection.getsockname() == str(health_path) and value is False:
                accepted_setblocking_calls += 1
                if accepted_setblocking_calls >= 2:
                    raise OSError(errno.ENOMEM, "controlled health setblocking failure")
            original_setblocking(connection, value)

        monkeypatch.setattr(socket.socket, "setblocking", failing_setblocking)
    else:
        class FailingSelector:
            def __init__(self) -> None:
                self.real = original_selector_factory()
                self.listener: Any = None

            def register(self, fileobj: Any, events: int) -> Any:
                if self.listener is None:
                    self.listener = fileobj
                return self.real.register(fileobj, events)

            def unregister(self, fileobj: Any) -> Any:
                if fileobj is not self.listener:
                    raise OSError(errno.ENOMEM, "controlled health unregister failure")
                return self.real.unregister(fileobj)

            def select(self, timeout: float | None = None) -> Any:
                return self.real.select(timeout)

            def close(self) -> None:
                self.real.close()

        monkeypatch.setattr(sandbox.selectors, "DefaultSelector", FailingSelector)

    client_closed = threading.Event()

    def trigger() -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not health_path.exists():
            time.sleep(0.01)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.5)
                client.connect(str(health_path))
                client.sendall(b'{"request_id":"00000000000000000000000000000000","type":"health"}\n')
                try:
                    client.recv(1)
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            client_closed.set()

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
        assert client_closed.wait(1)
        assert sandbox._FATAL.is_set()
        assert sandbox._STOP.is_set()
        assert status_path.read_text(encoding="ascii") == "fatal"
        assert sandbox._ACTIVE_CONNECTIONS == set()
    finally:
        sandbox._FATAL.clear()
        sandbox._STOP.clear()
        socket_path.unlink(missing_ok=True)
        health_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
