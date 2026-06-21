from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_AUDIO = "INVALID_AUDIO"
    TOO_LONG = "TOO_LONG"
    TOO_LARGE = "TOO_LARGE"
    NOT_FOUND = "NOT_FOUND"
    OOM_ERROR = "OOM_ERROR"
    DIARIZATION_FAILED = "DIARIZATION_FAILED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class Error(BaseModel):
    error_code: ErrorCode
    error_message: str


class TaskAccepted(BaseModel):
    task_id: uuid.UUID
    call_id: str
    status: str = "queued"
    webhook_enabled: bool


class TaskQueued(BaseModel):
    task_id: uuid.UUID
    call_id: str
    status: str = "queued"
    created_at: datetime


class TaskProcessing(BaseModel):
    task_id: uuid.UUID
    call_id: str
    status: str = "processing"
    progress: int | None = Field(None, ge=0, le=100)


class WebhookStatusEnum(str, Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"


class Word(BaseModel):
    word: str
    start: float
    end: float
    score: float = Field(ge=0.0, le=1.0)
    speaker: str


class Segment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str
    words: list[Word] = []


class Transcript(BaseModel):
    language: str = "ru"
    duration: float
    speakers: list[str]
    segments: list[Segment]


class TaskCompleted(BaseModel):
    task_id: uuid.UUID
    call_id: str
    status: str = "completed"
    completed_at: datetime
    webhook_status: WebhookStatusEnum | None = None
    webhook_attempts: int = 0
    transcript: Transcript


class TaskFailed(BaseModel):
    task_id: uuid.UUID
    call_id: str
    status: str = "failed"
    webhook_status: WebhookStatusEnum | None = None
    error_code: str
    error_message: str


class HealthResponse(BaseModel):
    status: str  # "ok" | "degraded"
    postgres: bool
    redis: bool
    gpu_available: bool
    models_loaded: bool
    gpu_memory_used_mb: int


class WebhookRetryResponse(BaseModel):
    task_id: uuid.UUID
    webhook_status: str = "pending"
