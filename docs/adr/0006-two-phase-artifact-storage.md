# 0006 — Two-phase artifact storage

**Status:** Accepted

## Context

The component producing the bytes is a browser. Playwright writes a download to a path it is
given — it does not hand back a stream. A conventional `store(bytes)` interface would mean
buffering a multi-gigabyte file in memory or inventing a temporary file at the call site.

## Decision

Storage is a reservation. `reserve()` yields a sink with a `staging_path`; the provider fills
it; `commit()` validates, hashes, and publishes.

## Consequences

The size ceiling, the SHA-256, the filename sanitization, and the atomic publish all live in
one place, so a new provider cannot get any of them wrong. Providers do not touch the
filesystem directly at all.

Publishing is an atomic rename from a staging directory on the same filesystem, so a reader
sees either no file or the whole file, never a partial one. Leaving the reservation without
committing — an exception, an abandoned download, an oversized file — deletes the staging file,
so a failure never leaves debris.

The digest is computed while storing, not read back afterwards, so it genuinely describes the
bytes that were written.

The cost is a slightly unusual interface: `with store.reserve(...) as sink` is more to explain
than `store.save(data)`, and it presumes the producer can write to a path. For a browser that
is exactly right; for a future provider streaming from HTTP it means writing to the staging
path rather than yielding chunks, which is a mild imposition.

Deduplication uses hard links rather than copies, so one download satisfying several submitted
items costs one copy of the bytes while still giving every item its own path — no client needs
to know deduplication happened. A copy fallback covers filesystems without hard-link support.
