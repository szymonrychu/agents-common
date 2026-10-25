import asyncio
import os
import signal
import time

import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from agents_common.db import open_pool
from agents_common.runtime import LostLock, advisory_singleton, run_service
from agents_common.settings import BaseServiceSettings, CsvList, SettingsError, load_settings


class WebhookSettings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", extra="ignore")

    hosts: CsvList = []


def _env(monkeypatch, **kw):
    for k in list(os.environ):
        if k.startswith(("WEBHOOK_", "OTHER_")):
            monkeypatch.delenv(k)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_settings_prefix_and_defaults(monkeypatch):
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="postgresql://x/y",
        WEBHOOK_ORCHESTRATOR_URL="http://orch",
        WEBHOOK_ORCHESTRATOR_TOKEN="tok",
        DATABASE_URL="ignored",
    )
    s = load_settings(WebhookSettings)
    assert s.database_url.get_secret_value() == "postgresql://x/y"
    assert (s.http_host, s.http_port, s.log_level, s.shutdown_grace_seconds) == (
        "0.0.0.0",
        8000,
        "INFO",
        20,
    )
    assert s.orchestrator_token.get_secret_value() == "tok"


def test_settings_token_file_wins(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text("  from-file\n")
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="postgresql://x/y",
        WEBHOOK_ORCHESTRATOR_URL="http://orch",
        WEBHOOK_ORCHESTRATOR_TOKEN="inline",
        WEBHOOK_ORCHESTRATOR_TOKEN_FILE=str(f),
    )
    assert load_settings(WebhookSettings).orchestrator_token.get_secret_value() == "from-file"


def test_settings_csv_list(monkeypatch):
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="u",
        WEBHOOK_ORCHESTRATOR_URL="http://o",
        WEBHOOK_ORCHESTRATOR_TOKEN="t",
        WEBHOOK_HOSTS=" a, ,b,,c ",
    )
    assert load_settings(WebhookSettings).hosts == ["a", "b", "c"]


def test_settings_errors_name_every_bad_variable_without_secrets(monkeypatch):
    _env(monkeypatch, WEBHOOK_HTTP_PORT="abc", WEBHOOK_ORCHESTRATOR_TOKEN="s3cr3t-value")
    with pytest.raises(SettingsError) as ei:
        load_settings(WebhookSettings)
    msg = str(ei.value)
    assert "WEBHOOK_DATABASE_URL: required" in msg
    assert "WEBHOOK_ORCHESTRATOR_URL: required" in msg
    assert "WEBHOOK_HTTP_PORT" in msg
    assert "s3cr3t-value" not in msg


def test_settings_missing_token_file_is_named(monkeypatch, tmp_path):
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="u",
        WEBHOOK_ORCHESTRATOR_URL="http://o",
        WEBHOOK_ORCHESTRATOR_TOKEN_FILE=str(tmp_path / "nope"),
    )
    with pytest.raises(SettingsError, match="WEBHOOK_ORCHESTRATOR_TOKEN_FILE"):
        load_settings(WebhookSettings)


def test_settings_repr_hides_secret(monkeypatch):
    _env(
        monkeypatch,
        WEBHOOK_DATABASE_URL="postgresql://u:pw-secret@h/d",
        WEBHOOK_ORCHESTRATOR_URL="http://o",
        WEBHOOK_ORCHESTRATOR_TOKEN="tok-secret",
    )
    s = load_settings(WebhookSettings)
    assert isinstance(s.orchestrator_token, SecretStr)
    assert "tok-secret" not in repr(s)
    assert "pw-secret" not in repr(s)


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
