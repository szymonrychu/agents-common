# Changelog

## v0.14.0

Session readiness. Needs an agents-orchestrator whose SessionView has `summary` (any current one). No breaking change.

- `orchestrator`: `SessionSummary` (`ready`, `start_state`), `Session.summary` and `Session.is_up` (active, start state `ready`, at least one ready agent; no summary is not up).
- `fake_orchestrator`: created sessions carry a `booting` summary; `set_session_ready(session_id, ready=1, start_state="ready")`.

## v0.13.0

Label scope. Behaviour change in `labels_match` only (same name and signature).

- `labels`: `labels_match(entry, target)` now lets only `project:` labels restrict. An entry without a `project:` label (unlabelled, or descriptive tags only) matches every target; an entry with `project:` labels matches only when the target holds one of them. Before, any entry label restricted, so a tag-only entry failed to match a target with other labels.

## v0.12.0

Projects. No breaking change. Needs an agents-orchestrator with the projects API (`/api/v1/projects`); `default` is the project every preset and session falls back to.

- `orchestrator`: `Project` model; `Preset.project` and `Session.project` (default `"default"`).
- `orchestrator`: `OrchestratorClient.list_projects()` (active projects) and `get_project(slug)` (archived included; an unknown slug raises `NotFound`).
- `orchestrator`: `create_session(..., project=)` places the session in that project. A 409 `conflict` adopts the existing session of that slug only when it is in the requested project (any project when `project` is None); otherwise the conflict is re-raised.
- `fake_orchestrator`: seeds the `default` project, `add_project`, `add_preset(project=)`, the `/api/v1/projects` routes, and answers 422 `unknown_project` / `project_archived` on session create like the orchestrator.

## v0.11.0

Shared labels. No breaking change.

- `labels`: new module with the preset label rules (`normalize_label`, `normalize_labels`), the reserved `project:` prefix (`is_project_label`) and `labels_match`. The orchestrator and agents-taskboard keep their own copies for now.
- `orchestrator`: `Preset.labels` (default empty list), parsed from the orchestrator `PresetView`.
- `fake_orchestrator`: `add_preset(..., labels=None)` stores the labels.

## v0.10.0

Agent token check. Needs an agents-orchestrator with `POST /api/v1/sessions/{id}/agent-token/verify` on the service port (PR 73). No breaking change.

- `orchestrator`: `OrchestratorClient.verify_agent_token(session_id, token)` returns the confirmed session id on 200 and `None` on 404; other statuses raise (`OrchestratorError`, 5xx `OrchestratorUnavailable`). The token travels in the body only.
- `fake_orchestrator`: the verify route, `set_agent_token(session_id, token, answer_id=None)`, routes through `fail_next("verify_agent_token", status)`.

## v0.9.0

Session discard. Needs an agents-orchestrator that allows DELETE on the service port (4.1.0). No breaking change.

- `orchestrator`: `OrchestratorClient.discard_session(session_id)` sends `DELETE /api/v1/sessions/{id}` and returns the `Session` (202); 404 raises `NotFound`, 5xx `OrchestratorUnavailable`.
- `fake_orchestrator`: DELETE route on `/api/v1/sessions/{id}` (404 on unknown), records the call in `discarded`, routes through `fail_next("discard_session", status)`.

## v0.8.0

Message attachments and file download. Needs an agents-orchestrator with attachments and the file download route. No breaking change.

- `orchestrator`: `Attachment` (`name`, `mime`, `data` bytes; the client base64-encodes it), `OrchestratorClient.post_message(..., attachments=)`, `Message.attachments` (`AttachmentMeta`: `name`, `mime`, `size`, `path`).
- `orchestrator`: `OrchestratorClient.download_file(session_id, path, agent_id=None, max_bytes=)` streams a file the agent wrote, raising `OrchestratorError(413, "too_large")` over the cap (default 8 MiB).
- `orchestrator`: `OrchestratorClient.list_sessions()`.
- `runs`: `dispatch`, `post_update` and `deliver` pass an `attachments` keyword through to `post_message`.
- `fake_orchestrator`: stores posted attachments (`attachments`), serves downloads (`files`, `downloads`) and routes `download_file` through `fail_next`.

## v0.7.0

Usage quota refusals. Needs agents-orchestrator 3.1.0 for the refusal; no breaking change.

- `orchestrator`: `UsageQuotaRefused` (an `OrchestratorError`: `metric`, `percent`, `limit`, `resets_at`, `retry_after`, `retry_at(now)`), returned for a 409 `usage_quota`.
- `runs`: `dispatch` lets `UsageQuotaRefused` propagate and finishes nothing.
- `fake_orchestrator`: `refuse_usage`.

## v0.6.0

Service gate. No breaking change.

- `service_gate`: `ServiceTokenGate` bearer service-token ASGI guard and `load_tokens`.

## v0.5.0

Templating. No breaking change.

- `templating`: sandboxed Jinja2 `render`, `validate`, `undeclared_variables` and `preview`; `TemplateError` carries the message and line.
- `templating`: built-in `from_json` filter.

## v0.4.0

Command messages. Needs agents-orchestrator with `MessageView.command`. No breaking change.

- `orchestrator`: `Message.command` (`CommandOutcome`: `state`, `result`, `error`, `turn_id`, `at`).
- `runs`: `track` judges a command message by its outcome (failure reasons `command_<error>` and `command_cancelled`).
- `fake_orchestrator`: `complete_command` and `fail_command`.

## v0.3.0

Session client. Needs agents-orchestrator with the session API.

Breaking:

- `orchestrator`: `Cluster` is now `Session`.
- `create_cluster`, `get_cluster`, `running_cluster`, `set_cluster`, `discard_cluster` and `remove_cluster` are now `create_session`, `get_session`, `running_session`, `set_session`, `discard_session` and `remove_session` (client, `runs` and `fake_orchestrator`).
- `RunRef.session_id` replaces the cluster id field.
- Failure reason `session_not_found`.
- Routes are `/api/v1/sessions`.

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
