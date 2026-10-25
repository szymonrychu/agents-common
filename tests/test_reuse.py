"""Reuse of a running agent: preset flag, running_cluster, deliver, track_delivery."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import Cluster, OrchestratorClient, OrchestratorUnavailable, Preset
from agents_common.runs import RunRef, deliver, running_cluster, track_delivery

TOKEN = "tok-reuse"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MARK = "<!-- agents-webhook:event:7 -->"


class Crash(Exception):
    pass


class MemoryStore:
    def __init__(self, run: RunRef) -> None:
        self.row: dict[str, Any] = {
            "status": "in-progress",
            "cluster_id": run.cluster_id,
            "message_id": run.message_id,
            "reason": None,
        }
        self.crash_on: str | None = None

    async def set_cluster(self, run_id: Any, cluster_id: str) -> None:
        self.row["cluster_id"] = cluster_id

    async def set_message(self, run_id: Any, message_id: str) -> None:
        if self.crash_on == "set_message":
            raise Crash
        self.row["message_id"] = message_id

    async def finish(self, run_id: Any, status: str, reason: str | None) -> bool:
        self.row["status"], self.row["reason"] = status, reason
        return True


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator(token=TOKEN)


@pytest.fixture
async def client(fake):
    async with OrchestratorClient(
        "http://orch", TOKEN, transport=httpx.ASGITransport(app=fake.app)
    ) as c:
        yield c


def _run(**kw: Any) -> RunRef:
    base: dict[str, Any] = {
        "id": 1,
        "slug": "event-7",
        "preset_id": "p",
        "cluster_id": None,
        "message_id": None,
        "started_at": T0,
    }
    return RunRef(**{**base, **kw})


async def _cluster(client, fake) -> str:
    preset = fake.add_preset("p")
    return str((await client.create_cluster(preset.id, "event-7")).id)


def test_preset_reuse_flag():
    pid = uuid.uuid4()
    assert Preset(id=pid, name="a", spec={"reuse_running_agent": True}).reuse_running_agent is True
    assert (
        Preset(id=pid, name="a", spec={"reuse_running_agent": False}).reuse_running_agent is False
    )
    assert (
        Preset(id=pid, name="a", spec={"reuse_running_agent": "yes"}).reuse_running_agent is False
    )
    assert Preset(id=pid, name="a").reuse_running_agent is False
    assert Cluster(id=pid, slug="s").status is None


async def test_running_cluster_states(client, fake):
    cid = await _cluster(client, fake)
    found = await running_cluster(client, cid)
    assert found is not None and str(found.id) == cid
    fake.discard_cluster(cid)
    assert await running_cluster(client, cid) is None
    fake.remove_cluster(cid)
    assert await running_cluster(client, cid) is None
    fake.fail_next("get_cluster", 503)
    with pytest.raises(OrchestratorUnavailable):
        await running_cluster(client, cid)


async def test_deliver_posts_once_and_adopts(client, fake):
    cid = await _cluster(client, fake)
    run = _run()
    store = MemoryStore(run)
    store.crash_on = "set_message"
    with pytest.raises(Crash):
        await deliver(client, store, run, cid, "hi", MARK)
    assert len(fake.messages) == 1
    store.crash_on = None
    run = dataclasses.replace(run, cluster_id=store.row["cluster_id"])
    out = await deliver(client, store, run, cid, "hi", MARK)
    assert out is not None and out.message_id == "1" and out.cluster_id == cid
    assert len(fake.messages) == 1
    assert MARK in fake.messages[1]["text"]
    assert store.row["status"] == "in-progress"


async def test_deliver_gone_returns_none(client, fake):
    cid = await _cluster(client, fake)
    run = _run()
    store = MemoryStore(run)
    fake.discard_cluster(cid)
    assert await deliver(client, store, run, cid, "hi", MARK) is None
    assert store.row["status"] == "in-progress"
    fake.remove_cluster(cid)
    assert await deliver(client, store, run, cid, "hi", MARK) is None
    assert store.row["status"] == "in-progress"
    cid2 = await _cluster(client, fake)
    fake.fail_next("post_message", 410)
    assert await deliver(client, store, run, cid2, "hi", MARK) is None
    assert store.row["status"] == "in-progress"
    fake.fail_next("post_message", 503)
    with pytest.raises(OrchestratorUnavailable):
        await deliver(client, store, run, cid2, "hi", MARK)


async def test_track_delivery_outcomes(client, fake):
    cid = await _cluster(client, fake)
    run = _run()
    store = MemoryStore(run)
    sent = await deliver(client, store, run, cid, "a", MARK)
    assert sent is not None
    assert await track_delivery(client, store, sent, timeout_seconds=60, now=T0) is None
    assert store.row["status"] == "in-progress"
    fake.mark_sent(1)
    assert await track_delivery(client, store, sent, timeout_seconds=60, now=T0) == "handled"
    assert store.row["status"] == "handled"

    # cancelled after discard -> gone, run left open
    store2 = MemoryStore(run)
    fake.discard_cluster(cid)
    assert fake.messages[1]["status"] == "sent"  # sent messages are untouched
    cid2 = await _cluster_named(client, fake, "event-8")
    m2 = await deliver(client, store2, run, cid2, "b", MARK)
    fake.discard_cluster(cid2)
    assert await track_delivery(client, store2, m2, timeout_seconds=60, now=T0) == "gone"
    assert store2.row["status"] == "in-progress"

    # failed
    cid3 = await _cluster_named(client, fake, "event-9")
    m3 = await deliver(client, store2, run, cid3, "c", MARK)
    fake.fail_turn(int(m3.message_id))
    assert await track_delivery(client, store2, m3, timeout_seconds=60, now=T0) == "gone"
    assert store2.row["status"] == "in-progress"

    # message gone
    fake.remove_cluster(cid3)
    assert await track_delivery(client, store2, m3, timeout_seconds=60, now=T0) == "gone"

    # timeout while still queued
    cid4 = await _cluster_named(client, fake, "event-10")
    m4 = await deliver(client, store2, run, cid4, "d", MARK)
    late = T0 + timedelta(seconds=61)
    assert await track_delivery(client, store2, m4, timeout_seconds=60, now=late) is None
    assert store2.row["status"] == "failed" and store2.row["reason"] == "timeout"


async def _cluster_named(client, fake, slug: str) -> str:
    preset = next(iter(fake.presets))
    return str((await client.create_cluster(preset, slug)).id)


async def test_fake_discard_and_sent(client, fake):
    preset = fake.add_preset("p", is_default=True, spec={"reuse_running_agent": True})
    assert (await client.get_preset(preset.id)).reuse_running_agent is True
    cid = str((await client.create_cluster(preset.id, "s")).id)
    mid = (await client.post_message(cid, "x")).id
    fake.mark_sent(mid)
    assert (await client.get_message(mid)).status == "sent"
    mid2 = (await client.post_message(cid, "y")).id
    fake.discard_cluster(cid)
    assert (await client.get_cluster(cid)).desired == "deleting"
    assert (await client.get_message(mid2)).status == "cancelled"
    assert (await client.get_message(mid)).status == "sent"
    from agents_common.orchestrator import NotFound, OrchestratorError

    with pytest.raises(OrchestratorError) as err:
        await client.post_message(cid, "z")
    assert err.value.status == 409 and err.value.code == "conflict"
    fake.remove_cluster(cid)
    with pytest.raises(NotFound):
        await client.get_cluster(cid)
