from __future__ import annotations

import importlib
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .conftest import (
    api_data,
    api_error,
    command,
    create_item,
    create_project_item,
    principal_headers,
    upload_video,
)
from .test_api_contract_runtime import create_issue, finalize
from .test_task6_fix1_technical_qc import _settings


def _modules() -> tuple[Any, Any, Any]:
    qc = importlib.import_module("backend.app.technical_qc")
    db = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.database"
    )
    models = importlib.import_module(
        "backend.app.modules.final_cut_review.infra.sqlalchemy_models"
    )
    return qc, db, models


def _complete_initial_qc(project_ref_id: str) -> None:
    qc, db, _models = _modules()
    with db.SessionLocal.begin() as session:
        assert qc.discover_ready_technical_qc_tasks(session) == 1
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        assert claim.project_ref_id == project_ref_id
        assert qc.complete_technical_qc_task(
            session,
            claim,
        )


def _rerun(client: Any, project_ref_id: str, *, command_id: str | None = None) -> Any:
    body = command(
        "RerunProjectTechnicalQc",
        {"project_ref_id": project_ref_id},
        command_id=command_id,
    )
    return client.post(
        f"/api/v1/final-cut-review/edit/projects/{project_ref_id}/technical-qc/rerun",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )


def test_rerun_requeues_finalized_current_version_and_replays_same_command_once(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-RERUN-FINALIZED")
    _complete_initial_qc(project["project_ref_id"])
    finalization = finalize(client, project["project_ref_id"], item)

    response = _rerun(
        client,
        project["project_ref_id"],
        command_id="RerunProjectTechnicalQc_same-command",
    )

    assert response.status_code == 202, response.text
    assert api_data(response) == {
        "project_ref_id": project["project_ref_id"],
        "status": "queued",
        "queued_item_count": 1,
    }

    replay = _rerun(
        client,
        project["project_ref_id"],
        command_id="RerunProjectTechnicalQc_same-command",
    )
    assert replay.status_code == 202, replay.text
    assert api_data(replay) == api_data(response)

    qc, db, models = _modules()
    with db.SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(models.TechnicalQcTaskModel).where(
                    models.TechnicalQcTaskModel.project_ref_id
                    == project["project_ref_id"]
                )
            )
        )
        authority = session.get(models.ReviewItemModel, item["id"])
        active_finalization = session.get(
            models.FinalizationRecordModel,
            finalization["id"],
        )

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "queued"
    assert task.attempts == 0
    assert task.processed_ms == 0
    assert task.claim_id is None
    assert task.lease_expires_at is None
    assert task.started_at is None
    assert task.completed_at is None
    assert authority is not None
    assert authority.workflow_status == "finalized"
    assert authority.active_finalization_id == finalization["id"]
    assert active_finalization is not None
    assert active_finalization.status == "active"

    progress = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"
        "/technical-qc-progress"
    )
    assert progress.status_code == 200, progress.text
    assert api_data(progress) == {"active_task_count": 1, "episodes": []}

    duplicate = _rerun(client, project["project_ref_id"])
    assert duplicate.status_code == 409, duplicate.text
    assert api_error(duplicate)["code"] == "REVIEW_IN_PROGRESS"


