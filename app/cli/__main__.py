from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import click
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.models import ApiToken


def get_engine():
    return create_async_engine(settings.DATABASE_URL, echo=False)


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _parse_duration(duration_str: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", duration_str.strip())
    if not match:
        raise click.BadParameter(f"Invalid duration '{duration_str}'. Use formats like 7d, 24h, 30m")
    value, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return timedelta(days=value)
    elif unit == "h":
        return timedelta(hours=value)
    else:
        return timedelta(minutes=value)


@click.group()
def cli():
    """Transcription service admin CLI."""


@cli.command()
@click.option("--name", required=True, help="Token name (identifier)")
@click.option("--expires-in", default=None, help="TTL: e.g. 7d, 24h, 30m. Omit for no expiry.")
def create_token(name: str, expires_in: str | None):
    """Create a new API token and print it once."""

    async def _run():
        raw_token = secrets.token_urlsafe(48)
        token_hash = _hash_token(raw_token)
        expires_at = None
        if expires_in:
            expires_at = datetime.now(timezone.utc) + _parse_duration(expires_in)

        engine = get_engine()
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            record = ApiToken(
                id=uuid.uuid4(),
                name=name,
                token_hash=token_hash,
                is_active=True,
                expires_at=expires_at,
            )
            session.add(record)
            await session.commit()
        await engine.dispose()
        return raw_token, expires_at

    raw_token, expires_at = asyncio.run(_run())
    click.echo(f"\nToken created: {name}")
    click.echo(f"Value (save this — shown only once):\n  {raw_token}")
    if expires_at:
        click.echo(f"Expires at: {expires_at.isoformat()}")
    else:
        click.echo("Expires: never")


@cli.command()
def list_tokens():
    """List all tokens (metadata only, no raw values)."""

    async def _run():
        engine = get_engine()
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(select(ApiToken).order_by(ApiToken.created_at))
            tokens = result.scalars().all()
        await engine.dispose()
        return tokens

    tokens = asyncio.run(_run())
    if not tokens:
        click.echo("No tokens found.")
        return

    click.echo(f"\n{'ID':<36}  {'Name':<20}  {'Active':<6}  {'Created':<25}  {'Expires'}")
    click.echo("-" * 110)
    for t in tokens:
        expires = t.expires_at.isoformat() if t.expires_at else "never"
        click.echo(f"{t.id!s:<36}  {t.name:<20}  {'yes' if t.is_active else 'no':<6}  {t.created_at.isoformat():<25}  {expires}")


@cli.command()
@click.option("--name", required=True, help="Token name to revoke")
def revoke_token(name: str):
    """Revoke a token by name."""

    async def _run():
        engine = get_engine()
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                update(ApiToken).where(ApiToken.name == name).values(is_active=False).returning(ApiToken.id)
            )
            rows = result.fetchall()
            await session.commit()
        await engine.dispose()
        return len(rows)

    count = asyncio.run(_run())
    if count:
        click.echo(f"Revoked {count} token(s) with name '{name}'.")
    else:
        click.echo(f"No token found with name '{name}'.")


@cli.command()
def revoke_all():
    """Revoke all tokens."""

    async def _run():
        engine = get_engine()
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                update(ApiToken).where(ApiToken.is_active.is_(True)).values(is_active=False).returning(ApiToken.id)
            )
            count = len(result.fetchall())
            await session.commit()
        await engine.dispose()
        return count

    count = asyncio.run(_run())
    click.echo(f"Revoked {count} token(s).")


if __name__ == "__main__":
    cli()
