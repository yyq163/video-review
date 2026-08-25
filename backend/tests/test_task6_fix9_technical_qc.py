from __future__ import annotations

import importlib
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


def test_fatal_status_and_cleanup_memory_errors_cannot_override_exit70(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"tqc-fix9-fatal-{time.time_ns()}.sock"
    script = f"""
import errno, socket, threading, time
from pathlib import Path
import backend.app.technical_qc_sandbox as sandbox
scan_path = Path({str(socket_path)!r})
health_path = sandbox.sandbox_health_socket_path(scan_path)
original_accept = socket.socket.accept
def fail_health_accept(listener):
    if listener.getsockname() == str(health_path):
        raise OSError(errno.EMFILE, 'controlled health fatal')
    return original_accept(listener)
socket.socket.accept = fail_health_accept
original_write = sandbox._atomic_worker_state
def fail_fatal_write(path, value):
    if value == 'fatal':
        raise MemoryError('controlled fatal status memory failure')
    original_write(path, value)
sandbox._atomic_worker_state = fail_fatal_write
original_unlink = Path.unlink
def fail_socket_unlink(path, *args, **kwargs):
    if path in (scan_path, health_path):
        raise MemoryError('controlled fatal cleanup memory failure')
    return original_unlink(path, *args, **kwargs)
Path.unlink = fail_socket_unlink
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


def test_normal_stop_does_not_swallow_cleanup_memory_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _sandbox(tmp_path, monkeypatch)
    socket_path = Path("/tmp") / f"tqc-fix9-normal-{time.time_ns()}.sock"
    health_path = sandbox.sandbox_health_socket_path(socket_path)
    original_unlink = Path.unlink

    def fail_scan_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == socket_path:
            raise MemoryError("controlled normal cleanup failure")
        original_unlink(path, *args, **kwargs)

    def stop() -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not health_path.exists():
            time.sleep(0.01)
        sandbox._STOP.set()

    monkeypatch.setattr(Path, "unlink", fail_scan_unlink)
    threading.Thread(target=stop, daemon=True).start()
    with pytest.raises(MemoryError, match="controlled normal cleanup failure"):
        sandbox.serve_forever(socket_path, concurrency=1)
    sandbox._STOP.clear()
    original_unlink(socket_path, missing_ok=True)
    original_unlink(health_path, missing_ok=True)
    original_unlink(socket_path.with_name("sandbox.status"), missing_ok=True)
