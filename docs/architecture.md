# Architecture

## The shape

Ports and adapters. Business rules sit in the middle knowing nothing about HTTP, Playwright,
or the filesystem; everything concrete plugs in at the edges.

```
                       ┌──────────────────────────────┐
   HTTP request ──────▶│  api/   authentication,      │
   (bearer token)      │         routers, schemas,    │
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

            core/keyring/ ── token verification, credential resolution
                             (talks to keyring; nothing else does)
```

## Who is asking

media-tool authenticates nobody. It verifies a token keyring signed, against keys keyring
publishes, and takes the account from it:

```
person ──login──▶ keyring ──session──▶ their assistant
                     │
  assistant asks keyring for a token with audience "media-tool"
                     │
  assistant ──Authorization: Bearer <token>──▶ media-tool
                                                  │
                        verified locally against keyring's JWKS (no round trip)
                                                  │
                        needs a login for a site:
                        ├─ Authorization: Bearer <media-tool's own service token>
                        └─ X-Keyring-User-Token: <the same token it was handed>
                                                  │
                                                  ▼
                                               keyring
```

Three properties hold this together, and each is tested by name:

1. **media-tool mints nothing.** It forwards the token it was handed. There is no code path by
   which it can name an account, and a contract test asserts the published schema never offers
   one.
2. **Verification is local.** Keys are cached; the price is that a revoked session stays valid
   until its token expires.
3. **Credentials never enter stored state.** Resolved per attempt, held in local scope, written
   nowhere.

See [ADR-0008](adr/0008-delegated-identity.md), [ADR-0009](adr/0009-credentials-per-attempt.md)
and [ADR-0010](adr/0010-account-scoping.md).

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

1. The authentication middleware resolves the account from the bearer token, before routing,
   and binds it for the request. An unknown path is a `401` rather than a `404`.
2. `POST /v1/downloads` checks the account's quotas, validates the body, normalizes each item
   into a `MediaQuery` (kind inference, `S01E03` tag, canonical key), creates a `Job` owned by
   that account, and returns `202` immediately.
3. The runner starts one asyncio task for the job, carrying the caller's token in the task and
   nowhere else. Inside, items are grouped by canonical key — duplicates are downloaded once —
   and fanned out under a semaphore.
4. Each item gets its own timeout and retry budget. If the site needs a login, the credential
   is resolved at the start of each attempt. A miss is not retried, nor is a credential
   failure; a timeout or an unavailable browser is, with capped, jittered exponential backoff.
5. Outcomes are written back to the job as they land, so a mid-flight poll shows real
   progress rather than nothing-then-everything.
6. When the last item settles, the job reaches `succeeded`, `partial`, or `failed`, and
   anyone waiting on the long-poll is released.

## Concurrency

Three separate limits, because they constrain different resources:

| Limit | Setting | Protects |
| --- | --- | --- |
| Items downloading at once | `MEDIA_TOOL_DOWNLOAD_CONCURRENCY` | The target site, and our own I/O |
| Browser pages at once | `MEDIA_TOOL_BROWSER__MAX_PAGES` | Memory — pages are expensive |
| Per-item wall time | `MEDIA_TOOL_DOWNLOAD_TIMEOUT_SECONDS` | One stuck item blocking a batch |

Three more are per account, and protect the other people sharing the process rather than the
process itself: unfinished jobs, bytes stored, and request rate. See
[docs/operations.md](operations.md).

## Time

Nothing reads the wall clock directly. Every component that behaves differently over time
takes a `Clock`, and the runner also takes an injectable sleeper and jitter function. That is
why retry, timeout, and retention behaviour can be tested exactly, with no `sleep` anywhere in
the suite. The single exception is the year bound in `domain/media.py`, which is a validation
limit rather than orchestration timing.

## Isolation

Two independent layers, and the second is the one that matters when the first has a bug:

- Every job carries its account, and every read of a job takes the account making it. Somebody
  else's job answers `404`, never `403` — a `403` would confirm the id is real.
- The account is the outermost segment of the artifact path, so a path built for the wrong
  account does not name another account's file. It names nothing.

`AccountId` is a parsed type whose rules are about what is safe to write down: one path
segment, no separators, no `..`. That is checked once, where a token's subject becomes an
account, and every layer downstream gets to assume it.

## Retention

Job expiry drives artifact deletion, so a file never outlives the record describing it. A
background sweeper evicts expired jobs and purges their directories; a second, mtime-based
sweep is a backstop for orphans whose job record died with the process. A failing sweep is
logged and retried on the next tick rather than silently ending retention for the life of the
process.

## Failure handling

Provider errors carry a stable `code` (`not_found`, `timeout`, `provider_unavailable`,
`reauthenticate`, `credential_missing`) that becomes the machine-readable reason on a failed
item, so a client can branch without parsing prose. The last two are separate because their
fixes are: signing in again, and connecting an account. An unexpected exception fails only its own item, is logged in full, and is reported
with a generic message — its text can carry paths or credentials.

Every HTTP failure is RFC 9457 `application/problem+json`, mapped in exactly one place
(`api/errors.py`). Unexpected errors are caught inside the request-id binding rather than by
Starlette's outermost error middleware, because that runs after the binding has unwound and
would return a 500 with no request id — the one thing the caller is told to quote.
