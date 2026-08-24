from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session as OrmSession

from .conftest import (
    api_data,
    api_error,
    command,
    create_item,
    create_project,
    principal_headers,
    upload_video,
)
from .test_api_contract_runtime import finalize, prepare_ready_package


def _project_with_items(
    client: TestClient,
    code: str,
    count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    project = create_project(client, code)
    items = [
        create_item(
            client,
            project["project_ref_id"],
            upload_video(
                client,
                filename=f"{code.lower()}-{index}.mp4",
                seed=bytes([65 + index]),
            ),
            item_code=f"{code}-{index + 1}",
            episode_no=index + 1,
        )
        for index in range(count)
    ]
    return project, items


def _project_command(
    client: TestClient,
    project: dict[str, Any],
    command_type: str,
    route_suffix: str,
    *,
    lock_version: int | None = None,
    command_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Any]:
    body = command(
        command_type,
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id=command_id,
    )
    headers = {
        "Idempotency-Key": body["command_id"],
        **(
            {"If-Match": str(project["lock_version"] if lock_version is None else lock_version)}
            if lock_version is not None or "lock_version" in project
            else {}
        ),
        **(extra_headers or {}),
    }
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/{route_suffix}",
        json=body,
        headers=headers,
    )
    return body, response


def _finalize_all(
    client: TestClient,
    project: dict[str, Any],
    *,
    command_id: str,
) -> dict[str, Any]:
    _body, response = _project_command(
        client,
        project,
        "FinalizeProject",
        "finalize",
        command_id=command_id,
    )
    assert response.status_code == 200, response.text
    return api_data(response)


def test_finalize_project_handles_mixed_then_all_finalized_as_atomic_idempotent_summary(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        OutboxEventModel,
    )

    project, items = _project_with_items(client, "TASK3MIXED", 2)
    finalize(client, project["project_ref_id"], items[0])

    body, response = _project_command(
        client,
        project,
        "FinalizeProject",
        "finalize",
        command_id="task3-finalize-mixed",
    )
    assert response.status_code == 200, response.text
    summary = api_data(response)
    assert summary["project"]["completion_status"] == "completed"
    assert summary["project"]["lock_version"] == project["lock_version"] + 1
    assert {item["workflow_state"] for item in summary["items"]} == {"finalized"}

    replay = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize",
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "If-Match": str(project["lock_version"]),
        },
    )
    assert replay.status_code == 200, replay.text
    assert api_data(replay) == summary

    all_finalized_project = summary["project"]
    _second_body, all_finalized = _project_command(
        client,
        all_finalized_project,
        "FinalizeProject",
        "finalize",
        command_id="task3-finalize-all-noop",
    )
    assert all_finalized.status_code == 200, all_finalized.text
    assert api_data(all_finalized) == summary

    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"]
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == 2


def test_finalize_project_empty_project_is_safe_summary_noop(client: TestClient) -> None:
    project = create_project(client, "TASK3EMPTY")

    _body, response = _project_command(
        client,
        project,
        "FinalizeProject",
        "finalize",
        command_id="task3-finalize-empty",
    )

    assert response.status_code == 200, response.text
    summary = api_data(response)
    assert summary["project"]["completion_status"] == "empty"
    assert summary["project"]["lock_version"] == project["lock_version"]
    assert summary["items"] == []


def test_finalize_project_fault_on_later_item_rolls_back_all_business_side_effects(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.modules.final_cut_review.infra.repositories as repository_module
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        OperationLogModel,
        OutboxEventModel,
        ProjectRefModel,
        ReviewItemModel,
    )

    project, _items = _project_with_items(client, "TASK3ROLLBACK", 2)
    original_sha256 = repository_module.regular_file_sha256
    calls = 0

    def reject_second_source(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            return "0" * 64
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(repository_module, "regular_file_sha256", reject_second_source)
    _body, response = _project_command(
        client,
        project,
        "FinalizeProject",
        "finalize",
        command_id="task3-finalize-rollback",
    )

    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "FILE_HASH_MISMATCH"
    assert calls == 2
    request_id = response.json()["error"]["request_id"]
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"]
            )
        ) == 0
        assert set(
            session.scalars(
                select(ReviewItemModel.workflow_status).where(
                    ReviewItemModel.project_ref_id == project["project_ref_id"]
                )
            )
        ) == {"pending_review"}
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == 0
        assert session.get(ProjectRefModel, project["project_ref_id"]).lock_version == project["lock_version"]
        audit = session.scalars(
            select(OperationLogModel).where(OperationLogModel.request_id == request_id)
        ).one()
        assert audit.command_type == "FinalizeProject"
        assert audit.result == "error"
        assert audit.failure_stage == "execution"


