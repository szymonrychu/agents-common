# agents-common

Shared Python library for the agents trigger services (agents-webhook, agents-schedule):
database access, settings, orchestrator client, run tracking and test fixtures, so each
service stays small.

## Install

    agents-common @ git+https://github.com/szymonrychu/agents-common@v0.1.0

Test helpers need the extra: `agents-common[testing] @ git+https://github.com/szymonrychu/agents-common@v0.1.0`.

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

Public names: `OrchestratorClient`, `Preset`, `Cluster`, `Turn`, `Message`,
`OrchestratorError`, `NotFound`, `OrchestratorUnavailable`.

    from agents_common.orchestrator import OrchestratorClient

    async with OrchestratorClient(base_url, token) as client:
        cluster = await client.create_cluster(preset_id, "run-42")  # idempotent on slug
        await client.post_message(cluster.id, "hello")

## `fake_orchestrator`

Public names: `FakeOrchestrator` (an in-process ASGI app speaking the orchestrator API).

    from agents_common.fake_orchestrator import FakeOrchestrator

    fake = FakeOrchestrator()
    client = OrchestratorClient("http://orch", fake.token, transport=httpx.ASGITransport(app=fake.app))

## `runs`

Public names: `dispatch`, `post_update`, `track`, `marker`, `RunRef`, `RunStore`,
`PresetBinding`, `RunStatus`, `RunId`.

    from agents_common.runs import dispatch, marker

    run = await dispatch(client, store, run, text, marker("webhook", run.id))
    # safe to call again after a crash: no second cluster, no second message

## `testing`

Public names: fixtures `pg_url`, `migrated_db` (a pytest plugin registered via the `pytest11`
entry point; needs the `testing` extra and Docker).

    def test_it(migrated_db):
        url = migrated_db("myservice.migrations")  # fresh DB cloned from a migrated template

## Versioning

Releases are git tags `vMAJOR.MINOR.PATCH`. Tags are never re-pointed: a fix is a new tag.
Consumers pin a tag in their dependency line.
