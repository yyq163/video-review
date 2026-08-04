from __future__ import annotations

import logging
import os
from ipaddress import ip_address
from collections.abc import Callable

from prometheus_client import Counter, Histogram, start_http_server

LOGGER = logging.getLogger(__name__)

UPLOAD_REJECTION_REASONS = frozenset(
    {
        "session_global",
        "session_principal",
        "reservation_global",
        "reservation_principal",
        "cleanup_pending",
        "storage_low_watermark",
        "storage_unavailable",
        "file_too_large",
        "invalid_request",
        "other",
    }
)
TASK_OUTCOMES = frozenset(
    {"ready", "failed", "retry", "queued", "skipped", "timeout", "cancelled"}
)
MEDIA_KINDS = frozenset({"playback_faststart", "thumbnail"})
WORKER_METRICS_PORTS = frozenset({9101, 9102})
PACKAGE_FAILURE_CODES = frozenset(
    {
        "PACKAGE_BUILD_FAILED",
        "PACKAGE_BUILD_INTERRUPTED",
        "PACKAGE_BUILD_RETRY_SCHEDULED",
        "PACKAGE_BUILD_TIMEOUT",
        "PACKAGE_SOURCE_MISSING",
        "STORAGE_UNAVAILABLE",
        "FILE_TOO_LARGE",
    }
)
MEDIA_FAILURE_CODES = frozenset(
    {
        "MEDIA_DERIVATIVE_ATTEMPTS_EXHAUSTED",
        "MEDIA_DERIVATIVE_FAILED",
        "MEDIA_DERIVATIVE_FILE_ID_CONFLICT",
        "MEDIA_DERIVATIVE_KIND_INVALID",
        "MEDIA_DERIVATIVE_LEASE_LOST",
        "MEDIA_DERIVATIVE_OUTPUT_INVALID",
        "MEDIA_DERIVATIVE_PROBE_MISMATCH",
        "MEDIA_DERIVATIVE_PUBLISH_CHANGED",
        "MEDIA_DERIVATIVE_PUBLISH_COLLISION",
        "MEDIA_DERIVATIVE_PUBLISH_FAILED",
        "MEDIA_DERIVATIVE_SOURCE_CHANGED",
        "MEDIA_DERIVATIVE_SOURCE_HASH_MISMATCH",
        "MEDIA_DERIVATIVE_SOURCE_METADATA_MISMATCH",
        "MEDIA_DERIVATIVE_SOURCE_MISSING",
        "MEDIA_DERIVATIVE_SOURCE_PATH_INVALID",
        "MEDIA_DERIVATIVE_SOURCE_PROBE_FAILED",
        "MEDIA_DERIVATIVE_STAGING_IDENTITY_CHANGED",
        "MEDIA_DERIVATIVE_STAGING_UNSAFE",
        "MEDIA_DERIVATIVE_STORAGE_FAILED",
        "MEDIA_DERIVATIVE_STREAM_MISMATCH",
        "MEDIA_DERIVATIVE_VERSION_NOT_CURRENT",
        "MEDIA_DERIVATIVE_VERSION_MISSING",
        "MEDIA_FASTSTART_INVALID",
        "MEDIA_THUMBNAIL_DIMENSIONS_INVALID",
        "MEDIA_THUMBNAIL_INVALID",
        "MEDIA_THUMBNAIL_SIGNATURE_INVALID",
        "MEDIA_TRANSFORM_FAILED",
        "MEDIA_TRANSFORM_OUTPUT_UNAVAILABLE",
        "MEDIA_TRANSFORM_OUTPUT_LIMIT",
        "MEDIA_TRANSFORM_TERMINATION_FAILED",
        "MEDIA_TRANSFORM_TIMEOUT",
        "MEDIA_TRANSFORM_UNAVAILABLE",
    }
)

UPLOAD_REJECTIONS = Counter(
    "fcr_upload_rejections_total",
    "Upload rejection branches; callers must use the fixed reason set.",
    ("reason",),
)
PACKAGE_TASK_RESULTS = Counter(
    "fcr_package_task_results_total",
    "Package task outcomes with a bounded failure code.",
    ("outcome", "failure_code"),
)
PACKAGE_TASK_DURATION = Histogram(
    "fcr_package_task_duration_seconds",
    "Package task execution duration.",
    ("outcome",),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 1800, 3600, 7200),
)
MEDIA_TASK_RESULTS = Counter(
    "fcr_media_task_results_total",
    "Media task outcomes with bounded kind and failure code.",
    ("kind", "outcome", "failure_code"),
)
MEDIA_TASK_DURATION = Histogram(
    "fcr_media_task_duration_seconds",
    "Media task execution duration.",
    ("kind", "outcome"),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 1800, 3600),
)


def _best_effort(operation: Callable[[], object]) -> None:
    try:
        operation()
    except Exception:
        LOGGER.warning(
            "telemetry_update_failed_without_affecting_business",
            exc_info=True,
        )


def _bounded_label(value: object, allowed: frozenset[str]) -> str:
    normalized = str(value).lower()
    return normalized if normalized in allowed else "other"


def start_worker_metrics_server(port: int) -> None:
    if port not in WORKER_METRICS_PORTS:
        LOGGER.error(
            "worker_metrics_port_rejected",
            extra={"port": port},
        )
        return
    bind_host = os.environ.get("WORKER_METRICS_BIND_HOST", "").strip()
    try:
        address = ip_address(bind_host)
    except ValueError:
        LOGGER.error("worker_metrics_bind_host_rejected")
        return
    if (
        not address.is_private
        or address.is_loopback
        or address.is_unspecified
        or address.is_multicast
    ):
        LOGGER.error("worker_metrics_bind_host_rejected")
        return
    _best_effort(
        lambda: start_http_server(
            port=port,
            addr=bind_host,
        )
    )


def observe_upload_rejection(reason: str) -> None:
    bounded_reason = _bounded_label(reason, UPLOAD_REJECTION_REASONS)
    _best_effort(
        lambda: UPLOAD_REJECTIONS.labels(reason=bounded_reason).inc()
    )


def observe_package_task(
    outcome: str,
    duration_seconds: float,
    failure_code: str = "",
) -> None:
    bounded_outcome = _bounded_label(outcome, TASK_OUTCOMES)
    bounded_code = failure_code if failure_code in PACKAGE_FAILURE_CODES else "other"
    bounded_duration = max(0.0, duration_seconds)
    _best_effort(
        lambda: PACKAGE_TASK_RESULTS.labels(
            outcome=bounded_outcome,
            failure_code=bounded_code,
        ).inc()
    )
    _best_effort(
        lambda: PACKAGE_TASK_DURATION.labels(outcome=bounded_outcome).observe(
            bounded_duration
        )
    )


def observe_media_task(
    kind: str,
    outcome: str,
    duration_seconds: float,
    failure_code: str = "",
) -> None:
    bounded_kind = _bounded_label(kind, MEDIA_KINDS)
    bounded_outcome = _bounded_label(outcome, TASK_OUTCOMES)
    bounded_code = failure_code if failure_code in MEDIA_FAILURE_CODES else "other"
    bounded_duration = max(0.0, duration_seconds)
    _best_effort(
        lambda: MEDIA_TASK_RESULTS.labels(
            kind=bounded_kind,
            outcome=bounded_outcome,
            failure_code=bounded_code,
        ).inc()
    )
    _best_effort(
        lambda: MEDIA_TASK_DURATION.labels(
            kind=bounded_kind,
            outcome=bounded_outcome,
        ).observe(bounded_duration)
    )
