from __future__ import annotations

from prometheus_client import generate_latest

from backend.app import nginx_metrics_exporter as exporter


def test_bounded_syslog_record_parses_without_sensitive_dimensions() -> None:
    record = exporter.parse_record(
        b"<190>Aug  1 10:00:00 nginx fcr_nginx: "
        b"fcr_range media get 206 1048576 0.250 0.012 true complete"
    )
    assert record == exporter.RangeMetricRecord(
        kind="media",
        method="get",
        status="206",
        response_bytes=1_048_576,
        duration_seconds=0.25,
        ttfb_seconds=0.012,
        range_requested="true",
        completion="complete",
    )


def test_unexpected_kind_or_sensitive_suffix_is_rejected() -> None:
    assert exporter.parse_record(
        b"fcr_range other get 200 1 0.1 0.01 false complete"
    ) is None
    assert exporter.parse_record(
        b"fcr_range media get 206 1 0.1 0.01 true complete /private/file.mp4"
    ) is None
    assert exporter.parse_record(b"\xffsecret") is None
    assert exporter.parse_record(
        b"fcr_range media get 206 " + b"9" * 5_000 + b" 0.1 0.01 true complete"
    ) is None
    assert exporter.parse_record(
        b"fcr_range media get 206 1 99999.999999 0.01 true complete"
    ) is None
    assert exporter.parse_record(
        b"fcr_range media get 206 53687091201 0.1 0.01 true complete"
    ) is None


def test_exporter_uses_single_threaded_bounded_datagram_reads() -> None:
    assert exporter._BoundedUdpServer.max_packet_size == 1024
    assert not issubclass(exporter._BoundedUdpServer, exporter.socketserver.ThreadingMixIn)


def test_status_is_bounded_and_metrics_contain_no_identifiers() -> None:
    record = exporter.parse_record(
        b"fcr_range stream head 599 0 0.050 - false cancelled"
    )
    assert record is not None
    assert record.status == "other"
    exporter.observe_record(record)
    metrics = generate_latest().decode("utf-8")
    assert 'fcr_nginx_range_requests_total{kind="stream",method="head",range_requested="false",status="other"}' in metrics
    assert "project" not in record.__dict__
    assert "path" not in record.__dict__
    assert "address" not in record.__dict__
