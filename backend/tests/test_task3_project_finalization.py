from __future__ import annotations

import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
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


def _run_concurrent_requests(*requests: Any) -> list[Any]:
    barrier = threading.Barrier(len(requests))
    responses: list[Any] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()

    def run(request: Any) -> None:
        try:
            barrier.wait(timeout=5)
            response = request()
            with result_lock:
                responses.append(response)
        except BaseException as exc:
            with result_lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(request,), daemon=True) for request in requests]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads), "concurrent command did not terminate within 10 seconds"
    assert errors == []
    return responses


def _assert_allowed_concurrent_responses(responses: list[Any], *, successes: set[int]) -> None:
    assert sum(response.status_code == 200 for response in responses) in successes
    assert all(response.status_code in {200, 409} for response in responses)
    for response in responses:
        if response.status_code == 409:
            assert api_error(response)["code"] in {
                "IDEMPOTENCY_CONFLICT",
                "OPTIMISTIC_LOCK_CONFLICT",
                "RESOURCE_STATE_CONFLICT",
                "REVIEW_ITEM_FINALIZED",
            }


def _disable_sqlite_authority_guards(connection: Any) -> None:
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
    trigger_names = list(
        connection.scalars(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND (name LIKE 'trg_review_items_%' "
                "OR name LIKE 'trg_review_versions_%' "
                "OR name LIKE 'trg_finalizations_%')"
            )
        )
    )
    for trigger_name in trigger_names:
        connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')


def _insert_orphan_active_finalization(
    project_id: str,
    item: dict[str, Any],
    *,
    suffix: str,
) -> str:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        ReviewVersionModel,
    )

    finalization_id = f"fin_orphan_{suffix}"
    with SessionLocal.begin() as session:
        version = session.get(ReviewVersionModel, item["current_version_id"])
        assert version is not None
        session.add(
            FinalizationRecordModel(
                id=finalization_id,
                project_ref_id=project_id,
                review_item_id=item["id"],
                version_id=version.id,
                version_no=version.version_no,
                original_file_id=version.original_file_id,
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
        )
    return finalization_id


def _authority_business_snapshot(*project_ids: str) -> dict[str, Any]:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        OutboxEventModel,
        ProjectRefModel,
        ReviewItemModel,
    )

    with SessionLocal() as session:
        projects = list(
            session.scalars(
                select(ProjectRefModel)
                .where(ProjectRefModel.id.in_(project_ids))
                .order_by(ProjectRefModel.id)
            )
        )
        items = list(
            session.scalars(
                select(ReviewItemModel)
                .where(ReviewItemModel.project_ref_id.in_(project_ids))
                .order_by(ReviewItemModel.project_ref_id, ReviewItemModel.id)
            )
        )
        finalizations = list(
            session.scalars(
                select(FinalizationRecordModel)
                .where(FinalizationRecordModel.project_ref_id.in_(project_ids))
                .order_by(FinalizationRecordModel.project_ref_id, FinalizationRecordModel.id)
            )
        )
        events = list(
            session.scalars(
                select(OutboxEventModel)
                .where(OutboxEventModel.project_ref_id.in_(project_ids))
                .order_by(OutboxEventModel.id)
            )
        )
        packages = list(
            session.scalars(
                select(FinalCutPackageSnapshotModel)
                .where(FinalCutPackageSnapshotModel.project_ref_id.in_(project_ids))
                .order_by(FinalCutPackageSnapshotModel.id)
            )
        )
        invalidations = list(
            session.scalars(
                select(FinalizationPackageInvalidationModel)
                .where(FinalizationPackageInvalidationModel.project_ref_id.in_(project_ids))
                .order_by(FinalizationPackageInvalidationModel.id)
            )
        )
        return {
            "projects": tuple(
                (project.id, project.lock_version, project.updated_at)
                for project in projects
            ),
            "items": tuple(
                (
                    item.project_ref_id,
                    item.id,
                    item.workflow_status,
                    item.active_finalization_id,
                    item.current_version_id,
                    item.lock_version,
                    item.updated_at,
                )
                for item in items
            ),
            "finalizations": tuple(
                (
                    row.project_ref_id,
                    row.id,
                    row.review_item_id,
                    row.version_id,
                    row.status,
                    row.revoked_at,
                )
                for row in finalizations
            ),
            "events": tuple(
                (
                    row.event_id,
                    row.event_type,
                    row.aggregate_id,
                    row.aggregate_version,
                    row.finalization_id,
                )
                for row in events
            ),
            "packages": tuple(
                (row.id, row.status, row.storage_reclaimed_at, row.updated_at)
                for row in packages
            ),
            "invalidations": tuple(
                (
                    row.id,
                    row.finalization_id,
                    row.package_id,
                    row.cleanup_status,
                    row.cleanup_attempts,
                )
                for row in invalidations
            ),
        }


