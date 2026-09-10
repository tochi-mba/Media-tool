# Operations

## Running it

```bash
make run                                   # dev, :8000, reload
uv run media-tool                          # production entry point
docker build -t media-tool . && docker run -p 8000:8000 media-tool
```

`GET /healthy` returns `200` when everything is usable and `503` when any dependency is
degraded, with the same body either way. Point a liveness probe at it. It is the only route
that does not need a token, and its `identity` check reports how callers are identified —
`keyring` or `disabled` — which is the first thing to look at on an unfamiliar instance.

Nothing starts without keyring. Set `MEDIA_TOOL_KEYRING_BASE_URL` and
`MEDIA_TOOL_KEYRING_SERVICE_TOKEN`, or set `MEDIA_TOOL_REQUIRE_AUTHENTICATION=false` and
understand what that means (below).

## Configuration

Every setting is an environment variable prefixed `MEDIA_TOOL_`. Nested browser settings use a
double underscore. **Unknown variables under the prefix are rejected at startup**, so a typo
fails loudly instead of leaving a default silently in place.

### Core

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_ENVIRONMENT` | `local` | Reported by `/healthy`. |
| `MEDIA_TOOL_LOG_LEVEL` | `INFO` | |
| `MEDIA_TOOL_LOG_FORMAT` | `json` | `console` for human-readable local output. |
| `MEDIA_TOOL_HOST` / `_PORT` | `127.0.0.1` / `8000` | |

### Identity

media-tool authenticates nobody itself. Callers present a token keyring signed, and this
service verifies it against keyring's published keys. See [ADR-0008](adr/0008-delegated-identity.md).

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_REQUIRE_AUTHENTICATION` | `true` | `false` makes every request one account. Laptops only. |
| `MEDIA_TOOL_KEYRING_BASE_URL` | — | **Required.** Where keyring is. Startup fails without it. |
| `MEDIA_TOOL_KEYRING_SERVICE_TOKEN` | — | **Required.** This service's entry in keyring's `service_tokens`. |
| `MEDIA_TOOL_KEYRING_ISSUER` | `https://keyring.local` | Must equal keyring's own `KEYRING_ISSUER`, or every token is refused. |
| `MEDIA_TOOL_KEYRING_AUDIENCE` | `media-tool` | Must equal the name keyring mints tokens for. |
| `MEDIA_TOOL_KEYRING_JWKS_CACHE_TTL_SECONDS` | `300` | How long public keys are held before re-reading. |
| `MEDIA_TOOL_KEYRING_TIMEOUT_SECONDS` | `5` | Per call to keyring. |
| `MEDIA_TOOL_DEFAULT_PROFILE` | `default` | Credential profile used when a request names none. |
| `MEDIA_TOOL_ANONYMOUS_ACCOUNT` | `local` | Who everyone is when authentication is off. |

Two settings must agree with keyring or nothing works, and both fail closed: `KEYRING_ISSUER`
and `KEYRING_AUDIENCE`. A mismatch refuses every token with the same message every other bad
token gets, so check these first when a working deployment suddenly accepts nobody.

### Per-account limits

One process shared by several people. Without these, any one of them can make it useless for
the rest by accident.

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_MAX_ACTIVE_JOBS_PER_ACCOUNT` | `5` | Unfinished jobs. Finished ones cost nothing. |
| `MEDIA_TOOL_MAX_BYTES_PER_ACCOUNT` | 16 GiB | Stored artifacts, walked at submit time. |
| `MEDIA_TOOL_REQUEST_BURST_PER_ACCOUNT` | `60` | Requests allowed at once. |
| `MEDIA_TOOL_REQUESTS_PER_SECOND_PER_ACCOUNT` | `5` | Sustained rate the burst refills at. |

All four are per account, so one person hitting a limit leaves everyone else working. Both
quotas answer `429` naming which one was reached, with `Retry-After`.

### Work

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_PROVIDER` | `stub` | `stub` or `browser`. |
| `MEDIA_TOOL_RECIPE_PATH` | — | Required when provider is `browser`. |
| `MEDIA_TOOL_MAX_ITEMS_PER_REQUEST` | `50` | |
| `MEDIA_TOOL_DOWNLOAD_CONCURRENCY` | `4` | Items in flight at once. |
| `MEDIA_TOOL_DOWNLOAD_TIMEOUT_SECONDS` | `120` | Per item, per attempt. |
| `MEDIA_TOOL_MAX_ATTEMPTS` | `3` | Misses are never retried regardless. |
| `MEDIA_TOOL_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | `0.5` / `8` | Exponential, capped, jittered. |

### Storage and retention

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_ARTIFACT_DIR` | `var/artifacts` | Resolved to an absolute path at startup. |
| `MEDIA_TOOL_MAX_FILE_BYTES` | 2 GiB | Oversized downloads are aborted and discarded. |
| `MEDIA_TOOL_JOB_TTL_SECONDS` | `3600` | Job expiry also deletes its files. |
| `MEDIA_TOOL_ARTIFACT_TTL_SECONDS` | `3600` | Orphan sweep, for files whose job died with the process. |
| `MEDIA_TOOL_JOB_SWEEP_INTERVAL_SECONDS` | `60` | |

### Browser

