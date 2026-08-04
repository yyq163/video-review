from __future__ import annotations

import subprocess
import sys

from backend.app import telemetry_metrics


def test_telemetry_metrics_import_does_not_import_main_or_routes() -> None:
    script = """
import sys
import backend.app.telemetry_metrics
blocked = {
    "backend.app.main",
    "backend.app.modules.review_http.upload_routes",
    "backend.app.modules.review_http.query_routes",
    "backend.app.modules.review_http.command_routes",
}
raise SystemExit(1 if blocked & sys.modules.keys() else 0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_telemetry_metric_label_sets_are_bounded() -> None:
    assert telemetry_metrics._bounded_label(
        "session_global",
        telemetry_metrics.UPLOAD_REJECTION_REASONS,
    ) == "session_global"
    assert telemetry_metrics._bounded_label(
        "project/user/file",
        telemetry_metrics.UPLOAD_REJECTION_REASONS,
    ) == "other"
    assert telemetry_metrics._bounded_label(
        "ready",
        telemetry_metrics.TASK_OUTCOMES,
    ) == "ready"
    assert "MEDIA_THUMBNAIL_SIGNATURE_INVALID" in telemetry_metrics.MEDIA_FAILURE_CODES


def test_telemetry_failure_never_blocks_business_callers(monkeypatch) -> None:
    def fail_labels(**_labels):
        raise RuntimeError("metrics registry unavailable")

    monkeypatch.setattr(telemetry_metrics.UPLOAD_REJECTIONS, "labels", fail_labels)
    telemetry_metrics.observe_upload_rejection("session_global")


def test_worker_metrics_server_uses_only_fixed_management_ports(monkeypatch) -> None:
    calls: list[tuple[int, str]] = []

    def record_server(*, port: int, addr: str):
        calls.append((port, addr))
        return object()

    monkeypatch.setattr(telemetry_metrics, "start_http_server", record_server)
    monkeypatch.setenv("WORKER_METRICS_BIND_HOST", "10.231.58.12")
    telemetry_metrics.start_worker_metrics_server(9101)
    telemetry_metrics.start_worker_metrics_server(9102)
    telemetry_metrics.start_worker_metrics_server(8000)
    assert calls == [(9101, "10.231.58.12"), (9102, "10.231.58.12")]


def test_worker_metrics_server_rejects_missing_wildcard_and_public_bind_hosts(
    monkeypatch,
) -> None:
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        telemetry_metrics,
        "start_http_server",
        lambda *, port, addr: calls.append((port, addr)),
    )
    for value in ("", "0.0.0.0", "127.0.0.1", "8.8.8.8"):
        monkeypatch.setenv("WORKER_METRICS_BIND_HOST", value)
        telemetry_metrics.start_worker_metrics_server(9101)
    assert calls == []


def test_worker_metrics_server_failure_is_best_effort(monkeypatch) -> None:
    def fail_server(*, port: int, addr: str):
        raise OSError(f"cannot bind {addr}:{port}")

    monkeypatch.setattr(telemetry_metrics, "start_http_server", fail_server)
    monkeypatch.setenv("WORKER_METRICS_BIND_HOST", "10.231.58.12")
    telemetry_metrics.start_worker_metrics_server(9101)
