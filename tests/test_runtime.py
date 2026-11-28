import asyncio
import os
import signal
import time

import pytest

from agents_common.db import open_pool
from agents_common.runtime import LostLock, advisory_singleton, run_service


async def test_singleton_only_one_holder_then_takeover(pg_url):
    pool = await open_pool(pg_url)
    events: list[str] = []
    release = asyncio.Event()

    async def holder(name: str, wait: asyncio.Event | None):
        async with advisory_singleton(pool, "k1", heartbeat_seconds=0.1):
            events.append(f"{name}-in")
            if wait:
                await wait.wait()
            else:
                await asyncio.sleep(0.1)
            events.append(f"{name}-out")

    a = asyncio.create_task(holder("a", release))
    await asyncio.sleep(0.3)
    b = asyncio.create_task(holder("b", None))
    await asyncio.sleep(0.5)
    assert events == ["a-in"]
    release.set()
    await asyncio.wait_for(asyncio.gather(a, b), 10)
    assert events == ["a-in", "a-out", "b-in", "b-out"]
    await pool.close()


async def test_singleton_lost_lock_raises(pg_url):
    import psycopg

    pool = await open_pool(pg_url)
    with pytest.raises(LostLock):
        async with advisory_singleton(pool, "k2", heartbeat_seconds=0.1):
            async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as c:
                await c.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' "
                    "AND granted AND pid <> pg_backend_pid()"
                )
            await asyncio.sleep(5)
    await pool.close()


async def test_run_service_stops_on_sigterm_within_grace():
    started = asyncio.Event()
    seen: dict[str, float] = {}

    async def main(stop: asyncio.Event) -> None:
        started.set()
        await stop.wait()
        seen["stopped"] = time.monotonic()

    task = asyncio.create_task(run_service(main, grace_seconds=2))
    await started.wait()
    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 5) == 0
    assert seen["stopped"] - t0 < 2


async def test_run_service_cancels_main_that_ignores_stop():
    started = asyncio.Event()

    async def main(stop: asyncio.Event) -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(run_service(main, grace_seconds=0.3))
    await started.wait()
    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert time.monotonic() - t0 < 2