def _seed_corrupt_project_finalization_authority(
    client: TestClient,
    corruption: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from backend.app.modules.final_cut_review.infra import database as database_module

    item_count = 2 if corruption == "cross_item_pointer" else 1
    project, items = _project_with_items(
        client,
        f"TASK3AUTHORITY{corruption.upper()}",
        item_count,
    )
    finalization: dict[str, Any] | None = None
    if corruption in {"revoked_pointer", "cross_item_pointer", "nonfinalized_active"}:
        finalization = finalize(
            client,
            project["project_ref_id"],
            items[0],
            if_match=items[0]["lock_version"],
        )

    with database_module.engine.connect() as connection:
        _disable_sqlite_authority_guards(connection)

        if corruption == "missing_pointer":
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'finalized', active_finalization_id = NULL "
                    "WHERE id = :item_id"
                ),
                {"item_id": items[0]["id"]},
            )
        elif corruption == "wrong_pointer":
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'finalized', active_finalization_id = :pointer "
                    "WHERE id = :item_id"
                ),
                {"pointer": "fin_missing_authority", "item_id": items[0]["id"]},
            )
        elif corruption == "revoked_pointer":
            assert finalization is not None
            connection.execute(
                text(
                    "UPDATE finalizations SET status = 'revoked', revoked_at = CURRENT_TIMESTAMP "
                    "WHERE id = :finalization_id"
                ),
                {"finalization_id": finalization["id"]},
            )
        elif corruption == "cross_item_pointer":
            assert finalization is not None
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'finalized', active_finalization_id = :pointer "
                    "WHERE id = :item_id"
                ),
                {"pointer": finalization["id"], "item_id": items[1]["id"]},
            )
        elif corruption == "nonfinalized_active":
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'in_review' "
                    "WHERE id = :item_id"
                ),
                {"item_id": items[0]["id"]},
            )
        else:
            raise AssertionError(f"unknown corruption fixture {corruption}")
        connection.commit()
    return project, items


@pytest.mark.parametrize(
    "corruption",
    [
        "wrong_pointer",
        "missing_pointer",
        "revoked_pointer",
        "cross_item_pointer",
        "nonfinalized_active",
    ],
)
@pytest.mark.parametrize(
    ("command_type", "route_suffix"),
    [
        ("FinalizeProject", "finalize"),
        ("RevokeProjectFinalization", "finalization/revoke"),
    ],
)
def test_project_commands_fail_closed_on_bidirectional_finalization_authority_corruption(
    client: TestClient,
    corruption: str,
    command_type: str,
    route_suffix: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        IdempotencyRecordModel,
        OperationLogModel,
    )

    project, _items = _seed_corrupt_project_finalization_authority(client, corruption)
    command_id = f"task3-authority-{command_type.lower()}-{corruption}"
    _body, response = _project_command(
        client,
        project,
        command_type,
        route_suffix,
        command_id=command_id,
    )

    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"
    request_id = response.json()["error"]["request_id"]
    with SessionLocal() as session:
        assert session.get(IdempotencyRecordModel, command_id) is None
        audit = session.scalars(
            select(OperationLogModel).where(OperationLogModel.request_id == request_id)
        ).one()
        assert audit.command_type == command_type
        assert audit.result == "error"
        assert audit.error_code == "RESOURCE_STATE_CONFLICT"
        assert audit.failure_stage == "execution"
        assert audit.idempotency_key_hash == hashlib.sha256(command_id.encode()).hexdigest()
        assert command_id not in repr(vars(audit))


