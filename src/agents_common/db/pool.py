"""Connection pool, NOTIFY helper and a reconnecting LISTEN stream."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


async def open_pool(url: str, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(url, min_size=min_size, max_size=max_size, open=False)
    await pool.open(wait=True)
    return pool


async def notify(conn: psycopg.AsyncConnection, channel: str, payload: str = "") -> None:
    await conn.execute("SELECT pg_notify(%s, %s)", (channel, payload))


async def listen(
    url: str,
    channel: str,
    *,
    initial_backoff: float = 0.5,
    max_backoff: float = 30.0,
) -> AsyncIterator[str]:
    """Yield notification payloads on a dedicated connection, reconnecting with backoff."""
    backoff = initial_backoff
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                backoff = initial_backoff
                async for n in conn.notifies():
                    yield n.payload
        except (psycopg.OperationalError, OSError) as e:
            log.warning("listen connection lost (%s); retrying in %.1fs", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
