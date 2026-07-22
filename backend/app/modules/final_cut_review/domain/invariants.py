from __future__ import annotations

from .enums import ReviewWorkflowStatus
from .errors import ReviewError


def ensure_not_finalized(status: str) -> None:
    if status == ReviewWorkflowStatus.FINALIZED.value:
        raise ReviewError("REVIEW_ITEM_FINALIZED", "条目已定稿，拒绝写操作")


def ensure_can_upload_version(status: str, supersede_reason: str | None = None) -> None:
    ensure_not_finalized(status)
    if status == ReviewWorkflowStatus.IN_REVIEW.value:
        raise ReviewError("REVIEW_IN_PROGRESS", "审阅中禁止上传新版本")
    if status == ReviewWorkflowStatus.PENDING_REVIEW.value and not supersede_reason:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "待审阅状态追加版本必须填写 supersede_reason")


def ensure_can_create_issue(status: str) -> None:
    ensure_not_finalized(status)
    if status == ReviewWorkflowStatus.CHANGES_REQUESTED.value:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "已要求修改，当前版本不可继续新增意见")


def ensure_issue_writable(status: str) -> None:
    ensure_not_finalized(status)
    if status == ReviewWorkflowStatus.CHANGES_REQUESTED.value:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "当前版本意见已只读")


def ensure_current_version(target_version_id: str, current_version_id: str) -> None:
    if target_version_id != current_version_id:
        raise ReviewError("VERSION_NOT_CURRENT", "目标版本不是当前版本")


def ensure_finalizable(status: str, unresolved_current: int) -> None:
    ensure_not_finalized(status)
    if status not in {ReviewWorkflowStatus.PENDING_REVIEW.value, ReviewWorkflowStatus.IN_REVIEW.value}:
        raise ReviewError("RESOURCE_STATE_CONFLICT", "当前状态不可定稿")
    if unresolved_current > 0:
        raise ReviewError("UNRESOLVED_ISSUES_EXIST", "当前版本仍有未解决意见")