def _seed_direct_authority_case(
    client: TestClient,
    authority_case: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    from backend.app.modules.final_cut_review.infra import database as database_module

    if authority_case == "mixed_healthy_finalized_plus_pending_orphan":
        project, items = _project_with_items(client, "TASK3DIRECTMIXEDORPHAN", 2)
        finalize(client, project["project_ref_id"], items[0], if_match=items[0]["lock_version"])
        _insert_orphan_active_finalization(
            project["project_ref_id"],
            items[1],
            suffix="mixed_pending",
        )
        return project, (project["project_ref_id"],)

    if authority_case == "mixed_bad_finalized_pointer_plus_pending_target":
        project, items = _project_with_items(client, "TASK3DIRECTMIXEDPOINTER", 2)
        with database_module.engine.connect() as connection:
            _disable_sqlite_authority_guards(connection)
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'finalized', "
                    "active_finalization_id = 'fin_missing_mixed_target' WHERE id = :item_id"
                ),
                {"item_id": items[0]["id"]},
            )
            connection.commit()
        return project, (project["project_ref_id"],)

    if authority_case == "same_item_old_noncurrent_version_pointer":
        project, items = _project_with_items(client, "TASK3DIRECTOLDVERSION", 1)
        finalize(client, project["project_ref_id"], items[0], if_match=items[0]["lock_version"])
        new_file_id = upload_video(
            client,
            filename="task3-direct-current-v2.mp4",
            seed=b"V2",
        )
        new_version_id = f"ver_direct_v2_{uuid.uuid4().hex}"
        with database_module.engine.connect() as connection:
            _disable_sqlite_authority_guards(connection)
            connection.execute(
                text("UPDATE review_versions SET is_current = 0 WHERE id = :old_version_id"),
                {"old_version_id": items[0]["current_version_id"]},
            )
            connection.execute(
                text(
                    "INSERT INTO review_versions ("
                    "id, project_ref_id, review_item_id, previous_version_id, version_no, version_label, is_current, "
                    "original_file_id, original_filename, mime_type, file_size, sha256, duration_ms, width, height, "
                    "fps_num, fps_den, media_probe_version, responsible_editor_id, responsible_editor_name_snapshot, "
                    "playback_asset_id, thumbnail_asset_id, version_note, change_summary, lock_version, created_at"
                    ") SELECT "
                    ":new_version_id, old.project_ref_id, old.review_item_id, old.id, 2, 'V2', 1, "
                    "file.id, file.original_filename, file.mime_type, file.file_size, file.sha256, file.duration_ms, "
                    "file.width, file.height, file.fps_num, file.fps_den, file.media_probe_version, "
                    "old.responsible_editor_id, old.responsible_editor_name_snapshot, file.id, NULL, NULL, NULL, 1, CURRENT_TIMESTAMP "
                    "FROM review_versions old JOIN file_objects file ON file.id = :new_file_id "
                    "WHERE old.id = :old_version_id"
                ),
                {
                    "new_version_id": new_version_id,
                    "new_file_id": new_file_id,
                    "old_version_id": items[0]["current_version_id"],
                },
            )
            connection.execute(
                text("UPDATE review_items SET current_version_id = :version_id WHERE id = :item_id"),
                {"version_id": new_version_id, "item_id": items[0]["id"]},
            )
            connection.commit()
        return project, (project["project_ref_id"],)

    if authority_case == "cross_project_pointer":
        source_project, source_items = _project_with_items(client, "TASK3DIRECTCROSSSOURCE", 1)
        source_finalization = finalize(
            client,
            source_project["project_ref_id"],
            source_items[0],
            if_match=source_items[0]["lock_version"],
        )
        target_project, target_items = _project_with_items(client, "TASK3DIRECTCROSSTARGET", 1)
        with database_module.engine.connect() as connection:
            _disable_sqlite_authority_guards(connection)
            connection.execute(
                text(
                    "UPDATE review_items SET workflow_status = 'finalized', active_finalization_id = :pointer "
                    "WHERE id = :item_id"
                ),
                {"pointer": source_finalization["id"], "item_id": target_items[0]["id"]},
            )
            connection.commit()
        return target_project, (
            source_project["project_ref_id"],
            target_project["project_ref_id"],
        )

    if authority_case == "orphan_active_without_pointer":
        project, items = _project_with_items(client, "TASK3DIRECTORPHAN", 1)
        _insert_orphan_active_finalization(
            project["project_ref_id"],
            items[0],
            suffix="explicit_reverse",
        )
        return project, (project["project_ref_id"],)

    raise AssertionError(f"unknown direct authority case {authority_case}")


