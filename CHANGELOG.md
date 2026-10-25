# Changelog

## v0.1.0

First release.

- `settings`: `BaseServiceSettings`, `load_settings`, `SettingsError`.
- `db`: pool, LISTEN/NOTIFY, migrations and status-guarded queue updates.
- `runtime`: `advisory_singleton`, `run_service`.
- `orchestrator`: typed `OrchestratorClient` with idempotent `create_cluster`.
- `fake_orchestrator`: in-process `FakeOrchestrator` for tests.
- `runs`: crash-safe `dispatch`, `post_update` and `track` with message markers.
- `testing`: pytest plugin with `pg_url` and `migrated_db` fixtures.