def test_finalized_rerun_adds_only_new_issue_and_preserves_human_changes(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-RERUN-OPINIONS")
    _complete_initial_qc(project["project_ref_id"])
    finalization = finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text

    qc, db, models = _modules()
    original_interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        first_claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        first_issue_ids = qc.persist_technical_qc_findings(
            session,
            first_claim,
            [original_interval],
        )
        assert len(first_issue_ids) == 1
        assert qc.complete_technical_qc_task(
            session,
            first_claim,
        )
    original_issue_id = first_issue_ids[0]
    with db.SessionLocal() as session:
        original_issue = session.get(
            models.ReviewIssueModel,
            original_issue_id,
        )
        assert original_issue is not None
        initial_revision_id = original_issue.current_revision_id

    issue_base = (
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions/{item['current_version_id']}"
        f"/issues/{original_issue_id}"
    )
    update_body = command(
        "UpdateReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": original_issue_id,
            "content": "人工编辑后保留的技术审查意见",
        },
    )
    updated = client.patch(
        issue_base,
        json=update_body,
        headers={"If-Match": "1"},
    )
    assert updated.status_code == 200, updated.text

    with db.SessionLocal() as session:
        issue = session.get(models.ReviewIssueModel, original_issue_id)
        assert issue is not None
        assert issue.current_revision_id != initial_revision_id
        issue.current_revision_id = initial_revision_id
        issue.lock_version += 1
        with pytest.raises(IntegrityError, match="finalized review item is frozen"):
            session.flush()
        session.rollback()

    resolve_body = command(
        "ResolveReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": original_issue_id,
        },
    )
    resolved = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/versions/{item['current_version_id']}"
        f"/issues/{original_issue_id}/resolve",
        json=resolve_body,
        headers={"If-Match": "2"},
    )
    assert resolved.status_code == 200, resolved.text

    reopen_body = command(
        "ReopenReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": original_issue_id,
        },
    )
    reopened = client.post(
        f"{issue_base}/reopen",
        json=reopen_body,
        headers={"If-Match": "3"},
    )
    assert reopened.status_code == 409, reopened.text
    assert api_error(reopened)["code"] == "REVIEW_ITEM_FINALIZED"

    with db.SessionLocal() as session:
        issue = session.get(models.ReviewIssueModel, original_issue_id)
        assert issue is not None
        issue.status = "unresolved"
        issue.lock_version += 1
        with pytest.raises(IntegrityError, match="finalized review item is frozen"):
            session.flush()
        session.rollback()

    delete_body = command(
        "SoftDeleteReviewIssue",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": original_issue_id,
        },
    )
    deleted = client.post(
        f"{issue_base}/soft-delete",
        json=delete_body,
        headers={"If-Match": "3"},
    )
    assert deleted.status_code == 200, deleted.text

    with db.SessionLocal() as session:
        issue = session.get(models.ReviewIssueModel, original_issue_id)
        assert issue is not None
        current_revision = session.get(
            models.ReviewIssueRevisionModel,
            issue.current_revision_id,
        )
        assert current_revision is not None
        session.add(
            models.ReviewIssueRevisionModel(
                id="rev_deleted_qc_issue_rejected",
                project_ref_id=issue.project_ref_id,
                review_item_id=issue.review_item_id,
                version_id=issue.version_id,
                issue_id=issue.id,
                revision_no=current_revision.revision_no + 1,
                content="软删除后不得再修改",
            )
        )
        with pytest.raises(IntegrityError, match="finalized review item is frozen"):
            session.flush()
        session.rollback()

    queued_again = _rerun(client, project["project_ref_id"])
    assert queued_again.status_code == 202, queued_again.text
    new_interval = qc.QcInterval(
        start_frame=20,
        end_frame=20,
        start_ms=800,
        end_ms=840,
        reason_codes=("white_frame",),
        values={"yavg": 255.0},
    )
    with db.SessionLocal.begin() as session:
        second_claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        second_issue_ids = qc.persist_technical_qc_findings(
            session,
            second_claim,
            [original_interval, new_interval],
        )
        assert qc.complete_technical_qc_task(
            session,
            second_claim,
        )

    assert original_issue_id in second_issue_ids
    assert len(set(second_issue_ids)) == 2
    with db.SessionLocal() as session:
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel)
                .where(models.ReviewIssueModel.review_item_id == item["id"])
                .order_by(models.ReviewIssueModel.issue_no)
            )
        )
        old_issue = session.get(models.ReviewIssueModel, original_issue_id)
        old_revision = session.get(
            models.ReviewIssueRevisionModel,
            old_issue.current_revision_id if old_issue is not None else "",
        )
        authority = session.get(models.ReviewItemModel, item["id"])
        active_finalization = session.get(
            models.FinalizationRecordModel,
            finalization["id"],
        )

    assert len(issues) == 2
    assert old_issue is not None
    assert old_issue.status == "resolved"
    assert old_issue.deleted_at is not None
    assert old_issue.lock_version == 4
    assert old_revision is not None
    assert old_revision.content == "人工编辑后保留的技术审查意见"
    assert authority is not None
    assert authority.workflow_status == "finalized"
    assert authority.active_finalization_id == finalization["id"]
    assert active_finalization is not None
    assert active_finalization.status == "active"


