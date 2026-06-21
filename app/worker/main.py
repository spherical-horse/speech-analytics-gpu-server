from __future__ import annotations

import logging

import structlog
from arq import cron
from arq.connections import RedisSettings

from app.core.config import settings
from app.core.redis import get_redis_settings
from app.worker.tasks import cleanup_expired_tasks, transcribe_task
from app.worker.webhook import deliver_webhook


def configure_logging() -> None:
    def _redact_tokens(logger, method, event_dict: dict) -> dict:
        sensitive = {"token", "authorization", "bearer", "token_raw", "token_hash"}
        for key in list(event_dict.keys()):
            if key.lower() in sensitive:
                event_dict[key] = "[REDACTED]"
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact_tokens,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(settings.LOG_LEVEL)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


log = structlog.get_logger()


async def startup(ctx: dict) -> None:
    configure_logging()
    log.info("worker_starting")

    from app.core.db import AsyncSessionLocal
    from app.worker.pipeline import load_models

    ctx["db_session_factory"] = AsyncSessionLocal

    import asyncio

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, load_models)
    log.info("worker_ready")


async def shutdown(ctx: dict) -> None:
    log.info("worker_shutting_down")
    from app.core.redis import close_arq_pool

    await close_arq_pool()


class WorkerSettings:
    functions = [transcribe_task, deliver_webhook, cleanup_expired_tasks]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = get_redis_settings()
    max_jobs = 1
    job_timeout = settings.TASK_TIMEOUT_SECONDS
    keep_result = 3600
    retry_jobs = True
    max_tries = 2
    cron_jobs = [
        cron(cleanup_expired_tasks, minute={0, 15, 30, 45}),
    ]
