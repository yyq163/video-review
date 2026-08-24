"""persist independent technical QC tasks and idempotent findings

Revision ID: 20260825_0026
Revises: 20260825_0025
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260825_0026"
down_revision = "20260825_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "technical_qc_tasks",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("input_file_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("claim_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_ms", sa.BigInteger(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("version_id", name="uq_technical_qc_tasks_version"),
        sa.ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            name="fk_technical_qc_tasks_version_identity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_file_id"],
            ["file_objects.id"],
            name="fk_technical_qc_tasks_input_file",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status in ('queued','running','complete','failed')",
            name="ck_technical_qc_tasks_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_technical_qc_tasks_attempts"),
        sa.CheckConstraint(
            "processed_ms >= 0 AND total_ms > 0 AND processed_ms <= total_ms",
            name="ck_technical_qc_tasks_progress",
        ),
        sa.CheckConstraint(
            "length(trim(rule_version)) BETWEEN 1 AND 64",
            name="ck_technical_qc_tasks_rule_version",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND claim_id IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status != 'running' AND claim_id IS NULL AND lease_expires_at IS NULL)",
            name="ck_technical_qc_tasks_claim",
        ),
        sa.CheckConstraint(
            "(status = 'complete' AND completed_at IS NOT NULL AND processed_ms = total_ms) "
            "OR (status != 'complete' AND completed_at IS NULL)",
            name="ck_technical_qc_tasks_completed",
        ),
    )
    op.create_index(
        "ix_technical_qc_tasks_claim",
        "technical_qc_tasks",
        ["status", "next_attempt_at", "lease_expires_at", "created_at", "id"],
    )
    op.create_index(
        "ix_technical_qc_tasks_project_running",
        "technical_qc_tasks",
        ["project_ref_id", "status", "review_item_id", "id"],
    )

    op.create_table(
        "technical_qc_findings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("project_ref_id", sa.String(length=64), nullable=False),
        sa.Column("review_item_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("issue_id", sa.String(length=64), nullable=True),
        sa.Column("start_frame", sa.BigInteger(), nullable=False),
        sa.Column("end_frame", sa.BigInteger(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("value_details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("identity_hash", name="uq_technical_qc_findings_identity"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["technical_qc_tasks.id"],
            name="fk_technical_qc_findings_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "project_ref_id", "review_item_id"],
            ["review_versions.id", "review_versions.project_ref_id", "review_versions.review_item_id"],
            name="fk_technical_qc_findings_version_identity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issue_id"],
            ["review_issues.id"],
            name="fk_technical_qc_findings_issue",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("length(identity_hash) = 64", name="ck_technical_qc_findings_identity"),
        sa.CheckConstraint(
            "start_frame >= 0 AND end_frame >= start_frame",
            name="ck_technical_qc_findings_frames",
        ),
        sa.CheckConstraint(
            "start_ms >= 0 AND end_ms >= start_ms",
            name="ck_technical_qc_findings_time",
        ),
    )
    op.create_index(
        "ix_technical_qc_findings_task",
        "technical_qc_findings",
        ["task_id", "start_frame", "end_frame", "id"],
    )
    op.create_index(
        "ix_technical_qc_findings_issue",
        "technical_qc_findings",
        ["issue_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    task_count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM technical_qc_tasks")).scalar_one(),
    )
    finding_count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM technical_qc_findings")).scalar_one(),
    )
    if task_count or finding_count:
        raise RuntimeError(
            "refusing to downgrade technical QC schema while persisted task data exists",
        )
    op.drop_index("ix_technical_qc_findings_issue", table_name="technical_qc_findings")
    op.drop_index("ix_technical_qc_findings_task", table_name="technical_qc_findings")
    op.drop_table("technical_qc_findings")
    op.drop_index("ix_technical_qc_tasks_project_running", table_name="technical_qc_tasks")
    op.drop_index("ix_technical_qc_tasks_claim", table_name="technical_qc_tasks")
    op.drop_table("technical_qc_tasks")
