from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import ApiToken


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def authenticate_request(request: Request, session: AsyncSession) -> ApiToken:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Missing Bearer token"})

    raw_token = auth_header.removeprefix("Bearer ").strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Empty Bearer token"})

    incoming_hash = hash_token(raw_token)

    result = await session.execute(select(ApiToken).where(ApiToken.token_hash == incoming_hash))
    token_record = result.scalar_one_or_none()

    if token_record is None:
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Invalid or expired bearer token"})

    # Timing-safe comparison (both sides are SHA-256 digests)
    if not hmac.compare_digest(token_record.token_hash, incoming_hash):
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Invalid or expired bearer token"})

    if not token_record.is_active:
        raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Invalid or expired bearer token"})

    if token_record.expires_at is not None:
        now = datetime.now(timezone.utc)
        expires = token_record.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            raise HTTPException(status_code=401, detail={"error_code": "UNAUTHORIZED", "error_message": "Invalid or expired bearer token"})

    return token_record
