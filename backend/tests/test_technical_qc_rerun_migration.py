from __future__ import annotations

import os
import importlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from backend.app.settings import get_settings

from .test_postgresql_constraints import (
    _drop_postgres_temp_database,
    _postgres_database_url,
    _postgres_temp_database,
    _prepare_postgresql_schema,
    _seed_item,
    _suffix,
)
from .test_task1_contract_and_migration import _alembic_config, _set_migration_env


def test_0030_sqlite_defers_qc_finding_issue_identity_and_restores_on_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "technical-qc-rerun-migration.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)
    alembic_command.upgrade(config, "20260826_0029")

    alembic_command.upgrade(config, "20260902_0030")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        finding_table_sql = str(
            engine.connect()
            .execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='technical_qc_findings'"
                )
            )
            .scalar_one()
        ).upper()
        assert "DEFERRABLE INITIALLY DEFERRED" in finding_table_sql
        trigger_rows = engine.connect().execute(
            text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND name IN ("
                "'trg_review_issues_freeze_after_finalized_insert',"
                "'trg_review_issues_freeze_after_finalized_update',"
                "'trg_issue_revisions_freeze_after_finalized_insert',"
                "'trg_annotation_sets_freeze_after_finalized_insert'"
                ")"
            )
        ).all()
        assert len(trigger_rows) == 4
        assert all("TECHNICAL_QC_FINDINGS" in str(sql).upper() for _name, sql in trigger_rows)
    finally:
        engine.dispose()

    alembic_command.downgrade(config, "20260826_0029")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        finding_table_sql = str(
            engine.connect()
            .execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='technical_qc_findings'"
                )
            )
            .scalar_one()
        ).upper()
        assert "DEFERRABLE INITIALLY DEFERRED" not in finding_table_sql
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "20260902_0030")