def test_expired_claim_cannot_persist_a_finalized_technical_opinion(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-RERUN-EXPIRED")
    _complete_initial_qc(project["project_ref_id"])
    finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text

    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert task is not None
        task.lease_expires_at = models.utcnow() - timedelta(seconds=1)

    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        with pytest.raises(qc.TechnicalQcError) as caught:
            qc.persist_technical_qc_findings(
                session,
                claim,
                [interval],
            )
        assert caught.value.code == "TECHNICAL_QC_LEASE_LOST"

    with db.SessionLocal() as session:
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
        authority = session.get(models.ReviewItemModel, item["id"])
    assert issues == []
    assert authority is not None
    assert authority.workflow_status == "finalized"


@pytest.mark.parametrize(
    "operation",
    ["progress", "renew", "probe", "complete", "fail"],
)
def test_expired_claim_cannot_mutate_or_resurrect_task_lease(
    client: Any,
    operation: str,
) -> None:
    project, _item = create_project_item(
        client,
        code=f"TQC-EXPIRED-{operation.upper()}",
    )
    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert task is not None
        task.lease_expires_at = models.utcnow() - timedelta(seconds=1)

    with db.SessionLocal.begin() as session:
        if operation == "progress":
            result = qc.update_technical_qc_progress(
                session,
                claim.task_id,
                claim.claim_id,
                processed_ms=10,
            )
        elif operation == "renew":
            result = qc.renew_technical_qc_lease(
                session,
                claim.task_id,
                claim.claim_id,
                lease_seconds=60,
            )
        elif operation == "probe":
            result = qc.persist_technical_qc_probe(
                session,
                claim.task_id,
                claim.claim_id,
                qc.TechnicalQcMediaProbe(
                    video_frame_count=25,
                    video_duration_ms=1_000,
                    total_ms=1_000,
                ),
            )
        elif operation == "complete":
            result = qc.complete_technical_qc_task(
                session,
                claim,
            )
        else:
            result = qc.fail_technical_qc_task(
                session,
                claim.task_id,
                claim.claim_id,
                error_code="TECHNICAL_QC_SCAN_FAILED",
                retryable=True,
                max_attempts=3,
                retry_delay_seconds=30,
            )
        assert result is False
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        assert task is not None
        assert task.status == "running"
        assert task.claim_id == claim.claim_id
        assert task.processed_ms == 0


def test_worker_does_not_complete_after_losing_lease_during_scan(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, item = create_project_item(client, code="TQC-LEASE-LOST-FINALIZE")
    _complete_initial_qc(project["project_ref_id"])
    finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text
    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]

    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )

    def expire_during_scan(*_args: Any, **_kwargs: Any) -> Any:
        with db.SessionLocal.begin() as session:
            task = session.get(models.TechnicalQcTaskModel, claim.task_id)
            assert task is not None
            task.lease_expires_at = models.utcnow() - timedelta(seconds=1)
        return qc.TechnicalQcScanResult(
            intervals=(interval,),
            processed_ms=1_000,
            frames_processed=25,
            video_frame_count=25,
            video_duration_ms=1_000,
            total_ms=1_000,
        )

    monkeypatch.setattr(qc, "scan_technical_qc_claim", expire_during_scan)

    outcome = qc._process_technical_qc_claim(
        claim,
        _settings(concurrency=1, per_project=1),
        runtime_binding=None,
    )

    assert outcome == "skipped"
    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
    assert task is not None
    assert task.status == "running"
    assert task.claim_id == claim.claim_id
    assert issues == []


def test_worker_does_not_complete_after_playback_proxy_drifts_with_no_new_findings(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-PROXY-DRIFT")
    _complete_initial_qc(project["project_ref_id"])
    finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text
    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        derivative = session.scalar(
            select(models.MediaDerivativeTaskModel).where(
                models.MediaDerivativeTaskModel.version_id == claim.version_id,
                models.MediaDerivativeTaskModel.kind == "playback_faststart",
            )
        )
        assert derivative is not None
        derivative.status = "queued"
        derivative.output_file_id = None

    with db.SessionLocal.begin() as session:
        with pytest.raises(qc.TechnicalQcError) as caught:
            qc.persist_technical_qc_findings(session, claim, [])
        assert caught.value.code == "TECHNICAL_QC_LEASE_LOST"
        assert not qc.complete_technical_qc_task(
            session,
            claim,
        )

    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
    assert task is not None
    assert task.status == "running"
    assert task.claim_id == claim.claim_id
    assert issues == []


def test_rerun_preflight_is_atomic_when_any_current_playback_is_not_ready(
    client: Any,
) -> None:
    project, first_item = create_project_item(client, code="TQC-RERUN-ATOMIC")
    second_item = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="second.mp4", seed=b"2"),
        item_code="FC002",
        episode_no=2,
    )
    _qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        version = session.get(
            models.ReviewVersionModel,
            second_item["current_version_id"],
        )
        assert version is not None
        version.playback_asset_id = None
        derivative = session.scalar(
            select(models.MediaDerivativeTaskModel).where(
                models.MediaDerivativeTaskModel.version_id == version.id,
                models.MediaDerivativeTaskModel.kind == "playback_faststart",
            )
        )
        assert derivative is not None
        derivative.status = "queued"
        derivative.output_file_id = None

    response = _rerun(client, project["project_ref_id"])

    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "PLAYBACK_NOT_READY"
    with db.SessionLocal() as session:
        tasks = list(
            session.scalars(
                select(models.TechnicalQcTaskModel).where(
                    models.TechnicalQcTaskModel.project_ref_id
                    == project["project_ref_id"]
                )
            )
        )
    assert tasks == []
    assert first_item["current_version_id"] != second_item["current_version_id"]


