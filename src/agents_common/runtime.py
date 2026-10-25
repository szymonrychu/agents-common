"""Worker runtime: a Postgres advisory-lock singleton and a signal-aware service runner."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable

import psycopg
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

EXIT_LOCK_LOST = 3


class LostLock(RuntimeError):
    """The advisory lock connection dropped or no longer owns the lock."""


def lock_key(name: str) -> int:
    """Stable signed 64-bit key for a lock name."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


async def _owns(conn: psycopg.AsyncConnection, key: int) -> bool:
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = "
        "pg_backend_pid() AND granted AND ((classid::bigint << 32) | objid::bigint) = %s)",
        (key,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _heartbeat(
    conn: psycopg.AsyncConnection, key: int, interval: float, host: asyncio.Task, state: dict
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            ok = await asyncio.wait_for(_owns(conn, key), max(interval * 3, 1))
        except (psycopg.Error, OSError, TimeoutError) as exc:
            log.error("singleton heartbeat failed: %s: %s", type(exc).__name__, exc)
            ok = False
        if not ok:
            state["lost"] = True
            host.cancel()
            return


@contextlib.asynccontextmanager
async def advisory_singleton(
    pool: AsyncConnectionPool, key: str, heartbeat_seconds: float = 10
) -> AsyncIterator[None]:
    """Hold a session advisory lock on a dedicated pool connection; wait while another holds it.

    A heartbeat re-checks ownership; if the connection drops the body is cancelled and
    LostLock is raised from the `async with`.
    """
    num = lock_key(key)
    conn = await pool.getconn()
    state = {"lost": False}
    hb: asyncio.Task | None = None
    host = asyncio.current_task()
    assert host is not None
    try:
        await conn.set_autocommit(True)
        poll = min(0.2, max(heartbeat_seconds, 0.01))
        while True:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (num,))
            row = await cur.fetchone()
            if row and row[0]:
                break
            await asyncio.sleep(poll)
        hb = asyncio.create_task(_heartbeat(conn, num, heartbeat_seconds, host, state))
        try:
            yield
        except asyncio.CancelledError:
            if state["lost"]:
                host.uncancel()
                raise LostLock(f"advisory lock {key!r} lost") from None
            raise
        else:
            if state["lost"]:
                raise LostLock(f"advisory lock {key!r} lost")
    finally:
        if hb is not None:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
        broken = state["lost"] or conn.closed or conn.broken
        if not broken:
            with contextlib.suppress(Exception):
                await conn.execute("SELECT pg_advisory_unlock(%s)", (num,))
        if broken:
            with contextlib.suppress(Exception):
                await conn.close()
        await pool.putconn(conn)


async def run_service(
    main: Callable[[asyncio.Event], Awaitable[None]], *, grace_seconds: float
) -> int:
    """Run `main(stop)` until it ends or SIGTERM/SIGINT sets `stop`.

    After a signal main gets `grace_seconds` to return, then it is cancelled.
    Returns 0 on a clean stop, EXIT_LOCK_LOST when LostLock escaped, 1 on any other failure.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        task = asyncio.create_task(main(stop), name="service")
        stop_wait = asyncio.create_task(stop.wait())
        await asyncio.wait({task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        stop_wait.cancel()
        stop.set()
        try:
            async with asyncio.timeout(grace_seconds):
                await asyncio.shield(task)
        except TimeoutError:
            log.warning("service did not stop within %.1fs; cancelling", grace_seconds)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return 0
    except LostLock:
        log.error("advisory lock lost")
        return EXIT_LOCK_LOST
    except Exception:
        log.exception("service failed")
        return 1
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
