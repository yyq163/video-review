from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.modules.final_cut_review.application.command_handlers import CommandBus
from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FinalCutPackageSnapshotModel,
    FinalizationPackageInvalidationModel,
    FinalizationRecordModel,
    IdempotencyRecordModel,
    OutboxEventModel,
    ProjectRefModel,
    ReviewItemModel,
    utcnow,
)
from backend.app.modules.review_access.policies import (
    ConfiguredWriteGuardAdapter,
    NoAccountAuthorizationAdapter,
    StaticEntryPolicyAdapter,
    WriteGuardSessionSigner,
)
from backend.app.settings import get_settings

from .test_postgresql_constraints import _file, _postgres_database_url, _prepare_postgresql_schema, _suffix, _version


@dataclass(frozen=True)
class ConcurrentCommand:
    label: str
    command_type: str
    payload: dict[str, Any]
    expected_version: int
    idempotency_key: str


def _context(project_id: str, request_id: str) -> ExecutionContext:
    return ExecutionContext(
        entry_source="review",
        request_id=request_id,
        principal=PrincipalRef(kind="system", id="task3-postgresql-concurrency", project_ref_ids=(project_id,)),
        write_guard=WriteGuardState(mode="none", verified=True),
    )


def _bus(session: Session) -> CommandBus:
    settings = get_settings()
    return CommandBus(
        repository=SqlAlchemyReviewRepository(session, settings),
        entry_policy=StaticEntryPolicyAdapter(),
        write_guard=ConfiguredWriteGuardAdapter(settings, WriteGuardSessionSigner(settings)),
        principal_authorization=NoAccountAuthorizationAdapter(),
    )


def _seed_two_item_project(session: Session, prefix: str) -> tuple[str, list[str], list[str], list[Path]]:
    project = ProjectRefModel(
        id=f"prj_{prefix}", project_code=f"TASK3PG_{prefix}", project_name=f"Task 3 PG {prefix}", fps_num=25, fps_den=1
    )
    files = [_file(f"file_{uuid.uuid4().hex}") for _index in range(2)]
    items = [
        ReviewItemModel(
            id=f"item_{prefix}_{index}",
            project_ref_id=project.id,
            item_code=f"TASK3PG_{prefix}_{index}",
            episode_no=index + 1,
            title=f"Task 3 PG item {index}",
        )
        for index in range(2)
    ]
    session.add_all([project, *files, *items])
    session.flush()
    versions = [
        _version(f"ver_{prefix}_{index}", project.id, items[index].id, files[index], 1, True)
        for index in range(2)
    ]
    session.add_all(versions)
    session.flush()
    for item, version in zip(items, versions, strict=True):
        item.current_version_id = version.id
    session.commit()
    return (
        project.id,
        [item.id for item in items],
        [version.id for version in versions],
        [Path(file.storage_path) for file in files],
    )


def _seed_finalization_state(
    session: Session, project_id: str, item_ids: list[str], version_ids: list[str], mode: str
) -> None:
    repository = SqlAlchemyReviewRepository(session, get_settings())
    if mode == "none":
        return
    if mode == "all":
        repository.execute(
            "FinalizeProject",
            {"project_ref_id": project_id, "confirmed": True},
            _context(project_id, f"seed-all-{project_id}"),
            1,
        )
    elif mode == "mixed":
        repository.execute(
            "FinalizeVersion",
            {
                "project_ref_id": project_id,
                "review_item_id": item_ids[0],
                "version_id": version_ids[0],
                "confirmed": True,
            },
            _context(project_id, f"seed-mixed-{project_id}"),
            1,
        )
    else:
        raise AssertionError(f"unknown seed mode {mode}")
    session.commit()


def _seed_shared_ready_package(session: Session, project_id: str, prefix: str) -> tuple[str, Path]:
    finalizations = list(
        session.scalars(
            select(FinalizationRecordModel)
            .where(FinalizationRecordModel.project_ref_id == project_id, FinalizationRecordModel.status == "active")
            .order_by(FinalizationRecordModel.review_item_id)
        )
    )
    assert len(finalizations) == 2
    package_id = f"pkg_{uuid.uuid4().hex}"
    package_root = get_settings().package_root
    package_root.mkdir(parents=True, exist_ok=True)
    package_path = package_root / f"{package_id}.zip"
    package_bytes = f"task3-shared-ready-package-{prefix}".encode()
    package_path.write_bytes(package_bytes)
    session.add(
        FinalCutPackageSnapshotModel(
            id=package_id,
            project_ref_id=project_id,
            status="ready",
            package_filename=f"{prefix}.zip",
            storage_path=str(package_path),
            expires_at=utcnow() + timedelta(hours=1),
            total_bytes=sum(row.file_size for row in finalizations),
            storage_bytes=len(package_bytes),
            sha256=hashlib.sha256(package_bytes).hexdigest(),
            items=[
                {"review_item_id": row.review_item_id, "version_id": row.version_id, "finalization_id": row.id}
                for row in finalizations
            ],
        )
    )
    session.commit()
    return package_id, package_path


