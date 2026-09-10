# 0002 — Asynchronous jobs rather than a synchronous call

**Status:** Accepted

## Context

A batch of browser-driven downloads can take minutes. The alternatives were a synchronous
request that returns when everything is done, or a job that is submitted and polled.

## Decision

`POST /v1/downloads` returns `202` with a job id immediately. Progress is read with
`GET /v1/downloads/{job_id}`, which also accepts `wait_seconds` (0–60) to long-poll until the
job finishes.

## Consequences

No request is held open for minutes, so no proxy or load balancer times out mid-download, and
a batch can be far larger than any request timeout would allow. Per-item results stream into
the job as they land, so a mid-flight poll shows real progress.

The cost is a second round trip and a job lifecycle to model — which is why `wait_seconds`
exists. Without it, every client reimplements polling, and an AI assistant calling this as a
tool would be especially bad at it: too fast, or giving up too early. With it, the common case
is one call.

Long-polling is capped at 60 seconds so the endpoint cannot be used to pin connections open
indefinitely. Elapsing returns the job as it stands and is a normal outcome, not an error.

## Alternatives considered

**Synchronous with a short cap.** Simplest, but it makes the batch size a function of the
proxy timeout, which is the wrong thing to couple.

**Webhooks.** Right answer eventually for long batches, wrong first move: it requires callers
to run a server, which an assistant calling a tool cannot do.
