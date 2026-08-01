from __future__ import annotations

import logging
import math
import os
import re
import socketserver
from dataclasses import dataclass

from prometheus_client import Counter, Histogram, start_http_server

logger = logging.getLogger(__name__)

ALLOWED_KINDS = frozenset({"media", "stream"})
ALLOWED_METHODS = frozenset({"get", "head", "other"})
ALLOWED_STATUSES = frozenset(
    {
        "200",
        "206",
        "304",
        "400",
        "401",
        "403",
        "404",
        "409",
        "416",
        "429",
        "499",
        "500",
        "502",
        "503",
        "504",
    }
)
MAX_RECORD_BYTES = 512
MAX_RESPONSE_BYTES = 50 * 1024 * 1024 * 1024
MAX_DURATION_SECONDS = 86_400.0
MAX_TTFB_SECONDS = 3_600.0
SAFE_LOG = re.compile(
    r"(?:^|\s)fcr_range "
    r"(?P<kind>media|stream|other) "
    r"(?P<method>get|head|other) "
    r"(?P<status>[0-9]{3}) "
    r"(?P<bytes>[0-9]{1,11}) "
    r"(?P<duration>[0-9]{1,5}(?:\.[0-9]{1,6})?) "
    r"(?P<ttfb>-|[0-9]{1,4}(?:\.[0-9]{1,6})?) "
    r"(?P<range>true|false) "
    r"(?P<completion>complete|cancelled)$"
)

REQUESTS = Counter(
    "fcr_nginx_range_requests_total",
    "NGINX media/stream requests from the bounded metrics log.",
    ("kind", "method", "status", "range_requested"),
)
RESPONSE_BYTES = Counter(
    "fcr_nginx_range_response_bytes_total",
    "NGINX response bytes without path, file, principal, address, or Range value.",
    ("kind", "status"),
)
DURATION = Histogram(
    "fcr_nginx_range_duration_seconds",
    "NGINX request duration for protected media and stream authorization.",
    ("kind", "range_requested"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
TTFB = Histogram(
    "fcr_nginx_range_ttfb_seconds",
    "Server-side TTFB proxy from the FastAPI authorization upstream header time.",
    ("kind", "status"),
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
TTFB_UNAVAILABLE = Counter(
    "fcr_nginx_range_ttfb_unavailable_total",
    "Requests for which NGINX did not report an upstream header time.",
    ("kind", "status"),
)
FAILURES = Counter(
    "fcr_nginx_range_failures_total",
    "NGINX media/stream failures grouped only by a bounded status.",
    ("kind", "status"),
)
CANCELLATIONS = Counter(
    "fcr_nginx_range_cancellations_total",
    "NGINX client-closed media/stream requests (status 499).",
    ("kind", "range_requested"),
)
PARSE_FAILURES = Counter(
    "fcr_nginx_metrics_parse_failures_total",
    "Rejected or malformed bounded NGINX metric log records.",
)


@dataclass(frozen=True)
class RangeMetricRecord:
    kind: str
    method: str
    status: str
    response_bytes: int
    duration_seconds: float
    ttfb_seconds: float | None
    range_requested: str
    completion: str


def parse_record(payload: bytes) -> RangeMetricRecord | None:
    if len(payload) > MAX_RECORD_BYTES:
        return None
    try:
        text = payload.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError:
        return None
    match = SAFE_LOG.search(text)
    if match is None:
        return None
    kind = match.group("kind")
    if kind not in ALLOWED_KINDS:
        return None
    method = match.group("method")
    status = match.group("status")
    response_bytes = int(match.group("bytes"))
    duration_seconds = float(match.group("duration"))
    ttfb_seconds = (
        None if match.group("ttfb") == "-" else float(match.group("ttfb"))
    )
    if (
        response_bytes > MAX_RESPONSE_BYTES
        or not math.isfinite(duration_seconds)
        or duration_seconds > MAX_DURATION_SECONDS
        or (
            ttfb_seconds is not None
            and (
                not math.isfinite(ttfb_seconds)
                or ttfb_seconds > MAX_TTFB_SECONDS
            )
        )
    ):
        return None
    return RangeMetricRecord(
        kind=kind,
        method=method if method in ALLOWED_METHODS else "other",
        status=status if status in ALLOWED_STATUSES else "other",
        response_bytes=response_bytes,
        duration_seconds=duration_seconds,
        ttfb_seconds=ttfb_seconds,
        range_requested=match.group("range"),
        completion=match.group("completion"),
    )


def observe_record(record: RangeMetricRecord) -> None:
    REQUESTS.labels(
        kind=record.kind,
        method=record.method,
        status=record.status,
        range_requested=record.range_requested,
    ).inc()
    RESPONSE_BYTES.labels(kind=record.kind, status=record.status).inc(
        record.response_bytes
    )
    DURATION.labels(
        kind=record.kind,
        range_requested=record.range_requested,
    ).observe(record.duration_seconds)
    if record.ttfb_seconds is None:
        TTFB_UNAVAILABLE.labels(kind=record.kind, status=record.status).inc()
    else:
        TTFB.labels(kind=record.kind, status=record.status).observe(
            record.ttfb_seconds
        )
    if record.status == "499" or record.completion == "cancelled":
        CANCELLATIONS.labels(
            kind=record.kind,
            range_requested=record.range_requested,
        ).inc()
    elif record.status == "other" or int(record.status) >= 400:
        FAILURES.labels(kind=record.kind, status=record.status).inc()


class _DatagramHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        payload = self.request[0]
        record = parse_record(payload)
        if record is None:
            PARSE_FAILURES.inc()
            return
        observe_record(record)


class _BoundedUdpServer(socketserver.UDPServer):
    allow_reuse_address = True
    max_packet_size = 1024


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    bind_host = os.environ.get("NGINX_METRICS_BIND_HOST", "127.0.0.1")
    udp_port = int(os.environ.get("NGINX_METRICS_UDP_PORT", "5514"))
    metrics_port = int(os.environ.get("NGINX_METRICS_HTTP_PORT", "9103"))
    start_http_server(metrics_port, addr=bind_host)
    logger.info("NGINX metrics exporter ready")
    with _BoundedUdpServer((bind_host, udp_port), _DatagramHandler) as server:
        server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