def _snapshot(session: Session, project_id: str) -> dict[str, Any]:
    project = session.get(ProjectRefModel, project_id)
    assert project is not None
    items = list(
        session.scalars(
            select(ReviewItemModel).where(ReviewItemModel.project_ref_id == project_id).order_by(ReviewItemModel.id)
        )
    )
    finalizations = list(
        session.scalars(
            select(FinalizationRecordModel)
            .where(FinalizationRecordModel.project_ref_id == project_id)
            .order_by(FinalizationRecordModel.review_item_id, FinalizationRecordModel.id)
        )
    )
    events = list(
        session.scalars(select(OutboxEventModel).where(OutboxEventModel.project_ref_id == project_id).order_by(OutboxEventModel.id))
    )
    packages = list(
        session.scalars(
            select(FinalCutPackageSnapshotModel)
            .where(FinalCutPackageSnapshotModel.project_ref_id == project_id)
            .order_by(FinalCutPackageSnapshotModel.id)
        )
    )
    invalidations = list(
        session.scalars(
            select(FinalizationPackageInvalidationModel)
            .where(FinalizationPackageInvalidationModel.project_ref_id == project_id)
            .order_by(FinalizationPackageInvalidationModel.finalization_id)
        )
    )
    return {
        "project_lock": project.lock_version,
        "items": {
            item.id: (item.workflow_status, item.active_finalization_id, item.current_version_id, item.lock_version)
            for item in items
        },
        "finalizations": tuple((row.id, row.review_item_id, row.version_id, row.status) for row in finalizations),
        "events": tuple((row.event_id, row.event_type, row.review_item_id, row.finalization_id) for row in events),
        "packages": tuple((row.id, row.status, row.storage_reclaimed_at) for row in packages),
        "invalidations": tuple(
            (row.id, row.review_item_id, row.finalization_id, row.package_id, row.cleanup_status)
            for row in invalidations
        ),
    }


