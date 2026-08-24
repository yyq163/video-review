from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.safe_files import PinnedRegularFile

from .conftest import (
    api_data,
    api_error,
    command,
    create_item,
    create_project,
    create_project_item,
    principal_headers,
    upload_video,
)


def _delete_item(
    client: TestClient,
    project_ref_id: str,
    item: dict[str, Any],
    *,
    command_id: str | None = None,
) -> Any:
    body = command(
        "DeleteReviewItem",
        {
            "project_ref_id": project_ref_id,
            "review_item_id": item["id"],
            "confirmed": True,
        },
        command_id=command_id,
    )
    return client.post(
        f"/api/v1/final-cut-review/edit/projects/{project_ref_id}/items/{item['id']}/delete",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "If-Match": str(item["lock_version"]),
        },
    )


def _seed_derivative_file(
    file_id: str,
    content: bytes,
    *,
    owner_principal_id: str = "test-system",
) -> Path:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FileObjectModel
    from backend.app.settings import get_settings

    target = get_settings().storage_root / "files" / file_id
    target.write_bytes(content)
    with SessionLocal.begin() as session:
        session.add(
            FileObjectModel(
                id=file_id,
                original_filename="derivative.bin",
                mime_type="application/octet-stream",
                file_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                storage_path=str(target),
                owner_principal_id=owner_principal_id,
                owner_principal_kind="system",
                duration_ms=10_000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                media_probe_version="task4-test",
            ),
        )
    return target


def test_delete_review_item_commits_database_and_restarts_durable_cleanup_after_file_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FileObjectModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewItemModel,
        ReviewVersionModel,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_DURABLE_ITEM")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        original_file_id = version.original_file_id
        file_object = observer.get(FileObjectModel, original_file_id)
        assert file_object is not None
        storage_path = Path(file_object.storage_path)
        assert storage_path.is_file()

    original_unlink = PinnedRegularFile.unlink

    def fail_physical_cleanup(
        _pinned: PinnedRegularFile,
        *,
        missing_ok: bool = True,
    ) -> bool:
        del missing_ok
        raise OSError("forced task4 physical cleanup failure")

    monkeypatch.setattr(PinnedRegularFile, "unlink", fail_physical_cleanup)
    response = _delete_item(client, project["project_ref_id"], item)

    assert response.status_code == 200, response.text
    assert api_data(response) == {
        "project_ref_id": project["project_ref_id"],
        "review_item_id": item["id"],
        "outcome": "deleted",
        "error_code": None,
    }
    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is None
        assert observer.get(ReviewVersionModel, item["current_version_id"]) is None
        assert observer.get(FileObjectModel, original_file_id) is None
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            )
        )
        assert intent is not None
        assert intent.status == "failed"
        assert intent.completed_at is None
        artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
            )
        )
        assert artifact is not None
        assert artifact.relative_logical_target == f"files/{original_file_id}"
        assert artifact.status == "failed"
        assert artifact.last_error_code is not None
    assert storage_path.is_file()

    monkeypatch.setattr(PinnedRegularFile, "unlink", original_unlink)
    from backend.app.maintenance_cleanup import process_review_deletion_intents

    removed, failed = process_review_deletion_intents(get_settings())
    assert (removed, failed) == (1, 0)
    assert not storage_path.exists()
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            )
        )
        assert intent is not None
        assert intent.status == "complete"
        assert intent.completed_at is not None


