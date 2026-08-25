from __future__ import annotations

import importlib
import json
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from sqlalchemy import inspect, select

from .conftest import (
    api_data,
    command,
    create_item,
    create_project,
    create_project_item,
    upload_video,
)
from .test_task1_contract_and_migration import _alembic_config, _set_migration_env
from .test_postgresql_constraints import (
    _drop_postgres_temp_database,
    _postgres_database_url,
    _postgres_temp_database,
    _prepare_postgresql_schema,
)
from .test_api_contract_runtime import create_issue, finalize


def _technical_qc() -> Any:
    return importlib.import_module("backend.app.technical_qc")


def test_project_technical_qc_progress_query_exposes_only_running_episode_rows(client: Any) -> None:
    project, _item = create_project_item(client, code="TASK6-PROGRESS")

    response = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/technical-qc-progress",
    )

    assert response.status_code == 200, response.text
    assert api_data(response) == {"episodes": []}


def test_0026_clean_sqlite_round_trip_preserves_task_and_finding_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "task6-technical-qc.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    config = _alembic_config(root)

    alembic_command.upgrade(config, "20260825_0025")
    alembic_command.upgrade(config, "20260825_0026")
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        schema = inspect(engine)
        assert {"technical_qc_tasks", "technical_qc_findings"}.issubset(
            schema.get_table_names(),
        )
        task_columns = {column["name"] for column in schema.get_columns("technical_qc_tasks")}
        assert {
            "id",
            "project_ref_id",
            "review_item_id",
            "version_id",
            "input_file_id",
            "status",
            "claim_id",
            "lease_expires_at",
            "processed_ms",
            "total_ms",
            "error_code",
            "rule_version",
            "attempts",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
        }.issubset(task_columns)
        finding_columns = {
            column["name"] for column in schema.get_columns("technical_qc_findings")
        }
        assert {
            "id",
            "task_id",
            "version_id",
            "identity_hash",
            "issue_id",
            "start_frame",
            "end_frame",
            "start_ms",
            "end_ms",
            "reason_codes",
        }.issubset(finding_columns)
        task_uniques = {
            tuple(constraint["column_names"])
            for constraint in schema.get_unique_constraints("technical_qc_tasks")
        }
        finding_uniques = {
            tuple(constraint["column_names"])
            for constraint in schema.get_unique_constraints("technical_qc_findings")
        }
        assert ("version_id",) in task_uniques
        assert ("identity_hash",) in finding_uniques
    finally:
        engine.dispose()

    alembic_command.downgrade(config, "20260825_0025")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "technical_qc_tasks" not in inspect(engine).get_table_names()
        assert "technical_qc_findings" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    alembic_command.upgrade(config, "20260825_0026")


def test_0026_real_postgresql_upgrade_downgrade_upgrade() -> None:
    database_url = _postgres_database_url()
    temporary = _postgres_temp_database(database_url)
    previous_database_url = os.environ.get("DATABASE_URL")
    from backend.app.settings import get_settings

    try:
        _prepare_postgresql_schema(temporary.url, "20260825_0025")
        os.environ["DATABASE_URL"] = temporary.url
        get_settings.cache_clear()
        root = Path(__file__).resolve().parents[2]
        config = _alembic_config(root)
        alembic_command.upgrade(config, "20260825_0026")

        from sqlalchemy import create_engine

        engine = create_engine(temporary.url)
        try:
            assert {"technical_qc_tasks", "technical_qc_findings"}.issubset(
                inspect(engine).get_table_names(),
            )
        finally:
            engine.dispose()

        alembic_command.downgrade(config, "20260825_0025")
        engine = create_engine(temporary.url)
        try:
            assert "technical_qc_tasks" not in inspect(engine).get_table_names()
        finally:
            engine.dispose()
        alembic_command.upgrade(config, "20260825_0026")
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        get_settings.cache_clear()
        _drop_postgres_temp_database(temporary)


def test_short_shot_intervals_accept_exactly_one_two_and_three_frames() -> None:
    qc = _technical_qc()

    intervals = qc.short_shot_intervals(
        scene_cut_frames=[0, 1, 3, 6, 10, 10],
        total_frames=10,
        fps_num=25,
        fps_den=1,
    )

    assert [
        (interval.start_frame, interval.end_frame, interval.frame_count)
        for interval in intervals
    ] == [(0, 0, 1), (1, 2, 2), (3, 5, 3)]


@pytest.mark.parametrize(
    ("rule_code", "duration_ms", "expected"),
    [
        ("continuous_black", 1_000, False),
        ("continuous_black", 1_001, True),
        ("continuous_white", 1_000, False),
        ("continuous_white", 1_001, True),
        ("silence", 2_000, False),
        ("silence", 2_001, True),
    ],
)
def test_duration_thresholds_are_strict(
    rule_code: str,
    duration_ms: int,
    expected: bool,
) -> None:
    qc = _technical_qc()

    assert qc.duration_rule_hit(rule_code, duration_ms) is expected