def test_manual_issue_insert_under_finalized_item_remains_database_blocked(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-RERUN-FREEZE")
    finalization = finalize(client, project["project_ref_id"], item)
    _qc, db, models = _modules()

    with pytest.raises(IntegrityError, match="finalized review item is frozen"):
        with db.SessionLocal.begin() as session:
            session.add(
                models.ReviewIssueModel(
                    id="iss_manual_finalized_rejected",
                    project_ref_id=project["project_ref_id"],
                    review_item_id=item["id"],
                    version_id=item["current_version_id"],
                    issue_no=99,
                    status="unresolved",
                    current_revision_id="rev_manual_finalized_rejected",
                    timestamp_ms=0,
                    frame_number=0,
                )
            )
            session.flush()

    with db.SessionLocal() as session:
        authority = session.get(models.ReviewItemModel, item["id"])
        active_finalization = session.get(
            models.FinalizationRecordModel,
            finalization["id"],
        )
    assert authority is not None
    assert authority.workflow_status == "finalized"
    assert active_finalization is not None
    assert active_finalization.status == "active"


def test_rerun_requires_project_scoped_principal_authorization(client: Any) -> None:
    project, _item = create_project_item(client, code="TQC-RERUN-AUTH")
    _complete_initial_qc(project["project_ref_id"])
    body = command(
        "RerunProjectTechnicalQc",
        {"project_ref_id": project["project_ref_id"]},
    )

    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        "/technical-qc/rerun",
        json=body,
        headers={
            **principal_headers(("prj_not_authorized",)),
            "Idempotency-Key": body["command_id"],
        },
    )

    assert response.status_code == 403, response.text
    assert api_error(response)["code"] == "PRINCIPAL_PERMISSION_DENIED"
    _qc, db, models = _modules()
    with db.SessionLocal() as session:
        task = session.scalar(
            select(models.TechnicalQcTaskModel).where(
                models.TechnicalQcTaskModel.project_ref_id
                == project["project_ref_id"]
            )
        )
    assert task is not None
    assert task.status == "complete"


def test_existing_manual_issue_cannot_be_reclassified_as_technical_qc(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-MANUAL-LAUNDER")
    manual_issue = create_issue(
        client,
        project["project_ref_id"],
        item,
        content="人工意见不得改绑为技术审查意见",
    )
    current_response = client.get(
        f"/api/v1/final-cut-review/projects/{project['project_ref_id']}"
        f"/items/{item['id']}"
    )
    assert current_response.status_code == 200, current_response.text
    current_item = api_data(current_response)
    finalize(
        client,
        project["project_ref_id"],
        item,
        if_match=current_item["lock_version"],
    )

    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        qc.discover_ready_technical_qc_tasks(session)
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]

    with db.SessionLocal() as session:
        session.add(
            models.TechnicalQcFindingModel(
                id="tqf_manual_launder_rejected",
                task_id=claim.task_id,
                project_ref_id=claim.project_ref_id,
                review_item_id=claim.review_item_id,
                version_id=claim.version_id,
                identity_hash="f" * 64,
                issue_id=manual_issue["id"],
                start_frame=manual_issue["frame_number"],
                end_frame=manual_issue["frame_number"],
                start_ms=manual_issue["timestamp_ms"],
                end_ms=manual_issue["timestamp_ms"] + 40,
                reason_codes=["black_frame"],
                value_details={"pblack": 100.0},
            )
        )
        with pytest.raises(IntegrityError, match="technical QC finding authority invalid"):
            session.flush()
        session.rollback()

    with db.SessionLocal() as session:
        finding = session.get(
            models.TechnicalQcFindingModel,
            "tqf_manual_launder_rejected",
        )
        issue = session.get(models.ReviewIssueModel, manual_issue["id"])
    assert finding is None
    assert issue is not None
    assert issue.current_revision_id == manual_issue["current_revision_id"]