def test_finalize_project_requires_project_if_match_and_project_scope_permission(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import FinalizationRecordModel

    project, _items = _project_with_items(client, "TASK3AUTH", 1)
    route = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize"

    missing_body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    missing = client.post(
        route,
        json=missing_body,
        headers={"Idempotency-Key": missing_body["command_id"]},
    )
    assert missing.status_code == 422
    assert api_error(missing)["code"] == "VALIDATION_ERROR"

    stale_body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    stale = client.post(
        route,
        json=stale_body,
        headers={"Idempotency-Key": stale_body["command_id"], "If-Match": "999"},
    )
    assert stale.status_code == 409
    assert api_error(stale)["code"] == "OPTIMISTIC_LOCK_CONFLICT"

    denied_body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    denied = client.post(
        route,
        json=denied_body,
        headers={
            **principal_headers(("prj_not_authorized",), principal_id="task3-denied", principal_kind="user"),
            "Idempotency-Key": denied_body["command_id"],
            "If-Match": str(project["lock_version"]),
        },
    )
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"]
            )
        ) == 0


def test_finalize_project_locks_project_before_items_in_stable_item_order(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.application.context import (
        ExecutionContext,
        PrincipalRef,
        WriteGuardState,
    )
    from backend.app.modules.final_cut_review.infra import database as database_module
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.settings import get_settings

    project, _items = _project_with_items(client, "TASK3LOCKS", 2)
    statements: list[str] = []

    def capture_statement(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if " from project_refs " in f" {normalized} " or " from review_items " in f" {normalized} ":
            statements.append(normalized)

    event.listen(database_module.engine, "before_cursor_execute", capture_statement)
    session = database_module.SessionLocal()
    try:
        result = SqlAlchemyReviewRepository(session, get_settings()).execute(
            "FinalizeProject",
            {"project_ref_id": project["project_ref_id"], "confirmed": True},
            ExecutionContext(
                entry_source="review",
                request_id="task3-lock-order",
                principal=PrincipalRef(kind="system", id="task3-lock-order", project_ref_ids=("*",)),
                write_guard=WriteGuardState(mode="none", verified=True),
            ),
            project["lock_version"],
        )
        assert result["project"]["completion_status"] == "completed"
    finally:
        session.rollback()
        session.close()
        event.remove(database_module.engine, "before_cursor_execute", capture_statement)

    project_lock_index = next(
        index for index, statement in enumerate(statements) if "from project_refs" in statement
    )
    item_lock_index = next(
        index
        for index, statement in enumerate(statements)
        if "from review_items" in statement and "order by review_items.id" in statement
    )
    assert project_lock_index < item_lock_index


def test_finalize_project_concurrent_identical_command_does_not_duplicate(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        OutboxEventModel,
        ProjectRefModel,
    )

    project, _items = _project_with_items(client, "TASK3DOUBLE", 2)
    body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-concurrent-double-click",
    )
    route = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize"
    barrier = threading.Barrier(2)

    def submit() -> Any:
        barrier.wait(timeout=5)
        return client.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _index: submit(), range(2)))

    assert any(response.status_code == 200 for response in responses)
    assert all(response.status_code in {200, 409} for response in responses)
    for response in responses:
        if response.status_code == 409:
            assert api_error(response)["code"] == "IDEMPOTENCY_CONFLICT"
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"]
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == 2
        assert session.get(ProjectRefModel, project["project_ref_id"]).lock_version == project["lock_version"] + 1


