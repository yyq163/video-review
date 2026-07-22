from __future__ import annotations

from .enums import ReviewWorkflowStatus
from .errors import ReviewError


def ensure_not_finalized(status: str) -> None:
    if status == ReviewWorkflowStatus.FINALIZED.value:
        raise ReviewError("REVIEW_ITEM_FINALIZED", "条目已定稿，拒绝写操作")


def ensure_can_upload_version(status: str, current_issue_count: int) -> None:
    ensure_not_finalized(status)
    if current_issue_count < 1:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "当前版本至少需要一条意见才能上传下一版本")


def ensure_can_create_issue(status: str) -> None:
    ensure_not_finalized(status)


def ensure_issue_writable(status: str) -> None:
    ensure_not_finalized(status)


def ensure_current_version(target_version_id: str, current_version_id: str) -> None:
    if target_version_id != current_version_id:
        raise ReviewError("VERSION_NOT_CURRENT", "目标版本不是当前版本")


def ensure_finalizable(status: str) -> None:
    ensure_not_finalized(status)
    if status not in {
        ReviewWorkflowStatus.PENDING_REVIEW.value,
        ReviewWorkflowStatus.IN_REVIEW.value,
        ReviewWorkflowStatus.CHANGES_REQUESTED.value,
    }:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "当前状态不可定稿")
