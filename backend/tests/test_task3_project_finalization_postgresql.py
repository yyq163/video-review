from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.modules.final_cut_review.application.command_handlers import CommandBus
from backend.app.modules.final_cut_review.application.context import (
    ExecutionContext,
    PrincipalRef,
    WriteGuardState,
)
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FinalizationRecordModel,
    IdempotencyRecordModel,
    OutboxEventModel,
    ProjectRefModel,
    ReviewItemModel,
)
from backend.app.modules.review_access.policies import (
    ConfiguredWriteGuardAdapter,
    NoAccountAuthorizationAdapter,
    StaticEntryPolicyAdapter,
    WriteGuardSessionSigner,
)
from backend.app.settings import get_settings

from .test_postgresql_constraints import (
    _file,
    _postgres_database_url,
    _prepare_postgresql_schema,
    _suffix,
    _version,
)


@dataclass(frozen=True)
class ConcurrentCommand:
    command_type: str
    payload: dict[str, Any]
    expected_version: int
    idempotency_key: str


def _context(project_id: str, request_id: str) -> ExecutionContext:
    return ExecutionContext(
        entry_source="review",
        request_id=request_id,
        principal=PrincipalRef(
            kind="system",
            id="task3-postgresql-concurrency",
            project_ref_ids=(project_id,),
        ),
        write_guard=WriteGuardState(mode="none", verified=True),
    )


def _bus(session: Session) -> CommandBus:
    settings = get_settings()
    return CommandBus(
        repository=SqlAlchemyReviewRepository(session, settings),
        entry_policy=StaticEntryPolicyAdapter(),
        write_guard=ConfiguredWriteGuardAdapter(
            settings,
            WriteGuardSessionSigner(settings),
        ),
        principal_authorization=NoAccountAuthorizationAdapter(),
    )


def _seed_two_item_project(session: Session, prefix: str) -> tuple[str, list[str], list[str]]:
    project = ProjectRefModel(
        id=f"prj_{prefix}",
        project_code=f"TASK3PG_{prefix}",
        project_name=f"Task 3 PG {prefix}",
        fps_num=25,
        fps_den=1,
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
        _version(
            f"ver_{prefix}_{index}",
            project.id,
            items[index].id,
            files[index],
            1,
            True,
        )
        for index in range(2)
    ]
    session.add_all(versions)
    session.flush()
    for item, version in zip(items, versions, strict=True):
        item.current_version_id = version.id
    session.commit()
    return project.id, [item.id for item in items], [version.id for version in versions]


def _seed_finalization_state(
    session: Session,
    project_id: str,
    item_ids: list[str],
    version_ids: list[str],
    mode: str,
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


def _run_postgresql_concurrency(
    session_factory: sessionmaker[Session],
    project_id: str,
    commands: tuple[ConcurrentCommand, ConcurrentCommand],
) -> tuple[list[str], list[BaseException]]:
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def run(index: int, target: ConcurrentCommand) -> None:
        try:
            with session_factory() as session:
                session.execute(text("SET LOCAL lock_timeout = '5s'"))
                session.execute(text("SET LOCAL statement_timeout = '8s'"))
                barrier.wait(timeout=5)
                try:
                    _bus(session).execute(
                        target.command_type,
                        target.payload,
                        _context(project_id, f"task3-pg-race-{index}-{uuid.uuid4().hex}"),
                        target.expected_version,
                        target.idempotency_key,
                    )
                    session.commit()
                    outcome = "ok"
                except ReviewError as exc:
                    session.rollback()
                    outcome = exc.code
                with result_lock:
                    results.append(outcome)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(index, target), daemon=True)
        for index, target in enumerate(commands)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=12)
    if any(thread.is_alive() for thread in threads):
        errors.append(TimeoutError("PostgreSQL project finalization concurrency did not terminate within 12 seconds"))
    return results, errors