def test_finalize_project_lost_commit_ack_replays_committed_summary_without_duplicate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        IdempotencyRecordModel,
        OutboxEventModel,
    )

    project, _items = _project_with_items(client, "TASK3ACK", 1)
    original_commit = OrmSession.commit
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        original_commit(session)
        if not raised:
            raised = True
            raise RuntimeError("synthetic lost project finalization acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id=f"task3-ack-{uuid.uuid4().hex}",
    )
    route = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize"
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        response = no_raise.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        )

        assert response.status_code == 500
        assert raised is True
        with SessionLocal() as session:
            record = session.get(IdempotencyRecordModel, body["command_id"])
            assert record is not None and record.status_code == 200
            committed_summary = record.response_json
            assert session.scalar(
                select(func.count()).select_from(FinalizationRecordModel).where(
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"]
                )
            ) == 1

        monkeypatch.setattr(OrmSession, "commit", original_commit)
        replay = no_raise.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        )
        assert replay.status_code == 200, replay.text
        assert api_data(replay) == committed_summary
    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"]
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == 1


def test_revoke_project_finalization_invalidates_all_package_members_atomically_and_is_idempotent(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        OperationLogModel,
        OutboxEventModel,
    )

    project, _items = _project_with_items(client, "TASK3REVOKE", 2)
    finalized = _finalize_all(
        client,
        project,
        command_id="task3-revoke-seed-finalize",
    )
    package, _package_command = prepare_ready_package(client, project["project_ref_id"])
    route = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke"

    missing_lock_body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    missing_lock = client.post(
        route,
        json=missing_lock_body,
        headers={"Idempotency-Key": missing_lock_body["command_id"]},
    )
    assert missing_lock.status_code == 422
    assert api_error(missing_lock)["code"] == "VALIDATION_ERROR"

    denied_body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
    )
    denied = client.post(
        route,
        json=denied_body,
        headers={
            **principal_headers(("prj_not_authorized",), principal_id="task3-revoke-denied", principal_kind="user"),
            "Idempotency-Key": denied_body["command_id"],
            "If-Match": str(finalized["project"]["lock_version"]),
        },
    )
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-revoke-project",
    )
    response = client.post(
        route,
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "If-Match": str(finalized["project"]["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    summary = api_data(response)
    assert summary["project"]["completion_status"] == "in_progress"
    assert summary["project"]["lock_version"] == finalized["project"]["lock_version"] + 1
    assert {item["workflow_state"] for item in summary["items"]} == {"in_review"}
    assert {item["revocation_cleanup_status"] for item in summary["items"]} == {"pending"}

    replay = client.post(
        route,
        json=body,
        headers={
            "Idempotency-Key": body["command_id"],
            "If-Match": str(finalized["project"]["lock_version"]),
        },
    )
    assert replay.status_code == 200, replay.text
    assert api_data(replay) == summary

    no_target_body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-revoke-project-noop",
    )
    no_target = client.post(
        route,
        json=no_target_body,
        headers={
            "Idempotency-Key": no_target_body["command_id"],
            "If-Match": str(summary["project"]["lock_version"]),
        },
    )
    assert no_target.status_code == 200, no_target.text
    assert api_data(no_target) == summary

    with SessionLocal() as session:
        finalizations = list(
            session.scalars(
                select(FinalizationRecordModel).where(
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"]
                )
            )
        )
        assert len(finalizations) == 2
        assert {finalization.status for finalization in finalizations} == {"revoked"}
        invalidations = list(
            session.scalars(
                select(FinalizationPackageInvalidationModel).where(
                    FinalizationPackageInvalidationModel.package_id == package["id"]
                )
            )
        )
        assert len(invalidations) == 2
        assert {invalidation.cleanup_status for invalidation in invalidations} == {"pending"}
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        assert package_row.status == "invalidated"
        assert package_row.storage_reclaimed_at is None
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.finalization.revoked",
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(OperationLogModel).where(
                OperationLogModel.command_type == "RevokeProjectFinalization",
                OperationLogModel.result == "ok",
            )
        ) == 3


@pytest.mark.parametrize("item_count", [0, 1])
def test_revoke_project_finalization_without_finalized_targets_is_safe_summary_noop(
    client: TestClient,
    item_count: int,
) -> None:
    project, _items = _project_with_items(client, f"TASK3REVOKENOOP{item_count}", item_count)

    _body, response = _project_command(
        client,
        project,
        "RevokeProjectFinalization",
        "finalization/revoke",
        command_id=f"task3-revoke-noop-{item_count}",
    )

    assert response.status_code == 200, response.text
    summary = api_data(response)
    assert summary["project"]["lock_version"] == project["lock_version"]
    assert len(summary["items"]) == item_count
    assert summary["project"]["completion_status"] == ("empty" if item_count == 0 else "in_progress")


def test_revoke_project_finalization_later_database_fault_rolls_back_items_packages_events_and_project_lock(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        OutboxEventModel,
        ProjectRefModel,
        ReviewItemModel,
    )

    project, _items = _project_with_items(client, "TASK3REVOKEROLLBACK", 2)
    finalized = _finalize_all(
        client,
        project,
        command_id="task3-revoke-rollback-finalize",
    )
    package, _package_command = prepare_ready_package(client, project["project_ref_id"])
    original_event = SqlAlchemyReviewRepository._event
    revoke_events = 0

    def fail_second_revoke_event(
        repository: SqlAlchemyReviewRepository,
        event_type: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        nonlocal revoke_events
        if event_type == "review.finalization.revoked":
            revoke_events += 1
            if revoke_events == 2:
                raise RuntimeError("synthetic second project revoke event failure")
        original_event(repository, event_type, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyReviewRepository, "_event", fail_second_revoke_event)
    body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-revoke-rollback",
    )
    with pytest.raises(RuntimeError, match="synthetic second project revoke event failure"):
        client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke",
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(finalized["project"]["lock_version"]),
            },
        )

    assert revoke_events == 2
    with SessionLocal() as session:
        assert set(
            session.scalars(
                select(ReviewItemModel.workflow_status).where(
                    ReviewItemModel.project_ref_id == project["project_ref_id"]
                )
            )
        ) == {"finalized"}
        assert set(
            session.scalars(
                select(FinalizationRecordModel.status).where(
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"]
                )
            )
        ) == {"active"}
        assert session.scalar(
            select(func.count()).select_from(FinalizationPackageInvalidationModel).where(
                FinalizationPackageInvalidationModel.project_ref_id == project["project_ref_id"]
            )
        ) == 0
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.status == "ready"
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.finalization.revoked",
            )
        ) == 0
        assert session.get(ProjectRefModel, project["project_ref_id"]).lock_version == finalized["project"]["lock_version"]


