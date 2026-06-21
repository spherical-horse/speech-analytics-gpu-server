from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import httpx
import structlog
from sqlalchemy import update

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.models import Task

log = structlog.get_logger()


def _sign_payload(raw_token: str, body: bytes) -> str:
    digest = hmac.new(raw_token.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _build_payload(task: Task) -> bytes:
    if task.status == "completed":
        data = {
            "event": "task.completed",
            "task_id": str(task.id),
            "call_id": task.call_id,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "transcript": task.transcript_data,
        }
    else:
        data = {
            "event": "task.failed",
            "task_id": str(task.id),
            "call_id": task.call_id,
            "failed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error_code": task.error_code,
            "error_message": task.error_message,
        }
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def deliver_webhook(ctx: dict, task_id: str, attempt: int) -> None:
    bound_log = log.bind(task_id=task_id, attempt=attempt)

    async with AsyncSessionLocal() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        if task is None or not task.webhook_url:
            return

        body = _build_payload(task)
        signature = _sign_payload(task.token_raw, body)

        start = time.monotonic()
        status_code = None
        error_msg = None

        try:
            async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    task.webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Signature": signature,
                        "X-Webhook-Task-Id": task_id,
                        "User-Agent": "TranscriptionService-Webhook/1.0",
                    },
                )
            status_code = response.status_code
            duration_ms = int((time.monotonic() - start) * 1000)

            if 200 <= status_code < 300:
                await session.execute(
                    update(Task)
                    .where(Task.id == uuid.UUID(task_id))
                    .values(webhook_status="delivered", webhook_attempts=attempt)
                )
                await session.commit()
                bound_log.info(
                    "webhook.delivered",
                    url=task.webhook_url,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
                return

            error_msg = f"HTTP {status_code}"

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            error_msg = str(exc)
            bound_log.warning(
                "webhook.attempt_failed",
                url=task.webhook_url,
                error=error_msg,
                duration_ms=duration_ms,
            )

        # Failed attempt
        await session.execute(
            update(Task)
            .where(Task.id == uuid.UUID(task_id))
            .values(webhook_attempts=attempt, webhook_last_error=error_msg)
        )
        await session.commit()

        if attempt < settings.WEBHOOK_MAX_ATTEMPTS:
            backoff_list = settings.WEBHOOK_BACKOFF_SECONDS
            delay = backoff_list[attempt] if attempt < len(backoff_list) else backoff_list[-1]

            from app.core.redis import get_arq_pool

            pool = await get_arq_pool()
            await pool.enqueue_job(
                "deliver_webhook",
                task_id,
                attempt + 1,
                _defer_by=timedelta(seconds=delay),
            )
            bound_log.info("webhook.retry_scheduled", delay_seconds=delay, next_attempt=attempt + 1)
        else:
            await session.execute(
                update(Task)
                .where(Task.id == uuid.UUID(task_id))
                .values(webhook_status="failed")
            )
            await session.commit()
            bound_log.error(
                "webhook.failed",
                url=task.webhook_url,
                last_error=error_msg,
            )
