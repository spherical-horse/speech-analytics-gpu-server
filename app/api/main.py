from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import settings
from app.core.redis import close_arq_pool, get_arq_pool

import logging
import structlog


def configure_logging() -> None:
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


def _redact_tokens(logger, method, event_dict: dict) -> dict:
    sensitive = {"token", "authorization", "bearer", "token_raw", "token_hash"}
    for key in list(event_dict.keys()):
        if key.lower() in sensitive:
            event_dict[key] = "[REDACTED]"
    return event_dict


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await get_arq_pool()
    yield
    await close_arq_pool()


app = FastAPI(
    title="Transcription & Diarization Service",
    version="2.0.0",
    description="Сервис транскрибации и диаризации звонков на базе WhisperX + pyannote.",
    lifespan=lifespan,
)


@app.middleware("http")
async def check_content_length(request: Request, call_next):
    if request.method == "POST" and request.url.path.endswith("/transcribe"):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.MAX_FILE_SIZE_BYTES:
            return JSONResponse(
                status_code=413,
                content={"error_code": "TOO_LARGE", "error_message": f"File exceeds {settings.MAX_FILE_SIZE_MB} MB limit"},
            )
    return await call_next(request)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log = structlog.get_logger()
    log.error("unhandled_exception", exc=str(exc))
    return JSONResponse(status_code=500, content={"error_code": "UNKNOWN_ERROR", "error_message": str(exc)})


app.include_router(router, prefix="/api/v1")
