from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FinalCutPackageSnapshotModel,
    FinalizationPackageInvalidationModel,
    FinalizationRecordModel,
    IdempotencyRecordModel,
    OutboxEventModel,
    ReviewItemModel,
    ReviewVersionModel,
)
from backend.app.settings import get_settings

from .test_task1_contract_and_migration import _alembic_config, _set_migration_env
from .test_task3_project_finalization_postgresql import (
    _bus,
    _context,
    _seed_finalization_state,
    _seed_shared_ready_package,
    _seed_two_item_project,
    _snapshot,
)
from .test_postgresql_constraints import (
    _drop_postgres_temp_database,
    _postgres_database_url,
    _postgres_temp_database,
    _prepare_postgresql_schema,
)


REVIEW_ITEM_FREEZE_TRIGGER = "trg_review_items_freeze_after_finalized"


def _sqlite_trigger_sql(db_path: Path, trigger_name: str) -> str:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            statement = connection.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :trigger_name",
                ),
                {"trigger_name": trigger_name},
            )
    finally:
        engine.dispose()
    assert isinstance(statement, str)
    return " ".join(statement.split())


def _drop_sqlite_trigger(db_path: Path, trigger_name: str) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(text(f'DROP TRIGGER "{trigger_name}"'))
    finally:
        engine.dispose()


def _postgres_function_sql(database_url: str, function_name: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            statement = connection.scalar(
                text(
                    "SELECT pg_get_functiondef(CAST(:function_name AS regprocedure))",
                ),
                {"function_name": f"{function_name}()"},
            )
    finally:
        engine.dispose()
    assert isinstance(statement, str)
    return " ".join(statement.split())


def _downgrade_postgresql(database_url: str, revision: str) -> None:
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        root = Path(__file__).resolve().parents[2]
        alembic_command.downgrade(_alembic_config(root), revision)
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()


def _assert_exact_revoke_inventory(statement: str) -> None:
    assert "NEW.workflow_status = 'in_review'" in statement
    assert "NEW.active_finalization_id IS NULL" in statement
    assert "NEW.current_version_id = OLD.current_version_id" in statement
    assert "NEW.current_version_is_current = OLD.current_version_is_current" in statement
    assert "NEW.lock_version = OLD.lock_version + 1" in statement


def _assert_strict_revoke_inventory(statement: str) -> None:
    assert "WHEN OLD.workflow_status = 'finalized' BEGIN" in statement
    assert "NEW.workflow_status = 'in_review'" not in statement


def _assert_exact_postgresql_revoke_inventory(statement: str) -> None:
    assert "NEW.workflow_status = 'in_review'" in statement
    assert "NEW.active_finalization_id IS NULL" in statement
    assert "NEW.current_version_id IS NOT DISTINCT FROM OLD.current_version_id" in statement
    assert "NEW.current_version_is_current IS NOT DISTINCT FROM OLD.current_version_is_current" in statement
    assert "NEW.lock_version = OLD.lock_version + 1" in statement


def _assert_strict_postgresql_revoke_inventory(statement: str) -> None:
    assert "IF OLD.workflow_status = 'finalized' THEN" in statement
    assert "NEW.workflow_status = 'in_review'" not in statement


def _assert_strict_revoke_commands_fail(
    database_url: str,
    project_id: str,
    item_ids: list[str],
    baseline: dict[str, Any],
) -> None:
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with SessionLocal() as session:
            with pytest.raises((DBAPIError, ReviewError)) as project_error:
                _bus(session).execute(
                    "RevokeProjectFinalization",
                    {"project_ref_id": project_id, "confirmed": True},
                    _context(project_id, f"task5-strict-project-{uuid.uuid4().hex}"),
                    baseline["project_lock"],
                    f"idem_task5_strict_project_{uuid.uuid4().hex}",
                )
            if isinstance(project_error.value, ReviewError):
                assert project_error.value.code == "RESOURCE_STATE_CONFLICT"
                assert isinstance(project_error.value.__cause__, DBAPIError)
            assert "finalized review item is frozen" in str(
                project_error.value.__cause__ or project_error.value,
            )
            session.rollback()
            with pytest.raises((DBAPIError, ReviewError)) as single_error:
                _bus(session).execute(
                    "RevokeFinalization",
                    {
                        "project_ref_id": project_id,
                        "review_item_id": item_ids[0],
                        "confirmed": True,
                    },
                    _context(project_id, f"task5-strict-single-{uuid.uuid4().hex}"),
                    baseline["items"][item_ids[0]][3],
                    f"idem_task5_strict_single_{uuid.uuid4().hex}",
                )
            if isinstance(single_error.value, ReviewError):
                assert single_error.value.code == "RESOURCE_STATE_CONFLICT"
                assert isinstance(single_error.value.__cause__, DBAPIError)
            assert "finalized review item is frozen" in str(
                single_error.value.__cause__ or single_error.value,
            )
            session.rollback()
            assert _snapshot(session, project_id) == baseline
    finally:
        engine.dispose()


def test_0025_restores_0020_exact_sqlite_revoke_transition_after_0024_strict_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task5-finalization-revoke-governance.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)

    alembic_command.upgrade(config, "20260731_0020")
    exact_0020 = _sqlite_trigger_sql(
        db_path,
        REVIEW_ITEM_FREEZE_TRIGGER,
    )
    _assert_exact_revoke_inventory(exact_0020)

    alembic_command.upgrade(config, "20260824_0023")
    _drop_sqlite_trigger(db_path, REVIEW_ITEM_FREEZE_TRIGGER)

    alembic_command.upgrade(config, "20260824_0024")
    strict_0024 = _sqlite_trigger_sql(
        db_path,
        REVIEW_ITEM_FREEZE_TRIGGER,
    )
    _assert_strict_revoke_inventory(strict_0024)

    alembic_command.upgrade(config, "20260825_0025")
    assert (
        _sqlite_trigger_sql(
            db_path,
            REVIEW_ITEM_FREEZE_TRIGGER,
        )
        == exact_0020
    )

    alembic_command.downgrade(config, "20260824_0024")
    assert _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER) == strict_0024
    alembic_command.upgrade(config, "20260825_0025")
    assert _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER) == exact_0020


