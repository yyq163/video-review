from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.app import thumbnail_similarity as thumbnail_similarity_module
from backend.app.modules.final_cut_review.infra.database import Base
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewItemModel,
    ReviewVersionModel,
    utcnow,
)
from backend.app.thumbnail_similarity import (
    THUMBNAIL_FALLBACK_FRAME_MS,
    reconcile_project_thumbnail_groups,
    thumbnail_signatures_are_similar,
    thumbnail_variant_file_ids,
)


def _file(file_id: str, mime_type: str = "image/jpeg") -> FileObjectModel:
    return FileObjectModel(
        id=file_id,
        original_filename=f"{file_id}.bin",
        mime_type=mime_type,
        file_size=1,
        sha256=(file_id.encode().hex() + "0" * 64)[:64],
        storage_path=f"/data/files/{file_id}",
        owner_principal_id="owner",
        owner_principal_kind="system",
        duration_ms=10_000,
        width=320,
        height=180,
        fps_num=25,
        fps_den=1,
        media_probe_version="test",
    )


def _add_current_version(
    session: Session,
    *,
    suffix: str,
    signature: str | None,
    fallback_ready: bool,
    is_current: bool = True,
) -> tuple[ReviewVersionModel, MediaDerivativeTaskModel]:
    item_id = f"item_{suffix}"
    version_id = f"ver_{suffix}"
    source_id = f"source_{suffix}"
    frame_zero_id = f"thumb0_{suffix}"
    frame_three_id = f"thumb3_{suffix}"
    item = ReviewItemModel(
        id=item_id,
        project_ref_id="prj_similarity",
        item_code=suffix,
        episode_no=int(suffix) if suffix.isdigit() else None,
        title=suffix,
        workflow_status="pending_review",
        current_version_id=None,
        current_version_is_current=True,
        lock_version=1,
    )
    session.add(item)
    session.add_all(
        [
            _file(source_id, "video/mp4"),
            _file(frame_zero_id),
            _file(frame_three_id),
        ]
    )
    session.flush()
    version = ReviewVersionModel(
        id=version_id,
        project_ref_id="prj_similarity",
        review_item_id=item_id,
        previous_version_id=None,
        version_no=1,
        version_label="V1",
        is_current=is_current,
        original_file_id=source_id,
        original_filename=f"{suffix}.mp4",
        mime_type="video/mp4",
        file_size=1,
        sha256=(source_id.encode().hex() + "0" * 64)[:64],
        duration_ms=10_000,
        width=320,
        height=180,
        fps_num=25,
        fps_den=1,
        media_probe_version="test",
        thumbnail_asset_id=frame_zero_id,
        lock_version=1,
    )
    session.add(version)
    session.flush()
    if is_current:
        item.current_version_id = version.id
    details: dict[str, object] = {
        "variants": {
            "0": frame_zero_id,
            **({str(THUMBNAIL_FALLBACK_FRAME_MS): frame_three_id} if fallback_ready else {}),
        },
        "desired_frame_ms": 0,
    }
    if signature is not None:
        details["frame_signature"] = signature
    task = MediaDerivativeTaskModel(
        id=f"task_{suffix}",
        project_ref_id="prj_similarity",
        review_item_id=item_id,
        version_id=version_id,
        kind="thumbnail",
        status="ready",
        attempts=1,
        output_file_id=frame_zero_id,
        result_details=details,
    )
    session.add(task)
    session.flush()
    return version, task


def test_similarity_is_strictly_over_eighty_five_percent() -> None:
    assert thumbnail_signatures_are_similar("0000000000000000", f"{(1 << 9) - 1:016x}")
    assert not thumbnail_signatures_are_similar("0000000000000000", f"{(1 << 10) - 1:016x}")
    assert not thumbnail_signatures_are_similar("not-a-signature", "0000000000000000")


def test_project_reconciliation_uses_fallback_for_similar_current_group_only() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        first_version, first_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=True,
        )
        second_version, second_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=True,
        )
        distinct_version, distinct_task = _add_current_version(
            session,
            suffix="3",
            signature="ffffffffffffffff",
            fallback_ready=True,
        )
        historical_version, historical_task = _add_current_version(
            session,
            suffix="history",
            signature="0000000000000000",
            fallback_ready=True,
            is_current=False,
        )

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        session.flush()

        assert first_task.output_file_id == "thumb3_1"
        assert second_task.output_file_id == "thumb3_2"
        assert first_version.thumbnail_asset_id == "thumb3_1"
        assert second_version.thumbnail_asset_id == "thumb3_2"
        assert distinct_task.output_file_id == "thumb0_3"
        assert distinct_version.thumbnail_asset_id == "thumb0_3"
        assert historical_task.output_file_id == "thumb0_history"
        assert historical_version.thumbnail_asset_id == "thumb0_history"


def test_missing_fallback_is_queued_and_legacy_discovery_is_explicit() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        _first_version, first_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=False,
        )
        _second_version, second_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=False,
        )
        _legacy_version, legacy_task = _add_current_version(
            session,
            suffix="3",
            signature=None,
            fallback_ready=False,
        )

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        assert first_task.status == "queued"
        assert second_task.status == "queued"
        assert legacy_task.status == "ready"

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=True,
        )
        assert legacy_task.status == "queued"


