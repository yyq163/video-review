from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    MediaDerivativeTaskModel,
    ReviewItemModel,
    ReviewVersionModel,
    utcnow,
)


THUMBNAIL_FRAME_ZERO_MS = 0
THUMBNAIL_FALLBACK_FRAME_MS = 3_000
THUMBNAIL_SIGNATURE_HEX_LENGTH = 16
THUMBNAIL_SIGNATURE_BITS = THUMBNAIL_SIGNATURE_HEX_LENGTH * 4
THUMBNAIL_STRICT_SIMILARITY_PERCENT = 85


def thumbnail_signatures_are_similar(left: str, right: str) -> bool:
    """Return whether two 64-bit dHash signatures are strictly over 85% alike."""
    if not (_valid_signature(left) and _valid_signature(right)):
        return False
    distance = (int(left, 16) ^ int(right, 16)).bit_count()
    matching_bits = THUMBNAIL_SIGNATURE_BITS - distance
    return matching_bits * 100 > THUMBNAIL_STRICT_SIMILARITY_PERCENT * THUMBNAIL_SIGNATURE_BITS


def thumbnail_variant_file_ids(details: Mapping[str, Any] | None) -> set[str]:
    variants = details.get("variants") if isinstance(details, Mapping) else None
    if not isinstance(variants, Mapping):
        return set()
    return {
        value
        for key, value in variants.items()
        if key in {str(THUMBNAIL_FRAME_ZERO_MS), str(THUMBNAIL_FALLBACK_FRAME_MS)}
        and isinstance(value, str)
        and value
    }