def _run_forced_project_lock_contention(
    engine: Any,
    session_factory: sessionmaker[Session],
    project_id: str,
    command_a: ConcurrentCommand,
    command_b: ConcurrentCommand,
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, int]]:
    a_holds_project_lock = threading.Event()
    release_a = threading.Event()
    b_issued_project_lock = threading.Event()
    results: dict[str, str] = {}
    errors: list[BaseException] = []
    traces: dict[str, list[str]] = {"a": [], "b": []}
    backend_pids: dict[str, int] = {}
    shared_lock = threading.Lock()

    def run(label: str, target: ConcurrentCommand) -> None:
        with session_factory() as session:
            connection = session.connection()
            connection.execute(text("SET LOCAL lock_timeout = '7s'"))
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            backend_pids[label] = int(connection.scalar(text("SELECT pg_backend_pid()")))
            project_lock_observed = False

            def before_cursor_execute(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                normalized = " ".join(statement.split())
                if "FOR UPDATE" not in normalized:
                    return
                traces[label].append(normalized)
                if label == "b" and "FROM project_refs" in normalized:
                    b_issued_project_lock.set()

            def after_cursor_execute(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                nonlocal project_lock_observed
                normalized = " ".join(statement.split())
                if label == "a" and not project_lock_observed and "FROM project_refs" in normalized and "FOR UPDATE" in normalized:
                    project_lock_observed = True
                    a_holds_project_lock.set()
                    if not release_a.wait(timeout=6):
                        raise TimeoutError("thread A project-lock pause exceeded 6 seconds")

            event.listen(connection, "before_cursor_execute", before_cursor_execute)
            event.listen(connection, "after_cursor_execute", after_cursor_execute)
            try:
                try:
                    _bus(session).execute(
                        target.command_type,
                        target.payload,
                        _context(project_id, f"task3-pg-{target.label}-{uuid.uuid4().hex}"),
                        target.expected_version,
                        target.idempotency_key,
                    )
                    session.commit()
                    outcome = "ok"
                except ReviewError as exc:
                    session.rollback()
                    outcome = exc.code
                with shared_lock:
                    results[label] = outcome
            except BaseException as exc:
                session.rollback()
                with shared_lock:
                    errors.append(exc)
            finally:
                event.remove(connection, "before_cursor_execute", before_cursor_execute)
                event.remove(connection, "after_cursor_execute", after_cursor_execute)

    thread_a = threading.Thread(target=run, args=("a", command_a), daemon=True)
    thread_b = threading.Thread(target=run, args=("b", command_b), daemon=True)
    thread_a.start()
    a_lock_seen = a_holds_project_lock.wait(timeout=5)
    if a_lock_seen:
        thread_b.start()
    b_lock_issued = a_lock_seen and b_issued_project_lock.wait(timeout=5)
    lock_wait_observed = False
    observed_wait: tuple[str | None, str | None, str | None, str | None] | None = None
    if b_lock_issued:
        deadline = time.monotonic() + 5
        with engine.connect() as observer:
            while time.monotonic() < deadline:
                row = observer.execute(
                    text("SELECT wait_event_type, wait_event, state, query FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": backend_pids["b"]},
                ).one_or_none()
                observed_wait = tuple(row) if row is not None else None
                if row is not None and row[0] == "Lock":
                    lock_wait_observed = True
                    break
                time.sleep(0.02)
    release_a.set()
    thread_a.join(timeout=12)
    if thread_b.ident is not None:
        thread_b.join(timeout=12)

    assert a_lock_seen, "thread A never acquired the project FOR UPDATE lock"
    assert b_lock_issued, "thread B never issued the contending project FOR UPDATE lock"
    assert lock_wait_observed, f"third connection did not observe PostgreSQL Lock wait: {observed_wait!r}"
    assert observed_wait is not None and observed_wait[3] is not None
    assert "project_refs" in observed_wait[3] and "FOR UPDATE" in observed_wait[3]
    assert not thread_a.is_alive() and not thread_b.is_alive(), "PostgreSQL race did not terminate within bounded join"
    assert errors == []
    assert set(results) == {"a", "b"}
    assert set(backend_pids) == {"a", "b"}
    assert backend_pids["a"] != backend_pids["b"]
    return results, traces, backend_pids


def _assert_lock_traces(traces: dict[str, list[str]]) -> None:
    for label in ("a", "b"):
        assert traces[label], f"connection {label} did not record a business FOR UPDATE"
        assert "FROM project_refs" in traces[label][0], f"connection {label} first business lock was not project: {traces[label][0]}"
    project_trace = traces["a"]
    project_index = next(index for index, sql in enumerate(project_trace) if "FROM project_refs" in sql)
    item_index = next(
        index
        for index, sql in enumerate(project_trace)
        if "FROM review_items" in sql and "ORDER BY review_items.id" in sql
    )
    finalization_index = next(
        index
        for index, sql in enumerate(project_trace)
        if "FROM finalizations" in sql and "ORDER BY finalizations.review_item_id" in sql
    )
    assert project_index < item_index < finalization_index


def _assert_authority(snapshot: dict[str, Any]) -> None:
    finalizations = {
        row[0]: {"review_item_id": row[1], "version_id": row[2], "status": row[3]}
        for row in snapshot["finalizations"]
    }
    active_ids: set[str] = set()
    for item_id, (status, pointer, current_version_id, _lock_version) in snapshot["items"].items():
        assert (status == "finalized") == (pointer is not None)
        if pointer is None:
            continue
        active_ids.add(pointer)
        assert finalizations[pointer] == {"review_item_id": item_id, "version_id": current_version_id, "status": "active"}
    assert active_ids == {finalization_id for finalization_id, data in finalizations.items() if data["status"] == "active"}


@pytest.mark.parametrize("round_index", range(3))
@pytest.mark.parametrize(
    "scenario",
    ["project_finalize_vs_single_finalize", "project_revoke_vs_single_revoke", "project_finalize_vs_project_revoke"],
)
def test_postgresql_project_finalization_forced_lock_wait_has_exact_winner_and_state(
    scenario: str,
    round_index: int,
) -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url, pool_size=4, max_overflow=0)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    file_paths: list[Path] = []
    package_path: Path | None = None
    try:
        with SessionLocal() as seed:
            prefix = f"{_suffix()}_{round_index}_{scenario[-5:]}"
            project_id, item_ids, version_ids, file_paths = _seed_two_item_project(seed, prefix)
            seed_mode = {
                "project_finalize_vs_single_finalize": "none",
                "project_revoke_vs_single_revoke": "all",
                "project_finalize_vs_project_revoke": "mixed",
            }[scenario]
            _seed_finalization_state(seed, project_id, item_ids, version_ids, seed_mode)
            package_id: str | None = None
            if scenario == "project_revoke_vs_single_revoke":
                package_id, package_path = _seed_shared_ready_package(seed, project_id, prefix)

        with SessionLocal() as observer:
            baseline = _snapshot(observer, project_id)
            _assert_authority(baseline)

        if scenario == "project_finalize_vs_single_finalize":
            command_a = ConcurrentCommand(
                "project-finalize",
                "FinalizeProject",
                {"project_ref_id": project_id, "confirmed": True},
                baseline["project_lock"],
                f"idem_{prefix}_project_finalize",
            )
            command_b = ConcurrentCommand(
                "single-finalize",
                "FinalizeVersion",
                {
                    "project_ref_id": project_id,
                    "review_item_id": item_ids[0],
                    "version_id": version_ids[0],
                    "confirmed": True,
                },
                baseline["items"][item_ids[0]][3],
                f"idem_{prefix}_single_finalize",
            )
        elif scenario == "project_revoke_vs_single_revoke":
            command_a = ConcurrentCommand(
                "project-revoke",
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                baseline["project_lock"],
                f"idem_{prefix}_project_revoke",
            )
            command_b = ConcurrentCommand(
                "single-revoke",
                "RevokeFinalization",
                {"project_ref_id": project_id, "review_item_id": item_ids[0], "confirmed": True},
                baseline["items"][item_ids[0]][3],
                f"idem_{prefix}_single_revoke",
            )
        else:
            command_a = ConcurrentCommand(
                "project-finalize",
                "FinalizeProject",
                {"project_ref_id": project_id, "confirmed": True},
                baseline["project_lock"],
                f"idem_{prefix}_project_finalize",
            )
            command_b = ConcurrentCommand(
                "project-revoke",
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                baseline["project_lock"],
                f"idem_{prefix}_project_revoke",
            )

        results, traces, _backend_pids = _run_forced_project_lock_contention(
            engine, SessionLocal, project_id, command_a, command_b
        )
        assert results == {"a": "ok", "b": "OPTIMISTIC_LOCK_CONFLICT"}
        _assert_lock_traces(traces)

        with SessionLocal() as observer:
            after = _snapshot(observer, project_id)
            _assert_authority(after)
            winner_record = observer.get(IdempotencyRecordModel, command_a.idempotency_key)
            loser_record = observer.get(IdempotencyRecordModel, command_b.idempotency_key)
            assert winner_record is not None and winner_record.status_code == 200
            assert loser_record is None

        baseline_events = set(baseline["events"])
        event_delta = [event_row for event_row in after["events"] if event_row not in baseline_events]
        assert after["project_lock"] == baseline["project_lock"] + 1

        if scenario == "project_finalize_vs_single_finalize":
            assert len(after["finalizations"]) == 2
            assert all(row[3] == "active" for row in after["finalizations"])
            assert all(after["items"][item_id][0] == "finalized" for item_id in item_ids)
            assert all(after["items"][item_id][3] == baseline["items"][item_id][3] + 1 for item_id in item_ids)
            assert [row[1] for row in event_delta] == ["review.version.finalized", "review.version.finalized"]
            assert after["packages"] == baseline["packages"] == ()
            assert after["invalidations"] == baseline["invalidations"] == ()
        elif scenario == "project_revoke_vs_single_revoke":
            assert package_id is not None
            assert len(after["finalizations"]) == len(baseline["finalizations"]) == 2
            assert all(row[3] == "revoked" for row in after["finalizations"])
            assert all(after["items"][item_id][0] == "in_review" for item_id in item_ids)
            assert all(after["items"][item_id][1] is None for item_id in item_ids)
            assert all(after["items"][item_id][3] == baseline["items"][item_id][3] + 1 for item_id in item_ids)
            assert [row[1] for row in event_delta] == ["review.finalization.revoked", "review.finalization.revoked"]
            assert after["packages"] == ((package_id, "invalidated", None),)
            assert len(after["invalidations"]) == 2
            finalization_items = {row[0]: row[1] for row in after["finalizations"]}
            assert {(row[1], row[2], row[3], row[4]) for row in after["invalidations"]} == {
                (finalization_items[finalization_id], finalization_id, package_id, "pending")
                for finalization_id in finalization_items
            }
        else:
            baseline_finalizations = {row[0]: row for row in baseline["finalizations"]}
            after_finalizations = {row[0]: row for row in after["finalizations"]}
            assert len(baseline_finalizations) == 1
            assert len(after_finalizations) == 2
            assert baseline_finalizations.items() <= after_finalizations.items()
            assert all(row[3] == "active" for row in after_finalizations.values())
            assert after["items"][item_ids[0]][3] == baseline["items"][item_ids[0]][3]
            assert after["items"][item_ids[1]][3] == baseline["items"][item_ids[1]][3] + 1
            assert all(after["items"][item_id][0] == "finalized" for item_id in item_ids)
            assert [row[1] for row in event_delta] == ["review.version.finalized"]
            assert not any(row[1] == "review.finalization.revoked" for row in event_delta)
            assert after["packages"] == baseline["packages"] == ()
            assert after["invalidations"] == baseline["invalidations"] == ()
    except DBAPIError as exc:
        pytest.fail(f"PostgreSQL project finalization concurrency raised DBAPIError: {exc}")
    finally:
        if package_path is not None:
            package_path.unlink(missing_ok=True)
        for file_path in file_paths:
            file_path.unlink(missing_ok=True)
        engine.dispose()
