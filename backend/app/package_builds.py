from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Never

from sqlalchemy import or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra import database as database_module
from backend.app.modules.final_cut_review.infra.database import SessionLocal
from backend.app.modules.final_cut_review.infra.repositories import (
    PackageBuildArtifact,
    PackageBuildClaim,
    SqlAlchemyReviewRepository,
)
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalCutPackageSnapshotModel, utcnow
from backend.app.safe_files import (
    UnsafeFilePathError,
    pin_regular_file,
    unlink_regular_file_if_identity,
)
from backend.app.settings import Settings, get_database_settings

LOGGER = logging.getLogger(__name__)
PACKAGE_BUILD_BATCH_SIZE = 1
PACKAGE_BUILD_ADVISORY_LOCK_KEY = 5064946991358883142
HEARTBEAT_REFRESH_SECONDS = 30
MAX_WORKER_SECONDS = 86_400
_SQLITE_BUILD_LOCK = threading.Lock()


@contextmanager
def _worker_session() -> Iterator[Session]:
    with SessionLocal() as session:
        database_module.require_runtime_participant_session(session)
        yield session


def _worker_context(project_ref_id: str) -> ExecutionContext:
    return ExecutionContext(
        entry_source="review",
        request_id=str(uuid.uuid4()),
        principal=PrincipalRef(kind="system", id="package-worker", project_ref_ids=(project_ref_id,)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )


@contextmanager
def _package_build_lock(bind_source: Engine | Session) -> Iterator[bool]:
    bind = bind_source.get_bind() if isinstance(bind_source, Session) else bind_source
    if bind.dialect.name != "postgresql":
        acquired = _SQLITE_BUILD_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _SQLITE_BUILD_LOCK.release()
        return
    if not isinstance(bind, Engine):
        raise RuntimeError("package worker requires an engine-bound PostgreSQL session")
    with bind.connect() as lock_connection:
        acquired = bool(
            lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": PACKAGE_BUILD_ADVISORY_LOCK_KEY},
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                unlocked = lock_connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": PACKAGE_BUILD_ADVISORY_LOCK_KEY},
                )
                lock_connection.commit()
                if unlocked is not True:
                    raise RuntimeError("package worker advisory lock was not released by its owner")
            else:
                lock_connection.rollback()


def _package_staging_absent(claim: PackageBuildClaim) -> bool:
    settings = get_database_settings()
    try:
        with pin_regular_file(claim.staging_path, settings.package_root) as pinned:
            return pinned is None or not pinned.exists
    except (OSError, UnsafeFilePathError):
        return False


def _discard_artifact(artifact: PackageBuildArtifact) -> bool:
    settings = get_database_settings()
    try:
        unlink_regular_file_if_identity(
            artifact.storage_path,
            settings.package_root,
            device=artifact.device,
            inode=artifact.inode,
        )
    except (OSError, UnsafeFilePathError):
        return False
    try:
        with pin_regular_file(artifact.storage_path, settings.package_root) as pinned:
            return pinned is None or not pinned.exists
    except (OSError, UnsafeFilePathError):
        return False


def _record_build_failure(
    claim: PackageBuildClaim,
    context: ExecutionContext,
    error_code: str,
    *,
    retryable: bool,
    storage_reclaimed: bool,
) -> str:
    settings = get_database_settings()
    with _worker_session() as failure_session:
        failure_repository = SqlAlchemyReviewRepository(failure_session, settings)
        status = failure_repository.record_package_build_failure(
            claim.package_id,
            context,
            error_code,
            lease_id=claim.lease_id,
            retryable=retryable,
            storage_reclaimed=storage_reclaimed,
        )
        failure_session.commit()
    return status


def process_package_snapshot(package_id: str) -> str:
    settings = get_database_settings()
    with database_module.runtime_participant_lease(database_module.engine, settings):
        return _process_package_snapshot(package_id, settings)


def _process_package_snapshot(package_id: str, settings: Settings) -> str:
    with _worker_session() as lookup_session:
        project_ref_id = lookup_session.scalar(select(FinalCutPackageSnapshotModel.project_ref_id).where(FinalCutPackageSnapshotModel.id == package_id))
    if project_ref_id is None:
        return "skipped"
    context = _worker_context(project_ref_id)
    with _package_build_lock(database_module.engine) as lock_acquired:
        if not lock_acquired:
            return "skipped"
        with _worker_session() as claim_session:
            claim_repository = SqlAlchemyReviewRepository(claim_session, settings)
            claim_status, claim = claim_repository.claim_package_build(package_id, context)
            claim_session.commit()
        if claim_status != "claimed" or claim is None:
            return claim_status
        try:
            artifact = claim_repository.build_prepared_package(claim)
        except TimeoutError:
            timeout_status = _record_build_failure(
                claim,
                context,
                "PACKAGE_BUILD_TIMEOUT",
                retryable=True,
                storage_reclaimed=_package_staging_absent(claim),
            )
            if timeout_status == "failed":
                return "failed"
            raise
        except (ReviewError, UnsafeFilePathError) as exc:
            error_code = exc.code if isinstance(exc, ReviewError) else "STORAGE_UNAVAILABLE"
            return _record_build_failure(
                claim,
                context,
                error_code,
                retryable=False,
                storage_reclaimed=_package_staging_absent(claim),
            )
        except Exception as exc:
            LOGGER.error(
                "package_build_unexpected_failure",
                extra={"error_type": type(exc).__name__},
            )
            failure_status = _record_build_failure(
                claim,
                context,
                "PACKAGE_BUILD_FAILED",
                retryable=True,
                storage_reclaimed=_package_staging_absent(claim),
            )
            return "failed" if failure_status == "failed" else "skipped"
        with _worker_session() as publish_session:
            publish_repository = SqlAlchemyReviewRepository(publish_session, settings)
            publish_status = publish_repository.publish_prepared_package(artifact, context)
            publish_session.commit()
        if publish_status == "ready":
            return "ready"
        storage_reclaimed = _discard_artifact(artifact)
        if publish_status == "skipped":
            return "skipped"
        error_code = "FILE_TOO_LARGE" if publish_status == "quota_exceeded" else "STORAGE_UNAVAILABLE"
        return _record_build_failure(
            claim,
            context,
            error_code,
            retryable=False,
            storage_reclaimed=storage_reclaimed,
        )


def process_pending_packages(batch_size: int = PACKAGE_BUILD_BATCH_SIZE) -> dict[str, int]:
    if not 1 <= batch_size <= PACKAGE_BUILD_BATCH_SIZE:
        raise ValueError("package build batch size is outside the supported range")
    settings = get_database_settings()
    with database_module.runtime_participant_lease(database_module.engine, settings):
        with _worker_session() as session:
            package_ids = list(
                session.scalars(
                    select(FinalCutPackageSnapshotModel.id)
                    .where(
                        FinalCutPackageSnapshotModel.status == "preparing",
                        or_(
                            FinalCutPackageSnapshotModel.next_build_attempt_at.is_(None),
                            FinalCutPackageSnapshotModel.next_build_attempt_at <= utcnow(),
                        ),
                    )
                    .order_by(FinalCutPackageSnapshotModel.created_at, FinalCutPackageSnapshotModel.id)
                    .limit(batch_size)
                )
            )
        results = {"built_packages": 0, "failed_package_builds": 0, "skipped_package_builds": 0}
        for package_id in package_ids:
            status = _process_package_snapshot(package_id, settings)
            if status == "ready":
                results["built_packages"] += 1
            elif status == "failed":
                results["failed_package_builds"] += 1
            else:
                results["skipped_package_builds"] += 1
        return results


def _process_with_timeout(timeout_seconds: int) -> dict[str, int]:
    def raise_timeout(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError("package build cycle timed out")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return process_pending_packages()
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)


def _pulse_heartbeat(path: Path, stop: threading.Event) -> None:
    while not stop.wait(HEARTBEAT_REFRESH_SECONDS):
        path.touch()


def run_package_worker_loop(
    interval_seconds: int,
    *,
    cycle_timeout_seconds: int,
    heartbeat_path: Path = Path("/tmp/fj-package-worker-heartbeat"),
    status_path: Path = Path("/tmp/fj-package-worker-status"),
) -> Never:
    if not 1 <= interval_seconds <= MAX_WORKER_SECONDS:
        raise ValueError("interval_seconds is outside the supported range")
    if not 1 <= cycle_timeout_seconds <= MAX_WORKER_SECONDS:
        raise ValueError("cycle_timeout_seconds is outside the supported range")
    heartbeat_path.touch()
    status_path.write_text("running", encoding="ascii")
    while True:
        stop = threading.Event()
        pulse = threading.Thread(
            target=_pulse_heartbeat,
            args=(heartbeat_path, stop),
            daemon=True,
            name="package-worker-heartbeat",
        )
        pulse.start()
        try:
            result = _process_with_timeout(cycle_timeout_seconds)
        except Exception as exc:
            status_path.write_text("error", encoding="ascii")
            heartbeat_path.touch()
            print(json.dumps({"status": "error", "error_type": type(exc).__name__}, sort_keys=True), flush=True)
            raise RuntimeError("package worker cycle failed") from exc
        finally:
            stop.set()
            pulse.join(timeout=1)
        status = "degraded" if result["failed_package_builds"] else "ok"
        status_path.write_text(status, encoding="ascii")
        heartbeat_path.touch()
        print(json.dumps({"status": status, **result}, sort_keys=True), flush=True)
        remaining = interval_seconds
        while remaining > 0:
            duration = min(remaining, HEARTBEAT_REFRESH_SECONDS)
            time.sleep(duration)
            heartbeat_path.touch()
            remaining -= duration
        status_path.write_text("running", encoding="ascii")


def _bounded_seconds(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_WORKER_SECONDS:
        raise argparse.ArgumentTypeError(f"value must be between 1 and {MAX_WORKER_SECONDS}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--interval-seconds", type=_bounded_seconds, default=2)
    run_parser.add_argument("--cycle-timeout-seconds", type=_bounded_seconds, default=7200)
    args = parser.parse_args()
    if args.command == "once":
        print(json.dumps(process_pending_packages(), sort_keys=True), flush=True)
        return 0
    try:
        run_package_worker_loop(args.interval_seconds, cycle_timeout_seconds=args.cycle_timeout_seconds)
    except RuntimeError as exc:
        LOGGER.error("package_worker_stopped", extra={"error_type": type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
