# Changelog

## v0.2.0

Agent reuse support. v0.1.0 names are unchanged.

- `orchestrator`: `Preset.spec` (sparse preset spec), `Preset.reuse_running_agent` (optional flag) and `Cluster.status` (status document).
- `runs`: `running_cluster`, `deliver` and `track_delivery` for posting into a still-running cluster.
- `fake_orchestrator`: `add_preset(spec=...)`, `mark_sent`, `discard_cluster` and `remove_cluster` helpers.

## v0.1.0

First release.

- `settings`: `BaseServiceSettings`, `load_settings`, `SettingsError`.
- `db`: pool, LISTEN/NOTIFY, migrations and status-guarded queue updates.
- `runtime`: `advisory_singleton`, `run_service`.
- `orchestrator`: typed `OrchestratorClient` with idempotent `create_cluster`.
- `fake_orchestrator`: in-process `FakeOrchestrator` for tests.
- `runs`: crash-safe `dispatch`, `post_update` and `track` with message markers.
- `testing`: pytest plugin with `pg_url` and `migrated_db` fixtures.