def test_overlapping_intervals_merge_all_reasons_but_adjacent_intervals_stay_separate() -> None:
    qc = _technical_qc()
    intervals = [
        qc.QcInterval(
            start_frame=5,
            end_frame=5,
            start_ms=200,
            end_ms=240,
            reason_codes=("black_frame",),
            values={"pblack": 100.0},
        ),
        qc.QcInterval(
            start_frame=5,
            end_frame=7,
            start_ms=200,
            end_ms=320,
            reason_codes=("short_shot",),
            values={"scene_score": 13.2},
        ),
        qc.QcInterval(
            start_frame=8,
            end_frame=8,
            start_ms=320,
            end_ms=360,
            reason_codes=("white_frame",),
            values={"yavg": 255.0},
        ),
    ]

    merged = qc.merge_overlapping_intervals(intervals)

    assert len(merged) == 2
    assert (merged[0].start_frame, merged[0].end_frame) == (5, 7)
    assert merged[0].reason_codes == ("black_frame", "short_shot")
    assert merged[0].values == {"pblack": 100.0, "scene_score": 13.2}
    assert (merged[1].start_frame, merged[1].end_frame) == (8, 8)


def test_ready_playback_discovery_is_idempotent_and_never_creates_unready_task(client: Any) -> None:
    ready_project, ready_item = create_project_item(client, code="TASK6-READY")
    waiting_project, waiting_item = create_project_item(client, code="TASK6-WAITING")
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal.begin() as session:
        waiting_derivative = session.scalar(
            select(models.MediaDerivativeTaskModel).where(
                models.MediaDerivativeTaskModel.version_id
                == waiting_item["current_version_id"],
                models.MediaDerivativeTaskModel.kind == "playback_faststart",
            ),
        )
        assert waiting_derivative is not None
        waiting_derivative.status = "queued"
        waiting_derivative.output_file_id = None

    qc = _technical_qc()
    with db.SessionLocal.begin() as session:
        assert qc.discover_ready_technical_qc_tasks(session) == 1
        assert qc.discover_ready_technical_qc_tasks(session) == 0
        tasks = list(session.scalars(select(models.TechnicalQcTaskModel)))

    assert [(task.project_ref_id, task.version_id) for task in tasks] == [
        (ready_project["project_ref_id"], ready_item["current_version_id"]),
    ]
    assert all(
        task.version_id != waiting_item["current_version_id"] for task in tasks
    )


def test_claims_round_robin_projects_and_keep_each_project_episode_ascending(client: Any) -> None:
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    project_a = create_project(client, "TASK6-FAIR-A")
    project_b = create_project(client, "TASK6-FAIR-B")
    for project, episodes in ((project_a, (3, 1, 2)), (project_b, (2, 1))):
        for episode in episodes:
            create_item(
                client,
                project["project_ref_id"],
                upload_video(client, seed=f"{project['project_ref_id']}-{episode}".encode()),
                item_code=f"{project['project_ref_id'][-6:]}-{episode}",
                episode_no=episode,
            )

    qc = _technical_qc()
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    with db.SessionLocal.begin() as session:
        assert qc.discover_ready_technical_qc_tasks(session) == 5
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=4,
            per_project_limit=2,
            lease_seconds=60,
            now=now,
        )

    assert len(claims) == 4
    assert claims[0].project_ref_id != claims[1].project_ref_id
    by_project: dict[str, list[int]] = {}
    for claim in claims:
        by_project.setdefault(claim.project_ref_id, []).append(claim.episode_no)
    assert set(by_project) == {
        project_a["project_ref_id"],
        project_b["project_ref_id"],
    }
    assert all(episodes == sorted(episodes) for episodes in by_project.values())
    assert all(len(episodes) <= 2 for episodes in by_project.values())


def test_expired_lease_reclaims_and_progress_is_monotonic_for_current_claim_only(client: Any) -> None:
    _project, _item = create_project_item(client, code="TASK6-LEASE")
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    qc = _technical_qc()
    first_now = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        first = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=30,
            now=first_now,
        )[0]

    with db.SessionLocal.begin() as session:
        assert qc.update_technical_qc_progress(
            session,
            first.task_id,
            first.claim_id,
            processed_ms=25,
            now=first_now + timedelta(seconds=5),
        )

    reclaim_now = first_now + timedelta(seconds=31)
    with db.SessionLocal.begin() as session:
        recovered = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=30,
            now=reclaim_now,
        )[0]
        assert recovered.task_id == first.task_id
        assert recovered.claim_id != first.claim_id
        assert not qc.update_technical_qc_progress(
            session,
            first.task_id,
            first.claim_id,
            processed_ms=50,
            now=reclaim_now,
        )
        assert qc.update_technical_qc_progress(
            session,
            recovered.task_id,
            recovered.claim_id,
            processed_ms=40,
            now=reclaim_now,
        )
        assert qc.update_technical_qc_progress(
            session,
            recovered.task_id,
            recovered.claim_id,
            processed_ms=30,
            now=reclaim_now,
        )

    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, first.task_id)
        assert task is not None
        assert task.processed_ms == 40
        assert task.attempts == 2


