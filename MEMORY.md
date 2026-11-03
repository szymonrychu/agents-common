# agents-common memory

Lessons that outlive a session. Newest first.

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