def test_delete_project_physically_removes_business_graph_and_is_immediately_unlisted(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FileObjectModel,
        OperationLogModel,
        OutboxEventModel,
        ProjectRefModel,
        ReviewDeletionIntentModel,
        ReviewIssueModel,
        ReviewIssueRevisionModel,
        ReviewAnnotationSetModel,
        ReviewThreadMessageModel,
        ReviewDecisionModel,
        ReviewItemModel,
        ReviewVersionModel,
    )
    from backend.tests.test_api_contract_runtime import annotation, create_issue

    project, item = create_project_item(client, code="TASK4_DELETE_PROJECT")
    issue = create_issue(
        client,
        project["project_ref_id"],
        item,
        content="task4 topology issue",
        ann=annotation("task4"),
    )
    message_body = command(
        "AddReviewMessage",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "version_id": item["current_version_id"],
            "issue_id": issue["id"],
            "content": "task4 topology reply",
        },
    )
    message_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues/{issue['id']}/messages",
        json=message_body,
        headers={"Idempotency-Key": message_body["command_id"]},
    )
    assert message_response.status_code == 200, message_response.text
    current_project = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}",
        ),
    )
    body = command(
        "DeleteProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="DeleteProject_task4_topology",
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/delete",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "If-Match": str(current_project["lock_version"]),
        },
    )

    assert response.status_code == 200, response.text
    assert api_data(response) == {
        "project_ref_id": project["project_ref_id"],
        "outcome": "deleted",
        "error_code": None,
    }
    assert project["project_ref_id"] not in {
        row["project_ref_id"]
        for row in api_data(client.get("/api/v1/final-cut-review/projects"))
    }
    with SessionLocal() as observer:
        assert observer.get(ProjectRefModel, project["project_ref_id"]) is None
        assert observer.get(ReviewItemModel, item["id"]) is None
        assert observer.get(ReviewVersionModel, item["current_version_id"]) is None
        assert observer.get(ReviewIssueModel, issue["id"]) is None
        assert observer.scalar(
            select(func.count())
            .select_from(ReviewIssueRevisionModel)
            .where(ReviewIssueRevisionModel.issue_id == issue["id"]),
        ) == 0
        for model in (
            ReviewAnnotationSetModel,
            ReviewThreadMessageModel,
            ReviewDecisionModel,
        ):
            assert observer.scalar(
                select(func.count())
                .select_from(model)
                .where(model.project_ref_id == project["project_ref_id"]),
            ) == 0
        assert observer.scalar(
            select(func.count())
            .select_from(OutboxEventModel)
            .where(OutboxEventModel.project_ref_id == project["project_ref_id"]),
        ) == 0
        assert observer.scalar(
            select(func.count())
            .select_from(FileObjectModel)
            .join(
                ReviewVersionModel,
                ReviewVersionModel.original_file_id == FileObjectModel.id,
                isouter=True,
            )
            .where(ReviewVersionModel.project_ref_id == project["project_ref_id"]),
        ) == 0
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.subject_type == "project",
            ),
        )
        assert intent is not None
        audit = observer.scalar(
            select(OperationLogModel).where(
                OperationLogModel.command_type == "DeleteProject",
                OperationLogModel.result == "ok",
            ),
        )
        assert audit is not None
        assert audit.idempotency_key_hash is not None
        assert body["command_id"] not in str(audit.__dict__)


def test_delete_review_items_batch_settles_each_item_without_rolling_back_successes(
    client: TestClient,
) -> None:
    from backend.tests.test_api_contract_runtime import create_issue

    project = create_project(client, code="TASK4_BATCH")
    first = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="task4-batch-first.mp4", seed=b"first"),
        item_code="TASK4_BATCH_FIRST",
    )
    locked = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="task4-batch-locked.mp4", seed=b"locked"),
        item_code="TASK4_BATCH_LOCKED",
    )
    third = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="task4-batch-third.mp4", seed=b"third"),
        item_code="TASK4_BATCH_THIRD",
    )
    create_issue(client, project["project_ref_id"], locked)
    body = command(
        "DeleteReviewItemsBatch",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_ids": [first["id"], locked["id"], third["id"]],
            "confirmed": True,
        },
        command_id="DeleteReviewItemsBatch_task4_independent",
    )
    response = client.post(
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}/items/delete-batch",
        json=body,
        headers={"Idempotency-Key": body["command_id"]},
    )

    assert response.status_code == 200, response.text
    assert api_data(response) == {
        "project_ref_id": project["project_ref_id"],
        "results": [
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": first["id"],
                "outcome": "deleted",
                "error_code": None,
            },
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": locked["id"],
                "outcome": "failed",
                "error_code": "RESOURCE_STATE_CONFLICT",
            },
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": third["id"],
                "outcome": "deleted",
                "error_code": None,
            },
        ],
    }
    remaining = {
        row["id"]
        for row in api_data(
            client.get(
                f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items",
            ),
        )
    }
    assert first["id"] not in remaining
    assert locked["id"] in remaining
    assert third["id"] not in remaining


