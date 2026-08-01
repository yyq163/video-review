from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, MutableMapping
from ipaddress import ip_address, ip_network
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, REGISTRY, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from backend.app.main import app as application
from backend.app.modules.final_cut_review.infra.database import SessionLocal
from backend.app.settings import get_settings
from backend.app.telemetry_metrics import (
    MEDIA_FAILURE_CODES,
    MEDIA_KINDS,
    PACKAGE_FAILURE_CODES,
)

logger = logging.getLogger(__name__)

METRICS_PATH = "/internal/metrics"
ALLOWED_HTTP_METHODS = frozenset({"get", "head", "post", "put", "patch", "delete", "options"})
ROUTE_FAMILIES = frozenset(
    {
        "health",
        "project_list",
        "project_detail",
        "project_aggregate",
        "project_items",
        "item_detail",
        "versions",
        "version_detail",
        "issues",
        "issue_detail",
        "issue_revisions",
        "issue_messages",
        "finalization",
        "stream",
        "thumbnail",
        "original_download",
        "package",
        "package_download_session",
        "package_download",
        "upload_init",
        "upload_part",
        "upload_status",
        "upload_complete",
        "upload_abort",
        "command",
        "other",
    }
)
UPLOAD_STATUSES = frozenset({"initiated", "receiving", "finalizing", "completed", "aborted"})
MEDIA_STATUSES = frozenset({"queued", "running", "ready", "failed"})
PACKAGE_STATUSES = frozenset({"preparing", "ready", "failed", "expired", "invalidated"})
UPLOAD_OPERATIONS = frozenset({"init", "part", "status", "complete", "abort"})
OUTCOMES = frozenset({"success", "client_error", "server_error", "timeout", "cancelled"})
ROUTE_PATTERNS = (
    (re.compile(r"^/(?:runtimez|healthz)$"), "health"),
    (re.compile(r"^/projects$"), "project_list"),
    (re.compile(r"^/projects/[^/]+$"), "project_detail"),
    (re.compile(r"^/projects/[^/]+/summary$"), "project_aggregate"),
    (re.compile(r"^/projects/[^/]+/items$"), "project_items"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+$"), "item_detail"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions$"), "versions"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+$"), "version_detail"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/issues$"), "issues"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/issues/[^/]+$"), "issue_detail"),
    (
        re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/issues/[^/]+/revisions$"),
        "issue_revisions",
    ),
    (
        re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/issues/[^/]+/messages$"),
        "issue_messages",
    ),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/finalization$"), "finalization"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/stream$"), "stream"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/versions/[^/]+/thumbnail$"), "thumbnail"),
    (re.compile(r"^/projects/[^/]+/items/[^/]+/finalized-original/download$"), "original_download"),
    (re.compile(r"^/review/projects/[^/]+/finalized-originals/packages/[^/]+$"), "package"),
    (
        re.compile(r"^/review/projects/[^/]+/finalized-originals/packages/[^/]+/download-session$"),
        "package_download_session",
    ),
    (
        re.compile(r"^/review/projects/[^/]+/finalized-originals/packages/[^/]+/download$"),
        "package_download",
    ),
    (re.compile(r"^/uploads/init$"), "upload_init"),
    (re.compile(r"^/uploads/[^/]+/parts/[^/]+$"), "upload_part"),
    (re.compile(r"^/uploads/[^/]+$"), "upload_status"),
    (re.compile(r"^/uploads/[^/]+/complete$"), "upload_complete"),
    (re.compile(r"^/uploads/[^/]+/abort$"), "upload_abort"),
)


def _metrics_source_allowed(scope: Mapping[str, Any]) -> bool:
    configured_cidr = os.environ.get("MANAGEMENT_NETWORK_CIDR", "").strip()
    client = scope.get("client")
    if not configured_cidr or not isinstance(client, (tuple, list)) or not client:
        return False
    try:
        network = ip_network(configured_cidr, strict=True)
        source = ip_address(str(client[0]))
    except ValueError:
        return False
    return bool(
        network.is_private
        and not network.is_loopback
        and source.version == network.version
        and source in network
    )

