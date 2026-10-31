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