def test_delete_review_item_rejects_active_finalization_then_allows_explicitly_revoked_item(
    client: TestClient,
) -> None:
    from backend.tests.test_api_contract_runtime import finalize

    project, item = create_project_item(client, code="TASK4_REVOKE_FIRST")
    finalize(client, project["project_ref_id"], item)
    finalized_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
        ),
    )
    rejected = _delete_item(client, project["project_ref_id"], finalized_item)
    assert rejected.status_code == 409
    assert api_error(rejected)["code"] == "RESOURCE_STATE_CONFLICT"

    revoke_body = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke_body,
        headers={
            "Idempotency-Key": revoke_body["command_id"],
            "If-Match": str(finalized_item["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    revoked_item = api_data(revoked)["review_item"]
    assert api_data(revoked)["cleanup_status"] == "complete"

    deleted = _delete_item(client, project["project_ref_id"], revoked_item)
    assert deleted.status_code == 200, deleted.text
    assert api_data(deleted)["outcome"] == "deleted"


def test_delete_review_item_captures_and_cleans_original_playback_thumbnail_and_variants(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        MediaDerivativeTaskModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )

    project, item = create_project_item(client, code="TASK4_ALL_MEDIA")
    playback_id = f"media_{uuid.uuid4().hex}"
    thumbnail_zero_id = f"media_{uuid.uuid4().hex}"
    thumbnail_fallback_id = f"media_{uuid.uuid4().hex}"
    paths = {
        playback_id: _seed_derivative_file(playback_id, b"task4-playback"),
        thumbnail_zero_id: _seed_derivative_file(thumbnail_zero_id, b"task4-thumbnail-zero"),
        thumbnail_fallback_id: _seed_derivative_file(
            thumbnail_fallback_id,
            b"task4-thumbnail-fallback",
        ),
    }
    with SessionLocal.begin() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        tasks = list(
            session.scalars(
                select(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.version_id == version.id,
                ),
            ),
        )
        for task in tasks:
            task.status = "ready"
            if task.kind == "playback_faststart":
                task.output_file_id = playback_id
                version.playback_asset_id = playback_id
            else:
                task.output_file_id = thumbnail_zero_id
                task.result_details = {
                    "variants": {
                        "0": thumbnail_zero_id,
                        "3000": thumbnail_fallback_id,
                    },
                }
                version.thumbnail_asset_id = thumbnail_zero_id
    response = _delete_item(client, project["project_ref_id"], item)
    assert response.status_code == 200, response.text
    for path in paths.values():
        assert not path.exists()
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "complete"
        artifacts = list(
            observer.scalars(
                select(ReviewDeletionArtifactModel).where(
                    ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
                ),
            ),
        )
        assert {artifact.artifact_kind for artifact in artifacts} == {
            "original_media",
            "playback_derivative",
            "thumbnail",
        }
        assert {artifact.status for artifact in artifacts} == {"deleted"}


def test_delete_review_item_keeps_a_shared_derivative_file_and_object(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FileObjectModel,
        MediaDerivativeTaskModel,
        ReviewVersionModel,
    )

    project = create_project(client, code="TASK4_SHARED_MEDIA")
    first = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="shared-first.mp4", seed=b"first"),
        item_code="TASK4_SHARED_FIRST",
    )
    second = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="shared-second.mp4", seed=b"second"),
        item_code="TASK4_SHARED_SECOND",
    )
    shared_id = f"media_{uuid.uuid4().hex}"
    shared_path = _seed_derivative_file(shared_id, b"shared-playback")
    with SessionLocal.begin() as session:
        for version_id in (first["current_version_id"], second["current_version_id"]):
            version = session.get(ReviewVersionModel, version_id)
            assert version is not None
            version.playback_asset_id = shared_id
            task = session.scalar(
                select(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.version_id == version_id,
                    MediaDerivativeTaskModel.kind == "playback_faststart",
                ),
            )
            assert task is not None
            task.status = "ready"
            task.output_file_id = shared_id

    response = _delete_item(client, project["project_ref_id"], first)
    assert response.status_code == 200, response.text
    assert shared_path.is_file()
    with SessionLocal() as observer:
        assert observer.get(FileObjectModel, shared_id) is not None
        second_version = observer.get(ReviewVersionModel, second["current_version_id"])
        assert second_version is not None
        assert second_version.playback_asset_id == shared_id


