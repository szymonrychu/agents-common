import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import (
    Attachment,
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
    Preset,
    Project,
    Session,
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
    "/api/v1/projects",
    "/api/v1/projects/{slug}",
    "/api/v1/sessions",
    "/api/v1/sessions/{id}",
    "/api/v1/sessions/{id}/agent-token/verify",
    "/api/v1/sessions/{id}/messages",
    "/api/v1/sessions/{id}/files/download",
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


def test_attachment_to_json():
    att = Attachment("a.png", "image/png", b"\x89PNG")
    assert att.to_json() == {"name": "a.png", "mime": "image/png", "data_b64": "iVBORw=="}


async def test_post_message_with_attachments(fake):
    preset = fake.add_preset("default", is_default=True)
    att = Attachment("a.png", "image/png", b"\x89PNG")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        posted = await client.post_message(session.id, "see", attachments=[att])
        assert fake.attachments[posted.id] == [att]
        meta = {
            "name": "a.png",
            "mime": "image/png",
            "size": 4,
            "path": ".attachments/orch-1/a.png",
        }
        assert fake.messages[posted.id]["attachments"] == [meta]
        got = await client.get_message(posted.id)
        assert [a.model_dump() for a in got.attachments] == [meta]
        listed = await client.list_messages(session.id)
        assert [a.model_dump() for a in listed[0].attachments] == [meta]


async def test_post_message_without_attachments_sends_no_key(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        posted = await client.post_message(session.id, "plain")
        await client.post_message(session.id, "empty", attachments=[])
        got = await client.get_message(posted.id)
    assert got.attachments == []
    for req in fake.requests[-3:-1]:
        assert "attachments" not in await req.json()


async def test_post_message_invalid_base64_is_422(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        resp = await client._http.post(
            f"/api/v1/sessions/{session.id}/messages",
            json={
                "text": "x",
                "attachments": [{"name": "a.png", "mime": "image/png", "data_b64": "!!"}],
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_attachment"


async def test_download_file_returns_bytes(fake):
    preset = fake.add_preset("default", is_default=True)
    data = bytes(range(256))
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        fake.put_file(str(session.id), "out/a.bin", data)
        assert await client.download_file(session.id, "out/a.bin") == data
    assert fake.requests[-1].query_params["path"] == "out/a.bin"
    assert fake.downloads == [(str(session.id), "out/a.bin", None)]


async def test_download_file_passes_agent_id(fake):
    preset = fake.add_preset("default", is_default=True)
    agent_id = uuid.uuid4()
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        fake.put_file(str(session.id), "a.txt", b"hi")
        await client.download_file(session.id, "a.txt", agent_id)
    assert fake.downloads == [(str(session.id), "a.txt", str(agent_id))]


async def test_download_file_missing_is_not_found(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        with pytest.raises(NotFound):
            await client.download_file(session.id, "nope.txt")


async def test_download_file_over_cap_raises_413(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        fake.put_file(str(session.id), "a.bin", b"abcd")
        with pytest.raises(OrchestratorError) as info:
            await client.download_file(session.id, "a.bin", max_bytes=3)
    assert (info.value.status, info.value.code) == (413, "too_large")


async def test_download_file_growing_body_raises_413():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(10):
                yield b"abcd"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Chunks())

    client = OrchestratorClient("http://orch", TOKEN, transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(OrchestratorError) as info:
            await client.download_file("s", "a.bin", max_bytes=10)
    assert info.value.status == 413


async def test_download_file_5xx_unavailable(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "pr-1")
        fake.put_file(str(session.id), "a.txt", b"hi")
        fake.fail_next("download_file", 503)
        with pytest.raises(OrchestratorUnavailable):
            await client.download_file(session.id, "a.txt")
        assert await client.download_file(session.id, "a.txt") == b"hi"


async def test_list_sessions_parses(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        assert await client.list_sessions() == []
        await client.create_session(preset.id, "s-a")
        await client.create_session(preset.id, "s-b")
        sessions = await client.list_sessions()
    assert sorted(s.slug for s in sessions) == ["s-a", "s-b"]


async def test_discard_session_returns_session_and_records_call(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "s-del")
        discarded = await client.discard_session(session.id)
        assert discarded.id == session.id
        assert discarded.desired == "deleting"
        assert await client.discard_session(session.id) is not None
    deletes = [r for r in fake.requests if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == [f"/api/v1/sessions/{session.id}"] * 2
    assert fake.discarded == [str(session.id), str(session.id)]


async def test_discard_session_unknown_is_not_found(fake):
    async with make_client(fake) as client:
        with pytest.raises(NotFound):
            await client.discard_session(uuid.uuid4())
    assert fake.discarded == []


async def test_discard_session_5xx_unavailable(fake):
    preset = fake.add_preset("default", is_default=True)
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "s-del")
        fake.fail_next("discard_session", 503)
        with pytest.raises(OrchestratorUnavailable):
            await client.discard_session(session.id)
        assert (await client.discard_session(session.id)).id == session.id


async def new_session(fake: FakeOrchestrator) -> str:
    preset = fake.add_preset("p")
    async with make_client(fake) as c:
        return str((await c.create_session(preset.id, "s")).id)


async def test_verify_agent_token_ok(fake):
    sid = await new_session(fake)
    fake.set_agent_token(sid, "s3cr3t-value")
    async with make_client(fake) as c:
        assert await c.verify_agent_token(sid, "s3cr3t-value") == uuid.UUID(sid)
    body = [r for r in fake.requests if r.url.path.endswith("/agent-token/verify")]
    assert len(body) == 1 and body[0].method == "POST" and "s3cr3t-value" not in str(body[0].url)


async def test_verify_agent_token_refusal_is_none(fake):
    sid = await new_session(fake)
    fake.set_agent_token(sid, "s3cr3t-value")
    async with make_client(fake) as c:
        assert await c.verify_agent_token(sid, "wrong") is None
        assert await c.verify_agent_token(str(uuid.uuid4()), "s3cr3t-value") is None


async def test_verify_agent_token_returns_echoed_id(fake):
    sid, other = await new_session(fake), str(uuid.uuid4())
    fake.set_agent_token(sid, "s3cr3t-value", answer_id=other)
    async with make_client(fake) as c:
        assert await c.verify_agent_token(sid, "s3cr3t-value") == uuid.UUID(other)


async def test_verify_agent_token_other_statuses_raise(fake):
    sid = await new_session(fake)
    fake.set_agent_token(sid, "s3cr3t-value")
    async with make_client(fake) as c:
        fake.fail_next("verify_agent_token", 401)
        with pytest.raises(OrchestratorError) as err:
            await c.verify_agent_token(sid, "s3cr3t-value")
        assert "s3cr3t-value" not in str(err.value)
        fake.fail_next("verify_agent_token", 503)
        with pytest.raises(OrchestratorUnavailable):
            await c.verify_agent_token(sid, "s3cr3t-value")


async def test_preset_labels_roundtrip_and_default(fake):
    fake.add_preset("p", labels=["a"])
    plain = fake.add_preset("q")
    async with make_client(fake) as client:
        presets = {p.name: p for p in await client.list_presets()}
    assert presets["p"].labels == ["a"]
    assert presets["q"].labels == []
    assert plain.name == "q"
    assert Preset.model_validate({"id": str(uuid.uuid4()), "name": "x"}).labels == []


async def test_create_session_sends_project(fake):
    fake.add_project("alpha", "Alpha")
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "s1", project="alpha")
        assert session.project == "alpha"
        assert (await client.create_session(preset.id, "s2")).project == "default"
    assert [s["project"] for s in fake.sessions.values()] == ["alpha", "default"]


async def test_conflict_adopts_same_project_by_slug(fake):
    fake.add_project("alpha", "Alpha")
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        first = await client.create_session(preset.id, "same", project="alpha")
        second = await client.create_session(preset.id, "same", project="alpha")
    assert second.id == first.id
    assert fake.requests[-1].url.path == "/api/v1/sessions"
    assert fake.requests[-1].url.query == "slug=same"


async def test_conflict_other_project_reraises(fake):
    fake.add_project("alpha", "Alpha")
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        await client.create_session(preset.id, "same")
        with pytest.raises(OrchestratorError) as ei:
            await client.create_session(preset.id, "same", project="alpha")
    assert (ei.value.status, ei.value.code) == (409, "conflict")


async def test_create_session_unknown_and_archived_project(fake):
    fake.add_project("old", "Old", archived=True)
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        with pytest.raises(OrchestratorError) as ei:
            await client.create_session(preset.id, "a", project="nope")
        assert (ei.value.status, ei.value.code) == (422, "unknown_project")
        with pytest.raises(OrchestratorError) as ei:
            await client.create_session(preset.id, "b", project="old")
        assert (ei.value.status, ei.value.code) == (422, "project_archived")


async def test_list_and_get_project(fake):
    fake.add_project("alpha", "Alpha", description="d")
    fake.add_project("old", "Old", archived=True)
    async with make_client(fake) as client:
        assert [p.slug for p in await client.list_projects()] == ["default", "alpha"]
        assert await client.get_project("alpha") == Project(
            slug="alpha", name="Alpha", description="d", archived=False
        )
        assert (await client.get_project("old")).archived is True


async def test_get_project_404_not_found(fake):
    async with make_client(fake) as client:
        with pytest.raises(NotFound):
            await client.get_project("missing")


async def test_preset_project_defaults(fake):
    fake.add_project("alpha", "Alpha")
    fake.add_preset("a", project="alpha")
    fake.add_preset("b")
    async with make_client(fake) as client:
        assert {p.name: p.project for p in await client.list_presets()} == {
            "a": "alpha",
            "b": "default",
        }


async def _new_session(fake, client):
    preset = fake.add_preset("default", is_default=True)
    return await client.create_session(preset.id, "ready-1")


async def test_new_session_is_booting(fake):
    async with make_client(fake) as client:
        created = await _new_session(fake, client)
        session = await client.get_session(created.id)
    assert session.summary.start_state == "booting"
    assert session.summary.ready == 0
    assert session.summary.ready_with_session == 0
    assert not session.is_up


async def test_session_ready_summary_parsed(fake):
    async with make_client(fake) as client:
        created = await _new_session(fake, client)
        fake.set_session_ready(str(created.id))
        session = await client.get_session(created.id)
    assert session.summary.ready == 1
    assert session.summary.start_state == "ready"
    assert session.summary.ready_with_session == 1
    assert session.is_up


async def test_ready_pod_without_wrapper_session_is_not_up(fake):
    async with make_client(fake) as client:
        created = await _new_session(fake, client)
        fake.set_session_ready(str(created.id), ready=1, ready_with_session=0)
        session = await client.get_session(created.id)
    assert session.summary.ready == 1
    assert session.summary.start_state == "ready"
    assert session.summary.ready_with_session == 0
    assert not session.is_up


def test_summary_without_ready_with_session_is_not_up():
    session = Session.model_validate(
        {
            "id": str(uuid.uuid4()),
            "slug": "s",
            "desired": "active",
            "summary": {"ready": 1, "start_state": "ready"},
        }
    )
    assert session.summary.ready_with_session == 0
    assert not session.is_up


async def test_bootstrap_failed_is_not_up(fake):
    async with make_client(fake) as client:
        created = await _new_session(fake, client)
        fake.set_session_ready(str(created.id), ready=1, start_state="bootstrap_failed")
        session = await client.get_session(created.id)
    assert not session.is_up


async def test_deleting_session_is_not_up(fake):
    async with make_client(fake) as client:
        created = await _new_session(fake, client)
        fake.set_session_ready(str(created.id))
        fake.discard_session(str(created.id))
        session = await client.get_session(created.id)
    assert session.desired == "deleting"
    assert not session.is_up


def test_session_without_summary_is_not_up():
    session = Session.model_validate({"id": str(uuid.uuid4()), "slug": "s"})
    assert session.summary is None
    assert not session.is_up


async def test_post_message_sends_from_user_only_when_set(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "fu")
        omitted = await client.post_message(session.id, "a")
        yes = await client.post_message(session.id, "b", from_user=True)
        no = await client.post_message(session.id, "c", from_user=False)
    assert fake.messages[omitted.id]["from_user"] is None
    assert fake.messages[yes.id]["from_user"] is True
    assert fake.messages[no.id]["from_user"] is False


async def test_fake_rejects_non_bool_from_user(fake):
    preset = fake.add_preset("p")
    async with make_client(fake) as client:
        session = await client.create_session(preset.id, "fu2")
        resp = await client._http.post(
            f"/api/v1/sessions/{session.id}/messages",
            json={"text": "x", "from_user": "yes"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
