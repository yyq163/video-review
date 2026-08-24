from __future__ import annotations

import argparse
import array
import fcntl
import json
import math
import os
import selectors
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
    REASON_LABELS,
    TechnicalQcClaim,
    TechnicalQcError,
    TechnicalQcFatalError,
    TechnicalQcMediaProbe,
    TechnicalQcScanResult,
    TechnicalQcThresholds,
    _atomic_worker_state,
    probe_technical_qc_media,
    run_technical_qc_ffmpeg,
)


MAX_REQUEST_BYTES = 16 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024
MAX_INTERVALS = 10_000
MAX_MEDIA_DURATION_MS = 7 * 24 * 60 * 60 * 1_000
SOCKET_POLL_SECONDS = 0.1
INITIAL_REQUEST_TIMEOUT_SECONDS = 0.25
HEALTH_REQUEST_TIMEOUT_SECONDS = 0.15
MAX_PENDING_HEALTH_CONNECTIONS = 32
_STOP = threading.Event()
_FATAL = threading.Event()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_CONNECTIONS: set[socket.socket] = set()
_STATUS_PATH: Path | None = None
_VALUE_KEYS = frozenset({"pblack", "yavg", "scene_score", "frame_count", "duration_ms"})
_ERROR_RETRYABILITY = {
    "TECHNICAL_QC_FILTER_UNAVAILABLE": False,
    "TECHNICAL_QC_MEDIA_INVALID": False,
    "TECHNICAL_QC_INPUT_UNREADABLE": False,
    "TECHNICAL_QC_INPUT_IDENTITY_INVALID": False,
    "TECHNICAL_QC_PROCESS_UNAVAILABLE": True,
    "TECHNICAL_QC_SCAN_FAILED": True,
    "TECHNICAL_QC_SCAN_TIMEOUT": True,
    "TECHNICAL_QC_OUTPUT_LIMIT": True,
    "TECHNICAL_QC_SCAN_CANCELLED": False,
    "TECHNICAL_QC_LEASE_LOST": False,
    "TECHNICAL_QC_INCOMPLETE_SCAN": True,
}


def _mark_fatal(_error: TechnicalQcFatalError) -> None:
    _FATAL.set()
    _STOP.set()
    if _STATUS_PATH is not None:
        _atomic_worker_state(_STATUS_PATH, "fatal")
    with _ACTIVE_LOCK:
        for connection in tuple(_ACTIVE_CONNECTIONS):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _json_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _receive_request_and_fd(
    connection: socket.socket,
    *,
    allow_health: bool = False,
) -> tuple[dict[str, Any], int]:
    data, ancillary, _flags, _address = connection.recvmsg(
        MAX_REQUEST_BYTES + 1,
        socket.CMSG_SPACE(array.array("i").itemsize),
    )
    if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\n"):
        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
    descriptors: list[int] = []
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptor_array = array.array("i")
            descriptor_array.frombytes(payload[: descriptor_array.itemsize])
            descriptors.extend(descriptor_array.tolist())
    try:
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError
        if request.get("type") == "health":
            if (
                not allow_health
                or descriptors
                or set(request) != {"type", "request_id"}
                or not isinstance(request["request_id"], str)
                or len(request["request_id"]) != 32
            ):
                raise ValueError
            return request, -1
        if len(descriptors) != 1:
            raise ValueError
        descriptor = descriptors[0]
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
        for descriptor in descriptors:
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
        ffmpeg_command = os.environ.get("TECHNICAL_QC_FFMPEG_COMMAND", "/usr/bin/ffmpeg")
        ffprobe_command = os.environ.get("TECHNICAL_QC_FFPROBE_COMMAND", "/usr/bin/ffprobe")
        probe = probe_technical_qc_media(
            handle,
            ffprobe_command=ffprobe_command,
            deadline=deadline,
            output_limit_bytes=request["output_limit_bytes"],
            should_cancel=lambda: _STOP.is_set() or _connection_closed(connection),
            on_late_fatal=_mark_fatal,
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
            on_late_fatal=_mark_fatal,
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
    handler: Callable[[dict[str, Any], int], dict[str, Any] | list[dict[str, Any]]],
) -> None:
    try:
        with _prepare_listener(socket_path) as listener:
            connection, _ = listener.accept()
            with connection:
                request, descriptor = _receive_request_and_fd(connection)
                try:
                    reply = handler(request, descriptor)
                    for item in reply if isinstance(reply, list) else [reply]:
                        connection.sendall(_json_line(item))
                finally:
                    os.close(descriptor)
    finally:
        socket_path.unlink(missing_ok=True)