def test_one_failed_artifact_does_not_block_other_artifacts_and_intent_completes_only_after_retry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.maintenance_cleanup import process_review_deletion_intents
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        MediaDerivativeTaskModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_ARTIFACT_ISOLATION")
    blocked_id = f"media_{uuid.uuid4().hex}"
    blocked_path = _seed_derivative_file(blocked_id, b"blocked-derivative")
    with SessionLocal.begin() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        original_path = get_settings().storage_root / "files" / version.original_file_id
        version.playback_asset_id = blocked_id
        task = session.scalar(
            select(MediaDerivativeTaskModel).where(
                MediaDerivativeTaskModel.version_id == version.id,
                MediaDerivativeTaskModel.kind == "playback_faststart",
            ),
        )
        assert task is not None
        task.status = "ready"
        task.output_file_id = blocked_id
    original_unlink = PinnedRegularFile.unlink

    def fail_one_artifact(
        pinned: PinnedRegularFile,
        *,
        missing_ok: bool = True,
    ) -> bool:
        if pinned.name == blocked_id:
            raise OSError("forced one-artifact failure")
        return original_unlink(pinned, missing_ok=missing_ok)

    monkeypatch.setattr(PinnedRegularFile, "unlink", fail_one_artifact)
    response = _delete_item(client, project["project_ref_id"], item)
    assert response.status_code == 200, response.text
    assert blocked_path.is_file()
    assert not original_path.exists()
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "failed"
        artifacts = list(
            observer.scalars(
                select(ReviewDeletionArtifactModel).where(
                    ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
                ),
            ),
        )
        assert {artifact.status for artifact in artifacts} == {"deleted", "failed"}

    monkeypatch.setattr(PinnedRegularFile, "unlink", original_unlink)
    assert process_review_deletion_intents(get_settings()) == (1, 0)
    assert not blocked_path.exists()


def test_delete_review_item_fails_closed_on_symlink_replacement_without_deleting_target(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
        ReviewVersionModel,
    )
    from backend.app.settings import get_settings

    project, item = create_project_item(client, code="TASK4_SYMLINK_REPLACE")
    with SessionLocal() as observer:
        version = observer.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        storage_path = get_settings().storage_root / "files" / version.original_file_id
    original_process = maintenance_cleanup.process_review_deletion_intents
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        lambda *_args, **_kwargs: (0, 0),
    )
    response = _delete_item(client, project["project_ref_id"], item)
    assert response.status_code == 200, response.text
    unrelated = get_settings().storage_root / "unrelated-task4.bin"
    unrelated.write_bytes(b"must-survive")
    storage_path.unlink()
    os.symlink(unrelated, storage_path)
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_process,
    )

    assert original_process(get_settings()) == (0, 1)
    assert unrelated.read_bytes() == b"must-survive"
    assert storage_path.is_symlink()
    with SessionLocal() as observer:
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "failed"
        artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
            ),
        )
        assert artifact is not None and artifact.status == "failed"
        assert artifact.last_error_code == "DELETION_ARTIFACT_IDENTITY_MISMATCH"


