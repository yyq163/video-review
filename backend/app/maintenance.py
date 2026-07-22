from __future__ import annotations

import argparse
import json
import signal
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Never

from backend.app.maintenance_cleanup import (
    MAX_TOMBSTONE_BYTES,
    _claim_terminal_or_stale_uploads,
    _cleanup_claimed_upload_parts,
    _cleanup_pending_deletes,
    _pending_delete_has_database_reference,
    _read_tombstone,
    _validated_tombstone_target,
    _validated_upload_part_path,
    cleanup_temporary_files,
)

MAX_MAINTENANCE_SECONDS = 86_400
MAX_CONSECUTIVE_ERRORS = 100
HEARTBEAT_REFRESH_SECONDS = 30
MAX_ERROR_RETRY_SECONDS = 10

__all__ = [
    "MAX_TOMBSTONE_BYTES",
    "_claim_terminal_or_stale_uploads",
    "_cleanup_claimed_upload_parts",
    "_cleanup_pending_deletes",
    "_pending_delete_has_database_reference",
    "_read_tombstone",
    "_validated_tombstone_target",
    "_validated_upload_part_path",
    "cleanup_temporary_files",
]


class RepeatedMaintenanceError(RuntimeError):
    pass


def maintenance_cycle() -> dict[str, int]:
    return cleanup_temporary_files()


def _cleanup_with_timeout(cleanup: Callable[[], dict[str, int]], timeout_seconds: int) -> dict[str, int]:
    def raise_timeout(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError("maintenance cleanup cycle timed out")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return cleanup()
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)


def _cleanup_result_payload(result: dict[str, int]) -> dict[str, object]:
    status = "degraded" if any(key.startswith("failed_") and value > 0 for key, value in result.items()) else "ok"
    return {"status": status, **result}


def _sleep_with_heartbeat(
    interval_seconds: int,
    heartbeat_path: Path,
    sleep: Callable[[float], None],
) -> None:
    remaining = interval_seconds
    while remaining > 0:
        duration = min(remaining, HEARTBEAT_REFRESH_SECONDS)
        sleep(duration)
        heartbeat_path.touch()
        remaining -= duration


def run_cleanup_loop(
    interval_seconds: int,
    *,
    cycle_timeout_seconds: int = 60,
    max_consecutive_errors: int = 3,
    heartbeat_path: Path = Path("/tmp/fj-maintenance-heartbeat"),
    status_path: Path = Path("/tmp/fj-maintenance-status"),
    cleanup: Callable[[], dict[str, int]] = maintenance_cycle,
    sleep: Callable[[float], None] = time.sleep,
) -> Never:
    if not 1 <= interval_seconds <= MAX_MAINTENANCE_SECONDS:
        raise ValueError("interval_seconds is outside the supported range")
    if not 1 <= cycle_timeout_seconds <= MAX_MAINTENANCE_SECONDS:
        raise ValueError("cycle_timeout_seconds is outside the supported range")
    if not 1 <= max_consecutive_errors <= MAX_CONSECUTIVE_ERRORS:
        raise ValueError("max_consecutive_errors is outside the supported range")
    consecutive_errors = 0
    heartbeat_path.touch()
    status_path.write_text("starting", encoding="ascii")
    while True:
        try:
            result = _cleanup_with_timeout(cleanup, cycle_timeout_seconds)
            payload = _cleanup_result_payload(result)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            payload = {
                "status": "error",
                "error_type": type(exc).__name__,
                "consecutive_errors": consecutive_errors,
            }
        heartbeat_path.touch()
        status_path.write_text(str(payload["status"]), encoding="ascii")
        print(json.dumps(payload, sort_keys=True), flush=True)
        if consecutive_errors >= max_consecutive_errors:
            raise RepeatedMaintenanceError("maintenance cleanup failed repeatedly")
        next_interval_seconds = min(interval_seconds, MAX_ERROR_RETRY_SECONDS) if consecutive_errors else interval_seconds
        _sleep_with_heartbeat(next_interval_seconds, heartbeat_path, sleep)


def _bounded_seconds(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_MAINTENANCE_SECONDS:
        raise argparse.ArgumentTypeError(f"value must be between 1 and {MAX_MAINTENANCE_SECONDS}")
    return parsed


def _bounded_error_limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_CONSECUTIVE_ERRORS:
        raise argparse.ArgumentTypeError(f"value must be between 1 and {MAX_CONSECUTIVE_ERRORS}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["cleanup", "run"])
    parser.add_argument("--interval-seconds", type=_bounded_seconds, default=300)
    parser.add_argument("--cycle-timeout-seconds", type=_bounded_seconds, default=60)
    parser.add_argument("--max-consecutive-errors", type=_bounded_error_limit, default=3)
    args = parser.parse_args()
    if args.command == "cleanup":
        try:
            payload = _cleanup_result_payload(maintenance_cycle())
        except Exception as exc:
            print(json.dumps({"status": "error", "error_type": type(exc).__name__}, sort_keys=True), flush=True)
            return 1
        print(json.dumps(payload, sort_keys=True), flush=True)
    else:
        try:
            run_cleanup_loop(
                args.interval_seconds,
                cycle_timeout_seconds=args.cycle_timeout_seconds,
                max_consecutive_errors=args.max_consecutive_errors,
            )
        except RepeatedMaintenanceError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