def test_non_retryable_failure_is_terminal_and_never_reclaimed(client: Any) -> None:
    _project, _item = create_project_item(client, code="TASK6-TERMINAL")
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    qc = _technical_qc()
    failed_at = datetime(2026, 8, 25, 1, tzinfo=timezone.utc)
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=30,
            now=failed_at,
        )[0]
        assert qc.fail_technical_qc_task(
            session,
            claim.task_id,
            claim.claim_id,
            error_code="TECHNICAL_QC_MEDIA_INVALID",
            retryable=False,
            max_attempts=3,
            retry_delay_seconds=30,
            now=failed_at,
        )

    with db.SessionLocal.begin() as session:
        assert qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=30,
            now=failed_at + timedelta(hours=1),
        ) == []


def test_progress_query_is_project_scoped_sorted_and_removes_completed_rows(client: Any) -> None:
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    project = create_project(client, "TASK6-RUNNING")
    other_project, _other_item = create_project_item(client, code="TASK6-OTHER")
    for episode in (2, 1):
        create_item(
            client,
            project["project_ref_id"],
            upload_video(client, seed=f"running-{episode}".encode()),
            item_code=f"RUN-{episode}",
            episode_no=episode,
        )
    qc = _technical_qc()
    now = datetime.now(timezone.utc)
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claims = qc.claim_technical_qc_tasks(
            session,
            limit=3,
            per_project_limit=2,
            lease_seconds=60,
            now=now,
        )
        project_claims = [
            claim for claim in claims if claim.project_ref_id == project["project_ref_id"]
        ]
        assert [claim.episode_no for claim in project_claims] == [1, 2]
        for index, claim in enumerate(project_claims, start=1):
            assert qc.update_technical_qc_progress(
                session,
                claim.task_id,
                claim.claim_id,
                processed_ms=index * 100,
                now=now,
            )

    response = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/technical-qc-progress",
    )
    assert response.status_code == 200, response.text
    episodes = api_data(response)["episodes"]
    assert [episode["episode_no"] for episode in episodes] == [1, 2]
    assert [episode["review_item_id"] for episode in episodes] == [
        claim.review_item_id for claim in project_claims
    ]
    assert all(
        set(episode) == {"review_item_id", "episode_no", "processed_ms", "total_ms"}
        for episode in episodes
    )
    assert all(episode["processed_ms"] <= episode["total_ms"] for episode in episodes)

    completed = project_claims[0]
    with db.SessionLocal.begin() as session:
        assert qc.complete_technical_qc_task(
            session,
            completed.task_id,
            completed.claim_id,
            now=now + timedelta(seconds=1),
        )

    refreshed = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/technical-qc-progress",
    )
    assert refreshed.status_code == 200, refreshed.text
    assert [episode["episode_no"] for episode in api_data(refreshed)["episodes"]] == [2]
    assert other_project["project_ref_id"] != project["project_ref_id"]


def _claim_single_ready_task(client: Any, code: str) -> tuple[Any, Any, Any, Any]:
    project, item = create_project_item(client, code=code)
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    qc = _technical_qc()
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=2,
            lease_seconds=60,
            now=datetime(2026, 8, 25, 3, tzinfo=timezone.utc),
        )[0]
    return project, item, db, claim


def test_persisted_finding_identity_is_ack_idempotent_and_body_contains_all_interval_evidence(
    client: Any,
) -> None:
    _project, _item, db, claim = _claim_single_ready_task(client, "TASK6-ACK")
    qc = _technical_qc()
    interval = qc.QcInterval(
        start_frame=5,
        end_frame=7,
        start_ms=200,
        end_ms=320,
        reason_codes=("black_frame", "short_shot"),
        values={"pblack": 100.0, "scene_score": 13.2},
    )
    with db.SessionLocal.begin() as session:
        first_issue_ids = qc.persist_technical_qc_findings(session, claim, [interval])
    with db.SessionLocal.begin() as session:
        retried_issue_ids = qc.persist_technical_qc_findings(session, claim, [interval])

    assert retried_issue_ids == first_issue_ids
    assert len(first_issue_ids) == 1
    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        findings = list(session.scalars(select(models.TechnicalQcFindingModel)))
        issues = list(session.scalars(select(models.ReviewIssueModel)))
        revisions = list(session.scalars(select(models.ReviewIssueRevisionModel)))
    assert len(findings) == len(issues) == len(revisions) == 1
    assert findings[0].issue_id == issues[0].id == first_issue_ids[0]
    body = revisions[0].content
    assert "黑帧" in body
    assert "极短镜头" in body
    assert "00:00:00.200" in body
    assert "F5-F7" in body
    assert "3 帧" in body
    assert "120 ms" in body
    assert "pblack=100" in body
    assert "scene_score=13.2" in body


