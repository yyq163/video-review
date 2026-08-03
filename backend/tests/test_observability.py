from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import yaml

from backend.app import observability


def test_labels_are_bounded() -> None:
    assert observability._bounded_label("ready", observability.PACKAGE_STATUSES) == "ready"
    assert observability._bounded_label("project/customer/file", observability.PACKAGE_STATUSES) == "other"
    assert observability._http_method({"method": "GET"}) == "get"
    assert observability._http_method({"method": "USER-CONTROLLED"}) == "other"


def test_staging_scan_budget_counts_every_directory_entry(monkeypatch) -> None:
    class Entry:
        name = "ordinary-media.mp4"

        def is_file(self, *, follow_symlinks: bool) -> bool:
            del follow_symlinks
            return True

    class Scan:
        def __enter__(self):
            return iter(Entry() for _ in range(10_001))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(observability.os, "scandir", lambda _directory: Scan())

    try:
        observability._bounded_staging_totals("/managed/files", "media")
    except RuntimeError as exc:
        assert "bounded entry limit" in str(exc)
    else:
        raise AssertionError("ordinary files must consume the staging scan budget")


def test_route_family_never_uses_project_item_version_or_upload_identifiers() -> None:
    examples = {
        "/api/v1/final-cut-review/projects/project-secret/summary": "project_aggregate",
        "/api/v1/final-cut-review/projects/project-secret/items": "project_items",
        (
            "/api/v1/final-cut-review/projects/project-secret/items/item-secret/"
            "versions/version-secret/stream"
        ): "stream",
        "/api/v1/files/uploads/upload-secret/parts/42": "upload_part",
        "/api/v1/files/uploads/upload-secret/complete": "upload_complete",
        "/api/v1/files/uploads/init": "upload_init",
        "/api/v1/files/uploads/upload-secret/abort": "upload_abort",
        "/api/v1/final-cut-review/files/uploads/upload-secret": "upload_status",
    }
    for path, expected in examples.items():
        family = observability._route_family(path)
        assert family == expected
        assert "secret" not in family
        assert family in observability.ROUTE_FAMILIES


def test_request_range_and_outcomes_are_bounded() -> None:
    assert observability._request_has_range({"headers": [(b"range", b"bytes=1-2")]}) == "true"
    assert observability._request_has_range({"headers": [(b"x-user", b"range")]}) == "false"
    assert observability._outcome(206) == "success"
    assert observability._outcome(429) == "client_error"
    assert observability._outcome(504) == "timeout"
    assert observability._outcome(200, cancelled=True) == "cancelled"


def test_pg_statements_queries_expose_bounded_hotspots_without_query_text() -> None:
    aggregate = str(observability.PG_STATEMENTS_SQL).lower()
    hotspots = str(observability.PG_STATEMENT_HOTSPOTS_SQL).lower()
    assert "fcr_pg_stat_summary" in aggregate
    assert "fcr_pg_stat_hotspots(5)" in hotspots
    assert "queryid" in hotspots
    for statement in (aggregate, hotspots):
        assert " query," not in statement
        assert " query " not in statement


def test_cadvisor_uses_stable_compose_service_labels_and_drops_runtime_identity() -> None:
    prometheus_path = (
        Path(__file__).resolve().parents[2]
        / "ops"
        / "prometheus"
        / "prometheus.yml"
    )
    configuration = yaml.safe_load(prometheus_path.read_text(encoding="utf-8"))
    job = next(
        item
        for item in configuration["scrape_configs"]
        if item["job_name"] == "fj-final-cut-review-cadvisor"
    )
    relabels = job["metric_relabel_configs"]
    keep, replace, labeldrop = relabels
    service_label = "container_label_com_docker_compose_service"
    assert keep == {
        "source_labels": [service_label],
        "regex": "backend|package-worker|media-worker|postgres",
        "action": "keep",
    }
    assert replace == {
        "source_labels": [service_label],
        "regex": "(backend|package-worker|media-worker|postgres)",
        "target_label": "service",
        "replacement": "$1",
        "action": "replace",
    }
    assert labeldrop["action"] == "labeldrop"
    assert set(labeldrop["regex"].split("|")) == {
        "container_label_.*",
        "id",
        "image",
        "name",
    }


def test_shared_io_hotspot_ranking_excludes_cache_hits() -> None:
    bootstrap = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "bootstrap_database_roles.py"
    ).read_text(encoding="utf-8")
    shared_io_rank = bootstrap.split("SELECT 'shared_io'::text", 1)[1].split(
        "UNION ALL", 1
    )[0]
    assert "shared_blks_read" in shared_io_rank
    assert "shared_blks_written" in shared_io_rank
    assert "shared_blks_hit" not in shared_io_rank
    assert "shared_blks_dirtied" not in shared_io_rank


