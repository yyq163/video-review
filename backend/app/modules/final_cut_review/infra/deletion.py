from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.application.context import ExecutionContext
from backend.app.modules.final_cut_review.domain.errors import ReviewError, not_found
from backend.app.modules.review_contracts.generated import (
    ProjectDeletionResultDTO,
    ReviewItemDeletionResultDTO,
    ReviewItemsBatchDeletionResultDTO,
)
from backend.app.safe_files import UnsafeFilePathError, contained_path, pin_regular_file
from backend.app.settings import Settings
from backend.app.storage_mutation_locks import (
    acquire_storage_target_mutation_locks,
)
from backend.app.thumbnail_similarity import (
    reconcile_project_thumbnail_groups,
    thumbnail_variant_file_ids,
)

from .sqlalchemy_models import (
    FileObjectModel,
    FinalCutPackageSnapshotModel,
    FinalizationPackageInvalidationModel,
    FinalizationRecordModel,
    MediaDerivativeTaskModel,
    OutboxConsumerReceiptModel,
    OutboxEventModel,
    ProjectRefModel,
    ReviewDecisionModel,
    ReviewDeletionArtifactModel,
    ReviewDeletionIntentModel,
    ReviewIssueModel,
    ReviewItemModel,
    ReviewThreadMessageModel,
    ReviewVersionModel,
    UploadSessionModel,
    utcnow,
)