@pytest.mark.parametrize(
    "scenario",
    [
        "project_finalize_vs_single_finalize",
        "project_revoke_vs_single_revoke",
        "project_finalize_vs_project_revoke",
    ],
)
def test_postgresql_project_finalization_commands_serialize_without_deadlock_or_mixed_state(
    scenario: str,
) -> None:
    database_url = _postgres_database_url()
    _prepare_postgresql_schema(database_url)
    engine = create_engine(database_url, pool_size=3, max_overflow=0)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    statements: dict[int, list[str]] = {}
    statement_lock = threading.Lock()

    @event.listens_for(engine, "before_cursor_execute")
    def record_statement(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split())
        if "FOR UPDATE" in normalized:
            with statement_lock:
                statements.setdefault(id(connection), []).append(normalized)

    try:
        with SessionLocal() as seed:
            prefix = f"{_suffix()}_{scenario[-8:]}"
            project_id, item_ids, version_ids = _seed_two_item_project(seed, prefix)
            seed_mode = {
                "project_finalize_vs_single_finalize": "none",
                "project_revoke_vs_single_revoke": "all",
                "project_finalize_vs_project_revoke": "mixed",
            }[scenario]
            _seed_finalization_state(seed, project_id, item_ids, version_ids, seed_mode)

        with SessionLocal() as observer:
            project = observer.get(ProjectRefModel, project_id)
            items = {
                item.id: item
                for item in observer.scalars(
                    select(ReviewItemModel).where(ReviewItemModel.project_ref_id == project_id)
                )
            }
            assert project is not None
            if scenario == "project_finalize_vs_single_finalize":
                commands = (
                    ConcurrentCommand(
                        "FinalizeProject",
                        {"project_ref_id": project_id, "confirmed": True},
                        project.lock_version,
                        f"idem_{prefix}_project_finalize",
                    ),
                    ConcurrentCommand(
                        "FinalizeVersion",
                        {
                            "project_ref_id": project_id,
                            "review_item_id": item_ids[0],
                            "version_id": version_ids[0],
                            "confirmed": True,
                        },
                        items[item_ids[0]].lock_version,
                        f"idem_{prefix}_single_finalize",
                    ),
                )
            elif scenario == "project_revoke_vs_single_revoke":
                commands = (
                    ConcurrentCommand(
                        "RevokeProjectFinalization",
                        {"project_ref_id": project_id, "confirmed": True},
                        project.lock_version,
                        f"idem_{prefix}_project_revoke",
                    ),
                    ConcurrentCommand(
                        "RevokeFinalization",
                        {
                            "project_ref_id": project_id,
                            "review_item_id": item_ids[0],
                            "confirmed": True,
                        },
                        items[item_ids[0]].lock_version,
                        f"idem_{prefix}_single_revoke",
                    ),
                )
            else:
                commands = (
                    ConcurrentCommand(
                        "FinalizeProject",
                        {"project_ref_id": project_id, "confirmed": True},
                        project.lock_version,
                        f"idem_{prefix}_project_finalize",
                    ),
                    ConcurrentCommand(
                        "RevokeProjectFinalization",
                        {"project_ref_id": project_id, "confirmed": True},
                        project.lock_version,
                        f"idem_{prefix}_project_revoke",
                    ),
                )

        with statement_lock:
            statements.clear()
        results, errors = _run_postgresql_concurrency(
            SessionLocal,
            project_id,
            commands,
        )
        assert errors == []
        assert len(results) == 2
        assert all(
            result in {"ok", "OPTIMISTIC_LOCK_CONFLICT", "RESOURCE_STATE_CONFLICT", "REVIEW_ITEM_FINALIZED"}
            for result in results
        )

        project_lock_sequences = [
            sequence
            for sequence in statements.values()
            if any("FROM review_items" in sql and "ORDER BY review_items.id" in sql for sql in sequence)
        ]
        assert project_lock_sequences
        for sequence in project_lock_sequences:
            project_index = next(index for index, sql in enumerate(sequence) if "FROM project_refs" in sql)
            item_index = next(
                index
                for index, sql in enumerate(sequence)
                if "FROM review_items" in sql and "ORDER BY review_items.id" in sql
            )
            finalization_index = next(
                index
                for index, sql in enumerate(sequence)
                if "FROM finalizations" in sql and "ORDER BY finalizations.review_item_id" in sql
            )
            assert project_index < item_index < finalization_index

        with SessionLocal() as observer:
            project = observer.get(ProjectRefModel, project_id)
            assert project is not None
            active_count = observer.scalar(
                select(func.count()).select_from(FinalizationRecordModel).where(
                    FinalizationRecordModel.project_ref_id == project_id,
                    FinalizationRecordModel.status == "active",
                )
            )
            finalized_item_count = observer.scalar(
                select(func.count()).select_from(ReviewItemModel).where(
                    ReviewItemModel.project_ref_id == project_id,
                    ReviewItemModel.workflow_status == "finalized",
                )
            )
            assert active_count == finalized_item_count
            records = list(
                observer.scalars(
                    select(IdempotencyRecordModel).where(
                        IdempotencyRecordModel.key.in_(tuple(command.idempotency_key for command in commands))
                    )
                )
            )
            assert all(record.status_code == 200 for record in records)
            assert len(records) == results.count("ok")

            if scenario == "project_finalize_vs_single_finalize":
                assert results.count("ok") in {1, 2}
                assert active_count == 2
                assert project.lock_version == 2
                assert observer.scalar(
                    select(func.count()).select_from(OutboxEventModel).where(
                        OutboxEventModel.project_ref_id == project_id,
                        OutboxEventModel.event_type == "review.version.finalized",
                    )
                ) == 2
            elif scenario == "project_revoke_vs_single_revoke":
                assert results.count("ok") in {1, 2}
                assert active_count == 0
                assert project.lock_version == 3
                assert observer.scalar(
                    select(func.count()).select_from(OutboxEventModel).where(
                        OutboxEventModel.project_ref_id == project_id,
                        OutboxEventModel.event_type == "review.finalization.revoked",
                    )
                ) == 2
            else:
                assert results.count("ok") == 1
                assert active_count in {0, 2}
                assert project.lock_version == 2
    except DBAPIError as exc:
        pytest.fail(f"PostgreSQL project finalization concurrency raised DBAPIError: {exc}")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
        engine.dispose()