| Variable | Default | Notes |
| --- | --- | --- |
| `MEDIA_TOOL_BROWSER__HEADLESS` | `true` | Set `false` when hunting for selectors. |
| `MEDIA_TOOL_BROWSER__EXECUTABLE_PATH` | — | Needed when Playwright is newer than the available Chromium; also bypasses the revision check. |
| `MEDIA_TOOL_BROWSER__MAX_PAGES` | `2` | Pages cost real memory. |
| `MEDIA_TOOL_BROWSER__NAVIGATION_TIMEOUT_MS` | `15000` | |
| `MEDIA_TOOL_BROWSER__ACTION_TIMEOUT_MS` | `10000` | |
| `MEDIA_TOOL_BROWSER__DOWNLOAD_TIMEOUT_MS` | `120000` | |
| `MEDIA_TOOL_BROWSER__STORAGE_STATE_PATH` | — | Saved session for sites needing a login. |
| `MEDIA_TOOL_BROWSER__USER_AGENT` | — | |

## Capacity

Memory is dominated by browser pages, not by the API. Budget a few hundred MB per concurrent
page and keep `BROWSER__MAX_PAGES` at or below `DOWNLOAD_CONCURRENCY` — more download slots
than pages just means waiting on the semaphore.

Disk is bounded by `MAX_FILE_BYTES × concurrent downloads` plus everything inside the
retention window. `/healthy` reports free bytes; alert on it.

## Exposing it

This service is built to be reachable by a handful of people over the internet. It is not
built to be reachable by everyone. Work down this list before it is:

1. **Terminate TLS at a reverse proxy** (Caddy, nginx, Traefik) and let it hold the
   certificate. media-tool speaks plain HTTP and should bind to localhost, with the proxy in
   front. A bearer token over plain HTTP is a bearer token in somebody's transit logs.
2. **Check `/healthy` says `identity.mode = "keyring"`.** If it says `disabled`, every caller
   is the same account and every caller can read every file. That mode exists for a laptop.
3. **Set both keyring settings from a secret store, not from a shell.**
   `MEDIA_TOOL_KEYRING_SERVICE_TOKEN` is a shared secret; it is held as a `SecretStr` so it
   cannot be printed with the rest of the settings, and it should not reach a shell history
   either.
4. **Set the per-account limits deliberately.** The defaults suit a household. Disk is the one
   that bites: `MAX_BYTES_PER_ACCOUNT × people` is the floor for how much you need.
5. **Consider blocking `/docs`, `/redoc` and `/openapi.json` at the proxy.** They are open so
   the interactive docs work in a browser. They describe the service rather than anybody using
   it, but there is no reason to publish even that to the whole internet.
6. **Keep the artifact directory off any path the proxy serves.** Downloaded files are
   attacker-supplied bytes; the only thing that should read them is this service.

What the service does for itself:

- **Every `/v1` route needs a token**, checked before routing — so an unknown path answers
  `401` rather than `404`, and the set of routes is not readable by asking.
- **One account cannot see another's jobs or files**, at two independent layers: the store
  checks, and the filesystem layout means a path built for the wrong account names nothing.
  Cross-account access answers `404`, never `403`. See [ADR-0010](adr/0010-account-scoping.md).
- **Per-account quotas and rate limiting**, so one caller cannot starve the others.
- **Credentials are resolved per attempt and stored nowhere** — not on the job, not in a log,
  not in any response. See [ADR-0009](adr/0009-credentials-per-attempt.md).

What remains your problem:

- **Recipes are trusted input.** A recipe names the URLs to visit and the elements to click.
  Treat a recipe file like code, and do not accept one from an untrusted source.
- **Downloaded files are untrusted.** They are stored, hashed, and served back verbatim; the
  service does not scan them. Serving them onward to browsers means serving attacker-supplied
  bytes — the `Content-Disposition: attachment` header is deliberate.
- Filenames from `Content-Disposition` are sanitized against traversal, absolute paths, NUL
  and control characters before touching disk, and lookups re-check containment afterwards
  (which is what catches a symlink pointing out of the artifact root).
- Unexpected error text is never returned to callers; it can carry paths or credentials.
  Callers get a request id that ties the response to the full log record.

## Legal note

Automating downloads is subject to the target site's terms of service and to applicable law,
and that is the operator's responsibility, not the tool's. Where a site offers an official
API, prefer it: it will be more stable than any selector-based recipe.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `/healthy` reports the provider degraded | Playwright not installed (`uv sync --all-extras`) or the browser crashed. |
| Startup fails with "unknown provider" or "cannot read recipe" | `MEDIA_TOOL_RECIPE_PATH` unset or wrong. Deliberate: better than accepting work it cannot do. |
| Every item is `not_found` | Selectors have drifted. Re-check with `BROWSER__HEADLESS=false`. |
| Every item is `timeout` | The trigger click starts no download — wrong selector, or the site now interposes a page. |
| `/healthy` reports storage unwritable | `ARTIFACT_DIR` missing, unmounted, or not writable by the process. |
| Jobs vanish after a restart | Expected — the job store is in-memory. See ADR-0003. |
| Startup fails with "keyring_base_url is required" | Authentication is on and keyring is unconfigured. Set both keyring variables, or turn authentication off deliberately. |
| Every request answers `401` on a deployment that worked | `KEYRING_ISSUER` or `KEYRING_AUDIENCE` no longer matches keyring. Both fail closed and say nothing more. |
| `/healthy` reports identity not ready | media-tool has never read keyring's keys. Check the base URL and that keyring is up; cached keys survive an outage, a cold start does not. |
| An item fails with `reauthenticate` | The caller's token expired mid-job. They sign in again and resubmit; see ADR-0009 for why it is not refreshed here. |
| An item fails with `credential_missing` | That account has not connected the site in keyring, the named profile does not exist, or the stored login lacks a field the recipe types. |
| Submissions answer `429` | A per-account limit. The detail says which and what clears it. |