@pytest.mark.parametrize(
    "authority_case",
    [
        "mixed_healthy_finalized_plus_pending_orphan",
        "mixed_bad_finalized_pointer_plus_pending_target",
        "same_item_old_noncurrent_version_pointer",
        "cross_project_pointer",
        "orphan_active_without_pointer",
    ],
)
@pytest.mark.parametrize(
    ("command_type", "route_suffix"),
    [
        ("FinalizeProject", "finalize"),
        ("RevokeProjectFinalization", "finalization/revoke"),
    ],
)
def test_project_commands_directly_reject_mixed_version_cross_project_and_reverse_orphan_authority(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    authority_case: str,
    command_type: str,
    route_suffix: str,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        IdempotencyRecordModel,
        OperationLogModel,
    )

    project, observed_project_ids = _seed_direct_authority_case(client, authority_case)
    before = _authority_business_snapshot(*observed_project_ids)
    if authority_case in {
        "mixed_healthy_finalized_plus_pending_orphan",
        "orphan_active_without_pointer",
    }:
        import backend.app.modules.final_cut_review.infra.repositories as repository_module

        def reject_late_source_hash(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("authority validation must precede source hashing")

        monkeypatch.setattr(repository_module, "regular_file_sha256", reject_late_source_hash)
    command_id = f"task3-direct-{command_type.lower()}-{authority_case}"
    request_id_header = str(uuid.uuid4())
    request_id = request_id_header.replace("-", "")
    _body, response = _project_command(
        client,
        project,
        command_type,
        route_suffix,
        command_id=command_id,
        extra_headers={"X-Request-ID": request_id_header},
    )

    assert response.status_code == 409, response.text
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"
    assert _authority_business_snapshot(*observed_project_ids) == before
    key_hash = hashlib.sha256(command_id.encode()).hexdigest()
    with SessionLocal() as session:
        assert session.get(IdempotencyRecordModel, command_id) is None
        audits = list(
            session.scalars(
                select(OperationLogModel).where(
                    OperationLogModel.request_id == request_id,
                    OperationLogModel.command_type == command_type,
                )
            )
        )
        assert len(audits) == 1
        audit = audits[0]
        assert audit.result == "error"
        assert audit.error_code == "RESOURCE_STATE_CONFLICT"
        assert audit.failure_stage == "execution"
        assert audit.idempotency_key_hash == key_hash
        assert command_id not in repr(vars(audit))
        assert session.scalar(
            select(func.count()).select_from(OperationLogModel).where(
                OperationLogModel.idempotency_key_hash == key_hash,
                OperationLogModel.result.in_(("ok", "unknown")),
            )
        ) == 0


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
        IdempotencyRecordModel,
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
    body, response = _project_command(
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
        assert session.get(IdempotencyRecordModel, body["command_id"]) is None
        audit = session.scalars(
            select(OperationLogModel).where(OperationLogModel.request_id == request_id)
        ).one()
        assert audit.command_type == "FinalizeProject"
        assert audit.result == "error"
        assert audit.failure_stage == "execution"
        assert audit.idempotency_key_hash == hashlib.sha256(body["command_id"].encode()).hexdigest()
        assert body["command_id"] not in repr(vars(audit))


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


def test_sqlite_project_finalize_and_single_finalize_with_distinct_keys_terminate_without_duplicates(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        OutboxEventModel,
        ProjectRefModel,
    )

    project, items = _project_with_items(client, "TASK3RACEFINALIZE", 2)
    project_body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-race-project-finalize",
    )
    single_body = command(
        "FinalizeVersion",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": items[0]["id"],
            "version_id": items[0]["current_version_id"],
            "confirmed": True,
        },
        command_id="task3-race-single-finalize",
    )
    responses = _run_concurrent_requests(
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize",
            json=project_body,
            headers={
                "Idempotency-Key": project_body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        ),
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{items[0]['id']}/versions/{items[0]['current_version_id']}/finalize",
            json=single_body,
            headers={
                "Idempotency-Key": single_body["command_id"],
                "If-Match": str(items[0]["lock_version"]),
            },
        ),
    )
    _assert_allowed_concurrent_responses(responses, successes={1, 2})

    with SessionLocal() as session:
        finalizations = list(
            session.scalars(
                select(FinalizationRecordModel).where(
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"]
                )
            )
        )
        assert len(finalizations) == 2
        assert len({finalization.review_item_id for finalization in finalizations}) == 2
        assert {finalization.status for finalization in finalizations} == {"active"}
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == 2
        project_row = session.get(ProjectRefModel, project["project_ref_id"])
        assert project_row is not None and project_row.lock_version == project["lock_version"] + 1


