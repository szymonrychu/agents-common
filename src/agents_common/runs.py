"""Idempotent dispatch and tracking of one triggered run (design D8).

A run owns one agents-orchestrator session (deterministic slug) and one prompt message carrying
a marker. agents-orchestrator has no idempotency key, so every step is skipped when its id is
already stored and a message carrying the marker is adopted instead of posting another: calling
`dispatch` again after a crash at any point creates no second session and posts no second message.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    Session,
)

RunStatus = Literal["handled", "failed"]
RunId = int | str


@dataclass(frozen=True)
class PresetBinding:
    """What a trigger maps to: a preset and an optional prompt template."""

    preset_id: str
    prompt_template: str | None = None


@dataclass(frozen=True)
class RunRef:
    """The run columns a service stores."""

    id: RunId
    slug: str
    preset_id: str
    session_id: str | None
    message_id: str | None
    started_at: datetime


@runtime_checkable
class RunStore(Protocol):
    """Writes of one run; each is a status-guarded update on an in-progress row."""

    async def set_session(self, run_id: RunId, session_id: str) -> None: ...

    async def set_message(self, run_id: RunId, message_id: str) -> None: ...

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool: ...


def marker(run_kind: str, run_id: RunId) -> str:
    return f"<!-- agents-{run_kind}:{run_id} -->"


async def dispatch(
    client: OrchestratorClient, store: RunStore, run: RunRef, text: str, marker: str
) -> RunRef:
    """Create the run's session and post its prompt, each step at most once.

    A missing preset or session finishes the run failed (`preset_not_found`,
    `session_not_found`) and re-raises the NotFound; other agents-orchestrator errors propagate.
    """
    session_id = run.session_id
    if session_id is None:
        try:
            session = await client.create_session(run.preset_id, run.slug)
        except NotFound:
            await store.finish(run.id, "failed", "preset_not_found")
            raise
        session_id = str(session.id)
        await store.set_session(run.id, session_id)
        run = dataclasses.replace(run, session_id=session_id)
    return await _post_once(client, store, run, session_id, text, marker)


async def post_update(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    session_id: str,
    text: str,
    marker: str,
) -> RunRef:
    """Post `text` to an existing session with the same adopt-before-post rule."""
    if run.session_id is None:
        await store.set_session(run.id, session_id)
        run = dataclasses.replace(run, session_id=session_id)
    return await _post_once(client, store, run, session_id, text, marker)


async def running_session(client: OrchestratorClient, session_id: str) -> Session | None:
    """The session while it exists with desired `active`; None when gone or being deleted.

    OrchestratorUnavailable propagates.
    """
    try:
        session = await client.get_session(session_id)
    except NotFound:
        return None
    return session if session.desired == "active" else None


async def deliver(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    session_id: str,
    text: str,
    marker: str,
) -> RunRef | None:
    """Like `post_update`, but a gone session returns None and leaves the run open.

    Gone means 404 `not_found`, or 409 / 410 on the post (session being deleted); the caller
    then falls back to a new session.
    """
    if run.session_id is None:
        await store.set_session(run.id, session_id)
        run = dataclasses.replace(run, session_id=session_id)
    try:
        return await _post_once(client, store, run, session_id, text, marker, fail_run=False)
    except NotFound:
        return None
    except OrchestratorError as exc:
        if exc.status in (409, 410):
            return None
        raise


async def _post_once(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    session_id: str,
    text: str,
    marker: str,
    *,
    fail_run: bool = True,
) -> RunRef:
    if run.message_id is not None:
        return run
    try:
        existing = await client.list_messages(session_id)
        adopted = next((m for m in existing if m.text and marker in m.text), None)
        message = adopted or await client.post_message(session_id, f"{text}\n\n{marker}")
    except NotFound:
        if fail_run:
            await store.finish(run.id, "failed", "session_not_found")
        raise
    message_id = str(message.id)
    await store.set_message(run.id, message_id)
    return dataclasses.replace(run, message_id=message_id)


async def track(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    now: datetime,
    timeout_seconds: float,
) -> RunStatus | None:
    """Poll the run's message once; finish the run when it is decided.

    Returns the status the run was finished with, or None while it is still running.
    OrchestratorUnavailable propagates so the caller retries on its next tick.
    """
    if run.message_id is None:
        raise ValueError(f"run {run.id} has no message to track")
    try:
        message = await client.get_message(int(run.message_id))
    except NotFound:
        return await _finish(store, run, "failed", "session_not_found")
    turn = message.turn
    if turn is not None and turn.status == "completed":
        return await _finish(store, run, "handled", None)
    if message.status in ("failed", "cancelled"):
        return await _finish(store, run, "failed", f"message_{message.status}")
    if turn is not None and turn.status == "failed":
        return await _finish(store, run, "failed", "turn_failed")
    if (now - run.started_at).total_seconds() > timeout_seconds:
        return await _finish(store, run, "failed", "timeout")
    return None


async def track_delivery(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    *,
    timeout_seconds: float,
    now: datetime | None = None,
) -> Literal["handled", "gone"] | None:
    """Poll a delivered message once.

    `sent` finishes the run handled; cancelled, failed or a missing message returns "gone"
    without finishing so the caller can fall back; past the timeout the run is finished
    failed (`timeout`); still queued or dispatching returns None.
    """
    if run.message_id is None:
        raise ValueError(f"run {run.id} has no message to track")
    try:
        message = await client.get_message(int(run.message_id))
    except NotFound:
        return "gone"
    if message.status == "sent":
        await store.finish(run.id, "handled", None)
        return "handled"
    if message.status in ("cancelled", "failed"):
        return "gone"
    now = now or datetime.now(UTC)
    if (now - run.started_at).total_seconds() > timeout_seconds:
        await store.finish(run.id, "failed", "timeout")
    return None


async def _finish(store: RunStore, run: RunRef, status: RunStatus, reason: str | None) -> RunStatus:
    await store.finish(run.id, status, reason)
    return status
