from __future__ import annotations

import argparse
import array
import fcntl
import json
import os
import signal
import socket
import stat
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO


# The sandbox image has no database secret or data mount. These values only
# satisfy imports shared with the scanner and point at its private tmpfs.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ALLOW_SQLITE_FOR_TESTS", "true")
os.environ.setdefault("STORAGE_ROOT", "/tmp/fj-technical-qc-storage")
os.environ.setdefault("PACKAGE_ROOT", "/tmp/fj-technical-qc-packages")

from backend.app.technical_qc import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    QcInterval,
    TechnicalQcClaim,
    TechnicalQcError,
    TechnicalQcMediaProbe,
    TechnicalQcScanResult,
    TechnicalQcThresholds,
    _descriptor_sha256,
    probe_technical_qc_media,
    run_technical_qc_ffmpeg,
)


MAX_REQUEST_BYTES = 16 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024
SOCKET_POLL_SECONDS = 0.1
_STOP = threading.Event()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CONNECTIONS: set[socket.socket] = set()


def _json_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _receive_request_and_fd(connection: socket.socket) -> tuple[dict[str, Any], int]:
    descriptor_array = array.array("i")
    data, ancillary, _flags, _address = connection.recvmsg(
        MAX_REQUEST_BYTES + 1,
        socket.CMSG_SPACE(descriptor_array.itemsize),
    )
    if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\n"):
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
    descriptors: list[int] = []
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptor_array.frombytes(payload[: descriptor_array.itemsize])
            descriptors.extend(descriptor_array.tolist())
    if len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
    descriptor = descriptors[0]
    try:
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError
        expected = {
            "type",
            "request_id",
            "input_size",
            "input_sha256",
            "total_ms",
            "width",
            "height",
            "fps_num",
            "fps_den",
            "timeout_ms",
            "output_limit_bytes",
            "thresholds",
        }
        if set(request) != expected or request["type"] != "scan":
            raise ValueError
        if not isinstance(request["request_id"], str) or len(request["request_id"]) != 32:
            raise ValueError
        for key in (
            "input_size",
            "total_ms",
            "width",
            "height",
            "fps_num",
            "fps_den",
            "timeout_ms",
            "output_limit_bytes",
        ):
            if not isinstance(request[key], int) or request[key] <= 0:
                raise ValueError
        if (
            not isinstance(request["input_sha256"], str)
            or len(request["input_sha256"]) != 64
        ):
            raise ValueError
        threshold_keys = {
            "black_pixel_ratio",
            "black_luma",
            "white_yavg",
            "scene_score",
            "silence_db",
            "analysis_width",
        }
        if not isinstance(request["thresholds"], dict) or set(request["thresholds"]) != threshold_keys:
            raise ValueError
        metadata = os.fstat(descriptor)
        access_mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        if (
            access_mode != os.O_RDONLY
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != request["input_size"]
        ):
            raise ValueError
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        os.close(descriptor)
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False) from exc
    return request, descriptor


def _send(connection: socket.socket, payload: dict[str, Any], lock: threading.Lock) -> None:
    encoded = _json_line(payload)
    if len(encoded) > MAX_REPLY_BYTES:
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
    with lock:
        connection.sendall(encoded)


def _connection_closed(connection: socket.socket) -> bool:
    try:
        return connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def _interval_payload(interval: QcInterval) -> dict[str, Any]:
    return {
        "start_frame": interval.start_frame,
        "end_frame": interval.end_frame,
        "start_ms": interval.start_ms,
        "end_ms": interval.end_ms,
        "reason_codes": list(interval.reason_codes),
        "values": interval.values,
    }


