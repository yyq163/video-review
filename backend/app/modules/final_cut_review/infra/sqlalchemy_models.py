from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectRefModel(Base):
    __tablename__ = "project_refs"
    __table_args__ = (
        UniqueConstraint("project_code", name="uq_project_refs_project_code"),
        UniqueConstraint("source", "external_project_id", name="uq_project_refs_source_external_project_id"),
        CheckConstraint("source in ('local','host')", name="ck_project_refs_source"),
        CheckConstraint("lifecycle_status in ('active','archived')", name="ck_project_refs_lifecycle_status"),
        CheckConstraint("lock_version >= 1", name="ck_project_refs_lock_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_code: Mapped[str] = mapped_column(String(128), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    external_project_id: Mapped[str | None] = mapped_column(String(128))
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __mapper_args__ = {"version_id_col": lock_version, "version_id_generator": False}


Index("ix_project_refs_lifecycle_status", ProjectRefModel.lifecycle_status)
Index("ix_project_refs_deleted_at", ProjectRefModel.deleted_at)


class FileObjectModel(Base):
    __tablename__ = "file_objects"
    __table_args__ = (
        CheckConstraint("file_size > 0", name="ck_file_objects_file_size"),
        CheckConstraint("duration_ms > 0", name="ck_file_objects_duration_ms"),
        CheckConstraint("width > 0 and height > 0", name="ck_file_objects_dimensions"),
        CheckConstraint("fps_num > 0 and fps_den > 0", name="ck_file_objects_fps"),
        CheckConstraint("length(sha256) = 64", name="ck_file_objects_sha256_len"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    owner_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_principal_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_num: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_den: Mapped[int] = mapped_column(Integer, nullable=False)
    media_probe_version: Mapped[str] = mapped_column(String(64), nullable=False, default="local-probe-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_file_objects_sha256", FileObjectModel.sha256)
Index("ix_file_objects_owner", FileObjectModel.owner_principal_id)


class UploadSessionModel(Base):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        CheckConstraint("declared_size > 0", name="ck_upload_sessions_declared_size"),
        CheckConstraint(
            "status in ('initiated','receiving','finalizing','completed','aborted')",
            name="ck_upload_sessions_status",
        ),
        CheckConstraint("length(expected_sha256) = 64", name="ck_upload_sessions_sha256_len"),
        CheckConstraint("reserved_bytes = declared_size * 2", name="ck_upload_sessions_reserved_bytes"),
        CheckConstraint(
            "(status = 'finalizing' AND finalization_lease_id IS NOT NULL "
            "AND finalization_lease_expires_at IS NOT NULL AND finalization_file_id IS NOT NULL "
            "AND length(finalization_idempotency_key_hash) = 64 AND length(finalization_request_hash) = 64) "
            "OR (status != 'finalizing' AND finalization_lease_id IS NULL "
            "AND finalization_lease_expires_at IS NULL AND finalization_file_id IS NULL "
            "AND finalization_idempotency_key_hash IS NULL AND finalization_request_hash IS NULL)",
            name="ck_upload_sessions_finalization_lease",
        ),
        CheckConstraint(
            "parts_cleanup_confirmed_at IS NULL OR status IN ('completed','aborted')",
            name="ck_upload_sessions_cleanup_terminal",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="initiated")
    temp_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    owner_principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_principal_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    received_parts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parts_cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalization_lease_id: Mapped[str | None] = mapped_column(String(64))
    finalization_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalization_file_id: Mapped[str | None] = mapped_column(String(64))
    finalization_idempotency_key_hash: Mapped[str | None] = mapped_column(String(64))
    finalization_request_hash: Mapped[str | None] = mapped_column(String(64))
    file_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("file_objects.id", ondelete="RESTRICT"))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_num: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_den: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_upload_sessions_status", UploadSessionModel.status)
Index("ix_upload_sessions_owner", UploadSessionModel.owner_principal_id)
Index("ix_upload_sessions_cleanup", UploadSessionModel.status, UploadSessionModel.updated_at, UploadSessionModel.id)
Index(
    "ix_upload_sessions_quota",
    UploadSessionModel.parts_cleanup_confirmed_at,
    UploadSessionModel.owner_principal_kind,
    UploadSessionModel.owner_principal_id,
)


def _prepare_upload_reservation(_mapper: object, _connection: object, upload: UploadSessionModel) -> None:
    if upload.reserved_bytes is None:
        upload.reserved_bytes = upload.declared_size * 2
    if upload.status != "finalizing":
        upload.finalization_lease_id = None
        upload.finalization_lease_expires_at = None
        upload.finalization_file_id = None
        upload.finalization_idempotency_key_hash = None
        upload.finalization_request_hash = None


event.listen(UploadSessionModel, "before_insert", _prepare_upload_reservation)
event.listen(UploadSessionModel, "before_update", _prepare_upload_reservation)


class ReviewItemModel(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("project_ref_id", "item_code", name="uq_review_items_project_item_code"),
        UniqueConstraint("id", "project_ref_id", name="uq_review_items_id_project"),
        ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["current_version_id", "project_ref_id", "id", "current_version_is_current"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id", "review_versions.is_current"],
            name="fk_review_items_current_version_identity",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["active_finalization_id", "project_ref_id", "id", "current_version_id"],
            ["finalizations.id", "finalizations.project_ref_id", "finalizations.review_item_id", "finalizations.version_id"],
            name="fk_review_items_active_finalization_current_version",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint("workflow_status in ('pending_review','in_review','changes_requested','finalized')", name="ck_review_items_workflow_status"),
        CheckConstraint(
            "workflow_status != 'finalized' OR (active_finalization_id IS NOT NULL AND current_version_id IS NOT NULL)",
            name="ck_review_items_finalized_has_active_finalization",
        ),
        CheckConstraint("current_version_id IS NULL OR current_version_is_current = true", name="ck_review_items_current_version_true"),
        CheckConstraint("lock_version >= 1", name="ck_review_items_lock_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_code: Mapped[str] = mapped_column(String(128), nullable=False)
    episode_no: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    workflow_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    current_version_id: Mapped[str | None] = mapped_column(String(64))
    current_version_is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_finalization_id: Mapped[str | None] = mapped_column(String(64))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __mapper_args__ = {"version_id_col": lock_version, "version_id_generator": False}


Index("ix_review_items_project_ref_id", ReviewItemModel.project_ref_id)


class ReviewVersionModel(Base):
    __tablename__ = "review_versions"
    __table_args__ = (
        UniqueConstraint("review_item_id", "version_no", name="uq_review_versions_item_version_no"),
        UniqueConstraint("original_file_id", name="uq_review_versions_original_file_id"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", name="uq_review_versions_identity"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", "is_current", name="uq_review_versions_identity_current"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", "original_file_id", name="uq_review_versions_identity_file"),
        ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["review_item_id", "project_ref_id"], ["review_items.id", "review_items.project_ref_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["previous_version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["original_file_id"], ["file_objects.id"], ondelete="RESTRICT"),
        CheckConstraint("version_no >= 1", name="ck_review_versions_version_no"),
        CheckConstraint("file_size > 0", name="ck_review_versions_file_size"),
        CheckConstraint("duration_ms > 0", name="ck_review_versions_duration_ms"),
        CheckConstraint("width > 0 and height > 0", name="ck_review_versions_dimensions"),
        CheckConstraint("fps_num > 0 and fps_den > 0", name="ck_review_versions_fps"),
        CheckConstraint("lock_version >= 1", name="ck_review_versions_lock_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_version_id: Mapped[str | None] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str] = mapped_column(String(32), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    original_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_num: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_den: Mapped[int] = mapped_column(Integer, nullable=False)
    media_probe_version: Mapped[str] = mapped_column(String(64), nullable=False)
    playback_asset_id: Mapped[str | None] = mapped_column(String(64))
    thumbnail_asset_id: Mapped[str | None] = mapped_column(String(64))
    version_note: Mapped[str | None] = mapped_column(Text)
    change_summary: Mapped[str | None] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index(
    "uq_review_versions_current_true",
    ReviewVersionModel.review_item_id,
    unique=True,
    sqlite_where=ReviewVersionModel.is_current.is_(True),
    postgresql_where=ReviewVersionModel.is_current.is_(True),
)
Index("ix_review_versions_project_item", ReviewVersionModel.project_ref_id, ReviewVersionModel.review_item_id)


class ReviewIssueModel(Base):
    __tablename__ = "review_issues"
    __table_args__ = (
        UniqueConstraint("review_item_id", "issue_no", name="uq_review_issues_item_issue_no"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", name="uq_review_issues_identity"),
        ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["current_revision_id", "id", "project_ref_id", "review_item_id", "version_id"],
            [
                "issue_revisions.id",
                "issue_revisions.issue_id",
                "issue_revisions.project_ref_id",
                "issue_revisions.review_item_id",
                "issue_revisions.version_id",
            ],
            name="fk_review_issues_current_revision_identity",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("status in ('unresolved','resolved')", name="ck_review_issues_status"),
        CheckConstraint("timestamp_ms >= 0 and frame_number >= 0", name="ck_review_issues_time"),
        CheckConstraint("lock_version >= 1", name="ck_review_issues_lock_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unresolved")
    current_revision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    __mapper_args__ = {"version_id_col": lock_version, "version_id_generator": False}


Index("ix_review_issues_version", ReviewIssueModel.project_ref_id, ReviewIssueModel.review_item_id, ReviewIssueModel.version_id)
Index(
    "ix_review_issues_active_version",
    ReviewIssueModel.project_ref_id,
    ReviewIssueModel.review_item_id,
    ReviewIssueModel.version_id,
    ReviewIssueModel.deleted_at,
)


class ReviewIssueRevisionModel(Base):
    __tablename__ = "issue_revisions"
    __table_args__ = (
        UniqueConstraint("issue_id", "revision_no", name="uq_issue_revisions_issue_revision_no"),
        UniqueConstraint("id", "issue_id", name="uq_issue_revisions_id_issue"),
        UniqueConstraint("id", "issue_id", "project_ref_id", "review_item_id", "version_id", name="uq_issue_revisions_current_identity"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", "issue_id", name="uq_issue_revisions_identity"),
        ForeignKeyConstraint(
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_no >= 1", name="ck_issue_revisions_revision_no"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    annotation_set_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_issue_revisions_issue", ReviewIssueRevisionModel.issue_id)


class ReviewAnnotationSetModel(Base):
    __tablename__ = "annotation_sets"
    __table_args__ = (
        UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", "issue_id", name="uq_annotation_sets_identity"),
        ForeignKeyConstraint(
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("timestamp_ms >= 0 and frame_number >= 0", name="ck_annotation_sets_time"),
        CheckConstraint("canvas_width > 0 and canvas_height > 0 and video_width > 0 and video_height > 0", name="ck_annotation_sets_dimensions"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    frame_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canvas_width: Mapped[int] = mapped_column(Integer, nullable=False)
    canvas_height: Mapped[int] = mapped_column(Integer, nullable=False)
    video_width: Mapped[int] = mapped_column(Integer, nullable=False)
    video_height: Mapped[int] = mapped_column(Integer, nullable=False)
    shapes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReviewThreadMessageModel(Base):
    __tablename__ = "thread_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index(
    "ix_thread_messages_issue",
    ReviewThreadMessageModel.project_ref_id,
    ReviewThreadMessageModel.review_item_id,
    ReviewThreadMessageModel.version_id,
    ReviewThreadMessageModel.issue_id,
)


class ReviewDecisionModel(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("decision_type in ('changes_requested','finalized')", name="ck_review_decisions_decision_type"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_review_decisions_version", ReviewDecisionModel.project_ref_id, ReviewDecisionModel.review_item_id, ReviewDecisionModel.version_id)


class FinalizationRecordModel(Base):
    __tablename__ = "finalizations"
    __table_args__ = (
        UniqueConstraint("id", "project_ref_id", "review_item_id", name="uq_finalizations_identity"),
        UniqueConstraint("id", "project_ref_id", "review_item_id", "version_id", name="uq_finalizations_current_version_identity"),
        ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id", "original_file_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id", "review_versions.original_file_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("status = 'active'", name="ck_finalization_active_only"),
        CheckConstraint("version_no >= 1", name="ck_finalizations_version_no"),
        CheckConstraint("file_size > 0", name="ck_finalizations_file_size"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_num: Mapped[int] = mapped_column(Integer, nullable=False)
    fps_den: Mapped[int] = mapped_column(Integer, nullable=False)
    media_probe_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index(
    "uq_finalizations_active_review_item",
    FinalizationRecordModel.review_item_id,
    unique=True,
    sqlite_where=FinalizationRecordModel.status == "active",
    postgresql_where=FinalizationRecordModel.status == "active",
)
Index("ix_finalizations_project_version", FinalizationRecordModel.project_ref_id, FinalizationRecordModel.review_item_id, FinalizationRecordModel.version_id)


class FinalCutPackageSnapshotModel(Base):
    __tablename__ = "package_snapshots"
    __table_args__ = (
        UniqueConstraint("id", "project_ref_id", name="uq_package_snapshots_id_project"),
        ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], ondelete="RESTRICT"),
        CheckConstraint("status in ('preparing','ready','failed','expired')", name="ck_package_snapshots_status"),
        CheckConstraint("sha256 is null or length(sha256) = 64", name="ck_package_snapshots_sha256"),
        CheckConstraint("status != 'ready' or sha256 is not null", name="ck_package_snapshots_ready_sha256"),
        CheckConstraint("storage_bytes >= 0", name="ck_package_snapshots_storage_bytes"),
        CheckConstraint("build_attempts >= 0", name="ck_package_snapshots_build_attempts"),
        CheckConstraint(
            "(build_lease_id is null and build_lease_expires_at is null) "
            "or (status = 'preparing' and build_lease_id is not null and build_lease_expires_at is not null)",
            name="ck_package_snapshots_build_lease",
        ),
        CheckConstraint(
            "(download_session_hash is null) = (download_session_expires_at is null)",
            name="ck_package_snapshots_download_session_pair",
        ),
        CheckConstraint(
            "(download_lease_id is null) = (download_lease_expires_at is null)",
            name="ck_package_snapshots_download_lease_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    package_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    build_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_build_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_lease_id: Mapped[str | None] = mapped_column(String(64))
    build_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_session_hash: Mapped[str | None] = mapped_column(String(64))
    download_session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_lease_id: Mapped[str | None] = mapped_column(String(64))
    download_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_download_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_reclaimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_package_snapshots_project", FinalCutPackageSnapshotModel.project_ref_id)
Index(
    "ix_package_snapshots_worker_queue",
    FinalCutPackageSnapshotModel.status,
    FinalCutPackageSnapshotModel.next_build_attempt_at,
    FinalCutPackageSnapshotModel.created_at,
    FinalCutPackageSnapshotModel.id,
)
Index(
    "ix_package_snapshots_build_lease",
    FinalCutPackageSnapshotModel.status,
    FinalCutPackageSnapshotModel.build_lease_expires_at,
)
Index(
    "ix_package_snapshots_cleanup",
    FinalCutPackageSnapshotModel.storage_reclaimed_at,
    FinalCutPackageSnapshotModel.updated_at,
    FinalCutPackageSnapshotModel.expires_at,
    FinalCutPackageSnapshotModel.id,
)
Index(
    "ix_package_snapshots_quota",
    FinalCutPackageSnapshotModel.status,
    FinalCutPackageSnapshotModel.storage_bytes,
)
Index(
    "uq_package_snapshots_preparing_project",
    FinalCutPackageSnapshotModel.project_ref_id,
    unique=True,
    sqlite_where=FinalCutPackageSnapshotModel.status == "preparing",
    postgresql_where=FinalCutPackageSnapshotModel.status == "preparing",
)


class OutboxEventModel(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_event_id"),
        UniqueConstraint("sequence", name="uq_outbox_events_sequence"),
        ForeignKeyConstraint(["project_ref_id"], ["project_refs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["review_item_id", "project_ref_id"], ["review_items.id", "review_items.project_ref_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["review_issues.id", "review_issues.project_ref_id", "review_issues.review_item_id", "review_issues.version_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finalization_id", "project_ref_id", "review_item_id"],
            ["finalizations.id", "finalizations.project_ref_id", "finalizations.review_item_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["package_id", "project_ref_id"], ["package_snapshots.id", "package_snapshots.project_ref_id"], ondelete="RESTRICT"),
        CheckConstraint("event_version >= 1", name="ck_outbox_events_event_version"),
        CheckConstraint("aggregate_version >= 1", name="ck_outbox_events_aggregate_version"),
        CheckConstraint("sequence >= 1", name="ck_outbox_events_sequence"),
        CheckConstraint("status in ('pending','publishing','failed','dispatched')", name="ck_outbox_events_status"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    project_ref_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_item_id: Mapped[str | None] = mapped_column(String(64))
    version_id: Mapped[str | None] = mapped_column(String(64))
    issue_id: Mapped[str | None] = mapped_column(String(64))
    finalization_id: Mapped[str | None] = mapped_column(String(64))
    package_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_outbox_events_status_id", OutboxEventModel.status, OutboxEventModel.id)


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("length(request_hash) = 64", name="ck_idempotency_records_hash_len"),
        CheckConstraint("status_code in (102, 200)", name="ck_idempotency_records_status_code"),
    )

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_idempotency_records_command_type", IdempotencyRecordModel.command_type)


class OperationLogModel(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        CheckConstraint("entry_source in ('edit','review','embedded','unspecified')", name="ck_operation_logs_entry_source"),
        CheckConstraint("result in ('ok','error','unknown')", name="ck_operation_logs_result"),
        CheckConstraint(
            "idempotency_key_hash IS NULL OR length(idempotency_key_hash) = 64",
            name="ck_operation_logs_idempotency_hash",
        ),
        CheckConstraint(
            "operation_identity_hash IS NULL OR length(operation_identity_hash) = 64",
            name="ck_operation_logs_operation_identity_hash",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_source: Mapped[str] = mapped_column(String(32), nullable=False)
    command_type: Mapped[str] = mapped_column(String(128), nullable=False, server_default="LegacyOperation")
    capability: Mapped[str | None] = mapped_column(String(128))
    principal_kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="anonymous")
    principal_id: Mapped[str | None] = mapped_column(String(128))
    client_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64))
    operation_identity_hash: Mapped[str | None] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="request")
    resource_id: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    failure_stage: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


Index("ix_operation_logs_request_id", OperationLogModel.request_id)
Index("ix_operation_logs_command_type", OperationLogModel.command_type)
Index(
    "uq_operation_logs_unknown_identity",
    OperationLogModel.operation_identity_hash,
    unique=True,
    sqlite_where=((OperationLogModel.result == "unknown") & OperationLogModel.operation_identity_hash.is_not(None)),
    postgresql_where=((OperationLogModel.result == "unknown") & OperationLogModel.operation_identity_hash.is_not(None)),
)


class OutboxConsumerReceiptModel(Base):
    __tablename__ = "outbox_consumer_receipts"
    __table_args__ = (
        ForeignKeyConstraint(["event_id"], ["outbox_events.event_id"], ondelete="RESTRICT"),
        UniqueConstraint("event_id", "consumer_name", name="uq_outbox_consumer_event_consumer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