def test_0025_clean_sqlite_0024_upgrade_preserves_0020_exact_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task5-clean-0024-to-0025.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)

    alembic_command.upgrade(config, "20260731_0020")
    exact_0020 = _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER)
    alembic_command.upgrade(config, "20260824_0024")
    assert _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER) == exact_0020

    alembic_command.upgrade(config, "20260825_0025")
    assert _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER) == exact_0020


def test_0025_sqlite_strict_shape_blocks_revoke_then_exact_shape_allows_real_commands_and_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task5-sqlite-revoke-behavior.db"
    database_url = f"sqlite:///{db_path}"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    file_paths: list[Path] = []
    package_path: Path | None = None

    alembic_command.upgrade(config, "20260824_0023")
    _drop_sqlite_trigger(db_path, REVIEW_ITEM_FREEZE_TRIGGER)
    alembic_command.upgrade(config, "20260824_0024")
    _assert_strict_revoke_inventory(
        _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER),
    )

    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with SessionLocal() as seed:
            prefix = f"sqlite_strict_{uuid.uuid4().hex[:12]}"
            project_id, item_ids, version_ids, paths = _seed_two_item_project(
                seed,
                prefix,
            )
            file_paths.extend(paths)
            _seed_finalization_state(
                seed,
                project_id,
                item_ids,
                version_ids,
                "all",
            )
            package_id, package_path = _seed_shared_ready_package(
                seed,
                project_id,
                prefix,
            )
            baseline = _snapshot(seed, project_id)
    finally:
        engine.dispose()

    _assert_strict_revoke_commands_fail(
        database_url,
        project_id,
        item_ids,
        baseline,
    )

    alembic_command.upgrade(config, "20260825_0025")
    _assert_exact_revoke_inventory(
        _sqlite_trigger_sql(db_path, REVIEW_ITEM_FREEZE_TRIGGER),
    )

    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with SessionLocal() as session:
            project_revoke_key = f"idem_task5_project_{uuid.uuid4().hex}"
            _bus(session).execute(
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                _context(project_id, f"task5-project-{uuid.uuid4().hex}"),
                baseline["project_lock"],
                project_revoke_key,
            )
            session.commit()
            after = _snapshot(session, project_id)

            assert after["project_lock"] == baseline["project_lock"] + 1
            assert all(
                after["items"][item_id][0] == "in_review"
                and after["items"][item_id][1] is None
                and after["items"][item_id][3] == baseline["items"][item_id][3] + 1
                for item_id in item_ids
            )
            finalizations = list(
                session.scalars(
                    select(FinalizationRecordModel)
                    .where(
                        FinalizationRecordModel.project_ref_id == project_id,
                    )
                    .order_by(FinalizationRecordModel.review_item_id),
                ),
            )
            assert len(finalizations) == 2
            assert all(row.status == "revoked" and row.revoked_at is not None for row in finalizations)
            package = session.get(FinalCutPackageSnapshotModel, package_id)
            assert package is not None and package.status == "invalidated"
            invalidations = list(
                session.scalars(
                    select(FinalizationPackageInvalidationModel).where(
                        FinalizationPackageInvalidationModel.package_id == package_id,
                    ),
                ),
            )
            assert len(invalidations) == 2
            assert {row.cleanup_status for row in invalidations} == {"pending"}
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventModel)
                    .where(
                        OutboxEventModel.project_ref_id == project_id,
                        OutboxEventModel.event_type == "review.finalization.revoked",
                    ),
                )
                == 2
            )
            record = session.get(IdempotencyRecordModel, project_revoke_key)
            assert record is not None and record.status_code == 200

            before_replay = _snapshot(session, project_id)
            _bus(session).execute(
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                _context(project_id, f"task5-replay-{uuid.uuid4().hex}"),
                baseline["project_lock"],
                project_revoke_key,
            )
            session.commit()
            assert _snapshot(session, project_id) == before_replay

        with SessionLocal() as single:
            prefix = f"sqlite_single_{uuid.uuid4().hex[:12]}"
            single_project_id, single_item_ids, single_version_ids, paths = _seed_two_item_project(single, prefix)
            file_paths.extend(paths)
            _seed_finalization_state(
                single,
                single_project_id,
                single_item_ids,
                single_version_ids,
                "mixed",
            )
            single_baseline = _snapshot(single, single_project_id)
            single_key = f"idem_task5_single_{uuid.uuid4().hex}"
            _bus(single).execute(
                "RevokeFinalization",
                {
                    "project_ref_id": single_project_id,
                    "review_item_id": single_item_ids[0],
                    "confirmed": True,
                },
                _context(
                    single_project_id,
                    f"task5-single-{uuid.uuid4().hex}",
                ),
                single_baseline["items"][single_item_ids[0]][3],
                single_key,
            )
            single.commit()
            single_after = _snapshot(single, single_project_id)
            assert single_after["project_lock"] == single_baseline["project_lock"]
            assert single_after["items"][single_item_ids[0]][0] == "in_review"
            assert single_after["items"][single_item_ids[0]][1] is None
            assert single_after["items"][single_item_ids[0]][3] == single_baseline["items"][single_item_ids[0]][3] + 1
            assert single.get(IdempotencyRecordModel, single_key) is not None

        with SessionLocal() as guarded:
            prefix = f"sqlite_guard_{uuid.uuid4().hex[:12]}"
            guard_project_id, guard_item_ids, guard_version_ids, paths = _seed_two_item_project(guarded, prefix)
            file_paths.extend(paths)
            _seed_finalization_state(
                guarded,
                guard_project_id,
                guard_item_ids,
                guard_version_ids,
                "mixed",
            )
            guarded_item = guarded.get(ReviewItemModel, guard_item_ids[0])
            assert guarded_item is not None
            guarded_item.workflow_status = "in_review"
            guarded_item.active_finalization_id = None
            guarded_item.title = "non-exact mutation"
            guarded_item.lock_version += 1
            with pytest.raises(
                DBAPIError,
                match="finalized review item is frozen",
            ):
                guarded.commit()
            guarded.rollback()

            guarded_version = guarded.get(
                ReviewVersionModel,
                guard_version_ids[0],
            )
            assert guarded_version is not None
            guarded_version.original_filename = "child-mutation.mp4"
            with pytest.raises(
                DBAPIError,
                match="finalized review item is frozen",
            ):
                guarded.commit()
    finally:
        engine.dispose()
        if package_path is not None:
            package_path.unlink(missing_ok=True)
        for file_path in file_paths:
            file_path.unlink(missing_ok=True)