def _default_handler(
    request: dict[str, Any],
    descriptor: int,
    connection: socket.socket,
) -> dict[str, Any]:
    deadline = time.monotonic() + request["timeout_ms"] / 1_000
    request_id = request["request_id"]
    send_lock = threading.Lock()
    thresholds = TechnicalQcThresholds(**request["thresholds"])
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        digest = _descriptor_sha256(
            handle.fileno(),
            request["input_size"],
            deadline=deadline,
            should_cancel=lambda: _STOP.is_set() or _connection_closed(connection),
            on_heartbeat=None,
        )
        if digest != request["input_sha256"]:
            raise TechnicalQcError("TECHNICAL_QC_INPUT_IDENTITY_INVALID", retryable=False)
        ffmpeg_command = os.environ.get("TECHNICAL_QC_FFMPEG_COMMAND", "/usr/bin/ffmpeg")
        ffprobe_command = os.environ.get("TECHNICAL_QC_FFPROBE_COMMAND", "/usr/bin/ffprobe")
        probe = probe_technical_qc_media(
            handle,
            ffprobe_command=ffprobe_command,
            deadline=deadline,
            output_limit_bytes=request["output_limit_bytes"],
            should_cancel=lambda: _STOP.is_set() or _connection_closed(connection),
        )
        _send(
            connection,
            {
                "type": "probe",
                "request_id": request_id,
                "video_frame_count": probe.video_frame_count,
                "video_duration_ms": probe.video_duration_ms,
                "total_ms": probe.total_ms,
            },
            send_lock,
        )
        claim = TechnicalQcClaim(
            task_id="sandbox",
            claim_id="sandbox",
            project_ref_id="sandbox",
            review_item_id="sandbox",
            version_id="sandbox",
            episode_no=None,
            input_file_id="sandbox",
            input_storage_path="<fd>",
            input_sha256=request["input_sha256"],
            input_size=request["input_size"],
            total_ms=request["total_ms"],
            width=request["width"],
            height=request["height"],
            fps_num=request["fps_num"],
            fps_den=request["fps_den"],
            rule_version="technical-qc-v1",
            video_frame_count=probe.video_frame_count,
            video_duration_ms=probe.video_duration_ms,
            container_duration_ms=probe.total_ms,
        )
        result = run_technical_qc_ffmpeg(
            claim,
            input_handle=handle,
            ffmpeg_command=ffmpeg_command,
            timeout_seconds=request["timeout_ms"] / 1_000,
            output_limit_bytes=request["output_limit_bytes"],
            on_progress=lambda processed_ms: _send(
                connection,
                {
                    "type": "progress",
                    "request_id": request_id,
                    "processed_ms": processed_ms,
                    "total_ms": probe.total_ms,
                },
                send_lock,
            ),
            should_cancel=lambda: _STOP.is_set() or _connection_closed(connection),
            thresholds=thresholds,
            probe=probe,
            deadline=deadline,
        )
    return {
        "type": "result",
        "request_id": request_id,
        "frames_processed": result.frames_processed,
        "video_frame_count": result.video_frame_count,
        "video_duration_ms": result.video_duration_ms,
        "total_ms": result.total_ms,
        "processed_ms": result.processed_ms,
        "intervals": [_interval_payload(interval) for interval in result.intervals],
    }


def _prepare_listener(socket_path: Path) -> socket.socket:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = socket_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(metadata.st_mode):
            raise RuntimeError("sandbox socket path is not a socket")
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o660)
    listener.listen(16)
    return listener


def serve_once(
    socket_path: Path,
    *,
    handler: Callable[[dict[str, Any], int], dict[str, Any]],
) -> None:
    try:
        with _prepare_listener(socket_path) as listener:
            connection, _ = listener.accept()
            with connection:
                request, descriptor = _receive_request_and_fd(connection)
                try:
                    reply = handler(request, descriptor)
                    connection.sendall(_json_line(reply))
                finally:
                    os.close(descriptor)
    finally:
        socket_path.unlink(missing_ok=True)


def _handle_connection(connection: socket.socket) -> None:
    with connection:
        with _ACTIVE_LOCK:
            _ACTIVE_CONNECTIONS.add(connection)
        descriptor = -1
        try:
            request, descriptor = _receive_request_and_fd(connection)
            reply = _default_handler(request, descriptor, connection)
            connection.sendall(_json_line(reply))
        except TechnicalQcError as exc:
            try:
                connection.sendall(
                    _json_line(
                        {
                            "type": "error",
                            "code": exc.code,
                            "retryable": exc.retryable,
                        },
                    ),
                )
            except OSError:
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with _ACTIVE_LOCK:
                _ACTIVE_CONNECTIONS.discard(connection)