def test_automatic_finding_reuses_manual_issue_workflow_transition(
    client: Any,
) -> None:
    _project, item, db, claim = _claim_single_ready_task(
        client,
        "TASK6-AUTO-WORKFLOW",
    )
    qc = _technical_qc()
    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        before = session.get(models.ReviewItemModel, item["id"])
        assert before is not None
        assert before.workflow_status == "pending_review"
        initial_lock_version = before.lock_version

    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        issue_ids = qc.persist_technical_qc_findings(session, claim, [interval])

    assert len(issue_ids) == 1
    with db.SessionLocal() as session:
        transitioned = session.get(models.ReviewItemModel, item["id"])
        assert transitioned is not None
        assert transitioned.workflow_status == "changes_requested"
        assert transitioned.lock_version == initial_lock_version + 1
        transitioned_lock_version = transitioned.lock_version

    with db.SessionLocal.begin() as session:
        assert qc.persist_technical_qc_findings(session, claim, [interval]) == issue_ids

    with db.SessionLocal() as session:
        retried = session.get(models.ReviewItemModel, item["id"])
        assert retried is not None
        assert retried.workflow_status == "changes_requested"
        assert retried.lock_version == transitioned_lock_version


def test_superseded_claim_does_not_create_issue_or_change_current_version_state(
    client: Any,
) -> None:
    project, item, db, claim = _claim_single_ready_task(client, "TASK6-STALE-VERSION")
    create_issue(client, project["project_ref_id"], item, content="request V2")
    item_for_upload = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"
            f"/items/{item['id']}"
        )
    )
    new_file = upload_video(client, filename="v2.mp4", seed=b"2")
    upload = command(
        "UploadReviewVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "original_file_id": new_file,
            "change_summary": "v2",
        },
    )
    uploaded = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions",
        json=upload,
        headers={
            "Idempotency-Key": upload["command_id"],
            "If-Match": str(item_for_upload["lock_version"]),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    current = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"
            f"/items/{item['id']}"
        )
    )
    assert current["current_version_id"] != claim.version_id
    assert current["workflow_status"] == "pending_review"

    interval = _technical_qc().QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        assert _technical_qc().persist_technical_qc_findings(
            session,
            claim,
            [interval],
        ) == []

    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        authority = session.get(models.ReviewItemModel, item["id"])
        issue_count = len(
            list(
                session.scalars(
                    select(models.ReviewIssueModel).where(
                        models.ReviewIssueModel.review_item_id == item["id"]
                    )
                )
            )
        )
    assert authority is not None
    assert authority.workflow_status == "pending_review"
    assert authority.current_version_id == current["current_version_id"]
    assert authority.lock_version == current["lock_version"]
    assert issue_count == 1


def test_finalized_claim_completes_without_late_issue_or_retryable_failure(
    client: Any,
) -> None:
    project, item, db, claim = _claim_single_ready_task(client, "TASK6-FINALIZED-RACE")
    finalize(client, project["project_ref_id"], item)
    interval = _technical_qc().QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        assert _technical_qc().persist_technical_qc_findings(
            session,
            claim,
            [interval],
        ) == []
        assert _technical_qc().complete_technical_qc_task(
            session,
            claim.task_id,
            claim.claim_id,
        )

    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        authority = session.get(models.ReviewItemModel, item["id"])
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
    assert authority is not None and authority.workflow_status == "finalized"
    assert task is not None and task.status == "complete" and task.error_code is None
    assert issues == []


def test_automatic_issue_emits_manual_flow_events_and_system_audit_once(
    client: Any,
) -> None:
    _project, item, db, claim = _claim_single_ready_task(client, "TASK6-AUTO-EVENT")
    qc = _technical_qc()
    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        issue_ids = qc.persist_technical_qc_findings(session, claim, [interval])
    with db.SessionLocal.begin() as session:
        assert qc.persist_technical_qc_findings(session, claim, [interval]) == issue_ids

    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    with db.SessionLocal() as session:
        issue_events = list(
            session.scalars(
                select(models.OutboxEventModel).where(
                    models.OutboxEventModel.event_type == "review.issue.created",
                    models.OutboxEventModel.issue_id.in_(issue_ids),
                )
            )
        )
        session_events = list(
            session.scalars(
                select(models.OutboxEventModel).where(
                    models.OutboxEventModel.event_type == "review.session.started",
                    models.OutboxEventModel.review_item_id == item["id"],
                    models.OutboxEventModel.version_id == claim.version_id,
                )
            )
        )
        audit_rows = list(
            session.scalars(
                select(models.OperationLogModel).where(
                    models.OperationLogModel.principal_kind == "system",
                    models.OperationLogModel.principal_id == "technical-qc-worker",
                    models.OperationLogModel.resource_id.in_(issue_ids),
                )
            )
        )
    assert len(issue_events) == len(issue_ids) == 1
    assert len(session_events) == 1
    assert len(audit_rows) == 1
    assert issue_events[0].metadata_json["principal_id"] == "technical-qc-worker"
    assert audit_rows[0].command_type == "CreateReviewIssue"
    assert audit_rows[0].capability == "review.issue.create"
    assert audit_rows[0].result == "ok"