def normalize_thumbnail_result_details(
    details: Mapping[str, Any] | None,
    *,
    existing_output_file_id: str | None = None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    signature = details.get("frame_signature") if isinstance(details, Mapping) else None
    if isinstance(signature, str) and _valid_signature(signature):
        normalized["frame_signature"] = signature.lower()
    variants: dict[str, str] = {}
    source_variants = details.get("variants") if isinstance(details, Mapping) else None
    if isinstance(source_variants, Mapping):
        for frame_ms in (THUMBNAIL_FRAME_ZERO_MS, THUMBNAIL_FALLBACK_FRAME_MS):
            value = source_variants.get(str(frame_ms))
            if isinstance(value, str) and value:
                variants[str(frame_ms)] = value
    if not variants and existing_output_file_id:
        # Tasks created before the similarity feature produced only the true
        # first frame, so their authoritative output is the frame-zero variant.
        variants[str(THUMBNAIL_FRAME_ZERO_MS)] = existing_output_file_id
    normalized["variants"] = variants
    desired = details.get("desired_frame_ms") if isinstance(details, Mapping) else None
    normalized["desired_frame_ms"] = (
        desired
        if desired in {THUMBNAIL_FRAME_ZERO_MS, THUMBNAIL_FALLBACK_FRAME_MS}
        else THUMBNAIL_FRAME_ZERO_MS
    )
    return normalized


def reconcile_project_thumbnail_groups(
    session: Session,
    project_ref_id: str,
    *,
    discover_legacy_signatures: bool,
    changed_task_id: str | None = None,
) -> None:
    """Reconcile only current versions inside one already project-locked transaction."""
    rows = list(
        session.execute(
            select(ReviewVersionModel, MediaDerivativeTaskModel)
            .join(
                ReviewItemModel,
                (ReviewItemModel.id == ReviewVersionModel.review_item_id)
                & (ReviewItemModel.project_ref_id == ReviewVersionModel.project_ref_id),
            )
            .join(
                MediaDerivativeTaskModel,
                (MediaDerivativeTaskModel.version_id == ReviewVersionModel.id)
                & (MediaDerivativeTaskModel.project_ref_id == ReviewVersionModel.project_ref_id)
                & (MediaDerivativeTaskModel.review_item_id == ReviewVersionModel.review_item_id)
                & (MediaDerivativeTaskModel.kind == "thumbnail"),
            )
            .where(
                ReviewVersionModel.project_ref_id == project_ref_id,
                ReviewVersionModel.is_current.is_(True),
                ReviewItemModel.current_version_id == ReviewVersionModel.id,
            )
            .order_by(ReviewVersionModel.id)
            .with_for_update()
        )
    )
    details_by_task: dict[str, dict[str, Any]] = {}
    signatures: dict[str, str] = {}
    versions_by_task: dict[str, ReviewVersionModel] = {}
    tasks_by_id: dict[str, MediaDerivativeTaskModel] = {}
    for version, task in rows:
        details = normalize_thumbnail_result_details(
            task.result_details,
            existing_output_file_id=(
                task.output_file_id
                if task.output_file_id and task.output_file_id == version.thumbnail_asset_id
                else None
            ),
        )
        details_by_task[task.id] = details
        versions_by_task[task.id] = version
        tasks_by_id[task.id] = task
        signature = details.get("frame_signature")
        if isinstance(signature, str):
            signatures[task.id] = signature

    desired_by_task: dict[str, int] = {}
    if changed_task_id is not None and changed_task_id in signatures:
        # Publishing one new frame-zero signature can only add edges to the
        # already-reconciled graph. Compare that signature with the existing
        # current versions in O(N); existing transitive components already
        # have desired_frame_ms=3000 and do not need pairwise reconstruction.
        changed_signature = signatures[changed_task_id]
        matched_task_ids = {
            task_id
            for task_id, signature in signatures.items()
            if task_id != changed_task_id
            and thumbnail_signatures_are_similar(changed_signature, signature)
        }
        for task_id, details in details_by_task.items():
            desired_by_task[task_id] = details["desired_frame_ms"]
        desired_by_task[changed_task_id] = (
            THUMBNAIL_FALLBACK_FRAME_MS
            if matched_task_ids
            else THUMBNAIL_FRAME_ZERO_MS
        )
        for task_id in matched_task_ids:
            desired_by_task[task_id] = THUMBNAIL_FALLBACK_FRAME_MS
    else:
        parents = {task_id: task_id for task_id in signatures}

        def find(task_id: str) -> str:
            while parents[task_id] != task_id:
                parents[task_id] = parents[parents[task_id]]
                task_id = parents[task_id]
            return task_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        signature_items = sorted(signatures.items())
        for index, (left_id, left_signature) in enumerate(signature_items):
            for right_id, right_signature in signature_items[index + 1 :]:
                if thumbnail_signatures_are_similar(left_signature, right_signature):
                    union(left_id, right_id)

        component_sizes: dict[str, int] = {}
        for task_id in signatures:
            root = find(task_id)
            component_sizes[root] = component_sizes.get(root, 0) + 1
        desired_by_task = {
            task_id: (
                THUMBNAIL_FALLBACK_FRAME_MS
                if component_sizes[find(task_id)] > 1
                else THUMBNAIL_FRAME_ZERO_MS
            )
            for task_id in signatures
        }

    now = utcnow()
    version_updates: list[tuple[ReviewVersionModel, str]] = []
    for task_id, task in tasks_by_id.items():
        details = details_by_task[task_id]
        prior_desired_frame_ms = details["desired_frame_ms"]
        signature = signatures.get(task_id)
        if signature is None:
            if (
                discover_legacy_signatures
                and task.status == "ready"
                and str(THUMBNAIL_FRAME_ZERO_MS) in details["variants"]
            ):
                details["desired_frame_ms"] = THUMBNAIL_FRAME_ZERO_MS
                task.status = "queued"
                task.attempts = 0
                task.next_attempt_at = None
                task.lease_id = None
                task.lease_expires_at = None
                task.error_code = None
                task.failure_details = None
                task.updated_at = now
            if task.result_details != details:
                task.result_details = details
            continue

        desired_frame_ms = desired_by_task[task_id]
        desired_frame_changed = desired_frame_ms != prior_desired_frame_ms
        details["desired_frame_ms"] = desired_frame_ms
        if task.result_details != details:
            task.result_details = details
        desired_file_id = details["variants"].get(str(desired_frame_ms))
        can_activate_cached_variant = task.status == "ready" or desired_frame_changed
        if (
            desired_file_id
            and task.status != "running"
            and can_activate_cached_variant
        ):
            if (
                task.status != "ready"
                or task.output_file_id != desired_file_id
                or task.next_attempt_at is not None
                or task.lease_id is not None
                or task.lease_expires_at is not None
                or task.error_code is not None
                or task.failure_details is not None
            ):
                task.status = "ready"
                task.output_file_id = desired_file_id
                task.next_attempt_at = None
                task.lease_id = None
                task.lease_expires_at = None
                task.error_code = None
                task.failure_details = None
                task.updated_at = now
            version = versions_by_task[task_id]
            if version.thumbnail_asset_id != desired_file_id:
                version_updates.append((version, desired_file_id))
        elif not desired_file_id and desired_frame_changed and task.status != "running":
            task.status = "queued"
            task.attempts = 0
            task.next_attempt_at = None
            task.lease_id = None
            task.lease_expires_at = None
            task.error_code = None
            task.failure_details = None
            task.updated_at = now
    # Finalized-version triggers authorize only pointers backed by an already
    # ready task. Flush task authority first, then publish the selected pointer.
    session.flush()
    for version, desired_file_id in version_updates:
        version.thumbnail_asset_id = desired_file_id


def _valid_signature(value: str) -> bool:
    if len(value) != THUMBNAIL_SIGNATURE_HEX_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