def test_delete_review_item_cleans_an_exclusive_revoked_project_package(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
    )
    from backend.tests.test_api_contract_runtime import finalize, prepare_ready_package

    project, item = create_project_item(client, code="TASK4_PACKAGE")
    finalize(client, project["project_ref_id"], item)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    with SessionLocal() as observer:
        package_row = observer.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_path = Path(package_row.storage_path)
    assert package_path.is_file()
    finalized_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
        ),
    )
    revoke_body = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke_body,
        headers={
            "Idempotency-Key": revoke_body["command_id"],
            "If-Match": str(finalized_item["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    revoked_item = api_data(revoked)["review_item"]
    response = _delete_item(client, project["project_ref_id"], revoked_item)
    assert response.status_code == 200, response.text
    assert not package_path.exists()
    with SessionLocal() as observer:
        assert observer.get(FinalCutPackageSnapshotModel, package["id"]) is None
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.review_item_id_snapshot == item["id"],
            ),
        )
        assert intent is not None and intent.status == "complete"
        package_artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
                ReviewDeletionArtifactModel.artifact_kind == "project_package",
            ),
        )
        assert package_artifact is not None
        assert package_artifact.status == "deleted"


def test_review_deletion_cleanup_claim_is_leased_and_can_be_reclaimed_after_expiry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionIntentModel,
        utcnow,
    )
    from backend.app.settings import get_settings
    from datetime import timedelta

    project, item = create_project_item(client, code="TASK4_LEASE")
    original_process = maintenance_cleanup.process_review_deletion_intents
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        lambda *_args, **_kwargs: (0, 0),
    )
    response = _delete_item(client, project["project_ref_id"], item)
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        first_claim = maintenance_cleanup._claim_review_deletion_intents(
            session,
            utcnow(),
        )
    assert len(first_claim) == 1
    with SessionLocal() as session:
        assert maintenance_cleanup._claim_review_deletion_intents(
            session,
            utcnow(),
        ) == []
    with SessionLocal.begin() as session:
        intent = session.get(ReviewDeletionIntentModel, first_claim[0][0])
        assert intent is not None
        intent.cleanup_claim_expires_at = utcnow() - timedelta(seconds=1)
    with SessionLocal() as session:
        second_claim = maintenance_cleanup._claim_review_deletion_intents(
            session,
            utcnow(),
        )
    assert len(second_claim) == 1
    assert second_claim[0][0] == first_claim[0][0]
    assert second_claim[0][1] != first_claim[0][1]
    monkeypatch.setattr(
        maintenance_cleanup,
        "process_review_deletion_intents",
        original_process,
    )
    with SessionLocal.begin() as session:
        intent = session.get(ReviewDeletionIntentModel, second_claim[0][0])
        assert intent is not None
        intent.cleanup_claim_expires_at = utcnow() - timedelta(seconds=1)
    assert original_process(get_settings()) == (1, 0)


def test_delete_review_item_rejects_an_active_package_lease_without_auto_revoke_or_cleanup(
    client: TestClient,
) -> None:
    from datetime import timedelta

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ReviewItemModel,
        utcnow,
    )
    from backend.tests.test_api_contract_runtime import finalize, prepare_ready_package

    project, item = create_project_item(client, code="TASK4_PACKAGE_LEASE")
    finalize(client, project["project_ref_id"], item)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    current = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
        ),
    )
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    revoked_item = api_data(revoked)["review_item"]
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_row.download_lease_id = f"download_{uuid.uuid4().hex}"
        package_row.download_lease_expires_at = utcnow() + timedelta(minutes=5)

    response = _delete_item(client, project["project_ref_id"], revoked_item)
    assert response.status_code == 409
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"
    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is not None
        assert observer.get(FinalCutPackageSnapshotModel, package["id"]) is not None