def test_0030_postgresql_defers_qc_finding_issue_identity_round_trip() -> None:
    database_url = _postgres_database_url()
    temporary = _postgres_temp_database(database_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        _prepare_postgresql_schema(temporary.url, "20260826_0029")
        os.environ["DATABASE_URL"] = temporary.url
        get_settings.cache_clear()
        config = _alembic_config(Path(__file__).resolve().parents[2])

        alembic_command.upgrade(config, "20260902_0030")
        engine = create_engine(temporary.url)
        try:
            row = engine.connect().execute(
                text(
                    "SELECT condeferrable, condeferred "
                    "FROM pg_constraint "
                    "WHERE conname = 'fk_technical_qc_findings_issue_identity'"
                )
            ).one()
            assert row == (True, True)
        finally:
            engine.dispose()

        alembic_command.downgrade(config, "20260826_0029")
        engine = create_engine(temporary.url)
        try:
            row = engine.connect().execute(
                text(
                    "SELECT condeferrable, condeferred "
                    "FROM pg_constraint "
                    "WHERE conname = 'fk_technical_qc_findings_issue_identity'"
                )
            ).one()
            assert row == (False, False)
        finally:
            engine.dispose()

        alembic_command.upgrade(config, "20260902_0030")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(temporary)


def test_0030_postgresql_allows_only_claim_backed_finalized_qc_opinions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _postgres_database_url()
    temporary = _postgres_temp_database(database_url)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    get_settings.cache_clear()
    engine = None
    try:
        _prepare_postgresql_schema(temporary.url, "20260826_0029")
        engine = create_engine(temporary.url)
        PgSession = sessionmaker(bind=engine, expire_on_commit=False)
        models: Any = importlib.import_module(
            "backend.app.modules.final_cut_review.infra.sqlalchemy_models"
        )
        qc: Any = importlib.import_module("backend.app.technical_qc")

        with PgSession() as session:
            project, item, version, source = _seed_item(session, _suffix())
            derivative = models.MediaDerivativeTaskModel(
                id=f"mdt_{_suffix()}",
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                kind="playback_faststart",
                status="ready",
                output_file_id=source.id,
                attempts=1,
            )
            task = models.TechnicalQcTaskModel(
                id=f"tqc_{_suffix()}",
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                input_file_id=source.id,
                status="running",
                claim_id=f"claim_{_suffix()}",
                lease_expires_at=models.utcnow() + timedelta(minutes=5),
                processed_ms=0,
                total_ms=version.duration_ms,
                rule_version=qc.RULE_VERSION,
                attempts=1,
            )
            finalization = models.FinalizationRecordModel(
                id=f"fin_{_suffix()}",
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                version_no=version.version_no,
                original_file_id=source.id,
                original_filename=version.original_filename,
                mime_type=version.mime_type,
                file_size=version.file_size,
                sha256=version.sha256,
                duration_ms=version.duration_ms,
                width=version.width,
                height=version.height,
                fps_num=version.fps_num,
                fps_den=version.fps_den,
                media_probe_version=version.media_probe_version,
                status="active",
            )
            session.add_all([derivative, task, finalization])
            session.flush()
            item.workflow_status = "finalized"
            item.active_finalization_id = finalization.id
            session.commit()
            project_id = project.id
            item_id = item.id
            version_id = version.id
            task_id = task.id
            claim_id = task.claim_id
            finalization_id = finalization.id
            source_snapshot = (
                source.id,
                source.storage_path,
                source.sha256,
                source.file_size,
                source.width,
                source.height,
                source.fps_num,
                source.fps_den,
            )

        claim = qc.TechnicalQcClaim(
            task_id=task_id,
            claim_id=claim_id,
            project_ref_id=project_id,
            review_item_id=item_id,
            version_id=version_id,
            episode_no=1,
            input_file_id=source_snapshot[0],
            input_storage_path=source_snapshot[1],
            input_sha256=source_snapshot[2],
            input_size=source_snapshot[3],
            total_ms=1_000,
            width=source_snapshot[4],
            height=source_snapshot[5],
            fps_num=source_snapshot[6],
            fps_den=source_snapshot[7],
            rule_version=qc.RULE_VERSION,
        )
        interval = qc.QcInterval(
            start_frame=5,
            end_frame=5,
            start_ms=200,
            end_ms=240,
            reason_codes=("black_frame",),
            values={"pblack": 100.0},
        )

        with pytest.raises(DBAPIError):
            with PgSession.begin() as session:
                qc.persist_technical_qc_findings(session, claim, [interval])

        _prepare_postgresql_schema(temporary.url, "20260902_0030")
        with PgSession.begin() as session:
            issue_ids = qc.persist_technical_qc_findings(
                session,
                claim,
                [interval],
            )
        assert len(issue_ids) == 1

        with PgSession.begin() as session:
            issue = session.get(models.ReviewIssueModel, issue_ids[0])
            item = session.get(models.ReviewItemModel, item_id)
            finalization = session.get(
                models.FinalizationRecordModel,
                finalization_id,
            )
            assert issue is not None
            assert item is not None
            assert item.workflow_status == "finalized"
            assert item.active_finalization_id == finalization_id
            assert finalization is not None
            assert finalization.status == "active"

            manual = models.ReviewIssueModel(
                id=f"iss_manual_{_suffix()}",
                project_ref_id=project_id,
                review_item_id=item_id,
                version_id=version_id,
                issue_no=99,
                status="unresolved",
                current_revision_id=f"rev_manual_{_suffix()}",
                timestamp_ms=0,
                frame_number=0,
            )
            session.add(manual)
            with pytest.raises(DBAPIError, match="finalized review item is frozen"):
                session.flush()
            session.rollback()

        with PgSession.begin() as session:
            repository_module = importlib.import_module(
                "backend.app.modules.final_cut_review.infra.repositories"
            )
            context_module = importlib.import_module(
                "backend.app.modules.final_cut_review.application.context"
            )
            repository = repository_module.SqlAlchemyReviewRepository(
                session,
                get_settings(),
            )
            edited = repository.update_review_issue(
                {
                    "project_ref_id": project_id,
                    "review_item_id": item_id,
                    "version_id": version_id,
                    "issue_id": issue_ids[0],
                    "content": "PostgreSQL 人工编辑后保留",
                },
                context_module.ExecutionContext(
                    entry_source="review",
                    request_id=f"req_{_suffix()}",
                    principal=context_module.PrincipalRef(
                        kind="system",
                        id="pg-qc-rerun-test",
                        project_ref_ids=(project_id,),
                    ),
                    write_guard=context_module.WriteGuardState(
                        mode="none",
                        verified=True,
                    ),
                ),
                1,
            )
            assert edited["current_revision"]["content"] == "PostgreSQL 人工编辑后保留"

        with PgSession.begin() as session:
            assert qc.persist_technical_qc_findings(
                session,
                claim,
                [interval],
            ) == issue_ids
        with PgSession() as session:
            issues = list(
                session.scalars(
                    select(models.ReviewIssueModel).where(
                        models.ReviewIssueModel.review_item_id == item_id
                    )
                )
            )
            issue = issues[0]
            revision = session.get(
                models.ReviewIssueRevisionModel,
                issue.current_revision_id,
            )
            assert len(issues) == 1
            assert issue.lock_version == 2
            assert revision is not None
            assert revision.content == "PostgreSQL 人工编辑后保留"
    finally:
        if engine is not None:
            engine.dispose()
        get_settings.cache_clear()
        _drop_postgres_temp_database(temporary)
