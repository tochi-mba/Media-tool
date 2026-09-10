# HTTP API

Interactive docs at `/docs` when the service is running; `/openapi.json` is the machine
-readable contract. This page covers the parts a schema cannot express.

Every route has a stable `operation_id`. They are public API — they become MCP tool names —
so they are never renamed casually. See [docs/mcp.md](mcp.md).

| Operation | Route |
| --- | --- |
| `get_health` | `GET /healthy` |
| `create_download_job` | `POST /v1/downloads` |
| `get_download_job` | `GET /v1/downloads/{job_id}` |
| `cancel_download_job` | `DELETE /v1/downloads/{job_id}` |
| `fetch_download_file` | `GET /v1/downloads/{job_id}/items/{index}/file` |

## Authentication

Every route except `GET /healthy` needs a bearer token that keyring signed for this service:

```
Authorization: Bearer <token from keyring, audience "media-tool">
```

Your assistant gets one by asking keyring for a service token with
`{"audience": "media-tool"}`. media-tool verifies it locally against keyring's published
keys — there is no round trip per request — and takes your account from it.

**The account comes from the token and from nowhere else.** No field, path or query parameter
names an account; there is no request you can make for somebody else's work. Everything you
submit, every file it produces, and every limit you are held to is yours alone.

| Response | Means |
| --- | --- |
| `401` | No token, or one this service will not accept. It does not say which check failed. |
| `403` | Never sent. Somebody else's job answers `404`, exactly like one that does not exist. |
| `429` | A per-account limit. The detail names which; `Retry-After` says how long to wait. |
| `503` | keyring could not be reached. Your token is probably fine; try again shortly. |

Tokens are short-lived — fifteen minutes by default. That is longer than most jobs and shorter
than some; see [Logins](#logins-for-sites-that-need-one) for what happens when a job outlives
one.

## Submitting a batch

```bash
curl -sS -X POST localhost:8000/v1/downloads \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"items": [{"name": "Severance", "season": 1, "episode": 3},
                 {"name": "Dune", "year": 2021}]}'
```

```json
{ "job_id": "6f1c…", "status": "queued", "item_count": 2,
  "created_at": "2026-09-10T12:00:00Z", "links": {"self": "/v1/downloads/6f1c…"} }
```

`202`, with `Location` and `Retry-After` headers. Work happens in the background.

### How items are interpreted

| Given | Inferred kind | Because |
| --- | --- | --- |
| `season` and/or `episode` | `series` | |
| `year` only | `movie` | |
| `name` only | `unknown` | The provider decides what to do with it. |

- `season` may be `0` — that means specials.
- `episode` may be given **without** a season: anime is routinely numbered absolutely.
- Names are whitespace-normalized for display and case-folded for identity, so
  `"  the   WIRE "` and `"The Wire"` are the same request.
- Unknown fields are rejected rather than ignored, so an invented parameter fails loudly.
- At most `MEDIA_TOOL_MAX_ITEMS_PER_REQUEST` (default 50) items per request.

### Logins for sites that need one

Some sites will not hand over a file without a login. Those are stored in keyring, not here,
and are read at the moment they are needed:

```json
{ "items": [{"name": "Dune", "year": 2021}], "profile": "default" }
```

`profile` names one of *your* credential profiles in keyring. It is optional — omit it and
`MEDIA_TOOL_DEFAULT_PROFILE` is used. It never names an account: whose credentials these are
comes from your token, so there is no profile name that reaches somebody else's.

Nothing about a credential comes back. It is resolved per attempt, typed into the site's form,
and held nowhere — not on the job, not in a response, not in a log record. See
[ADR-0009](adr/0009-credentials-per-attempt.md).

Two item-level failures are specific to this and worth handling distinctly:

| `error.code` | Means | What fixes it |
| --- | --- | --- |
| `reauthenticate` | Your token expired while the job was still running. | Sign in again and resubmit. |
| `credential_missing` | No stored login for that site on that profile, or one missing a field the site needs. | Connect the account in keyring, or add the missing field. |

Neither is retried — asking again with the same expired token cannot succeed.

### Duplicates

Items with the same canonical key are downloaded **once**. Every position still gets its own
result and its own working file link, so no client needs to know deduplication happened. A
deduplicated item reports `source_url` as `deduplicated://same-request`.

The year is deliberately excluded from a series key: the same episode submitted with and
without a year is still the same episode.

## Checking a job

```bash
curl -s "localhost:8000/v1/downloads/$JOB?wait_seconds=30"
```

`wait_seconds` (0–60, default 0) **long-polls**: the request returns as soon as the job
reaches a terminal state, or when the wait elapses — whichever is first. Either way you get
the job as it currently stands; a timeout is a normal outcome, not an error. Use it instead
of a polling loop.

### Job statuses

| Status | Meaning |
| --- | --- |
| `queued` | Accepted, not started. |
| `running` | At least one item in flight. |
| `succeeded` | Every item produced a file. |
| `partial` | Some produced files, some did not. A distinct outcome, not a kind of failure. |
| `failed` | No item produced a file. |
| `cancelled` | Abandoned. Items that already finished keep their outcome. |

### Item error codes

Stable and safe to branch on:

| Code | Meaning | Retried? |
| --- | --- | --- |
| `not_found` | The site has nothing matching. | **No** — looking again will not change that. |
| `timeout` | The download did not complete in time. | Yes |
| `provider_unavailable` | No browser, bad recipe, site unreachable. | Yes |
| `internal_error` | An unexpected fault. Check the logs with the request id. | Yes |

## Fetching a file

`GET /v1/downloads/{job_id}/items/{index}/file` streams the capture with
`Content-Disposition: attachment`. `404` if the item produced no file or the retention window
has removed it.

The reported `sha256` is of the stored bytes, computed while storing them — not read back
afterwards — so it is a genuine integrity check.

## Errors

Every failure is RFC 9457 `application/problem+json`:

```json
{ "type": "https://media-tool.invalid/problems/validation-failed",
  "title": "Validation failed", "status": 422,
  "detail": "the request body failed validation",
  "request_id": "d69c2bc5…",
  "errors": [{"location": "body.items.0.name", "message": "String should have at least 1 character"}] }
```

`request_id` is on every response, including a 500, and also in the `X-Request-ID` header.
Supply your own to trace a request across services. When something fails unexpectedly the
detail is deliberately vague — quote the request id and the logs will have the rest.

## Site recipes

A recipe describes how to get from a query to a download on one site. It is data, so a new
site needs no code. Full schema: `SiteRecipe` in `providers/browser/recipes.py`.

```json
{
  "name": "example",
  "start_url": "https://example.test/search?q={query}",
  "steps": [
    { "action": "fill",     "selector": "#q", "value": "{query}" },
    { "action": "click",    "selector": "#search" },
    { "action": "wait_for", "selector": ".result" }
  ],
  "result_selector": ".result",
  "match": { "text_contains": ["{name}", "{episode_tag}"] },
  "download_trigger": { "action": "click", "selector": ".result a.download" }
}
```

| Placeholder | Value |
| --- | --- |
| `{query}` | Full search string, e.g. `Severance S01E03` |
| `{name}` | The title alone |
| `{episode_tag}` | `S01E03`, or empty when not an episode |
| `{year}` | The year, or empty |

`match` runs **before** the trigger. Without it the click would land on whatever happened to
be first and the job would report success for the wrong file. A placeholder that resolves to
empty does not constrain, so `{episode_tag}` simply does not apply to a film.

Step-by-step guidance for writing one is in [AGENTS.md](../AGENTS.md#recipe-add-a-site-recipe).