MANAGED_FILE_ID_RE = re.compile(r"^(?:file|media)_[0-9a-f]{32}$")
PACKAGE_ID_RE = re.compile(r"^pkg_[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _stable_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class _DeletionScope:
    item_ids: tuple[str, ...]
    version_ids: tuple[str, ...]
    issue_ids: tuple[str, ...]
    finalization_ids: tuple[str, ...]
    derivative_task_ids: tuple[str, ...]


class DurableDeletionCoordinator:
    """Persist deletion intent and remove one bounded business graph transactionally."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        execution_identity: str,
        enqueue_post_commit: Callable[[str], None],
    ) -> None:
        self.session = session
        self.settings = settings
        self.execution_identity = execution_identity
        self.enqueue_post_commit = enqueue_post_commit

    def delete_project(
        self,
        payload: dict[str, Any],
        context: ExecutionContext,
        expected_version: int | None,
    ) -> dict[str, Any]:
        del context
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "删除项目必须二次确认")
        project = self._project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        self._expect_lock(project, expected_version)
        items = list(
            self.session.scalars(
                select(ReviewItemModel)
                .where(ReviewItemModel.project_ref_id == project.id)
                .order_by(ReviewItemModel.id)
                .with_for_update(),
            ),
        )
        if any(
            item.workflow_status == "finalized"
            or item.active_finalization_id is not None
            for item in items
        ):
            raise ReviewError(
                "RESOURCE_STATE_CONFLICT",
                "项目仍有已定稿分集，必须先显式撤销定稿",
            )
        scope = self._scope(project.id, tuple(item.id for item in items))
        self._assert_no_active_finalization(scope)
        packages = self._packages(project.id)
        self._assert_cleanup_not_leased(scope, packages)
        file_candidates = self._acquire_scope_storage_mutation_locks(
            scope,
            packages,
        )
        intent = self._create_intent(
            command_type="DeleteProject",
            subject_type="project",
            project_ref_id=project.id,
            review_item_id=None,
        )
        file_ids = self._capture_scope_files(
            intent,
            scope,
            candidates=file_candidates,
        )
        package_ids = self._capture_packages(intent, packages)
        self._delete_scope(
            project=project,
            scope=scope,
            package_ids=package_ids,
            file_ids=file_ids,
            delete_project=True,
        )
        self._finish_database_deletion(intent)
        return ProjectDeletionResultDTO(
            project_ref_id=project.id,
            outcome="deleted",
            error_code=None,
        ).model_dump(mode="json")

    def delete_review_item(
        self,
        payload: dict[str, Any],
        context: ExecutionContext,
        expected_version: int | None,
        *,
        command_type: str = "DeleteReviewItem",
        locked_project: ProjectRefModel | None = None,
    ) -> dict[str, Any]:
        del context
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "删除分集必须二次确认")
        project = locked_project or self._project(
            payload["project_ref_id"],
            for_update=True,
        )
        if project.id != payload["project_ref_id"]:
            raise not_found()
        self._assert_project_active(project)
        item = self.session.scalar(
            select(ReviewItemModel)
            .where(
                ReviewItemModel.project_ref_id == project.id,
                ReviewItemModel.id == payload["review_item_id"],
            )
            .with_for_update(),
        )
        if item is None:
            raise not_found()
        self._expect_lock(item, expected_version)
        scope = self._scope(project.id, (item.id,))
        issue_count = len(scope.issue_ids)
        finalizations = list(
            self.session.scalars(
                select(FinalizationRecordModel)
                .where(FinalizationRecordModel.id.in_(scope.finalization_ids))
                .order_by(FinalizationRecordModel.id)
                .with_for_update(),
            ),
        ) if scope.finalization_ids else []
        self._assert_item_delete_eligible(
            item,
            version_count=len(scope.version_ids),
            issue_count=issue_count,
            finalizations=finalizations,
        )
        packages = self._packages_for_finalizations(
            project.id,
            set(scope.finalization_ids),
        )
        self._assert_cleanup_not_leased(scope, packages)
        deletable_packages = [
            package
            for package in packages
            if self._package_is_exclusive_to_finalizations(
                package,
                set(scope.finalization_ids),
            )
        ]
        file_candidates = self._acquire_scope_storage_mutation_locks(
            scope,
            deletable_packages,
        )
        intent = self._create_intent(
            command_type=command_type,
            subject_type="review_item",
            project_ref_id=project.id,
            review_item_id=item.id,
        )
        file_ids = self._capture_scope_files(
            intent,
            scope,
            candidates=file_candidates,
        )
        package_ids = self._capture_packages(intent, deletable_packages)
        self._delete_scope(
            project=project,
            scope=scope,
            package_ids=package_ids,
            file_ids=file_ids,
            delete_project=False,
        )
        reconcile_project_thumbnail_groups(
            self.session,
            project.id,
            discover_legacy_signatures=False,
        )
        self.session.flush()
        self._finish_database_deletion(intent)
        return ReviewItemDeletionResultDTO(
            project_ref_id=project.id,
            review_item_id=item.id,
            outcome="deleted",
            error_code=None,
        ).model_dump(mode="json")

    def delete_review_items_batch(
        self,
        payload: dict[str, Any],
        context: ExecutionContext,
        expected_version: int | None,
    ) -> dict[str, Any]:
        del expected_version
        if payload.get("confirmed") is not True:
            raise ReviewError("VALIDATION_ERROR", "批量删除分集必须二次确认")
        project = self._project(payload["project_ref_id"], for_update=True)
        self._assert_project_active(project)
        batch_scope = self._scope(
            project.id,
            tuple(payload["review_item_ids"]),
        )
        batch_packages = self._packages(project.id)
        self._acquire_scope_storage_mutation_locks(
            batch_scope,
            batch_packages,
        )
        results: list[dict[str, Any]] = []
        for review_item_id in payload["review_item_ids"]:
            savepoint = self.session.begin_nested()
            try:
                result = self.delete_review_item(
                    {
                        "project_ref_id": project.id,
                        "review_item_id": review_item_id,
                        "confirmed": True,
                    },
                    context,
                    None,
                    command_type="DeleteReviewItemsBatch",
                    locked_project=project,
                )
                savepoint.commit()
                results.append(result)
            except ReviewError as error:
                savepoint.rollback()
                results.append(
                    ReviewItemDeletionResultDTO(
                        project_ref_id=project.id,
                        review_item_id=review_item_id,
                        outcome="failed",
                        error_code=error.code,
                    ).model_dump(mode="json"),
                )
            except IntegrityError:
                savepoint.rollback()
                results.append(
                    ReviewItemDeletionResultDTO(
                        project_ref_id=project.id,
                        review_item_id=review_item_id,
                        outcome="failed",
                        error_code="RESOURCE_STATE_CONFLICT",
                    ).model_dump(mode="json"),
                )
        return ReviewItemsBatchDeletionResultDTO(
            project_ref_id=project.id,
            results=results,
        ).model_dump(mode="json")

    def _project(
        self,
        project_ref_id: str,
        *,
        for_update: bool,
    ) -> ProjectRefModel:
        statement = select(ProjectRefModel).where(
            ProjectRefModel.id == project_ref_id,
        )
        if for_update:
            statement = statement.with_for_update()
        project = self.session.scalar(statement)
        if project is None:
            raise not_found()
        return project

    @staticmethod
    def _assert_project_active(project: ProjectRefModel) -> None:
        if project.deleted_at is not None:
            raise ReviewError("RESOURCE_STATE_CONFLICT", "项目已删除")
        if project.lifecycle_status != "active":
            raise ReviewError("RESOURCE_STATE_CONFLICT", "归档项目只读，请先恢复项目")

    @staticmethod
    def _expect_lock(model: Any, expected_version: int | None) -> None:
        if expected_version is not None and model.lock_version != expected_version:
            raise ReviewError("OPTIMISTIC_LOCK_CONFLICT", "乐观锁冲突")

    def _scope(
        self,
        project_ref_id: str,
        item_ids: tuple[str, ...],
    ) -> _DeletionScope:
        if not item_ids:
            return _DeletionScope((), (), (), (), ())
        versions = list(
            self.session.scalars(
                select(ReviewVersionModel)
                .where(
                    ReviewVersionModel.project_ref_id == project_ref_id,
                    ReviewVersionModel.review_item_id.in_(item_ids),
                )
                .order_by(ReviewVersionModel.review_item_id, ReviewVersionModel.id)
                .with_for_update(),
            ),
        )
        issues = list(
            self.session.scalars(
                select(ReviewIssueModel)
                .where(
                    ReviewIssueModel.project_ref_id == project_ref_id,
                    ReviewIssueModel.review_item_id.in_(item_ids),
                )
                .order_by(ReviewIssueModel.review_item_id, ReviewIssueModel.id)
                .with_for_update(),
            ),
        )
        finalizations = list(
            self.session.scalars(
                select(FinalizationRecordModel)
                .where(
                    FinalizationRecordModel.project_ref_id == project_ref_id,
                    FinalizationRecordModel.review_item_id.in_(item_ids),
                )
                .order_by(
                    FinalizationRecordModel.review_item_id,
                    FinalizationRecordModel.id,
                )
                .with_for_update(),
            ),
        )
        tasks = list(
            self.session.scalars(
                select(MediaDerivativeTaskModel)
                .where(
                    MediaDerivativeTaskModel.project_ref_id == project_ref_id,
                    MediaDerivativeTaskModel.review_item_id.in_(item_ids),
                )
                .order_by(
                    MediaDerivativeTaskModel.review_item_id,
                    MediaDerivativeTaskModel.id,
                )
                .with_for_update(),
            ),
        )
        return _DeletionScope(
            item_ids=item_ids,
            version_ids=tuple(version.id for version in versions),
            issue_ids=tuple(issue.id for issue in issues),
            finalization_ids=tuple(finalization.id for finalization in finalizations),
            derivative_task_ids=tuple(task.id for task in tasks),
        )

    def _assert_no_active_finalization(self, scope: _DeletionScope) -> None:
        if not scope.finalization_ids:
            return
        active = self.session.scalar(
            select(func.count())
            .select_from(FinalizationRecordModel)
            .where(
                FinalizationRecordModel.id.in_(scope.finalization_ids),
                FinalizationRecordModel.status == "active",
            ),
        ) or 0
        if active:
            raise ReviewError(
                "RESOURCE_STATE_CONFLICT",
                "删除范围仍有 active finalization，必须先显式撤销定稿",
            )

    @staticmethod
    def _assert_item_delete_eligible(
        item: ReviewItemModel,
        *,
        version_count: int,
        issue_count: int,
        finalizations: list[FinalizationRecordModel],
    ) -> None:
        if item.active_finalization_id is not None or any(
            finalization.status == "active" for finalization in finalizations
        ):
            raise ReviewError(
                "RESOURCE_STATE_CONFLICT",
                "分集仍处于定稿状态，必须先显式撤销定稿",
            )
        explicitly_revoked = bool(finalizations) and all(
            finalization.status == "revoked" for finalization in finalizations
        )
        workflow_eligible = item.workflow_status == "pending_review" or (
            item.workflow_status == "in_review" and explicitly_revoked
        )
        if (
            not workflow_eligible
            or version_count != 1
            or issue_count > 0
            or (finalizations and not explicitly_revoked)
        ):
            raise ReviewError("RESOURCE_STATE_CONFLICT", "审核已开始，不能删除分集")

    def _packages(self, project_ref_id: str) -> list[FinalCutPackageSnapshotModel]:
        return list(
            self.session.scalars(
                select(FinalCutPackageSnapshotModel)
                .where(FinalCutPackageSnapshotModel.project_ref_id == project_ref_id)
                .order_by(FinalCutPackageSnapshotModel.id)
                .with_for_update(),
            ),
        )

    def _packages_for_finalizations(
        self,
        project_ref_id: str,
        finalization_ids: set[str],
    ) -> list[FinalCutPackageSnapshotModel]:
        if not finalization_ids:
            return []
        package_ids = set(
            self.session.scalars(
                select(FinalizationPackageInvalidationModel.package_id).where(
                    FinalizationPackageInvalidationModel.finalization_id.in_(
                        finalization_ids,
                    ),
                ),
            ),
        )
        packages = self._packages(project_ref_id)
        return [
            package
            for package in packages
            if package.id in package_ids
            or any(
                isinstance(item, dict)
                and item.get("finalization_id") in finalization_ids
                for item in package.items
            )
        ]

    @staticmethod
    def _package_is_exclusive_to_finalizations(
        package: FinalCutPackageSnapshotModel,
        finalization_ids: set[str],
    ) -> bool:
        package_finalizations = {
            value
            for item in package.items
            if isinstance(item, dict)
            and isinstance((value := item.get("finalization_id")), str)
        }
        return bool(package_finalizations) and package_finalizations <= finalization_ids

    def _assert_cleanup_not_leased(
        self,
        scope: _DeletionScope,
        packages: list[FinalCutPackageSnapshotModel],
    ) -> None:
        now = utcnow()
        if scope.derivative_task_ids:
            running = self.session.scalar(
                select(func.count())
                .select_from(MediaDerivativeTaskModel)
                .where(
                    MediaDerivativeTaskModel.id.in_(scope.derivative_task_ids),
                    MediaDerivativeTaskModel.status == "running",
                    MediaDerivativeTaskModel.lease_expires_at > now,
                ),
            ) or 0
            if running:
                raise ReviewError(
                    "RESOURCE_STATE_CONFLICT",
                    "媒体衍生任务仍被 worker 锁定，暂时不能删除",
                )
        for package in packages:
            if package.status == "preparing":
                raise ReviewError(
                    "RESOURCE_STATE_CONFLICT",
                    "项目包仍在生成，暂时不能删除",
                )
            if (
                package.build_lease_id is not None
                and package.build_lease_expires_at is not None
                and _aware(package.build_lease_expires_at) > now
            ) or (
                package.download_lease_id is not None
                and package.download_lease_expires_at is not None
                and _aware(package.download_lease_expires_at) > now
            ):
                raise ReviewError(
                    "RESOURCE_STATE_CONFLICT",
                    "项目包仍被下载或清理租约锁定，暂时不能删除",
                )

    def _create_intent(
        self,
        *,
        command_type: str,
        subject_type: str,
        project_ref_id: str,
        review_item_id: str | None,
    ) -> ReviewDeletionIntentModel:
        operation_identity_hash = _stable_hash(
            {
                "execution_identity": self.execution_identity,
                "command_type": command_type,
                "subject_type": subject_type,
                "project_ref_id": project_ref_id,
                "review_item_id": review_item_id,
            },
        )
        intent = ReviewDeletionIntentModel(
            id=f"delete_{hashlib.sha256(operation_identity_hash.encode()).hexdigest()[:32]}",
            command_type=command_type,
            operation_identity_hash=operation_identity_hash,
            subject_type=subject_type,
            project_ref_id_snapshot=project_ref_id,
            review_item_id_snapshot=review_item_id,
            status="pending",
            attempts=0,
        )
        self.session.add(intent)
        self.session.flush()
        return intent

    def _scope_file_candidates(
        self,
        scope: _DeletionScope,
    ) -> dict[str, str]:
        candidates: dict[str, str] = {}
        if scope.version_ids:
            versions = list(
                self.session.scalars(
                    select(ReviewVersionModel).where(
                        ReviewVersionModel.id.in_(scope.version_ids),
                    ),
                ),
            )
            for version in versions:
                candidates[version.original_file_id] = "original_media"
                if version.playback_asset_id:
                    candidates.setdefault(
                        version.playback_asset_id,
                        "playback_derivative",
                    )
                if version.thumbnail_asset_id:
                    candidates.setdefault(version.thumbnail_asset_id, "thumbnail")
        if scope.derivative_task_ids:
            tasks = list(
                self.session.scalars(
                    select(MediaDerivativeTaskModel).where(
                        MediaDerivativeTaskModel.id.in_(scope.derivative_task_ids),
                    ),
                ),
            )
            for task in tasks:
                kind = "thumbnail" if task.kind == "thumbnail" else "playback_derivative"
                if task.output_file_id:
                    candidates.setdefault(task.output_file_id, kind)
                if task.kind == "thumbnail":
                    for file_id in thumbnail_variant_file_ids(task.result_details):
                        candidates.setdefault(file_id, "thumbnail")
        return candidates

    def _acquire_scope_storage_mutation_locks(
        self,
        scope: _DeletionScope,
        packages: list[FinalCutPackageSnapshotModel],
    ) -> dict[str, str]:
        candidates = self._scope_file_candidates(scope)
        acquire_storage_target_mutation_locks(
            self.session,
            self.settings,
            {
                *(f"files/{file_id}" for file_id in candidates),
                *(f"packages/{package.id}.zip" for package in packages),
            },
        )
        return candidates

    def _capture_scope_files(
        self,
        intent: ReviewDeletionIntentModel,
        scope: _DeletionScope,
        *,
        candidates: dict[str, str],
    ) -> set[str]:
        deletable: set[str] = set()
        for file_id, artifact_kind in candidates.items():
            if self._file_has_reference_outside_scope(file_id, scope):
                continue
            file_object = self.session.get(FileObjectModel, file_id)
            if file_object is None:
                continue
            self._capture_file_artifact(intent, file_object, artifact_kind)
            deletable.add(file_id)
        return deletable

    def _file_has_reference_outside_scope(
        self,
        file_id: str,
        scope: _DeletionScope,
    ) -> bool:
        version_filter = or_(
            ReviewVersionModel.original_file_id == file_id,
            ReviewVersionModel.playback_asset_id == file_id,
            ReviewVersionModel.thumbnail_asset_id == file_id,
        )
        version_statement = select(func.count()).select_from(ReviewVersionModel).where(
            version_filter,
        )
        if scope.version_ids:
            version_statement = version_statement.where(
                ReviewVersionModel.id.not_in(scope.version_ids),
            )
        if (self.session.scalar(version_statement) or 0) > 0:
            return True
        finalization_statement = select(func.count()).select_from(
            FinalizationRecordModel,
        ).where(FinalizationRecordModel.original_file_id == file_id)
        if scope.finalization_ids:
            finalization_statement = finalization_statement.where(
                FinalizationRecordModel.id.not_in(scope.finalization_ids),
            )
        if (self.session.scalar(finalization_statement) or 0) > 0:
            return True
        task_statement = select(MediaDerivativeTaskModel).where(
            MediaDerivativeTaskModel.output_file_id == file_id,
        )
        if scope.derivative_task_ids:
            task_statement = task_statement.where(
                MediaDerivativeTaskModel.id.not_in(scope.derivative_task_ids),
            )
        if self.session.scalar(task_statement.limit(1)) is not None:
            return True
        outside_tasks = select(MediaDerivativeTaskModel)
        if scope.derivative_task_ids:
            outside_tasks = outside_tasks.where(
                MediaDerivativeTaskModel.id.not_in(scope.derivative_task_ids),
            )
        for task in self.session.scalars(outside_tasks):
            if file_id in thumbnail_variant_file_ids(task.result_details):
                return True
        return False

    def _capture_file_artifact(
        self,
        intent: ReviewDeletionIntentModel,
        file_object: FileObjectModel,
        artifact_kind: str,
    ) -> None:
        if MANAGED_FILE_ID_RE.fullmatch(file_object.id) is None:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件对象标识不符合存储合同")
        if SHA256_RE.fullmatch(file_object.sha256) is None:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件对象缺少强内容身份")
        expected_path = contained_path(
            Path("files") / file_object.id,
            self.settings.storage_root,
        )
        actual_path = contained_path(
            file_object.storage_path,
            self.settings.storage_root,
        )
        if actual_path != expected_path:
            raise ReviewError("STORAGE_UNAVAILABLE", "文件路径不符合存储合同")
        try:
            with pin_regular_file(actual_path, self.settings.storage_root) as pinned:
                if pinned is None or not pinned.exists:
                    return
                if None in (
                    pinned.device,
                    pinned.inode,
                    pinned.ctime_ns,
                    pinned.size,
                ):
                    raise UnsafeFilePathError("file identity is incomplete")
                self.session.add(
                    ReviewDeletionArtifactModel(
                        id=f"artifact_{hashlib.sha256(f'{intent.id}\0files/{file_object.id}'.encode()).hexdigest()[:32]}",
                        deletion_intent_id=intent.id,
                        artifact_kind=artifact_kind,
                        relative_logical_target=f"files/{file_object.id}",
                        expected_device=pinned.device,
                        expected_inode=pinned.inode,
                        expected_ctime_ns=pinned.ctime_ns,
                        expected_size=pinned.size,
                        expected_sha256=file_object.sha256,
                        status="pending",
                        attempts=0,
                    ),
                )
        except (OSError, UnsafeFilePathError) as error:
            raise ReviewError(
                "STORAGE_UNAVAILABLE",
                "文件身份无法安全捕获",
            ) from error

    def _capture_packages(
        self,
        intent: ReviewDeletionIntentModel,
        packages: list[FinalCutPackageSnapshotModel],
    ) -> set[str]:
        captured: set[str] = set()
        for package in packages:
            if PACKAGE_ID_RE.fullmatch(package.id) is None:
                raise ReviewError("STORAGE_UNAVAILABLE", "项目包标识不符合存储合同")
            expected_path = contained_path(
                Path(f"{package.id}.zip"),
                self.settings.package_root,
            )
            actual_path = contained_path(
                package.storage_path,
                self.settings.package_root,
            )
            if actual_path != expected_path:
                raise ReviewError("STORAGE_UNAVAILABLE", "项目包路径不符合存储合同")
            try:
                with pin_regular_file(actual_path, self.settings.package_root) as pinned:
                    if pinned is not None and pinned.exists:
                        if not package.sha256 or SHA256_RE.fullmatch(package.sha256) is None:
                            raise UnsafeFilePathError("package digest is unavailable")
                        if None in (
                            pinned.device,
                            pinned.inode,
                            pinned.ctime_ns,
                            pinned.size,
                        ):
                            raise UnsafeFilePathError("package identity is incomplete")
                        self.session.add(
                            ReviewDeletionArtifactModel(
                                id=f"artifact_{hashlib.sha256(f'{intent.id}\0packages/{package.id}.zip'.encode()).hexdigest()[:32]}",
                                deletion_intent_id=intent.id,
                                artifact_kind="project_package",
                                relative_logical_target=f"packages/{package.id}.zip",
                                expected_device=pinned.device,
                                expected_inode=pinned.inode,
                                expected_ctime_ns=pinned.ctime_ns,
                                expected_size=pinned.size,
                                expected_sha256=package.sha256,
                                status="pending",
                                attempts=0,
                            ),
                        )
            except (OSError, UnsafeFilePathError) as error:
                raise ReviewError(
                    "STORAGE_UNAVAILABLE",
                    "项目包身份无法安全捕获",
                ) from error
            captured.add(package.id)
        return captured

    def _delete_scope(
        self,
        *,
        project: ProjectRefModel,
        scope: _DeletionScope,
        package_ids: set[str],
        file_ids: set[str],
        delete_project: bool,
    ) -> None:
        # The live SQLite schema contains the same issue -> current revision ->
        # issue cycle as PostgreSQL.  PostgreSQL's named current-revision FK is
        # deferrable; SQLite needs its transaction-local equivalent before the
        # authorized durable deletion removes both sides of that cycle.
        if self.session.get_bind().dialect.name == "sqlite":
            self.session.execute(text("PRAGMA defer_foreign_keys=ON"))
        event_statement = select(OutboxEventModel.event_id).where(
            OutboxEventModel.project_ref_id == project.id,
        )
        if not delete_project:
            event_predicates: list[Any] = []
            if scope.item_ids:
                event_predicates.extend(
                    [
                        OutboxEventModel.review_item_id.in_(scope.item_ids),
                        OutboxEventModel.aggregate_id.in_(scope.item_ids),
                    ],
                )
            if scope.version_ids:
                event_predicates.extend(
                    [
                        OutboxEventModel.version_id.in_(scope.version_ids),
                        OutboxEventModel.aggregate_id.in_(scope.version_ids),
                    ],
                )
            if scope.issue_ids:
                event_predicates.extend(
                    [
                        OutboxEventModel.issue_id.in_(scope.issue_ids),
                        OutboxEventModel.aggregate_id.in_(scope.issue_ids),
                    ],
                )
            if scope.finalization_ids:
                event_predicates.extend(
                    [
                        OutboxEventModel.finalization_id.in_(scope.finalization_ids),
                        OutboxEventModel.aggregate_id.in_(scope.finalization_ids),
                    ],
                )
            if package_ids:
                event_predicates.extend(
                    [
                        OutboxEventModel.package_id.in_(package_ids),
                        OutboxEventModel.aggregate_id.in_(package_ids),
                    ],
                )
            event_statement = event_statement.where(or_(*event_predicates))
        event_ids = tuple(self.session.scalars(event_statement))
        if event_ids:
            self.session.execute(
                delete(OutboxConsumerReceiptModel).where(
                    OutboxConsumerReceiptModel.event_id.in_(event_ids),
                ),
            )
            self.session.execute(
                delete(OutboxEventModel).where(
                    OutboxEventModel.event_id.in_(event_ids),
                ),
            )
        invalidation_predicates: list[Any] = []
        if scope.finalization_ids:
            invalidation_predicates.append(
                FinalizationPackageInvalidationModel.finalization_id.in_(
                    scope.finalization_ids,
                ),
            )
        if package_ids:
            invalidation_predicates.append(
                FinalizationPackageInvalidationModel.package_id.in_(package_ids),
            )
        if invalidation_predicates:
            self.session.execute(
                delete(FinalizationPackageInvalidationModel).where(
                    or_(*invalidation_predicates),
                ),
            )
        if scope.issue_ids:
            self.session.execute(
                delete(ReviewThreadMessageModel).where(
                    ReviewThreadMessageModel.issue_id.in_(scope.issue_ids),
                ),
            )
        if scope.version_ids:
            self.session.execute(
                delete(ReviewDecisionModel).where(
                    ReviewDecisionModel.version_id.in_(scope.version_ids),
                ),
            )
        if scope.issue_ids:
            self.session.execute(
                delete(ReviewIssueModel).where(
                    ReviewIssueModel.id.in_(scope.issue_ids),
                ),
            )
        if scope.item_ids:
            self.session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id.in_(scope.item_ids))
                .values(current_version_id=None, active_finalization_id=None),
            )
            self.session.flush()
        if scope.finalization_ids:
            self.session.execute(
                delete(FinalizationRecordModel).where(
                    FinalizationRecordModel.id.in_(scope.finalization_ids),
                ),
            )
        if scope.derivative_task_ids:
            self.session.execute(
                delete(MediaDerivativeTaskModel).where(
                    MediaDerivativeTaskModel.id.in_(scope.derivative_task_ids),
                ),
            )
        if scope.version_ids:
            self.session.execute(
                delete(ReviewVersionModel).where(
                    ReviewVersionModel.id.in_(scope.version_ids),
                ),
            )
        if scope.item_ids:
            self.session.execute(
                delete(ReviewItemModel).where(
                    ReviewItemModel.id.in_(scope.item_ids),
                ),
            )
        if package_ids:
            self.session.execute(
                delete(FinalCutPackageSnapshotModel).where(
                    FinalCutPackageSnapshotModel.id.in_(package_ids),
                ),
            )
        if file_ids:
            uploads = list(
                self.session.scalars(
                    select(UploadSessionModel)
                    .where(UploadSessionModel.file_id.in_(file_ids))
                    .order_by(UploadSessionModel.id)
                    .with_for_update(),
                ),
            )
            for upload in uploads:
                upload.file_id = None
                if upload.parts_cleanup_confirmed_at is not None and not upload.received_parts:
                    self.session.delete(upload)
                else:
                    if upload.received_parts:
                        upload.parts_cleanup_confirmed_at = None
                    upload.updated_at = utcnow()
            self.session.flush()
            self.session.execute(
                delete(FileObjectModel).where(FileObjectModel.id.in_(file_ids)),
            )
        if delete_project:
            self.session.delete(project)
        self.session.flush()

    def _finish_database_deletion(
        self,
        intent: ReviewDeletionIntentModel,
    ) -> None:
        artifact_count = self.session.scalar(
            select(func.count())
            .select_from(ReviewDeletionArtifactModel)
            .where(ReviewDeletionArtifactModel.deletion_intent_id == intent.id),
        ) or 0
        now = utcnow()
        if artifact_count == 0:
            intent.status = "complete"
            intent.completed_at = now
            intent.updated_at = now
        else:
            self.enqueue_post_commit(intent.id)
        self.session.flush()
