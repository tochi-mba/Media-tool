# Operations

## Running it

```bash
make run                                   # dev, :8000, reload
uv run media-tool                          # production entry point
docker build -t media-tool . && docker run -p 8000:8000 media-tool
```

`GET /healthy` returns `200` when everything is usable and `503` when any dependency is
degraded, with the same body either way. Point a liveness probe at it.

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

## Security posture

**This service is not safe to expose publicly as it stands.** Be deliberate about that:

- **No authentication and no rate limiting.** Anyone who can reach it can make the server
  drive a browser to arbitrary sites and write files to its disk. Run it on localhost or
  behind an authenticating proxy.
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