def test_delete_review_item_does_not_delete_a_package_shared_with_another_finalization(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
    )
    from backend.tests.test_api_contract_runtime import finalize, prepare_ready_package

    project = create_project(client, code="TASK4_SHARED_PACKAGE")
    first = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="shared-package-first.mp4", seed=b"first"),
        item_code="TASK4_SHARED_PACKAGE_FIRST",
        episode_no=1,
    )
    second = create_item(
        client,
        project["project_ref_id"],
        upload_video(client, filename="shared-package-second.mp4", seed=b"second"),
        item_code="TASK4_SHARED_PACKAGE_SECOND",
        episode_no=2,
    )
    finalize(client, project["project_ref_id"], first)
    finalize(client, project["project_ref_id"], second)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    with SessionLocal() as observer:
        package_row = observer.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_path = Path(package_row.storage_path)
    first_current = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{first['id']}",
        ),
    )
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": first["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{first['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(first_current["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    response = _delete_item(
        client,
        project["project_ref_id"],
        api_data(revoked)["review_item"],
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as observer:
        assert observer.get(FinalCutPackageSnapshotModel, package["id"]) is not None
    assert package_path.is_file()


def test_delete_project_captures_and_cleans_revoked_project_package_artifact(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        ReviewDeletionArtifactModel,
        ReviewDeletionIntentModel,
    )
    from backend.tests.test_api_contract_runtime import finalize, prepare_ready_package

    project, item = create_project_item(client, code="TASK4_PROJECT_PACKAGE")
    finalize(client, project["project_ref_id"], item)
    package, _package_command = prepare_ready_package(
        client,
        project["project_ref_id"],
    )
    with SessionLocal() as observer:
        package_row = observer.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_path = Path(package_row.storage_path)
    current_item = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}",
        ),
    )
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    revoked = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current_item["lock_version"]),
        },
    )
    assert revoked.status_code == 200, revoked.text
    current_project = api_data(
        client.get(
            f"/api/v1/final-cut-review/projects/{project['project_ref_id']}",
        ),
    )
    deletion = command(
        "DeleteProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/delete",
        json=deletion,
        headers={
            "Idempotency-Key": deletion["command_id"],
            "If-Match": str(current_project["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    assert not package_path.exists()
    with SessionLocal() as observer:
        assert observer.get(FinalCutPackageSnapshotModel, package["id"]) is None
        intent = observer.scalar(
            select(ReviewDeletionIntentModel).where(
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
                ReviewDeletionIntentModel.subject_type == "project",
            ),
        )
        assert intent is not None and intent.status == "complete"
        package_artifact = observer.scalar(
            select(ReviewDeletionArtifactModel).where(
                ReviewDeletionArtifactModel.deletion_intent_id == intent.id,
                ReviewDeletionArtifactModel.artifact_kind == "project_package",
            ),
        )
        assert package_artifact is not None and package_artifact.status == "deleted"


def test_delete_review_items_batch_replays_once_and_binds_result_to_principal(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionIntentModel,
    )

    project = create_project(client, code="TASK4_BATCH_IDEMPOTENT")
    items = [
        create_item(
            client,
            project["project_ref_id"],
            upload_video(
                client,
                filename=f"task4-batch-idempotent-{index}.mp4",
                seed=str(index).encode(),
            ),
            item_code=f"TASK4_BATCH_IDEMPOTENT_{index}",
        )
        for index in range(2)
    ]
    body = command(
        "DeleteReviewItemsBatch",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_ids": [item["id"] for item in items],
            "confirmed": True,
        },
        command_id="DeleteReviewItemsBatch_task4_replay",
    )
    route = (
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        "/items/delete-batch"
    )
    headers = {"Idempotency-Key": body["command_id"]}
    first = client.post(route, json=body, headers=headers)
    replay = client.post(route, json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert api_data(replay) == api_data(first)
    with SessionLocal() as observer:
        intents = list(
            observer.scalars(
                select(ReviewDeletionIntentModel).where(
                    ReviewDeletionIntentModel.command_type
                    == "DeleteReviewItemsBatch",
                    ReviewDeletionIntentModel.project_ref_id_snapshot
                    == project["project_ref_id"],
                ),
            ),
        )
        assert len(intents) == 2
        assert {intent.review_item_id_snapshot for intent in intents} == {
            item["id"] for item in items
        }

    other_principal = client.post(
        route,
        json=body,
        headers={
            **principal_headers(
                (project["project_ref_id"],),
                principal_id="task4-other-principal",
            ),
            "Idempotency-Key": body["command_id"],
        },
    )
    assert other_principal.status_code == 403
    assert api_error(other_principal)["code"] == "PRINCIPAL_PERMISSION_DENIED"


def test_delete_review_items_batch_lost_commit_ack_replays_without_redeleting(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup

    from sqlalchemy.orm import Session as OrmSession

    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        OperationLogModel,
        ReviewDeletionIntentModel,
        ReviewItemModel,
    )
    from backend.app.settings import get_settings

    project = create_project(client, code="TASK4_BATCH_ACK")
    items = [
        create_item(
            client,
            project["project_ref_id"],
            upload_video(
                client,
                filename=f"task4-batch-ack-{index}.mp4",
                seed=f"ack-{index}".encode(),
            ),
            item_code=f"TASK4_BATCH_ACK_{index}",
        )
        for index in range(2)
    ]
    body = command(
        "DeleteReviewItemsBatch",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_ids": [item["id"] for item in items],
            "confirmed": True,
        },
        command_id="DeleteReviewItemsBatch_task4_lost_ack",
    )
    route = (
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        "/items/delete-batch"
    )
    original_commit = OrmSession.commit
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        original_commit(session)
        if not raised:
            raised = True
            raise RuntimeError("synthetic task4 batch acknowledgement loss")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    request_id = str(uuid.uuid4())
    with TestClient(
        client.app,
        headers=principal_headers(),
        raise_server_exceptions=False,
    ) as no_raise:
        lost = no_raise.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "X-Request-ID": request_id,
            },
        )
        assert lost.status_code == 500, lost.text
        monkeypatch.setattr(OrmSession, "commit", original_commit)
        replay = no_raise.post(
            route,
            json=body,
            headers={"Idempotency-Key": body["command_id"]},
        )
        assert replay.status_code == 200, replay.text
        assert [entry["outcome"] for entry in api_data(replay)["results"]] == [
            "deleted",
            "deleted",
        ]
    with SessionLocal() as observer:
        assert all(observer.get(ReviewItemModel, item["id"]) is None for item in items)
        intents = list(
            observer.scalars(
                select(ReviewDeletionIntentModel).where(
                    ReviewDeletionIntentModel.command_type
                    == "DeleteReviewItemsBatch",
                    ReviewDeletionIntentModel.project_ref_id_snapshot
                    == project["project_ref_id"],
                ),
            ),
        )
        assert len(intents) == 2
        audits = list(
            observer.scalars(
                select(OperationLogModel).where(
                    OperationLogModel.request_id == uuid.UUID(request_id).hex,
                ),
            ),
        )
        assert len(audits) == 1
        assert audits[0].result == "ok"
    with SessionLocal() as observer:
        assert observer.scalar(
            select(func.count())
            .select_from(ReviewDeletionIntentModel)
            .where(
                ReviewDeletionIntentModel.command_type
                == "DeleteReviewItemsBatch",
                ReviewDeletionIntentModel.project_ref_id_snapshot
                == project["project_ref_id"],
            ),
        ) == 2
    assert maintenance_cleanup.process_review_deletion_intents(
        get_settings(),
    ) == (2, 0)


def test_concurrent_delete_review_item_commands_create_one_intent_and_one_database_delete(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        ReviewDeletionIntentModel,
        ReviewItemModel,
    )

    project, item = create_project_item(client, code="TASK4_DELETE_CONCURRENT")
    route = (
        f"/api/v1/final-cut-review/edit/projects/{project['project_ref_id']}"
        f"/items/{item['id']}/delete"
    )

    def invoke(command_id: str) -> Any:
        body = command(
            "DeleteReviewItem",
            {
                "project_ref_id": project["project_ref_id"],
                "review_item_id": item["id"],
                "confirmed": True,
            },
            command_id=command_id,
        )
        return client.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": command_id,
                "If-Match": str(item["lock_version"]),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                invoke,
                ("DeleteReviewItem_task4_race_a", "DeleteReviewItem_task4_race_b"),
            ),
        )

    assert sum(response.status_code == 200 for response in responses) == 1
    assert all(response.status_code in {200, 404, 409} for response in responses)
    with SessionLocal() as observer:
        assert observer.get(ReviewItemModel, item["id"]) is None
        assert observer.scalar(
            select(func.count())
            .select_from(ReviewDeletionIntentModel)
            .where(ReviewDeletionIntentModel.review_item_id_snapshot == item["id"]),
        ) == 1
