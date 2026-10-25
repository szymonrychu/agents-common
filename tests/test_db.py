import asyncio
import shutil
import threading
import uuid

import psycopg
import pytest
from psycopg import sql

from agents_common.db import (
    MigrationError,
    claim,
    listen,
    notify,
    open_pool,
    run_migrations,
    transition,
)

PKG = "sample_pkg"


@pytest.fixture
def empty_url(pg_url):
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    base = pg_url.rsplit("/", 1)[0]
    yield f"{base}/{name}"
    with psycopg.connect(pg_url, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_migrations_apply_once_in_order_under_concurrency(empty_url):
    results: list[list[str]] = []

    def go():
        results.append(run_migrations(empty_url, PKG))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = sorted(results, key=len)
    assert names[0] == []
    assert names[1] == ["0001_items.sql", "0002_seed.sql"]
    with psycopg.connect(empty_url) as c:
        assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert run_migrations(empty_url, PKG) == []


def test_migrations_edited_applied_file_raises(empty_url, tmp_path, monkeypatch):
    pkg = tmp_path / "edpkg"
    shutil.copytree("tests/sample_pkg", pkg)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert run_migrations(empty_url, "edpkg") == ["0001_items.sql", "0002_seed.sql"]
    (pkg / "migrations" / "0002_seed.sql").write_text("INSERT INTO items (note) VALUES ('x');")
    with pytest.raises(MigrationError):
        run_migrations(empty_url, "edpkg")


def test_migrated_db_clones_template(migrated_db):
    url = migrated_db(PKG)
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 2


async def test_transition_only_from_expected_status(migrated_db):
    pool = await open_pool(migrated_db(PKG))
    try:
        async with pool.connection() as conn:
            assert await transition(conn, "items", 1, "queued", "done", note="x") is True
            assert await transition(conn, "items", 1, "queued", "failed") is False
            row = await (await conn.execute("SELECT status, note FROM items WHERE id=1")).fetchone()
            assert row == ("done", "x")
            with pytest.raises(ValueError):
                await transition(conn, "items; drop", 1, "queued", "done")
            with pytest.raises(ValueError):
                await transition(conn, "items", 1, "queued", "done", **{"note=1": "x"})
    finally:
        await pool.close()


async def test_claim_skip_locked_never_returns_same_row(migrated_db):
    pool = await open_pool(migrated_db(PKG))
    q = "SELECT id FROM items WHERE status=%s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
    try:
        async with pool.connection() as c1, pool.connection() as c2, c1.transaction():
            r1 = await claim(c1, q, ("queued",))
            async with c2.transaction():
                r2 = await claim(c2, q, ("queued",))
        assert r1["id"] != r2["id"]
    finally:
        await pool.close()


async def test_listen_receives_notify(pg_url):
    got = []

    async def consume():
        async for payload in listen(pg_url, "chan_x"):
            got.append(payload)
            return

    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(15):
            while not got:
                await asyncio.sleep(0.2)
                async with await psycopg.AsyncConnection.connect(pg_url, autocommit=True) as c:
                    await notify(c, "chan_x", "hello")
    finally:
        task.cancel()
    assert got == ["hello"]
