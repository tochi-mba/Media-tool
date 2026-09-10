# Architecture

## The shape

Ports and adapters. Business rules sit in the middle knowing nothing about HTTP, Playwright,
or the filesystem; everything concrete plugs in at the edges.

```
                       ┌──────────────────────────────┐
   HTTP request ──────▶│  api/   routers, schemas,    │
                       │         problem+json, ids    │
                       └──────────────┬───────────────┘
                                      │
                       ┌──────────────▼───────────────┐
                       │  jobs/  store (+ long-poll)  │
                       │         runner               │
                       └───────┬──────────────┬───────┘
                               │              │
              ┌────────────────▼──┐      ┌────▼─────────────────┐
              │ providers/        │      │ storage/             │
              │  DownloadProvider │─────▶│  ArtifactStore       │
              │  ├ stub           │      │  └ local filesystem  │
              │  └ browser        │      └──────────────────────┘
              └─────────┬─────────┘
                        │
              ┌─────────▼──────────┐
              │ Playwright         │
              │ (headless Chromium)│
              └────────────────────┘

                  domain/  ── used by all of the above, depends on none of them
```

Direction is enforced by import-linter contracts in `pyproject.toml`, so a violation fails
CI rather than being noticed in review. See [ADR-0001](adr/0001-ports-and-adapters.md).

## The two ports that matter

**`DownloadProvider`** (`providers/base.py`) — given one query and one staging sink, get a
file into it. `StubDownloadProvider` fabricates one; `BrowserDownloadProvider` drives
Chromium. The job runner cannot tell which is live, which is what makes the browser work a
configuration change rather than a rewrite.

**`ArtifactStore`** (`storage/base.py`) — where captured files live. Expressed as a two-phase
reservation rather than "here are the bytes", because the producer is a browser: Playwright
writes a download to a path it is given. The store hands out a staging path, the provider
fills it, and the store decides whether the result is acceptable and where it belongs. That
keeps the size ceiling, the digest, and the atomic publish in one place.

## Request lifecycle

1. `POST /v1/downloads` validates the body, normalizes each item into a `MediaQuery` (kind
   inference, `S01E03` tag, canonical key), creates a `Job`, and returns `202` immediately.
2. The runner starts one asyncio task for the job. Inside, items are grouped by canonical
   key — duplicates are downloaded once — and fanned out under a semaphore.
3. Each item gets its own timeout and retry budget. A miss is not retried; a timeout or an
   unavailable browser is, with capped, jittered exponential backoff.
4. Outcomes are written back to the job as they land, so a mid-flight poll shows real
   progress rather than nothing-then-everything.
5. When the last item settles, the job reaches `succeeded`, `partial`, or `failed`, and
   anyone waiting on the long-poll is released.

## Concurrency

Three separate limits, because they constrain different resources:

| Limit | Setting | Protects |
| --- | --- | --- |
| Items downloading at once | `MEDIA_TOOL_DOWNLOAD_CONCURRENCY` | The target site, and our own I/O |
| Browser pages at once | `MEDIA_TOOL_BROWSER__MAX_PAGES` | Memory — pages are expensive |
| Per-item wall time | `MEDIA_TOOL_DOWNLOAD_TIMEOUT_SECONDS` | One stuck item blocking a batch |

## Time

Nothing reads the wall clock directly. Every component that behaves differently over time
takes a `Clock`, and the runner also takes an injectable sleeper and jitter function. That is
why retry, timeout, and retention behaviour can be tested exactly, with no `sleep` anywhere in
the suite. The single exception is the year bound in `domain/media.py`, which is a validation
limit rather than orchestration timing.

## Retention

Job expiry drives artifact deletion, so a file never outlives the record describing it. A
background sweeper evicts expired jobs and purges their directories; a second, mtime-based
sweep is a backstop for orphans whose job record died with the process. A failing sweep is
logged and retried on the next tick rather than silently ending retention for the life of the
process.

## Failure handling

Provider errors carry a stable `code` (`not_found`, `timeout`, `provider_unavailable`) that
becomes the machine-readable reason on a failed item, so a client can branch without parsing
prose. An unexpected exception fails only its own item, is logged in full, and is reported
with a generic message — its text can carry paths or credentials.

Every HTTP failure is RFC 9457 `application/problem+json`, mapped in exactly one place
(`api/errors.py`). Unexpected errors are caught inside the request-id binding rather than by
Starlette's outermost error middleware, because that runs after the binding has unwound and
would return a 500 with no request id — the one thing the caller is told to quote.
