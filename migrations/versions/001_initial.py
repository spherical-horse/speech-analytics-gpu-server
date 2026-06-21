"""Initial schema: tasks and api_tokens

Revision ID: 001
Revises:
Create Date: 2026-06-22

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", sa.Text, nullable=False),
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="queued",
        ),
        sa.Column("progress", sa.SmallInteger, server_default="0"),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("transcript_data", JSONB, nullable=True),
        sa.Column("webhook_url", sa.Text, nullable=True),
        sa.Column("webhook_status", sa.Text, nullable=True),
        sa.Column("webhook_attempts", sa.SmallInteger, server_default="0"),
        sa.Column("webhook_last_error", sa.Text, nullable=True),
        sa.Column("token_hash", sa.LargeBinary, nullable=False),
        sa.Column("token_raw", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('queued','processing','completed','failed')", name="ck_tasks_status"),
        sa.CheckConstraint(
            "webhook_status IN ('pending','delivered','failed') OR webhook_status IS NULL",
            name="ck_tasks_webhook_status",
        ),
    )

    op.create_index(
        "idx_tasks_status_completed",
        "tasks",
        ["status", "completed_at"],
        postgresql_where=sa.text("status = 'completed'"),
    )


def downgrade() -> None:
    op.drop_index("idx_tasks_status_completed", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("api_tokens")
