# 0003 — In-memory job store for v1

**Status:** Accepted, expected to change

## Context

Jobs need to live between the request that creates them and the request that reads them. The
options were a dictionary in the process, or Redis/Postgres from the start.

## Decision

An in-memory store behind a `JobStore` protocol. The protocol exists from day one; the
implementation is a lock-guarded dictionary.

## Consequences

**Jobs do not survive a restart, and do not span replicas.** A deploy loses in-flight work,
and running two instances behind a load balancer will not work — a client can be routed to an
instance that has never heard of its job. This is a real limitation, not a detail: it is why
the service is currently single-instance.

In exchange, v1 has no external dependency to run, deploy, or test against, and the whole
pipeline is exercisable with `make check` and nothing else installed.

Artifacts follow the same lifecycle: job expiry drives file deletion, so a file never
outlives the record describing it.

## Migrating

The port is `media_tool/jobs/store.py`. A replacement needs `add`, `get`, `save`,
`purge_expired`, `count`, and `wait_for_terminal`. Only the last is interesting: the in-memory
version uses an `asyncio.Event` per job, which does not work across processes. In Redis it
would be a pub/sub subscription or a blocking read on a per-job key; in Postgres, `LISTEN`
/`NOTIFY`. Everything else is a straightforward key-value mapping.

Do this when any of these becomes true: work must survive a deploy, more than one replica is
needed, or a job must be auditable after the fact.
