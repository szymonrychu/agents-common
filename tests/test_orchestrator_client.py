import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    UsageQuotaRefused,
)

TOKEN = "tok-123"
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


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

        session = await client.create_session(preset.id, "pr-1")
        assert session.slug == "pr-1"
        assert session.preset_id == preset.id
        assert (await client.get_session(session.id)).slug == "pr-1"

        accepted = await client.post_message(session.id, "hello")
        assert accepted.status == "queued"
        msgs = await client.list_messages(session.id)
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


async def test_message_command_outcome_roundtrip(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "cmd")
        turn = await client.post_message(session.id, "hello")
        ok = await client.post_message(session.id, "/compact")
        promoted = await client.post_message(session.id, "/review")
        failed = await client.post_message(session.id, "/nope")
        cancelled = await client.post_message(session.id, "/clear")

        fake.complete_turn(turn.id, "done")
        fake.complete_command(ok.id)
        fake.complete_command(promoted.id, result="turn", turn_id="turn-7")
        fake.fail_command(failed.id, "unknown_command")
        fake.fail_command(cancelled.id, None, state="cancelled")
        with pytest.raises(ValueError):
            fake.fail_command(failed.id, "x", state="completed")

        got_turn = await client.get_message(turn.id)
        assert got_turn.command is None

        got = await client.get_message(ok.id)
        assert got.status == "sent"
        assert got.command is not None
        assert (got.command.state, got.command.result, got.command.error) == (
            "completed",
            "ok",
            None,
        )
        assert got.command.turn_id is None
        assert got.command.at is not None

        got = await client.get_message(promoted.id)
        assert (got.command.state, got.command.result) == ("completed", "turn")
        assert got.command.turn_id == "turn-7"

        got = await client.get_message(failed.id)
        assert got.status == "sent"
        assert (got.command.state, got.command.result) == ("failed", None)
        assert got.command.error == "unknown_command"

        got = await client.get_message(cancelled.id)
        assert got.status == "sent"
        assert (got.command.state, got.command.error) == ("cancelled", None)

        listed = {m.id: m.command for m in await client.list_messages(session.id)}
        assert listed[turn.id] is None
        assert listed[failed.id].error == "unknown_command"


async def test_roundtrip_sends_bearer_header(fake):
    async with make_client(fake) as client:
        await client.list_presets()
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"


async def test_fail_turn(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "x")
        m = await client.post_message(session.id, "t")
        fake.fail_turn(m.id)
        got = await client.get_message(m.id)
        assert got.status == "failed"


async def test_conflict_returns_existing_session(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        first = await client.create_session(preset.id, "same")
        second = await client.create_session(preset.id, "same")
    assert second.id == first.id
    assert len(fake.sessions) == 1


async def test_conflict_not_resolvable_raises(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        await client.create_session(preset.id, "same")
        fake.fail_next("create_session", 409)
        fake.sessions.clear()
        with pytest.raises(OrchestratorError) as ei:
            await client.create_session(preset.id, "same")
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
            await client.get_session(uuid.uuid4())
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
            await client.create_session(uuid.uuid4(), "x")


SESSION_ROUTES = {
    "/api/v1/presets",
    "/api/v1/presets/{id}",
    "/api/v1/sessions",
    "/api/v1/sessions/{id}",
    "/api/v1/sessions/{id}/messages",
    "/api/v1/messages/{id}",
}


async def test_session_routes(fake):
    assert {route.path for route in fake.app.routes} == SESSION_ROUTES
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "s-1")
        assert (await client.create_session(preset.id, "s-1")).id == session.id
        assert (await client.get_session(session.id)).slug == "s-1"
        posted = await client.post_message(session.id, "hi")
        assert posted.session_id == session.id
        assert [m.session_id for m in await client.list_messages(session.id)] == [session.id]
    sid = str(session.id)
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ("POST", "/api/v1/sessions"),
        ("POST", "/api/v1/sessions"),
        ("GET", "/api/v1/sessions"),
        ("GET", f"/api/v1/sessions/{sid}"),
        ("POST", f"/api/v1/sessions/{sid}/messages"),
        ("GET", f"/api/v1/sessions/{sid}/messages"),
    ]
    assert set(fake.sessions) == {sid}


async def test_usage_quota_parsed(fake):
    preset = fake.add_preset("p")
    fake.refuse_usage(NOW + timedelta(hours=2))
    async with make_client(fake) as client:
        with pytest.raises(UsageQuotaRefused) as ei:
            await client.create_session(preset.id, "q")
    exc = ei.value
    assert exc.status == 409
    assert exc.code == "usage_quota"
    assert exc.metric == "seven_day"
    assert exc.percent == 90.0
    assert exc.limit == 80
    assert exc.resets_at == NOW + timedelta(hours=2)
    assert exc.retry_after is not None
    assert exc.retry_after > 0
    assert isinstance(exc, OrchestratorError)
    assert not isinstance(exc, NotFound)
    assert fake.sessions == {}


async def test_usage_quota_retry_after_falls_back_to_header():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"error": {"code": "usage_quota", "message": "over"}},
            headers={"Retry-After": "90"},
        )

    client = OrchestratorClient("http://orch", TOKEN, transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(UsageQuotaRefused) as ei:
            await client.create_session(uuid.uuid4(), "q")
    assert ei.value.retry_after == 90
    assert ei.value.metric is None
    assert ei.value.percent is None
    assert ei.value.limit is None
    assert ei.value.resets_at is None


async def test_plain_409_is_not_usage(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        await client.create_session(preset.id, "same")
        fake.fail_next("create_session", 409)
        fake.sessions.clear()
        with pytest.raises(OrchestratorError) as ei:
            await client.create_session(preset.id, "same")
    assert not isinstance(ei.value, UsageQuotaRefused)


def _refused(**kwargs) -> UsageQuotaRefused:
    return UsageQuotaRefused(409, "usage_quota", "over quota", **kwargs)


def test_retry_at_uses_resets_at():
    exc = _refused(resets_at=NOW + timedelta(hours=2), retry_after=7200)
    assert exc.retry_at(NOW) == NOW + timedelta(hours=2, seconds=60)


def test_retry_at_from_retry_after():
    assert _refused(retry_after=120).retry_at(NOW) == NOW + timedelta(seconds=180)


def test_retry_at_default():
    assert _refused().retry_at(NOW) == NOW + timedelta(seconds=360)


def test_retry_at_never_in_past():
    assert _refused(resets_at=NOW - timedelta(hours=1)).retry_at(NOW) == NOW + timedelta(seconds=60)


async def test_refuse_usage_times(fake):
    preset = fake.add_preset("p")
    fake.refuse_usage(NOW + timedelta(hours=1), times=2)
    async with make_client(fake) as client:
        for _ in range(2):
            with pytest.raises(UsageQuotaRefused):
                await client.create_session(preset.id, "q")
        assert fake.sessions == {}
        session = await client.create_session(preset.id, "q")
    assert session.slug == "q"