def test_reconciliation_preserves_retry_backoff_and_exhausted_failure_for_same_variant() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        _first_version, first_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=False,
        )
        _second_version, second_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=False,
        )

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        retry_at = utcnow() + timedelta(minutes=5)
        first_task.status = "failed"
        first_task.attempts = 3
        first_task.error_code = "MEDIA_TRANSFORM_FAILED"
        first_task.failure_details = {"stage": "transform", "attempt": 3}
        second_task.status = "queued"
        second_task.attempts = 2
        second_task.next_attempt_at = retry_at
        second_task.error_code = "MEDIA_TRANSFORM_FAILED"
        second_task.failure_details = {"stage": "transform", "attempt": 2}
        session.flush()

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )

        assert (first_task.status, first_task.attempts, first_task.error_code) == (
            "failed",
            3,
            "MEDIA_TRANSFORM_FAILED",
        )
        assert first_task.failure_details == {"stage": "transform", "attempt": 3}
        assert (second_task.status, second_task.attempts) == ("queued", 2)
        assert second_task.next_attempt_at == retry_at
        assert second_task.failure_details == {"stage": "transform", "attempt": 2}


def test_reconciliation_preserves_missing_artifact_rebuild_for_cached_variant_id() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        _first_version, first_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=True,
        )
        _second_version, second_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=True,
        )
        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        retry_at = utcnow() + timedelta(minutes=5)
        first_task.status = "queued"
        first_task.attempts = 2
        first_task.next_attempt_at = retry_at
        first_task.error_code = "MEDIA_DERIVATIVE_ARTIFACT_MISSING"
        first_task.failure_details = {"stage": "artifact_read"}
        session.flush()

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
            changed_task_id=second_task.id,
        )

        assert first_task.status == "queued"
        assert first_task.attempts == 2
        assert first_task.next_attempt_at == retry_at
        assert first_task.error_code == "MEDIA_DERIVATIVE_ARTIFACT_MISSING"
        assert first_task.failure_details == {"stage": "artifact_read"}


def test_reconciliation_does_not_steal_a_running_task_lease_when_variant_is_cached() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        _first_version, first_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=True,
        )
        _second_version, _second_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=True,
        )
        first_task.status = "running"
        first_task.lease_id = "lease-live"
        first_task.lease_expires_at = utcnow() + timedelta(minutes=5)
        session.flush()

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )

        assert first_task.status == "running"
        assert first_task.lease_id == "lease-live"
        assert first_task.lease_expires_at is not None


def test_current_version_departure_reverts_surviving_group_member_to_frame_zero() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        departing_version, departing_task = _add_current_version(
            session,
            suffix="1",
            signature="0000000000000000",
            fallback_ready=True,
        )
        surviving_version, surviving_task = _add_current_version(
            session,
            suffix="2",
            signature="0000000000000001",
            fallback_ready=True,
        )

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        assert departing_task.output_file_id == "thumb3_1"
        assert surviving_task.output_file_id == "thumb3_2"

        departing_item = session.get(ReviewItemModel, departing_version.review_item_id)
        assert departing_item is not None
        departing_item.current_version_id = None
        session.flush()
        departing_version.is_current = False
        session.flush()

        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )

        assert surviving_task.output_file_id == "thumb0_2"
        assert surviving_version.thumbnail_asset_id == "thumb0_2"


def test_incremental_reconciliation_compares_only_the_changed_signature(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        tasks = [
            _add_current_version(
                session,
                suffix=str(index),
                signature=f"{index:016x}",
                fallback_ready=True,
            )[1]
            for index in range(1, 9)
        ]
        comparisons = 0
        original = thumbnail_similarity_module.thumbnail_signatures_are_similar

        def count_comparison(left: str, right: str) -> bool:
            nonlocal comparisons
            comparisons += 1
            return original(left, right)

        monkeypatch.setattr(
            thumbnail_similarity_module,
            "thumbnail_signatures_are_similar",
            count_comparison,
        )
        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
            changed_task_id=tasks[-1].id,
        )

        assert comparisons == len(tasks) - 1


def test_incremental_steady_state_does_not_rewrite_unchanged_tasks() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ProjectRefModel(
                id="prj_similarity",
                project_code="similarity",
                project_name="Similarity",
                description="",
                lifecycle_status="active",
                lock_version=1,
            )
        )
        tasks = [
            _add_current_version(
                session,
                suffix=str(index),
                signature=f"{index:016x}",
                fallback_ready=True,
            )[1]
            for index in range(1, 9)
        ]
        reconcile_project_thumbnail_groups(
            session,
            "prj_similarity",
            discover_legacy_signatures=False,
        )
        session.commit()

        update_statements: list[str] = []

        def capture_updates(
            _connection,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.lstrip().upper().startswith("UPDATE"):
                update_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_updates)
        try:
            reconcile_project_thumbnail_groups(
                session,
                "prj_similarity",
                discover_legacy_signatures=False,
                changed_task_id=tasks[-1].id,
            )
            session.flush()
        finally:
            event.remove(engine, "before_cursor_execute", capture_updates)

        assert update_statements == []


def test_variant_file_ids_are_bounded_to_supported_frames() -> None:
    assert thumbnail_variant_file_ids(
        {"variants": {"0": "zero", "3000": "three", "9000": "ignored", "bad": 1}}
    ) == {"zero", "three"}
