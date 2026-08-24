"""harden technical QC media identity and tenant constraints

Revision ID: 20260825_0027
Revises: 20260825_0026
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260825_0027"
down_revision = "20260825_0026"
branch_labels = None
depends_on = None


def _batch_recreate() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    with op.batch_alter_table("technical_qc_tasks", recreate=_batch_recreate()) as batch:
        batch.add_column(sa.Column("video_frame_count", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("video_duration_ms", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("container_duration_ms", sa.BigInteger(), nullable=True))
        batch.create_unique_constraint(
            "uq_technical_qc_tasks_identity",
            ["id", "project_ref_id", "review_item_id", "version_id"],
        )
        batch.create_check_constraint(
            "ck_technical_qc_tasks_probe_facts",
            "(video_frame_count IS NULL AND video_duration_ms IS NULL AND container_duration_ms IS NULL) "
            "OR (video_frame_count > 0 AND video_duration_ms > 0 AND container_duration_ms > 0)",
        )

    op.create_index(
        "uq_technical_qc_tasks_running_item",
        "technical_qc_tasks",
        ["review_item_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
        postgresql_where=sa.text("status = 'running'"),
    )

    with op.batch_alter_table("technical_qc_findings", recreate=_batch_recreate()) as batch:
        batch.drop_constraint("fk_technical_qc_findings_task", type_="foreignkey")
        batch.drop_constraint("fk_technical_qc_findings_issue", type_="foreignkey")
        batch.create_foreign_key(
            "fk_technical_qc_findings_task_identity",
            "technical_qc_tasks",
            ["task_id", "project_ref_id", "review_item_id", "version_id"],
            ["id", "project_ref_id", "review_item_id", "version_id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_technical_qc_findings_issue_identity",
            "review_issues",
            ["issue_id", "project_ref_id", "review_item_id", "version_id"],
            ["id", "project_ref_id", "review_item_id", "version_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    task_count = int(
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM technical_qc_tasks")).scalar_one(),
    )
    if task_count:
        raise RuntimeError(
            "refusing to downgrade technical QC hardening while persisted task data exists",
        )
    with op.batch_alter_table("technical_qc_findings", recreate=_batch_recreate()) as batch:
        batch.drop_constraint("fk_technical_qc_findings_issue_identity", type_="foreignkey")
        batch.drop_constraint("fk_technical_qc_findings_task_identity", type_="foreignkey")
        batch.create_foreign_key(
            "fk_technical_qc_findings_task",
            "technical_qc_tasks",
            ["task_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_technical_qc_findings_issue",
            "review_issues",
            ["issue_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.drop_index(
        "uq_technical_qc_tasks_running_item",
        table_name="technical_qc_tasks",
    )
    with op.batch_alter_table("technical_qc_tasks", recreate=_batch_recreate()) as batch:
        batch.drop_constraint("ck_technical_qc_tasks_probe_facts", type_="check")
        batch.drop_constraint("uq_technical_qc_tasks_identity", type_="unique")
        batch.drop_column("container_duration_ms")
        batch.drop_column("video_duration_ms")
        batch.drop_column("video_frame_count")