def test_queryid_is_exported_as_two_exact_numeric_words_without_a_label() -> None:
    assert observability._queryid_words(0) == (0, 0)
    assert observability._queryid_words(0x12345678ABCDEF01) == (
        0x12345678,
        0xABCDEF01,
    )
    assert observability._queryid_words(-1) == (0xFFFFFFFF, 0xFFFFFFFF)


def test_metrics_path_bypasses_business_application(monkeypatch) -> None:
    downstream_called = False
    messages: list[MutableMapping[str, Any]] = []

    async def downstream(
        _scope: MutableMapping[str, Any],
        _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        _send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(
        _scope: MutableMapping[str, Any],
        _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"fcr_http_requests_total 1\n"})

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    application = observability.InstrumentedApplication(downstream, metrics)
    monkeypatch.setenv("MANAGEMENT_NETWORK_CIDR", "10.231.58.0/24")
    asyncio.run(
        application(
            {
                "type": "http",
                "path": observability.METRICS_PATH,
                "method": "GET",
                "client": ("10.231.58.20", 43120),
            },
            receive,
            send,
        )
    )

    assert downstream_called is False
    assert messages[0]["status"] == 200


def test_metrics_rendering_does_not_block_business_event_loop(monkeypatch) -> None:
    render_started = threading.Event()
    release_render = threading.Event()
    business_completed = False

    def slow_render(_registry: object) -> bytes:
        render_started.set()
        assert release_render.wait(timeout=2)
        return b"fcr_runtime 1\n"

    monkeypatch.setattr(observability, "generate_latest", slow_render)

    async def scenario() -> None:
        nonlocal business_completed
        messages: list[MutableMapping[str, Any]] = []

        async def downstream(
            _scope: MutableMapping[str, Any],
            _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
            send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
        ) -> None:
            nonlocal business_completed
            business_completed = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive() -> MutableMapping[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: MutableMapping[str, Any]) -> None:
            messages.append(message)

        application = observability.InstrumentedApplication(
            downstream,
            observability.IsolatedMetricsApplication(),
        )
        monkeypatch.setenv("MANAGEMENT_NETWORK_CIDR", "10.231.58.0/24")
        metrics_task = asyncio.create_task(
            application(
                {
                    "type": "http",
                    "path": observability.METRICS_PATH,
                    "method": "GET",
                    "client": ("10.231.58.20", 43120),
                },
                receive,
                send,
            )
        )
        for _ in range(100):
            if render_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert render_started.is_set()
        await application(
            {
                "type": "http",
                "path": "/projects",
                "method": "GET",
                "client": ("127.0.0.1", 43121),
            },
            receive,
            send,
        )
        assert business_completed is True
        release_render.set()
        await metrics_task

    asyncio.run(scenario())


def test_metrics_failure_returns_bounded_503_without_calling_business_application(
    monkeypatch,
) -> None:
    downstream_called = False
    messages: list[MutableMapping[str, Any]] = []

    async def downstream(
        _scope: MutableMapping[str, Any],
        _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        _send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(
        _scope: MutableMapping[str, Any],
        _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        _send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        raise RuntimeError("sensitive internal failure")

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    application = observability.InstrumentedApplication(downstream, metrics)
    monkeypatch.setenv("MANAGEMENT_NETWORK_CIDR", "10.231.58.0/24")
    asyncio.run(
        application(
            {
                "type": "http",
                "path": observability.METRICS_PATH,
                "method": "GET",
                "client": ("10.231.58.20", 43120),
            },
            receive,
            send,
        )
    )

    assert downstream_called is False
    assert messages[0]["status"] == 503
    assert b"sensitive" not in messages[1]["body"]


def test_metrics_path_is_hidden_outside_management_network(monkeypatch) -> None:
    monkeypatch.setenv("MANAGEMENT_NETWORK_CIDR", "10.231.58.0/24")
    downstream_called = False
    metrics_called = False
    messages: list[MutableMapping[str, Any]] = []

    async def downstream(*_args: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(*_args: Any) -> None:
        nonlocal metrics_called
        metrics_called = True

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        messages.append(message)

    application = observability.InstrumentedApplication(downstream, metrics)
    asyncio.run(
        application(
            {
                "type": "http",
                "path": observability.METRICS_PATH,
                "method": "GET",
                "client": ("10.231.57.9", 43120),
            },
            receive,
            send,
        )
    )
    assert downstream_called is False
    assert metrics_called is False
    assert messages[0]["status"] == 404