HTTP_REQUESTS = Counter(
    "fcr_http_requests_total",
    "Completed backend HTTP requests grouped only by a fixed route family.",
    ("route_family", "method", "status"),
)
HTTP_DURATION = Histogram(
    "fcr_http_request_duration_seconds",
    "Backend HTTP request duration without path, principal, project, or file labels.",
    ("route_family", "method"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
HTTP_INFLIGHT = Gauge(
    "fcr_http_requests_inflight",
    "Backend HTTP requests currently in flight.",
    ("route_family", "method"),
)
HTTP_TTFB = Histogram(
    "fcr_http_first_response_seconds",
    "Time to the first response body chunk, grouped only by a fixed route family.",
    ("route_family",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
HTTP_RESPONSE_BYTES = Counter(
    "fcr_http_response_bytes_total",
    "Backend response-body bytes grouped only by fixed route family and status.",
    ("route_family", "status"),
)
HTTP_CANCELLED = Counter(
    "fcr_http_cancelled_total",
    "Cancelled backend requests grouped only by a fixed route family.",
    ("route_family",),
)
WORKSPACE_REFRESH = Counter(
    "fcr_workspace_refresh_requests_total",
    "Workspace aggregate/detail refreshes; no project identifier is recorded.",
    ("route_family", "status"),
)
STREAM_AUTHORIZATION = Counter(
    "fcr_stream_authorization_total",
    "FastAPI stream authorization outcomes before X-Accel handoff.",
    ("status", "range_requested"),
)
UPLOAD_REQUESTS = Counter(
    "fcr_upload_requests_total",
    "Upload request outcomes grouped only by fixed operation and bounded outcome.",
    ("operation", "outcome"),
)

UPLOAD_AGGREGATES_SQL = text(
    """
    SELECT status, count(*)::bigint AS session_count,
           coalesce(sum(reserved_bytes), 0)::bigint AS reserved_bytes
    FROM upload_sessions
    WHERE parts_cleanup_confirmed_at IS NULL
    GROUP BY status
    """
)
UPLOAD_CLEANUP_SQL = text(
    """
    SELECT count(*)::bigint
    FROM upload_sessions
    WHERE status IN ('completed', 'aborted')
      AND parts_cleanup_confirmed_at IS NULL
    """
)
MEDIA_AGGREGATES_SQL = text(
    """
    SELECT kind, status, count(*)::bigint,
           coalesce(sum(attempts), 0)::bigint,
           coalesce(max(
               CASE WHEN status IN ('queued', 'running')
                    THEN extract(epoch FROM (current_timestamp - created_at))
                    ELSE 0 END
           ), 0)::double precision
    FROM media_derivative_tasks
    GROUP BY kind, status
    """
)
PACKAGE_AGGREGATES_SQL = text(
    """
    SELECT status, count(*)::bigint,
           coalesce(sum(storage_bytes), 0)::bigint,
           coalesce(sum(build_attempts), 0)::bigint,
           coalesce(max(
               CASE WHEN status = 'preparing'
                    THEN extract(epoch FROM (current_timestamp - created_at))
                    ELSE 0 END
           ), 0)::double precision
    FROM package_snapshots
    GROUP BY status
    """
)
PG_STATEMENTS_SQL = text(
    """
    SELECT calls, total_exec_time_ms, rows,
           shared_blocks_hit, shared_blocks_read, temp_blocks_written
    FROM fcr_observability.fcr_pg_stat_summary()
    """
)
PG_STATEMENT_HOTSPOTS_SQL = text(
    """
    SELECT criterion, rank, queryid, calls, total_exec_time_ms, rows,
           shared_blocks_hit, shared_blocks_read,
           shared_blocks_dirtied, shared_blocks_written,
           temp_blocks_read, temp_blocks_written
    FROM fcr_observability.fcr_pg_stat_hotspots(5)
    """
)
UPLOAD_RESERVATION_SCOPE_SQL = text(
    """
    WITH active AS (
        SELECT owner_principal_kind, owner_principal_id, reserved_bytes
        FROM upload_sessions
        WHERE parts_cleanup_confirmed_at IS NULL
    ),
    per_principal AS (
        SELECT owner_principal_kind, owner_principal_id,
               count(*)::bigint AS session_count,
               coalesce(sum(reserved_bytes), 0)::bigint AS reserved_bytes
        FROM active
        GROUP BY owner_principal_kind, owner_principal_id
    )
    SELECT
        (SELECT count(*)::bigint FROM active),
        (SELECT coalesce(sum(reserved_bytes), 0)::bigint FROM active),
        coalesce((SELECT max(session_count) FROM per_principal), 0)::bigint,
        coalesce((SELECT max(reserved_bytes) FROM per_principal), 0)::bigint
    """
)
PACKAGE_RESERVATION_SQL = text(
    """
    SELECT coalesce(sum(storage_bytes), 0)::bigint
    FROM package_snapshots
    WHERE status = 'preparing' AND storage_reclaimed_at IS NULL
    """
)


def _queryid_words(queryid: int) -> tuple[int, int]:
    unsigned = queryid & ((1 << 64) - 1)
    return unsigned >> 32, unsigned & 0xFFFFFFFF
MEDIA_FAILURE_SQL = text(
    """
    SELECT error_code, count(*)::bigint
    FROM media_derivative_tasks
    WHERE status = 'failed' AND error_code IS NOT NULL
    GROUP BY error_code
    """
)
PACKAGE_FAILURE_SQL = text(
    """
    SELECT failure_details ->> 'error_code' AS error_code, count(*)::bigint
    FROM package_snapshots
    WHERE failure_details ->> 'error_code' IS NOT NULL
    GROUP BY failure_details ->> 'error_code'
    """
)


def _bounded_label(value: object, allowed: frozenset[str]) -> str:
    normalized = str(value).lower()
    return normalized if normalized in allowed else "other"


def _http_method(scope: Mapping[str, Any]) -> str:
    return _bounded_label(scope.get("method", ""), ALLOWED_HTTP_METHODS)


def _route_family(path: object) -> str:
    normalized = str(path)
    for prefix in (
        "/api/v1/final-cut-review/files",
        "/api/v1/final-cut-review",
        "/api/v1/files",
    ):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :] or "/"
            break
    for pattern, family in ROUTE_PATTERNS:
        if pattern.fullmatch(normalized):
            return family
    if normalized.startswith(("/edit/", "/review/")):
        return "command"
    return "other"


def _request_has_range(scope: Mapping[str, Any]) -> str:
    for name, _value in scope.get("headers", ()):
        if bytes(name).lower() == b"range":
            return "true"
    return "false"


def _outcome(status: int, *, cancelled: bool = False) -> str:
    if cancelled:
        return "cancelled"
    if status in {408, 504}:
        return "timeout"
    if 200 <= status < 400:
        return "success"
    if 400 <= status < 500:
        return "client_error"
    return "server_error"


def _best_effort(operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception:
        logger.warning("observability update failed without affecting the request", exc_info=True)


def _bounded_staging_totals(directory: str, kind: str) -> tuple[int, int]:
    count = 0
    total_bytes = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if count >= 10_000:
                raise RuntimeError("staging scan exceeded the bounded entry limit")
            if kind == "package":
                matches = entry.name.endswith(".staging.zip")
            else:
                matches = (
                    entry.name.startswith(".")
                    and ".staging" in entry.name
                    and entry.name.endswith(".tmp")
                )
            if not matches or not entry.is_file(follow_symlinks=False):
                continue
            metadata = entry.stat(follow_symlinks=False)
            count += 1
            total_bytes += metadata.st_size
    return count, total_bytes


class DatabaseAggregateCollector:
    """Expose bounded database aggregates without query text or business identifiers."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        success = GaugeMetricFamily(
            "fcr_observability_collection_success",
            "Whether the most recent bounded database collection completed.",
            labels=["collector"],
        )
        try:
            with SessionLocal() as session:
                if session.get_bind().dialect.name != "postgresql":
                    raise RuntimeError("database aggregates require PostgreSQL")
                session.execute(text("SET LOCAL statement_timeout = '1000ms'"))

                upload_sessions = GaugeMetricFamily(
                    "fcr_upload_sessions",
                    "Upload sessions retaining reservations, grouped only by bounded status.",
                    labels=["status"],
                )
                upload_reserved = GaugeMetricFamily(
                    "fcr_upload_reserved_bytes",
                    "Reserved upload bytes grouped only by bounded status.",
                    labels=["status"],
                )
                for status, session_count, reserved_bytes in session.execute(UPLOAD_AGGREGATES_SQL):
                    label = _bounded_label(status, UPLOAD_STATUSES)
                    upload_sessions.add_metric([label], int(session_count))
                    upload_reserved.add_metric([label], int(reserved_bytes))

                upload_cleanup = GaugeMetricFamily(
                    "fcr_upload_cleanup_pending",
                    "Terminal upload sessions whose parts cleanup is not confirmed.",
                )
                upload_cleanup.add_metric([], int(session.execute(UPLOAD_CLEANUP_SQL).scalar_one()))

                reservation_values = session.execute(UPLOAD_RESERVATION_SCOPE_SQL).one()
                upload_scope_sessions = GaugeMetricFamily(
                    "fcr_upload_reservation_sessions",
                    "Active reservation session count; max_principal has no principal label.",
                    labels=["scope"],
                )
                upload_scope_bytes = GaugeMetricFamily(
                    "fcr_upload_reservation_bytes",
                    "Active reserved bytes; max_principal has no principal label.",
                    labels=["scope"],
                )
                upload_scope_sessions.add_metric(["global"], int(reservation_values[0]))
                upload_scope_bytes.add_metric(["global"], int(reservation_values[1]))
                upload_scope_sessions.add_metric(["max_principal"], int(reservation_values[2]))
                upload_scope_bytes.add_metric(["max_principal"], int(reservation_values[3]))

                package_reservation = GaugeMetricFamily(
                    "fcr_package_reservation_bytes",
                    "Package storage reserved by preparing snapshots.",
                )
                package_reservation.add_metric(
                    [],
                    int(session.execute(PACKAGE_RESERVATION_SQL).scalar_one()),
                )
                runtime_settings = get_settings()
                upload_limits = GaugeMetricFamily(
                    "fcr_upload_admission_limit",
                    "Configured upload admission limits; scope and resource are fixed labels.",
                    labels=["scope", "resource"],
                )
                upload_limits.add_metric(
                    ["global", "sessions"],
                    runtime_settings.max_active_upload_sessions_global,
                )
                upload_limits.add_metric(
                    ["max_principal", "sessions"],
                    runtime_settings.max_active_upload_sessions_per_principal,
                )
                upload_limits.add_metric(
                    ["global", "reserved_bytes"],
                    runtime_settings.max_reserved_upload_bytes_global,
                )
                upload_limits.add_metric(
                    ["max_principal", "reserved_bytes"],
                    runtime_settings.max_reserved_upload_bytes_per_principal,
                )
                storage_free = GaugeMetricFamily(
                    "fcr_storage_free_bytes",
                    "Free bytes on the managed storage filesystem; no path label is exposed.",
                )
                storage_free.add_metric([], shutil.disk_usage(runtime_settings.storage_root).free)
                storage_low_watermark = GaugeMetricFamily(
                    "fcr_upload_storage_low_watermark_bytes",
                    "Configured upload low-watermark bytes.",
                )
                storage_low_watermark.add_metric(
                    [],
                    runtime_settings.upload_storage_low_watermark_bytes,
                )
                staging_files = GaugeMetricFamily(
                    "fcr_staging_files",
                    "Bounded staging-file count grouped only by worker kind.",
                    labels=["worker"],
                )
                staging_bytes = GaugeMetricFamily(
                    "fcr_staging_bytes",
                    "Bounded staging bytes grouped only by worker kind.",
                    labels=["worker"],
                )
                try:
                    package_staging = _bounded_staging_totals(
                        str(runtime_settings.package_root),
                        "package",
                    )
                    media_staging = _bounded_staging_totals(
                        str(runtime_settings.storage_root / "files"),
                        "media",
                    )
                    for worker, totals in (
                        ("package", package_staging),
                        ("media", media_staging),
                    ):
                        staging_files.add_metric([worker], totals[0])
                        staging_bytes.add_metric([worker], totals[1])
                    success.add_metric(["staging"], 1)
                except (OSError, RuntimeError):
                    logger.warning(
                        "staging observability degraded without affecting business traffic",
                        exc_info=True,
                    )
                    success.add_metric(["staging"], 0)

                media_tasks = GaugeMetricFamily(
                    "fcr_media_derivative_tasks",
                    "Media derivative tasks grouped only by bounded kind and status.",
                    labels=["kind", "status"],
                )
                media_attempts = GaugeMetricFamily(
                    "fcr_media_derivative_attempts",
                    "Media task attempts grouped only by bounded kind and status.",
                    labels=["kind", "status"],
                )
                media_backlog_age = GaugeMetricFamily(
                    "fcr_media_derivative_backlog_age_seconds",
                    "Oldest queued/running media task age grouped only by bounded kind and status.",
                    labels=["kind", "status"],
                )
                for kind, status, task_count, attempts, backlog_age in session.execute(
                    MEDIA_AGGREGATES_SQL
                ):
                    kind_label = _bounded_label(kind, MEDIA_KINDS)
                    status_label = _bounded_label(status, MEDIA_STATUSES)
                    media_tasks.add_metric(
                        [kind_label, status_label],
                        int(task_count),
                    )
                    media_attempts.add_metric([kind_label, status_label], int(attempts))
                    media_backlog_age.add_metric(
                        [kind_label, status_label],
                        max(0.0, float(backlog_age)),
                    )

                media_failures = GaugeMetricFamily(
                    "fcr_media_derivative_failures",
                    "Current failed media tasks grouped by a fixed failure-code allowlist.",
                    labels=["failure_code"],
                )
                for failure_code, failure_count in session.execute(MEDIA_FAILURE_SQL):
                    bounded_code = (
                        str(failure_code) if str(failure_code) in MEDIA_FAILURE_CODES else "other"
                    )
                    media_failures.add_metric([bounded_code], int(failure_count))

                package_snapshots = GaugeMetricFamily(
                    "fcr_package_snapshots",
                    "Package snapshots grouped only by bounded status.",
                    labels=["status"],
                )
                package_storage = GaugeMetricFamily(
                    "fcr_package_storage_bytes",
                    "Package storage bytes grouped only by bounded status.",
                    labels=["status"],
                )
                package_attempts = GaugeMetricFamily(
                    "fcr_package_build_attempts",
                    "Package build attempts grouped only by bounded status.",
                    labels=["status"],
                )
                package_queue_age = GaugeMetricFamily(
                    "fcr_package_queue_age_seconds",
                    "Oldest preparing package age grouped only by bounded status.",
                    labels=["status"],
                )
                for status, snapshot_count, storage_bytes, attempts, queue_age in session.execute(
                    PACKAGE_AGGREGATES_SQL
                ):
                    label = _bounded_label(status, PACKAGE_STATUSES)
                    package_snapshots.add_metric([label], int(snapshot_count))
                    package_storage.add_metric([label], int(storage_bytes))
                    package_attempts.add_metric([label], int(attempts))
                    package_queue_age.add_metric([label], max(0.0, float(queue_age)))

                package_failures = GaugeMetricFamily(
                    "fcr_package_failures",
                    "Current package failures grouped by a fixed failure-code allowlist.",
                    labels=["failure_code"],
                )
                for failure_code, failure_count in session.execute(PACKAGE_FAILURE_SQL):
                    bounded_code = (
                        str(failure_code) if str(failure_code) in PACKAGE_FAILURE_CODES else "other"
                    )
                    package_failures.add_metric([bounded_code], int(failure_count))

                pg_stat_values = session.execute(PG_STATEMENTS_SQL).one()
                pg_stat_names = (
                    "calls",
                    "total_exec_time_ms",
                    "rows",
                    "shared_blocks_hit",
                    "shared_blocks_read",
                    "temp_blocks_written",
                )
                pg_stat_metrics: list[GaugeMetricFamily] = []
                for metric_name, value in zip(pg_stat_names, pg_stat_values, strict=True):
                    metric = GaugeMetricFamily(
                        f"fcr_pg_statements_{metric_name}",
                        "Aggregate pg_stat_statements value for the runtime role and current database.",
                    )
                    metric.add_metric([], float(value))
                    pg_stat_metrics.append(metric)

                pg_hotspot_metric_names = (
                    "queryid_high32",
                    "queryid_low32",
                    "calls",
                    "total_exec_time_ms",
                    "rows",
                    "shared_blocks_hit",
                    "shared_blocks_read",
                    "shared_blocks_dirtied",
                    "shared_blocks_written",
                    "temp_blocks_read",
                    "temp_blocks_written",
                )
                pg_hotspot_metrics = {
                    metric_name: GaugeMetricFamily(
                        f"fcr_pg_statements_hotspot_{metric_name}",
                        "Top-five pg_stat_statements hotspot without SQL text or identifier labels.",
                        labels=["criterion", "rank"],
                    )
                    for metric_name in pg_hotspot_metric_names
                }
                allowed_criteria = {"calls", "total_exec_time", "shared_io", "temp_io"}
                for row in session.execute(PG_STATEMENT_HOTSPOTS_SQL):
                    criterion = str(row[0])
                    rank = int(row[1])
                    if criterion not in allowed_criteria or not 1 <= rank <= 5:
                        continue
                    high32, low32 = _queryid_words(row[2])
                    values = (high32, low32, *row[3:])
                    for metric_name, value in zip(
                        pg_hotspot_metric_names,
                        values,
                        strict=True,
                    ):
                        pg_hotspot_metrics[metric_name].add_metric(
                            [criterion, str(rank)],
                            float(value),
                        )

            success.add_metric(["database"], 1)
            yield upload_sessions
            yield upload_reserved
            yield upload_cleanup
            yield upload_scope_sessions
            yield upload_scope_bytes
            yield package_reservation
            yield upload_limits
            yield storage_free
            yield storage_low_watermark
            yield staging_files
            yield staging_bytes
            yield media_tasks
            yield media_attempts
            yield media_backlog_age
            yield media_failures
            yield package_snapshots
            yield package_storage
            yield package_attempts
            yield package_queue_age
            yield package_failures
            yield from pg_stat_metrics
            yield from pg_hotspot_metrics.values()
        except Exception:
            logger.warning("database observability degraded without affecting business traffic", exc_info=True)
            success.add_metric(["database"], 0)
        yield success


REGISTRY.register(DatabaseAggregateCollector())
metrics_application = make_asgi_app(registry=REGISTRY)

ASGIReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
ASGISend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApplication = Callable[[MutableMapping[str, Any], ASGIReceive, ASGISend], Awaitable[None]]


class InstrumentedApplication:
    def __init__(self, downstream: ASGIApplication, metrics: ASGIApplication) -> None:
        self.downstream = downstream
        self.metrics = metrics

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.downstream(scope, receive, send)
            return
        if scope.get("path") == METRICS_PATH:
            if not _metrics_source_allowed(scope):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send({"type": "http.response.body", "body": b"not found\n"})
                return
            try:
                await self.metrics(scope, receive, send)
            except Exception:
                logger.warning("metrics rendering failed without affecting business traffic", exc_info=True)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send({"type": "http.response.body", "body": b"metrics temporarily unavailable\n"})
            return

        method = _http_method(scope)
        route_family = _route_family(scope.get("path", ""))
        range_requested = _request_has_range(scope)
        status = 500
        response_bytes = 0
        first_body_observed = False
        cancelled = False
        started_at = time.perf_counter()
        _best_effort(
            lambda: HTTP_INFLIGHT.labels(route_family=route_family, method=method).inc()
        )

        async def observe_send(message: MutableMapping[str, Any]) -> None:
            nonlocal first_body_observed, response_bytes, status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            if message.get("type") == "http.response.body":
                body = bytes(message.get("body", b""))
                response_bytes += len(body)
                if not first_body_observed and body:
                    first_body_observed = True
                    elapsed = max(0.0, time.perf_counter() - started_at)
                    _best_effort(
                        lambda: HTTP_TTFB.labels(route_family=route_family).observe(elapsed)
                    )
            await send(message)

        try:
            await self.downstream(scope, receive, observe_send)
        except asyncio.CancelledError:
            cancelled = True
            _best_effort(lambda: HTTP_CANCELLED.labels(route_family=route_family).inc())
            raise
        finally:
            duration = max(0.0, time.perf_counter() - started_at)
            status_label = str(status)
            _best_effort(
                lambda: HTTP_INFLIGHT.labels(route_family=route_family, method=method).dec()
            )
            _best_effort(
                lambda: HTTP_REQUESTS.labels(
                    route_family=route_family,
                    method=method,
                    status=status_label,
                ).inc()
            )
            _best_effort(
                lambda: HTTP_DURATION.labels(
                    route_family=route_family,
                    method=method,
                ).observe(duration)
            )
            _best_effort(
                lambda: HTTP_RESPONSE_BYTES.labels(
                    route_family=route_family,
                    status=status_label,
                ).inc(response_bytes)
            )
            if method == "get" and route_family in {
                "project_detail",
                "project_aggregate",
                "project_items",
            }:
                _best_effort(
                    lambda: WORKSPACE_REFRESH.labels(
                        route_family=route_family,
                        status=status_label,
                    ).inc()
                )
            if route_family == "stream":
                _best_effort(
                    lambda: STREAM_AUTHORIZATION.labels(
                        status=status_label,
                        range_requested=range_requested,
                    ).inc()
                )
            if route_family.startswith("upload_"):
                operation = route_family.removeprefix("upload_")
                _best_effort(
                    lambda: UPLOAD_REQUESTS.labels(
                        operation=_bounded_label(operation, UPLOAD_OPERATIONS),
                        outcome=_outcome(status, cancelled=cancelled),
                    ).inc()
                )


app = InstrumentedApplication(application, metrics_application)


async def _empty_receive() -> MutableMapping[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard_send(_message: MutableMapping[str, Any]) -> None:
    return None


async def readiness_probe() -> AsyncIterator[None]:
    """Small import-time probe target for static validation; it performs no I/O."""
    yield
