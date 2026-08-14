from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass

from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra import database as database_module
from backend.app.modules.final_cut_review.infra.repositories import new_id
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    MediaDerivativeTaskModel,
    ProjectRefModel,
    ReviewItemModel,
    ReviewVersionModel,
    utcnow,
)
from backend.app.settings import Settings, get_database_settings


THUMBNAIL_BACKFILL_LOCK_NAMESPACE = "fj-final-cut-review:thumbnail-backfill:v1"
THUMBNAIL_BACKFILL_MAX_BATCH_SIZE = 100
THUMBNAIL_BACKFILL_MAX_LIMIT = 100_000
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ThumbnailBackfillCandidate:
    project_ref_id: str
    review_item_id: str
    version_id: str


@dataclass(frozen=True, slots=True)
class ThumbnailBackfillResult:
    mode: str
    operation_id: str | None
    eligible: int
    enqueued: int
    skipped_after_lock: int


def _candidate_statement(project_ref_id: str | None = None):
    thumbnail_task_exists = exists(
        select(MediaDerivativeTaskModel.id).where(
            MediaDerivativeTaskModel.version_id == ReviewVersionModel.id,
            MediaDerivativeTaskModel.kind == "thumbnail",
        )
    )
    statement = (
        select(
            ProjectRefModel.id,
            ReviewItemModel.id,
            ReviewVersionModel.id,
        )
        .join(
            ReviewItemModel,
            ReviewItemModel.project_ref_id == ProjectRefModel.id,
        )
        .join(
            ReviewVersionModel,
            (ReviewVersionModel.project_ref_id == ProjectRefModel.id)
            & (ReviewVersionModel.review_item_id == ReviewItemModel.id),
        )
        .where(
            ProjectRefModel.deleted_at.is_(None),
            ReviewItemModel.current_version_id == ReviewVersionModel.id,
            ReviewItemModel.current_version_is_current.is_(True),
            ReviewVersionModel.is_current.is_(True),
            ReviewVersionModel.thumbnail_asset_id.is_(None),
            ~thumbnail_task_exists,
        )
        .order_by(
            ProjectRefModel.id,
            ReviewItemModel.id,
            ReviewVersionModel.id,
        )
    )
    if project_ref_id is not None:
        statement = statement.where(ProjectRefModel.id == project_ref_id)
    return statement


def find_thumbnail_backfill_candidates(
    session: Session,
    *,
    project_ref_id: str | None = None,
    limit: int | None = None,
) -> list[ThumbnailBackfillCandidate]:
    statement = _candidate_statement(project_ref_id)
    if limit is not None:
        statement = statement.limit(limit)
    return [
        ThumbnailBackfillCandidate(
            project_ref_id=row[0],
            review_item_id=row[1],
            version_id=row[2],
        )
        for row in session.execute(statement)
    ]


def _lock_backfill_transaction(session: Session) -> None:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:namespace, 0))"),
            {"namespace": THUMBNAIL_BACKFILL_LOCK_NAMESPACE},
        )