def test_automatic_issues_reuse_edit_ignore_delete_and_do_not_block_finalization(
    client: Any,
) -> None:
    project, item, db, claim = _claim_single_ready_task(client, "TASK6-AUTO-ACTIONS")
    qc = _technical_qc()
    intervals = [
        qc.QcInterval(
            start_frame=5,
            end_frame=5,
            start_ms=200,
            end_ms=240,
            reason_codes=("black_frame",),
            values={"pblack": 100.0},
        ),
        qc.QcInterval(
            start_frame=10,
            end_frame=10,
            start_ms=400,
            end_ms=440,
            reason_codes=("white_frame",),
            values={"yavg": 255.0},
        ),
    ]
    with db.SessionLocal.begin() as session:
        issue_ids = qc.persist_technical_qc_findings(session, claim, intervals)
    assert len(issue_ids) == 2

    base = (
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions/{item['current_version_id']}/issues"
    )
    update_body = command(
        "UpdateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue_ids[0],
            "content": "人工补充后的自动技术意见",
        },
    )
    updated = client.patch(
        f"{base}/{issue_ids[0]}",
        json=update_body,
        headers={"If-Match": "1"},
    )
    assert updated.status_code == 200, updated.text
    assert api_data(updated)["current_revision"]["content"] == "人工补充后的自动技术意见"

    resolve_body = command(
        "ResolveReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue_ids[0],
        },
    )
    ignored = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions/{item['current_version_id']}"
        f"/issues/{issue_ids[0]}/resolve",
        json=resolve_body,
        headers={"If-Match": "2"},
    )
    assert ignored.status_code == 200, ignored.text
    assert api_data(ignored)["status"] == "resolved"

    delete_body = command(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue_ids[1],
        },
    )
    deleted = client.post(
        f"{base}/{issue_ids[1]}/soft-delete",
        json=delete_body,
        headers={"If-Match": "1"},
    )
    assert deleted.status_code == 200, deleted.text
    assert api_data(deleted)["deleted_at"] is not None

    with db.SessionLocal.begin() as session:
        assert qc.persist_technical_qc_findings(session, claim, intervals) == issue_ids
    with db.SessionLocal() as session:
        models = importlib.import_module(
            "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
        )
        assert len(list(session.scalars(select(models.ReviewIssueModel)))) == 2

    item_authority = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
    )
    assert item_authority.status_code == 200, item_authority.text
    item_lock = api_data(item_authority)["lock_version"]
    finalize_body = command(
        "FinalizeVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "confirmed": True,
        },
    )
    finalized = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions/{item['current_version_id']}/finalize",
        json=finalize_body,
        headers={"If-Match": str(item_lock), "Idempotency-Key": finalize_body["command_id"]},
    )
    assert finalized.status_code == 200, finalized.text
    assert api_data(finalized)["status"] == "active"


def _claim_for_parser(qc: Any) -> Any:
    return qc.TechnicalQcClaim(
        task_id="tqc_test",
        claim_id="claim_test",
        project_ref_id="project_test",
        review_item_id="item_test",
        version_id="version_test",
        episode_no=1,
        input_file_id="file_test",
        input_storage_path="files/file_test",
        input_sha256="a" * 64,
        input_size=1_024,
        total_ms=4_000,
        width=1_920,
        height=1_080,
        fps_num=25,
        fps_den=1,
        rule_version=qc.RULE_VERSION,
    )


def test_ffmpeg_argv_uses_fd_full_frame_branch_and_mature_filters_without_source_path() -> None:
    qc = _technical_qc()
    claim = _claim_for_parser(qc)

    argv = qc.build_technical_qc_ffmpeg_argv(
        claim,
        input_fd=9,
        ffmpeg_command="/usr/bin/ffmpeg",
    )

    assert argv[0] == "/usr/bin/ffmpeg"
    assert "-nostdin" in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe"
    assert argv[argv.index("-i") + 1] == "/dev/fd/9"
    assert claim.input_storage_path not in "\n".join(argv)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "scale=" in graph
    assert "split=4" in graph
    assert "blackframe=" in graph
    assert "signalstats" in graph
    assert "scdet=" in graph
    assert "metadata=mode=print" in graph
    assert "[vfull]" in argv
    assert "silencedetect=" in argv[argv.index("-af") + 1]
    assert "framestep" not in graph
    assert "fps=" not in graph
    assert argv[-3:] == ["-f", "null", "-"]