def test_sqlite_project_revoke_and_single_revoke_with_distinct_keys_terminate_without_duplicate_invalidations(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        OutboxEventModel,
        ProjectRefModel,
    )

    project, _items = _project_with_items(client, "TASK3RACEREVOKE", 2)
    finalized = _finalize_all(client, project, command_id="task3-race-revoke-seed")
    package, _package_command = prepare_ready_package(client, project["project_ref_id"])
    first_item = finalized["items"][0]
    project_body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-race-project-revoke",
    )
    single_body = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": first_item["id"],
            "confirmed": True,
        },
        command_id="task3-race-single-revoke",
    )
    responses = _run_concurrent_requests(
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke",
            json=project_body,
            headers={
                "Idempotency-Key": project_body["command_id"],
                "If-Match": str(finalized["project"]["lock_version"]),
            },
        ),
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{first_item['id']}/finalization/revoke",
            json=single_body,
            headers={
                "Idempotency-Key": single_body["command_id"],
                "If-Match": str(first_item["lock_version"]),
            },
        ),
    )
    _assert_allowed_concurrent_responses(responses, successes={1, 2})

    with SessionLocal() as session:
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
        assert len({(row.finalization_id, row.package_id) for row in invalidations}) == 2
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.finalization.revoked",
            )
        ) == 2
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.status == "invalidated"
        project_row = session.get(ProjectRefModel, project["project_ref_id"])
        assert project_row is not None
        assert project_row.lock_version == finalized["project"]["lock_version"] + 1


def test_sqlite_project_finalize_and_project_revoke_with_distinct_keys_leave_uniform_project_state(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalizationRecordModel,
        IdempotencyRecordModel,
        OutboxEventModel,
        ProjectRefModel,
        ReviewItemModel,
    )

    project, items = _project_with_items(client, "TASK3RACEPROJECT", 2)
    finalize(client, project["project_ref_id"], items[0], if_match=items[0]["lock_version"])
    finalize_body = command(
        "FinalizeProject",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-race-project-finalize-project",
    )
    revoke_body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id="task3-race-project-revoke-project",
    )
    responses = _run_concurrent_requests(
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalize",
            json=finalize_body,
            headers={
                "Idempotency-Key": finalize_body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        ),
        lambda: client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke",
            json=revoke_body,
            headers={
                "Idempotency-Key": revoke_body["command_id"],
                "If-Match": str(project["lock_version"]),
            },
        ),
    )
    _assert_allowed_concurrent_responses(responses, successes={1})

    with SessionLocal() as session:
        active_count = session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"],
                FinalizationRecordModel.status == "active",
            )
        )
        assert active_count in {0, 2}
        finalized_item_count = session.scalar(
            select(func.count()).select_from(ReviewItemModel).where(
                ReviewItemModel.project_ref_id == project["project_ref_id"],
                ReviewItemModel.workflow_status == "finalized",
            )
        )
        assert finalized_item_count == active_count
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.version.finalized",
            )
        ) == (2 if active_count == 2 else 1)
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.finalization.revoked",
            )
        ) == (1 if active_count == 0 else 0)
        project_row = session.get(ProjectRefModel, project["project_ref_id"])
        assert project_row is not None and project_row.lock_version == project["lock_version"] + 1
        records = list(
            session.scalars(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.key.in_((finalize_body["command_id"], revoke_body["command_id"]))
                )
            )
        )
        assert len(records) == 1
        assert records[0].status_code == 200


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


