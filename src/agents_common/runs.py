"""Idempotent dispatch and tracking of one triggered run (design D8).

A run owns one cluster (deterministic slug) and one prompt message carrying a marker. The
orchestrator has no idempotency key, so every step is skipped when its id is already stored
and a message carrying the marker is adopted instead of posting another: calling `dispatch`
again after a crash at any point creates no second cluster and posts no second message.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from agents_common.orchestrator import NotFound, OrchestratorClient

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
    cluster_id: str | None
    message_id: str | None
    started_at: datetime


@runtime_checkable
class RunStore(Protocol):
    """Writes of one run; each is a status-guarded update on an in-progress row."""

    async def set_cluster(self, run_id: RunId, cluster_id: str) -> None: ...

    async def set_message(self, run_id: RunId, message_id: str) -> None: ...

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool: ...


def marker(run_kind: str, run_id: RunId) -> str:
    return f"<!-- agents-{run_kind}:{run_id} -->"


async def dispatch(
    client: OrchestratorClient, store: RunStore, run: RunRef, text: str, marker: str
) -> RunRef:
    """Create the run's cluster and post its prompt, each step at most once.

    A missing preset or cluster finishes the run failed (`preset_not_found`,
    `cluster_not_found`) and re-raises the NotFound; other orchestrator errors propagate.
    """
    cluster_id = run.cluster_id
    if cluster_id is None:
        try:
            cluster = await client.create_cluster(run.preset_id, run.slug)
        except NotFound:
            await store.finish(run.id, "failed", "preset_not_found")
            raise
        cluster_id = str(cluster.id)
        await store.set_cluster(run.id, cluster_id)
        run = dataclasses.replace(run, cluster_id=cluster_id)
    return await _post_once(client, store, run, cluster_id, text, marker)


async def post_update(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    cluster_id: str,
    text: str,
    marker: str,
) -> RunRef:
    """Post `text` to an existing cluster with the same adopt-before-post rule."""
    if run.cluster_id is None:
        await store.set_cluster(run.id, cluster_id)
        run = dataclasses.replace(run, cluster_id=cluster_id)
    return await _post_once(client, store, run, cluster_id, text, marker)


async def _post_once(
    client: OrchestratorClient,
    store: RunStore,
    run: RunRef,
    cluster_id: str,
    text: str,
    marker: str,
) -> RunRef:
    if run.message_id is not None:
        return run
    try:
        existing = await client.list_messages(cluster_id)
        adopted = next((m for m in existing if m.text and marker in m.text), None)
        message = adopted or await client.post_message(cluster_id, f"{text}\n\n{marker}")
    except NotFound:
        await store.finish(run.id, "failed", "cluster_not_found")
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
        return await _finish(store, run, "failed", "cluster_not_found")
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


async def _finish(store: RunStore, run: RunRef, status: RunStatus, reason: str | None) -> RunStatus:
    await store.finish(run.id, status, reason)
    return status
