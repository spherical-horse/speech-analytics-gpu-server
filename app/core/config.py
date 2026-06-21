from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # DB
    DB_HOST: str = "postgres"
    DB_PORT: int = 5432
    DB_NAME: str = "transcript"
    DB_USER: str = "app"
    DB_PASSWORD: str = ""

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # HuggingFace
    HF_TOKEN: str = ""

    # Models
    WHISPER_MODEL: str = "large-v3-turbo"
    WHISPER_LANGUAGE: str = "ru"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_BATCH_SIZE: int = 32
    DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"
    MIN_SPEAKERS: int = 2
    MAX_SPEAKERS: int = 4

    # Limits
    MAX_FILE_SIZE_MB: int = 300
    MAX_DURATION_MIN: int = 180
    TASK_TIMEOUT_SECONDS: int = 2700
    TTL_HOURS: int = 24
    TMP_DIR: str = "/tmp/transcripts"
    UPLOADS_DIR: str = "/tmp/uploads"

    # Webhook
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    WEBHOOK_MAX_ATTEMPTS: int = 3
    WEBHOOK_BACKOFF_SECONDS: list[int] = [5, 30, 120]
    WEBHOOK_ALLOW_HTTP: bool = False

    # Logging
    LOG_LEVEL: str = "INFO"

    @field_validator("WEBHOOK_BACKOFF_SECONDS", mode="before")
    @classmethod
    def parse_backoff(cls, v: object) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",")]
        return v  # type: ignore[return-value]

    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        return f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def MAX_FILE_SIZE_BYTES(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def MAX_DURATION_SECONDS(self) -> float:
        return self.MAX_DURATION_MIN * 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