def test_revoke_project_finalization_lost_commit_ack_replays_identical_summary_without_duplicate_side_effects(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        FinalizationRecordModel,
        IdempotencyRecordModel,
        OperationLogModel,
        OutboxEventModel,
        ProjectRefModel,
    )

    project, _items = _project_with_items(client, "TASK3REVOKEACK", 2)
    finalized = _finalize_all(
        client,
        project,
        command_id="task3-revoke-ack-finalize",
    )
    package, _package_command = prepare_ready_package(client, project["project_ref_id"])
    original_commit = OrmSession.commit
    raised = False

    def commit_then_raise(session: OrmSession) -> None:
        nonlocal raised
        original_commit(session)
        if not raised:
            raised = True
            raise RuntimeError("synthetic lost project revocation acknowledgement")

    monkeypatch.setattr(OrmSession, "commit", commit_then_raise)
    body = command(
        "RevokeProjectFinalization",
        {"project_ref_id": project["project_ref_id"], "confirmed": True},
        command_id=f"task3-revoke-ack-{uuid.uuid4().hex}",
    )
    route = f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke"
    with TestClient(client.app, headers=principal_headers(), raise_server_exceptions=False) as no_raise:
        response = no_raise.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(finalized["project"]["lock_version"]),
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
                    FinalizationRecordModel.project_ref_id == project["project_ref_id"],
                    FinalizationRecordModel.status == "revoked",
                )
            ) == 2
            assert session.scalar(
                select(func.count()).select_from(OutboxEventModel).where(
                    OutboxEventModel.project_ref_id == project["project_ref_id"],
                    OutboxEventModel.event_type == "review.finalization.revoked",
                )
            ) == 2
            assert session.scalar(
                select(func.count()).select_from(FinalizationPackageInvalidationModel).where(
                    FinalizationPackageInvalidationModel.package_id == package["id"]
                )
            ) == 2
            package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
            assert package_row is not None and package_row.status == "invalidated"
            project_row = session.get(ProjectRefModel, project["project_ref_id"])
            assert project_row is not None
            assert project_row.lock_version == finalized["project"]["lock_version"] + 1
            audits = list(
                session.scalars(
                    select(OperationLogModel).where(
                        OperationLogModel.command_type == "RevokeProjectFinalization",
                        OperationLogModel.idempotency_key_hash == hashlib.sha256(body["command_id"].encode()).hexdigest(),
                    )
                )
            )
            assert [audit.result for audit in audits] == ["ok"]

        monkeypatch.setattr(OrmSession, "commit", original_commit)
        replay = no_raise.post(
            route,
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(finalized["project"]["lock_version"]),
            },
        )
        assert replay.status_code == 200, replay.text
        assert api_data(replay) == committed_summary

    with SessionLocal() as session:
        assert session.scalar(
            select(func.count()).select_from(FinalizationRecordModel).where(
                FinalizationRecordModel.project_ref_id == project["project_ref_id"],
                FinalizationRecordModel.status == "revoked",
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(OutboxEventModel).where(
                OutboxEventModel.project_ref_id == project["project_ref_id"],
                OutboxEventModel.event_type == "review.finalization.revoked",
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(FinalizationPackageInvalidationModel).where(
                FinalizationPackageInvalidationModel.package_id == package["id"]
            )
        ) == 2
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None and package_row.status == "invalidated"
        project_row = session.get(ProjectRefModel, project["project_ref_id"])
        assert project_row is not None
        assert project_row.lock_version == finalized["project"]["lock_version"] + 1
        audits = list(
            session.scalars(
                select(OperationLogModel).where(
                    OperationLogModel.command_type == "RevokeProjectFinalization",
                    OperationLogModel.idempotency_key_hash == hashlib.sha256(body["command_id"].encode()).hexdigest(),
                )
            )
        )
        assert [audit.result for audit in audits] == ["ok", "ok"]


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
        IdempotencyRecordModel,
        OperationLogModel,
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
    request_id_header = "00000000-0000-4000-8000-000000000301"
    request_id = request_id_header.replace("-", "")
    with pytest.raises(RuntimeError, match="synthetic second project revoke event failure"):
        client.post(
            f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/finalization/revoke",
            json=body,
            headers={
                "Idempotency-Key": body["command_id"],
                "If-Match": str(finalized["project"]["lock_version"]),
                "X-Request-ID": request_id_header,
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
        assert session.get(IdempotencyRecordModel, body["command_id"]) is None
        audit = session.scalars(
            select(OperationLogModel).where(OperationLogModel.request_id == request_id)
        ).one()
        assert audit.command_type == "RevokeProjectFinalization"
        assert audit.result == "error"
        assert audit.failure_stage == "execution"
        assert audit.idempotency_key_hash == hashlib.sha256(body["command_id"].encode()).hexdigest()
        assert body["command_id"] not in repr(vars(audit))


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