def test_revoke_project_finalization_physical_package_cleanup_failure_stays_failed_without_database_rollback(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance_cleanup
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        ReviewItemModel,
    )
    from backend.app.package_builds import process_pending_packages

    project, _items = _project_with_items(client, "TASK3REVOKECLEANUP", 2)
    finalized = _finalize_all(
        client,
        project,
        command_id="task3-revoke-cleanup-finalize",
    )
    package, _package_command = prepare_ready_package(client, project["project_ref_id"])
    _body, response = _project_command(
        client,
        finalized["project"],
        "RevokeProjectFinalization",
        "finalization/revoke",
        command_id="task3-revoke-cleanup",
    )
    assert response.status_code == 200, response.text

    def fail_package_delete(*_args: Any, **_kwargs: Any) -> object:
        raise OSError("synthetic project package cleanup failure")

    monkeypatch.setattr(
        maintenance_cleanup,
        "delete_claimed_package_artifact",
        fail_package_delete,
    )
    cleanup = process_pending_packages()
    assert cleanup["failed_package_cleanups"] == 1

    with SessionLocal() as session:
        assert set(
            session.scalars(
                select(ReviewItemModel.workflow_status).where(
                    ReviewItemModel.project_ref_id == project["project_ref_id"]
                )
            )
        ) == {"in_review"}
        assert set(
            session.scalars(
                select(FinalizationRecordModel.status).where(
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"]
                )
            )
        ) == {"revoked"}
        invalidations = list(
            session.scalars(
                select(FinalizationPackageInvalidationModel).where(
                    FinalizationPackageInvalidationModel.package_id == package["id"]
                )
            )
        )
        assert len(invalidations) == 2
        assert {invalidation.cleanup_status for invalidation in invalidations} == {"failed"}
        assert all(invalidation.cleanup_attempts == 1 for invalidation in invalidations)
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        assert package_row.status == "invalidated"
        assert package_row.storage_reclaimed_at is None
