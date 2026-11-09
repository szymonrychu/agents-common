# agents-common memory

Lessons that outlive a session. Newest first.

- 2026-11-08: `agents_common.labels` exists so the vault has one owner of the label rules and the
  labels epic has a seam to switch to. It copies the orchestrator rules verbatim; the orchestrator
  and agents-taskboard keep their own copies until that epic. `is_project_label` assumes a project
  slug is a DNS-label-like `[a-z0-9]([-a-z0-9]*[a-z0-9])?` of at most 32 characters, applied to
  normalised labels; any `project:` label that fails it is invalid. `labels_match` treats an empty
  entry as match-all and does no normalisation.

- 2026-10-25: a shared library (design D1) exists so agents-webhook and agents-schedule share one
  tested database, orchestrator-client and run-tracking layer instead of copying it; services pin
  a tag and tags are never re-pointed.
- 2026-10-25: run idempotency (design D8) rests on a marker comment in the prompt message: the
  orchestrator has no idempotency key, so `dispatch` adopts an existing message carrying the
  marker (and an existing cluster with the slug) instead of creating a second one after a crash.
- 2026-10-31: `agents_common.templating` is ported from agents-webhook's prompt sandbox but is
  context-generic: `render(template, context)` takes a plain dict (must be picklable, it crosses a
  process boundary) and the webhook-only `from_json` filter stays in the webhook. One
  `TemplateError(message, line)` covers syntax, size, timeout and runtime failures so `preview`
  can report `error{line,message}` without parsing strings; the line comes from the template frame
  in the traceback.
- 2026-11-01: `UsageQuotaRefused.retry_at(now)` is `max(resets_at or now + retry_after or now + 300 s,
  now) + 60 s`: the 60 s margin lets the usage window actually reset before the retry, the floor
  keeps a stale `resets_at` from retrying at once, and the 300 s default matches the usage poll
  period. It subclasses `OrchestratorError` (not `NotFound`) and `create_session` only adopts on
  409 `conflict`, so the refusal reaches the trigger services untouched; malformed optional
  fields in the 409 body parse to None rather than hiding the refusal.
- 2026-11-02: `Attachment` is a frozen dataclass holding raw `bytes` and the client does the
  base64 encoding (`to_json`), so callers never handle the wire format and the fake can compare
  real bytes. `download_file` streams with a cap (`max_bytes`, default 8 MiB) and aborts at the
  first chunk past it (also on a declared Content-Length), so an agent-written huge file cannot
  exhaust memory; the cap surfaces as `OrchestratorError(413, "too_large")`.
- 2026-11-03: `discard_session` is a thin DELETE returning the 202 `SessionView`; the client does not
  treat 404 specially beyond `NotFound`, because the orchestrator answers 404 both for an unknown
  session and for one the calling service did not create (ownership check), and callers close
  best effort. The fake's DELETE is a separate route from GET with a private handler
  (`_delete_session`) since the public helper `discard_session` already exists on the fake; it
  only 404s on unknown ids and does not model ownership.
- 2026-11-04: `verify_agent_token` returns `uuid.UUID | None` (confirmed id, or None on 404) instead of a
  bool: the orchestrator answer echoes a session id and the caller must check it is the one it
  asked about, so the client hands it over rather than hiding it. 404 is not an error here
  because the orchestrator uses one 404 for every refusal; all other statuses still raise.
- 2026-11-09: projects (v0.11.0, with labels). `Preset.project`/`Session.project` default "default";
  `Project` model; `create_session(..., project=)` adopts a 409 `conflict` session via
  `GET /api/v1/sessions?slug=` only when its project matches (any when project is None), else
  re-raises, so a slug clash across projects is never silently adopted. `list_projects` and
  `get_project` (NotFound on 404, archived included) back taskboard's ProjectLookup. The fake
  seeds `default`, has `add_project`, `add_preset(project=)`, and answers 422 `unknown_project` /
  `project_archived` on create like the orchestrator.
