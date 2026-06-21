from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from pathlib import Path

import aiofiles
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import authenticate_request, hash_token
from app.api.schemas import (
    ErrorCode,
    HealthResponse,
    TaskAccepted,
    TaskCompleted,
    TaskFailed,
    TaskProcessing,
    TaskQueued,
    Transcript,
    WebhookRetryResponse,
)
from app.core.config import settings
from app.core.db import get_session
from app.core.models import ApiToken, Task
from app.core.redis import get_arq_pool

log = structlog.get_logger()

router = APIRouter()

ALLOWED_EXTENSIONS = {"wav", "mp3", "ogg", "flac"}
AUDIO_MAGIC_BYTES: dict[bytes, str] = {
    b"RIFF": "wav",
    b"ID3\x03": "mp3",
    b"ID3\x04": "mp3",
    b"\xff\xfb": "mp3",
    b"\xff\xf3": "mp3",
    b"\xff\xf2": "mp3",
    b"OggS": "ogg",
    b"fLaC": "flac",
}


def _detect_audio_format(header: bytes) -> bool:
    for magic, _ in AUDIO_MAGIC_BYTES.items():
        if header[: len(magic)] == magic:
            return True
    return False


def _get_extension(filename: str | None, content_type: str | None) -> str:
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_EXTENSIONS:
            return ext
    ct_map = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/flac": "flac",
        "audio/x-flac": "flac",
    }
    if content_type and content_type in ct_map:
        return ct_map[content_type]
    return "bin"


@router.post("/transcribe", status_code=202)
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    call_id: str = Form(...),
    webhook_url: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> TaskAccepted:
    auth_header = request.headers.get("Authorization", "")
    raw_token = auth_header.removeprefix("Bearer ").strip()
    token_record = await authenticate_request(request, session)

    # Validate webhook_url
    if webhook_url is not None:
        if not settings.WEBHOOK_ALLOW_HTTP and not webhook_url.startswith("https://"):
            raise HTTPException(
                status_code=400,
                detail={"error_code": ErrorCode.INVALID_AUDIO, "error_message": "webhook_url must use https://"},
            )

    # Stream file, check size and magic bytes
    ext = _get_extension(file.filename, file.content_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail={"error_code": ErrorCode.INVALID_AUDIO, "error_message": f"Unsupported file format: {ext}"},
        )

    task_id = uuid.uuid4()
    upload_path = Path(settings.UPLOADS_DIR) / f"{task_id}.{ext}"
    upload_path.parent.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    header_checked = False
    max_bytes = settings.MAX_FILE_SIZE_BYTES

    try:
        async with aiofiles.open(upload_path, "wb") as out:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                if not header_checked:
                    if not _detect_audio_format(chunk):
                        raise HTTPException(
                            status_code=400,
                            detail={"error_code": ErrorCode.INVALID_AUDIO, "error_message": "File does not appear to be a valid audio file"},
                        )
                    header_checked = True
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    await out.close()
                    upload_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail={"error_code": ErrorCode.TOO_LARGE, "error_message": f"File exceeds {settings.MAX_FILE_SIZE_MB} MB limit"},
                    )
                await out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail={"error_code": ErrorCode.INVALID_AUDIO, "error_message": str(exc)},
        ) from exc

    token_hash_bytes = hash_token(raw_token)

    task = Task(
        id=task_id,
        call_id=call_id,
        status="queued",
        progress=0,
        webhook_url=webhook_url,
        webhook_status="pending" if webhook_url else None,
        token_hash=token_hash_bytes,
        token_raw=raw_token,
    )
    session.add(task)
    await session.commit()

    arq_pool = await get_arq_pool()
    await arq_pool.enqueue_job("transcribe_task", str(task_id), str(ext))

    log.info("task_queued", task_id=str(task_id), call_id=call_id, webhook_enabled=bool(webhook_url))

    return TaskAccepted(
        task_id=task_id,
        call_id=call_id,
        status="queued",
        webhook_enabled=bool(webhook_url),
    )


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    auth_header = request.headers.get("Authorization", "")
    raw_token = auth_header.removeprefix("Bearer ").strip()
    await authenticate_request(request, session)

    result = await session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()

    incoming_hash = hash_token(raw_token)
    if task is None or not hmac.compare_digest(task.token_hash, incoming_hash):
        raise HTTPException(status_code=404, detail={"error_code": ErrorCode.NOT_FOUND, "error_message": "Task not found or has expired"})

    if task.status == "queued":
        return TaskQueued(task_id=task.id, call_id=task.call_id, status="queued", created_at=task.created_at)

    if task.status == "processing":
        return TaskProcessing(task_id=task.id, call_id=task.call_id, status="processing", progress=task.progress)

    if task.status == "completed":
        transcript = Transcript.model_validate(task.transcript_data)
        return TaskCompleted(
            task_id=task.id,
            call_id=task.call_id,
            status="completed",
            completed_at=task.completed_at,
            webhook_status=task.webhook_status,
            webhook_attempts=task.webhook_attempts,
            transcript=transcript,
        )

    # failed
    return TaskFailed(
        task_id=task.id,
        call_id=task.call_id,
        status="failed",
        webhook_status=task.webhook_status,
        error_code=task.error_code or "UNKNOWN_ERROR",
        error_message=task.error_message or "",
    )


@router.post("/tasks/{task_id}/webhook/retry", status_code=202)
async def retry_webhook(
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> WebhookRetryResponse:
    auth_header = request.headers.get("Authorization", "")
    raw_token = auth_header.removeprefix("Bearer ").strip()
    await authenticate_request(request, session)

    result = await session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()

    incoming_hash = hash_token(raw_token)
    if task is None or not hmac.compare_digest(task.token_hash, incoming_hash):
        raise HTTPException(status_code=404, detail={"error_code": ErrorCode.NOT_FOUND, "error_message": "Task not found"})

    if not task.webhook_url:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "BAD_REQUEST", "error_message": "Task has no webhook_url configured"},
        )

    task.webhook_status = "pending"
    task.webhook_attempts = 0
    task.webhook_last_error = None
    await session.commit()

    arq_pool = await get_arq_pool()
    await arq_pool.enqueue_job("deliver_webhook", str(task_id), 1)

    return WebhookRetryResponse(task_id=task_id, webhook_status="pending")


@router.get("/health", include_in_schema=True)
async def health(session: AsyncSession = Depends(get_session)) -> HealthResponse:
    postgres_ok = False
    try:
        await session.execute(text("SELECT 1"))
        postgres_ok = True
    except Exception:
        pass

    redis_ok = False
    try:
        pool = await get_arq_pool()
        await pool.ping()
        redis_ok = True
    except Exception:
        pass

    gpu_available = False
    gpu_memory_used_mb = 0
    try:
        import torch  # type: ignore[import-not-found]

        gpu_available = torch.cuda.is_available()
        if gpu_available:
            gpu_memory_used_mb = int(torch.cuda.memory_allocated() / 1024 / 1024)
    except ImportError:
        pass

    from app.worker import pipeline  # type: ignore[import-not-found]

    models_loaded = pipeline.models_loaded()

    overall = "ok" if (postgres_ok and redis_ok) else "degraded"

    return HealthResponse(
        status=overall,
        postgres=postgres_ok,
        redis=redis_ok,
        gpu_available=gpu_available,
        models_loaded=models_loaded,
        gpu_memory_used_mb=gpu_memory_used_mb,
    )