def test_0025_postgresql_random_database_restores_real_revoke_commands_and_roundtrips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_database_url = _postgres_database_url()
    database = _postgres_temp_database(source_database_url)
    function_name = "fcr_check_review_item_refs_and_freeze"
    file_paths: list[Path] = []
    package_path: Path | None = None
    engine = None
    try:
        monkeypatch.setenv("DATABASE_URL", database.url)
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "pg-storage"))
        monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "pg-packages"))
        monkeypatch.setenv(
            "WRITE_GUARD_SESSION_SECRET",
            "task5-pg-gate-signing-secret-0123456789abcdef",
        )
        get_settings.cache_clear()

        _prepare_postgresql_schema(database.url, "20260731_0020")
        exact_0020 = _postgres_function_sql(database.url, function_name)
        _assert_exact_postgresql_revoke_inventory(exact_0020)

        _prepare_postgresql_schema(database.url, "20260824_0024")
        strict_0024 = _postgres_function_sql(database.url, function_name)
        _assert_strict_postgresql_revoke_inventory(strict_0024)

        engine = create_engine(database.url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as seed:
            prefix = f"pg_strict_{uuid.uuid4().hex[:12]}"
            project_id, item_ids, version_ids, paths = _seed_two_item_project(
                seed,
                prefix,
            )
            file_paths.extend(paths)
            _seed_finalization_state(
                seed,
                project_id,
                item_ids,
                version_ids,
                "all",
            )
            package_id, package_path = _seed_shared_ready_package(
                seed,
                project_id,
                prefix,
            )
            baseline = _snapshot(seed, project_id)
        engine.dispose()
        engine = None

        _assert_strict_revoke_commands_fail(
            database.url,
            project_id,
            item_ids,
            baseline,
        )

        _prepare_postgresql_schema(database.url, "20260825_0025")
        assert _postgres_function_sql(database.url, function_name) == exact_0020

        engine = create_engine(database.url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            project_key = f"idem_task5_pg_project_{uuid.uuid4().hex}"
            _bus(session).execute(
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                _context(project_id, f"task5-pg-project-{uuid.uuid4().hex}"),
                baseline["project_lock"],
                project_key,
            )
            session.commit()
            after = _snapshot(session, project_id)
            assert after["project_lock"] == baseline["project_lock"] + 1
            assert all(
                after["items"][item_id][0] == "in_review"
                and after["items"][item_id][1] is None
                and after["items"][item_id][3] == baseline["items"][item_id][3] + 1
                for item_id in item_ids
            )
            finalizations = list(
                session.scalars(
                    select(FinalizationRecordModel)
                    .where(
                        FinalizationRecordModel.project_ref_id == project_id,
                    )
                    .order_by(FinalizationRecordModel.review_item_id),
                ),
            )
            assert all(row.status == "revoked" and row.revoked_at is not None for row in finalizations)
            package = session.get(FinalCutPackageSnapshotModel, package_id)
            assert package is not None and package.status == "invalidated"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(FinalizationPackageInvalidationModel)
                    .where(
                        FinalizationPackageInvalidationModel.package_id == package_id,
                    ),
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventModel)
                    .where(
                        OutboxEventModel.project_ref_id == project_id,
                        OutboxEventModel.event_type == "review.finalization.revoked",
                    ),
                )
                == 2
            )
            record = session.get(IdempotencyRecordModel, project_key)
            assert record is not None and record.status_code == 200

            before_replay = _snapshot(session, project_id)
            _bus(session).execute(
                "RevokeProjectFinalization",
                {"project_ref_id": project_id, "confirmed": True},
                _context(project_id, f"task5-pg-replay-{uuid.uuid4().hex}"),
                baseline["project_lock"],
                project_key,
            )
            session.commit()
            assert _snapshot(session, project_id) == before_replay

        with SessionLocal() as single:
            prefix = f"pg_single_{uuid.uuid4().hex[:12]}"
            single_project_id, single_item_ids, single_version_ids, paths = _seed_two_item_project(single, prefix)
            file_paths.extend(paths)
            _seed_finalization_state(
                single,
                single_project_id,
                single_item_ids,
                single_version_ids,
                "mixed",
            )
            single_baseline = _snapshot(single, single_project_id)
            single_key = f"idem_task5_pg_single_{uuid.uuid4().hex}"
            _bus(single).execute(
                "RevokeFinalization",
                {
                    "project_ref_id": single_project_id,
                    "review_item_id": single_item_ids[0],
                    "confirmed": True,
                },
                _context(
                    single_project_id,
                    f"task5-pg-single-{uuid.uuid4().hex}",
                ),
                single_baseline["items"][single_item_ids[0]][3],
                single_key,
            )
            single.commit()
            single_after = _snapshot(single, single_project_id)
            assert single_after["items"][single_item_ids[0]][0] == "in_review"
            assert single_after["items"][single_item_ids[0]][1] is None
            assert single.get(IdempotencyRecordModel, single_key) is not None

        with SessionLocal() as guarded:
            prefix = f"pg_guard_{uuid.uuid4().hex[:12]}"
            guard_project_id, guard_item_ids, guard_version_ids, paths = _seed_two_item_project(guarded, prefix)
            file_paths.extend(paths)
            _seed_finalization_state(
                guarded,
                guard_project_id,
                guard_item_ids,
                guard_version_ids,
                "mixed",
            )
            guarded_item = guarded.get(ReviewItemModel, guard_item_ids[0])
            assert guarded_item is not None
            guarded_item.workflow_status = "in_review"
            guarded_item.active_finalization_id = None
            guarded_item.title = "non-exact pg mutation"
            guarded_item.lock_version += 1
            with pytest.raises(DBAPIError, match="finalized review item is frozen"):
                guarded.commit()
            guarded.rollback()

            guarded_version = guarded.get(
                ReviewVersionModel,
                guard_version_ids[0],
            )
            assert guarded_version is not None
            guarded_version.original_filename = "pg-child-mutation.mp4"
            with pytest.raises(DBAPIError, match="finalized review item is frozen"):
                guarded.commit()

        engine.dispose()
        engine = None
        _downgrade_postgresql(database.url, "20260824_0024")
        assert _postgres_function_sql(database.url, function_name) == strict_0024

        engine = create_engine(database.url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as seed:
            prefix = f"pg_roundtrip_{uuid.uuid4().hex[:12]}"
            roundtrip_project_id, roundtrip_item_ids, roundtrip_version_ids, paths = _seed_two_item_project(seed, prefix)
            file_paths.extend(paths)
            _seed_finalization_state(
                seed,
                roundtrip_project_id,
                roundtrip_item_ids,
                roundtrip_version_ids,
                "all",
            )
            roundtrip_baseline = _snapshot(seed, roundtrip_project_id)
        engine.dispose()
        engine = None

        _assert_strict_revoke_commands_fail(
            database.url,
            roundtrip_project_id,
            roundtrip_item_ids,
            roundtrip_baseline,
        )
        _prepare_postgresql_schema(database.url, "20260825_0025")
        assert _postgres_function_sql(database.url, function_name) == exact_0020

        engine = create_engine(database.url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        with SessionLocal() as session:
            _bus(session).execute(
                "RevokeProjectFinalization",
                {
                    "project_ref_id": roundtrip_project_id,
                    "confirmed": True,
                },
                _context(
                    roundtrip_project_id,
                    f"task5-pg-roundtrip-{uuid.uuid4().hex}",
                ),
                roundtrip_baseline["project_lock"],
                f"idem_task5_pg_roundtrip_{uuid.uuid4().hex}",
            )
            session.commit()
            roundtrip_after = _snapshot(session, roundtrip_project_id)
            assert all(roundtrip_after["items"][item_id][0] == "in_review" and roundtrip_after["items"][item_id][1] is None for item_id in roundtrip_item_ids)
    finally:
        if engine is not None:
            engine.dispose()
        if package_path is not None:
            package_path.unlink(missing_ok=True)
        for file_path in file_paths:
            file_path.unlink(missing_ok=True)
        get_settings.cache_clear()
        _drop_postgres_temp_database(database)
