"""Crash-safe dispatch and tracking of one run (agents_common.runs) against the fake."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import NotFound, OrchestratorClient, OrchestratorUnavailable
from agents_common.runs import (
    PresetBinding,
    RunRef,
    RunStore,
    dispatch,
    marker,
    post_update,
    track,
)

TOKEN = "tok-runs"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MARK = "<!-- agents-webhook:event:1 -->"


class Crash(Exception):
    """Simulated process death between two steps."""


class MemoryStore:
    """In-memory RunStore: every write is guarded on the run being in-progress."""

    def __init__(self) -> None:
        self.rows: dict[Any, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.crash_on: str | None = None

    def add(self, run: RunRef) -> None:
        self.rows[run.id] = {
            "status": "in-progress",
            "session_id": run.session_id,
            "message_id": run.message_id,
            "reason": None,
        }

    def ref(self, run: RunRef) -> RunRef:
        row = self.rows[run.id]
        return dataclasses.replace(run, session_id=row["session_id"], message_id=row["message_id"])

    def _guard(self, name: str, run_id: Any) -> dict[str, Any] | None:
        if self.crash_on == name:
            raise Crash(name)
        self.calls.append((name, run_id))
        row = self.rows[run_id]
        return row if row["status"] == "in-progress" else None

    async def set_session(self, run_id: Any, session_id: str) -> None:
        row = self._guard("set_session", run_id)
        if row is not None and row["session_id"] is None:
            row["session_id"] = session_id

    async def set_message(self, run_id: Any, message_id: str) -> None:
        row = self._guard("set_message", run_id)
        if row is not None and row["message_id"] is None:
            row["message_id"] = message_id

    async def finish(self, run_id: Any, status: str, reason: str | None) -> bool:
        row = self._guard("finish", run_id)
        if row is None:
            return False
        row["status"], row["reason"] = status, reason
        return True


class CrashingClient:
    """Proxies an OrchestratorClient and raises Crash right after `after` returns."""

    def __init__(self, inner: OrchestratorClient, after: str):
        self._inner = inner
        self._after = after

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._inner, name)
        if name != self._after:
            return method

        async def crashing(*args: Any, **kwargs: Any) -> Any:
            await method(*args, **kwargs)
            raise Crash(name)

        return crashing


@pytest.fixture
def fake() -> FakeOrchestrator:
    return FakeOrchestrator(token=TOKEN)


@pytest.fixture
async def client(fake):
    async with OrchestratorClient(
        "http://orch", TOKEN, transport=httpx.ASGITransport(app=fake.app)
    ) as c:
        yield c


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def run(fake, store) -> RunRef:
    preset = fake.add_preset("default")
    ref = RunRef(
        id=1,
        slug="wh-1",
        preset_id=str(preset.id),
        session_id=None,
        message_id=None,
        started_at=T0,
    )
    store.add(ref)
    return ref


def posts(fake: FakeOrchestrator) -> list[Any]:
    return [r for r in fake.requests if r.method == "POST" and r.url.path.endswith("/messages")]


def creates(fake: FakeOrchestrator) -> list[Any]:
    return [r for r in fake.requests if r.method == "POST" and r.url.path == "/api/v1/sessions"]


def test_marker_format():
    assert marker("webhook", 7) == "<!-- agents-webhook:7 -->"
    assert marker("webhook:event", "7") == "<!-- agents-webhook:event:7 -->"


def test_preset_binding_and_store_protocol(store):
    binding = PresetBinding(preset_id="p-1", prompt_template=None)
    assert binding.preset_id == "p-1"
    assert binding.prompt_template is None
    assert PresetBinding(preset_id="p-2", prompt_template="do $event").prompt_template
    assert isinstance(store, RunStore)


async def test_dispatch_happy_path(fake, client, store, run):
    out = await dispatch(client, store, run, "do the thing", MARK)

    assert len(fake.sessions) == 1
    session = next(iter(fake.sessions.values()))
    assert session["slug"] == "wh-1"
    assert session["preset_id"] == run.preset_id
    assert out.session_id == session["id"]
    assert len(fake.messages) == 1
    msg = next(iter(fake.messages.values()))
    assert msg["text"] == "do the thing\n\n" + MARK
    assert msg["session_id"] == session["id"]
    assert out.message_id == str(msg["id"])
    assert store.ref(run) == out
    assert store.calls == [("set_session", 1), ("set_message", 1)]

    fake.complete_turn(msg["id"], "done")
    assert await track(client, store, out, T0 + timedelta(seconds=5), 3600) == "handled"
    assert store.rows[1]["status"] == "handled"
    assert store.rows[1]["reason"] is None


async def test_dispatch_again_is_a_noop_when_both_ids_are_stored(fake, client, store, run):
    out = await dispatch(client, store, run, "x", MARK)
    n = len(fake.requests)
    again = await dispatch(client, store, out, "x", MARK)
    assert again == out
    assert len(fake.requests) == n


async def test_crash_after_create_resumes_without_second_session(fake, client, store, run):
    with pytest.raises(Crash):
        await dispatch(CrashingClient(client, "create_session"), store, run, "x", MARK)
    assert len(fake.sessions) == 1
    assert store.rows[1]["session_id"] is None  # died before set_session

    out = await dispatch(client, store, store.ref(run), "x", MARK)

    assert len(fake.sessions) == 1  # 409 resolved the existing session by slug
    assert out.session_id == next(iter(fake.sessions))
    assert len(fake.messages) == 1


async def test_crash_after_set_session_resumes_without_second_session(fake, client, store, run):
    with pytest.raises(Crash):
        await dispatch(CrashingClient(client, "list_messages"), store, run, "x", MARK)
    stored = store.ref(run)
    assert stored.session_id is not None
    assert stored.message_id is None
    assert fake.messages == {}

    out = await dispatch(client, store, stored, "x", MARK)

    assert len(creates(fake)) == 1  # no create call on resume
    assert len(fake.sessions) == 1
    assert out.session_id == stored.session_id
    assert len(fake.messages) == 1


async def test_crash_after_post_adopts_the_marker_message(fake, client, store, run):
    with pytest.raises(Crash):
        await dispatch(CrashingClient(client, "post_message"), store, run, "x", MARK)
    assert len(fake.messages) == 1
    assert store.rows[1]["message_id"] is None
    n_posts = len(posts(fake))

    out = await dispatch(client, store, store.ref(run), "x", MARK)

    assert len(posts(fake)) == n_posts  # nothing posted on resume
    assert len(fake.messages) == 1
    assert out.message_id == str(next(iter(fake.messages)))
    assert store.rows[1]["message_id"] == out.message_id


async def test_crash_in_set_message_adopts_the_marker_message(fake, client, store, run):
    store.crash_on = "set_message"
    with pytest.raises(Crash):
        await dispatch(client, store, run, "x", MARK)
    store.crash_on = None
    assert len(fake.messages) == 1

    out = await dispatch(client, store, store.ref(run), "x", MARK)

    assert len(fake.messages) == 1
    assert len(posts(fake)) == 1
    assert out.message_id == str(next(iter(fake.messages)))


async def test_crash_resume_ignores_messages_without_the_marker(fake, client, store, run):
    session = await client.create_session(run.preset_id, run.slug)
    other = await client.post_message(session.id, "unrelated <!-- agents-webhook:event:11 -->")
    await store.set_session(run.id, str(session.id))

    out = await dispatch(client, store, store.ref(run), "x", MARK)

    assert out.message_id != str(other.id)
    assert fake.messages[int(out.message_id)]["text"].endswith(MARK)


async def test_dispatch_409_resolves_existing_session(fake, client, store, run):
    existing = await client.create_session(run.preset_id, "wh-1")

    out = await dispatch(client, store, run, "x", MARK)

    assert out.session_id == str(existing.id)
    assert len(fake.sessions) == 1
    assert store.rows[1]["session_id"] == str(existing.id)


async def test_dispatch_preset_not_found_fails_the_run(fake, client, store, run):
    gone = dataclasses.replace(run, preset_id="00000000-0000-0000-0000-000000000000")
    with pytest.raises(NotFound):
        await dispatch(client, store, gone, "x", MARK)
    assert store.rows[1]["status"] == "failed"
    assert store.rows[1]["reason"] == "preset_not_found"
    assert fake.sessions == {}


async def test_missing_session_fails_run(fake, client, store, run):
    lost = dataclasses.replace(run, session_id="00000000-0000-0000-0000-000000000000")
    store.rows[1]["session_id"] = lost.session_id
    with pytest.raises(NotFound):
        await dispatch(client, store, lost, "x", MARK)
    assert store.rows[1]["status"] == "failed"
    assert store.rows[1]["reason"] == "session_not_found"
    assert fake.messages == {}


async def test_dispatch_unavailable_propagates_and_leaves_the_run(fake, client, store, run):
    fake.fail_next("create_session", 503)
    with pytest.raises(OrchestratorUnavailable):
        await dispatch(client, store, run, "x", MARK)
    assert store.rows[1]["status"] == "in-progress"
    assert ("finish", 1) not in store.calls


async def _dispatched(client, store, run) -> RunRef:
    return await dispatch(client, store, run, "x", MARK)


async def test_track_completed_is_handled(fake, client, store, run):
    out = await _dispatched(client, store, run)
    fake.complete_turn(int(out.message_id), "ok")
    assert await track(client, store, out, T0, 60) == "handled"
    assert (store.rows[1]["status"], store.rows[1]["reason"]) == ("handled", None)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_track_message_failed_or_cancelled(fake, client, store, run, status):
    out = await _dispatched(client, store, run)
    fake.messages[int(out.message_id)]["status"] = status
    assert await track(client, store, out, T0, 60) == "failed"
    assert (store.rows[1]["status"], store.rows[1]["reason"]) == ("failed", f"message_{status}")


async def test_track_fake_fail_turn_is_message_failed(fake, client, store, run):
    out = await _dispatched(client, store, run)
    fake.fail_turn(int(out.message_id))
    assert await track(client, store, out, T0, 60) == "failed"
    assert store.rows[1]["reason"] == "message_failed"


async def test_track_turn_failed(fake, client, store, run):
    out = await _dispatched(client, store, run)
    msg = fake.messages[int(out.message_id)]
    msg["status"] = "sent"
    msg["turn"] = {"status": "failed", "started_at": T0.isoformat(), "completed_at": None}
    assert await track(client, store, out, T0, 60) == "failed"
    assert (store.rows[1]["status"], store.rows[1]["reason"]) == ("failed", "turn_failed")


async def test_track_timeout(fake, client, store, run):
    out = await _dispatched(client, store, run)
    assert await track(client, store, out, T0 + timedelta(seconds=60), 60) is None
    assert store.rows[1]["status"] == "in-progress"
    assert await track(client, store, out, T0 + timedelta(seconds=61), 60) == "failed"
    assert (store.rows[1]["status"], store.rows[1]["reason"]) == ("failed", "timeout")


async def test_track_still_running_returns_none(fake, client, store, run):
    out = await _dispatched(client, store, run)
    msg = fake.messages[int(out.message_id)]
    msg["status"] = "sent"
    msg["turn"] = {"status": "running", "started_at": T0.isoformat(), "completed_at": None}
    assert await track(client, store, out, T0 + timedelta(seconds=1), 60) is None
    assert store.rows[1]["status"] == "in-progress"
    assert ("finish", 1) not in store.calls


async def test_track_completed_wins_over_timeout(fake, client, store, run):
    out = await _dispatched(client, store, run)
    fake.complete_turn(int(out.message_id), "ok")
    assert await track(client, store, out, T0 + timedelta(hours=2), 60) == "handled"


async def test_track_unavailable_propagates(fake, client, store, run):
    out = await _dispatched(client, store, run)
    fake.fail_next("get_message", 503)
    with pytest.raises(OrchestratorUnavailable):
        await track(client, store, out, T0, 60)
    assert store.rows[1]["status"] == "in-progress"


async def test_track_message_gone_is_session_not_found(fake, client, store, run):
    out = await _dispatched(client, store, run)
    fake.messages.clear()
    fake.sessions.clear()
    assert await track(client, store, out, T0, 60) == "failed"
    assert (store.rows[1]["status"], store.rows[1]["reason"]) == ("failed", "session_not_found")


async def test_track_requires_a_message_id(client, store, run):
    with pytest.raises(ValueError):
        await track(client, store, run, T0, 60)


@pytest.fixture
async def parent(fake, client, store, run) -> RunRef:
    return await _dispatched(client, store, run)


def _update_run(store: MemoryStore, parent: RunRef) -> RunRef:
    ref = RunRef(
        id=2,
        slug=parent.slug,
        preset_id=parent.preset_id,
        session_id=None,
        message_id=None,
        started_at=T0,
    )
    store.add(ref)
    return ref


UPDATE_MARK = "<!-- agents-webhook:event:2 -->"


async def test_update_message_posts_to_the_existing_session(fake, client, store, parent):
    upd = _update_run(store, parent)

    out = await post_update(client, store, upd, parent.session_id, "new comment", UPDATE_MARK)

    assert len(fake.sessions) == 1
    assert len(creates(fake)) == 1  # only the parent's create
    assert len(fake.messages) == 2
    msg = fake.messages[int(out.message_id)]
    assert msg["session_id"] == parent.session_id
    assert msg["text"] == "new comment\n\n" + UPDATE_MARK
    assert store.rows[2]["message_id"] == out.message_id
    assert store.rows[2]["session_id"] == parent.session_id
    assert out.session_id == parent.session_id


async def test_update_message_crash_after_post_adopts(fake, client, store, parent):
    upd = _update_run(store, parent)
    with pytest.raises(Crash):
        await post_update(
            CrashingClient(client, "post_message"), store, upd, parent.session_id, "c", UPDATE_MARK
        )
    assert len(fake.messages) == 2
    n_posts = len(posts(fake))

    out = await post_update(client, store, store.ref(upd), parent.session_id, "c", UPDATE_MARK)

    assert len(posts(fake)) == n_posts
    assert len(fake.messages) == 2
    assert fake.messages[int(out.message_id)]["text"].endswith(UPDATE_MARK)
    assert out.message_id != parent.message_id


async def test_update_message_with_stored_id_posts_nothing(fake, client, store, parent):
    upd = _update_run(store, parent)
    out = await post_update(client, store, upd, parent.session_id, "c", UPDATE_MARK)
    n = len(fake.requests)
    assert await post_update(client, store, out, parent.session_id, "c", UPDATE_MARK) == out
    assert len(fake.requests) == n


async def test_update_message_session_gone_fails_the_run(fake, client, store, parent):
    upd = _update_run(store, parent)
    fake.sessions.clear()
    with pytest.raises(NotFound):
        await post_update(client, store, upd, parent.session_id, "c", UPDATE_MARK)
    assert (store.rows[2]["status"], store.rows[2]["reason"]) == ("failed", "session_not_found")
