from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.domain.errors import ReviewError

from .sqlalchemy_models import ReviewIssueModel, ReviewItemModel, utcnow


def sync_item_workflow_for_current_issues(
    session: Session,
    item: ReviewItemModel,
) -> None:
    """Synchronize the authoritative item state while holding its row lock."""
    if item.workflow_status == "finalized":
        raise ReviewError("REVIEW_ITEM_FINALIZED", "已定稿条目不可修改意见状态")
    current_version_id = item.current_version_id
    if not current_version_id:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "审阅条目缺少当前版本")
    unresolved = int(
        session.scalar(
            select(func.count())
            .select_from(ReviewIssueModel)
            .where(
                ReviewIssueModel.review_item_id == item.id,
                ReviewIssueModel.version_id == current_version_id,
                ReviewIssueModel.status == "unresolved",
                ReviewIssueModel.deleted_at.is_(None),
            )
        )
        or 0
    )
    item.workflow_status = "changes_requested" if unresolved > 0 else "in_review"
    item.lock_version += 1
    item.updated_at = utcnow()
