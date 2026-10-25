import uuid

import httpx
import pytest

from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)

TOKEN = "tok-123"


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator(token=TOKEN)


def make_client(fake: FakeOrchestrator, token: str = TOKEN) -> OrchestratorClient:
    return OrchestratorClient("http://orch", token, transport=httpx.ASGITransport(app=fake.app))


async def test_roundtrip_all_methods(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        assert [p.id for p in await client.list_presets()] == [preset.id]
        got = await client.get_preset(preset.id)
        assert got.name == "default"

        cluster = await client.create_cluster(preset.id, "pr-1")
        assert cluster.slug == "pr-1"
        assert cluster.preset_id == preset.id
        assert (await client.get_cluster(cluster.id)).slug == "pr-1"

        accepted = await client.post_message(cluster.id, "hello")
        assert accepted.status == "queued"
        msgs = await client.list_messages(cluster.id)
        assert [m.id for m in msgs] == [accepted.id]
        assert msgs[0].text == "hello"

        msg = await client.get_message(accepted.id)
        assert msg.turn is None
        fake.complete_turn(accepted.id, "done!")
        msg = await client.get_message(accepted.id)
        assert msg.status == "sent"
        assert msg.turn is not None
        assert msg.turn.status == "completed"
        assert msg.turn.final_text == "done!"
        assert msg.turn.completed_at is not None


async def test_roundtrip_sends_bearer_header(fake):
    async with make_client(fake) as client:
        await client.list_presets()
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"


async def test_fail_turn(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        cluster = await client.create_cluster(preset.id, "x")
        m = await client.post_message(cluster.id, "t")
        fake.fail_turn(m.id)
        got = await client.get_message(m.id)
        assert got.status == "failed"


async def test_conflict_returns_existing_cluster(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        first = await client.create_cluster(preset.id, "same")
        second = await client.create_cluster(preset.id, "same")
    assert second.id == first.id
    assert len(fake.clusters) == 1


async def test_conflict_not_resolvable_raises(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        await client.create_cluster(preset.id, "same")
        fake.fail_next("create_cluster", 409)
        fake.clusters.clear()
        with pytest.raises(OrchestratorError) as ei:
            await client.create_cluster(preset.id, "same")
    assert ei.value.status == 409
    assert ei.value.code == "conflict"


async def test_error_401(fake):
    async with make_client(fake, token="wrong") as client:
        with pytest.raises(OrchestratorError) as ei:
            await client.list_presets()
    assert ei.value.status == 401
    assert ei.value.code == "unauthorized"
    assert not isinstance(ei.value, OrchestratorUnavailable)


async def test_error_404(fake):
    async with make_client(fake) as client:
        with pytest.raises(NotFound) as ei:
            await client.get_cluster(uuid.uuid4())
        assert ei.value.status == 404
        assert ei.value.code == "not_found"
        with pytest.raises(NotFound):
            await client.get_message(999)
        with pytest.raises(NotFound):
            await client.get_preset(uuid.uuid4())


async def test_error_5xx_is_unavailable(fake):
    fake.fail_next("list_presets", 503)
    async with make_client(fake) as client:
        with pytest.raises(OrchestratorUnavailable):
            await client.list_presets()
        assert await client.list_presets() == []


async def test_error_transport_is_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = OrchestratorClient("http://orch", TOKEN, transport=httpx.MockTransport(boom))
    async with client:
        with pytest.raises(OrchestratorUnavailable):
            await client.list_presets()


async def test_error_non_json_body(fake):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="nope")

    async with OrchestratorClient(
        "http://orch", TOKEN, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OrchestratorError) as ei:
            await client.list_presets()
    assert ei.value.status == 400


async def test_error_taken_slug_and_unknown_preset(fake):
    async with make_client(fake) as client:
        with pytest.raises(NotFound):
            await client.create_cluster(uuid.uuid4(), "x")