def _blackframe_log(frame_count: int, fps: int = 25) -> str:
    return "\n".join(
        f"[Parsed_blackframe_2 @ 0x1] frame:{frame} pblack:100 pts:{frame} t:{frame / fps:.6f} type:I last_keyframe:0"
        for frame in range(frame_count)
    )


def test_ffmpeg_parser_detects_single_black_and_white_frames_and_strict_continuous_boundary() -> None:
    qc = _technical_qc()
    exact_one_second = qc.parse_technical_qc_ffmpeg_log(
        _blackframe_log(25),
        fps_num=25,
        fps_den=1,
        total_frames=25,
        total_ms=1_000,
    )
    over_one_second = qc.parse_technical_qc_ffmpeg_log(
        _blackframe_log(26),
        fps_num=25,
        fps_den=1,
        total_frames=26,
        total_ms=1_040,
    )
    white = qc.parse_technical_qc_ffmpeg_log(
        "frame:0 pts:0 pts_time:0\nlavfi.signalstats.YAVG=255",
        fps_num=25,
        fps_den=1,
        total_frames=1,
        total_ms=40,
    )

    assert any("black_frame" in interval.reason_codes for interval in exact_one_second)
    assert not any("continuous_black" in interval.reason_codes for interval in exact_one_second)
    assert any("continuous_black" in interval.reason_codes for interval in over_one_second)
    assert any("white_frame" in interval.reason_codes for interval in white)


def test_ffmpeg_parser_silence_is_strictly_greater_than_two_seconds() -> None:
    qc = _technical_qc()
    exact = qc.parse_technical_qc_ffmpeg_log(
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 2 | silence_duration: 2",
        fps_num=25,
        fps_den=1,
        total_frames=50,
        total_ms=2_000,
    )
    over = qc.parse_technical_qc_ffmpeg_log(
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 2.001 | silence_duration: 2.001",
        fps_num=25,
        fps_den=1,
        total_frames=51,
        total_ms=2_040,
    )

    assert not any("silence" in interval.reason_codes for interval in exact)
    assert any("silence" in interval.reason_codes for interval in over)


def test_ffmpeg_parser_uses_scene_pts_for_one_two_three_frame_short_shots() -> None:
    qc = _technical_qc()
    log = "\n".join(
        [
            "frame:0 pts:0 pts_time:0",
            "lavfi.scd.score=12",
            "frame:1 pts:1 pts_time:0.04",
            "lavfi.scd.score=13",
            "frame:2 pts:3 pts_time:0.12",
            "lavfi.scd.score=14",
            "frame:3 pts:6 pts_time:0.24",
            "lavfi.scd.score=15",
        ],
    )

    intervals = qc.parse_technical_qc_ffmpeg_log(
        log,
        fps_num=25,
        fps_den=1,
        total_frames=10,
        total_ms=400,
    )

    short_shots = [
        interval for interval in intervals if "short_shot" in interval.reason_codes
    ]
    assert [(item.start_frame, item.end_frame) for item in short_shots] == [
        (0, 0),
        (1, 2),
        (3, 5),
    ]


def test_ffmpeg_failure_classification_and_log_redaction_never_expose_input_or_token() -> None:
    qc = _technical_qc()
    raw = (
        "No such filter: 'scdet' /private/storage/files/secret.mp4 "
        "token=super-secret Authorization: Bearer abc.def.ghi"
    )

    assert qc.classify_technical_qc_ffmpeg_failure(raw) == "TECHNICAL_QC_FILTER_UNAVAILABLE"
    redacted = qc.redact_technical_qc_log(
        raw,
        sensitive_values=("/private/storage/files/secret.mp4",),
    )
    assert "/private/storage/files/secret.mp4" not in redacted
    assert "super-secret" not in redacted
    assert "abc.def.ghi" not in redacted
    assert "<redacted>" in redacted


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o700)
    return path


def _file_claim(qc: Any, path: Path, *, total_ms: int, frames: int = 10) -> Any:
    payload = path.read_bytes()
    return qc.TechnicalQcClaim(
        task_id="tqc_process",
        claim_id="claim_process",
        project_ref_id="project_process",
        review_item_id="item_process",
        version_id="version_process",
        episode_no=1,
        input_file_id="file_process",
        input_storage_path=str(path),
        input_sha256=hashlib.sha256(payload).hexdigest(),
        input_size=len(payload),
        total_ms=total_ms,
        width=64,
        height=64,
        fps_num=25,
        fps_den=1,
        rule_version=qc.RULE_VERSION,
    )


