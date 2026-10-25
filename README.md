# agents-common

Shared Python library for the agents trigger services (agents-webhook, agents-schedule):
database access, settings, orchestrator client, run tracking and test fixtures, so each
service stays small.

## Install

    agents-common @ git+https://github.com/szymonrychu/agents-common@v0.3.0

Test helpers need the extra: `agents-common[testing] @ git+https://github.com/szymonrychu/agents-common@v0.3.0`.

## `settings`

Public names: `BaseServiceSettings`, `SettingsError`, `load_settings`.

    from agents_common.settings import BaseServiceSettings, load_settings

    class Settings(BaseServiceSettings):
        webhook_secret: str

    settings = load_settings(Settings)  # raises SettingsError naming every missing variable

## `db`

Public names: `open_pool`, `listen`, `notify`, `run_migrations`, `MigrationError`, `claim`,
`transition`.

    from agents_common.db import open_pool, run_migrations, transition

    run_migrations(url, "myservice.migrations")
    pool = await open_pool(url)
    # status-guarded update: only moves the row when it is still in the expected status

## `runtime`

Public names: `advisory_singleton`, `run_service`, `lock_key`, `LostLock`.

    from agents_common.runtime import advisory_singleton

    async with advisory_singleton(pool, "agents-schedule"):
        ...  # only one replica holds the Postgres advisory lock at a time

## `orchestrator`

Client of the agents-orchestrator session API (`/api/v1/sessions`, `/api/v1/sessions/{id}`,
`/api/v1/sessions/{id}/messages`).

Public names: `OrchestratorClient` (`create_session`, `get_session`, `list_messages`,
`post_message`, `get_message`, `list_presets`, `get_preset`), `Preset`, `Session`, `Turn`,
`Message` (`session_id`), `OrchestratorError`, `NotFound`, `OrchestratorUnavailable`.
`Preset.spec` is the sparse preset spec and `Preset.reuse_running_agent` its optional
agent-reuse flag; `Session.status` is the session status document.

    from agents_common.orchestrator import OrchestratorClient

    async with OrchestratorClient(base_url, token) as client:
        session = await client.create_session(preset_id, "run-42")  # idempotent on slug
        await client.post_message(session.id, "hello")

## `fake_orchestrator`

Public names: `FakeOrchestrator` (an in-process ASGI app speaking the agents-orchestrator
session API; its `sessions` and `messages` dicts are the store).
Helpers: `add_preset(name, is_default=False, spec=None)`, `mark_sent(message_id)`,
`discard_session(session_id)` (desired `deleting`, queued messages cancelled, posts answer 409
`session_deleting`), `remove_session(session_id)` (404), `complete_turn`, `fail_turn`,
`fail_next(route, status)` (route labels are client method names plus `list_sessions`,
`create_session`, `get_session`).

    from agents_common.fake_orchestrator import FakeOrchestrator

    fake = FakeOrchestrator()
    client = OrchestratorClient("http://orch", fake.token, transport=httpx.ASGITransport(app=fake.app))

## `runs`

Public names: `dispatch`, `post_update`, `running_session`, `deliver`, `track_delivery`,
`track`, `marker`, `RunRef` (`session_id`), `RunStore` (`set_session`, `set_message`,
`finish`), `PresetBinding`, `RunStatus`, `RunId`. A missing preset fails the run with
`preset_not_found`, a missing session with `session_not_found`.

    from agents_common.runs import dispatch, marker

    run = await dispatch(client, store, run, text, marker("webhook", run.id))
    # safe to call again after a crash: no second session, no second message

Agent reuse: `running_session(client, session_id)` returns the session only while it exists
with desired `active`. `deliver(...)` posts like `post_update` but returns None (run left open)
when the session is gone, so the caller can create a new one. `track_delivery(client, store, run,
timeout_seconds=...)` returns "handled" (message sent, run finished), "gone" (message cancelled,
failed or missing, run left open), or None (still pending; past the timeout the run is finished
failed with `timeout`).

## `testing`

Public names: fixtures `pg_url`, `migrated_db` (a pytest plugin registered via the `pytest11`
entry point; needs the `testing` extra and Docker).

    def test_it(migrated_db):
        url = migrated_db("myservice.migrations")  # fresh DB cloned from a migrated template

## Versioning

Releases are git tags `vMAJOR.MINOR.PATCH`. Tags are never re-pointed: a fix is a new tag.
Consumers pin a tag in their dependency line.