def serve_forever(socket_path: Path, *, concurrency: int) -> None:
    if not 1 <= concurrency <= 8:
        raise ValueError("sandbox concurrency is outside the supported range")
    _STOP.clear()

    def stop(_signum: int, _frame: Any) -> None:
        _STOP.set()
        with _ACTIVE_LOCK:
            for connection in tuple(_ACTIVE_CONNECTIONS):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        with _prepare_listener(socket_path) as listener, ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="technical-qc-sandbox",
        ) as executor:
            listener.settimeout(SOCKET_POLL_SECONDS)
            while not _STOP.is_set():
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                executor.submit(_handle_connection, connection)
    finally:
        socket_path.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def wait_for_socket(socket_path: Path, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if stat.S_ISSOCK(socket_path.stat().st_mode):
                return
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise TimeoutError("sandbox socket did not become ready")


def _threshold_payload(thresholds: TechnicalQcThresholds) -> dict[str, int]:
    return {
        "black_pixel_ratio": thresholds.black_pixel_ratio,
        "black_luma": thresholds.black_luma,
        "white_yavg": thresholds.white_yavg,
        "scene_score": thresholds.scene_score,
        "silence_db": thresholds.silence_db,
        "analysis_width": thresholds.analysis_width,
    }


def _parse_interval(payload: Any) -> QcInterval:
    expected = {
        "start_frame",
        "end_frame",
        "start_ms",
        "end_ms",
        "reason_codes",
        "values",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError
    return QcInterval(
        start_frame=int(payload["start_frame"]),
        end_frame=int(payload["end_frame"]),
        start_ms=int(payload["start_ms"]),
        end_ms=int(payload["end_ms"]),
        reason_codes=tuple(str(code) for code in payload["reason_codes"]),
        values=dict(payload["values"]),
    )


def scan_via_sandbox(
    socket_path: Path,
    claim: TechnicalQcClaim,
    input_handle: BinaryIO,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    thresholds: TechnicalQcThresholds = DEFAULT_THRESHOLDS,
    on_progress: Callable[[int], None] | None = None,
    on_probe: Callable[[TechnicalQcMediaProbe], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[], bool] | None = None,
    heartbeat_interval_seconds: float = 15.0,
) -> TechnicalQcScanResult:
    deadline = time.monotonic() + timeout_seconds
    request_id = uuid.uuid4().hex
    request = {
        "type": "scan",
        "request_id": request_id,
        "input_size": claim.input_size,
        "input_sha256": claim.input_sha256,
        "total_ms": claim.total_ms,
        "width": claim.width,
        "height": claim.height,
        "fps_num": claim.fps_num,
        "fps_den": claim.fps_den,
        "timeout_ms": max(1, int(timeout_seconds * 1_000)),
        "output_limit_bytes": output_limit_bytes,
        "thresholds": _threshold_payload(thresholds),
    }
    descriptor_array = array.array("i", [input_handle.fileno()])
    total_received = 0
    buffer = bytearray()
    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(SOCKET_POLL_SECONDS, timeout_seconds))
            connection.connect(str(socket_path))
            connection.sendmsg(
                [_json_line(request)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor_array)],
            )
            while True:
                if time.monotonic() >= deadline:
                    raise TechnicalQcError("TECHNICAL_QC_SCAN_TIMEOUT", retryable=True)
                if should_cancel is not None and should_cancel():
                    raise TechnicalQcError("TECHNICAL_QC_SCAN_CANCELLED", retryable=False)
                if on_heartbeat is not None and time.monotonic() >= next_heartbeat:
                    if on_heartbeat() is not True:
                        raise TechnicalQcError("TECHNICAL_QC_LEASE_LOST", retryable=False)
                    next_heartbeat = time.monotonic() + heartbeat_interval_seconds
                try:
                    chunk = connection.recv(16 * 1024)
                except TimeoutError:
                    continue
                if not chunk:
                    raise TechnicalQcError("TECHNICAL_QC_SANDBOX_UNAVAILABLE", retryable=True)
                total_received += len(chunk)
                if total_received > output_limit_bytes:
                    raise TechnicalQcError("TECHNICAL_QC_OUTPUT_LIMIT", retryable=True)
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise TechnicalQcError(
                            "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                            retryable=False,
                        ) from exc
                    if not isinstance(message, dict):
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    kind = message.get("type")
                    if kind == "error" and set(message) == {"type", "code", "retryable"}:
                        if not isinstance(message["retryable"], bool):
                            raise TechnicalQcError(
                                "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                                retryable=False,
                            )
                        raise TechnicalQcError(
                            str(message["code"]),
                            retryable=message["retryable"],
                        )
                    if message.get("request_id") != request_id:
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    if kind == "progress" and set(message) == {
                        "type",
                        "request_id",
                        "processed_ms",
                        "total_ms",
                    }:
                        processed_ms = int(message["processed_ms"])
                        if processed_ms < 0 or processed_ms > int(message["total_ms"]):
                            raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                        if on_progress is not None:
                            on_progress(processed_ms)
                        continue
                    if kind == "probe" and set(message) == {
                        "type",
                        "request_id",
                        "video_frame_count",
                        "video_duration_ms",
                        "total_ms",
                    }:
                        probe = TechnicalQcMediaProbe(
                            int(message["video_frame_count"]),
                            int(message["video_duration_ms"]),
                            int(message["total_ms"]),
                        )
                        if min(probe.video_frame_count, probe.video_duration_ms, probe.total_ms) <= 0:
                            raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                        if on_probe is not None:
                            on_probe(probe)
                        continue
                    result_fields = {
                        "type",
                        "request_id",
                        "frames_processed",
                        "video_frame_count",
                        "video_duration_ms",
                        "total_ms",
                        "processed_ms",
                        "intervals",
                    }
                    if kind != "result" or set(message) != result_fields:
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    result = TechnicalQcScanResult(
                        intervals=tuple(_parse_interval(item) for item in message["intervals"]),
                        processed_ms=int(message["processed_ms"]),
                        frames_processed=int(message["frames_processed"]),
                        video_frame_count=int(message["video_frame_count"]),
                        video_duration_ms=int(message["video_duration_ms"]),
                        total_ms=int(message["total_ms"]),
                    )
                    if (
                        result.frames_processed != result.video_frame_count
                        or result.processed_ms != result.total_ms
                        or min(
                            result.frames_processed,
                            result.video_duration_ms,
                            result.total_ms,
                        ) <= 0
                    ):
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    return result
    except TechnicalQcError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_UNAVAILABLE", retryable=True) from exc


def _bounded_concurrency(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 8:
        raise argparse.ArgumentTypeError("concurrency must be between 1 and 8")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=Path("/run/fj-technical-qc/sandbox.sock"),
    )
    parser.add_argument(
        "--concurrency",
        type=_bounded_concurrency,
        default=4,
    )
    args = parser.parse_args()
    serve_forever(args.socket_path, concurrency=args.concurrency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