def enqueue_thumbnail_backfill_candidates(
    session: Session,
    candidates: list[ThumbnailBackfillCandidate],
    *,
    operation_id: str,
) -> tuple[int, int]:
    if SAFE_IDENTIFIER.fullmatch(operation_id) is None:
        raise ValueError("operation id is invalid")
    _lock_backfill_transaction(session)
    enqueued = 0
    skipped = 0
    for candidate in candidates:
        project = session.scalar(
            select(ProjectRefModel)
            .where(ProjectRefModel.id == candidate.project_ref_id)
            .with_for_update()
        )
        item = session.scalar(
            select(ReviewItemModel)
            .where(
                ReviewItemModel.id == candidate.review_item_id,
                ReviewItemModel.project_ref_id == candidate.project_ref_id,
            )
            .with_for_update()
        )
        version = session.scalar(
            select(ReviewVersionModel)
            .where(
                ReviewVersionModel.id == candidate.version_id,
                ReviewVersionModel.project_ref_id == candidate.project_ref_id,
                ReviewVersionModel.review_item_id == candidate.review_item_id,
            )
            .with_for_update()
        )
        existing_task = session.scalar(
            select(MediaDerivativeTaskModel.id).where(
                MediaDerivativeTaskModel.version_id == candidate.version_id,
                MediaDerivativeTaskModel.kind == "thumbnail",
            )
        )
        if (
            project is None
            or project.deleted_at is not None
            or item is None
            or version is None
            or item.current_version_id != version.id
            or not item.current_version_is_current
            or not version.is_current
            or version.thumbnail_asset_id is not None
            or existing_task is not None
        ):
            skipped += 1
            continue
        now = utcnow()
        session.add(
            MediaDerivativeTaskModel(
                id=new_id("mdt"),
                project_ref_id=project.id,
                review_item_id=item.id,
                version_id=version.id,
                kind="thumbnail",
                status="queued",
                attempts=0,
                result_details={"backfill_operation_id": operation_id},
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        enqueued += 1
    return enqueued, skipped


def run_thumbnail_backfill(
    settings: Settings,
    *,
    apply: bool,
    project_ref_id: str | None = None,
    operation_id: str | None = None,
    limit: int | None = None,
    batch_size: int = 50,
) -> ThumbnailBackfillResult:
    if not 1 <= batch_size <= THUMBNAIL_BACKFILL_MAX_BATCH_SIZE:
        raise ValueError("batch size is outside the supported range")
    if limit is not None and not 1 <= limit <= THUMBNAIL_BACKFILL_MAX_LIMIT:
        raise ValueError("limit is outside the supported range")
    if project_ref_id is not None and SAFE_IDENTIFIER.fullmatch(project_ref_id) is None:
        raise ValueError("project ref id is invalid")
    if operation_id is not None and SAFE_IDENTIFIER.fullmatch(operation_id) is None:
        raise ValueError("operation id is invalid")
    if apply and operation_id is None:
        raise ValueError("operation id is required in apply mode")

    with database_module.runtime_participant_lease(database_module.engine, settings):
        if not apply:
            with database_module.SessionLocal() as session:
                database_module.require_runtime_participant_session(session)
                eligible = len(
                    find_thumbnail_backfill_candidates(
                        session,
                        project_ref_id=project_ref_id,
                        limit=limit,
                    )
                )
                session.rollback()
            return ThumbnailBackfillResult(
                mode="dry-run",
                operation_id=None,
                eligible=eligible,
                enqueued=0,
                skipped_after_lock=0,
            )

        total_enqueued = 0
        total_skipped = 0
        remaining = limit
        while remaining is None or remaining > 0:
            current_batch_size = min(batch_size, remaining) if remaining is not None else batch_size
            with database_module.SessionLocal() as session:
                database_module.require_runtime_participant_session(session)
                candidates = find_thumbnail_backfill_candidates(
                    session,
                    project_ref_id=project_ref_id,
                    limit=current_batch_size,
                )
                if not candidates:
                    session.rollback()
                    break
                assert operation_id is not None
                enqueued, skipped = enqueue_thumbnail_backfill_candidates(
                    session,
                    candidates,
                    operation_id=operation_id,
                )
                session.commit()
            total_enqueued += enqueued
            total_skipped += skipped
            if remaining is not None:
                remaining -= len(candidates)
        return ThumbnailBackfillResult(
            mode="apply",
            operation_id=operation_id,
            eligible=total_enqueued + total_skipped,
            enqueued=total_enqueued,
            skipped_after_lock=total_skipped,
        )
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Queue missing current-version thumbnails without converting legacy playback media."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report eligible rows without writing (default).")
    mode.add_argument("--apply", action="store_true", help="Queue thumbnail tasks in bounded transactions.")
    parser.add_argument("--project-ref-id")
    parser.add_argument(
        "--operation-id",
        help="Required for --apply; stable identifier used for exact audit and rollback selection.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=50)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_thumbnail_backfill(
        get_database_settings(),
        apply=bool(args.apply),
        project_ref_id=args.project_ref_id,
        operation_id=args.operation_id,
        limit=args.limit,
        batch_size=args.batch_size,
    )
    print(
        json.dumps(
            {
                "mode": result.mode,
                "operation_id": result.operation_id,
                "eligible": result.eligible,
                "enqueued": result.enqueued,
                "skipped_after_lock": result.skipped_after_lock,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
