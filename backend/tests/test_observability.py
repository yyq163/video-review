from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from backend.app import observability


def test_labels_are_bounded() -> None:
    assert observability._bounded_label("ready", observability.PACKAGE_STATUSES) == "ready"
    assert observability._bounded_label("project/customer/file", observability.PACKAGE_STATUSES) == "other"
    assert observability._http_method({"method": "GET"}) == "get"
    assert observability._http_method({"method": "USER-CONTROLLED"}) == "other"


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


def test_pg_statements_query_does_not_select_query_text_or_query_id() -> None:
    statement = str(observability.PG_STATEMENTS_SQL).lower()
    assert "queryid" not in statement
    assert " query," not in statement
    assert " query " not in statement
    assert "fcr_pg_stat_summary" in statement


def test_metrics_path_bypasses_business_application(monkeypatch) -> None:
    downstream_called = False
    messages: list[dict[str, Any]] = []

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        _send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"fcr_http_requests_total 1\n"})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
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


def test_metrics_failure_returns_bounded_503_without_calling_business_application(
    monkeypatch,
) -> None:
    downstream_called = False
    messages: list[dict[str, Any]] = []

    async def downstream(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        _send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(
        _scope: dict[str, Any],
        _receive: Callable[[], Awaitable[dict[str, Any]]],
        _send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        raise RuntimeError("sensitive internal failure")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
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
    messages: list[dict[str, Any]] = []

    async def downstream(*_args: Any) -> None:
        nonlocal downstream_called
        downstream_called = True

    async def metrics(*_args: Any) -> None:
        nonlocal metrics_called
        metrics_called = True

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
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
