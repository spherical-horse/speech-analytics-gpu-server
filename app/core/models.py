from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, LargeBinary, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('queued','processing','completed','failed')", name="ck_tasks_status"),
        CheckConstraint(
            "webhook_status IN ('pending','delivered','failed') OR webhook_status IS NULL",
            name="ck_tasks_webhook_status",
        ),
        Index("idx_tasks_status_completed", "status", "completed_at", postgresql_where="status = 'completed'"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    progress: Mapped[int | None] = mapped_column(SmallInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    transcript_data: Mapped[dict | None] = mapped_column(JSONB)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    webhook_status: Mapped[str | None] = mapped_column(Text)
    webhook_attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    webhook_last_error: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_raw: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