def test_real_ffmpeg_process_consumes_every_frame_and_reports_monotonic_media_progress(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("host ffmpeg is unavailable")
    media = tmp_path / "controlled-h264.mp4"
    created = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=25:duration=0.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.4",
            "-frames:v",
            "10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(media),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    qc = _technical_qc()
    claim = _file_claim(qc, media, total_ms=400)
    progress: list[int] = []

    result = qc.scan_technical_qc_claim(
        claim,
        storage_root=tmp_path,
        ffmpeg_command=ffmpeg,
        timeout_seconds=15,
        output_limit_bytes=2 * 1024 * 1024,
        on_progress=progress.append,
    )

    assert result.frames_processed == 10
    assert result.processed_ms == 400
    assert progress
    assert progress == sorted(progress)
    assert progress[-1] == 400


def test_real_ffmpeg_filters_detect_single_black_and_white_frames_without_audio(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("host ffmpeg is unavailable")
    media = tmp_path / "controlled-black-white-h264.mp4"
    created = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=64x64:r=25:d=0.2,geq=lum='if(eq(N,0),0,if(eq(N,1),255,128))':cb=128:cr=128",
            "-frames:v",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media),
        ],
        check=False,
        capture_output=True,
        timeout=15,
    )
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    qc = _technical_qc()
    result = qc.scan_technical_qc_claim(
        _file_claim(qc, media, total_ms=200, frames=5),
        storage_root=tmp_path,
        ffmpeg_command=ffmpeg,
        timeout_seconds=15,
        output_limit_bytes=2 * 1024 * 1024,
    )

    reasons = {
        reason
        for interval in result.intervals
        for reason in interval.reason_codes
    }
    assert result.frames_processed == 5
    assert "black_frame" in reasons
    assert "white_frame" in reasons


def test_technical_qc_subprocess_boundary_is_shell_free_and_fd_pinned_without_preexec(
    tmp_path: Path,
) -> None:
    qc = _technical_qc()
    media = tmp_path / "input.mp4"
    media.write_bytes(b"safe-input")
    with media.open("rb") as handle:
        descriptor = handle.fileno()
        kwargs = qc.technical_qc_popen_kwargs(descriptor)

    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (descriptor,)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is False
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
    assert "preexec_fn" not in kwargs


def test_scan_rejects_symlink_and_detects_path_replacement_after_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _technical_qc()
    real = tmp_path / "real.mp4"
    real.write_bytes(b"real-media")
    link = tmp_path / "link.mp4"
    link.symlink_to(real)
    symlink_claim = _file_claim(qc, real, total_ms=400)
    symlink_claim = replace(symlink_claim, input_storage_path=str(link))
    with pytest.raises(qc.TechnicalQcError) as symlink_error:
        qc.scan_technical_qc_claim(
            symlink_claim,
            storage_root=tmp_path,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=1,
            output_limit_bytes=4_096,
        )
    assert symlink_error.value.code == "TECHNICAL_QC_INPUT_IDENTITY_INVALID"

    replacement_target = tmp_path / "replace.mp4"
    replacement_target.write_bytes(b"original-media")
    replacement_claim = _file_claim(qc, replacement_target, total_ms=400)

    def replace_during_scan(*_args: Any, **_kwargs: Any) -> Any:
        replacement = tmp_path / "replacement.tmp"
        replacement.write_bytes(b"replacement-media")
        os.replace(replacement, replacement_target)
        return qc.TechnicalQcScanResult(
            intervals=(),
            processed_ms=400,
            frames_processed=10,
        )

    monkeypatch.setattr(qc, "run_technical_qc_ffmpeg", replace_during_scan)
    with pytest.raises(qc.TechnicalQcError) as replacement_error:
        qc.scan_technical_qc_claim(
            replacement_claim,
            storage_root=tmp_path,
            ffmpeg_command="/usr/bin/false",
            timeout_seconds=1,
            output_limit_bytes=4_096,
        )
    assert replacement_error.value.code == "TECHNICAL_QC_INPUT_IDENTITY_INVALID"


@pytest.mark.parametrize(
    ("source", "timeout_seconds", "output_limit", "expected_code"),
    [
        (
            "import sys\nsys.stderr.write(\"No such filter: 'scdet'\\n\")\nsys.exit(1)\n",
            2,
            4_096,
            "TECHNICAL_QC_FILTER_UNAVAILABLE",
        ),
        (
            "import time\ntime.sleep(5)\n",
            0.1,
            4_096,
            "TECHNICAL_QC_SCAN_TIMEOUT",
        ),
        (
            "import sys\nsys.stderr.write('x' * 100000)\nsys.stderr.flush()\n",
            2,
            1_024,
            "TECHNICAL_QC_OUTPUT_LIMIT",
        ),
    ],
)
def test_scan_process_failure_matrix_is_bounded_and_classified(
    tmp_path: Path,
    source: str,
    timeout_seconds: float,
    output_limit: int,
    expected_code: str,
) -> None:
    qc = _technical_qc()
    executable = _write_executable(tmp_path / f"fake-{expected_code}.py", source)
    media = tmp_path / f"input-{expected_code}.mp4"
    media.write_bytes(b"not-used-by-fake-process")
    claim = _file_claim(qc, media, total_ms=400)

    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.run_technical_qc_ffmpeg(
            claim,
            input_handle=handle,
            ffmpeg_command=str(executable),
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit,
        )

    assert caught.value.code == expected_code


