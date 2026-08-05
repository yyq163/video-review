from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra.database import Base
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    FileObjectModel,
    FinalizationRecordModel,
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewItemModel,
    ReviewVersionModel,
)
from backend.app.thumbnail_backfill import (
    enqueue_thumbnail_backfill_candidates,
    find_thumbnail_backfill_candidates,
)


NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def _seed_current_version(
    session: Session,
    suffix: str,
    *,
    deleted: bool = False,
    with_thumbnail_asset: bool = False,
    with_thumbnail_task: bool = False,
    finalized: bool = False,
) -> tuple[str, str, str]:
    project_id = f"prj_{suffix}"
    item_id = f"item_{suffix}"
    version_id = f"ver_{suffix}"
    file_id = f"file_{suffix}"
    session.add(
        ProjectRefModel(
            id=project_id,
            project_code=f"code-{suffix}",
            project_name=f"project-{suffix}",
            description="",
            source="local",
            lifecycle_status="active",
            deleted_at=NOW if deleted else None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.add(
        FileObjectModel(
            id=file_id,
            original_filename="source.mp4",
            mime_type="video/mp4",
            file_size=100,
            sha256="a" * 64,
            storage_path=f"/storage/files/{file_id}",
            owner_principal_id="owner",
            owner_principal_kind="system",
            duration_ms=10_000,
            width=1920,
            height=1080,
            fps_num=25,
            fps_den=1,
            media_probe_version="test",
            created_at=NOW,
        )
    )
    item = ReviewItemModel(
        id=item_id,
        project_ref_id=project_id,
        item_code=f"item-{suffix}",
        episode_no=1,
        title=f"episode-{suffix}",
        workflow_status="in_review",
        current_version_id=None,
        current_version_is_current=True,
        lock_version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(item)
    session.flush()
    version = ReviewVersionModel(
        id=version_id,
        project_ref_id=project_id,
        review_item_id=item_id,
        previous_version_id=None,
        version_no=1,
        version_label="V1",
        is_current=True,
        original_file_id=file_id,
        original_filename="source.mp4",
        mime_type="video/mp4",
        file_size=100,
        sha256="a" * 64,
        duration_ms=10_000,
        width=1920,
        height=1080,
        fps_num=25,
        fps_den=1,
        media_probe_version="test",
        thumbnail_asset_id="thumb-existing" if with_thumbnail_asset else None,
        lock_version=1,
        created_at=NOW,
    )
    session.add(version)
    session.flush()
    item.current_version_id = version_id
    if with_thumbnail_task:
        session.add(
            MediaDerivativeTaskModel(
                id=f"mdt_{suffix}",
                project_ref_id=project_id,
                review_item_id=item_id,
                version_id=version_id,
                kind="thumbnail",
                status="queued",
                attempts=0,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    session.commit()
    if finalized:
        finalization = FinalizationRecordModel(
            id=f"fin_{suffix}",
            project_ref_id=project_id,
            review_item_id=item_id,
            version_id=version_id,
            version_no=1,
            original_file_id=file_id,
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
            finalized_at=NOW,
        )
        session.add(finalization)
        session.commit()
        item.active_finalization_id = finalization.id
        item.workflow_status = "finalized"
        session.commit()
    return project_id, item_id, version_id


def test_backfill_discovers_only_missing_current_thumbnail_rows() -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        eligible = _seed_current_version(session, "eligible")
        finalized = _seed_current_version(session, "finalized", finalized=True)
        _seed_current_version(session, "deleted", deleted=True)
        _seed_current_version(session, "asset", with_thumbnail_asset=True)
        _seed_current_version(session, "task", with_thumbnail_task=True)
        session.add(
            FileObjectModel(
                id="file_eligible_history",
                original_filename="history.mp4",
                mime_type="video/mp4",
                file_size=100,
                sha256="b" * 64,
                storage_path="/storage/files/file_eligible_history",
                owner_principal_id="owner",
                owner_principal_kind="system",
                duration_ms=10_000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                media_probe_version="test",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ReviewVersionModel(
                id="ver_eligible_history",
                project_ref_id=eligible[0],
                review_item_id=eligible[1],
                previous_version_id=None,
                version_no=2,
                version_label="V2",
                is_current=False,
                original_file_id="file_eligible_history",
                original_filename="history.mp4",
                mime_type="video/mp4",
                file_size=100,
                sha256="b" * 64,
                duration_ms=10_000,
                width=1920,
                height=1080,
                fps_num=25,
                fps_den=1,
                media_probe_version="test",
                thumbnail_asset_id=None,
                lock_version=1,
                created_at=NOW,
            )
        )
        session.commit()

        candidates = find_thumbnail_backfill_candidates(session)

    assert {(row.project_ref_id, row.review_item_id, row.version_id) for row in candidates} == {
        eligible,
        finalized,
    }


def test_backfill_apply_is_thumbnail_only_and_idempotent_after_revalidation() -> None:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        eligible = _seed_current_version(session, "one")
        candidates = find_thumbnail_backfill_candidates(session)
        assert [(row.project_ref_id, row.review_item_id, row.version_id) for row in candidates] == [eligible]

        assert enqueue_thumbnail_backfill_candidates(
            session,
            candidates,
            operation_id="thumbnail-backfill-test-1",
        ) == (1, 0)
        session.commit()
        tasks = list(session.scalars(select(MediaDerivativeTaskModel)))
        assert len(tasks) == 1
        assert tasks[0].kind == "thumbnail"
        assert tasks[0].status == "queued"
        assert tasks[0].result_details == {
            "backfill_operation_id": "thumbnail-backfill-test-1"
        }
        assert find_thumbnail_backfill_candidates(session) == []

        assert enqueue_thumbnail_backfill_candidates(
            session,
            candidates,
            operation_id="thumbnail-backfill-test-1",
        ) == (0, 1)
        session.commit()
        assert len(list(session.scalars(select(MediaDerivativeTaskModel)))) == 1