def _sandbox_status_ready() -> bool:
    if _STOP.is_set() or _FATAL.is_set() or _STATUS_PATH is None:
        return False
    try:
        return _STATUS_PATH.is_file() and _STATUS_PATH.read_text(encoding="ascii") == "ready"
    except OSError:
        return False


def sandbox_health_socket_path(socket_path: Path) -> Path:
    return socket_path.with_suffix(".health.sock")


def _handle_connection(connection: socket.socket) -> None:
    with connection:
        with _ACTIVE_LOCK:
            _ACTIVE_CONNECTIONS.add(connection)
        descriptor = -1
        try:
            connection.settimeout(INITIAL_REQUEST_TIMEOUT_SECONDS)
            request, descriptor = _receive_request_and_fd(connection)
            connection.settimeout(None)
            reply = _default_handler(request, descriptor, connection)
            connection.sendall(_json_line(reply))
        except TechnicalQcFatalError as exc:
            _mark_fatal(exc)
        except TechnicalQcError as exc:
            try:
                connection.sendall(
                    _json_line(
                        {
                            "type": "error",
                            "code": exc.code,
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


def _serve_health_connections(listener: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    pending: dict[socket.socket, float] = {}
    listener.setblocking(False)
    selector.register(listener, selectors.EVENT_READ)

    def close_pending(connection: socket.socket) -> None:
        try:
            selector.unregister(connection)
        except (KeyError, ValueError):
            pass
        pending.pop(connection, None)
        connection.close()

    try:
        while not _STOP.is_set():
            for key, _events in selector.select(SOCKET_POLL_SECONDS):
                if key.fileobj is listener:
                    while True:
                        try:
                            connection, _ = listener.accept()
                        except BlockingIOError:
                            break
                        if len(pending) >= MAX_PENDING_HEALTH_CONNECTIONS:
                            connection.close()
                            continue
                        connection.setblocking(False)
                        pending[connection] = (
                            time.monotonic() + HEALTH_REQUEST_TIMEOUT_SECONDS
                        )
                        selector.register(connection, selectors.EVENT_READ)
                    continue
                connection = key.fileobj
                if not isinstance(connection, socket.socket):
                    continue
                try:
                    request, descriptor = _receive_request_and_fd(
                        connection,
                        allow_health=True,
                    )
                    if descriptor != -1 or request["type"] != "health":
                        raise TechnicalQcError(
                            "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                            retryable=False,
                        )
                    connection.settimeout(HEALTH_REQUEST_TIMEOUT_SECONDS)
                    connection.sendall(
                        _json_line(
                            {
                                "type": "health",
                                "request_id": request["request_id"],
                                "status": (
                                    "ready" if _sandbox_status_ready() else "not_ready"
                                ),
                            },
                        ),
                    )
                except (OSError, TechnicalQcError):
                    pass
                finally:
                    close_pending(connection)
            now = time.monotonic()
            for connection, deadline in tuple(pending.items()):
                if now >= deadline:
                    close_pending(connection)
    finally:
        for connection in tuple(pending):
            close_pending(connection)
        selector.close()


def serve_forever(socket_path: Path, *, concurrency: int) -> None:
    global _STATUS_PATH
    if not 1 <= concurrency <= 8:
        raise ValueError("sandbox concurrency is outside the supported range")
    _STOP.clear()
    _FATAL.clear()
    status_path = socket_path.with_name("sandbox.status")
    health_socket_path = sandbox_health_socket_path(socket_path)
    _STATUS_PATH = status_path

    def stop(_signum: int, _frame: Any) -> None:
        _STOP.set()
        _atomic_worker_state(status_path, "stopping")
        with _ACTIVE_LOCK:
            for connection in tuple(_ACTIVE_CONNECTIONS):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        _atomic_worker_state(status_path, "starting")
        with (
            _prepare_listener(socket_path) as listener,
            _prepare_listener(health_socket_path) as health_listener,
            ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="technical-qc-sandbox",
            ) as executor,
        ):
            health_thread = threading.Thread(
                target=_serve_health_connections,
                args=(health_listener,),
                daemon=True,
                name="technical-qc-sandbox-health",
            )
            health_thread.start()
            try:
                _atomic_worker_state(status_path, "ready")
                listener.settimeout(SOCKET_POLL_SECONDS)
                while not _STOP.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    executor.submit(_handle_connection, connection)
            finally:
                _STOP.set()
                health_thread.join(timeout=1)
                if health_thread.is_alive():
                    _FATAL.set()
                    _atomic_worker_state(status_path, "fatal")
        if _FATAL.is_set():
            _atomic_worker_state(status_path, "fatal")
            raise SystemExit(70)
    finally:
        socket_path.unlink(missing_ok=True)
        health_socket_path.unlink(missing_ok=True)
        if not _FATAL.is_set():
            _atomic_worker_state(status_path, "stopped")
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


def sandbox_healthcheck(socket_path: Path, *, timeout_seconds: float = 1.0) -> bool:
    status_path = socket_path.with_name("sandbox.status")
    try:
        if status_path.read_text(encoding="ascii") != "ready":
            return False
    except OSError:
        return False
    request_id = uuid.uuid4().hex
    deadline = time.monotonic() + timeout_seconds
    response = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(sandbox_health_socket_path(socket_path)))
            connection.sendall(
                _json_line({"type": "health", "request_id": request_id}),
            )
            while b"\n" not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(MAX_REQUEST_BYTES + 1 - len(response))
                if not chunk:
                    return False
                response.extend(chunk)
                if len(response) > MAX_REQUEST_BYTES:
                    return False
    except (OSError, TimeoutError):
        return False
    if not response.endswith(b"\n") or response.count(b"\n") != 1:
        return False
    try:
        payload = json.loads(response)
    except json.JSONDecodeError:
        return False
    return payload == {
        "type": "health",
        "request_id": request_id,
        "status": "ready",
    }


def _threshold_payload(thresholds: TechnicalQcThresholds) -> dict[str, int]:
    return {
        "black_pixel_ratio": thresholds.black_pixel_ratio,
        "black_luma": thresholds.black_luma,
        "white_yavg": thresholds.white_yavg,
        "scene_score": thresholds.scene_score,
        "silence_db": thresholds.silence_db,
        "analysis_width": thresholds.analysis_width,
    }


def _validate_probe(
    message: dict[str, Any],
    claim: TechnicalQcClaim,
) -> TechnicalQcMediaProbe:
    probe = TechnicalQcMediaProbe(
        int(message["video_frame_count"]),
        int(message["video_duration_ms"]),
        int(message["total_ms"]),
    )
    maximum_total = min(MAX_MEDIA_DURATION_MS, claim.total_ms + 1_000)
    minimum_total = max(1, claim.total_ms - 1_000)
    maximum_frames = max(
        1,
        math.ceil(maximum_total * claim.fps_num / (1_000 * claim.fps_den)) + 1,
    )
    expected_video_ms = round(
        probe.video_frame_count * 1_000 * claim.fps_den / claim.fps_num,
    )
    frame_tolerance_ms = max(100, math.ceil(2_000 * claim.fps_den / claim.fps_num))
    if (
        not minimum_total <= probe.total_ms <= maximum_total
        or not 1 <= probe.video_frame_count <= maximum_frames
        or not 1 <= probe.video_duration_ms <= probe.total_ms + frame_tolerance_ms
        or abs(probe.video_duration_ms - expected_video_ms) > frame_tolerance_ms
    ):
        raise ValueError
    return probe


def _parse_interval(payload: Any, probe: TechnicalQcMediaProbe) -> QcInterval:
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
    interval = QcInterval(
        start_frame=int(payload["start_frame"]),
        end_frame=int(payload["end_frame"]),
        start_ms=int(payload["start_ms"]),
        end_ms=int(payload["end_ms"]),
        reason_codes=tuple(str(code) for code in payload["reason_codes"]),
        values=dict(payload["values"]),
    )
    if (
        interval.end_frame >= probe.video_frame_count
        or interval.end_ms > probe.total_ms
        or len(interval.reason_codes) != len(set(interval.reason_codes))
        or not set(interval.reason_codes) <= set(REASON_LABELS)
        or len(interval.values) > len(_VALUE_KEYS)
        or not set(interval.values) <= _VALUE_KEYS
    ):
        raise ValueError
    for key, value in interval.values.items():
        if key in {"frame_count", "duration_ms"}:
            if type(value) is not int:
                raise ValueError
            if key == "frame_count" and (
                value != interval.frame_count
                or value > probe.video_frame_count
                or "short_shot" not in interval.reason_codes
            ):
                raise ValueError
            if key == "duration_ms" and (
                value != interval.duration_ms
                or value > probe.total_ms
                or "silence" not in interval.reason_codes
            ):
                raise ValueError
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            raise ValueError
        if key in {"pblack", "scene_score"} and not 0 <= value <= 100:
            raise ValueError
        if key == "yavg" and not 0 <= value <= 255:
            raise ValueError
    if "pblack" in interval.values and "black_frame" not in interval.reason_codes:
        raise ValueError
    if "yavg" in interval.values and "white_frame" not in interval.reason_codes:
        raise ValueError
    if "scene_score" in interval.values and "short_shot" not in interval.reason_codes:
        raise ValueError
    return interval


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
    accepted_probe: TechnicalQcMediaProbe | None = None
    last_progress_ms = -1
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
                    code = (
                        "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID"
                        if accepted_probe is not None
                        else "TECHNICAL_QC_SANDBOX_UNAVAILABLE"
                    )
                    raise TechnicalQcError(code, retryable=accepted_probe is None)
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
                    if kind == "error" and set(message) == {"type", "code"}:
                        code = str(message["code"])
                        if code not in _ERROR_RETRYABILITY:
                            raise TechnicalQcError(
                                "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                                retryable=False,
                            )
                        raise TechnicalQcError(
                            code,
                            retryable=_ERROR_RETRYABILITY[code],
                        )
                    if message.get("request_id") != request_id:
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    if kind == "progress" and set(message) == {
                        "type",
                        "request_id",
                        "processed_ms",
                        "total_ms",
                    }:
                        if accepted_probe is None:
                            raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                        processed_ms = int(message["processed_ms"])
                        if (
                            int(message["total_ms"]) != accepted_probe.total_ms
                            or processed_ms < last_progress_ms
                            or processed_ms > accepted_probe.total_ms
                        ):
                            raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                        last_progress_ms = processed_ms
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
                        if accepted_probe is not None:
                            raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                        try:
                            probe = _validate_probe(message, claim)
                        except (TypeError, ValueError) as exc:
                            raise TechnicalQcError(
                                "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                                retryable=False,
                            ) from exc
                        accepted_probe = probe
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
                    if accepted_probe is None or last_progress_ms < 0:
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    if not isinstance(message["intervals"], list) or len(message["intervals"]) > MAX_INTERVALS:
                        raise TechnicalQcError("TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID", retryable=False)
                    try:
                        result = TechnicalQcScanResult(
                            intervals=tuple(_parse_interval(item, accepted_probe) for item in message["intervals"]),
                            processed_ms=int(message["processed_ms"]),
                            frames_processed=int(message["frames_processed"]),
                            video_frame_count=int(message["video_frame_count"]),
                            video_duration_ms=int(message["video_duration_ms"]),
                            total_ms=int(message["total_ms"]),
                        )
                    except (TypeError, ValueError) as exc:
                        raise TechnicalQcError(
                            "TECHNICAL_QC_SANDBOX_PROTOCOL_INVALID",
                            retryable=False,
                        ) from exc
                    if (
                        result.frames_processed != result.video_frame_count
                        or result.processed_ms != result.total_ms
                        or result.video_frame_count != accepted_probe.video_frame_count
                        or result.video_duration_ms != accepted_probe.video_duration_ms
                        or result.total_ms != accepted_probe.total_ms
                        or last_progress_ms > result.processed_ms
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


def _bounded_health_timeout(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 5:
        raise argparse.ArgumentTypeError("health timeout must be between 0 and 5 seconds")
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
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--health-timeout-seconds",
        type=_bounded_health_timeout,
        default=1.0,
    )
    args = parser.parse_args()
    if args.healthcheck:
        return 0 if sandbox_healthcheck(
            args.socket_path,
            timeout_seconds=args.health_timeout_seconds,
        ) else 1
    serve_forever(args.socket_path, concurrency=args.concurrency)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