def test_heartbeat_exception_kills_scan_and_returns_stable_failure(
    tmp_path: Path,
) -> None:
    qc = _technical_qc()
    executable = _write_executable(
        tmp_path / "fake-heartbeat-failure.py",
        "import time\ntime.sleep(5)\n",
    )
    media = tmp_path / "heartbeat-input.mp4"
    media.write_bytes(b"not-used-by-fake-process")
    claim = _file_claim(qc, media, total_ms=400)

    with media.open("rb") as handle, pytest.raises(qc.TechnicalQcError) as caught:
        qc.run_technical_qc_ffmpeg(
            claim,
            input_handle=handle,
            ffmpeg_command=str(executable),
            timeout_seconds=2,
            output_limit_bytes=4_096,
            on_heartbeat=lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
            heartbeat_interval_seconds=0.01,
        )

    assert caught.value.code == "TECHNICAL_QC_SCAN_FAILED"


def test_real_ffmpeg_rejects_invalid_media_without_disclosing_storage_path(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("host ffmpeg is unavailable")
    qc = _technical_qc()
    media = tmp_path / "sensitive-token=do-not-log.mp4"
    media.write_bytes(b"not-a-media-container")
    claim = _file_claim(qc, media, total_ms=400)

    with pytest.raises(qc.TechnicalQcError) as caught:
        qc.scan_technical_qc_claim(
            claim,
            storage_root=tmp_path,
            ffmpeg_command=ffmpeg,
            timeout_seconds=5,
            output_limit_bytes=64 * 1024,
        )

    assert caught.value.code == "TECHNICAL_QC_MEDIA_INVALID"
    assert str(media) not in str(caught.value)


def test_worker_batch_runs_bounded_parallel_and_isolates_one_project_failure(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qc = _technical_qc()
    db = importlib.import_module("backend.app.modules.final_cut_review.infra.database")
    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models",
    )
    failed_project, _ = create_project_item(client, code="TASK6-WORKER-FAIL")
    successful_project, _ = create_project_item(client, code="TASK6-WORKER-OK")
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def controlled_scan(claim: Any, **kwargs: Any) -> Any:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            kwargs["on_progress"](40)
            time.sleep(0.05)
            if claim.project_ref_id == failed_project["project_ref_id"]:
                raise qc.TechnicalQcError("TECHNICAL_QC_MEDIA_INVALID", retryable=False)
            return qc.TechnicalQcScanResult(
                intervals=(
                    qc.QcInterval(
                        start_frame=1,
                        end_frame=1,
                        start_ms=40,
                        end_ms=80,
                        reason_codes=("black_frame",),
                        values={"pblack": 100.0},
                    ),
                ),
                processed_ms=160,
                frames_processed=4,
            )
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(qc, "scan_technical_qc_claim", controlled_scan)
    settings = SimpleNamespace(
        storage_root=Path("/unused-by-controlled-scan"),
        technical_qc_ffmpeg_command="/usr/bin/false",
        technical_qc_timeout_seconds=5.0,
        technical_qc_output_limit_bytes=64 * 1024,
        technical_qc_worker_concurrency=2,
        technical_qc_per_project_concurrency=2,
        technical_qc_lease_seconds=60,
        technical_qc_worker_max_attempts=3,
        technical_qc_worker_retry_delay_seconds=30,
    )

    result = qc.process_technical_qc_batch(settings, runtime_binding=None)

    assert result == {"complete": 1, "failed": 1, "skipped": 0}
    assert max_active == 2
    with db.SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(models.TechnicalQcTaskModel).order_by(
                    models.TechnicalQcTaskModel.project_ref_id,
                ),
            ),
        )
        by_project = {task.project_ref_id: task for task in tasks}
        successful_task = by_project[successful_project["project_ref_id"]]
        failed_task = by_project[failed_project["project_ref_id"]]
        findings = list(session.scalars(select(models.TechnicalQcFindingModel)))
    assert successful_task.status == "complete"
    assert successful_task.processed_ms == successful_task.total_ms
    assert failed_task.status == "failed"
    assert failed_task.error_code == "TECHNICAL_QC_MEDIA_INVALID"
    assert failed_task.claim_id is None
    assert failed_task.lease_expires_at is None
    assert len(findings) == 1
    assert findings[0].project_ref_id == successful_project["project_ref_id"]


def test_dedicated_technical_qc_cli_once_reports_only_its_own_batch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    qc = _technical_qc()
    monkeypatch.setattr(
        qc,
        "process_pending_technical_qc",
        lambda: {"complete": 2, "failed": 1, "skipped": 0},
    )
    monkeypatch.setattr(sys, "argv", ["technical-qc", "once"])

    assert qc.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "complete": 2,
        "failed": 1,
        "skipped": 0,
    }
