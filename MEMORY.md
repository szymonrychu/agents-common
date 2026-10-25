# agents-common memory

Lessons that outlive a session. Newest first.

- 2026-10-25: a shared library (design D1) exists so agents-webhook and agents-schedule share one
  tested database, orchestrator-client and run-tracking layer instead of copying it; services pin
  a tag and tags are never re-pointed.
- 2026-10-25: run idempotency (design D8) rests on a marker comment in the prompt message: the
  orchestrator has no idempotency key, so `dispatch` adopts an existing message carrying the
  marker (and an existing cluster with the slug) instead of creating a second one after a crash.
