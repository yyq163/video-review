from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from .conftest import api_data, api_error, command, create_project_item, principal_headers
from .test_api_contract_runtime import finalize, prepare_ready_package


def _revoke_packaged_finalization(
    client: TestClient,
    *,
    code: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    project, item = create_project_item(client, code)
    finalize(client, project["project_ref_id"], item, if_match=item["lock_version"])
    package, _ = prepare_ready_package(client, project["project_ref_id"])
    current_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
    revoke = command(
        "RevokeFinalization",
        {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "confirmed": True,
        },
    )
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/finalization/revoke",
        json=revoke,
        headers={
            "Idempotency-Key": revoke["command_id"],
            "If-Match": str(current_item["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    return project, item, package, api_data(response)


def _retry_route(project_id: str, item_id: str) -> str:
    return f"/api/v1/final-cut-review/review/projects/{project_id}/items/{item_id}/finalization/cleanup/retry"


def _retry_command(project_id: str, item_id: str, *, confirmed: bool = True) -> dict[str, Any]:
    return command(
        "RetryFinalizationCleanup",
        {
            "project_ref_id": project_id,
            "review_item_id": item_id,
            "confirmed": confirmed,
        },
    )


def _mark_cleanup_failed(
    package_id: str,
    *,
    lease_id: str | None = None,
    lease_delta: timedelta | None = None,
) -> str:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )

    with SessionLocal.begin() as session:
        package = session.get(FinalCutPackageSnapshotModel, package_id)
        assert package is not None
        package.download_lease_id = lease_id
        package.download_lease_expires_at = utcnow() + lease_delta if lease_id is not None and lease_delta is not None else None
        invalidation = session.scalar(select(FinalizationPackageInvalidationModel).where(FinalizationPackageInvalidationModel.package_id == package_id))
        assert invalidation is not None
        invalidation.cleanup_status = "failed"
        invalidation.last_error_code = "PACKAGE_DELETE_TIMEOUT"
        invalidation.cleanup_attempts = 1
        return invalidation.id


def test_retry_cleanup_review_entry_requires_confirmation_lock_scope_and_is_idempotent(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import aware
    from backend.app.modules.review_contracts.generated import (
        COMMAND_CAPABILITIES,
        EDIT_ENTRY_PROFILE,
        REVIEW_ENTRY_PROFILE,
    )
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )

    project, item, package, revoked = _revoke_packaged_finalization(
        client,
        code="CLEANRETRYAUTH",
    )
    assert COMMAND_CAPABILITIES["RetryFinalizationCleanup"] == "review.finalization.revoke"
    assert "review.finalization.revoke" in REVIEW_ENTRY_PROFILE
    assert "review.finalization.revoke" not in EDIT_ENTRY_PROFILE
    expired_cleanup_lease = "cleanup_" + "a" * 32
    invalidation_id = _mark_cleanup_failed(
        package["id"],
        lease_id=expired_cleanup_lease,
        lease_delta=timedelta(seconds=-1),
    )
    route = _retry_route(project["project_ref_id"], item["id"])
    lock_version = revoked["review_item"]["lock_version"]

    rejected_confirmation = _retry_command(
        project["project_ref_id"],
        item["id"],
        confirmed=False,
    )
    response = client.post(
        route,
        json=rejected_confirmation,
        headers={
            "Idempotency-Key": rejected_confirmation["command_id"],
            "If-Match": str(lock_version),
        },
    )
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"

    missing_lock_command = _retry_command(project["project_ref_id"], item["id"])
    missing_lock = client.post(
        route,
        json=missing_lock_command,
        headers={"Idempotency-Key": missing_lock_command["command_id"]},
    )
    assert missing_lock.status_code == 422
    assert api_error(missing_lock)["code"] == "VALIDATION_ERROR"

    wrong_lock_command = _retry_command(project["project_ref_id"], item["id"])
    wrong_lock = client.post(
        route,
        json=wrong_lock_command,
        headers={
            "Idempotency-Key": wrong_lock_command["command_id"],
            "If-Match": str(lock_version + 99),
        },
    )
    assert wrong_lock.status_code == 409
    assert api_error(wrong_lock)["code"] == "OPTIMISTIC_LOCK_CONFLICT"

    denied_command = _retry_command(project["project_ref_id"], item["id"])
    denied = client.post(
        route,
        json=denied_command,
        headers={
            **principal_headers(
                ("prj_" + "f" * 32,),
                principal_id="cleanup-retry-denied",
                principal_kind="user",
            ),
            "Idempotency-Key": denied_command["command_id"],
            "If-Match": str(lock_version),
        },
    )
    assert denied.status_code == 403
    assert api_error(denied)["code"] == "PRINCIPAL_PERMISSION_DENIED"

    valid = _retry_command(project["project_ref_id"], item["id"])
    response = client.post(
        route,
        json=valid,
        headers={
            "Idempotency-Key": valid["command_id"],
            "If-Match": str(lock_version),
        },
    )
    assert response.status_code == 200, response.text
    result = api_data(response)
    assert result == {
        "project_ref_id": project["project_ref_id"],
        "review_item_id": item["id"],
        "cleanup_status": "pending",
        "requeued_package_ids": [package["id"]],
    }

    replay = client.post(
        route,
        json=valid,
        headers={
            "Idempotency-Key": valid["command_id"],
            "If-Match": str(lock_version),
        },
    )
    assert replay.status_code == 200
    assert api_data(replay) == result

    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None
        assert package_row.download_lease_id is None
        assert package_row.download_lease_expires_at is None
        assert aware(package_row.expires_at) < utcnow()
        assert invalidation is not None
        assert invalidation.cleanup_status == "pending"
        assert invalidation.last_error_code is None

    edit_entry = client.post(
        route.replace("/review/", "/edit/"),
        json=_retry_command(project["project_ref_id"], item["id"]),
        headers={"If-Match": str(lock_version)},
    )
    assert edit_entry.status_code == 404


def test_retry_cleanup_preserves_active_cleanup_lease_and_marks_warning_pending(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )

    project, item, package, revoked = _revoke_packaged_finalization(
        client,
        code="CLEANRETRYACTIVE",
    )
    lease_id = "cleanup_" + "b" * 32
    invalidation_id = _mark_cleanup_failed(
        package["id"],
        lease_id=lease_id,
        lease_delta=timedelta(minutes=3),
    )
    with SessionLocal() as session:
        before = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert before is not None
        lease_expires_at = before.download_lease_expires_at

    retry = _retry_command(project["project_ref_id"], item["id"])
    response = client.post(
        _retry_route(project["project_ref_id"], item["id"]),
        json=retry,
        headers={
            "Idempotency-Key": retry["command_id"],
            "If-Match": str(revoked["review_item"]["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    assert api_data(response)["cleanup_status"] == "pending"
    assert api_data(response)["requeued_package_ids"] == []

    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None
        assert package_row.download_lease_id == lease_id
        assert package_row.download_lease_expires_at == lease_expires_at
        assert invalidation is not None
        assert invalidation.cleanup_status == "pending"
        assert invalidation.last_error_code is None


def test_retry_cleanup_rejects_active_non_cleanup_lease_without_mutation(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
    )

    project, item, package, revoked = _revoke_packaged_finalization(
        client,
        code="CLEANRETRYBUSY",
    )
    lease_id = "download_" + "c" * 32
    invalidation_id = _mark_cleanup_failed(
        package["id"],
        lease_id=lease_id,
        lease_delta=timedelta(minutes=3),
    )
    retry = _retry_command(project["project_ref_id"], item["id"])
    response = client.post(
        _retry_route(project["project_ref_id"], item["id"]),
        json=retry,
        headers={
            "Idempotency-Key": retry["command_id"],
            "If-Match": str(revoked["review_item"]["lock_version"]),
        },
    )
    assert response.status_code == 409
    assert api_error(response)["code"] == "RESOURCE_STATE_CONFLICT"

    with SessionLocal() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert package_row is not None and package_row.download_lease_id == lease_id
        assert invalidation is not None
        assert invalidation.cleanup_status == "failed"
        assert invalidation.last_error_code == "PACKAGE_DELETE_TIMEOUT"


def test_retry_cleanup_complete_is_idempotent_and_does_not_requeue(
    client: TestClient,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )

    project, item, package, revoked = _revoke_packaged_finalization(
        client,
        code="CLEANRETRYDONE",
    )
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        invalidation = session.scalar(select(FinalizationPackageInvalidationModel).where(FinalizationPackageInvalidationModel.package_id == package["id"]))
        assert package_row is not None and invalidation is not None
        package_row.storage_reclaimed_at = utcnow()
        package_row.storage_bytes = 0
        invalidation.cleanup_status = "complete"
        invalidation.last_error_code = None

    route = _retry_route(project["project_ref_id"], item["id"])
    for _attempt in range(2):
        retry = _retry_command(project["project_ref_id"], item["id"])
        response = client.post(
            route,
            json=retry,
            headers={
                "Idempotency-Key": retry["command_id"],
                "If-Match": str(revoked["review_item"]["lock_version"]),
            },
        )
        assert response.status_code == 200, response.text
        assert api_data(response) == {
            "project_ref_id": project["project_ref_id"],
            "review_item_id": item["id"],
            "cleanup_status": "complete",
            "requeued_package_ids": [],
        }


def test_worker_failure_then_manual_retry_is_claimable_on_the_next_cycle(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.app.maintenance_cleanup as maintenance
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.repositories import aware
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )
    from backend.app.settings import get_database_settings

    project, item, package, revoked = _revoke_packaged_finalization(
        client,
        code="CLEANRETRYWORKERFAIL",
    )
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_row.expires_at = utcnow() - timedelta(seconds=1)

    with SessionLocal() as session:
        first_claims = maintenance._claim_expired_packages(
            session,
            utcnow(),
            lease_seconds=7200,
        )
    assert [package_id for package_id, _claim_id in first_claims] == [package["id"]]
    first_claim_id = first_claims[0][1]

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("synthetic package cleanup timeout")

    monkeypatch.setattr(maintenance, "delete_claimed_package_artifact", fail_delete)
    with SessionLocal() as session:
        assert maintenance._cleanup_claimed_expired_packages(
            session,
            first_claims,
            get_database_settings().package_root,
        ) == (0, 1)

    with SessionLocal() as session:
        failed_package = session.get(FinalCutPackageSnapshotModel, package["id"])
        failed_invalidation = session.scalar(
            select(FinalizationPackageInvalidationModel).where(FinalizationPackageInvalidationModel.package_id == package["id"])
        )
        assert failed_package is not None
        assert failed_package.download_lease_id is None
        assert failed_package.download_lease_expires_at is None
        assert aware(failed_package.expires_at) > utcnow()
        assert failed_invalidation is not None
        assert failed_invalidation.cleanup_status == "failed"

    retry = _retry_command(project["project_ref_id"], item["id"])
    response = client.post(
        _retry_route(project["project_ref_id"], item["id"]),
        json=retry,
        headers={
            "Idempotency-Key": retry["command_id"],
            "If-Match": str(revoked["review_item"]["lock_version"]),
        },
    )
    assert response.status_code == 200, response.text
    assert api_data(response)["requeued_package_ids"] == [package["id"]]

    with SessionLocal() as session:
        second_claims = maintenance._claim_expired_packages(session, utcnow())
    assert [package_id for package_id, _claim_id in second_claims] == [package["id"]]
    assert second_claims[0][1] != first_claim_id


def test_maintenance_claim_transitions_failed_invalidation_back_to_pending(
    client: TestClient,
) -> None:
    from backend.app.maintenance_cleanup import _claim_expired_packages
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FinalCutPackageSnapshotModel,
        FinalizationPackageInvalidationModel,
        utcnow,
    )

    _project, _item, package, _revoked = _revoke_packaged_finalization(
        client,
        code="CLEANCLAIMPENDING",
    )
    invalidation_id = _mark_cleanup_failed(package["id"])
    with SessionLocal.begin() as session:
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        package_row.expires_at = utcnow() - timedelta(seconds=1)

    with SessionLocal() as session:
        claims = _claim_expired_packages(session, utcnow())
        assert [package_id for package_id, _claim_id in claims] == [package["id"]]
        invalidation = session.get(FinalizationPackageInvalidationModel, invalidation_id)
        assert invalidation is not None
        assert invalidation.cleanup_status == "pending"
        assert invalidation.last_error_code is None
        package_row = session.get(FinalCutPackageSnapshotModel, package["id"])
        assert package_row is not None
        assert package_row.download_lease_id == claims[0][1]