def test_archive_keeps_existing_behavior_and_stops_queued_qc_claims(
    client: Any,
) -> None:
    project, _item = create_project_item(client, code="TQC-ARCHIVE-ACTIVE")
    _complete_initial_qc(project["project_ref_id"])
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text
    archive_body = command(
        "ArchiveProject",
        {"project_ref_id": project["project_ref_id"]},
    )

    archived = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}"
        "/archive",
        json=archive_body,
        headers={"If-Match": str(project["lock_version"])},
    )

    assert archived.status_code == 200, archived.text
    assert api_data(archived)["lifecycle_status"] == "archived"
    qc, db, models = _modules()
    with db.SessionLocal() as session:
        authority = session.get(
            models.ProjectRefModel,
            project["project_ref_id"],
        )
        task = session.scalar(
            select(models.TechnicalQcTaskModel).where(
                models.TechnicalQcTaskModel.project_ref_id
                == project["project_ref_id"]
            )
        )
    assert authority is not None
    assert authority.lifecycle_status == "archived"
    assert task is not None
    assert task.status == "queued"
    with db.SessionLocal.begin() as session:
        assert qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        ) == []


def test_archived_project_task_cannot_be_claimed_or_persist_late_findings(
    client: Any,
) -> None:
    project, item = create_project_item(client, code="TQC-ARCHIVE-GUARD")
    _complete_initial_qc(project["project_ref_id"])
    finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text
    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        project_row = session.get(
            models.ProjectRefModel,
            project["project_ref_id"],
        )
        assert project_row is not None
        project_row.lifecycle_status = "archived"

    with db.SessionLocal.begin() as session:
        assert qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        ) == []

    with db.SessionLocal.begin() as session:
        project_row = session.get(
            models.ProjectRefModel,
            project["project_ref_id"],
        )
        assert project_row is not None
        project_row.lifecycle_status = "active"
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]
        project_row.lifecycle_status = "archived"

    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )
    with db.SessionLocal.begin() as session:
        with pytest.raises(qc.TechnicalQcError) as caught:
            qc.persist_technical_qc_findings(
                session,
                claim,
                [interval],
            )
        assert caught.value.code == "TECHNICAL_QC_LEASE_LOST"
    with db.SessionLocal() as session:
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
    assert issues == []
    assert task is not None
    assert task.status == "running"


def test_worker_skips_when_authority_recovers_after_finding_persist_rejection(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, item = create_project_item(client, code="TQC-AUTHORITY-RECOVERY")
    _complete_initial_qc(project["project_ref_id"])
    finalize(client, project["project_ref_id"], item)
    queued = _rerun(client, project["project_ref_id"])
    assert queued.status_code == 202, queued.text
    qc, db, models = _modules()
    with db.SessionLocal.begin() as session:
        claim = qc.claim_technical_qc_tasks(
            session,
            limit=1,
            per_project_limit=1,
            max_attempts=3,
            lease_seconds=60,
        )[0]

    interval = qc.QcInterval(
        start_frame=5,
        end_frame=5,
        start_ms=200,
        end_ms=240,
        reason_codes=("black_frame",),
        values={"pblack": 100.0},
    )

    def archive_during_scan(*_args: Any, **_kwargs: Any) -> Any:
        with db.SessionLocal.begin() as session:
            project_row = session.get(
                models.ProjectRefModel,
                project["project_ref_id"],
            )
            assert project_row is not None
            project_row.lifecycle_status = "archived"
        return qc.TechnicalQcScanResult(
            intervals=(interval,),
            processed_ms=1_000,
            frames_processed=25,
            video_frame_count=25,
            video_duration_ms=1_000,
            total_ms=1_000,
        )

    real_persist = qc.persist_technical_qc_findings

    def persist_then_restore(*args: Any, **kwargs: Any) -> Any:
        try:
            return real_persist(*args, **kwargs)
        finally:
            with db.SessionLocal.begin() as session:
                project_row = session.get(
                    models.ProjectRefModel,
                    project["project_ref_id"],
                )
                assert project_row is not None
                project_row.lifecycle_status = "active"

    monkeypatch.setattr(qc, "scan_technical_qc_claim", archive_during_scan)
    monkeypatch.setattr(qc, "persist_technical_qc_findings", persist_then_restore)

    outcome = qc._process_technical_qc_claim(
        claim,
        _settings(concurrency=1, per_project=1),
        runtime_binding=None,
    )

    assert outcome == "skipped"
    with db.SessionLocal() as session:
        task = session.get(models.TechnicalQcTaskModel, claim.task_id)
        issues = list(
            session.scalars(
                select(models.ReviewIssueModel).where(
                    models.ReviewIssueModel.review_item_id == item["id"]
                )
            )
        )
    assert task is not None
    assert task.status == "running"
    assert task.claim_id == claim.claim_id
    assert issues == []
